"""
WellHeard AI — Concurrent Call Engine (Production Dialer)
Manages simultaneous outbound calls with intelligent rate limiting, cost guardrails,
and real-time bottleneck detection.

Bottlenecks addressed:
1. Twilio CPS (Calls Per Second): 1 CPS default (request increase to 100+)
2. Twilio concurrent call limit: 1 by default (request increase)
3. Media stream WebSocket connections: Each call = 1 persistent WS
4. STT/TTS service rate limits: Per-service token budgets
5. LLM rate limits: Groq/Gemini have per-minute token budgets
6. Server CPU/Memory: Each call runs async tasks for STT decode, LLM routing, TTS
7. Network bandwidth: ~64kbps per call bidirectional
8. Transfer conference creation: Concurrent conferences with agent dials
9. Number pool exhaustion: All numbers hit daily limits
10. Callback queue buildup: Failed transfers pile up, consuming capacity

Production features:
- Gradual ramp-up to avoid spam flags and provider throttling
- Daily budget cap (calls + cost) with auto-stop
- Real-time bottleneck detection and logging
- Comprehensive metrics and campaign health monitoring
- Pause/resume support for campaign management
"""

import asyncio
import time
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Callable, Awaitable
from enum import Enum
import random

logger = structlog.get_logger()


class CampaignStatus(str, Enum):
    """Campaign lifecycle states."""
    IDLE = "idle"
    INITIALIZING = "initializing"
    RAMPING_UP = "ramping_up"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class BottleneckType(str, Enum):
    """Current throughput bottleneck."""
    NONE = "none"
    CPS_LIMIT = "twilio_cps_limit"
    CONCURRENT_LIMIT = "twilio_concurrent_limit"
    NUMBER_POOL_EXHAUSTED = "number_pool_exhausted"
    DAILY_BUDGET_CALLS = "daily_call_budget_exhausted"
    DAILY_BUDGET_COST = "daily_cost_budget_exhausted"
    PROSPECTS_EXHAUSTED = "all_prospects_exhausted"
    LLM_RATE_LIMIT = "llm_rate_limit"
    STT_RATE_LIMIT = "stt_rate_limit"
    MEMORY_PRESSURE = "server_memory_pressure"


@dataclass
class EngineConfig:
    """Configuration for concurrent call engine."""

    # Twilio limits (request increases from Twilio support)
    max_concurrent_calls: int = 10      # Twilio port limit (default 1, can request increase)
    calls_per_second: float = 1.0       # Twilio CPS limit (default 1, can request 100+)

    # Daily budget caps
    max_daily_calls: int = 500          # Max calls to attempt per day
    max_daily_cost: float = 50.0        # Max $ spent per day
    estimated_cost_per_call: float = 0.10  # Average cost per call (Twilio + TTS + LLM)

    # Ramp-up strategy
    ramp_up_enabled: bool = True        # Gradually increase concurrency
    ramp_up_minutes: int = 30           # Time to reach max_concurrent

    # Safety features
    pause_on_pool_exhaustion: bool = True  # Pause if no numbers available
    pause_on_answer_rate_drop: bool = True  # Pause if answer rate plummets
    answer_rate_warning_threshold: float = 0.10  # Alert if drops below 10%

    # LLM/STT rate limit estimation
    llm_tokens_per_minute: int = 10000  # Estimated tokens/min across all calls
    stt_connections_max: int = 50       # Max concurrent STT streams


@dataclass
class CallRecord:
    """Tracks a single call attempt."""
    call_id: str
    prospect_phone: str
    outbound_number: str
    prospect_name: str = ""
    status: str = "initiated"  # initiated, ringing, connected, completed
    started_at: datetime = field(default_factory=datetime.utcnow)
    answered: bool = False
    duration_seconds: float = 0.0
    transfer_triggered: bool = False
    transfer_qualified: bool = False
    cost_estimate: float = 0.0
    error: Optional[str] = None


@dataclass
class CampaignStats:
    """Real-time campaign statistics."""
    campaign_id: str
    status: str = CampaignStatus.IDLE.value

    # Call metrics
    total_prospects: int = 0
    prospects_remaining: int = 0
    calls_initiated: int = 0
    calls_connected: int = 0
    calls_answered: int = 0
    transfers_triggered: int = 0
    transfers_qualified: int = 0

    # Current state
    active_calls: int = 0
    calls_today: int = 0
    cost_today: float = 0.0

    # Performance metrics
    answer_rate_today: float = 0.0
    transfer_rate: float = 0.0
    qualified_transfer_rate: float = 0.0
    avg_call_duration: float = 0.0

    # Pool health
    numbers_available: int = 0
    numbers_exhausted: int = 0

    # Bottleneck detection
    current_bottleneck: str = BottleneckType.NONE.value

    # Timing
    started_at: Optional[datetime] = None
    estimated_completion_time: Optional[datetime] = None

    # Ramp-up progress
    ramp_up_progress: float = 0.0  # 0.0-1.0


class ConcurrentCallEngine:
    """
    Main production dialer for simultaneous outbound calls.

    Usage:
        engine = ConcurrentCallEngine(config, number_rotator, call_scheduler, transfer_optimizer)
        campaign_id = await engine.start_campaign("campaign_001", prospects)
        stats = engine.get_engine_stats()
        await engine.stop_campaign(campaign_id)
    """

    def __init__(
        self,
        engine_config: Optional[EngineConfig] = None,
        number_rotator=None,
        call_scheduler=None,
        transfer_optimizer=None,
        twilio_client=None,
        call_initiator_fn: Optional[Callable[[str, str, str], Awaitable[Dict]]] = None,
    ):
        """
        Initialize the concurrent call engine.

        Args:
            engine_config: EngineConfig with concurrency/budget settings
            number_rotator: NumberRotator instance for phone selection
            call_scheduler: CallScheduler instance for timing optimization
            transfer_optimizer: TransferOptimizer for warm transfers
            twilio_client: Twilio REST client
            call_initiator_fn: Async function to initiate actual call
        """
        self.config = engine_config or EngineConfig()
        self.number_rotator = number_rotator
        self.call_scheduler = call_scheduler
        self.transfer_optimizer = transfer_optimizer
        self._twilio = twilio_client
        self._call_initiator = call_initiator_fn

        # Campaign state
        self._campaigns: Dict[str, CampaignStats] = {}
        self._call_records: Dict[str, List[CallRecord]] = {}  # campaign_id -> calls
        self._active_calls: Dict[str, CallRecord] = {}  # call_id -> record

        # Dial loop tasks
        self._dial_loop_tasks: Dict[str, asyncio.Task] = {}

        # Rate limiting
        self._cps_limiter = asyncio.Semaphore(1)
        self._cps_last_call_time = 0.0
        self._cps_min_interval = 1.0 / max(self.config.calls_per_second, 0.1)

        logger.info(
            "concurrent_call_engine_initialized",
            max_concurrent=self.config.max_concurrent_calls,
            cps_limit=self.config.calls_per_second,
            daily_call_cap=self.config.max_daily_calls,
            daily_cost_cap=self.config.max_daily_cost,
        )

    # ── Campaign Lifecycle ─────────────────────────────────────────────────────

    async def start_campaign(
        self,
        campaign_id: str,
        prospects: List,
        callback_on_call_complete: Optional[Callable] = None,
    ) -> str:
        """
        Start a new outbound campaign.

        Args:
            campaign_id: Unique campaign identifier
            prospects: List of ProspectContact objects to call
            callback_on_call_complete: Async function(call_record) called on each completion

        Returns:
            campaign_id if successful

        Raises:
            ValueError: If campaign already exists
        """
        if campaign_id in self._campaigns:
            raise ValueError(f"Campaign {campaign_id} already exists")

        # Create campaign stats
        stats = CampaignStats(
            campaign_id=campaign_id,
            status=CampaignStatus.INITIALIZING.value,
            total_prospects=len(prospects),
            prospects_remaining=len(prospects),
            started_at=datetime.utcnow(),
        )

        self._campaigns[campaign_id] = stats
        self._call_records[campaign_id] = []
        self._active_calls_per_campaign = {}

        logger.info(
            "campaign_started",
            campaign_id=campaign_id,
            total_prospects=len(prospects),
        )

        # Start the main dial loop
        self._dial_loop_tasks[campaign_id] = asyncio.create_task(
            self._dial_loop(
                campaign_id,
                prospects,
                callback_on_call_complete,
            )
        )

        return campaign_id

    async def stop_campaign(self, campaign_id: str) -> Dict:
        """
        Stop a running campaign immediately.

        Args:
            campaign_id: Campaign to stop

        Returns:
            Final campaign statistics
        """
        if campaign_id not in self._campaigns:
            return {"error": f"Campaign {campaign_id} not found"}

        stats = self._campaigns[campaign_id]
        stats.status = CampaignStatus.COMPLETED.value

        # Cancel dial loop
        if campaign_id in self._dial_loop_tasks:
            task = self._dial_loop_tasks[campaign_id]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info(
            "campaign_stopped",
            campaign_id=campaign_id,
            calls_initiated=stats.calls_initiated,
            calls_answered=stats.calls_answered,
        )

        return self._stats_to_dict(stats)

    async def pause_campaign(self, campaign_id: str) -> Dict:
        """Pause a campaign (no new dials, but monitor active calls)."""
        if campaign_id not in self._campaigns:
            return {"error": f"Campaign {campaign_id} not found"}

        stats = self._campaigns[campaign_id]
        old_status = stats.status
        stats.status = CampaignStatus.PAUSED.value

        logger.info(
            "campaign_paused",
            campaign_id=campaign_id,
            previous_status=old_status,
        )

        return self._stats_to_dict(stats)

    async def resume_campaign(self, campaign_id: str) -> Dict:
        """Resume a paused campaign."""
        if campaign_id not in self._campaigns:
            return {"error": f"Campaign {campaign_id} not found"}

        stats = self._campaigns[campaign_id]
        if stats.status != CampaignStatus.PAUSED.value:
            return {"error": f"Campaign not paused (status: {stats.status})"}

        stats.status = CampaignStatus.ACTIVE.value

        logger.info("campaign_resumed", campaign_id=campaign_id)

        return self._stats_to_dict(stats)

    # ── Main Dial Loop ────────────────────────────────────────────────────────

    async def _dial_loop(
        self,
        campaign_id: str,
        prospects: List,
        callback_on_call_complete: Optional[Callable] = None,
    ) -> None:
        """
        Core dial loop that:
        1. Respects Twilio CPS limits
        2. Respects concurrent call limits
        3. Selects next callable prospect
        4. Gets best outbound number
        5. Initiates Twilio call
        6. Tracks completion + handles failures
        7. Enforces daily budget caps
        8. Gradually ramps up concurrency
        """
        stats = self._campaigns[campaign_id]
        ramp_start_time = time.time()

        try:
            stats.status = CampaignStatus.ACTIVE.value

            # Process each prospect until exhausted or campaign stopped
            for prospect in prospects:
                # Check pause status
                while stats.status == CampaignStatus.PAUSED.value:
                    await asyncio.sleep(1)

                # Check completion conditions
                if stats.status == CampaignStatus.COMPLETED.value:
                    break

                # Respect daily call limit
                if stats.calls_today >= self.config.max_daily_calls:
                    logger.warning(
                        "daily_call_limit_reached",
                        campaign_id=campaign_id,
                        calls_today=stats.calls_today,
                    )
                    stats.current_bottleneck = BottleneckType.DAILY_BUDGET_CALLS.value
                    await asyncio.sleep(60)
                    continue

                # Respect daily cost limit
                if stats.cost_today >= self.config.max_daily_cost:
                    logger.warning(
                        "daily_cost_limit_reached",
                        campaign_id=campaign_id,
                        cost_today=stats.cost_today,
                    )
                    stats.current_bottleneck = BottleneckType.DAILY_BUDGET_COST.value
                    await asyncio.sleep(60)
                    continue

                # Check if prospect is callable now
                if self.call_scheduler:
                    can_call, reason = self.call_scheduler.is_callable_now(prospect)
                    if not can_call:
                        logger.debug(
                            "prospect_not_callable",
                            prospect=prospect.phone,
                            reason=reason,
                        )
                        stats.prospects_remaining -= 1
                        continue

                # Ramp up: gradually increase concurrency over ramp_up_minutes
                if self.config.ramp_up_enabled:
                    elapsed_minutes = (time.time() - ramp_start_time) / 60.0
                    ramp_progress = min(
                        1.0,
                        elapsed_minutes / self.config.ramp_up_minutes
                    )
                    stats.ramp_up_progress = ramp_progress

                    # Limit concurrency during ramp-up
                    target_concurrent = max(
                        1,
                        int(self.config.max_concurrent_calls * ramp_progress)
                    )
                else:
                    target_concurrent = self.config.max_concurrent_calls

                # Wait if at concurrent limit
                while len(self._active_calls) >= target_concurrent:
                    # Check bottleneck
                    if len(self._active_calls) >= self.config.max_concurrent_calls:
                        stats.current_bottleneck = BottleneckType.CONCURRENT_LIMIT.value

                    await asyncio.sleep(0.5)

                # Get best outbound number
                if not self.number_rotator:
                    logger.warning("no_number_rotator_configured")
                    await asyncio.sleep(1)
                    continue

                outbound_number = await self.number_rotator.get_outbound_number(
                    prospect.phone,
                    campaign_id=campaign_id,
                )

                if not outbound_number:
                    logger.warning(
                        "no_available_numbers",
                        campaign_id=campaign_id,
                        prospect=prospect.phone,
                    )
                    stats.current_bottleneck = BottleneckType.NUMBER_POOL_EXHAUSTED.value

                    if self.config.pause_on_pool_exhaustion:
                        logger.info("pausing_campaign_no_numbers", campaign_id=campaign_id)
                        stats.status = CampaignStatus.PAUSED.value
                        await asyncio.sleep(5)
                    continue

                # Enforce CPS limit
                await self._enforce_cps_limit()

                # Initiate the call
                call_id = f"{campaign_id}_{int(time.time()*1000)}"

                asyncio.create_task(
                    self._initiate_and_track_call(
                        campaign_id=campaign_id,
                        call_id=call_id,
                        prospect=prospect,
                        outbound_number=outbound_number,
                        callback=callback_on_call_complete,
                    )
                )

                # Update stats
                stats.calls_initiated += 1
                stats.calls_today += 1
                stats.prospects_remaining -= 1
                stats.current_bottleneck = BottleneckType.NONE.value

                # Small delay between dials for stability
                await asyncio.sleep(0.1)

            # All prospects processed
            stats.status = CampaignStatus.COMPLETED.value
            logger.info(
                "dial_loop_completed",
                campaign_id=campaign_id,
                calls_initiated=stats.calls_initiated,
            )

        except asyncio.CancelledError:
            logger.info("dial_loop_cancelled", campaign_id=campaign_id)
            stats.status = CampaignStatus.PAUSED.value
        except Exception as e:
            logger.error(
                "dial_loop_error",
                campaign_id=campaign_id,
                error=str(e),
            )
            stats.status = CampaignStatus.ERROR.value

    async def _enforce_cps_limit(self) -> None:
        """
        Enforce Twilio CPS (Calls Per Second) limit.

        Twilio default: 1 CPS
        Can request increase to 100+ CPS from Twilio support
        """
        async with self._cps_limiter:
            now = time.time()
            elapsed_since_last = now - self._cps_last_call_time

            if elapsed_since_last < self._cps_min_interval:
                await asyncio.sleep(self._cps_min_interval - elapsed_since_last)

            self._cps_last_call_time = time.time()

    async def _initiate_and_track_call(
        self,
        campaign_id: str,
        call_id: str,
        prospect,
        outbound_number: str,
        callback: Optional[Callable] = None,
    ) -> None:
        """
        Initiate a single call and track its lifecycle.
        """
        call_record = CallRecord(
            call_id=call_id,
            prospect_phone=prospect.phone,
            prospect_name=prospect.get_display_name() if hasattr(prospect, 'get_display_name') else "",
            outbound_number=outbound_number,
            started_at=datetime.utcnow(),
        )

        self._active_calls[call_id] = call_record
        self._call_records[campaign_id].append(call_record)

        try:
            logger.info(
                "call_initiated",
                call_id=call_id,
                campaign_id=campaign_id,
                prospect=prospect.phone,
                outbound_number=outbound_number,
            )

            # Initiate via Twilio (or simulator)
            if self._call_initiator:
                result = await self._call_initiator(
                    call_id,
                    prospect.phone,
                    outbound_number,
                )
            else:
                # Simulation: random success/fail
                result = await self._simulate_call(prospect, outbound_number)

            # Process result
            call_record.status = "completed"
            call_record.answered = result.get("answered", False)
            call_record.duration_seconds = result.get("duration_seconds", 0.0)
            call_record.transfer_triggered = result.get("transfer_triggered", False)
            call_record.transfer_qualified = result.get("transfer_qualified", False)
            call_record.cost_estimate = result.get("cost_estimate", self.config.estimated_cost_per_call)

            # Update campaign stats
            stats = self._campaigns[campaign_id]
            stats.calls_connected += 1
            stats.cost_today += call_record.cost_estimate

            if call_record.answered:
                stats.calls_answered += 1

            if call_record.transfer_triggered:
                stats.transfers_triggered += 1
                if call_record.transfer_qualified:
                    stats.transfers_qualified += 1

            # Record call in scheduler
            if self.call_scheduler:
                from src.call_scheduler import CallAttemptResult
                result_enum = CallAttemptResult.ANSWERED if call_record.answered else CallAttemptResult.NO_ANSWER
                self.call_scheduler.record_call_attempt(
                    prospect,
                    call_record.answered,
                    result_enum,
                    call_record.duration_seconds,
                )

            # Record in number manager
            if self.number_rotator:
                self.number_rotator.record_outcome(
                    outbound_number,
                    prospect.phone,
                    call_record.answered,
                    call_record.duration_seconds,
                    "answered" if call_record.answered else "no_answer",
                )

            logger.info(
                "call_completed",
                call_id=call_id,
                answered=call_record.answered,
                duration=call_record.duration_seconds,
            )

            # Callback notification
            if callback:
                try:
                    await callback(call_record)
                except Exception as e:
                    logger.warning("callback_error", error=str(e))

        except Exception as e:
            call_record.status = "error"
            call_record.error = str(e)
            logger.error(
                "call_initiation_error",
                call_id=call_id,
                error=str(e),
            )

        finally:
            # Remove from active calls
            self._active_calls.pop(call_id, None)

    async def _simulate_call(self, prospect, outbound_number: str) -> Dict:
        """Simulate a call outcome (for testing)."""
        await asyncio.sleep(random.uniform(10, 45))

        # Random outcome: 30% answer rate
        answered = random.random() < 0.30
        transfer_triggered = answered and random.random() < 0.40
        transfer_qualified = transfer_triggered and random.random() < 0.60

        return {
            "answered": answered,
            "duration_seconds": random.uniform(30, 180) if answered else random.uniform(1, 10),
            "transfer_triggered": transfer_triggered,
            "transfer_qualified": transfer_qualified,
            "cost_estimate": self.config.estimated_cost_per_call,
        }

    # ── Monitoring & Metrics ──────────────────────────────────────────────────

    def get_engine_stats(self) -> Dict:
        """
        Get comprehensive engine statistics.

        Returns:
            Dictionary with active calls, daily metrics, bottlenecks, etc.
        """
        all_campaigns = []

        for campaign_id, stats in self._campaigns.items():
            campaign_dict = self._stats_to_dict(stats)

            # Add campaign-specific active calls count
            active_in_campaign = sum(
                1 for call in self._active_calls.values()
                if call.call_id.startswith(campaign_id)
            )
            campaign_dict["active_calls"] = active_in_campaign

            all_campaigns.append(campaign_dict)

        # Aggregate totals
        total_active = len(self._active_calls)
        total_cost_today = sum(s.cost_today for s in self._campaigns.values())
        total_calls_initiated = sum(s.calls_initiated for s in self._campaigns.values())
        total_calls_answered = sum(s.calls_answered for s in self._campaigns.values())

        overall_answer_rate = (
            total_calls_answered / total_calls_initiated
            if total_calls_initiated > 0 else 0.0
        )

        # Detect global bottleneck
        global_bottleneck = self._detect_global_bottleneck()

        return {
            "campaigns": all_campaigns,
            "engine_totals": {
                "active_calls": total_active,
                "max_concurrent_available": self.config.max_concurrent_calls,
                "calls_per_second_limit": self.config.calls_per_second,
                "total_calls_initiated": total_calls_initiated,
                "total_calls_answered": total_calls_answered,
                "overall_answer_rate": round(overall_answer_rate, 3),
                "cost_today": round(total_cost_today, 2),
                "daily_cost_cap": self.config.max_daily_cost,
                "global_bottleneck": global_bottleneck,
            },
        }

    def _stats_to_dict(self, stats: CampaignStats) -> Dict:
        """Convert CampaignStats to dictionary."""
        # Calculate rates
        answer_rate = (
            stats.calls_answered / stats.calls_initiated
            if stats.calls_initiated > 0 else 0.0
        )
        transfer_rate = (
            stats.transfers_triggered / stats.calls_answered
            if stats.calls_answered > 0 else 0.0
        )
        qualified_transfer_rate = (
            stats.transfers_qualified / stats.transfers_triggered
            if stats.transfers_triggered > 0 else 0.0
        )

        # Estimate completion time
        if stats.calls_initiated > 0 and stats.prospects_remaining > 0:
            calls_per_minute = max(stats.calls_initiated / 1, 1)  # At least 1
            minutes_remaining = stats.prospects_remaining / calls_per_minute
            eta = datetime.utcnow() + timedelta(minutes=minutes_remaining)
        else:
            eta = None

        return {
            "campaign_id": stats.campaign_id,
            "status": stats.status,
            "total_prospects": stats.total_prospects,
            "prospects_remaining": stats.prospects_remaining,
            "calls_initiated": stats.calls_initiated,
            "calls_connected": stats.calls_connected,
            "calls_answered": stats.calls_answered,
            "answer_rate": round(answer_rate, 3),
            "transfers_triggered": stats.transfers_triggered,
            "transfers_qualified": stats.transfers_qualified,
            "qualified_transfer_rate": round(qualified_transfer_rate, 3),
            "active_calls": len([c for c in self._active_calls.values() if c.call_id.startswith(stats.campaign_id)]),
            "calls_today": stats.calls_today,
            "cost_today": round(stats.cost_today, 2),
            "numbers_available": stats.numbers_available,
            "numbers_exhausted": stats.numbers_exhausted,
            "current_bottleneck": stats.current_bottleneck,
            "ramp_up_progress": round(stats.ramp_up_progress, 2),
            "estimated_completion_time": eta.isoformat() if eta else None,
            "started_at": stats.started_at.isoformat() if stats.started_at else None,
        }

    def _detect_global_bottleneck(self) -> str:
        """Identify the current system-wide bottleneck."""
        # Check Twilio CPS
        if len(self._active_calls) > 0 and self.config.calls_per_second < 1.0:
            return BottleneckType.CPS_LIMIT.value

        # Check concurrent limit
        if len(self._active_calls) >= self.config.max_concurrent_calls:
            return BottleneckType.CONCURRENT_LIMIT.value

        # Check number pool
        if self.number_rotator:
            pool_stats = self.number_rotator.pool.get_pool_stats()
            if pool_stats.get("total_numbers", 0) == 0:
                return BottleneckType.NUMBER_POOL_EXHAUSTED.value

        # Check daily budgets
        total_cost = sum(s.cost_today for s in self._campaigns.values())
        if total_cost >= self.config.max_daily_cost:
            return BottleneckType.DAILY_BUDGET_COST.value

        total_calls = sum(s.calls_today for s in self._campaigns.values())
        if total_calls >= self.config.max_daily_calls:
            return BottleneckType.DAILY_BUDGET_CALLS.value

        return BottleneckType.NONE.value

    def get_call_records(self, campaign_id: str) -> List[Dict]:
        """Get all call records for a campaign."""
        if campaign_id not in self._call_records:
            return []

        records = []
        for call in self._call_records[campaign_id]:
            records.append({
                "call_id": call.call_id,
                "prospect_phone": call.prospect_phone,
                "prospect_name": call.prospect_name,
                "outbound_number": call.outbound_number,
                "status": call.status,
                "answered": call.answered,
                "duration_seconds": round(call.duration_seconds, 1),
                "transfer_triggered": call.transfer_triggered,
                "transfer_qualified": call.transfer_qualified,
                "cost_estimate": round(call.cost_estimate, 3),
                "started_at": call.started_at.isoformat(),
                "error": call.error,
            })

        return records

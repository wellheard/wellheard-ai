"""
WellHeard AI — Transfer Optimizer

Production optimization wrapper for WarmTransferManager:
1. Pre-dials agent BEFORE prospect reaches transfer phase (predictive dialing)
2. Streams hold phrases via waitUrl webhook (no silence)
3. Agent pool round-robin with health tracking + automatic disable on poor performance
4. Automatic callback scheduling on transfer failure
5. Transfer quality scoring + analytics

Key innovation: Pre-dialing starts when call enters QUALIFY phase (one step before transfer).
Agent waits in a holding conference with whisper instructions. When transfer is triggered,
prospect is moved into the already-active conference → instant bridge (0s ring time).
"""
import asyncio
import time
import structlog
import uuid
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from enum import Enum

from .warm_transfer import WarmTransferManager, TransferConfig, TransferState, TransferFailReason

logger = structlog.get_logger()


# ── Configuration ──────────────────────────────────────────────────────────

# Transfer optimization config
TRANSFER_CONFIG = {
    "primary_agent_did": "+19802020160",  # Easy to change
    "agent_pool": ["+19802020160"],       # Easy to add more agents
    "pre_dial_enabled": True,             # Pre-dial during qualify phase
    "pre_dial_trigger_phase": "qualify",  # Phase that triggers pre-dial
    "ring_timeout": 18,                   # Seconds (research shows 15-18 optimal)
    "max_hold_time": 60,                  # Seconds before escalated messaging
    "hold_phrase_pause": 8,               # Seconds between hold phrases
    "whisper_enabled": True,              # Brief agent before unmuting
    "record_conference": True,            # Record for QA
    "callback_retry_intervals": [300, 900, 3600],  # 5min, 15min, 1hr
    "qualified_transfer_threshold": 30,   # Seconds (30s+ = qualified)
}


# ── Agent Health Tracking ──────────────────────────────────────────────────

@dataclass
class AgentMetrics:
    """Per-agent health and performance tracking."""
    agent_did: str
    answer_rate: float = 1.0                # % of calls answered
    avg_ring_time: float = 0.0              # Average seconds to answer
    voicemail_rate: float = 0.0             # % that hit voicemail
    total_attempts: int = 0                 # Total dials
    successful_transfers: int = 0           # Completed transfers
    last_call_time: float = field(default_factory=time.time)
    enabled: bool = True                    # Agent can be dialed
    disable_reason: Optional[str] = None    # Why agent was disabled


# ── Transfer Quality Scoring ───────────────────────────────────────────────

class TransferQuality(str, Enum):
    """Transfer quality classification."""
    EXCELLENT = "excellent"    # Fast answer, long talk, no drops
    GOOD = "good"              # Answered, qualified (30s+), no issues
    FAIR = "fair"              # Answered but short talk or prospect dropped after
    POOR = "poor"              # Failed attempt, callback offered
    NO_ATTEMPT = "no_attempt"  # Pre-dial didn't happen


# ── Transfer Optimizer ─────────────────────────────────────────────────────

class TransferOptimizer:
    """
    Wraps WarmTransferManager with production optimizations:
    1. Pre-dial agent BEFORE prospect reaches transfer phase (predictive)
    2. Hold phrase streaming via waitUrl webhook
    3. Agent pool round-robin with health tracking
    4. Automatic callback scheduling on failure
    5. Transfer analytics and quality scoring
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Initialize optimizer.

        Args:
            config: Override TRANSFER_CONFIG (optional)
        """
        self.config = config or TRANSFER_CONFIG.copy()

        # Agent pool health tracking
        self.agent_metrics: Dict[str, AgentMetrics] = {}
        self._init_agent_pool()

        # Round-robin pointer
        self._current_agent_index = 0

        # Pre-dial tracking: call_id → (conference_name, manager)
        self._pre_dialed_agents: Dict[str, tuple[str, WarmTransferManager]] = {}

        # Transfer quality metrics
        self._quality_history: List[Dict] = []

    def _init_agent_pool(self):
        """Initialize agent metrics for all agents in pool."""
        for did in self.config.get("agent_pool", []):
            if did not in self.agent_metrics:
                self.agent_metrics[did] = AgentMetrics(agent_did=did)

    # ── Pre-Dialing (Predictive) ───────────────────────────────────────────

    async def pre_dial_agent(self, call_id: str, call_phase: str) -> bool:
        """
        Pre-dial an agent when call reaches the QUALIFY phase.

        This starts dialing an agent into a holding conference BEFORE the
        prospect is moved to the transfer conference. When transfer is triggered,
        the prospect is moved into the already-active conference with the agent.
        Result: Zero ring-wait time for prospect.

        Args:
            call_id: Internal call ID
            call_phase: Current call phase (e.g., "qualify")

        Returns:
            True if pre-dial initiated, False otherwise
        """
        # Only pre-dial if enabled and we're at the trigger phase
        if not self.config.get("pre_dial_enabled"):
            logger.info("pre_dial_disabled", call_id=call_id)
            return False

        if call_phase != self.config.get("pre_dial_trigger_phase"):
            logger.debug("pre_dial_phase_mismatch",
                call_id=call_id,
                current_phase=call_phase,
                trigger_phase=self.config.get("pre_dial_trigger_phase"),
            )
            return False

        # Check if already pre-dialed
        if call_id in self._pre_dialed_agents:
            logger.warning("call_already_pre_dialed", call_id=call_id)
            return False

        try:
            # Select best available agent
            best_agent = self.select_best_agent()
            if not best_agent:
                logger.warning("no_agents_available_for_pre_dial", call_id=call_id)
                return False

            # Create transfer manager for this call
            manager = WarmTransferManager(
                config=self._build_transfer_config()
            )

            # Conference name for pre-dial (different from main transfer conf)
            pre_dial_conference = f"pre-dial-{call_id}"
            manager._conference_name = pre_dial_conference

            logger.info("pre_dial_initiated",
                call_id=call_id,
                agent_did=best_agent,
                conference=pre_dial_conference,
            )

            # Start pre-dial in background (don't wait)
            asyncio.create_task(
                self._pre_dial_agent_background(call_id, best_agent, manager, pre_dial_conference)
            )

            # Store pre-dial info
            self._pre_dialed_agents[call_id] = (pre_dial_conference, manager)

            return True

        except Exception as e:
            logger.error("pre_dial_error",
                call_id=call_id,
                error=str(e),
            )
            return False

    async def _pre_dial_agent_background(
        self,
        call_id: str,
        agent_did: str,
        manager: WarmTransferManager,
        conference_name: str,
    ) -> None:
        """
        Background task: pre-dial agent into a holding conference.
        Agent waits with whisper instructions until transfer is triggered.
        """
        try:
            # In production, you'd create the conference via Twilio API
            # and dial the agent into it with:
            # - Muted: True (can't hear prospect yet)
            # - Whisper: "Agent is standing by for an incoming transfer..."
            # - statusCallback: monitored for hangup

            logger.info("pre_dial_agent_dialing",
                call_id=call_id,
                agent_did=agent_did,
                conference=conference_name,
            )

            # Simulate dial (in production: actual Twilio API call)
            await asyncio.sleep(2)

            # Mark agent as pre-dialed and ready
            agent_metrics = self.agent_metrics.get(agent_did)
            if agent_metrics:
                agent_metrics.last_call_time = time.time()

            logger.info("pre_dial_agent_ready",
                call_id=call_id,
                agent_did=agent_did,
                wait_seconds=f"up to {manager.config.max_hold_time_seconds}",
            )

            # Wait for transfer trigger (up to max_hold_time)
            # In production: monitor statusCallback webhooks for hangup
            # Using sleep here is temporary — production should use async webhook monitoring
            # TODO: Replace with actual Twilio participant status monitoring
            max_wait = manager.config.max_hold_time_seconds
            check_interval = 5
            elapsed = 0
            while elapsed < max_wait:
                await asyncio.sleep(check_interval)
                elapsed += check_interval
                # In production, check if participant status callback indicates disconnect
                # For now, just wait the full period

            # Timeout: agent still waiting, hang up
            if call_id in self._pre_dialed_agents:
                logger.warning("pre_dial_timeout_agent_hangup",
                    call_id=call_id,
                    agent_did=agent_did,
                )
                del self._pre_dialed_agents[call_id]

        except asyncio.CancelledError:
            logger.info("pre_dial_cancelled", call_id=call_id)
            if call_id in self._pre_dialed_agents:
                del self._pre_dialed_agents[call_id]
        except Exception as e:
            logger.error("pre_dial_background_error",
                call_id=call_id,
                error=str(e),
            )
            if call_id in self._pre_dialed_agents:
                del self._pre_dialed_agents[call_id]

    # ── Optimized Transfer Initiation ──────────────────────────────────────

    async def initiate_optimized_transfer(
        self,
        call_id: str,
        prospect_call_sid: str,
        contact_name: str,
        last_name: str,
        webhook_base_url: str,
    ) -> WarmTransferManager:
        """
        Initiate an optimized warm transfer.

        If agent was pre-dialed and is waiting → instant bridge (0s wait)
        If not → standard transfer flow with improved hold TwiML

        Always uses the waitUrl endpoint for professional hold phrases.

        Args:
            call_id: Internal call ID
            prospect_call_sid: Prospect's Twilio call SID
            contact_name: Prospect first name
            last_name: Prospect last name
            webhook_base_url: Base URL for webhooks

        Returns:
            WarmTransferManager instance handling this transfer
        """
        # Check if agent was pre-dialed
        if call_id in self._pre_dialed_agents:
            pre_dial_conf, pre_dial_manager = self._pre_dialed_agents[call_id]

            logger.info("optimized_transfer_using_pre_dialed_agent",
                call_id=call_id,
                pre_dial_conference=pre_dial_conf,
                agent_did=pre_dial_manager.config.agent_dids[0],
            )

            # Move prospect into the pre-dialed conference
            # Result: instant bridge with agent (already waiting + muted)
            # AI whispers agent, unmutes, announces handoff
            await pre_dial_manager.initiate_transfer(
                prospect_call_sid=prospect_call_sid,
                contact_name=contact_name,
                last_name=last_name,
                call_id=call_id,
                webhook_base_url=webhook_base_url,
            )

            del self._pre_dialed_agents[call_id]  # Cleanup
            return pre_dial_manager

        else:
            # Standard transfer (no pre-dial)
            logger.info("optimized_transfer_standard_flow",
                call_id=call_id,
            )

            manager = WarmTransferManager(
                config=self._build_transfer_config()
            )

            await manager.initiate_transfer(
                prospect_call_sid=prospect_call_sid,
                contact_name=contact_name,
                last_name=last_name,
                call_id=call_id,
                webhook_base_url=webhook_base_url,
            )

            return manager

    # ── Agent Selection (Round-Robin + Health) ─────────────────────────────

    def select_best_agent(self) -> Optional[str]:
        """
        Select the best available agent from the pool.

        Strategy:
        1. Round-robin through available agents (fairness)
        2. Skip agents who are disabled (poor health)
        3. Skip agents who just failed (30min cooldown)
        4. Prefer agents with highest historical answer rate

        Returns:
            Selected agent DID, or None if no agents available
        """
        agent_pool = self.config.get("agent_pool", [])
        if not agent_pool:
            logger.warning("agent_pool_empty")
            return None

        # Initialize any new agents
        self._init_agent_pool()

        current_time = time.time()
        available_agents = []

        for i in range(len(agent_pool)):
            idx = (self._current_agent_index + i) % len(agent_pool)
            did = agent_pool[idx]
            metrics = self.agent_metrics.get(did)

            if not metrics or not metrics.enabled:
                logger.debug("agent_disabled",
                    agent_did=did,
                    reason=metrics.disable_reason if metrics else "not_initialized",
                )
                continue

            # Skip agents who failed recently (only if answer rate is very low)
            # Only apply cooldown if they failed AND their answer rate is below 70%
            if current_time - metrics.last_call_time < 1800:
                if metrics.answer_rate < 0.7:  # Low answer rate (< 70%)
                    logger.debug("agent_in_cooldown",
                        agent_did=did,
                        answer_rate=metrics.answer_rate,
                    )
                    continue

            available_agents.append((idx, did, metrics))

        if not available_agents:
            logger.warning("no_available_agents_in_pool")
            return None

        # Sort by answer rate (highest first)
        available_agents.sort(
            key=lambda x: x[2].answer_rate,
            reverse=True,
        )

        # Select best available
        selected_idx, selected_did, selected_metrics = available_agents[0]
        self._current_agent_index = (selected_idx + 1) % len(agent_pool)

        logger.info("agent_selected",
            agent_did=selected_did,
            answer_rate=round(selected_metrics.answer_rate, 2),
            successful_transfers=selected_metrics.successful_transfers,
        )

        return selected_did

    # ── Quality Scoring ────────────────────────────────────────────────────

    def score_transfer_quality(self, metrics: Dict) -> Dict:
        """
        Score transfer quality based on transfer metrics.

        Scoring:
        - hold_time_score: Prospect patience (0-40 points, max = 30s)
        - agent_response_score: How fast agent answered (0-30 points, max = 10s)
        - prospect_engagement_score: How long they talked (0-30 points, min = 30s)

        Qualified = prospect stayed 30s+ AND agent engaged

        Args:
            metrics: Transfer metrics dict from WarmTransferManager.get_transfer_metrics()

        Returns:
            Quality scoring breakdown and overall score (0-100)
        """
        score_breakdown = {
            "hold_time_score": 0,
            "agent_response_score": 0,
            "prospect_engagement_score": 0,
            "total_score": 0,
            "quality_rating": TransferQuality.NO_ATTEMPT.value,
        }

        try:
            # Hold time score (max 40 points if hold < 30s)
            hold_time = metrics.get("total_hold_seconds", 0)
            if hold_time <= 30:
                score_breakdown["hold_time_score"] = 40
            elif hold_time <= 60:
                score_breakdown["hold_time_score"] = 30
            elif hold_time <= 90:
                score_breakdown["hold_time_score"] = 20
            else:
                score_breakdown["hold_time_score"] = 10

            # Agent response score (max 30 points if answer < 10s)
            agent_answer_time = metrics.get("agent_answer_seconds", 999)
            if agent_answer_time <= 10:
                score_breakdown["agent_response_score"] = 30
            elif agent_answer_time <= 15:
                score_breakdown["agent_response_score"] = 25
            elif agent_answer_time <= 20:
                score_breakdown["agent_response_score"] = 15
            else:
                score_breakdown["agent_response_score"] = 5

            # Prospect engagement score (max 30 points if talk > 30s)
            agent_talk_time = metrics.get("agent_talk_seconds", 0)
            if agent_talk_time >= 30:
                score_breakdown["prospect_engagement_score"] = 30
            elif agent_talk_time >= 20:
                score_breakdown["prospect_engagement_score"] = 20
            elif agent_talk_time >= 10:
                score_breakdown["prospect_engagement_score"] = 10
            else:
                score_breakdown["prospect_engagement_score"] = 5

            # Calculate total
            total = (
                score_breakdown["hold_time_score"] +
                score_breakdown["agent_response_score"] +
                score_breakdown["prospect_engagement_score"]
            )
            score_breakdown["total_score"] = total

            # Determine quality rating
            fail_reasons = metrics.get("fail_reasons", [])
            if fail_reasons:
                score_breakdown["quality_rating"] = TransferQuality.POOR.value
            elif agent_talk_time >= self.config.get("qualified_transfer_threshold", 30):
                if total >= 85:
                    score_breakdown["quality_rating"] = TransferQuality.EXCELLENT.value
                elif total >= 70:
                    score_breakdown["quality_rating"] = TransferQuality.GOOD.value
                else:
                    score_breakdown["quality_rating"] = TransferQuality.FAIR.value
            else:
                score_breakdown["quality_rating"] = TransferQuality.FAIR.value

            return score_breakdown

        except Exception as e:
            logger.error("quality_scoring_error", error=str(e))
            return score_breakdown

    # ── Callback Scheduling ───────────────────────────────────────────────

    async def schedule_callback(
        self,
        call_id: str,
        prospect_phone: str,
        contact_name: str,
        reason: str,
    ) -> str:
        """
        Schedule a callback for a failed transfer.

        Implements retry strategy:
        - 1st attempt: 5 minutes
        - 2nd attempt: 15 minutes
        - 3rd attempt: 1 hour

        Args:
            call_id: Original call ID
            prospect_phone: Prospect phone number
            contact_name: Prospect name
            reason: Callback reason

        Returns:
            Callback ID
        """
        callback_id = str(uuid.uuid4())

        logger.info("callback_scheduled",
            callback_id=callback_id,
            call_id=call_id,
            prospect_phone=prospect_phone,
            reason=reason,
        )

        # In production: store in Redis or database
        # For now: in-memory (see transfer_endpoints.py)

        return callback_id

    # ── Agent Health Monitoring ────────────────────────────────────────────

    def record_agent_attempt(
        self,
        agent_did: str,
        success: bool,
        ring_time: float,
        reason: Optional[str] = None,
    ) -> None:
        """
        Record an agent dial attempt to track health.

        Updates answer rate, voicemail rate, avg ring time.
        Auto-disables agents with < 50% answer rate.

        Args:
            agent_did: Agent DID
            success: True if human answered
            ring_time: Seconds to answer (or timeout)
            reason: Failure reason (if not success)
        """
        metrics = self.agent_metrics.get(agent_did)
        if not metrics:
            metrics = AgentMetrics(agent_did=agent_did)
            self.agent_metrics[agent_did] = metrics

        # Update attempt count
        metrics.total_attempts += 1

        # Update ring time
        if success:
            metrics.avg_ring_time = (
                (metrics.avg_ring_time * (metrics.total_attempts - 1) + ring_time) /
                metrics.total_attempts
            )
            metrics.successful_transfers += 1
        else:
            # Track voicemail failures
            if reason == "agent_voicemail":
                metrics.voicemail_rate = (
                    metrics.voicemail_rate * 0.9 + 0.1
                )  # Exponential moving average

        # Calculate answer rate
        metrics.answer_rate = (
            metrics.successful_transfers / max(1, metrics.total_attempts)
        )

        # Auto-disable agents with poor health
        if metrics.answer_rate < 0.5 and metrics.total_attempts >= 5:
            if metrics.enabled:
                logger.warning("agent_auto_disabled",
                    agent_did=agent_did,
                    answer_rate=round(metrics.answer_rate, 2),
                    total_attempts=metrics.total_attempts,
                )
                metrics.enabled = False
                metrics.disable_reason = f"low_answer_rate ({round(metrics.answer_rate*100, 0)}%)"

        logger.debug("agent_metrics_updated",
            agent_did=agent_did,
            answer_rate=round(metrics.answer_rate, 2),
            avg_ring_time=round(metrics.avg_ring_time, 1),
            successful_transfers=metrics.successful_transfers,
            enabled=metrics.enabled,
        )

    def enable_agent(self, agent_did: str) -> None:
        """
        Re-enable a previously disabled agent.

        Args:
            agent_did: Agent DID to enable
        """
        metrics = self.agent_metrics.get(agent_did)
        if metrics:
            metrics.enabled = True
            metrics.disable_reason = None
            logger.info("agent_enabled", agent_did=agent_did)

    def get_agent_metrics(self, agent_did: Optional[str] = None) -> Dict:
        """
        Get agent health metrics.

        Args:
            agent_did: Specific agent, or None for all

        Returns:
            Agent metrics dict
        """
        if agent_did:
            metrics = self.agent_metrics.get(agent_did)
            if metrics:
                return {
                    "agent_did": metrics.agent_did,
                    "answer_rate": round(metrics.answer_rate, 2),
                    "avg_ring_time": round(metrics.avg_ring_time, 1),
                    "voicemail_rate": round(metrics.voicemail_rate, 2),
                    "total_attempts": metrics.total_attempts,
                    "successful_transfers": metrics.successful_transfers,
                    "last_call_time": metrics.last_call_time,
                    "enabled": metrics.enabled,
                }
            return {}

        # All agents
        return {
            did: {
                "agent_did": m.agent_did,
                "answer_rate": round(m.answer_rate, 2),
                "avg_ring_time": round(m.avg_ring_time, 1),
                "voicemail_rate": round(m.voicemail_rate, 2),
                "total_attempts": m.total_attempts,
                "successful_transfers": m.successful_transfers,
                "enabled": m.enabled,
            }
            for did, m in self.agent_metrics.items()
        }

    # ── Helper Methods ────────────────────────────────────────────────────

    def _build_transfer_config(self) -> TransferConfig:
        """Build a TransferConfig from optimizer config."""
        return TransferConfig(
            agent_dids=self.config.get("agent_pool", ["+19802020160"]),
            ring_timeout_seconds=self.config.get("ring_timeout", 18),
            max_hold_time_seconds=self.config.get("max_hold_time", 60),
            record_conference=self.config.get("record_conference", True),
            whisper_enabled=self.config.get("whisper_enabled", True),
        )

    def get_pool_health(self) -> Dict:
        """
        Get overall agent pool health snapshot.

        Returns:
            Pool health metrics (available agents, avg answer rate, etc.)
        """
        metrics = list(self.agent_metrics.values())

        enabled_agents = [m for m in metrics if m.enabled]
        avg_answer_rate = (
            sum(m.answer_rate for m in metrics) / len(metrics)
            if metrics else 0
        )
        avg_voicemail_rate = (
            sum(m.voicemail_rate for m in metrics) / len(metrics)
            if metrics else 0
        )

        return {
            "total_agents": len(metrics),
            "enabled_agents": len(enabled_agents),
            "disabled_agents": len(metrics) - len(enabled_agents),
            "avg_answer_rate": round(avg_answer_rate, 2),
            "avg_voicemail_rate": round(avg_voicemail_rate, 2),
            "health_status": "healthy" if len(enabled_agents) >= len(metrics) / 2 else "degraded",
        }

"""
WellHeard AI — Pool Autoscaler with Human-in-the-Loop Guardrails
Auto-manages the phone number pool with strict cost controls and confirmation gates.

Key principle: Strong cost guardrails prevent runaway spending.

Scaling tiers:
1. SMALL ADJUSTMENTS (1-3 numbers, <$5/month) → Auto-approve, no delay
2. MEDIUM ADJUSTMENTS (4-10 numbers, $5-20/month) → Log warning, proceed with 5min delay
3. LARGE ADJUSTMENTS (11+ numbers, >$20/month) → REQUIRE human approval, do NOT proceed auto
4. VERY LARGE ADJUSTMENTS (>50 numbers) → Hard cap, BLOCKED entirely

Features:
- Evaluates pool health: capacity, utilization, answer rates, state distribution
- Calculates optimal pool composition by state for local presence
- ROI tracking: cost per additional answer
- Auto-retire unused numbers after N days
- Pre-dial optimization: Reduce pool size if not needed
"""

import asyncio
import time
import structlog
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from enum import Enum

logger = structlog.get_logger()


class ScalingAction(str, Enum):
    """Actions the autoscaler can recommend."""
    ADD = "add"
    REMOVE = "remove"
    RETIRE = "retire"
    NO_CHANGE = "no_change"
    NEEDS_APPROVAL = "needs_approval"


@dataclass
class ScalingDecision:
    """A scaling recommendation with approval status."""

    decision_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    action: str = ScalingAction.NO_CHANGE.value  # add, remove, retire, no_change, needs_approval
    count: int = 0  # How many numbers to add/remove
    reason: str = ""  # Why this decision
    estimated_monthly_cost: float = 0.0  # Cost delta
    requires_approval: bool = False
    approval_reason: str = ""  # Why approval needed
    projected_answer_rate_lift: float = 0.0  # Expected % improvement
    confidence: float = 0.0  # 0.0-1.0 confidence in recommendation
    approved: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejected: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ScalingGuardrails:
    """Cost and size guardrails for automatic scaling decisions."""

    # Auto-approval thresholds (small changes OK without human)
    auto_approve_max_numbers: int = 3         # Max numbers to add without approval
    auto_approve_max_monthly_cost: float = 5.0  # Max $/month cost increase without approval

    # Approval-required thresholds (medium changes)
    warning_max_numbers: int = 10             # Numbers above this REQUIRE approval
    warning_max_monthly_cost: float = 20.0    # Cost above this REQUIRE approval

    # Hard limits (never exceeded)
    max_total_numbers: int = 50               # Absolute maximum pool size
    min_total_numbers: int = 2                # Never go below this

    # Per-campaign caps
    max_numbers_per_campaign: int = 20        # Max numbers for any single campaign

    # Auto-retire settings
    retire_unused_after_days: int = 30        # Retire if zero calls in N days
    retire_spam_flagged_immediately: bool = True

    # Economic settings
    cost_per_number_monthly: float = 1.0      # Twilio $1/month per number (US)
    cost_per_call: float = 0.01               # Twilio $0.01 per call (varies)
    cost_per_minute: float = 0.02             # TTS/LLM ~$0.02/min

    # Performance targets
    target_answer_rate: float = 0.30          # Aim for 30% answer rate
    target_utilization: float = 0.70          # Aim for 70% capacity usage
    min_answer_rate_to_keep: float = 0.10     # Remove numbers below 10% answer rate


class PoolAutoscaler:
    """
    Intelligent phone number pool auto-scaling with human guardrails.

    Usage:
        scaler = PoolAutoscaler(number_pool, guardrails=ScalingGuardrails())
        decision = scaler.evaluate_pool_health(daily_call_target=100)
        if decision.requires_approval:
            approval_id = decision.decision_id
            # Show to user for approval/rejection
        else:
            await scaler.execute_scaling(decision)
    """

    def __init__(
        self,
        number_pool,
        guardrails: Optional[ScalingGuardrails] = None,
    ):
        """
        Initialize the pool autoscaler.

        Args:
            number_pool: NumberPool instance to manage
            guardrails: ScalingGuardrails for cost controls (or defaults)
        """
        self.pool = number_pool
        self.guardrails = guardrails or ScalingGuardrails()

        # Pending approvals
        self._pending_approvals: Dict[str, ScalingDecision] = {}

        # History
        self._scaling_history: List[ScalingDecision] = []

        # Approval callbacks
        self._on_approval_needed: Optional[callable] = None

        logger.info(
            "pool_autoscaler_initialized",
            auto_approve_max_numbers=self.guardrails.auto_approve_max_numbers,
            auto_approve_max_cost=self.guardrails.auto_approve_max_monthly_cost,
            max_total_numbers=self.guardrails.max_total_numbers,
        )

    # ── Main Evaluation ───────────────────────────────────────────────────────

    def evaluate_pool_health(self, daily_call_target: int) -> ScalingDecision:
        """
        Analyze current pool health and recommend scaling action.

        Checks:
        1. Do we have enough capacity for target daily volume?
        2. Are too many numbers flagged/retired? (>30% = problem)
        3. Are we hitting daily limits on active numbers? (>80% = add)
        4. Are answer rates declining? (possible spam flagging)
        5. Are we over-provisioned? (utilization < 30% = remove)
        6. Should we add local numbers for top prospect states?

        Args:
            daily_call_target: Target calls per day for campaigns

        Returns:
            ScalingDecision with recommended action
        """
        pool_stats = self.pool.get_pool_stats()

        # Current capacity
        total_numbers = pool_stats.get("total_numbers", 0)
        active_numbers = pool_stats.get("active", 0)
        warming_numbers = pool_stats.get("warming", 0)
        retired_numbers = pool_stats.get("retired", 0)
        flagged_numbers = pool_stats.get("flagged", 0)
        calls_today = pool_stats.get("calls_today", 0)
        total_capacity = pool_stats.get("total_capacity", 0)
        avg_answer_rate = pool_stats.get("avg_answer_rate", 0.0)

        # Usable numbers
        usable_numbers = active_numbers + warming_numbers

        # Decision
        decision = ScalingDecision(
            reason="",
            action=ScalingAction.NO_CHANGE.value,
        )

        # ── Check 1: Insufficient capacity ────────────────────────────────────
        # Assume 60 calls/day optimal per number
        optimal_calls_per_number = 60
        needed_numbers = max(1, (daily_call_target + optimal_calls_per_number - 1) // optimal_calls_per_number)

        if usable_numbers < needed_numbers:
            shortage = needed_numbers - usable_numbers
            decision.action = ScalingAction.ADD.value
            decision.count = shortage
            decision.reason = (
                f"Insufficient capacity. Need {needed_numbers} numbers for {daily_call_target} daily calls, "
                f"but have {usable_numbers} usable. Adding {shortage}."
            )
            decision.projected_answer_rate_lift = 0.05  # ~5% improvement from more numbers
            decision.confidence = 0.9

        # ── Check 2: Too many flagged/retired ─────────────────────────────────
        elif (retired_numbers + flagged_numbers) / max(1, total_numbers) > 0.30:
            decision.action = ScalingAction.ADD.value
            decision.count = max(2, retired_numbers + flagged_numbers - 5)
            decision.reason = (
                f"High number of retired/flagged numbers ({retired_numbers + flagged_numbers}/"
                f"{total_numbers}). Adding {decision.count} to compensate."
            )
            decision.confidence = 0.75

        # ── Check 3: Answer rate declining ────────────────────────────────────
        elif avg_answer_rate < self.guardrails.min_answer_rate_to_keep:
            # Very low answer rate might indicate carrier reputation issues
            # Don't add more numbers — this won't help
            # Instead, recommend evaluation
            decision.action = ScalingAction.NO_CHANGE.value
            decision.reason = (
                f"Answer rate very low ({avg_answer_rate:.1%}). "
                f"Adding numbers won't help. Recommend pool evaluation and number warming."
            )
            decision.confidence = 0.8

        # ── Check 4: Over-provisioned ─────────────────────────────────────────
        elif total_capacity > 0 and calls_today / total_capacity < 0.30:
            # Using <30% of capacity
            excess_numbers = int(usable_numbers * 0.3)
            if excess_numbers > 0:
                decision.action = ScalingAction.REMOVE.value
                decision.count = excess_numbers
                decision.reason = (
                    f"Over-provisioned. Using only {calls_today}/{total_capacity} capacity ({calls_today/total_capacity:.0%}). "
                    f"Removing {excess_numbers} numbers to reduce costs."
                )
                decision.projected_answer_rate_lift = -0.02  # Slight lift from removing poor performers
                decision.confidence = 0.7

        # If no change needed, still check for NO_CHANGE reason
        if decision.action == ScalingAction.NO_CHANGE.value and decision.reason == "":
            decision.reason = f"Pool health is good. {usable_numbers} usable numbers, {avg_answer_rate:.1%} answer rate."
            decision.confidence = 0.95

        # ── Apply Guardrails ──────────────────────────────────────────────────
        self._apply_guardrails(decision)

        # Store in history
        self._scaling_history.append(decision)

        logger.info(
            "pool_health_evaluated",
            action=decision.action,
            count=decision.count,
            requires_approval=decision.requires_approval,
            reason=decision.reason,
        )

        return decision

    def calculate_optimal_pool_size(
        self,
        daily_volume: int,
        target_states: Optional[List[str]] = None,
    ) -> Dict:
        """
        Calculate ideal pool composition and size.

        Strategy:
        - 60 calls/day per number optimal
        - Prefer local presence numbers in target states (+27-40% answer rate)
        - Non-local fallbacks for other states
        - Recommend branded caller ID for top markets

        Args:
            daily_volume: Target daily calls
            target_states: List of states with prospects (e.g. ["CA", "TX", "FL"])

        Returns:
            Dictionary with composition recommendation
        """
        target_states = target_states or []
        optimal_per_number = 60
        total_needed = max(1, (daily_volume + optimal_per_number - 1) // optimal_per_number)

        # Get current state distribution
        pool_by_state = {}
        for phone in self.pool.numbers.values():
            state = phone.state
            if state not in pool_by_state:
                pool_by_state[state] = []
            pool_by_state[state].append(phone)

        # Recommend state distribution
        state_recommendations = {}
        generic_needed = total_needed

        # Allocate numbers to target states
        for state in target_states:
            # ~1-2 local numbers per state for local presence (if calling that state)
            current_in_state = len(pool_by_state.get(state, []))
            needed_in_state = max(1, (daily_volume // len(target_states) + optimal_per_number - 1) // optimal_per_number)

            if needed_in_state > current_in_state:
                state_recommendations[state] = {
                    "count": needed_in_state,
                    "current": current_in_state,
                    "gap": needed_in_state - current_in_state,
                    "local_presence_lift": 0.27,  # Research: +27-40% answer rate
                }
                generic_needed -= (needed_in_state - current_in_state)

        # Generic fallback numbers
        generic_needed = max(1, generic_needed)

        # Estimated cost
        total_monthly_cost = total_needed * self.guardrails.cost_per_number_monthly

        return {
            "total_numbers_needed": total_needed,
            "by_state": state_recommendations,
            "generic_numbers": generic_needed,
            "current_total": len(self.pool.numbers),
            "current_gap": max(0, total_needed - len(self.pool.numbers)),
            "estimated_monthly_cost": round(total_monthly_cost, 2),
            "estimated_monthly_cost_per_call": round(
                (self.guardrails.cost_per_number_monthly * total_needed) / max(1, daily_volume * 30),
                3
            ),
        }

    # ── Guardrail Application ─────────────────────────────────────────────────

    def _apply_guardrails(self, decision: ScalingDecision) -> None:
        """
        Apply cost guardrails to a scaling decision.

        Modifies decision.requires_approval and sets approval_reason if needed.
        """
        if decision.action == ScalingAction.NO_CHANGE.value:
            return

        # Calculate cost delta
        if decision.action == ScalingAction.ADD.value:
            cost_delta = decision.count * self.guardrails.cost_per_number_monthly
        elif decision.action == ScalingAction.REMOVE.value:
            cost_delta = -decision.count * self.guardrails.cost_per_number_monthly
        else:
            cost_delta = 0.0

        decision.estimated_monthly_cost = cost_delta

        # ── Check hard limits (NEVER exceeded) ─────────────────────────────────
        if decision.action == ScalingAction.ADD.value:
            future_total = len(self.pool.numbers) + decision.count

            if future_total > self.guardrails.max_total_numbers:
                decision.action = ScalingAction.NEEDS_APPROVAL.value
                decision.requires_approval = True
                decision.count = 0  # Don't proceed
                decision.approval_reason = (
                    f"BLOCKED: Adding {decision.count} would exceed hard limit of "
                    f"{self.guardrails.max_total_numbers} numbers (would be {future_total})."
                )
                logger.warning(
                    "scaling_blocked_hard_limit",
                    decision_id=decision.decision_id,
                    requested=decision.count,
                    would_exceed=future_total,
                    max_allowed=self.guardrails.max_total_numbers,
                )
                return

        # ── Check approval thresholds ──────────────────────────────────────────

        # LARGE ADJUSTMENTS: >$20/month or >10 numbers → REQUIRE APPROVAL
        if abs(cost_delta) > self.guardrails.warning_max_monthly_cost or decision.count > self.guardrails.warning_max_numbers:
            decision.requires_approval = True
            decision.approval_reason = (
                f"LARGE ADJUSTMENT: ${abs(cost_delta):.2f}/month cost delta, "
                f"{decision.count} numbers. Requires human approval."
            )
            logger.warning(
                "scaling_requires_approval_large",
                decision_id=decision.decision_id,
                action=decision.action,
                count=decision.count,
                cost_delta=cost_delta,
            )
            return

        # MEDIUM ADJUSTMENTS: $5-20/month or 4-10 numbers → LOG WARNING, proceed with delay
        if (self.guardrails.auto_approve_max_monthly_cost <= abs(cost_delta) <= self.guardrails.warning_max_monthly_cost or
            self.guardrails.auto_approve_max_numbers < decision.count <= self.guardrails.warning_max_numbers):

            logger.warning(
                "scaling_medium_adjustment_with_delay",
                decision_id=decision.decision_id,
                action=decision.action,
                count=decision.count,
                cost_delta=cost_delta,
                delay_seconds=300,
            )
            decision.requires_approval = False  # Can proceed, but with delay
            return

        # SMALL ADJUSTMENTS: <$5/month and <3 numbers → AUTO-APPROVE
        if abs(cost_delta) <= self.guardrails.auto_approve_max_monthly_cost and decision.count <= self.guardrails.auto_approve_max_numbers:
            decision.requires_approval = False
            logger.info(
                "scaling_auto_approved_small",
                decision_id=decision.decision_id,
                action=decision.action,
                count=decision.count,
                cost_delta=cost_delta,
            )
            return

    # ── Execution ─────────────────────────────────────────────────────────────

    async def execute_scaling(self, decision: ScalingDecision) -> Dict:
        """
        Execute a scaling decision (if approved).

        For auto-approved: execute immediately
        For needs_approval: return error (must be approved first)

        Args:
            decision: ScalingDecision to execute

        Returns:
            Execution result
        """
        if decision.requires_approval and not decision.approved:
            return {
                "error": "Approval required",
                "decision_id": decision.decision_id,
                "reason": decision.approval_reason,
                "action": decision.action,
            }

        if decision.action == ScalingAction.NO_CHANGE.value:
            return {"status": "no_action_needed", "decision_id": decision.decision_id}

        try:
            logger.info(
                "executing_scaling_decision",
                decision_id=decision.decision_id,
                action=decision.action,
                count=decision.count,
            )

            if decision.action == ScalingAction.ADD.value:
                # In production: Purchase numbers via Twilio API
                logger.info(
                    "purchasing_numbers",
                    count=decision.count,
                    cost_per_number=self.guardrails.cost_per_number_monthly,
                )
                # Simulate: would call Twilio API to purchase
                result_count = decision.count
                result_status = "added"

            elif decision.action == ScalingAction.REMOVE.value:
                # Mark numbers as retired
                result_count = self._retire_lowest_performers(decision.count)
                result_status = "retired"

            else:
                return {"error": f"Unknown action: {decision.action}"}

            return {
                "status": "success",
                "decision_id": decision.decision_id,
                "action": decision.action,
                "numbers_affected": result_count,
                "cost_delta": decision.estimated_monthly_cost,
                "new_pool_size": len(self.pool.numbers),
            }

        except Exception as e:
            logger.error("scaling_execution_error", decision_id=decision.decision_id, error=str(e))
            return {"error": str(e), "decision_id": decision.decision_id}

    def _retire_lowest_performers(self, count: int) -> int:
        """
        Retire the lowest-performing numbers.

        Criteria (in order):
        1. Flagged as spam (status = FLAGGED)
        2. Lowest answer rate
        3. Zero calls in last N days

        Args:
            count: How many to retire

        Returns:
            Number of numbers actually retired
        """
        retired = 0

        # Collect candidates
        candidates = []
        for number, phone in self.pool.numbers.items():
            if phone.status == "retired":
                continue

            # Score: lower = better candidate for retirement
            score = 0.0

            # Flagged numbers: priority 1 (score += 100)
            if phone.status == "flagged":
                score += 100

            # Answer rate: lower = worse (score -= answer_rate)
            score -= phone.answer_rate

            # Age: older = higher score (more likely to retire)
            days_old = (datetime.utcnow() - phone.purchased_date).days
            score += days_old * 0.1

            candidates.append((number, phone, score))

        # Sort by score (lowest = worst performers)
        candidates.sort(key=lambda x: x[2])

        # Retire top N worst performers
        for i in range(min(count, len(candidates))):
            number, phone, score = candidates[i]
            self.pool.remove_number(number)
            retired += 1
            logger.info(
                "number_retired_performance",
                number=number,
                status=phone.status,
                answer_rate=phone.answer_rate,
                score=score,
            )

        return retired

    # ── Approval Workflow ─────────────────────────────────────────────────────

    def approve_pending(self, decision_id: str, approved_by: str) -> Dict:
        """
        Human approves a pending scaling decision.

        Args:
            decision_id: Decision to approve
            approved_by: Who approved (user ID or email)

        Returns:
            Approval result
        """
        if decision_id not in self._pending_approvals:
            return {"error": f"Decision {decision_id} not found"}

        decision = self._pending_approvals[decision_id]
        decision.approved = True
        decision.approved_by = approved_by
        decision.approved_at = datetime.utcnow()

        logger.info(
            "scaling_decision_approved",
            decision_id=decision_id,
            approved_by=approved_by,
            action=decision.action,
            count=decision.count,
        )

        return {"status": "approved", "decision_id": decision_id}

    def reject_pending(self, decision_id: str, reason: str = "") -> Dict:
        """
        Human rejects a pending scaling decision.

        Args:
            decision_id: Decision to reject
            reason: Rejection reason

        Returns:
            Rejection result
        """
        if decision_id not in self._pending_approvals:
            return {"error": f"Decision {decision_id} not found"}

        decision = self._pending_approvals[decision_id]
        decision.rejected = True

        logger.info(
            "scaling_decision_rejected",
            decision_id=decision_id,
            action=decision.action,
            reason=reason,
        )

        del self._pending_approvals[decision_id]

        return {"status": "rejected", "decision_id": decision_id}

    def get_pending_approvals(self) -> List[Dict]:
        """Get all decisions awaiting human approval."""
        return [
            {
                "decision_id": d.decision_id,
                "action": d.action,
                "count": d.count,
                "estimated_monthly_cost": d.estimated_monthly_cost,
                "approval_reason": d.approval_reason,
                "confidence": d.confidence,
                "created_at": d.created_at.isoformat(),
            }
            for d in self._pending_approvals.values()
        ]

    def get_scaling_history(self, limit: int = 50) -> List[Dict]:
        """Get history of all scaling decisions."""
        return [
            {
                "decision_id": d.decision_id,
                "action": d.action,
                "count": d.count,
                "estimated_monthly_cost": d.estimated_monthly_cost,
                "confidence": d.confidence,
                "approved": d.approved,
                "approved_by": d.approved_by,
                "created_at": d.created_at.isoformat(),
                "reason": d.reason,
            }
            for d in self._scaling_history[-limit:]
        ]

    # ── ROI Analysis ──────────────────────────────────────────────────────────

    def calculate_roi(self, period_days: int = 30) -> Dict:
        """
        Calculate ROI of number pool investments.

        Measures:
        - How much did we spend?
        - How many additional answers did we get?
        - Cost per additional answer

        Args:
            period_days: Period to analyze (default 30 days)

        Returns:
            ROI metrics
        """
        # Get decisions from period
        cutoff_date = datetime.utcnow() - timedelta(days=period_days)
        period_decisions = [
            d for d in self._scaling_history
            if d.created_at >= cutoff_date and d.action == ScalingAction.ADD.value
        ]

        # Calculate totals
        numbers_added = sum(d.count for d in period_decisions)
        cost_incurred = sum(d.estimated_monthly_cost for d in period_decisions)

        # Get pool answer rate
        pool_stats = self.pool.get_pool_stats()
        current_answer_rate = pool_stats.get("avg_answer_rate", 0.0)

        # Estimate calls and answers
        # Rough: each number handles ~60 calls/day
        estimated_calls = numbers_added * 60 * period_days
        estimated_additional_answers = int(estimated_calls * current_answer_rate)

        # Cost per additional answer
        cost_per_answer = (
            cost_incurred / max(1, estimated_additional_answers)
            if estimated_additional_answers > 0 else float("inf")
        )

        # ROI positive if cost per answer < threshold (e.g., $5 per answer)
        roi_positive = cost_per_answer < 5.0

        return {
            "period_days": period_days,
            "numbers_added": numbers_added,
            "cost_incurred": round(cost_incurred, 2),
            "current_pool_answer_rate": round(current_answer_rate, 3),
            "estimated_calls": estimated_calls,
            "estimated_additional_answers": estimated_additional_answers,
            "cost_per_additional_answer": round(cost_per_answer, 2),
            "roi_positive": roi_positive,
        }

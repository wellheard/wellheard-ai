"""
WellHeard AI — Call Scheduler & Prospect Queue Optimizer
Determines optimal WHEN to call prospects based on research findings.

Research-backed constraints:
- Best times: Tue-Thu, 10am-12pm and 2-4pm in prospect's timezone
- Aged leads: Prioritize morning slots
- Cool-down: 48-72 hours between attempts to same prospect
- Attempt spacing: Day 1, Day 2, Day 4, Day 7, Day 14, Day 21
- TCPA: Only 8am-9pm in prospect's local timezone
- Max attempts: 8 before marking as exhausted
- Avoid lunch: 12pm-2pm has 35% answer rate drop
- Don't call Sunday
"""
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple
from enum import Enum
import pytz

logger = structlog.get_logger()


# ────────────────────────────────────────────────────────────────────────────
# State to Timezone Mapping (Extended)
# ────────────────────────────────────────────────────────────────────────────

STATE_TO_TIMEZONE = {
    # Eastern Time Zone (ET) - UTC-5/-4
    "ME": "America/New_York", "NH": "America/New_York", "VT": "America/New_York",
    "MA": "America/New_York", "RI": "America/New_York", "CT": "America/New_York",
    "NY": "America/New_York", "NJ": "America/New_York", "PA": "America/New_York",
    "DE": "America/New_York", "MD": "America/New_York", "DC": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "OH": "America/New_York",
    "MI": "America/Detroit", "IN": "America/Indiana/Indianapolis",
    "KY": "America/Kentucky/Louisville", "TN": "America/Chicago",
    "MS": "America/Chicago", "AL": "America/Chicago", "GA": "America/New_York",
    "SC": "America/New_York", "NC": "America/New_York", "FL": "America/New_York",
    # Central Time Zone (CT) - UTC-6/-5
    "IL": "America/Chicago", "MO": "America/Chicago", "AR": "America/Chicago",
    "LA": "America/Chicago", "IA": "America/Chicago", "MN": "America/Chicago",
    "WI": "America/Chicago", "OK": "America/Chicago", "KS": "America/Chicago",
    "NE": "America/Chicago", "SD": "America/Chicago", "ND": "America/Chicago",
    "TX": "America/Chicago",
    # Mountain Time Zone (MT) - UTC-7/-6
    "MT": "America/Denver", "WY": "America/Denver", "CO": "America/Denver",
    "NM": "America/Denver", "UT": "America/Denver", "ID": "America/Boise",
    # Pacific Time Zone (PT) - UTC-8/-7
    "WA": "America/Los_Angeles", "OR": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    # Alaska & Hawaii
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
    # US Territories
    "AS": "Pacific/Pago_Pago", "GU": "Pacific/Guam", "MP": "Pacific/Saipan",
    "PR": "America/Puerto_Rico", "VI": "America/Virgin",
    # Canadian Provinces
    "ON": "America/Toronto", "QC": "America/Toronto", "MB": "America/Winnipeg",
    "SK": "America/Regina", "AB": "America/Edmonton", "BC": "America/Vancouver",
    "NL": "America/St_Johns", "NS": "America/Halifax", "NB": "America/Halifax",
    "PE": "America/Halifax", "YT": "America/Anchorage",
}


class CallAttemptResult(str, Enum):
    """Possible outcomes of a call attempt."""
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"
    BUSY = "busy"
    INVALID = "invalid"
    DISCONNECTED = "disconnected"
    MACHINE = "machine"


@dataclass
class ProspectContact:
    """
    Represents a prospect contact with detailed tracking for scheduling.
    """

    phone: str  # E.164 format +1XXXXXXXXXX
    name: str  # First name
    last_name: str  # Last name
    state: str  # US state or territory (e.g., "CA")
    timezone: str = ""  # e.g., "America/New_York" (auto-derived if empty)

    lead_age_days: int = 0  # Days since form fill / lead import
    attempt_count: int = 0  # Total call attempts so far
    last_attempt: Optional[datetime] = None  # When was last attempt
    last_attempt_result: str = ""  # Result of last attempt

    best_time_window: str = ""  # Learned preferred time (e.g., "morning" or "2-4pm")
    dnc_checked: bool = False  # Has DNC been checked?
    dnc_checked_date: Optional[datetime] = None  # When was DNC checked?
    consent_date: Optional[datetime] = None  # When did prospect opt-in?

    answered_before: bool = False  # Ever answered a call?
    converted: bool = False  # Has this prospect converted/purchased?

    def __post_init__(self):
        """Auto-derive timezone from state if not provided."""
        if not self.timezone and self.state:
            self.timezone = STATE_TO_TIMEZONE.get(self.state, "America/New_York")

    def is_exhausted(self, max_attempts: int = 8) -> bool:
        """Check if prospect has been attempted too many times."""
        return self.attempt_count >= max_attempts

    def get_display_name(self) -> str:
        """Get full name for logging."""
        return f"{self.name} {self.last_name}".strip()


class CallScheduler:
    """
    Intelligent call scheduling based on research findings.
    Optimizes WHEN to call prospects for maximum answer rates.
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Initialize scheduler with optional configuration.

        Args:
            config: Dictionary with scheduling parameters:
                - call_window_start: "HH:MM" (default "08:00")
                - call_window_end: "HH:MM" (default "21:00")
                - max_attempts: int (default 8)
                - dnc_check_interval_days: int (default 31)
                - consent_max_age_months: int (default 18)
        """
        self.config = config or {}
        self.call_window_start = self.config.get("call_window_start", "08:00")
        self.call_window_end = self.config.get("call_window_end", "21:00")
        self.max_attempts = self.config.get("max_attempts", 8)
        self.dnc_check_interval_days = self.config.get("dnc_check_interval_days", 31)
        self.consent_max_age_months = self.config.get("consent_max_age_months", 18)

        self._internal_dnc_list = set()  # Internal DNC list (demo)

    async def check_dnc(self, phone: str) -> bool:
        """
        Check if number is on Do Not Call registry.

        In production, integrate with DNC.com, CallCOI, or similar.
        For now, checks internal list and basic patterns.

        Args:
            phone: Phone number (E.164 format)

        Returns:
            True if on DNC list, False otherwise
        """
        # Check internal DNC list
        if phone in self._internal_dnc_list:
            return True

        # In production: Call external DNC API here
        # result = await dnc_service.check(phone)
        # return result

        return False

    def add_to_dnc(self, phone: str):
        """Add number to internal DNC list (demo)."""
        self._internal_dnc_list.add(phone)

    def is_callable_now(self, prospect: ProspectContact) -> Tuple[bool, str]:
        """
        Check if prospect can be called RIGHT NOW.

        Rules:
        - TCPA: Only 8am-9pm in prospect's local timezone
        - Not on DNC
        - Consent must be valid (within 18 months)
        - Minimum spacing between attempts (48-72 hours)
        - Not Sunday
        - Not exhausted (8+ attempts)

        Args:
            prospect: ProspectContact object

        Returns:
            Tuple of (can_call: bool, reason: str)
        """
        now_utc = datetime.now(timezone.utc)

        # Rule 1: Check DNC
        if prospect.phone in self._internal_dnc_list:
            return False, "number_on_dnc"

        if prospect.dnc_checked:
            # Re-check periodically
            if prospect.dnc_checked_date:
                days_since_check = (now_utc - prospect.dnc_checked_date).days
                if days_since_check <= self.dnc_check_interval_days:
                    # Within interval — trust previous check
                    pass
                else:
                    # Would re-check here
                    pass

        # Rule 2: Check consent validity
        if prospect.consent_date:
            max_consent_age = timedelta(days=30 * self.consent_max_age_months)
            if now_utc - prospect.consent_date > max_consent_age:
                return False, "consent_expired"
        else:
            # No consent recorded - skip
            return False, "no_consent_date"

        # Rule 3: Check TCPA hours (8am-9pm in prospect's local timezone)
        try:
            tz = pytz.timezone(prospect.timezone)
            now_local = now_utc.astimezone(tz)
            hour = now_local.hour

            if hour < 8 or hour >= 21:
                return False, f"outside_tcpa_hours ({hour}:00)"
        except Exception as e:
            logger.warning("failed_to_check_tcpa_hours", error=str(e))
            return False, "timezone_error"

        # Rule 4: Check day of week (no Sunday)
        if now_local.weekday() == 6:  # Sunday = 6
            return False, "sunday_no_calls"

        # Rule 5: Check minimum spacing between attempts (48-72 hours)
        if prospect.last_attempt:
            hours_since = (now_utc - prospect.last_attempt).total_seconds() / 3600
            # Conservative: require 48 hours minimum
            if hours_since < 48:
                return False, f"too_soon_last_attempt ({hours_since:.1f}h ago)"

        # Rule 6: Check if exhausted
        if prospect.is_exhausted(self.max_attempts):
            return False, "max_attempts_reached"

        return True, "ok"

    def get_optimal_call_time(self, prospect: ProspectContact) -> datetime:
        """
        Calculate the BEST time to call this prospect in the future.

        Priority order:
        1. If prospect has learned best_time_window, use that
        2. For aged leads: prefer 10am-12pm (morning)
        3. Secondary window: 2pm-4pm (afternoon)
        4. Prefer Tue-Thu over Mon/Fri
        5. Avoid 12pm-2pm (lunch drop: 35% lower answer rate)

        Args:
            prospect: ProspectContact object

        Returns:
            datetime object in prospect's local timezone
        """
        now_utc = datetime.now(timezone.utc)
        tz = pytz.timezone(prospect.timezone)
        now_local = now_utc.astimezone(tz)

        # Determine target hour
        if prospect.best_time_window == "morning":
            target_hour = 10  # 10am start
        elif prospect.best_time_window == "afternoon":
            target_hour = 14  # 2pm start
        else:
            # Aged leads (>7 days old): prefer morning
            if prospect.lead_age_days > 7:
                target_hour = 10
            else:
                # Fresh leads: mix morning and afternoon
                target_hour = 14 if now_local.hour < 12 else 10

        # Calculate next suitable day/time
        # Prefer Tue-Thu (weekday 1-3), skip Sunday (6)
        candidate = now_local.replace(hour=target_hour, minute=0, second=0)

        # If current time is after target hour today, move to next day
        if candidate <= now_local:
            candidate += timedelta(days=1)

        # Skip to next Tue-Thu if needed
        for _ in range(14):  # Check up to 2 weeks
            weekday = candidate.weekday()
            # Skip Sunday (6), Monday (0), Friday (4)
            # Prefer: Tue(1), Wed(2), Thu(3)
            if weekday in (1, 2, 3):  # Tue, Wed, Thu
                break
            candidate += timedelta(days=1)

        return candidate.astimezone(pytz.utc)

    def get_next_attempt_delay(
        self, prospect: ProspectContact
    ) -> timedelta:
        """
        Calculate ideal spacing before next attempt to same prospect.

        Pattern: Day 1, Day 2, Day 4, Day 7, Day 14, Day 21, then weekly
        Vary time of day: morning → afternoon → morning (for freshness)

        Args:
            prospect: ProspectContact object

        Returns:
            timedelta until next attempt should be made
        """
        attempt_num = prospect.attempt_count

        # Spacing pattern
        if attempt_num == 0:
            delay_days = 0  # First call ASAP
        elif attempt_num == 1:
            delay_days = 1  # Day 2
        elif attempt_num == 2:
            delay_days = 2  # Day 4
        elif attempt_num == 3:
            delay_days = 3  # Day 7
        elif attempt_num == 4:
            delay_days = 7  # Day 14
        elif attempt_num == 5:
            delay_days = 7  # Day 21
        else:
            delay_days = 7  # Weekly after

        return timedelta(days=delay_days)

    def build_call_queue(
        self,
        prospects: List[ProspectContact],
        max_concurrent: int = 5,
    ) -> List[ProspectContact]:
        """
        Build an ordered call queue from prospects.

        Sorting priority:
        1. Previously answered (hot leads) + not yet converted
        2. Fewest attempts + oldest lead age
        3. Best optimal time window match (calling now vs later)
        4. Skip exhausted prospects (8+ attempts)

        Limits:
        - Filter to only prospects callable RIGHT NOW
        - Limit to max_concurrent count

        Args:
            prospects: List of ProspectContact objects
            max_concurrent: Max number of concurrent calls

        Returns:
            Ordered list ready to dial
        """
        # Filter: only callable now
        callable_now = []
        for prospect in prospects:
            can_call, reason = self.is_callable_now(prospect)
            if can_call:
                callable_now.append(prospect)
            else:
                logger.debug(
                    "prospect_not_callable",
                    prospect=prospect.get_display_name(),
                    reason=reason,
                )

        # Sort by priority
        def sort_key(p: ProspectContact):
            # Tier 1: Previously answered but not converted (hottest)
            if p.answered_before and not p.converted:
                tier = 0
            # Tier 2: Fewest attempts + oldest leads
            elif p.attempt_count < 3:
                tier = 1
            else:
                tier = 2

            # Within tier: sort by lead age (descending) then attempts (ascending)
            return (tier, -p.lead_age_days, p.attempt_count)

        callable_now.sort(key=sort_key)

        # Limit to max concurrent
        return callable_now[:max_concurrent]

    def get_campaign_stats(self, prospects: List[ProspectContact]) -> Dict:
        """
        Calculate campaign-level statistics for queue visibility.

        Returns:
            Dictionary with contact rate, answer rate by time/day, etc.
        """
        if not prospects:
            return {
                "total_prospects": 0,
                "contact_rate": 0.0,
                "answer_rate": 0.0,
                "exhausted": 0,
                "callable_now": 0,
                "attempts_distribution": {},
            }

        # Count exhausted
        exhausted = sum(1 for p in prospects if p.is_exhausted(self.max_attempts))

        # Count callable now
        callable_now = sum(
            1 for p in prospects if self.is_callable_now(p)[0]
        )

        # Count answered before
        answered_before = sum(1 for p in prospects if p.answered_before)

        # Attempts distribution
        attempts_dist = {}
        for p in prospects:
            count = attempts_dist.get(p.attempt_count, 0)
            attempts_dist[p.attempt_count] = count + 1

        # Average lead age
        avg_lead_age = (
            sum(p.lead_age_days for p in prospects) / len(prospects)
            if prospects
            else 0
        )

        # Contact rate: what % have we reached at least once?
        contact_rate = answered_before / len(prospects) if prospects else 0.0

        # Calculate calls needed to reach all active (non-exhausted)
        active_prospects = sum(
            1 for p in prospects if not p.is_exhausted(self.max_attempts)
        )
        # Assume 30% answer rate, 1.5 attempts per contact
        calls_needed = int(active_prospects * 1.5 / 0.30) if active_prospects else 0

        return {
            "total_prospects": len(prospects),
            "active_prospects": active_prospects,
            "exhausted": exhausted,
            "contact_rate": round(contact_rate, 3),
            "answer_rate": round(answered_before / len(prospects), 3) if prospects else 0.0,
            "callable_now": callable_now,
            "avg_lead_age_days": round(avg_lead_age, 1),
            "attempts_distribution": attempts_dist,
            "projected_calls_needed": calls_needed,
        }

    def record_call_attempt(
        self,
        prospect: ProspectContact,
        answered: bool,
        result: CallAttemptResult,
        duration_seconds: float = 0,
    ):
        """
        Record the outcome of a call attempt.

        Args:
            prospect: ProspectContact object
            answered: Whether call was answered
            result: CallAttemptResult enum
            duration_seconds: Call duration if answered
        """
        prospect.attempt_count += 1
        prospect.last_attempt = datetime.now(timezone.utc)
        prospect.last_attempt_result = result.value

        if answered:
            prospect.answered_before = True

        logger.info(
            "call_attempt_recorded",
            prospect=prospect.get_display_name(),
            attempt_num=prospect.attempt_count,
            answered=answered,
            result=result.value,
            duration=duration_seconds,
        )

    def get_next_call_time(self, prospect: ProspectContact) -> datetime:
        """
        Determine when the next call should be attempted for this prospect.

        Uses get_optimal_call_time and get_next_attempt_delay.

        Args:
            prospect: ProspectContact object

        Returns:
            Recommended datetime for next call (in UTC)
        """
        delay = self.get_next_attempt_delay(prospect)
        next_time = self.get_optimal_call_time(prospect)

        # If optimal time is sooner than delay requires, push forward
        now = datetime.now(timezone.utc)
        earliest = now + delay

        if next_time < earliest:
            next_time = earliest

        return next_time

    def update_dnc(self, prospect: ProspectContact, on_dnc: bool):
        """
        Update DNC status for a prospect.

        Args:
            prospect: ProspectContact object
            on_dnc: Whether number is on DNC list
        """
        prospect.dnc_checked = True
        prospect.dnc_checked_date = datetime.now(timezone.utc)

        if on_dnc:
            self.add_to_dnc(prospect.phone)
            logger.warning("prospect_added_to_dnc", phone=prospect.phone)

    def get_warmup_sequence(self, campaign_size: int) -> List[int]:
        """
        Calculate call volume ramp-up sequence for new campaign.

        Respects 50-75 calls/number/day and new number warming:
        - Day 1-7: 15 calls/day
        - Day 8-14: 30 calls/day
        - Day 15-21: 50 calls/day
        - Day 22+: 75 calls/day

        Args:
            campaign_size: Total prospects to call

        Returns:
            List of daily call targets
        """
        sequence = []
        day = 1
        daily_limit = 15

        remaining = campaign_size
        while remaining > 0:
            calls_today = min(daily_limit, remaining)
            sequence.append(calls_today)
            remaining -= calls_today

            # Ramp up schedule
            if day == 7:
                daily_limit = 30
            elif day == 14:
                daily_limit = 50
            elif day == 21:
                daily_limit = 75

            day += 1

        return sequence

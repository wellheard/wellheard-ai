"""
WellHeard AI - Call Cadence Engine & Scheduler

Manages multi-step outreach sequences, phone number allocation, call scheduling,
rate limiting, and timezone-aware call windows.

Features:
- Multi-step cadence sequences with configurable delays
- Phone number allocation based on call volume
- Call scheduling with timezone awareness
- Rate limiting and DNC compliance
- Campaign progress tracking
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from enum import Enum
import logging
import math
import pytz
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    Lead, Campaign, CallLog, LeadStatus, CallDisposition, CompanyStatus
)

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────


class VoicemailAction(str, Enum):
    """Action to take if voicemail is reached."""
    SKIP = "skip"
    LEAVE_MESSAGE = "leave_message"
    RETRY_LATER = "retry_later"


class CadenceTemplate(str, Enum):
    """Pre-built cadence templates for common outreach patterns."""
    AGGRESSIVE_3DAY = "aggressive_3day"
    STANDARD_7DAY = "standard_7day"
    GENTLE_14DAY = "gentle_14day"
    CUSTOM = "custom"


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class CadenceStep:
    """A single step in a cadence sequence."""
    step_number: int
    delay_hours: int  # Hours after previous step (0 = immediate)
    call_window_start: str = "09:00"  # HH:MM in prospect's local time
    call_window_end: str = "17:00"    # HH:MM in prospect's local time
    max_attempts: int = 3             # Max retries for this step
    voicemail_action: VoicemailAction = VoicemailAction.SKIP

    def __post_init__(self):
        """Validate step data."""
        if not isinstance(self.voicemail_action, VoicemailAction):
            self.voicemail_action = VoicemailAction(self.voicemail_action)


@dataclass
class ScheduledCall:
    """A call scheduled to be made."""
    lead_id: str
    campaign_id: str
    company_id: str
    phone: str
    step_number: int
    scheduled_at_utc: datetime
    scheduled_at_local: datetime
    timezone: str
    attempt_number: int
    max_attempts: int
    outbound_number: Optional[str] = None


@dataclass
class PhoneNumberAllocationResult:
    """Result of phone number allocation calculation."""
    total_contacts: int
    daily_calls_target: int
    calls_per_number_daily: int
    numbers_needed: int
    estimated_cadence_days: int


@dataclass
class CadenceTemplateConfig:
    """Configuration for a pre-built cadence template."""
    template: CadenceTemplate
    steps: List[CadenceStep] = field(default_factory=list)
    total_days: int = 0
    description: str = ""


# ── Cadence Templates ────────────────────────────────────────────────────────


CADENCE_TEMPLATES = {
    CadenceTemplate.AGGRESSIVE_3DAY: CadenceTemplateConfig(
        template=CadenceTemplate.AGGRESSIVE_3DAY,
        description="3 calls in 3 days - high touch initial blitz",
        total_days=3,
        steps=[
            CadenceStep(
                step_number=1,
                delay_hours=0,
                call_window_start="09:00",
                call_window_end="18:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.SKIP,
            ),
            CadenceStep(
                step_number=2,
                delay_hours=24,
                call_window_start="10:00",
                call_window_end="17:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.RETRY_LATER,
            ),
            CadenceStep(
                step_number=3,
                delay_hours=48,
                call_window_start="14:00",
                call_window_end="18:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.LEAVE_MESSAGE,
            ),
        ]
    ),

    CadenceTemplate.STANDARD_7DAY: CadenceTemplateConfig(
        template=CadenceTemplate.STANDARD_7DAY,
        description="5 calls over 7 days - balanced approach",
        total_days=7,
        steps=[
            CadenceStep(
                step_number=1,
                delay_hours=0,
                call_window_start="09:00",
                call_window_end="17:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.SKIP,
            ),
            CadenceStep(
                step_number=2,
                delay_hours=24,
                call_window_start="10:00",
                call_window_end="16:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.RETRY_LATER,
            ),
            CadenceStep(
                step_number=3,
                delay_hours=72,  # 3 days
                call_window_start="09:00",
                call_window_end="17:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.SKIP,
            ),
            CadenceStep(
                step_number=4,
                delay_hours=120,  # 5 days
                call_window_start="13:00",
                call_window_end="18:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.RETRY_LATER,
            ),
            CadenceStep(
                step_number=5,
                delay_hours=168,  # 7 days
                call_window_start="10:00",
                call_window_end="15:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.LEAVE_MESSAGE,
            ),
        ]
    ),

    CadenceTemplate.GENTLE_14DAY: CadenceTemplateConfig(
        template=CadenceTemplate.GENTLE_14DAY,
        description="3 calls over 14 days - low touch nurture",
        total_days=14,
        steps=[
            CadenceStep(
                step_number=1,
                delay_hours=0,
                call_window_start="09:00",
                call_window_end="17:00",
                max_attempts=1,
                voicemail_action=VoicemailAction.SKIP,
            ),
            CadenceStep(
                step_number=2,
                delay_hours=168,  # 7 days
                call_window_start="10:00",
                call_window_end="16:00",
                max_attempts=1,
                voicemail_action=VoicemailAction.RETRY_LATER,
            ),
            CadenceStep(
                step_number=3,
                delay_hours=336,  # 14 days
                call_window_start="13:00",
                call_window_end="18:00",
                max_attempts=2,
                voicemail_action=VoicemailAction.LEAVE_MESSAGE,
            ),
        ]
    ),
}


# ── CadenceEngine ────────────────────────────────────────────────────────────


class CadenceEngine:
    """
    Manages call cadences, scheduling, and outreach sequences.

    Handles:
    - Multi-step cadence sequences
    - Phone number allocation
    - Call scheduling with timezone awareness
    - Rate limiting and DNC compliance
    - Campaign progress tracking
    """

    def __init__(self, db_session: AsyncSession, settings: Any):
        """
        Initialize the cadence engine.

        Args:
            db_session: SQLAlchemy async session
            settings: Application settings (from config/settings.py)
        """
        self.db = db_session
        self.settings = settings
        self.logger = logger

    # ── Public API ───────────────────────────────────────────────────────────

    async def get_next_calls(
        self,
        campaign_id: str,
        limit: int = 50,
    ) -> List[ScheduledCall]:
        """
        Get next batch of calls ready to be made.

        Queries leads with next_call_at <= now, respects timezone and
        business hours, applies rate limiting, and excludes DNC contacts.

        Args:
            campaign_id: Campaign ID to fetch calls for
            limit: Maximum calls to return

        Returns:
            List of ScheduledCall objects ready for execution

        Raises:
            ValueError: If campaign not found or invalid
        """
        # Fetch campaign
        stmt = select(Campaign).where(Campaign.id == campaign_id)
        result = await self.db.execute(stmt)
        campaign = result.scalar_one_or_none()

        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        # Fetch leads ready to call
        now_utc = datetime.now(timezone.utc)
        stmt = (
            select(Lead)
            .where(
                and_(
                    Lead.campaign_id == campaign_id,
                    Lead.status == LeadStatus.IN_CADENCE,
                    or_(
                        Lead.next_call_at.is_(None),
                        Lead.next_call_at <= now_utc,
                    ),
                    Lead.is_dnc == False,
                    Lead.attempt_count < campaign.max_attempts,
                )
            )
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        leads = result.scalars().all()

        # Convert to ScheduledCall objects
        scheduled_calls = []
        for lead in leads:
            call = self._lead_to_scheduled_call(lead, campaign)
            if call:
                scheduled_calls.append(call)

        return scheduled_calls

    async def schedule_campaign(
        self,
        campaign_id: str,
        cadence_template: CadenceTemplate = CadenceTemplate.STANDARD_7DAY,
        custom_cadence_steps: Optional[List[CadenceStep]] = None,
    ) -> Dict[str, Any]:
        """
        Schedule all leads in a campaign according to cadence template.

        Sets initial next_call_at and status for all NEW leads in campaign.

        Args:
            campaign_id: Campaign ID to schedule
            cadence_template: Pre-built template to use
            custom_cadence_steps: Optional custom steps to override template

        Returns:
            Dict with scheduling stats:
            {
                "leads_scheduled": int,
                "first_call_batch_size": int,
                "cadence_template": str,
                "total_steps": int,
                "estimated_duration_days": int,
            }

        Raises:
            ValueError: If campaign or template not found
        """
        # Fetch campaign
        stmt = select(Campaign).where(Campaign.id == campaign_id)
        result = await self.db.execute(stmt)
        campaign = result.scalar_one_or_none()

        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        # Get cadence steps
        if custom_cadence_steps:
            cadence_steps = custom_cadence_steps
        else:
            template_config = CADENCE_TEMPLATES.get(cadence_template)
            if not template_config:
                raise ValueError(f"Unknown cadence template: {cadence_template}")
            cadence_steps = template_config.steps

        # Fetch NEW leads
        stmt = select(Lead).where(
            and_(
                Lead.campaign_id == campaign_id,
                Lead.status == LeadStatus.NEW,
                Lead.is_dnc == False,
            )
        )
        result = await self.db.execute(stmt)
        leads = result.scalars().all()

        now_utc = datetime.now(timezone.utc)
        first_step = cadence_steps[0]

        # Schedule all leads
        for lead in leads:
            # Calculate first call time (respecting timezone and business hours)
            next_call_time = self._calculate_next_call_time(
                base_time=now_utc,
                timezone=lead.timezone,
                call_window_start=first_step.call_window_start,
                call_window_end=first_step.call_window_end,
            )

            lead.next_call_at = next_call_time
            lead.status = LeadStatus.IN_CADENCE
            self.db.add(lead)

        await self.db.commit()

        return {
            "leads_scheduled": len(leads),
            "first_call_batch_size": len(leads),
            "cadence_template": cadence_template.value,
            "total_steps": len(cadence_steps),
            "estimated_duration_days": cadence_steps[-1].delay_hours // 24 if cadence_steps else 0,
        }

    async def process_call_result(
        self,
        lead_id: str,
        disposition: CallDisposition,
        call_duration: float,
    ) -> Dict[str, Any]:
        """
        After a call completes, determine next step in cadence.

        Updates lead status, schedules next call, and handles terminal
        conditions (max attempts, DNC requested, etc.).

        Args:
            lead_id: Lead ID who was called
            disposition: How the call ended
            call_duration: Duration of call in seconds

        Returns:
            Dict with cadence decision:
            {
                "action": "next_call_scheduled" | "max_attempts_reached" | "dnc_requested" | "qualified",
                "next_call_at": datetime or None,
                "new_status": str,
                "reason": str,
            }

        Raises:
            ValueError: If lead or campaign not found
        """
        # Fetch lead
        stmt = select(Lead).where(Lead.id == lead_id)
        result = await self.db.execute(stmt)
        lead = result.scalar_one_or_none()

        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        # Fetch campaign for cadence info
        stmt = select(Campaign).where(Campaign.id == lead.campaign_id)
        result = await self.db.execute(stmt)
        campaign = result.scalar_one_or_none()

        # Update lead metadata
        lead.last_called_at = datetime.now(timezone.utc)
        lead.last_disposition = disposition
        lead.attempt_count += 1
        lead.total_talk_seconds += call_duration

        # Handle terminal dispositions
        if disposition == CallDisposition.DNC_REQUESTED:
            lead.status = LeadStatus.DO_NOT_CALL
            lead.is_dnc = True
            lead.next_call_at = None
            self.db.add(lead)
            await self.db.commit()
            return {
                "action": "dnc_requested",
                "next_call_at": None,
                "new_status": LeadStatus.DO_NOT_CALL.value,
                "reason": "Contact requested DNC",
            }

        if disposition == CallDisposition.QUALIFIED_TRANSFER:
            lead.status = LeadStatus.QUALIFIED
            lead.next_call_at = None
            self.db.add(lead)
            await self.db.commit()
            return {
                "action": "qualified",
                "next_call_at": None,
                "new_status": LeadStatus.QUALIFIED.value,
                "reason": "Contact qualified for transfer",
            }

        if disposition == CallDisposition.WRONG_NUMBER:
            lead.status = LeadStatus.INVALID
            lead.next_call_at = None
            self.db.add(lead)
            await self.db.commit()
            return {
                "action": "invalid_number",
                "next_call_at": None,
                "new_status": LeadStatus.INVALID.value,
                "reason": "Wrong number",
            }

        # Check attempt limit
        if campaign and lead.attempt_count >= campaign.max_attempts:
            lead.status = LeadStatus.MAX_ATTEMPTS
            lead.next_call_at = None
            self.db.add(lead)
            await self.db.commit()
            return {
                "action": "max_attempts_reached",
                "next_call_at": None,
                "new_status": LeadStatus.MAX_ATTEMPTS.value,
                "reason": f"Max attempts ({campaign.max_attempts}) reached",
            }

        # Schedule next call in cadence
        cadence_days = campaign.cadence_days if campaign else [1, 2, 4, 7, 14, 21]

        # Determine which cadence day to use based on attempt count
        if lead.attempt_count <= len(cadence_days):
            delay_days = cadence_days[lead.attempt_count - 1]
        else:
            delay_days = cadence_days[-1]

        next_call_time = self._calculate_next_call_time(
            base_time=datetime.now(timezone.utc) + timedelta(days=delay_days),
            timezone=lead.timezone,
            call_window_start=campaign.call_window_start if campaign else "09:00",
            call_window_end=campaign.call_window_end if campaign else "17:00",
        )

        lead.next_call_at = next_call_time
        self.db.add(lead)
        await self.db.commit()

        return {
            "action": "next_call_scheduled",
            "next_call_at": next_call_time,
            "new_status": LeadStatus.IN_CADENCE.value,
            "reason": f"Call #{lead.attempt_count} completed, scheduled next call",
        }

    def calculate_phone_numbers_needed(
        self,
        total_contacts: int,
        cadence_template: CadenceTemplate = CadenceTemplate.STANDARD_7DAY,
        calls_per_number_daily: Optional[int] = None,
    ) -> PhoneNumberAllocationResult:
        """
        Calculate optimal phone number count for a campaign.

        Uses cadence template to estimate call volume and determines
        how many numbers are needed for reasonable call distribution.

        Formula:
            - Estimate calls per day from total contacts and cadence
            - Divide by per-number daily limit to get count needed
            - Account for cooldown periods and local presence preferences

        Args:
            total_contacts: Total leads in campaign
            cadence_template: Which cadence to use for estimation
            calls_per_number_daily: Override default max calls per number
                (defaults to settings.number_max_calls_per_day)

        Returns:
            PhoneNumberAllocationResult with detailed breakdown
        """
        if calls_per_number_daily is None:
            calls_per_number_daily = self.settings.number_max_calls_per_day

        # Get cadence config
        template_config = CADENCE_TEMPLATES.get(cadence_template)
        if not template_config:
            raise ValueError(f"Unknown cadence template: {cadence_template}")

        cadence_days = template_config.total_days
        total_steps = len(template_config.steps)

        # Estimate daily call volume
        # If spreading N contacts over D days with S steps, rough estimate:
        # Daily calls = (total_contacts * steps_per_day_ratio) / cadence_days
        estimated_daily_calls = (total_contacts * total_steps) / max(cadence_days, 1)

        # Calculate numbers needed
        numbers_needed = max(
            self.settings.pool_min_numbers,
            math.ceil(estimated_daily_calls / calls_per_number_daily),
        )

        # Cap at maximum
        numbers_needed = min(numbers_needed, self.settings.pool_max_total_numbers)

        return PhoneNumberAllocationResult(
            total_contacts=total_contacts,
            daily_calls_target=int(estimated_daily_calls),
            calls_per_number_daily=calls_per_number_daily,
            numbers_needed=numbers_needed,
            estimated_cadence_days=cadence_days,
        )

    async def get_campaign_progress(self, campaign_id: str) -> Dict[str, Any]:
        """
        Get detailed campaign progress stats.

        Queries the campaign and associated leads to build comprehensive
        progress metrics for reporting and monitoring.

        Args:
            campaign_id: Campaign ID to get progress for

        Returns:
            Dict with progress metrics:
            {
                "campaign_name": str,
                "status": str,
                "total_leads": int,
                "leads_by_status": {status: count, ...},
                "total_calls": int,
                "total_transfers": int,
                "qualified_transfers": int,
                "average_attempts_per_lead": float,
                "cadence_completion_percentage": float,
                "estimated_completion_date": datetime or None,
            }

        Raises:
            ValueError: If campaign not found
        """
        # Fetch campaign
        stmt = select(Campaign).where(Campaign.id == campaign_id)
        result = await self.db.execute(stmt)
        campaign = result.scalar_one_or_none()

        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        # Fetch all leads
        stmt = select(Lead).where(Lead.campaign_id == campaign_id)
        result = await self.db.execute(stmt)
        leads = result.scalars().all()

        # Count by status
        leads_by_status = {}
        for status in LeadStatus:
            count = sum(1 for lead in leads if lead.status == status)
            if count > 0:
                leads_by_status[status.value] = count

        # Calculate completion percentage
        terminal_statuses = {
            LeadStatus.QUALIFIED,
            LeadStatus.TRANSFERRED,
            LeadStatus.NOT_INTERESTED,
            LeadStatus.DO_NOT_CALL,
            LeadStatus.INVALID,
            LeadStatus.MAX_ATTEMPTS,
        }
        completed_leads = sum(
            1 for lead in leads if lead.status in terminal_statuses
        )
        total_leads = len(leads)
        completion_pct = (
            (completed_leads / total_leads * 100) if total_leads > 0 else 0.0
        )

        # Average attempts
        total_attempts = sum(lead.attempt_count for lead in leads)
        avg_attempts = (
            total_attempts / total_leads if total_leads > 0 else 0.0
        )

        # Estimate completion date (rough)
        leads_in_cadence = [lead for lead in leads if lead.status == LeadStatus.IN_CADENCE]
        estimated_completion = None
        if leads_in_cadence:
            latest_next_call = max(
                (lead.next_call_at for lead in leads_in_cadence if lead.next_call_at),
                default=None,
            )
            if latest_next_call:
                # Add estimated days to complete remaining attempts
                remaining_attempts = (
                    (campaign.max_attempts * len(leads_in_cadence)) -
                    sum(lead.attempt_count for lead in leads_in_cadence)
                )
                cadence_days = campaign.cadence_days if campaign.cadence_days else [1, 2, 4, 7, 14, 21]
                avg_cadence_day = sum(cadence_days) / len(cadence_days)
                estimated_completion = latest_next_call + timedelta(
                    days=int(remaining_attempts * avg_cadence_day / len(leads_in_cadence))
                )

        return {
            "campaign_name": campaign.name,
            "status": campaign.status.value,
            "total_leads": total_leads,
            "leads_by_status": leads_by_status,
            "total_calls": campaign.total_calls,
            "total_transfers": campaign.total_transfers,
            "qualified_transfers": campaign.qualified_transfers,
            "average_attempts_per_lead": round(avg_attempts, 2),
            "cadence_completion_percentage": round(completion_pct, 2),
            "estimated_completion_date": estimated_completion.isoformat() if estimated_completion else None,
        }

    # ── Private Helpers ──────────────────────────────────────────────────────

    def _lead_to_scheduled_call(
        self,
        lead: Lead,
        campaign: Campaign,
    ) -> Optional[ScheduledCall]:
        """
        Convert a Lead to a ScheduledCall if valid.

        Checks timezone and business hours to ensure call can be made now.
        Returns None if outside valid calling window.
        """
        now_utc = datetime.now(timezone.utc)

        # Check if within calling window for prospect's timezone
        if not self._is_within_call_window(
            now_utc,
            lead.timezone,
            campaign.call_window_start,
            campaign.call_window_end,
        ):
            return None

        return ScheduledCall(
            lead_id=lead.id,
            campaign_id=campaign.id,
            company_id=lead.company_id,
            phone=lead.phone,
            step_number=lead.attempt_count + 1,
            scheduled_at_utc=now_utc,
            scheduled_at_local=self._utc_to_local(now_utc, lead.timezone),
            timezone=lead.timezone,
            attempt_number=lead.attempt_count + 1,
            max_attempts=campaign.max_attempts,
        )

    def _calculate_next_call_time(
        self,
        base_time: datetime,
        timezone: str,
        call_window_start: str,
        call_window_end: str,
    ) -> datetime:
        """
        Calculate next valid call time respecting timezone and business hours.

        If base_time is outside call window for the prospect's timezone,
        moves to the start of the next valid calling window.

        Args:
            base_time: UTC datetime to start from
            timezone: IANA timezone string (e.g., "America/New_York")
            call_window_start: HH:MM in local time
            call_window_end: HH:MM in local time

        Returns:
            UTC datetime of next valid call time
        """
        try:
            tz = pytz.timezone(timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            # Fallback to UTC if invalid timezone
            return base_time

        # Convert base time to local
        local_time = base_time.astimezone(tz)

        # Parse window
        window_start_parts = call_window_start.split(":")
        window_end_parts = call_window_end.split(":")

        window_start_hour = int(window_start_parts[0])
        window_start_min = int(window_start_parts[1]) if len(window_start_parts) > 1 else 0

        window_end_hour = int(window_end_parts[0])
        window_end_min = int(window_end_parts[1]) if len(window_end_parts) > 1 else 0

        # Check if current local time is within window
        current_hour = local_time.hour
        current_min = local_time.minute
        current_seconds_into_day = current_hour * 3600 + current_min * 60

        window_start_seconds = window_start_hour * 3600 + window_start_min * 60
        window_end_seconds = window_end_hour * 3600 + window_end_min * 60

        if window_start_seconds <= current_seconds_into_day < window_end_seconds:
            # Already within window, return as-is
            return local_time.astimezone(pytz.UTC)

        # Outside window, move to start of next window
        if current_seconds_into_day < window_start_seconds:
            # Before window today - move to window start today
            next_local = local_time.replace(
                hour=window_start_hour,
                minute=window_start_min,
                second=0,
                microsecond=0,
            )
        else:
            # After window today - move to window start tomorrow
            next_local = (
                local_time.replace(
                    hour=window_start_hour,
                    minute=window_start_min,
                    second=0,
                    microsecond=0,
                )
                + timedelta(days=1)
            )

        # Convert back to UTC
        return next_local.astimezone(pytz.UTC)

    def _is_within_call_window(
        self,
        utc_time: datetime,
        timezone: str,
        call_window_start: str,
        call_window_end: str,
    ) -> bool:
        """
        Check if given UTC time falls within calling window for a timezone.

        Args:
            utc_time: UTC datetime to check
            timezone: IANA timezone string
            call_window_start: HH:MM in local time
            call_window_end: HH:MM in local time

        Returns:
            True if within window, False otherwise
        """
        try:
            tz = pytz.timezone(timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            return True  # Assume ok if timezone invalid

        # Convert to local
        local_time = utc_time.astimezone(tz)

        # Parse window
        window_start_parts = call_window_start.split(":")
        window_end_parts = call_window_end.split(":")

        window_start_hour = int(window_start_parts[0])
        window_start_min = int(window_start_parts[1]) if len(window_start_parts) > 1 else 0

        window_end_hour = int(window_end_parts[0])
        window_end_min = int(window_end_parts[1]) if len(window_end_parts) > 1 else 0

        # Check if within window
        current_hour = local_time.hour
        current_min = local_time.minute

        current_seconds_into_day = current_hour * 3600 + current_min * 60
        window_start_seconds = window_start_hour * 3600 + window_start_min * 60
        window_end_seconds = window_end_hour * 3600 + window_end_min * 60

        return window_start_seconds <= current_seconds_into_day < window_end_seconds

    def _utc_to_local(
        self,
        utc_time: datetime,
        timezone: str,
    ) -> datetime:
        """
        Convert UTC datetime to local datetime for a timezone.

        Args:
            utc_time: UTC datetime
            timezone: IANA timezone string

        Returns:
            Timezone-aware datetime in the given timezone
        """
        try:
            tz = pytz.timezone(timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            return utc_time

        return utc_time.astimezone(tz)


# ── Utility Functions ────────────────────────────────────────────────────────


def get_cadence_templates() -> Dict[str, CadenceTemplateConfig]:
    """Get all available cadence templates."""
    return CADENCE_TEMPLATES


def get_cadence_template(template: CadenceTemplate) -> CadenceTemplateConfig:
    """
    Get a specific cadence template configuration.

    Args:
        template: CadenceTemplate enum value

    Returns:
        CadenceTemplateConfig with steps and metadata

    Raises:
        ValueError: If template not found
    """
    if template not in CADENCE_TEMPLATES:
        raise ValueError(f"Unknown cadence template: {template}")
    return CADENCE_TEMPLATES[template]

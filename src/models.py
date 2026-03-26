"""
WellHeard AI — Multi-Tenant Data Models

Shared database, shared schema with company_id on every table.
Supports: Companies, Campaigns, Leads/Contacts, Call Logs.

All models use SQLAlchemy ORM with async support.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from enum import Enum

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, JSON,
    ForeignKey, Index, Enum as SAEnum, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


# ── Enums ────────────────────────────────────────────────────────────────────


class CompanyStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TRIAL = "trial"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class LeadStatus(str, Enum):
    NEW = "new"
    IN_CADENCE = "in_cadence"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    TRANSFERRED = "transferred"
    NOT_INTERESTED = "not_interested"
    DO_NOT_CALL = "do_not_call"
    INVALID = "invalid"
    CALLBACK_SCHEDULED = "callback_scheduled"
    MAX_ATTEMPTS = "max_attempts"


class CallDisposition(str, Enum):
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    DISCONNECTED = "disconnected"
    WRONG_NUMBER = "wrong_number"
    QUALIFIED_TRANSFER = "qualified_transfer"
    NOT_QUALIFIED_TRANSFER = "not_qualified_transfer"
    CALLBACK_REQUESTED = "callback_requested"
    DNC_REQUESTED = "dnc_requested"


# ── Helper ───────────────────────────────────────────────────────────────────


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Models ───────────────────────────────────────────────────────────────────


class Company(Base):
    """
    A customer organization (tenant).
    All other tables reference company_id for isolation.
    """
    __tablename__ = "companies"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)  # URL-safe identifier
    status = Column(SAEnum(CompanyStatus), default=CompanyStatus.TRIAL, nullable=False)

    # Billing / limits
    max_campaigns = Column(Integer, default=5)
    max_concurrent_calls = Column(Integer, default=10)
    max_daily_calls = Column(Integer, default=500)
    monthly_call_budget = Column(Float, default=500.0)

    # Integration settings (per-company)
    webhook_url = Column(String(500), default="")
    webhook_secret = Column(String(128), default="")
    zoho_access_token = Column(Text, default="")
    zoho_refresh_token = Column(Text, default="")
    zoho_token_expiry = Column(DateTime, nullable=True)
    zoho_client_id = Column(String(255), default="")
    zoho_client_secret = Column(String(255), default="")

    # Metadata
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    # Relationships
    campaigns = relationship("Campaign", back_populates="company", lazy="selectin")

    def __repr__(self):
        return f"<Company {self.slug} ({self.status.value})>"


class Campaign(Base):
    """
    A calling campaign belonging to a company.
    Each campaign has its own script, phone numbers, and leads.
    """
    __tablename__ = "campaigns"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    status = Column(SAEnum(CampaignStatus), default=CampaignStatus.DRAFT, nullable=False)

    # Script / AI config
    pipeline_mode = Column(String(20), default="budget")  # "budget" or "quality"
    system_prompt = Column(Text, default="")
    greeting_text = Column(Text, default="")
    transfer_did = Column(String(20), default="")          # Licensed agent DID for this campaign
    transfer_did_backup = Column(String(20), default="")

    # Cadence settings
    cadence_days = Column(JSON, default=lambda: [1, 2, 4, 7, 14, 21])  # Days between attempts
    max_attempts = Column(Integer, default=8)
    call_window_start = Column(String(5), default="08:00")  # HH:MM local time
    call_window_end = Column(String(5), default="21:00")

    # Number pool (campaign-specific numbers, comma-separated)
    outbound_numbers = Column(Text, default="")

    # Stats (denormalized for quick access)
    total_leads = Column(Integer, default=0)
    total_calls = Column(Integer, default=0)
    total_transfers = Column(Integer, default=0)
    qualified_transfers = Column(Integer, default=0)

    # Custom fields schema (defined per campaign for lead imports)
    custom_fields_schema = Column(JSON, default=lambda: {})

    # Metadata
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    # Relationships
    company = relationship("Company", back_populates="campaigns")
    leads = relationship("Lead", back_populates="campaign", lazy="selectin")

    __table_args__ = (
        Index("ix_campaigns_company_status", "company_id", "status"),
    )

    def __repr__(self):
        return f"<Campaign {self.name} ({self.status.value})>"


class Lead(Base):
    """
    A prospect/contact in a campaign.
    Tracks all call attempts, cadence position, and custom data.
    """
    __tablename__ = "leads"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    campaign_id = Column(String(36), ForeignKey("campaigns.id"), nullable=False, index=True)

    # Contact info
    phone = Column(String(20), nullable=False)               # E.164 format
    first_name = Column(String(100), default="")
    last_name = Column(String(100), default="")
    email = Column(String(255), default="")

    # Location (for timezone + local presence)
    state = Column(String(2), default="")                     # US state code (CA, TX, etc.)
    city = Column(String(100), default="")
    zip_code = Column(String(10), default="")
    timezone = Column(String(50), default="")                 # IANA timezone (America/New_York)

    # Cadence tracking
    status = Column(SAEnum(LeadStatus), default=LeadStatus.NEW, nullable=False)
    attempt_count = Column(Integer, default=0)
    next_call_at = Column(DateTime, nullable=True)            # When to call next (UTC)
    last_called_at = Column(DateTime, nullable=True)
    last_disposition = Column(SAEnum(CallDisposition), nullable=True)

    # Consent & compliance
    consent_timestamp = Column(DateTime, nullable=True)       # When they opted in
    consent_source = Column(String(100), default="")          # "web_form", "api", etc.
    dnc_checked_at = Column(DateTime, nullable=True)
    is_dnc = Column(Boolean, default=False)

    # Custom fields from import (flexible JSON)
    custom_fields = Column(JSON, default=lambda: {})

    # ── Conversation Memory ──────────────────────────────────────────────
    # Persisted across calls so the AI remembers prior interactions.
    last_call_summary = Column(Text, default="")            # LLM-generated summary of most recent call
    cumulative_context = Column(Text, default="")           # Rolling context across ALL calls
    objection_types = Column(JSON, default=lambda: [])      # ["price", "timing", "not_interested", ...]
    behavior_notes = Column(Text, default="")               # E.g. "friendly, asked about warranty twice"
    preferred_callback_time = Column(String(50), default="") # E.g. "mornings", "after 5pm"
    rapport_points = Column(JSON, default=lambda: [])       # Things to reference: kids, dog, vacation, etc.
    sentiment_trend = Column(String(20), default="")        # "warming", "cooling", "neutral", "hostile"
    total_talk_seconds = Column(Float, default=0.0)         # Cumulative talk time across all calls

    # Metadata
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    # Relationships
    campaign = relationship("Campaign", back_populates="leads")
    call_logs = relationship("CallLog", back_populates="lead", lazy="selectin")

    __table_args__ = (
        Index("ix_leads_campaign_status", "campaign_id", "status"),
        Index("ix_leads_next_call", "campaign_id", "next_call_at"),
        Index("ix_leads_phone", "company_id", "phone"),
        UniqueConstraint("campaign_id", "phone", name="uq_campaign_phone"),
    )

    def __repr__(self):
        return f"<Lead {self.phone} ({self.status.value})>"


class CallLog(Base):
    """
    Record of every call attempt.
    Links to lead and campaign for reporting.
    """
    __tablename__ = "call_logs"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    campaign_id = Column(String(36), ForeignKey("campaigns.id"), nullable=False, index=True)
    lead_id = Column(String(36), ForeignKey("leads.id"), nullable=False, index=True)

    # Call details
    call_sid = Column(String(64), default="")                 # Twilio/Telnyx call SID
    outbound_number = Column(String(20), default="")          # Which number we called from
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, default=0.0)
    disposition = Column(SAEnum(CallDisposition), nullable=True)

    # AI metrics
    pipeline_mode = Column(String(20), default="")
    gate_score = Column(Float, nullable=True)                 # Transfer gate score 0-100
    gate_checks_passed = Column(Integer, nullable=True)
    transfer_attempted = Column(Boolean, default=False)
    transfer_qualified = Column(Boolean, default=False)       # Agent stayed 30s+
    agent_talk_seconds = Column(Float, default=0.0)

    # Cost
    call_cost = Column(Float, default=0.0)                    # Total cost of this call

    # Transcript (stored as JSON array of turns)
    # Format: [{"role": "user"|"assistant", "content": "...", "timestamp": "..."}, ...]
    transcript = Column(JSON, default=lambda: [])

    # Post-call LLM-generated summary
    call_summary = Column(Text, default="")                 # What happened on this call
    objections_detected = Column(JSON, default=lambda: [])  # Objections raised this call
    sentiment = Column(String(20), default="")              # Overall sentiment: positive/neutral/negative
    next_action = Column(Text, default="")                  # Recommended next step

    # Metadata
    created_at = Column(DateTime, default=_now, nullable=False)

    # Relationships
    lead = relationship("Lead", back_populates="call_logs")

    __table_args__ = (
        Index("ix_call_logs_campaign_date", "campaign_id", "started_at"),
    )

    def __repr__(self):
        return f"<CallLog {self.call_sid} ({self.disposition})>"

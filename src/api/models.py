"""
API Request/Response Models
Clean Pydantic models for the voice AI integration API.
"""
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


# ── Enums ─────────────────────────────────────────────────────────────────

class PipelineMode(str, Enum):
    BUDGET = "budget"     # ~$0.021/min
    QUALITY = "quality"   # ~$0.032/min


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, Enum):
    PENDING = "pending"
    RINGING = "ringing"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Request Models ────────────────────────────────────────────────────────

class AgentConfigRequest(BaseModel):
    """Agent configuration for a voice AI call."""
    agent_id: str = Field(default="default", description="Unique agent identifier")
    system_prompt: str = Field(
        default="You are a helpful AI assistant. Be concise and natural.",
        description="System prompt defining agent personality and behavior"
    )
    voice_id: str = Field(default="", description="Voice ID for TTS (provider-specific)")
    language: str = Field(default="en", description="Language code (e.g., 'en', 'es', 'fr')")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="LLM temperature")
    max_tokens: int = Field(default=256, ge=1, le=4096, description="Max response tokens")
    interruption_enabled: bool = Field(default=True, description="Allow user barge-in")
    greeting: str = Field(default="", description="Optional first message the agent speaks")
    tools: Optional[list[dict]] = Field(default=None, description="Function calling tool definitions")


class StartCallRequest(BaseModel):
    """Request to initiate a new voice AI call."""
    pipeline: PipelineMode = Field(default=PipelineMode.BUDGET, description="Pipeline: 'budget' ($0.021/min) or 'quality' ($0.032/min)")
    direction: CallDirection = Field(default=CallDirection.OUTBOUND)
    phone_number: str = Field(description="Target phone number (E.164 format)")
    agent: AgentConfigRequest = Field(default_factory=AgentConfigRequest)
    webhook_url: Optional[str] = Field(default=None, description="URL for call events webhook")
    max_duration_seconds: int = Field(default=1800, description="Max call duration")
    metadata: Optional[dict] = Field(default=None, description="Custom metadata attached to call")
    lead_id: Optional[str] = Field(default=None, description="Lead ID for conversation memory")
    campaign_id: Optional[str] = Field(default=None, description="Campaign ID for context")


class EndCallRequest(BaseModel):
    """Request to end an active call."""
    call_id: str
    reason: str = Field(default="normal", description="Reason for ending call")


# ── Response Models ───────────────────────────────────────────────────────

class CallResponse(BaseModel):
    """Response after starting a call."""
    call_id: str
    status: CallStatus
    pipeline: PipelineMode
    phone_number: str
    agent_id: str
    estimated_cost_per_minute: float
    message: str = ""


class CallMetricsResponse(BaseModel):
    """Detailed metrics for a completed call."""
    call_id: str
    pipeline_mode: str
    duration_seconds: float
    turns: int
    interruptions: int
    avg_latency_ms: float
    p95_latency_ms: float
    total_cost_usd: float
    cost_per_minute_usd: float
    cost_breakdown: list[dict]


class HealthResponse(BaseModel):
    """Platform health status."""
    status: str
    version: str
    providers: dict
    active_calls: int


class DashboardResponse(BaseModel):
    """Platform-wide metrics dashboard."""
    total_calls: int
    active_calls: int
    total_cost_usd: float
    total_minutes: float
    avg_cost_per_minute: float
    providers: dict


class ErrorResponse(BaseModel):
    """Error response."""
    error: str
    detail: str = ""
    code: str = ""

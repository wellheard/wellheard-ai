"""
WellHeard AI - Configuration & Settings
All environment variables and provider configuration in one place.
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from enum import Enum


class PipelineMode(str, Enum):
    BUDGET = "budget"       # ~$0.021/min - Deepgram + Groq + Deepgram Aura
    QUALITY = "quality"     # ~$0.032/min - Deepgram + Gemini + Cartesia


class Settings(BaseSettings):
    """Central configuration loaded from environment variables."""

    # ── Server ────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    api_key: str = Field(default="vs-change-me-in-production", description="API key for voice AI clients")
    redis_url: str = "redis://localhost:6379/0"
    base_url: str = ""  # Public URL (e.g., "https://wellheard-ai.fly.dev") — set automatically or via HV_BASE_URL
    database_url: str = "sqlite:///wellheard.db"  # SQLAlchemy database URL

    # ── Default Pipeline ──────────────────────────────────────────────────
    default_pipeline: PipelineMode = PipelineMode.BUDGET

    # ── Deepgram (STT + Budget TTS) ──────────────────────────────────────
    deepgram_api_key: str = ""
    deepgram_stt_model: str = "nova-3"
    deepgram_stt_language: str = "en"
    deepgram_tts_model: str = "aura-orpheus-en"  # Budget TTS

    # ── Groq (Budget LLM) ────────────────────────────────────────────────
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_max_tokens: int = 50  # Optimized: with smart routing + sentence streaming, 50 is plenty
    groq_temperature: float = 0.7

    # ── OpenAI (A/B test LLM — gpt-4.1-nano, gpt-4o-mini) ───────────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-nano"

    # ── Google Gemini (Quality LLM) ──────────────────────────────────────
    google_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_max_tokens: int = 50  # Optimized: with smart routing + sentence streaming, 50 is plenty
    gemini_temperature: float = 0.7

    # ── Cartesia (Quality TTS) ───────────────────────────────────────────
    cartesia_api_key: str = ""
    cartesia_voice_id: str = "734b0cda-9091-4144-9d4d-f33ffc2cc025"  # Vicky (cloned)
    cartesia_model: str = "sonic-3"
    cartesia_speed: float = 1.0
    cartesia_emotion: str = "happy"

    # ── Telephony Provider Selection ───────────────────────────────────────
    telephony_provider: str = "twilio"  # "twilio" or "telnyx"

    # ── Twilio ─────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    twilio_phone_number: str = ""

    # ── Warm Transfer ────────────────────────────────────────────────────
    transfer_agent_did: str = "+19048404634"  # Licensed agent DID — easy to change
    transfer_agent_did_backup: str = ""       # Backup agent DID for failover
    transfer_ring_timeout: int = 20           # Seconds to ring agent before failover
    transfer_max_hold_time: int = 90          # Max seconds prospect waits on hold
    transfer_max_retries: int = 2             # Max agents to try before callback fallback
    transfer_verify_duration: int = 30        # Seconds to monitor after bridge (qualified = 30s+)
    transfer_record_calls: bool = True        # Record transferred calls for QA
    transfer_callback_enabled: bool = True    # Offer callback if all agents fail
    transfer_whisper_enabled: bool = True     # Brief agent on prospect before bridge

    # ── Stripe (Billing) ──────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_price_starter: str = ""       # Stripe Price ID for Starter plan
    stripe_price_agency: str = ""        # Stripe Price ID for Agency plan
    stripe_coupon_50off: str = ""        # Stripe Coupon ID for 50% off first month

    # ── Telnyx (Telephony) ───────────────────────────────────────────────
    telnyx_api_key: str = ""
    telnyx_connection_id: str = ""
    telnyx_phone_number: str = ""

    # ── LiveKit (Media Server) ───────────────────────────────────────────
    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # ── Latency Targets (ms) ─────────────────────────────────────────────
    stt_latency_target: int = 300
    llm_latency_target: int = 500
    tts_latency_target: int = 150
    total_latency_target: int = 800
    failover_latency_threshold: int = 2000  # Switch to fallback above this

    # ── Cost Limits ───────────────────────────────────────────────────────
    max_cost_per_minute: float = 0.04   # $0.04 hard cap
    cost_alert_threshold: float = 0.035  # Alert when approaching cap

    # ── Call Settings ─────────────────────────────────────────────────────
    max_call_duration: int = 1800  # 30 minutes
    silence_timeout: int = 10     # seconds
    vad_threshold: float = 0.5
    interruption_threshold: float = 0.7

    # ── Number Management ────────────────────────────────────────────────────
    number_pool_numbers: str = "+13187222561"    # Comma-separated list of outbound numbers
    number_max_calls_per_day: int = 75           # Default max calls per number per day
    number_cooldown_min_minutes: int = 15        # Min cooldown between calls from same number
    number_cooldown_max_minutes: int = 45        # Max cooldown (randomized)
    number_warming_enabled: bool = True          # Enable gradual warming for new numbers
    number_local_presence_enabled: bool = True   # Prefer local area codes
    number_retire_after_days: int = 90           # Auto-retire after N days of heavy use

    # ── Call Scheduling ──────────────────────────────────────────────────────
    schedule_call_window_start: str = "08:00"    # Earliest call time (prospect local)
    schedule_call_window_end: str = "21:00"      # Latest call time (prospect local)
    schedule_max_attempts: int = 8               # Max call attempts per prospect
    schedule_dnc_check_interval_days: int = 31   # Re-check DNC every 31 days
    schedule_consent_max_age_months: int = 18    # Max consent age in months

    # ── Concurrent Call Engine ───────────────────────────────────────────────────
    engine_max_concurrent: int = 10              # Max simultaneous calls (Twilio port limit)
    engine_calls_per_second: float = 1.0         # Twilio CPS limit (1.0 default, can request increase)
    engine_max_daily_calls: int = 500            # Daily call budget cap
    engine_max_daily_cost: float = 50.0          # Daily cost cap ($)
    engine_ramp_up_minutes: int = 30             # Gradual ramp-up time from 1 to max concurrent
    engine_estimated_cost_per_call: float = 0.10  # Average cost per call (Twilio + TTS + LLM)

    # ── Pool Autoscaler ──────────────────────────────────────────────────────────
    pool_auto_approve_max_numbers: int = 3       # Max numbers to add without approval
    pool_auto_approve_max_cost: float = 5.0      # Max $/month cost increase without approval
    pool_warning_max_numbers: int = 10           # Numbers above this REQUIRE approval
    pool_warning_max_cost: float = 20.0          # Cost above this REQUIRE approval
    pool_max_total_numbers: int = 50             # Absolute hard limit on pool size
    pool_min_numbers: int = 2                    # Minimum pool size
    pool_cost_per_number_monthly: float = 1.0    # Twilio: ~$1/month per number
    pool_retire_unused_after_days: int = 30      # Retire if zero calls in N days

    # ── Transfer Gate ────────────────────────────────────────────────────────
    gate_min_prospect_turns: int = 4             # Check 1: Minimum prospect turns
    gate_min_prospect_words: int = 15            # Check 1: Minimum total prospect words
    gate_min_speech_ratio: float = 0.30          # Check 3: Prospect speech/total time ratio
    gate_min_relevance_score: float = 0.50       # Check 4: Average response relevance
    gate_min_audio_rms_dbfs: float = -40.0       # Check 5: Minimum RMS energy (dBFS)
    gate_max_turn_length_cv: float = 0.20        # Check 6: Max coefficient of variation
    gate_min_engagement_score: float = 0.50      # Check 7: Min engagement score
    gate_min_checks_passed: int = 6              # Must pass 6/8 checks
    gate_min_overall_score: int = 70             # Minimum score 0-100
    gate_agent_rejection_tighten: float = 0.40   # Tighten if rejection > 40%
    gate_agent_rejection_loosen: float = 0.10    # Loosen if rejection < 10%

    model_config = {"env_prefix": "HV_", "env_file": "config/.env", "extra": "ignore"}


settings = Settings()

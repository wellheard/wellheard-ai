"""Multi-Provider Telephony Failover System.

Wraps SignalWire, Twilio, and Telnyx providers with:
  - Automatic failover on provider-level errors (rate limits, account issues)
  - Single retry on transient errors (timeouts, 5xx)
  - Error classification to avoid retrying unrecoverable failures
  - Per-provider health tracking with auto-deprioritization
  - Structured diagnostic logging for every failure

Usage:
    failover = TelephonyFailover(settings)
    result = await failover.make_call(to_number, call_id, ws_url)
    # result.call_sid, result.provider, result.attempts
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Callable, Awaitable

import structlog

from config.settings import settings

logger = structlog.get_logger()


# ── Error Classification ──────────────────────────────────────────────────

class ErrorAction(Enum):
    """What to do when a call attempt fails."""
    RETRY_SAME = "retry_same"       # Transient — retry once with same provider
    FAILOVER = "failover"           # Provider issue — try next provider
    ABORT = "abort"                 # Unrecoverable — don't retry


# Known error patterns by provider
SIGNALWIRE_ERROR_MAP = {
    # Rate limits → failover
    "exceeded outbound call rate": ErrorAction.FAILOVER,
    "rate limit": ErrorAction.FAILOVER,
    "too many requests": ErrorAction.FAILOVER,
    # Account issues → failover
    "account suspended": ErrorAction.FAILOVER,
    "insufficient funds": ErrorAction.FAILOVER,
    "balance": ErrorAction.FAILOVER,
    # Number issues → abort (won't work on any provider)
    "to invalid format": ErrorAction.ABORT,
    "from invalid format": ErrorAction.ABORT,
    "invalid phone": ErrorAction.ABORT,
    "unverified": ErrorAction.ABORT,
    # Transient → retry
    "timeout": ErrorAction.RETRY_SAME,
    "connection": ErrorAction.RETRY_SAME,
    "internal server error": ErrorAction.RETRY_SAME,
}

TWILIO_ERROR_MAP = {
    # Twilio error codes: https://www.twilio.com/docs/api/errors
    21210: ErrorAction.FAILOVER,   # Number not verified
    21211: ErrorAction.ABORT,      # Invalid 'To' number
    21214: ErrorAction.ABORT,      # 'To' number not reachable
    21215: ErrorAction.ABORT,      # Account not allowed to call
    21216: ErrorAction.ABORT,      # Account not allowed to call intl
    21217: ErrorAction.FAILOVER,   # Phone number not provisioned for outbound
    21610: ErrorAction.ABORT,      # Message blocked (opt-out)
    20003: ErrorAction.FAILOVER,   # Permission denied
    20429: ErrorAction.FAILOVER,   # Too many requests
    30010: ErrorAction.FAILOVER,   # Message rate limit
    32017: ErrorAction.FAILOVER,   # Max concurrency reached
}

# Default: unrecognized errors → failover (conservative)
DEFAULT_ERROR_ACTION = ErrorAction.FAILOVER


def classify_signalwire_error(status_code: int, error_text: str) -> ErrorAction:
    """Classify a SignalWire error into an action."""
    error_lower = error_text.lower()
    for pattern, action in SIGNALWIRE_ERROR_MAP.items():
        if pattern in error_lower:
            return action
    if status_code >= 500:
        return ErrorAction.RETRY_SAME
    if status_code == 429:
        return ErrorAction.FAILOVER
    return DEFAULT_ERROR_ACTION


def classify_twilio_error(status_code: int, error_code: Optional[int], error_text: str) -> ErrorAction:
    """Classify a Twilio error into an action."""
    if error_code and error_code in TWILIO_ERROR_MAP:
        return TWILIO_ERROR_MAP[error_code]
    if status_code >= 500:
        return ErrorAction.RETRY_SAME
    if status_code == 429:
        return ErrorAction.FAILOVER
    return DEFAULT_ERROR_ACTION


# ── Health Tracking ───────────────────────────────────────────────────────

@dataclass
class ProviderHealth:
    """Tracks provider health with exponential decay."""
    name: str
    score: float = 100.0          # 0-100, starts healthy
    consecutive_failures: int = 0
    last_success: float = 0.0     # timestamp
    last_failure: float = 0.0     # timestamp
    deprioritized_until: float = 0.0  # timestamp — if > now(), skip this provider
    total_calls: int = 0
    total_failures: int = 0

    def record_success(self):
        self.score = min(100, self.score + 10)
        self.consecutive_failures = 0
        self.last_success = time.time()
        self.total_calls += 1

    def record_failure(self, action: ErrorAction):
        self.total_calls += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.last_failure = time.time()

        # Reduce score based on error type
        if action == ErrorAction.FAILOVER:
            self.score = max(0, self.score - 25)
        elif action == ErrorAction.RETRY_SAME:
            self.score = max(0, self.score - 5)

        # Auto-deprioritize after 3 consecutive failures
        if self.consecutive_failures >= 3:
            cooldown = min(1800, 60 * (2 ** (self.consecutive_failures - 3)))  # 60s, 120s, 240s... max 30min
            self.deprioritized_until = time.time() + cooldown
            logger.warning("provider_deprioritized",
                provider=self.name,
                cooldown_seconds=cooldown,
                consecutive_failures=self.consecutive_failures,
                score=self.score)

    @property
    def is_available(self) -> bool:
        return time.time() >= self.deprioritized_until

    @property
    def is_healthy(self) -> bool:
        return self.is_available and self.score >= 20


# ── Call Attempt Result ───────────────────────────────────────────────────

@dataclass
class CallAttempt:
    """Record of a single call attempt."""
    provider: str
    success: bool
    call_sid: Optional[str] = None
    error_code: Optional[int] = None
    error_message: Optional[str] = None
    error_action: Optional[ErrorAction] = None
    duration_ms: float = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class CallResult:
    """Final result of a call attempt (possibly with failover)."""
    success: bool
    call_sid: Optional[str] = None
    provider: Optional[str] = None
    telephony: object = None  # The telephony provider instance that succeeded
    attempts: List[CallAttempt] = field(default_factory=list)
    error_message: Optional[str] = None

    @property
    def total_attempts(self) -> int:
        return len(self.attempts)

    @property
    def diagnostic_summary(self) -> str:
        lines = []
        for i, a in enumerate(self.attempts, 1):
            status = "OK" if a.success else f"FAIL ({a.error_action.value if a.error_action else '?'})"
            lines.append(f"  #{i} {a.provider}: {status} — {a.error_message or 'success'} ({a.duration_ms:.0f}ms)")
        return "\n".join(lines)


# ── Main Failover Class ──────────────────────────────────────────────────

class TelephonyFailover:
    """Multi-provider telephony with automatic failover.

    Provider priority (configurable):
      1. SignalWire — cheapest, best media streams
      2. Twilio — most reliable, slightly higher cost
      3. Telnyx — backup

    The order dynamically adjusts based on health scores.
    """

    def __init__(self, provider_order: Optional[List[str]] = None):
        """Initialize with provider credentials from settings.

        Args:
            provider_order: Override default priority. e.g. ["twilio", "signalwire"]
        """
        self._health: Dict[str, ProviderHealth] = {}
        self._provider_order = provider_order or self._detect_available_providers()

        for name in self._provider_order:
            self._health[name] = ProviderHealth(name=name)

        logger.info("telephony_failover_initialized",
            providers=self._provider_order,
            count=len(self._provider_order))

    def _detect_available_providers(self) -> List[str]:
        """Detect which providers have valid credentials configured."""
        providers = []

        # Primary: whatever is configured as telephony_provider
        primary = settings.telephony_provider

        if primary == "signalwire" and settings.signalwire_project_id and settings.signalwire_api_token:
            providers.append("signalwire")
        elif primary == "twilio" and settings.twilio_account_sid and settings.twilio_auth_token:
            providers.append("twilio")
        elif primary == "vonage" and settings.vonage_api_key:
            providers.append("vonage")

        # Add remaining providers as fallbacks
        if "signalwire" not in providers and settings.signalwire_project_id and settings.signalwire_api_token:
            providers.append("signalwire")
        if "twilio" not in providers and settings.twilio_account_sid and settings.twilio_auth_token:
            providers.append("twilio")
        if "telnyx" not in providers and settings.telnyx_api_key:
            providers.append("telnyx")
        if "vonage" not in providers and settings.vonage_api_key:
            providers.append("vonage")

        return providers

    def _get_ordered_providers(self) -> List[str]:
        """Get providers ordered by health (healthy first, then by config order)."""
        available = [p for p in self._provider_order if self._health[p].is_available]
        unavailable = [p for p in self._provider_order if not self._health[p].is_available]

        # Sort available by health score (highest first), keeping config order as tiebreaker
        available.sort(key=lambda p: (-self._health[p].score, self._provider_order.index(p)))

        # Add unavailable at the end (as last resort)
        return available + unavailable

    def _create_provider(self, name: str):
        """Create a telephony provider instance."""
        if name == "signalwire":
            from .signalwire_telephony import SignalWireTelephony
            return SignalWireTelephony(
                project_id=settings.signalwire_project_id,
                api_token=settings.signalwire_api_token,
                space_name=settings.signalwire_space_name,
                phone_number=settings.signalwire_phone_number,
            )
        elif name == "twilio":
            from .twilio_telephony import TwilioTelephony
            return TwilioTelephony(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
                phone_number=settings.twilio_phone_number,
            )
        elif name == "telnyx":
            from .telnyx_telephony import TelnyxTelephony
            return TelnyxTelephony(
                api_key=settings.telnyx_api_key,
                connection_id=settings.telnyx_connection_id,
                phone_number=settings.telnyx_phone_number,
            )
        elif name == "vonage":
            from .vonage_telephony import VonageTelephony
            return VonageTelephony(
                api_key=settings.vonage_api_key,
                api_secret=settings.vonage_api_secret,
                application_id=settings.vonage_application_id,
                private_key=settings.vonage_private_key,
                phone_number=settings.vonage_phone_number,
            )
        else:
            raise ValueError(f"Unknown provider: {name}")

    async def _attempt_call(
        self,
        provider_name: str,
        to_number: str,
        call_id: str,
        ws_url: str,
        amd_enabled: bool = False,
        amd_callback_url: str = "",
    ) -> CallAttempt:
        """Make a single call attempt with one provider."""
        start = time.time()
        try:
            provider = self._create_provider(provider_name)
            call_sid = await provider.make_outbound_call(
                to_number=to_number,
                call_id=call_id,
                ws_url=ws_url,
                amd_enabled=amd_enabled,
                amd_callback_url=amd_callback_url,
            )

            self._health[provider_name].record_success()
            return CallAttempt(
                provider=provider_name,
                success=True,
                call_sid=call_sid,
                duration_ms=(time.time() - start) * 1000,
            )

        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            error_text = str(e)

            # Classify the error
            action = self._classify_error(provider_name, error_text)
            self._health[provider_name].record_failure(action)

            logger.warning("call_attempt_failed",
                provider=provider_name,
                error=error_text[:200],
                action=action.value,
                duration_ms=round(duration_ms),
                health_score=self._health[provider_name].score,
                consecutive_failures=self._health[provider_name].consecutive_failures)

            return CallAttempt(
                provider=provider_name,
                success=False,
                error_message=error_text[:200],
                error_action=action,
                duration_ms=duration_ms,
            )

    def _classify_error(self, provider_name: str, error_text: str) -> ErrorAction:
        """Classify an error based on provider and error text."""
        error_lower = error_text.lower()

        # Check for universal abort conditions first
        for pattern in ["invalid", "not a valid phone", "unallocated"]:
            if pattern in error_lower:
                return ErrorAction.ABORT

        # Provider-specific classification
        if provider_name == "signalwire":
            return classify_signalwire_error(0, error_text)
        elif provider_name == "twilio":
            # Try to extract Twilio error code from exception text
            import re
            code_match = re.search(r'\((\d{5})\)', error_text)
            code = int(code_match.group(1)) if code_match else None
            return classify_twilio_error(0, code, error_text)

        return DEFAULT_ERROR_ACTION

    async def make_call(
        self,
        to_number: str,
        call_id: str,
        ws_url: str,
        amd_enabled: bool = False,
        amd_callback_url: str = "",
    ) -> CallResult:
        """Make an outbound call with automatic failover.

        Tries providers in priority order. On transient errors, retries once
        with the same provider. On provider errors, fails over to next.
        On unrecoverable errors, aborts immediately.

        Returns CallResult with the successful provider or full failure diagnostics.
        """
        attempts = []
        providers = self._get_ordered_providers()

        if not providers:
            return CallResult(
                success=False,
                error_message="No telephony providers configured",
                attempts=[],
            )

        logger.info("failover_call_start",
            to=to_number,
            call_id=call_id,
            provider_order=[f"{p}(hp={self._health[p].score:.0f})" for p in providers])

        for provider_name in providers:
            # Attempt #1 with this provider
            attempt = await self._attempt_call(
                provider_name, to_number, call_id, ws_url,
                amd_enabled, amd_callback_url,
            )
            attempts.append(attempt)

            if attempt.success:
                provider_instance = self._create_provider(provider_name)
                logger.info("failover_call_success",
                    provider=provider_name,
                    call_sid=attempt.call_sid,
                    total_attempts=len(attempts))
                return CallResult(
                    success=True,
                    call_sid=attempt.call_sid,
                    provider=provider_name,
                    telephony=provider_instance,
                    attempts=attempts,
                )

            # Handle failure
            if attempt.error_action == ErrorAction.ABORT:
                logger.error("failover_call_abort",
                    provider=provider_name,
                    error=attempt.error_message,
                    reason="unrecoverable_error")
                return CallResult(
                    success=False,
                    error_message=f"Unrecoverable error: {attempt.error_message}",
                    attempts=attempts,
                )

            if attempt.error_action == ErrorAction.RETRY_SAME:
                # Wait briefly and retry once
                await asyncio.sleep(1.0)
                retry = await self._attempt_call(
                    provider_name, to_number, call_id, ws_url,
                    amd_enabled, amd_callback_url,
                )
                attempts.append(retry)

                if retry.success:
                    provider_instance = self._create_provider(provider_name)
                    logger.info("failover_call_success_on_retry",
                        provider=provider_name,
                        call_sid=retry.call_sid,
                        total_attempts=len(attempts))
                    return CallResult(
                        success=True,
                        call_sid=retry.call_sid,
                        provider=provider_name,
                        telephony=provider_instance,
                        attempts=attempts,
                    )

            # FAILOVER or retry failed → try next provider
            logger.info("failover_trying_next",
                failed_provider=provider_name,
                remaining=[p for p in providers if p != provider_name and providers.index(p) > providers.index(provider_name)])

        # All providers exhausted
        logger.error("failover_all_providers_failed",
            total_attempts=len(attempts),
            providers_tried=[a.provider for a in attempts],
            diagnostics="\n" + CallResult(success=False, attempts=attempts).diagnostic_summary)

        return CallResult(
            success=False,
            error_message=f"All {len(providers)} providers failed after {len(attempts)} attempts",
            attempts=attempts,
        )

    def get_health_report(self) -> Dict[str, dict]:
        """Get health status of all providers."""
        return {
            name: {
                "score": round(h.score, 1),
                "available": h.is_available,
                "healthy": h.is_healthy,
                "consecutive_failures": h.consecutive_failures,
                "total_calls": h.total_calls,
                "total_failures": h.total_failures,
                "deprioritized_until": h.deprioritized_until if h.deprioritized_until > time.time() else None,
            }
            for name, h in self._health.items()
        }

    def reset_provider(self, name: str):
        """Manually reset a provider's health (e.g., after fixing an issue)."""
        if name in self._health:
            self._health[name] = ProviderHealth(name=name)
            logger.info("provider_health_reset", provider=name)

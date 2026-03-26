"""
WellHeard AI — Call Disposition Tagging System

Comprehensive disposition classification engine with:
- Real-time disposition tagging during calls (based on conversation signals)
- Post-call QA verification using LLM
- Transfer qualification gating (≥120s minimum)
- Billing category mapping (qualified/not_qualified/no_contact)
- Retry scheduling and lead status updates

Dispositions ranked by favorability (most to least):
1. qualified_transfer — Call ≥120s AND successfully transferred
2. failed_transfer — Qualified but transfer failed (agent didn't pick up)
3. callback_requested — Prospect requested callback
4. interested_not_qualified — Showed interest but didn't meet criteria
5. objection_handled — Had objection, was addressed, didn't convert
6. not_interested — Clear rejection/no interest
7. do_not_call — Explicitly requested removal from calling list
8. voicemail — Reached voicemail/answering machine
9. no_answer — No one picked up the call
10. wrong_number — Not the intended person
11. silent_call — Connected but no meaningful audio
12. technical_error — Call failed due to technical issue
"""

from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass
import json


# ══════════════════════════════════════════════════════════════════════════════
# DISPOSITION ENUM
# ══════════════════════════════════════════════════════════════════════════════


class CallDisposition(str, Enum):
    """Complete set of call disposition categories.

    Ordered from most to least favorable for conversion and revenue.
    """

    # ── Successful conversions (highest value) ────────────────────────────────
    QUALIFIED_TRANSFER = "qualified_transfer"
    """Call met duration gate (≥120s) AND was successfully transferred to agent."""

    FAILED_TRANSFER = "failed_transfer"
    """Qualified by criteria (≥120s) but transfer failed (agent unavailable/no pickup)."""

    # ── Positive outcomes (good for nurturing) ────────────────────────────────
    CALLBACK_REQUESTED = "callback_requested"
    """Prospect explicitly requested to be called back (engaged, not rejected)."""

    INTERESTED_NOT_QUALIFIED = "interested_not_qualified"
    """Showed genuine interest but didn't meet transfer gate criteria (<120s)."""

    OBJECTION_HANDLED = "objection_handled"
    """Had objection that was addressed, but still didn't convert/transfer."""

    # ── Neutral/negative outcomes ─────────────────────────────────────────────
    NOT_INTERESTED = "not_interested"
    """Clear rejection or stated lack of interest."""

    DO_NOT_CALL = "do_not_call"
    """Prospect explicitly requested removal from calling list (legal requirement)."""

    # ── No meaningful conversation ────────────────────────────────────────────
    VOICEMAIL = "voicemail"
    """Reached answering machine or voicemail system (no human conversation)."""

    NO_ANSWER = "no_answer"
    """Phone rang but no one answered (potential callback candidate)."""

    WRONG_NUMBER = "wrong_number"
    """Call reached wrong person or disconnected number."""

    SILENT_CALL = "silent_call"
    """Connected but no meaningful audio detected (possible answering machine)."""

    # ── Technical issues (retry candidates) ────────────────────────────────────
    TECHNICAL_ERROR = "technical_error"
    """Call failed due to technical issue (network, carrier, system error)."""


# ══════════════════════════════════════════════════════════════════════════════
# DISPOSITION ENGINE
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class DispositionSignals:
    """Real-time signals extracted from call transcript and metadata.

    These signals drive the disposition tagging engine and help classify
    calls as they happen, enabling real-time retry decisions and transfers.
    """

    call_duration_seconds: float
    """Total call duration in seconds."""

    has_human_audio: bool = True
    """Whether meaningful human audio was detected (not silent/voicemail)."""

    reached_human: bool = True
    """Whether a live human answered the call."""

    prospect_transferred: bool = False
    """Whether prospect was successfully transferred to agent."""

    transfer_failed: bool = False
    """Whether transfer was attempted but failed."""

    requested_callback: bool = False
    """Whether prospect explicitly asked to be called back."""

    expressed_interest: bool = False
    """Whether prospect showed genuine interest in product/service."""

    objection_raised: bool = False
    """Whether prospect raised any objection."""

    objection_handled: bool = False
    """Whether raised objection was addressed during call."""

    explicit_rejection: bool = False
    """Whether prospect said 'not interested' or similar clear rejection."""

    dnc_request: bool = False
    """Whether prospect explicitly asked to be removed from call list."""

    wrong_contact: bool = False
    """Whether call reached wrong person/number."""

    transcript_contains_silence: bool = False
    """Whether transcript contains long periods of silence."""


@dataclass
class DispositionResult:
    """Result of disposition classification.

    Provides both the assigned disposition and supporting metadata for
    validation, logging, and decision-making.
    """

    disposition: CallDisposition
    """The assigned disposition category."""

    confidence: float
    """Confidence score 0.0-1.0 for this disposition (especially post-call)."""

    reasoning: str
    """Human-readable explanation of why this disposition was assigned."""

    billing_category: str
    """Billing classification: 'qualified' / 'not_qualified' / 'no_contact'."""

    should_retry: bool
    """Whether this lead should be retried in future cadence."""

    retry_delay_hours: int
    """Hours to wait before next retry attempt (0 = next cadence day)."""

    lead_status_override: Optional[str] = None
    """Optional lead status to set (e.g., 'qualified', 'do_not_call', 'callback_scheduled')."""


class DispositionEngine:
    """Real-time and post-call disposition classification engine.

    This engine provides two main workflows:

    1. Real-time tagging (`tag_realtime`): Quick classification during/after call
       based on call duration, transfer status, and basic signals. Used for
       immediate lead routing and retry decisions.

    2. Post-call QA (`verify_post_call`): Deep classification using LLM to analyze
       full transcript, validate real-time tags, and extract nuanced intent signals.
       Used for analytics, coaching, and training data.

    The engine enforces a transfer gate: prospects must talk ≥120 seconds to
    qualify for agent transfer.
    """

    # Configuration constants
    TRANSFER_GATE_SECONDS = 120
    """Minimum call duration to qualify for transfer (in seconds)."""

    AGENT_MIN_TALK_SECONDS = 30
    """Minimum agent talk time to count as successful transfer."""

    SILENT_THRESHOLD_RATIO = 0.6
    """If >60% of call is silence, consider it a silent_call."""

    def __init__(self):
        """Initialize the disposition engine."""
        pass

    # ──────────────────────────────────────────────────────────────────────────
    # REAL-TIME TAGGING (during/immediately after call)
    # ──────────────────────────────────────────────────────────────────────────

    def tag_realtime(
        self,
        signals: DispositionSignals,
    ) -> DispositionResult:
        """Tag call disposition in real-time based on immediate signals.

        This is a fast, high-confidence classification used during/immediately
        after the call for:
        - Real-time transfer decisions
        - Immediate lead status updates
        - Retry scheduling

        The transfer gate rule is applied here: only calls ≥120 seconds that
        were successfully transferred are marked as QUALIFIED_TRANSFER.

        Args:
            signals: Extracted signals from call metadata and conversation.

        Returns:
            DispositionResult with disposition, confidence, and routing info.

        Example:
            ```python
            signals = DispositionSignals(
                call_duration_seconds=145.0,
                reached_human=True,
                prospect_transferred=True,
            )
            result = engine.tag_realtime(signals)
            # result.disposition == CallDisposition.QUALIFIED_TRANSFER
            # result.billing_category == "qualified"
            # result.should_retry == False
            ```
        """

        # Priority logic: check conditions from most favorable to least favorable

        # 1. DNC requests are always top priority
        if signals.dnc_request:
            return DispositionResult(
                disposition=CallDisposition.DO_NOT_CALL,
                confidence=0.95,
                reasoning="Prospect explicitly requested removal from call list.",
                billing_category="no_contact",
                should_retry=False,
                retry_delay_hours=0,
                lead_status_override="do_not_call",
            )

        # 2. Technical errors are retry candidates
        if not signals.reached_human and not signals.has_human_audio:
            if signals.call_duration_seconds < 5:
                return DispositionResult(
                    disposition=CallDisposition.TECHNICAL_ERROR,
                    confidence=0.85,
                    reasoning="Call dropped immediately; likely technical issue.",
                    billing_category="no_contact",
                    should_retry=True,
                    retry_delay_hours=1,
                )

        # 3. Reached human, successful transfer
        if (
            signals.reached_human
            and signals.prospect_transferred
            and signals.call_duration_seconds >= self.TRANSFER_GATE_SECONDS
        ):
            return DispositionResult(
                disposition=CallDisposition.QUALIFIED_TRANSFER,
                confidence=0.98,
                reasoning=(
                    f"Prospect talked for {signals.call_duration_seconds:.0f}s (≥{self.TRANSFER_GATE_SECONDS}s gate) "
                    "and was successfully transferred to agent."
                ),
                billing_category="qualified",
                should_retry=False,
                retry_delay_hours=0,
            )

        # 4. Qualified by duration but transfer failed
        if (
            signals.reached_human
            and signals.transfer_failed
            and signals.call_duration_seconds >= self.TRANSFER_GATE_SECONDS
        ):
            return DispositionResult(
                disposition=CallDisposition.FAILED_TRANSFER,
                confidence=0.90,
                reasoning=(
                    f"Prospect qualified by duration ({signals.call_duration_seconds:.0f}s), "
                    "but transfer failed (agent unavailable or no pickup)."
                ),
                billing_category="qualified",
                should_retry=False,
                retry_delay_hours=0,
                lead_status_override="qualified",
            )

        # 5. Callback requested (engaged, not rejected)
        if signals.requested_callback:
            return DispositionResult(
                disposition=CallDisposition.CALLBACK_REQUESTED,
                confidence=0.92,
                reasoning="Prospect requested callback; shows engagement.",
                billing_category="not_qualified",
                should_retry=True,
                retry_delay_hours=24,
                lead_status_override="callback_scheduled",
            )

        # 6. Interested but didn't qualify by duration
        if (
            signals.reached_human
            and signals.expressed_interest
            and signals.call_duration_seconds < self.TRANSFER_GATE_SECONDS
        ):
            return DispositionResult(
                disposition=CallDisposition.INTERESTED_NOT_QUALIFIED,
                confidence=0.80,
                reasoning=(
                    f"Prospect showed interest but call was too short "
                    f"({signals.call_duration_seconds:.0f}s < {self.TRANSFER_GATE_SECONDS}s gate)."
                ),
                billing_category="not_qualified",
                should_retry=True,
                retry_delay_hours=72,
            )

        # 7. Objection was raised and handled (but no conversion)
        if signals.objection_raised and signals.objection_handled and signals.reached_human:
            return DispositionResult(
                disposition=CallDisposition.OBJECTION_HANDLED,
                confidence=0.75,
                reasoning="Prospect raised objection that was addressed, but didn't convert.",
                billing_category="not_qualified",
                should_retry=True,
                retry_delay_hours=72,
            )

        # 8. Clear rejection (not interested)
        if signals.explicit_rejection and signals.reached_human:
            return DispositionResult(
                disposition=CallDisposition.NOT_INTERESTED,
                confidence=0.85,
                reasoning="Prospect clearly stated lack of interest.",
                billing_category="not_qualified",
                should_retry=False,
                retry_delay_hours=0,
                lead_status_override="not_interested",
            )

        # 9. Wrong number/contact
        if signals.wrong_contact:
            return DispositionResult(
                disposition=CallDisposition.WRONG_NUMBER,
                confidence=0.90,
                reasoning="Call reached wrong person or invalid number.",
                billing_category="no_contact",
                should_retry=False,
                retry_delay_hours=0,
                lead_status_override="invalid",
            )

        # 10. Voicemail/answering machine
        if not signals.reached_human and signals.has_human_audio:
            return DispositionResult(
                disposition=CallDisposition.VOICEMAIL,
                confidence=0.85,
                reasoning="Reached voicemail or answering machine.",
                billing_category="no_contact",
                should_retry=True,
                retry_delay_hours=24,
            )

        # 11. Silent call (no audio or mostly silence)
        if signals.transcript_contains_silence or not signals.has_human_audio:
            return DispositionResult(
                disposition=CallDisposition.SILENT_CALL,
                confidence=0.80,
                reasoning="Connected but no meaningful human audio detected.",
                billing_category="no_contact",
                should_retry=True,
                retry_delay_hours=24,
            )

        # 12. No answer (phone rang but no pickup)
        if not signals.reached_human and signals.call_duration_seconds > 0:
            return DispositionResult(
                disposition=CallDisposition.NO_ANSWER,
                confidence=0.88,
                reasoning="Phone rang but no one answered.",
                billing_category="no_contact",
                should_retry=True,
                retry_delay_hours=24,
            )

        # Fallback: technical error
        return DispositionResult(
            disposition=CallDisposition.TECHNICAL_ERROR,
            confidence=0.60,
            reasoning="Unable to classify call with high confidence; treating as technical error.",
            billing_category="no_contact",
            should_retry=True,
            retry_delay_hours=2,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # POST-CALL QA VERIFICATION (deep analysis with LLM)
    # ──────────────────────────────────────────────────────────────────────────

    def verify_post_call(
        self,
        transcript: List[Dict[str, str]],
        call_duration_seconds: float,
        transfer_result: Optional[Dict[str, any]],
        real_time_disposition: CallDisposition,
        llm_generate_fn: Callable[[str], str],
    ) -> DispositionResult:
        """Verify and potentially correct disposition using LLM analysis.

        This post-call QA workflow uses an LLM to:
        1. Analyze the complete transcript for nuanced intent signals
        2. Detect objections, interest level, and sentiment
        3. Verify the real-time disposition was correct
        4. Suggest corrections if needed

        The LLM is NOT used to override clear transfer gate rules—only to
        refine disposition within the framework (e.g., is it INTERESTED_NOT_QUALIFIED
        vs. OBJECTION_HANDLED?).

        Args:
            transcript: List of conversation turns: [{"role": "user"|"assistant", "content": "..."}]
            call_duration_seconds: Total call duration in seconds
            transfer_result: Optional dict with transfer metadata (e.g., {"agent_talk_seconds": 45})
            real_time_disposition: The disposition assigned in real-time
            llm_generate_fn: Async-safe function that takes a prompt and returns LLM response text

        Returns:
            DispositionResult with high-confidence classification and LLM-verified reasoning.

        Example:
            ```python
            transcript = [
                {"role": "assistant", "content": "Hello, this is..."},
                {"role": "user", "content": "Hi, what's this about?"},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "I'm not really interested, sorry."},
            ]
            result = engine.verify_post_call(
                transcript=transcript,
                call_duration_seconds=65.0,
                transfer_result=None,
                real_time_disposition=CallDisposition.INTERESTED_NOT_QUALIFIED,
                llm_generate_fn=my_llm_fn,
            )
            # LLM might recommend changing to NOT_INTERESTED instead
            ```
        """

        # Build LLM prompt for detailed analysis
        transcript_text = self._format_transcript_for_llm(transcript)
        transfer_status = "SUCCESS" if transfer_result and transfer_result.get("success") else "NOT_ATTEMPTED"
        agent_talk_seconds = transfer_result.get("agent_talk_seconds", 0) if transfer_result else 0

        analysis_prompt = f"""
You are a call quality analyst for an AI-driven outbound calling system.

CALL DETAILS:
- Duration: {call_duration_seconds:.0f} seconds
- Transfer Gate (minimum to qualify): {self.TRANSFER_GATE_SECONDS} seconds
- Transfer Status: {transfer_status}
- Agent Talk Time (if transferred): {agent_talk_seconds:.0f} seconds
- Real-time Disposition (assigned immediately): {real_time_disposition.value}

TRANSCRIPT:
{transcript_text}

YOUR TASK:
Analyze this call transcript and answer the following questions:

1. HUMAN CONTACT: Did the AI reach a live human? (yes/no)

2. INTEREST LEVEL: What is the prospect's interest level? (high/medium/low/none)
   - Look for: questions asked, positive statements, engagement, "tell me more"
   - Discount for: polite but non-committal, "maybe later", "sounds good but..."

3. OBJECTIONS: What objections were raised? (list each objection briefly)
   - Examples: price, timing, need, competitor preference, trust, etc.

4. OBJECTIONS_HANDLED: Were objections addressed/overcome? (yes/no/partial)
   - Look for: AI providing answers, prospect accepting response, moving forward

5. EXPLICIT_REJECTION: Did prospect say "not interested" or equivalent clear no? (yes/no)
   - Must be explicit: "not interested", "not now", "don't call", not just "I have to go"

6. SENTIMENT: Overall sentiment by call end (positive/neutral/negative)

7. CALLBACK_REQUESTED: Did prospect ask to be called back? (yes/no)

8. DNC_REQUEST: Did prospect ask to be removed from list? (yes/no)

9. RECOMMENDATION: Based on transcript analysis, which disposition is most accurate?
   - QUALIFIED_TRANSFER: Talked ≥{self.TRANSFER_GATE_SECONDS}s AND transferred successfully
   - FAILED_TRANSFER: Talked ≥{self.TRANSFER_GATE_SECONDS}s but transfer failed
   - CALLBACK_REQUESTED: Explicitly asked to be called back
   - INTERESTED_NOT_QUALIFIED: Showed interest but <{self.TRANSFER_GATE_SECONDS}s OR wasn't transferred
   - OBJECTION_HANDLED: Had objection that was addressed
   - NOT_INTERESTED: Clear rejection
   - DO_NOT_CALL: Asked to be removed
   - VOICEMAIL: Reached voicemail only
   - NO_ANSWER: No one answered
   - WRONG_NUMBER: Wrong person/invalid
   - SILENT_CALL: No meaningful audio
   - TECHNICAL_ERROR: Technical failure

10. CONFIDENCE: How confident are you in the recommended disposition? (0.0-1.0)

11. REASONING: Explain your recommendation in 1-2 sentences.

Format your response as JSON:
{{
  "human_contact": "yes/no",
  "interest_level": "high/medium/low/none",
  "objections": ["objection1", "objection2"],
  "objections_handled": "yes/no/partial",
  "explicit_rejection": "yes/no",
  "sentiment": "positive/neutral/negative",
  "callback_requested": "yes/no",
  "dnc_request": "yes/no",
  "recommended_disposition": "disposition_value",
  "confidence": 0.85,
  "reasoning": "Your 1-2 sentence explanation here"
}}
"""

        # Get LLM analysis
        llm_response = llm_generate_fn(analysis_prompt)
        analysis = self._parse_llm_response(llm_response)

        # Map LLM recommendation to disposition (respecting transfer gate)
        recommended_disposition = self._map_llm_to_disposition(
            analysis,
            call_duration_seconds,
            transfer_result,
        )

        # Build result
        confidence = analysis.get("confidence", 0.75)
        reasoning = analysis.get("reasoning", "LLM-based classification")

        # Determine billing and retry behavior
        billing_category = self.get_billing_category(recommended_disposition)
        should_retry = self.should_retry(recommended_disposition)
        retry_delay = self.get_retry_delay_hours(recommended_disposition)

        return DispositionResult(
            disposition=recommended_disposition,
            confidence=confidence,
            reasoning=reasoning,
            billing_category=billing_category,
            should_retry=should_retry,
            retry_delay_hours=retry_delay,
            lead_status_override=self._get_status_override(recommended_disposition),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # HELPER: Format transcript for LLM
    # ──────────────────────────────────────────────────────────────────────────

    def _format_transcript_for_llm(self, transcript: List[Dict[str, str]]) -> str:
        """Format call transcript into readable text for LLM analysis.

        Args:
            transcript: List of turns with role and content

        Returns:
            Formatted transcript string
        """
        if not transcript:
            return "[No transcript available]"

        lines = []
        for turn in transcript:
            role = turn.get("role", "unknown").upper()
            content = turn.get("content", "")
            lines.append(f"{role}: {content}")

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # HELPER: Parse LLM JSON response
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_llm_response(self, response: str) -> Dict[str, any]:
        """Extract JSON from LLM response.

        Args:
            response: LLM text response (may contain JSON)

        Returns:
            Parsed JSON dict, or empty dict if parsing fails
        """
        try:
            # Try to find JSON block in response
            import re
            json_match = re.search(r"\{[\s\S]*\}", response)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError):
            pass

        # Fallback: return defaults
        return {
            "human_contact": "unknown",
            "interest_level": "unknown",
            "objections": [],
            "objections_handled": "unknown",
            "explicit_rejection": "unknown",
            "sentiment": "unknown",
            "callback_requested": "no",
            "dnc_request": "no",
            "recommended_disposition": CallDisposition.TECHNICAL_ERROR.value,
            "confidence": 0.50,
            "reasoning": "LLM response could not be parsed",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # HELPER: Map LLM analysis to disposition (respecting transfer gate)
    # ──────────────────────────────────────────────────────────────────────────

    def _map_llm_to_disposition(
        self,
        analysis: Dict[str, any],
        call_duration_seconds: float,
        transfer_result: Optional[Dict[str, any]],
    ) -> CallDisposition:
        """Map LLM analysis to final disposition, respecting transfer gate.

        The transfer gate is enforced here: only calls ≥120s that transferred
        successfully count as QUALIFIED_TRANSFER, regardless of LLM recommendation.

        Args:
            analysis: Parsed LLM analysis dict
            call_duration_seconds: Total call duration
            transfer_result: Transfer metadata if applicable

        Returns:
            Final CallDisposition value
        """

        # DNC always wins
        if analysis.get("dnc_request") == "yes":
            return CallDisposition.DO_NOT_CALL

        # Try to parse recommended disposition
        try:
            recommended = analysis.get("recommended_disposition", "").lower()

            # Validate transfer gate rules
            transfer_succeeded = transfer_result and transfer_result.get("success", False)

            if "qualified_transfer" in recommended:
                if call_duration_seconds >= self.TRANSFER_GATE_SECONDS and transfer_succeeded:
                    return CallDisposition.QUALIFIED_TRANSFER
                elif call_duration_seconds >= self.TRANSFER_GATE_SECONDS:
                    return CallDisposition.FAILED_TRANSFER
                else:
                    return CallDisposition.INTERESTED_NOT_QUALIFIED

            elif "failed_transfer" in recommended:
                if call_duration_seconds >= self.TRANSFER_GATE_SECONDS:
                    return CallDisposition.FAILED_TRANSFER
                else:
                    return CallDisposition.INTERESTED_NOT_QUALIFIED

            elif "callback_requested" in recommended:
                return CallDisposition.CALLBACK_REQUESTED

            elif "interested_not_qualified" in recommended:
                return CallDisposition.INTERESTED_NOT_QUALIFIED

            elif "objection_handled" in recommended:
                return CallDisposition.OBJECTION_HANDLED

            elif "not_interested" in recommended:
                return CallDisposition.NOT_INTERESTED

            elif "voicemail" in recommended:
                return CallDisposition.VOICEMAIL

            elif "no_answer" in recommended:
                return CallDisposition.NO_ANSWER

            elif "wrong_number" in recommended:
                return CallDisposition.WRONG_NUMBER

            elif "silent_call" in recommended:
                return CallDisposition.SILENT_CALL

            elif "technical_error" in recommended or "technical" in recommended:
                return CallDisposition.TECHNICAL_ERROR

        except Exception:
            pass

        # Fallback to technical error
        return CallDisposition.TECHNICAL_ERROR

    # ──────────────────────────────────────────────────────────────────────────
    # HELPER: Get lead status override for disposition
    # ──────────────────────────────────────────────────────────────────────────

    def _get_status_override(self, disposition: CallDisposition) -> Optional[str]:
        """Get recommended lead status for a disposition.

        Args:
            disposition: The call disposition

        Returns:
            Lead status string, or None to leave unchanged
        """
        mapping = {
            CallDisposition.QUALIFIED_TRANSFER: "transferred",
            CallDisposition.FAILED_TRANSFER: "qualified",
            CallDisposition.CALLBACK_REQUESTED: "callback_scheduled",
            CallDisposition.NOT_INTERESTED: "not_interested",
            CallDisposition.DO_NOT_CALL: "do_not_call",
            CallDisposition.WRONG_NUMBER: "invalid",
        }
        return mapping.get(disposition)

    # ──────────────────────────────────────────────────────────────────────────
    # PUBLIC: Billing Category
    # ──────────────────────────────────────────────────────────────────────────

    def get_billing_category(self, disposition: CallDisposition) -> str:
        """Map disposition to billing category.

        Billing categories determine:
        - Revenue impact (qualified = customer, not_qualified = prospecting, etc.)
        - Cost allocation (no_contact may be refunded)
        - Reporting and analytics

        Args:
            disposition: The call disposition

        Returns:
            One of: 'qualified', 'not_qualified', 'no_contact'

        Example:
            ```python
            assert engine.get_billing_category(CallDisposition.QUALIFIED_TRANSFER) == "qualified"
            assert engine.get_billing_category(CallDisposition.VOICEMAIL) == "no_contact"
            assert engine.get_billing_category(CallDisposition.INTERESTED_NOT_QUALIFIED) == "not_qualified"
            ```
        """

        # Qualified: successful transfer or qualified but failed to transfer
        qualified = {
            CallDisposition.QUALIFIED_TRANSFER,
            CallDisposition.FAILED_TRANSFER,
        }
        if disposition in qualified:
            return "qualified"

        # No contact: never reached human
        no_contact = {
            CallDisposition.VOICEMAIL,
            CallDisposition.NO_ANSWER,
            CallDisposition.WRONG_NUMBER,
            CallDisposition.SILENT_CALL,
            CallDisposition.TECHNICAL_ERROR,
        }
        if disposition in no_contact:
            return "no_contact"

        # Not qualified: reached human but didn't convert
        return "not_qualified"

    # ──────────────────────────────────────────────────────────────────────────
    # PUBLIC: Should Retry
    # ──────────────────────────────────────────────────────────────────────────

    def should_retry(self, disposition: CallDisposition) -> bool:
        """Determine if a lead should be retried in the calling cadence.

        Retry candidates include:
        - No answer (may try again at different time)
        - Voicemail (callback opportunity)
        - Technical errors (retry on next cadence day)
        - Callback requested (definitely retry)
        - Objections that were handled (nurture opportunity)
        - Interested but under gate (continue engagement)

        Do NOT retry:
        - Successful transfer (already qualified)
        - Explicit rejection (waste of resources)
        - DNC requests (legal requirement)
        - Wrong number (invalid lead)

        Args:
            disposition: The call disposition

        Returns:
            True if lead should be retried, False otherwise

        Example:
            ```python
            assert engine.should_retry(CallDisposition.QUALIFIED_TRANSFER) == False
            assert engine.should_retry(CallDisposition.NO_ANSWER) == True
            assert engine.should_retry(CallDisposition.DO_NOT_CALL) == False
            ```
        """

        no_retry = {
            CallDisposition.QUALIFIED_TRANSFER,
            CallDisposition.NOT_INTERESTED,
            CallDisposition.DO_NOT_CALL,
            CallDisposition.WRONG_NUMBER,
        }
        return disposition not in no_retry

    # ──────────────────────────────────────────────────────────────────────────
    # PUBLIC: Retry Delay
    # ──────────────────────────────────────────────────────────────────────────

    def get_retry_delay_hours(self, disposition: CallDisposition) -> int:
        """Get recommended delay (hours) before retrying this lead.

        Different dispositions warrant different retry strategies:
        - Technical errors: Quick retry (1-2 hours)
        - Voicemail: Next business day (24 hours)
        - No answer: Next business day (24 hours)
        - Callback requested: Next business day (24 hours)
        - Interested but under gate: Longer nurture (72 hours)
        - Objection handled: Nurture period (72 hours)

        Args:
            disposition: The call disposition

        Returns:
            Recommended delay in hours (0 = next cadence day, 1+ = explicit hours)

        Example:
            ```python
            assert engine.get_retry_delay_hours(CallDisposition.TECHNICAL_ERROR) == 1
            assert engine.get_retry_delay_hours(CallDisposition.VOICEMAIL) == 24
            assert engine.get_retry_delay_hours(CallDisposition.INTERESTED_NOT_QUALIFIED) == 72
            ```
        """

        delays = {
            CallDisposition.QUALIFIED_TRANSFER: 0,
            CallDisposition.FAILED_TRANSFER: 0,
            CallDisposition.TECHNICAL_ERROR: 1,
            CallDisposition.VOICEMAIL: 24,
            CallDisposition.NO_ANSWER: 24,
            CallDisposition.SILENT_CALL: 24,
            CallDisposition.CALLBACK_REQUESTED: 24,
            CallDisposition.INTERESTED_NOT_QUALIFIED: 72,
            CallDisposition.OBJECTION_HANDLED: 72,
            CallDisposition.NOT_INTERESTED: 0,
            CallDisposition.DO_NOT_CALL: 0,
            CallDisposition.WRONG_NUMBER: 0,
        }
        return delays.get(disposition, 0)

    # ──────────────────────────────────────────────────────────────────────────
    # STATS & REPORTING
    # ──────────────────────────────────────────────────────────────────────────

    def get_disposition_label(self, disposition: CallDisposition) -> str:
        """Get human-readable label for a disposition.

        Args:
            disposition: The call disposition

        Returns:
            Display-friendly label (e.g., "Qualified Transfer")
        """
        labels = {
            CallDisposition.QUALIFIED_TRANSFER: "Qualified Transfer",
            CallDisposition.FAILED_TRANSFER: "Failed Transfer",
            CallDisposition.CALLBACK_REQUESTED: "Callback Requested",
            CallDisposition.INTERESTED_NOT_QUALIFIED: "Interested (Not Qualified)",
            CallDisposition.OBJECTION_HANDLED: "Objection Handled",
            CallDisposition.NOT_INTERESTED: "Not Interested",
            CallDisposition.DO_NOT_CALL: "Do Not Call",
            CallDisposition.VOICEMAIL: "Voicemail",
            CallDisposition.NO_ANSWER: "No Answer",
            CallDisposition.WRONG_NUMBER: "Wrong Number",
            CallDisposition.SILENT_CALL: "Silent Call",
            CallDisposition.TECHNICAL_ERROR: "Technical Error",
        }
        return labels.get(disposition, disposition.value.replace("_", " ").title())

    def disposition_favorability_rank(self, disposition: CallDisposition) -> int:
        """Get favorability rank of a disposition (0=best, 11=worst).

        Used for sorting, reporting, and comparing call outcomes.

        Args:
            disposition: The call disposition

        Returns:
            Rank from 0 (best) to 11 (worst)
        """
        ranking = [
            CallDisposition.QUALIFIED_TRANSFER,
            CallDisposition.FAILED_TRANSFER,
            CallDisposition.CALLBACK_REQUESTED,
            CallDisposition.INTERESTED_NOT_QUALIFIED,
            CallDisposition.OBJECTION_HANDLED,
            CallDisposition.NOT_INTERESTED,
            CallDisposition.DO_NOT_CALL,
            CallDisposition.VOICEMAIL,
            CallDisposition.NO_ANSWER,
            CallDisposition.WRONG_NUMBER,
            CallDisposition.SILENT_CALL,
            CallDisposition.TECHNICAL_ERROR,
        ]
        try:
            return ranking.index(disposition)
        except ValueError:
            return 12  # Unknown


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FACTORY
# ══════════════════════════════════════════════════════════════════════════════


def create_disposition_engine() -> DispositionEngine:
    """Factory function to create a new disposition engine.

    Returns:
        Initialized DispositionEngine instance
    """
    return DispositionEngine()

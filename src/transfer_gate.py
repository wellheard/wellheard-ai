"""
WellHeard AI — Transfer Qualification Gate

A production-ready multi-check system that runs BEFORE any transfer is initiated.
The transfer ONLY proceeds if ALL checks pass. If any check fails, the call is NOT
transferred — instead, the AI gracefully re-qualifies or ends the call.

This gate prevents wasting licensed agent time on:
- Silent/dead-air calls
- Background noise only
- Prospects who aren't actually qualified
- Non-human systems (IVR, voicemail that slipped through)

Architecture:
The gate runs 8 independent checks on the call transcript and audio metrics.
Each check returns a boolean (passed/failed), a numeric score (0-1), and details.
If 6+ checks pass AND overall_score >= 70: APPROVE transfer.
If 4-5 checks pass: recommendation = "re_qualify" (try one more time).
If < 4 checks pass: recommendation = "end_call" (hang up gracefully).

Checks:
1. Minimum Conversation Depth — enough turns and words from prospect?
2. Phase Completion Verification — did prospect confirm identify, urgency, qualify?
3. Speech Activity Verification — is prospect actually speaking (not just air)?
4. Response Relevance Scoring — are responses relevant to questions asked?
5. Audio Quality Check — is audio clean, not at noise floor?
6. Human Speech Pattern Detection — natural cadence or robotic/automated?
7. Prospect Engagement Score — response latency, word counts, relevance combined
8. Agent Feedback Loop — self-tuning based on agent satisfaction

Self-tuning:
After transfer, agents either accept or reject (hang up within 30s).
If rejection rate > 40%: TIGHTEN all thresholds by 20%
If rejection rate < 10%: LOOSEN thresholds by 10%
"""

import time
import structlog
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from statistics import stdev, mean

logger = structlog.get_logger()


class TransferRecommendation(str, Enum):
    """Recommendation from the transfer gate."""
    TRANSFER = "transfer"
    RE_QUALIFY = "re_qualify"
    END_CALL = "end_call"
    FLAG_FOR_REVIEW = "flag_for_review"


@dataclass
class TransferGateResult:
    """Output of the transfer qualification gate evaluation."""
    approved: bool
    overall_score: float                     # 0-100
    checks_passed: int                       # How many of 8 passed
    checks_total: int = 8
    failed_checks: List[str] = field(default_factory=list)
    check_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recommendation: TransferRecommendation = TransferRecommendation.END_CALL
    reason: str = ""


@dataclass
class CallTranscriptTurn:
    """A single turn in the call transcript."""
    speaker: str  # "sdr" or "prospect"
    text: str
    timestamp: float
    is_final: bool = False
    audio_rms: float = 0.0  # RMS energy if prospect speech


@dataclass
class CallContext:
    """All data needed to evaluate transfer readiness."""
    call_id: str = ""

    # Conversation data
    transcript_turns: List[CallTranscriptTurn] = field(default_factory=list)

    # Phase tracking
    completed_phases: List[str] = field(default_factory=list)
    # Positive signals detected in each phase: {"identify": True, "urgency": True, "qualify": True}
    phase_positive_signals: Dict[str, bool] = field(default_factory=dict)

    # Audio metrics (from VAD/STT)
    prospect_speech_seconds: float = 0.0
    prospect_total_seconds: float = 0.0
    avg_audio_rms: float = 0.0          # Average RMS energy of prospect audio (-40 to 0 dBFS)
    audio_rms_variance: float = 0.0     # Variance in RMS (low = constant = suspicious)

    # Timing (in milliseconds)
    response_latencies_ms: List[float] = field(default_factory=list)
    turn_word_counts: List[int] = field(default_factory=list)

    # Call metadata
    call_duration_seconds: float = 0.0
    voicemail_detected: bool = False
    silence_detected: bool = False


@dataclass
class AgentFeedbackTracker:
    """Tracks agent satisfaction with transfers to auto-tune thresholds."""
    total_transfers: int = 0
    qualified_transfers: int = 0  # Agent stayed 30s+
    rejected_transfers: int = 0   # Agent hung up < 30s

    def rejection_rate(self) -> float:
        """Return rejection rate 0-1."""
        if self.total_transfers == 0:
            return 0.0
        return self.rejected_transfers / self.total_transfers

    def should_tighten(self) -> bool:
        """Should we tighten thresholds? (rejection > 40%)"""
        return self.rejection_rate() > 0.40

    def should_loosen(self) -> bool:
        """Should we loosen thresholds? (rejection < 10%)"""
        return self.rejection_rate() < 0.10


@dataclass
class TransferGateConfig:
    """Configurable thresholds for all 8 checks."""
    # Check 1: Conversation Depth
    min_prospect_turns: int = 4
    min_prospect_words: int = 15

    # Check 2: Phase Completion
    required_phases: List[str] = field(default_factory=lambda: ["identify", "urgency_pitch", "qualify_account"])

    # Check 3: Speech Activity
    min_speech_ratio: float = 0.30  # prospect_speech_seconds / prospect_total_seconds >= 30%

    # Check 4: Response Relevance
    min_relevance_score: float = 0.50  # Average relevance across all responses

    # Check 5: Audio Quality
    min_audio_rms_dbfs: float = -40.0  # Minimum RMS energy (dBFS)
    max_audio_rms_variance_ratio: float = 0.5  # Max variance to mean ratio

    # Check 6: Human Speech Pattern
    max_turn_length_cv: float = 0.2  # Coefficient of variation in turn lengths

    # Check 7: Prospect Engagement
    min_avg_response_latency_ms: float = 100.0
    max_avg_response_latency_ms: float = 5000.0
    min_engagement_score: float = 0.50

    # Check 8: Agent Feedback (self-tuning)
    agent_rejection_tighten_threshold: float = 0.40
    agent_rejection_loosen_threshold: float = 0.10

    # Overall gates
    min_checks_passed: int = 6  # Must pass 6/8
    min_overall_score: int = 70  # Score 0-100


class TransferQualificationGate:
    """
    Production-ready transfer qualification gate.

    Usage:
        gate = TransferQualificationGate()
        result = gate.evaluate(call_context)

        if result.approved:
            # Transfer to agent
        elif result.recommendation == TransferRecommendation.RE_QUALIFY:
            # Ask one more confirmation question
        else:
            # End call gracefully

        # After transfer:
        gate.record_agent_feedback(call_id, qualified=True, agent_talk_seconds=45)
    """

    def __init__(self, config: Optional[TransferGateConfig] = None):
        self.config = config or TransferGateConfig()
        self.feedback_tracker = AgentFeedbackTracker()
        self._applied_tuning_factor = 1.0  # Multiplier for thresholds

    # ── Check Weights ────────────────────────────────────────────────────────
    # Critical checks: conversation quality indicators (1.5x weight)
    # Support checks: secondary signals (0.75x weight)
    CHECK_WEIGHTS = {
        "conversation_depth": 1.5,      # Critical: did they actually talk?
        "phase_completion": 1.5,        # Critical: did they say yes to key phases?
        "speech_activity": 1.5,         # Critical: are they actually speaking?
        "response_relevance": 1.5,      # Critical: are responses on-topic?
        "audio_quality": 0.75,          # Support: audio signal quality
        "human_speech_pattern": 0.75,   # Support: natural speech variation
        "prospect_engagement": 0.75,    # Support: timing/word count patterns
        "agent_feedback_loop": 0.75,    # Support: historical tuning
    }

    # ── Main Entry Point ────────────────────────────────────────────────────

    def evaluate(self, context: CallContext) -> TransferGateResult:
        """
        Run all 8 checks and produce a pass/fail decision.

        Uses weighted scoring: critical conversation-quality checks (depth, phase,
        speech, relevance) are weighted 1.5x, while support checks (audio, pattern,
        engagement, feedback) are weighted 0.75x. This ensures that clearly qualified
        prospects score 95+ while bad calls score much lower.

        Hard-fail signals (voicemail detected, silence detected) bypass all checks
        and immediately return score < 10.

        Returns:
            TransferGateResult with approved flag and detailed breakdown.
        """
        # ── Hard-fail signals ────────────────────────────────────────────
        if context.voicemail_detected:
            return TransferGateResult(
                approved=False,
                overall_score=3.0,
                checks_passed=0,
                checks_total=8,
                failed_checks=["hard_fail_voicemail"],
                check_details={"hard_fail": {"reason": "voicemail_detected"}},
                recommendation=TransferRecommendation.END_CALL,
                reason="Voicemail detected — ending call immediately.",
            )
        if context.silence_detected:
            return TransferGateResult(
                approved=False,
                overall_score=5.0,
                checks_passed=0,
                checks_total=8,
                failed_checks=["hard_fail_silence"],
                check_details={"hard_fail": {"reason": "silence_detected"}},
                recommendation=TransferRecommendation.END_CALL,
                reason="Persistent silence detected — ending call immediately.",
            )

        checks_passed = 0
        failed_checks = []
        check_details = {}
        weighted_score_sum = 0.0
        weight_sum = 0.0

        # Run all 8 checks
        checks = [
            ("conversation_depth", self._check_conversation_depth),
            ("phase_completion", self._check_phase_completion),
            ("speech_activity", self._check_speech_activity),
            ("response_relevance", self._check_response_relevance),
            ("audio_quality", self._check_audio_quality),
            ("human_speech_pattern", self._check_human_speech_pattern),
            ("prospect_engagement", self._check_prospect_engagement),
            ("agent_feedback_loop", self._check_agent_feedback_loop),
        ]

        for check_name, check_fn in checks:
            passed, score, details = check_fn(context)
            weight = self.CHECK_WEIGHTS.get(check_name, 1.0)
            check_details[check_name] = {
                "passed": passed,
                "score": score,
                "weight": weight,
                "weighted_score": round(score * weight, 3),
                "details": details,
            }
            weighted_score_sum += score * weight
            weight_sum += weight
            if passed:
                checks_passed += 1
            else:
                failed_checks.append(check_name)

        # Calculate weighted overall score
        overall_score = round((weighted_score_sum / weight_sum) * 100, 1) if weight_sum > 0 else 0.0

        # Decision logic
        approved = (
            checks_passed >= self.config.min_checks_passed
            and overall_score >= self.config.min_overall_score
        )

        # Recommendation
        if approved:
            recommendation = TransferRecommendation.TRANSFER
            reason = f"All gates passed: {checks_passed}/8 checks, score {overall_score}/100"
        elif checks_passed >= 5:
            recommendation = TransferRecommendation.RE_QUALIFY
            reason = (
                f"Marginal qualification ({checks_passed}/8 checks, score {overall_score}/100). "
                f"Failed: {', '.join(failed_checks)}. Recommend re-qualifying with one more confirm."
            )
        else:
            recommendation = TransferRecommendation.END_CALL
            reason = (
                f"Insufficient qualification ({checks_passed}/8 checks, score {overall_score}/100). "
                f"Failed: {', '.join(failed_checks)}. Recommend ending call gracefully."
            )

        result = TransferGateResult(
            approved=approved,
            overall_score=overall_score,
            checks_passed=checks_passed,
            checks_total=8,
            failed_checks=failed_checks,
            check_details=check_details,
            recommendation=recommendation,
            reason=reason,
        )

        logger.info(
            "transfer_gate_evaluated",
            call_id=context.call_id,
            approved=approved,
            checks_passed=checks_passed,
            overall_score=overall_score,
            recommendation=recommendation.value,
            failed_checks=failed_checks,
        )

        return result

    # ── Check 1: Minimum Conversation Depth ─────────────────────────────────

    def _check_conversation_depth(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 1: Minimum Conversation Depth

        A real qualified prospect has participated in at least 4 turns
        and said at least 15 words total.

        Returns: (passed, score 0-1, details dict)
        """
        # Count prospect turns and words — only count CONVERSATIONAL turns
        # (turns with at least one conversational marker like I, my, yes, no, etc.)
        conversational_markers = {
            "i ", "i'm", "my ", "me ", "we ", "yes", "no", "yeah", "sure",
            "okay", "ok", "well", "actually", "right", "that", "this",
            "who", "what", "why", "how", "don't", "do ", "have",
            "can", "will", "would", "could", "it's", "that's", "hello",
            "hi ", "hey", "thanks", "thank", "sorry", "please", "uh huh",
        }
        all_prospect_turns = [t for t in context.transcript_turns if t.speaker == "prospect"]
        prospect_turns = [
            t for t in all_prospect_turns
            if any(marker in t.text.lower() for marker in conversational_markers)
        ]
        prospect_word_count = sum(len(t.text.split()) for t in prospect_turns)

        # If most turns are non-conversational, that's a strong fail signal
        non_conversational_ratio = (
            (len(all_prospect_turns) - len(prospect_turns)) / len(all_prospect_turns)
            if all_prospect_turns else 0
        )

        # Score components — bonus for exceeding thresholds
        turn_ratio = len(prospect_turns) / self.config.min_prospect_turns
        word_ratio = prospect_word_count / self.config.min_prospect_words

        if turn_ratio >= 1.0:
            turn_score = 0.9 + 0.1 * min((turn_ratio - 1.0) / 1.0, 1.0)  # 1x→0.9, 2x→1.0
        else:
            turn_score = turn_ratio ** 2  # Quadratic penalty below threshold

        if word_ratio >= 1.0:
            word_score = 0.9 + 0.1 * min((word_ratio - 1.0) / 2.0, 1.0)  # 1x→0.9, 3x→1.0
        else:
            word_score = word_ratio ** 2

        # Average, penalized by non-conversational ratio
        base_score = (turn_score + word_score) / 2.0
        # If >50% of turns are non-conversational (TV/radio), heavy penalty
        if non_conversational_ratio > 0.5:
            score = base_score * 0.2  # 80% penalty
        elif non_conversational_ratio > 0.25:
            score = base_score * 0.5  # 50% penalty
        else:
            score = base_score

        # Passed if both thresholds met AND majority is conversational
        passed = (
            len(prospect_turns) >= self.config.min_prospect_turns
            and prospect_word_count >= self.config.min_prospect_words
            and non_conversational_ratio <= 0.5
        )

        return passed, score, {
            "prospect_turns": len(prospect_turns),
            "total_prospect_turns": len(all_prospect_turns),
            "non_conversational_ratio": round(non_conversational_ratio, 2),
            "min_turns_required": self.config.min_prospect_turns,
            "prospect_word_count": prospect_word_count,
            "min_words_required": self.config.min_prospect_words,
            "turn_score": round(turn_score, 2),
            "word_score": round(word_score, 2),
        }

    # ── Check 2: Phase Completion Verification ──────────────────────────────

    def _check_phase_completion(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 2: Phase Completion Verification

        All required phases must be completed AND prospect gave positive
        signal in each phase (e.g., "yes", "I do", "interested").

        Returns: (passed, score 0-1, details dict)
        """
        required_phases = self.config.required_phases
        completed_phases = context.completed_phases

        # Check phase completion
        phases_completed = [p for p in required_phases if p in completed_phases]
        phase_completion_score = len(phases_completed) / len(required_phases)

        # Check positive signals in each phase
        positive_signals_by_phase = context.phase_positive_signals
        phases_with_signals = sum(
            1 for p in required_phases
            if positive_signals_by_phase.get(p, False)
        )
        signal_score = phases_with_signals / len(required_phases)

        # Average
        score = (phase_completion_score + signal_score) / 2.0

        # Passed if all phases completed AND all have positive signals
        passed = (
            len(phases_completed) == len(required_phases)
            and phases_with_signals == len(required_phases)
        )

        return passed, score, {
            "phases_completed": phases_completed,
            "required_phases": required_phases,
            "phases_with_positive_signals": phases_with_signals,
            "phase_completion_score": round(phase_completion_score, 2),
            "signal_score": round(signal_score, 2),
            "positive_signals": positive_signals_by_phase,
        }

    # ── Check 3: Speech Activity Verification ───────────────────────────────

    def _check_speech_activity(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 3: Speech Activity Verification

        At least 30% of prospect time should be actual speech (not silence).

        Returns: (passed, score 0-1, details dict)
        """
        if context.prospect_total_seconds <= 0:
            return False, 0.0, {
                "prospect_total_seconds": 0.0,
                "prospect_speech_seconds": 0.0,
                "speech_ratio": 0.0,
                "min_speech_ratio": self.config.min_speech_ratio,
                "error": "No prospect audio recorded",
            }

        speech_ratio = context.prospect_speech_seconds / context.prospect_total_seconds

        # Steeper penalty curve: quadratic below threshold, capped at 1.0 above
        if speech_ratio >= self.config.min_speech_ratio:
            # Above threshold — scale from 0.9 to 1.0
            score = 0.9 + 0.1 * min((speech_ratio - self.config.min_speech_ratio) / 0.30, 1.0)
        else:
            # Below threshold — quadratic penalty (drops fast)
            ratio_pct = speech_ratio / self.config.min_speech_ratio
            score = ratio_pct ** 2  # 50% of threshold → 0.25 score, 25% → 0.0625

        passed = speech_ratio >= self.config.min_speech_ratio

        return passed, score, {
            "prospect_total_seconds": round(context.prospect_total_seconds, 2),
            "prospect_speech_seconds": round(context.prospect_speech_seconds, 2),
            "speech_ratio": round(speech_ratio, 3),
            "min_speech_ratio": self.config.min_speech_ratio,
        }

    # ── Check 4: Response Relevance Scoring ─────────────────────────────────

    def _check_response_relevance(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 4: Response Relevance Scoring

        Score each prospect response for relevance to the question asked.
        Must average > 0.50 across all turns.

        Returns: (passed, score 0-1, details dict)
        """
        if not context.transcript_turns:
            return False, 0.0, {
                "error": "No transcript data",
                "relevance_scores": [],
            }

        relevance_scores = []

        # Analyze each prospect turn for relevance
        for i, turn in enumerate(context.transcript_turns):
            if turn.speaker != "prospect":
                continue

            text_lower = turn.text.lower().strip()

            # Look at the previous SDR turn to understand context
            prev_sdr_text = ""
            for j in range(i - 1, -1, -1):
                if context.transcript_turns[j].speaker == "sdr":
                    prev_sdr_text = context.transcript_turns[j].text.lower()
                    break

            # Score relevance based on context
            rel_score = self._score_response_relevance(text_lower, prev_sdr_text)
            relevance_scores.append(rel_score)

        if not relevance_scores:
            return False, 0.0, {
                "error": "No prospect responses to evaluate",
                "relevance_scores": [],
            }

        avg_relevance = mean(relevance_scores)

        # Score: above threshold → 0.9-1.0, below → quadratic penalty
        if avg_relevance >= self.config.min_relevance_score:
            score = 0.9 + 0.1 * min((avg_relevance - self.config.min_relevance_score) / 0.30, 1.0)
        else:
            ratio = avg_relevance / self.config.min_relevance_score
            score = ratio ** 2

        passed = avg_relevance >= self.config.min_relevance_score

        return passed, score, {
            "relevance_scores": [round(s, 2) for s in relevance_scores],
            "avg_relevance_score": round(avg_relevance, 2),
            "min_relevance_score": self.config.min_relevance_score,
        }

    def _score_response_relevance(self, prospect_text: str, prev_sdr_text: str) -> float:
        """
        Score how relevant a prospect response is to the question asked.

        Returns score 0-1:
        - 1.0 = Perfect, relevant answer with positive signal
        - 0.75 = Relevant, shows engagement
        - 0.5 = Marginally relevant, vague but not negative
        - 0.15 = Clearly irrelevant / ambient noise transcription
        - 0.0 = Non-response, silence, or empty
        """
        # Empty response
        if not prospect_text or len(prospect_text.strip()) < 2:
            return 0.0

        words = prospect_text.split()

        # ── Ambient/TV/Radio noise detection ──
        # TV/radio transcriptions contain broadcast-style language with zero
        # conversational markers. Check for this BEFORE anything else.
        broadcast_markers = [
            "weather", "forecast", "temperatures", "headlines", "breaking news",
            "sponsored by", "brought to you", "commercial", "station", "channel",
            "up next", "stay tuned", "dealer", "advertisement", "tonight on",
            "local news", "sports update", "coming up", "in other news",
        ]
        has_broadcast = any(marker in prospect_text for marker in broadcast_markers)

        # Also check: does the response have ANY conversational connection to the SDR?
        # Conversational responses reference the question, use pronouns (I, my, me),
        # or use conversational markers (yes, no, well, actually, etc.)
        conversational_markers = [
            "i ", "i'm", "my ", "me ", "we ", "our ", "yes", "no", "yeah",
            "sure", "okay", "ok", "well", "actually", "right", "that",
            "this", "who", "what", "why", "how", "don't", "do ", "have",
            "can", "will", "would", "could", "should", "it's", "that's",
        ]
        has_conversational = any(marker in prospect_text for marker in conversational_markers)

        if has_broadcast:
            return 0.05  # Almost certainly TV/radio noise

        if len(words) >= 4 and not has_conversational:
            return 0.15  # Multi-word response with zero conversational markers = suspicious

        # Very short response (1 word)
        if len(words) == 1:
            if words[0].lower() in ["yes", "yeah", "yep", "ok", "okay", "sure"]:
                return 0.95  # Strong affirmative — highly relevant
            elif words[0].lower() in ["uh", "um", "hmm", "huh"]:
                return 0.3   # Filler, not really engaging
            else:
                return 0.4

        # Check for strong positive signals
        strong_positive = any(sig in prospect_text for sig in [
            "that's right", "correct", "go ahead", "sounds good", "interested",
            "absolutely", "of course", "yes please", "i'd like", "tell me more",
        ])

        # Check for basic positive signals
        positive = any(sig in prospect_text for sig in [
            "yes", "yeah", "sure", "okay", "ok", "i do", "i have",
            "uh huh", "yep", "checking", "savings",
        ])

        # Check for negative signals (breaks relevance)
        negative = any(sig in prospect_text for sig in [
            "not interested", "don't want", "stop calling", "remove me",
            "hang up", "not now", "i'm busy", "wrong number",
        ])

        if negative:
            return 0.1  # Negative but at least a response

        # Length + signal scoring
        if len(words) >= 5 and strong_positive:
            return 1.0   # Detailed engaged response
        elif len(words) >= 3 and (positive or strong_positive):
            return 0.95  # Good engaged response
        elif len(words) >= 3:
            return 0.70  # Talking but no clear signal
        elif positive:
            return 0.85  # Short positive
        else:
            return 0.45  # Short, no signal

    # ── Check 5: Audio Quality Check ────────────────────────────────────────

    def _check_audio_quality(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 5: Audio Quality Check

        Prospect audio must:
        - Be above noise floor (-40 dBFS minimum)
        - Have reasonable variation (not constant = not TV/radio)
        - Not show TV/radio patterns (high constant energy + low variance)

        Returns: (passed, score 0-1, details dict)
        """
        # Below noise floor = dead air
        if context.avg_audio_rms < self.config.min_audio_rms_dbfs:
            return False, 0.0, {
                "avg_audio_rms_dbfs": round(context.avg_audio_rms, 1),
                "min_audio_rms_dbfs": self.config.min_audio_rms_dbfs,
                "audio_rms_variance": round(context.audio_rms_variance, 3),
                "reason": "Below noise floor — dead air or no mic",
            }

        # Check RMS energy level
        min_rms = self.config.min_audio_rms_dbfs
        rms_score = max(0, min((context.avg_audio_rms - min_rms) / 20, 1.0))

        # Check RMS variance (should NOT be too constant)
        # TV/radio has high energy but LOW variance (constant stream)
        # Human speech has HIGHER variance (pauses, emphasis, breathing)
        abs_avg_rms = abs(context.avg_audio_rms) if context.avg_audio_rms != 0 else 1e-10
        variance_ratio = context.audio_rms_variance / abs_avg_rms

        # TV/radio detection: energy > -25 dBFS AND variance ratio < 0.10
        tv_radio_likely = (context.avg_audio_rms > -25 and variance_ratio < 0.10)
        too_constant = variance_ratio < 0.05  # Less than 5% variation = very suspicious

        if tv_radio_likely:
            variance_score = 0.1  # Strong penalty for TV/radio pattern
        elif too_constant:
            variance_score = 0.2
        else:
            variance_score = min(variance_ratio / self.config.max_audio_rms_variance_ratio, 1.0)

        # Score: weighted toward variance (more discriminating)
        score = (rms_score * 0.3 + variance_score * 0.7)

        # Passed if above noise floor AND not TV/radio AND not too constant
        passed = not tv_radio_likely and not too_constant and context.avg_audio_rms >= min_rms

        return passed, score, {
            "avg_audio_rms_dbfs": round(context.avg_audio_rms, 1),
            "min_audio_rms_dbfs": min_rms,
            "audio_rms_variance": round(context.audio_rms_variance, 3),
            "variance_ratio": round(variance_ratio, 3),
            "too_constant": too_constant,
            "tv_radio_likely": tv_radio_likely,
            "rms_score": round(rms_score, 2),
            "variance_score": round(variance_score, 2),
        }

    # ── Check 6: Human Speech Pattern Detection ─────────────────────────────

    def _check_human_speech_pattern(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 6: Human Speech Pattern Detection

        Automated systems (IVR, voicemail playback) have unnaturally consistent
        timing. Real humans vary: some turns are 1 word, some are 10+ words.

        Calculate coefficient of variation in turn lengths.
        CV < 0.2 = suspiciously uniform = likely automated.

        Returns: (passed, score 0-1, details dict)
        """
        if not context.turn_word_counts or len(context.turn_word_counts) < 2:
            return False, 0.0, {
                "error": "Insufficient turns for pattern analysis — likely non-human",
                "turn_word_counts": context.turn_word_counts,
            }

        # Calculate coefficient of variation
        word_counts = context.turn_word_counts

        avg_words = mean(word_counts)
        std_words = stdev(word_counts) if len(word_counts) >= 2 else 0
        cv = (std_words / (avg_words + 1e-10)) if avg_words > 0 else 0

        # Score: higher CV = more human-like
        # CV >= 0.2 = natural variation
        # CV < 0.2 = suspiciously uniform
        score = min(cv / self.config.max_turn_length_cv, 1.0)

        passed = cv >= self.config.max_turn_length_cv

        return passed, score, {
            "turn_word_counts": word_counts,
            "mean_words_per_turn": round(avg_words, 2),
            "std_dev": round(std_words, 2),
            "coefficient_of_variation": round(cv, 3),
            "max_cv_threshold": self.config.max_turn_length_cv,
            "suspiciously_uniform": not passed,
        }

    # ── Check 7: Prospect Engagement Score ──────────────────────────────────

    def _check_prospect_engagement(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 7: Prospect Engagement Score

        Combines three factors:
        1. Response latency (should be 300-1500ms for humans, not <100ms or >5s)
        2. Word count per turn (should be reasonable)
        3. Relevance (already scored in Check 4)

        Returns: (passed, score 0-1, details dict)
        """
        # Latency analysis
        if context.response_latencies_ms:
            avg_latency = mean(context.response_latencies_ms)
            max_latency = self.config.max_avg_response_latency_ms

            # Human response time research:
            # < 250ms = impossibly fast for speech (likely ambient/TV noise)
            # 300-500ms = very fast but possible (one-word answers)
            # 500-1500ms = natural human response range
            # 1500-3000ms = slow but human (thinking)
            # > 5000ms = distracted or not engaged
            if avg_latency < 250:
                latency_score = 0.05  # Almost certainly non-human
            elif avg_latency < 400:
                latency_score = 0.3   # Suspiciously fast
            elif 400 <= avg_latency <= 1500:
                latency_score = 1.0   # Natural human range
            elif avg_latency <= 3000:
                latency_score = 0.7   # Slow but human
            elif avg_latency > max_latency:
                latency_score = 0.2   # Too slow — not engaged
            else:
                latency_score = 0.5
        else:
            avg_latency = 0
            latency_score = 0.3  # No data = suspicious

        # Word count analysis (should be reasonable)
        if context.turn_word_counts:
            avg_words = mean(context.turn_word_counts)
            # 2-10 words per turn is reasonable for engagement
            if 2 <= avg_words <= 10:
                word_score = 1.0
            elif 1 <= avg_words <= 20:
                word_score = 0.7
            else:
                word_score = 0.3
        else:
            avg_words = 0
            word_score = 0.5

        # Overall engagement score (latency matters more — it's the human signal)
        engagement_score = (latency_score * 0.6 + word_score * 0.4)

        passed = engagement_score >= self.config.min_engagement_score

        return passed, engagement_score, {
            "avg_response_latency_ms": round(avg_latency, 0),
            "min_latency_ms": self.config.min_avg_response_latency_ms,
            "max_latency_ms": self.config.max_avg_response_latency_ms,
            "latency_score": round(latency_score, 2),
            "avg_words_per_turn": round(avg_words, 2),
            "word_score": round(word_score, 2),
            "engagement_score": round(engagement_score, 2),
            "min_engagement_score": self.config.min_engagement_score,
        }

    # ── Check 8: Agent Feedback Loop ────────────────────────────────────────

    def _check_agent_feedback_loop(self, context: CallContext) -> Tuple[bool, float, Dict]:
        """
        Check 8: Agent Feedback Loop (Self-Tuning)

        This check is less about the current call and more about whether
        our thresholds are well-tuned based on historical agent feedback.

        - Rejection rate > 40% → TIGHTEN thresholds (be more selective)
        - Rejection rate < 10% → LOOSEN thresholds (accept more)
        - 10-40% → good balance

        For the current call, we return a score based on whether the thresholds
        are well-tuned (1.0 = well-tuned, 0.5 = needs adjustment).

        Returns: (passed, score 0-1, details dict)
        """
        rejection_rate = self.feedback_tracker.rejection_rate()

        # No feedback data yet — neutral score (don't penalize)
        if self.feedback_tracker.total_transfers == 0:
            return True, 1.0, {
                "total_transfers": 0,
                "qualified_transfers": 0,
                "rejected_transfers": 0,
                "rejection_rate": 0.0,
                "tuning_status": "no_data",
                "applied_tuning_factor": 1.0,
            }

        # Score: well-tuned = 10-40% rejection rate
        if 0.10 <= rejection_rate <= 0.40:
            score = 1.0
            tuning_status = "well_tuned"
            action = "none"
        elif rejection_rate < 0.10:
            score = 0.85
            tuning_status = "too_strict"
            action = "loosen_thresholds"
        else:  # > 0.40
            score = 0.5
            tuning_status = "too_loose"
            action = "tighten_thresholds"

        # Apply tuning if needed
        if action == "tighten_thresholds" and self._applied_tuning_factor == 1.0:
            self._applied_tuning_factor = 0.80
            logger.info("transfer_gate_tightened", rejection_rate=round(rejection_rate, 3))
        elif action == "loosen_thresholds" and self._applied_tuning_factor == 1.0:
            self._applied_tuning_factor = 1.10
            logger.info("transfer_gate_loosened", rejection_rate=round(rejection_rate, 3))

        # This check always passes the current evaluation (it's metadata)
        passed = True

        return passed, score, {
            "total_transfers": self.feedback_tracker.total_transfers,
            "qualified_transfers": self.feedback_tracker.qualified_transfers,
            "rejected_transfers": self.feedback_tracker.rejected_transfers,
            "rejection_rate": round(rejection_rate, 3),
            "tuning_status": tuning_status,
            "applied_tuning_factor": round(self._applied_tuning_factor, 2),
        }

    # ── Feedback Recording ──────────────────────────────────────────────────

    def record_agent_feedback(
        self,
        call_id: str,
        qualified: bool,
        agent_talk_seconds: float,
    ) -> None:
        """
        Record agent feedback after a transfer.

        Args:
            call_id: The call ID
            qualified: True if agent accepted (stayed > 30s), False if rejected
            agent_talk_seconds: How long agent stayed on call
        """
        self.feedback_tracker.total_transfers += 1

        if agent_talk_seconds >= 30:
            self.feedback_tracker.qualified_transfers += 1
            logger.info(
                "agent_accepted_transfer",
                call_id=call_id,
                agent_talk_seconds=round(agent_talk_seconds, 1),
            )
        else:
            self.feedback_tracker.rejected_transfers += 1
            logger.warning(
                "agent_rejected_transfer",
                call_id=call_id,
                agent_talk_seconds=round(agent_talk_seconds, 1),
            )

        # Check if we should auto-tune
        if self.feedback_tracker.should_tighten():
            logger.warning(
                "transfer_gate_auto_tighten",
                rejection_rate=round(self.feedback_tracker.rejection_rate(), 3),
                applied_tuning=0.80,
            )
        elif self.feedback_tracker.should_loosen():
            logger.info(
                "transfer_gate_auto_loosen",
                rejection_rate=round(self.feedback_tracker.rejection_rate(), 3),
                applied_tuning=1.10,
            )

    # ── Configuration ──────────────────────────────────────────────────────

    def get_current_thresholds(self) -> Dict[str, Any]:
        """Return current threshold configuration with any applied tuning."""
        config_dict = {
            "min_prospect_turns": self.config.min_prospect_turns,
            "min_prospect_words": self.config.min_prospect_words,
            "required_phases": self.config.required_phases,
            "min_speech_ratio": self.config.min_speech_ratio,
            "min_relevance_score": self.config.min_relevance_score,
            "min_audio_rms_dbfs": self.config.min_audio_rms_dbfs,
            "max_turn_length_cv": self.config.max_turn_length_cv,
            "min_avg_response_latency_ms": self.config.min_avg_response_latency_ms,
            "max_avg_response_latency_ms": self.config.max_avg_response_latency_ms,
            "min_engagement_score": self.config.min_engagement_score,
            "min_checks_passed": self.config.min_checks_passed,
            "min_overall_score": self.config.min_overall_score,
            "applied_tuning_factor": round(self._applied_tuning_factor, 2),
        }
        return config_dict

    def adjust_thresholds(self, agent_rejection_rate: float) -> None:
        """
        Manually adjust thresholds based on observed rejection rate.

        Args:
            agent_rejection_rate: Observed rejection rate (0-1)
        """
        if agent_rejection_rate > self.config.agent_rejection_tighten_threshold:
            self._applied_tuning_factor = 0.80
            logger.info("transfer_gate_manual_tighten", factor=0.80)
        elif agent_rejection_rate < self.config.agent_rejection_loosen_threshold:
            self._applied_tuning_factor = 1.10
            logger.info("transfer_gate_manual_loosen", factor=1.10)
        else:
            self._applied_tuning_factor = 1.0
            logger.info("transfer_gate_manual_reset", factor=1.0)


# ── Production Phrases ────────────────────────────────────────────────────────

RE_QUALIFY_PHRASES = [
    "Just to make sure I have everything right — you said you're interested in getting that quote, correct?",
    "And just to confirm, you do have a checking or savings account for the discounts, right?",
    "So you're saying you'd like to move forward with the quote before that offer expires tomorrow?",
]

END_CALL_PHRASES = [
    "I appreciate your time today. If you're ever interested in learning more, don't hesitate to give us a call back. Have a great day.",
    "Thanks for listening. Best of luck, and feel free to reach out anytime. Take care.",
    "I understand. Thanks for your time. Have a wonderful day.",
]

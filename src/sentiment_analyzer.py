"""
WellHeard AI — Sentiment Analyzer

Text-based sentiment detection for real-time adaptation of AI response style.
Analyzes prospect's transcribed speech to detect emotional state and sentiment shifts,
enabling contextual response generation and speed modulation.

Sentiment States:
- POSITIVE: Engaged, interested, moving forward
- NEUTRAL: Baseline, no strong emotional signal
- HESITANT: Uncertain, needs reassurance, not ready to decide
- FRUSTRATED: Irritated, annoyed, at risk of drop-off
- DISENGAGED: Checked out, minimal engagement, single-word responses
"""

import re
import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Tuple

logger = structlog.get_logger()


class SentimentState(str, Enum):
    """Five distinct prospect emotional states."""
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    HESITANT = "hesitant"
    FRUSTRATED = "frustrated"
    DISENGAGED = "disengaged"


@dataclass
class SentimentResult:
    """Result of sentiment analysis for a single turn."""
    state: SentimentState
    confidence: float  # 0.0-1.0
    signals: List[str] = field(default_factory=list)  # Detection signals found
    shift_detected: bool = False  # True if sentiment changed from previous
    prev_state: Optional[SentimentState] = None


class SentimentAnalyzer:
    """
    Analyzes prospect text for sentiment state and tracks trends over time.

    Uses keyword/pattern matching (no ML dependencies):
    - POSITIVE: Affirmations, interest signals, enthusiasm
    - HESITANT: Uncertainty, conditional language, request for time
    - FRUSTRATED: Anger signals, rejection, urgency complaints, caps, exclamation
    - DISENGAGED: Single words, very short responses, minimal engagement markers
    - NEUTRAL: Default when no strong signals match

    Tracks sentiment over last 3 turns to detect trends and shifts.
    """

    def __init__(self, lookback_turns: int = 3):
        """
        Initialize sentiment analyzer.

        Args:
            lookback_turns: Number of previous turns to track for trend detection.
        """
        self.lookback_turns = lookback_turns
        self.history: List[SentimentState] = []

    def analyze(self, text: str) -> SentimentResult:
        """
        Analyze a single text turn for sentiment.

        Returns:
            SentimentResult with detected state, confidence, and signals.
        """
        if not text or not text.strip():
            return SentimentResult(
                state=SentimentState.NEUTRAL,
                confidence=0.0,
                signals=["empty_input"]
            )

        text_clean = text.strip().lower()
        word_count = len(text_clean.split())

        # Detect each sentiment type with scoring
        frustration_score = self._detect_frustration(text_clean)
        disengagement_score = self._detect_disengagement(text_clean, word_count)
        hesitation_score = self._detect_hesitation(text_clean)
        positivity_score = self._detect_positivity(text_clean)

        # Determine state based on highest score + priority rules
        scores = {
            SentimentState.FRUSTRATED: frustration_score,
            SentimentState.DISENGAGED: disengagement_score,
            SentimentState.HESITANT: hesitation_score,
            SentimentState.POSITIVE: positivity_score,
        }

        # Priority logic: higher score wins, with tiebreaker rules
        # FRUSTRATED takes highest priority ONLY if score > 0.7 (strong signal)
        if frustration_score > 0.7:
            state = SentimentState.FRUSTRATED
            confidence = frustration_score
        # DISENGAGED: very short responses
        elif disengagement_score > 0.5:
            state = SentimentState.DISENGAGED
            confidence = disengagement_score
        # Otherwise, find the highest remaining score
        else:
            remaining = {
                SentimentState.HESITANT: hesitation_score,
                SentimentState.POSITIVE: positivity_score,
                SentimentState.FRUSTRATED: frustration_score,  # Lower threshold
            }
            if max(remaining.values()) > 0:
                state = max(remaining.items(), key=lambda x: x[1])[0]
                confidence = remaining[state]
            else:
                state = SentimentState.NEUTRAL
                confidence = 0.5  # Medium confidence for default state

        # Collect detected signals for logging
        signals = []
        if frustration_score > 0:
            signals.append(f"frustration({frustration_score:.2f})")
        if disengagement_score > 0:
            signals.append(f"disengagement({disengagement_score:.2f})")
        if hesitation_score > 0:
            signals.append(f"hesitation({hesitation_score:.2f})")
        if positivity_score > 0:
            signals.append(f"positivity({positivity_score:.2f})")

        # Detect shift from previous state
        prev_state = self.history[-1] if self.history else None
        shift_detected = prev_state is not None and prev_state != state

        # Record this sentiment in history
        self.history.append(state)
        if len(self.history) > self.lookback_turns:
            self.history = self.history[-self.lookback_turns:]

        return SentimentResult(
            state=state,
            confidence=confidence,
            signals=signals,
            shift_detected=shift_detected,
            prev_state=prev_state
        )

    def _detect_frustration(self, text: str) -> float:
        """
        Detect frustration signals: anger, rejection, urgency complaints.

        Returns:
            Score 0.0-1.0. Returns 1.0 (high confidence) if any strong signal found.
        """
        # Strong rejection phrases
        strong_rejects = {
            "stop calling",
            "stop call",
            "take me off",
            "remove me",
            "do not call",
            "don't call",
            "don't call",
            "quit calling",
            "leave me alone",
            "i said no",
            "absolutely not",
            "how did you get",
            "i don't want this",
            "quit bothering me",
        }
        for phrase in strong_rejects:
            if phrase in text:
                return 1.0

        # Moderate frustration markers
        moderate_markers = {
            "not interested": 0.9,
            "not right now": 0.7,
            "i don't have time": 0.8,
            "call me back": 0.6,
            "i'm busy": 0.6,
            "this is annoying": 0.9,
            "why are you": 0.7,
        }
        for marker, score in moderate_markers.items():
            if marker in text:
                return score

        # Exclamation patterns (multiple or intense)
        exclamation_count = text.count("!")
        if exclamation_count >= 2:
            return 0.8
        elif exclamation_count == 1 and any(w in text for w in ["no!", "stop!", "don't!"]):
            return 0.7

        # ALL CAPS for 3+ words
        all_caps_words = re.findall(r'\b[A-Z]{3,}\b', text)
        if len(all_caps_words) >= 2:
            return 0.75

        return 0.0

    def _detect_disengagement(self, text: str, word_count: int) -> float:
        """
        Detect disengagement: very short responses, minimal substance, repetition.

        Returns:
            Score 0.0-1.0. High score for single-word or extremely brief responses.
        """
        # Single word responses
        if word_count <= 1:
            return 0.95

        # Very short responses (2-4 words) with minimal engagement
        if word_count <= 4:
            engagement_words = {
                "yes", "yeah", "no", "nope", "ok", "okay", "uh huh", "sure", "hmm", "um"
            }
            if text in engagement_words or all(w in engagement_words for w in text.split()):
                return 0.85

        # Repeated minimal responses ("yeah yeah", "ok ok", "uh huh yeah")
        if re.search(r'\b(\w+)\b.*\b\1\b', text):  # Word repeated in same response
            if word_count <= 5:
                return 0.7

        # Silence markers (represented as very sparse content)
        silence_patterns = ["mm", "hmm", "uh", "um", "uh huh", "yeah yeah", "ok ok"]
        if text in silence_patterns or (word_count <= 2 and any(p in text for p in silence_patterns)):
            return 0.75

        return 0.0

    def _detect_hesitation(self, text: str) -> float:
        """
        Detect hesitation: uncertainty, conditional language, need for time.

        Returns:
            Score 0.0-1.0.
        """
        hesitation_markers = {
            "i don't know": 0.8,
            "i'm not sure": 0.75,
            "maybe": 0.6,
            "i guess": 0.65,
            "let me think": 0.7,
            "i need to think": 0.75,
            "not sure about": 0.65,
            "i'm uncertain": 0.8,
            "unsure": 0.75,
            "let me check": 0.5,
            "let me call back": 0.65,
            "call me back": 0.65,
            "call later": 0.65,
            "another time": 0.5,
            "i don't think so": 0.7,
            "kind of": 0.4,
            "sort of": 0.4,
            "i guess so": 0.65,
            "possibly": 0.5,
            "hmm": 0.4,
        }

        for marker, score in hesitation_markers.items():
            if marker in text:
                return score

        # Conditional language ("if", "assuming", "depending")
        conditionals = ["if you", "assuming", "depends", "contingent"]
        if any(c in text for c in conditionals):
            return 0.5

        return 0.0

    def _detect_positivity(self, text: str) -> float:
        """
        Detect positive signals: interest, affirmation, enthusiasm.

        Returns:
            Score 0.0-1.0.
        """
        positive_markers = {
            "yes": 0.7,
            "yeah": 0.65,
            "sure": 0.7,
            "okay": 0.65,
            "ok": 0.6,
            "absolutely": 0.9,
            "definitely": 0.85,
            "interested": 0.9,
            "tell me more": 0.95,
            "sounds good": 0.85,
            "i like that": 0.9,
            "love it": 0.95,
            "love that": 0.95,
            "great": 0.8,
            "perfect": 0.85,
            "excellent": 0.85,
            "let's do it": 0.9,
            "let's go": 0.85,
            "sounds interesting": 0.85,
            "i'm interested": 0.9,
            "why not": 0.75,
            "go ahead": 0.8,
            "sure thing": 0.85,
            "alright": 0.7,
            "good idea": 0.85,
            "makes sense": 0.75,
            "that works": 0.8,
            "thanks": 0.5,
            "appreciate it": 0.6,
        }

        for marker, score in positive_markers.items():
            if marker in text:
                return score

        return 0.0

    def get_trend(self) -> Tuple[SentimentState, str]:
        """
        Get current sentiment trend over last N turns.

        Returns:
            Tuple of (current_state, trend_description)
            Trend descriptions:
            - "stable": Same state for all lookback turns
            - "improving": Getting more positive/engaged
            - "declining": Getting more negative/frustrated/disengaged
            - "volatile": Shifting rapidly (3+ different states)
        """
        if not self.history:
            return SentimentState.NEUTRAL, "no_history"

        current = self.history[-1]

        if len(self.history) < 2:
            return current, "single_turn"

        # Check stability (same state throughout)
        if len(set(self.history)) == 1:
            return current, "stable"

        # Define state valence: higher = more positive
        STATE_VALENCE = {
            SentimentState.POSITIVE: 3,
            SentimentState.NEUTRAL: 2,
            SentimentState.HESITANT: 1,
            SentimentState.DISENGAGED: 0,
            SentimentState.FRUSTRATED: -1,
        }

        # Check overall direction
        first_valence = STATE_VALENCE.get(self.history[0], 2)
        current_valence = STATE_VALENCE.get(current, 2)

        # Check volatility (3+ different states in lookback window)
        if len(set(self.history)) >= 3:
            return current, "volatile"

        # Determine direction based on overall trend
        if current_valence > first_valence:
            return current, "improving"
        elif current_valence < first_valence:
            return current, "declining"
        else:
            return current, "stable"

    def is_sustained_frustration(self, min_turns: int = 2) -> bool:
        """
        Check if prospect has been frustrated for N+ consecutive turns.
        Used to trigger graceful exit.

        Args:
            min_turns: Minimum number of consecutive frustrated turns to trigger exit.

        Returns:
            True if frustrated for min_turns or more consecutive turns.
        """
        if len(self.history) < min_turns:
            return False

        recent = self.history[-min_turns:]
        return all(s == SentimentState.FRUSTRATED for s in recent)

    def get_system_prompt_injection(self) -> str:
        """
        Generate LLM system prompt injection based on current sentiment.

        Returns:
            String to inject into LLM system prompt to guide response style.
        """
        state = self.history[-1] if self.history else SentimentState.NEUTRAL

        injections = {
            SentimentState.POSITIVE: (
                "[SENTIMENT: POSITIVE] Prospect is engaged and interested. "
                "Match their energy, show enthusiasm, keep momentum. "
                "Move to the next step smoothly. "
                "Keep responses short and punchy."
            ),
            SentimentState.NEUTRAL: (
                "[SENTIMENT: NEUTRAL] Prospect is baseline engaged. "
                "Maintain conversational tone, ask clarifying questions, "
                "move the conversation forward naturally."
            ),
            SentimentState.HESITANT: (
                "[SENTIMENT: HESITANT] Prospect is uncertain or thinking. "
                "Slow down, be patient, ask open questions to let them think. "
                "Don't push the offer. Give reassurance that there's no rush. "
                "Listen more, talk less."
            ),
            SentimentState.FRUSTRATED: (
                "[SENTIMENT: FRUSTRATED] Prospect is irritated or annoyed. "
                "Acknowledge their feeling immediately. Be empathetic but brief. "
                "DO NOT push the offer. Offer to end the call gracefully. "
                "Priority: de-escalate, not close the sale."
            ),
            SentimentState.DISENGAGED: (
                "[SENTIMENT: DISENGAGED] Prospect is checked out, giving minimal responses. "
                "Ask a direct, specific question to re-engage them. "
                "If no response, offer to call back another time. "
                "Don't force the conversation."
            ),
        }

        return injections.get(state, injections[SentimentState.NEUTRAL])

    def get_speed_adjustment(self) -> float:
        """
        Get speech speed adjustment multiplier based on sentiment.
        Applied to base SPEED setting in inbound_handler.py.

        Returns:
            Multiplier to apply to base speed:
            - 0.95 (slow 5%) for frustrated prospects
            - 0.97 (normal) for hesitant/neutral
            - 1.03 (fast 3%) for positive/engaged
        """
        state = self.history[-1] if self.history else SentimentState.NEUTRAL

        adjustments = {
            SentimentState.FRUSTRATED: 0.95,  # Slow down, calming
            SentimentState.DISENGAGED: 0.97,  # Normal, deliberate
            SentimentState.HESITANT: 0.97,    # Normal, patient
            SentimentState.NEUTRAL: 0.97,     # Normal baseline
            SentimentState.POSITIVE: 1.03,    # Slight speed up, match energy
        }

        return adjustments.get(state, 1.0)

    def reset(self):
        """Clear sentiment history (call ended)."""
        self.history = []

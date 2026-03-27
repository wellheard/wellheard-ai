"""
WellHeard AI — Call State Tracker

Tracks structured call progress to prevent the AI from:
1. Repeating phrases it already said
2. Re-asking questions it already got answers to
3. Skipping steps or going backwards in the script
4. Losing track of collected information

Also tracks sentiment state for adaptive response generation.

The state is injected into every LLM call as a structured block,
giving the model a clear picture of where it is in the conversation.
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = structlog.get_logger()


# Import sentiment analyzer for adaptive responses
try:
    from src.sentiment_analyzer import SentimentAnalyzer, SentimentState
except ImportError:
    SentimentAnalyzer = None
    SentimentState = None


class ScriptStep(str, Enum):
    """The 3-step qualification script."""
    CONFIRM_INTEREST = "confirm_interest"   # Step 1: Does that ring a bell? → interest
    BANK_ACCOUNT = "bank_account"           # Step 2: Checking or savings?
    TRANSFER = "transfer"                   # Step 3: Connect to licensed agent
    COMPLETED = "completed"                 # Call qualified and transferred


class StepStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass
class CallStateTracker:
    """
    Structured call state that persists across all turns.

    Injected into the LLM system prompt so it always knows:
    - Which step it's on (and which are done)
    - What information it already collected
    - What questions it already asked (never ask again)
    - What the prospect already told it (never re-ask)
    """

    call_id: str = ""

    # ── Script Progress ────────────────────────────────────────────
    current_step: ScriptStep = ScriptStep.CONFIRM_INTEREST
    step_status: dict = field(default_factory=lambda: {
        ScriptStep.CONFIRM_INTEREST: StepStatus.NOT_STARTED,
        ScriptStep.BANK_ACCOUNT: StepStatus.NOT_STARTED,
        ScriptStep.TRANSFER: StepStatus.NOT_STARTED,
    })

    # ── Collected Information ──────────────────────────────────────
    # Once collected, NEVER ask again.
    prospect_remembers_request: Optional[bool] = None   # "Does that ring a bell?"
    prospect_interested: Optional[bool] = None          # Interested in quote?
    has_bank_account: Optional[bool] = None             # Checking or savings?
    bank_account_type: Optional[str] = None             # "checking", "savings", "both", "neither"
    transfer_accepted: Optional[bool] = None            # Agreed to connect with agent?

    # ── Questions Already Asked ────────────────────────────────────
    # Track exact questions so we never repeat them.
    questions_asked: list = field(default_factory=list)

    # ── Objections Handled ─────────────────────────────────────────
    objections_handled: list = field(default_factory=list)

    # ── Key Phrases Already Said ───────────────────────────────────
    # Track distinctive phrases to prevent verbatim repetition.
    key_phrases_said: list = field(default_factory=list)

    # ── Timing ─────────────────────────────────────────────────────
    turn_count: int = 0
    last_update_time: float = 0.0

    # ── Sentiment Tracking (Adaptive Responses) ────────────────────
    # Initialized lazily on first turn
    sentiment_analyzer: Optional['SentimentAnalyzer'] = field(default=None, init=False, repr=False)

    def advance_step(self):
        """Move to the next script step."""
        order = [ScriptStep.CONFIRM_INTEREST, ScriptStep.BANK_ACCOUNT, ScriptStep.TRANSFER]
        try:
            idx = order.index(self.current_step)
            self.step_status[self.current_step] = StepStatus.COMPLETED
            if idx + 1 < len(order):
                self.current_step = order[idx + 1]
                self.step_status[self.current_step] = StepStatus.IN_PROGRESS
            else:
                self.current_step = ScriptStep.COMPLETED
        except ValueError:
            pass  # Already completed

    def record_question_asked(self, question: str):
        """Record that we asked a specific question — never ask it again."""
        # Store a normalized version
        q_lower = question.strip().lower().rstrip("?").strip()
        if q_lower and q_lower not in [q.lower() for q in self.questions_asked]:
            self.questions_asked.append(question.strip())
            # Keep only last 10 to avoid bloat
            if len(self.questions_asked) > 10:
                self.questions_asked = self.questions_asked[-10:]

    def record_objection_handled(self, objection: str):
        """Record that we handled an objection — don't re-handle the same way."""
        obj_lower = objection.strip().lower()
        if obj_lower and obj_lower not in [o.lower() for o in self.objections_handled]:
            self.objections_handled.append(objection.strip())

    def record_key_phrase(self, phrase: str):
        """Record a distinctive phrase we said — don't say it again verbatim."""
        if phrase and len(phrase) > 20:  # Only track substantial phrases
            self.key_phrases_said.append(phrase[:150])
            if len(self.key_phrases_said) > 8:
                self.key_phrases_said = self.key_phrases_said[-8:]

    def update_from_exchange(self, user_text: str, assistant_text: str):
        """
        Analyze a user↔assistant exchange to update call state.
        Called after every turn completes.
        """
        self.turn_count += 1
        self.last_update_time = time.time()
        user_lower = user_text.strip().lower()
        asst_lower = assistant_text.strip().lower()

        # ── Detect what step we're on based on assistant response content ──

        # Step 1 detection: confirm interest
        if self.current_step == ScriptStep.CONFIRM_INTEREST:
            self.step_status[ScriptStep.CONFIRM_INTEREST] = StepStatus.IN_PROGRESS

            # Did we get an answer about interest?
            affirmatives = {"yes", "yeah", "yep", "sure", "okay", "ok", "sounds good",
                           "go ahead", "alright", "absolutely", "definitely", "of course",
                           "i'd like that", "tell me more", "i'm interested", "why not",
                           "let's do it", "sure thing"}
            negatives = {"no", "not interested", "no thanks", "don't want", "stop calling",
                        "remove me", "take me off", "do not call", "not right now"}

            # Check if user expressed interest or rejection
            for phrase in affirmatives:
                if phrase in user_lower:
                    self.prospect_interested = True
                    break
            for phrase in negatives:
                if phrase in user_lower:
                    self.prospect_interested = False
                    break

            # Check if the user remembered the request
            if any(w in user_lower for w in ["i remember", "rings a bell", "i did", "i do",
                                              "that's right", "i think so"]):
                self.prospect_remembers_request = True
            elif any(w in user_lower for w in ["don't remember", "don't recall", "no idea",
                                                "i never", "not sure"]):
                self.prospect_remembers_request = False

            # Advance to Step 2 when prospect confirms interest OR assistant moves to bank
            if self.prospect_interested:
                self.advance_step()
            elif any(w in asst_lower for w in ["checking", "savings", "bank account"]):
                self.prospect_interested = True
                self.advance_step()

        # Step 2 detection: bank account
        if self.current_step == ScriptStep.BANK_ACCOUNT:
            self.step_status[ScriptStep.BANK_ACCOUNT] = StepStatus.IN_PROGRESS

            # ── Detect "you already asked that" complaints ──
            # If the prospect says we already asked, advance immediately to avoid looping
            already_asked_markers = [
                "already asked", "you just asked", "asked me that",
                "you already", "said that already", "told you",
                "already told", "just said", "already answered",
                "you asked that", "asked that before", "same question",
            ]
            if any(marker in user_lower for marker in already_asked_markers):
                # They're frustrated — assume bank account is confirmed and move on
                if self.has_bank_account is None:
                    self.has_bank_account = True
                    self.bank_account_type = "assumed_yes"
                self.advance_step()
                logger.info("bank_account_auto_advanced_already_asked",
                    call_id=self.call_id, user_text=user_text[:80])

            # Did we get bank account info?
            elif "checking" in user_lower and "savings" in user_lower:
                self.has_bank_account = True
                self.bank_account_type = "both"
                self.advance_step()
            elif "checking" in user_lower:
                self.has_bank_account = True
                self.bank_account_type = "checking"
                self.advance_step()
            elif "savings" in user_lower:
                self.has_bank_account = True
                self.bank_account_type = "savings"
                self.advance_step()
            elif any(w in user_lower for w in ["neither", "don't have", "no bank",
                                                "no i don't", "nope"]):
                self.has_bank_account = False
                self.bank_account_type = "neither"
                self.advance_step()
            # If user says yes/yeah to the bank question — we're on BANK_ACCOUNT step,
            # so any affirmative response IS about the bank account. No need to check
            # assistant's words (the assistant may already be talking about transfer).
            elif any(w in user_lower for w in ["yes", "yeah", "yep", "i do", "sure",
                                                "of course", "both", "uh huh", "mm hmm",
                                                "yup", "right"]):
                self.has_bank_account = True
                self.bank_account_type = "yes"
                self.advance_step()

            # If assistant is talking about transfer/agent and we still haven't advanced,
            # check if bank account was already collected in a prior turn.
            # Use elif to prevent double-advancing within the same update_from_exchange call.
            elif any(w in asst_lower for w in ["licensed agent", "transfer", "connecting you",
                                                "agent standing by"]):
                if self.has_bank_account is not None:
                    # Bank account was answered in a prior turn — safe to advance
                    self.advance_step()
                # else: bank account not yet answered — stay on step 2

        # Step 3 detection: transfer
        if self.current_step == ScriptStep.TRANSFER:
            self.step_status[ScriptStep.TRANSFER] = StepStatus.IN_PROGRESS
            if any(w in user_lower for w in ["yes", "yeah", "okay", "sure", "sounds good"]):
                self.transfer_accepted = True

        # ── Track questions asked ──
        # Extract questions from assistant text
        sentences = assistant_text.replace("?", "?\n").split("\n")
        for s in sentences:
            s = s.strip()
            if s.endswith("?") and len(s) > 10:
                self.record_question_asked(s)

        # ── Track key phrases ──
        self.record_key_phrase(assistant_text)

        # ── Detect objections ──
        objection_markers = {
            "already insured": "already_insured",
            "can't afford": "cant_afford",
            "how much": "pricing",
            "send info": "send_info",
            "call later": "call_later",
            "not interested": "not_interested",
            "don't trust": "trust",
            "robot": "robot_check",
            "ai": "ai_check",
            "what company": "company_question",
        }
        for marker, label in objection_markers.items():
            if marker in user_lower:
                self.record_objection_handled(label)

        logger.debug("call_state_updated",
            call_id=self.call_id,
            turn=self.turn_count,
            step=self.current_step.value,
            interested=self.prospect_interested,
            bank=self.bank_account_type,
            questions_count=len(self.questions_asked),
        )

    def analyze_prospect_sentiment(self, text: str) -> Optional[dict]:
        """
        Analyze prospect text for sentiment state.

        Args:
            text: Prospect's transcribed speech

        Returns:
            Dict with sentiment state, confidence, signals, and trend info.
            Returns None if sentiment analyzer not available.
        """
        if not SentimentAnalyzer:
            return None

        # Initialize sentiment analyzer on first turn
        if self.sentiment_analyzer is None:
            self.sentiment_analyzer = SentimentAnalyzer(lookback_turns=3)

        result = self.sentiment_analyzer.analyze(text)

        return {
            "state": result.state.value,
            "confidence": round(result.confidence, 3),
            "signals": result.signals,
            "shift_detected": result.shift_detected,
            "prev_state": result.prev_state.value if result.prev_state else None,
            "trend": self.sentiment_analyzer.get_trend()[1],
            "sustained_frustration": self.sentiment_analyzer.is_sustained_frustration(min_turns=2),
        }

    def get_sentiment_prompt_injection(self) -> str:
        """
        Get sentiment-based system prompt injection for LLM.

        Returns:
            Prompt injection string to guide response style.
            Empty string if sentiment analyzer not initialized.
        """
        if not self.sentiment_analyzer or not SentimentAnalyzer:
            return ""

        return self.sentiment_analyzer.get_system_prompt_injection()

    def get_speech_speed_adjustment(self) -> float:
        """
        Get speech speed adjustment multiplier based on current sentiment.

        Returns:
            Multiplier (0.95-1.03) to apply to base SPEED.
            Returns 1.0 (no adjustment) if sentiment not initialized.
        """
        if not self.sentiment_analyzer or not SentimentAnalyzer:
            return 1.0

        return self.sentiment_analyzer.get_speed_adjustment()

    def to_prompt_block(self) -> str:
        """
        Generate a structured state block to inject into the LLM system prompt.
        This gives the model a clear, unambiguous picture of where it is.
        """
        lines = []
        lines.append("[CALL STATE — READ CAREFULLY BEFORE RESPONDING]")
        lines.append(f"Turn: {self.turn_count}")
        lines.append(f"Current step: {self.current_step.value.upper()}")

        # Step progress
        step_display = {
            ScriptStep.CONFIRM_INTEREST: "Step 1 (Confirm Interest)",
            ScriptStep.BANK_ACCOUNT: "Step 2 (Bank Account)",
            ScriptStep.TRANSFER: "Step 3 (Transfer)",
        }
        for step, label in step_display.items():
            status = self.step_status.get(step, StepStatus.NOT_STARTED)
            lines.append(f"  {label}: {status.value}")

        # Collected information
        lines.append("")
        lines.append("INFORMATION ALREADY COLLECTED (do NOT re-ask):")
        if self.prospect_remembers_request is not None:
            lines.append(f"  - Remembers filling out request: {'YES' if self.prospect_remembers_request else 'NO'}")
        if self.prospect_interested is not None:
            lines.append(f"  - Interested in quote: {'YES' if self.prospect_interested else 'NO'}")
        if self.has_bank_account is not None:
            lines.append(f"  - Has bank account: {'YES' if self.has_bank_account else 'NO'} ({self.bank_account_type or 'unknown type'})")
        if self.transfer_accepted is not None:
            lines.append(f"  - Accepted transfer: {'YES' if self.transfer_accepted else 'NO'}")

        # Questions already asked
        if self.questions_asked:
            lines.append("")
            lines.append("QUESTIONS YOU ALREADY ASKED (NEVER ask these again):")
            for q in self.questions_asked[-5:]:  # Show last 5
                lines.append(f"  - \"{q[:120]}\"")

        # Objections handled
        if self.objections_handled:
            lines.append("")
            lines.append(f"OBJECTIONS ALREADY HANDLED: {', '.join(self.objections_handled)}")

        lines.append("")
        lines.append("→ Move FORWARD. Never re-ask anything above.")
        lines.append("[END CALL STATE]")

        return "\n".join(lines)

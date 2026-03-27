"""
WellHeard AI — Text-Level Call Simulator

Runs LLM-to-LLM conversations to validate conversation quality without telephony.
Dramatically faster than audio-based tests (seconds vs minutes per scenario).

Tests the CONVERSATION LOGIC: script adherence, objection handling, step progression,
repetition avoidance, brevity, and naturalness. Audio quality/latency tested separately.
"""

import asyncio
import time
import re
import json
import structlog
from typing import Optional
from dataclasses import dataclass, field, asdict

from config.settings import settings
from .providers.groq_llm import GroqLLMProvider
from .test_actors import ProspectScenario, PROSPECT_SCENARIOS, select_scenario
from .inbound_handler import (
    OUTBOUND_SYSTEM_PROMPT,
    OUTBOUND_PITCH_TEXT,
    INBOUND_SYSTEM_PROMPT,
    INBOUND_PITCH_TEXT,
    TRANSFER_AGENT_NAME,
)

logger = structlog.get_logger()

# ── Conversation end signals ────────────────────────────────────────────────

GOODBYE_PATTERNS = [
    "have a wonderful day", "have a great day", "have a good day",
    "take care", "no worries at all", "bye", "goodbye", "good bye",
    "gotta go", "i gotta go",
]

TRANSFER_TRIGGERS = [
    "licensed agent standing by", "transfer you now", "connecting you to",
    "get her on the line", "let me connect you",
]

HANGUP_SIGNALS = [
    "stop responding", "hung up", "[hangs up]", "[end call]",
]

MAX_TURNS = 16  # Safety limit


# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class SimTurn:
    """A single turn in the simulated conversation."""
    turn_number: int
    speaker: str       # "becky" or "prospect"
    text: str
    is_scripted: bool = False  # True for greeting/pitch (not LLM-generated)
    latency_ms: float = 0.0   # LLM generation time
    word_count: int = 0
    ends_with_question: bool = False

@dataclass
class SimResult:
    """Complete simulation result."""
    scenario: str
    scenario_name: str
    persona: str
    turns: list = field(default_factory=list)
    total_turns: int = 0
    becky_turns: int = 0
    prospect_turns: int = 0
    end_reason: str = ""        # "transfer", "goodbye", "hangup", "max_turns"
    duration_ms: float = 0.0

    # Quality metrics (computed after simulation)
    avg_becky_words: float = 0.0
    max_becky_words: int = 0
    questions_per_turn: float = 0.0
    step_reached: int = 0       # 1=interest, 2=urgency, 3=bank, 4=transfer
    repetition_score: float = 0.0  # 0=no repetition, 1=fully repeated
    ai_disclosure_present: bool = False
    banned_phrases_used: list = field(default_factory=list)
    agent_name_correct: bool = True

    # Conversation text
    full_transcript: str = ""

    # Grading
    grade: dict = field(default_factory=dict)


# ── Simulator ───────────────────────────────────────────────────────────────

async def run_text_simulation(
    scenario_name: str = None,
    direction: str = "outbound",
    max_turns: int = MAX_TURNS,
) -> SimResult:
    """
    Run a full text-level call simulation.

    Args:
        scenario_name: Force a scenario (e.g., "easy_close"). None = random.
        direction: "outbound" or "inbound"
        max_turns: Maximum conversation turns before stopping.

    Returns:
        SimResult with full transcript and quality metrics.
    """
    t0 = time.time()

    # Select scenario
    scenario_enum, scenario_config = select_scenario(force=scenario_name)

    # Set up system prompts
    if direction == "outbound":
        becky_system = OUTBOUND_SYSTEM_PROMPT
        pitch_text = OUTBOUND_PITCH_TEXT
        greeting = "Hi, can you hear me ok?"
    else:
        becky_system = INBOUND_SYSTEM_PROMPT
        pitch_text = INBOUND_PITCH_TEXT
        greeting = "Hi, thanks for calling back. Can you hear me okay?"

    prospect_system = scenario_config["system_prompt"]
    persona = scenario_config.get("persona", "Unknown")

    # Initialize LLM providers (use Groq for both)
    becky_llm = GroqLLMProvider(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
    )
    prospect_llm = GroqLLMProvider(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
    )

    result = SimResult(
        scenario=scenario_enum.value,
        scenario_name=scenario_config["name"],
        persona=persona,
    )

    # ── Phase 1: Greeting ──
    result.turns.append(SimTurn(
        turn_number=0,
        speaker="becky",
        text=greeting,
        is_scripted=True,
        word_count=len(greeting.split()),
    ))

    # ── Phase 2: Pitch (pre-baked) ──
    result.turns.append(SimTurn(
        turn_number=1,
        speaker="becky",
        text=pitch_text,
        is_scripted=True,
        word_count=len(pitch_text.split()),
    ))

    # AI disclosure check
    if "AI assistant" in pitch_text or "ai assistant" in pitch_text.lower():
        result.ai_disclosure_present = True

    # ── Build conversation history for both sides ──
    # Becky's history: She knows she already said the greeting + pitch
    becky_history = [
        {"role": "assistant", "content": greeting},
        {"role": "assistant", "content": pitch_text},
    ]

    # Prospect's history: They heard the greeting + pitch
    prospect_history = [
        {"role": "user", "content": f"{greeting} {pitch_text}"},
    ]

    # ── Phase 3: Live conversation ──
    turn_number = 2
    end_reason = "max_turns"

    while turn_number < max_turns + 2:
        # ── Prospect's turn ──
        prospect_text = await _generate_response(
            prospect_llm, prospect_system, prospect_history, max_tokens=30)

        if not prospect_text or prospect_text.strip() == "":
            end_reason = "prospect_silent"
            break

        prospect_turn = SimTurn(
            turn_number=turn_number,
            speaker="prospect",
            text=prospect_text,
            word_count=len(prospect_text.split()),
        )
        result.turns.append(prospect_turn)

        # Update histories
        becky_history.append({"role": "user", "content": prospect_text})
        prospect_history.append({"role": "assistant", "content": prospect_text})

        # Check if prospect hung up or said goodbye
        prospect_lower = prospect_text.lower()
        if any(h in prospect_lower for h in HANGUP_SIGNALS):
            end_reason = "prospect_hangup"
            break

        # If prospect is saying goodbye (after Becky already said goodbye), end the call
        if any(g in prospect_lower for g in GOODBYE_PATTERNS):
            # Check if Becky already said goodbye in the previous turn
            prev_becky = [t for t in result.turns if t.speaker == "becky"]
            if prev_becky:
                last_becky_lower = prev_becky[-1].text.lower()
                if any(g in last_becky_lower for g in GOODBYE_PATTERNS):
                    end_reason = "mutual_goodbye"
                    break

        turn_number += 1

        # ── Becky's turn ──
        t_llm = time.time()
        becky_text = await _generate_response(
            becky_llm, becky_system, becky_history, max_tokens=60)
        llm_ms = (time.time() - t_llm) * 1000

        if not becky_text or becky_text.strip() == "":
            end_reason = "becky_silent"
            break

        becky_turn = SimTurn(
            turn_number=turn_number,
            speaker="becky",
            text=becky_text,
            latency_ms=llm_ms,
            word_count=len(becky_text.split()),
            ends_with_question=_ends_with_question(becky_text),
        )
        result.turns.append(becky_turn)

        # Update histories
        becky_history.append({"role": "assistant", "content": becky_text})
        prospect_history.append({"role": "user", "content": becky_text})

        # Check end conditions
        becky_lower = becky_text.lower()

        if any(t in becky_lower for t in TRANSFER_TRIGGERS):
            end_reason = "transfer"
            break

        if any(g in becky_lower for g in GOODBYE_PATTERNS):
            end_reason = "goodbye"
            break

        turn_number += 1

    # ── Compute metrics ──
    result.total_turns = len(result.turns)
    result.end_reason = end_reason
    result.duration_ms = (time.time() - t0) * 1000

    becky_turns = [t for t in result.turns if t.speaker == "becky" and not t.is_scripted]
    prospect_turns = [t for t in result.turns if t.speaker == "prospect"]

    result.becky_turns = len(becky_turns) + 2  # +2 for greeting+pitch
    result.prospect_turns = len(prospect_turns)

    if becky_turns:
        result.avg_becky_words = sum(t.word_count for t in becky_turns) / len(becky_turns)
        result.max_becky_words = max(t.word_count for t in becky_turns)
        result.questions_per_turn = sum(1 for t in becky_turns if t.ends_with_question) / len(becky_turns)

    # Step progression analysis
    result.step_reached = _analyze_step_progression(result.turns)

    # Repetition analysis
    result.repetition_score = _analyze_repetition(becky_turns)

    # Banned phrases check
    all_becky_text = " ".join(t.text.lower() for t in result.turns if t.speaker == "becky")
    banned = ["guarantee", "promise you", "absolutely sure", "100% coverage"]
    result.banned_phrases_used = [b for b in banned if b in all_becky_text]

    # Agent name check
    if "transfer" in end_reason:
        if TRANSFER_AGENT_NAME.lower() not in all_becky_text:
            result.agent_name_correct = False

    # Full transcript
    result.full_transcript = _format_transcript(result.turns)

    # Grade the conversation
    result.grade = grade_text_simulation(result)

    logger.info("text_simulation_complete",
        scenario=scenario_enum.value,
        turns=result.total_turns,
        end_reason=end_reason,
        step_reached=result.step_reached,
        grade=result.grade.get("overall_score", 0),
        duration_ms=round(result.duration_ms))

    return result


async def _generate_response(
    llm: GroqLLMProvider,
    system_prompt: str,
    history: list,
    max_tokens: int = 60,
) -> str:
    """Generate a single LLM response."""
    response_text = ""
    try:
        async for chunk in llm.generate_stream(
            messages=history,
            system_prompt=system_prompt,
            temperature=0.7,
            max_tokens=max_tokens,
        ):
            text = chunk.get("text", "")
            if text:
                response_text += text
            if chunk.get("is_complete"):
                break
    except Exception as e:
        logger.error("text_sim_llm_error", error=str(e))
        return ""

    # Clean up: remove asterisks, brackets, stage directions
    response_text = re.sub(r'\*[^*]+\*', '', response_text).strip()
    response_text = re.sub(r'\[[^\]]+\]', '', response_text).strip()

    return response_text


def _ends_with_question(text: str) -> bool:
    """Check if text ends with a question."""
    text = text.strip()
    if text.endswith("?"):
        return True
    # Check last sentence
    sentences = re.split(r'[.!?]', text)
    last = sentences[-1].strip() if sentences else ""
    question_starters = ["do you", "would you", "want me", "how", "what", "does that", "sound good", "right"]
    return any(q in last.lower() for q in question_starters)


def _analyze_step_progression(turns: list) -> int:
    """Analyze which qualification step Becky reached."""
    all_becky = " ".join(t.text.lower() for t in turns if t.speaker == "becky")

    step = 0

    # Step 1: Confirm interest (greeting + pitch counts)
    if "ring a bell" in all_becky or "go over it" in all_becky or "follow up" in all_becky:
        step = 1

    # Step 2: Urgency pitch
    urgency_markers = ["expires tomorrow", "runs out", "funeral costs", "nine thousand",
                       "preferred offer", "set aside", "never claimed", "burial", "cremation"]
    if any(m in all_becky for m in urgency_markers):
        step = max(step, 2)

    # Step 3: Bank account
    bank_markers = ["checking or savings", "bank account", "checking account",
                    "savings account", "biggest discounts"]
    if any(m in all_becky for m in bank_markers):
        step = max(step, 3)

    # Step 4: Transfer
    if any(t in all_becky for t in TRANSFER_TRIGGERS):
        step = max(step, 4)

    return step


def _analyze_repetition(becky_turns: list) -> float:
    """Measure repetition across Becky's responses (0=none, 1=fully repeated)."""
    if len(becky_turns) < 2:
        return 0.0

    total_overlap = 0.0
    comparisons = 0

    for i in range(1, len(becky_turns)):
        words_i = set(becky_turns[i].text.lower().split())
        for j in range(i):
            words_j = set(becky_turns[j].text.lower().split())
            if words_i and words_j:
                overlap = len(words_i & words_j) / max(len(words_i), 1)
                total_overlap += overlap
                comparisons += 1

    return total_overlap / max(comparisons, 1)


def _format_transcript(turns: list) -> str:
    """Format turns into a readable transcript."""
    lines = []
    for t in turns:
        label = "BECKY" if t.speaker == "becky" else "PROSPECT"
        prefix = "[scripted] " if t.is_scripted else ""
        lines.append(f"[Turn {t.turn_number}] {label}: {prefix}{t.text}")
    return "\n".join(lines)


# ── Text-Level Grading ──────────────────────────────────────────────────────

def grade_text_simulation(result: SimResult) -> dict:
    """
    Grade a text simulation on conversation quality.

    Categories (100 points total):
    1. Script Adherence (25 pts) — step progression, required elements
    2. Conversation Flow (25 pts) — brevity, questions, naturalness
    3. Sales Effectiveness (25 pts) — objection handling, outcome
    4. Compliance & Safety (25 pts) — AI disclosure, banned phrases, agent name
    """
    scores = {}
    findings = []
    improvements = []

    # ── 1. Script Adherence (25 pts) ──
    script_score = 25.0

    # Step progression
    if result.step_reached == 4:
        findings.append("PERFECT: Reached transfer (all 4 steps completed)")
    elif result.step_reached == 3:
        script_score -= 3
        findings.append(f"Good: Reached bank account question (step 3/4)")
    elif result.step_reached == 2:
        script_score -= 8
        findings.append(f"Partial: Only reached urgency pitch (step 2/4)")
        improvements.append("Ensure progression to bank account question before transfer")
    elif result.step_reached == 1:
        script_score -= 15
        findings.append(f"Weak: Only confirmed interest (step 1/4)")
        improvements.append("Must deliver urgency pitch and ask about bank account")
    else:
        script_score -= 20
        findings.append("FAIL: Never even confirmed interest")
        improvements.append("Becky must follow the 4-step qualification flow")

    # For graceful exits, step regression is expected — restore points
    exit_scenarios = ("not_interested", "wrong_number", "negative_hostile", "silence", "voicemail")
    if result.scenario in exit_scenarios and result.end_reason in ("goodbye", "prospect_hangup", "prospect_silent", "becky_silent"):
        script_score = min(script_score + 15, 25)  # Bonus back for appropriate exit
        findings.append("Appropriate graceful exit for this scenario")

    # Transfer without bank account = critical failure
    if result.end_reason == "transfer" and result.step_reached < 3:
        script_score -= 10
        findings.append("CRITICAL: Transferred without asking bank account question")
        improvements.append("MUST ask about checking/savings before transfer")

    scores["script_adherence"] = max(0, script_score)

    # ── 2. Conversation Flow (25 pts) ──
    flow_score = 25.0

    # Brevity
    if result.avg_becky_words > 0:
        if result.avg_becky_words <= 20:
            findings.append(f"Excellent brevity: {result.avg_becky_words:.0f} avg words/response")
        elif result.avg_becky_words <= 30:
            flow_score -= 3
            findings.append(f"Good brevity: {result.avg_becky_words:.0f} avg words")
        elif result.avg_becky_words <= 40:
            flow_score -= 8
            findings.append(f"Too wordy: {result.avg_becky_words:.0f} avg words")
            improvements.append("Reduce response length to under 25 words avg")
        else:
            flow_score -= 15
            findings.append(f"WAY too wordy: {result.avg_becky_words:.0f} avg words")
            improvements.append("CRITICAL: Responses must be under 35 words")

    # Max response length
    if result.max_becky_words > 50:
        flow_score -= 5
        findings.append(f"Longest response: {result.max_becky_words} words (too long)")
        improvements.append(f"Cap individual responses at 40 words max")

    # Questions per turn
    if result.questions_per_turn >= 0.9:
        findings.append(f"Great question rate: {result.questions_per_turn:.0%}")
    elif result.questions_per_turn >= 0.7:
        flow_score -= 3
        findings.append(f"OK question rate: {result.questions_per_turn:.0%}")
    else:
        flow_score -= 8
        findings.append(f"Low question rate: {result.questions_per_turn:.0%}")
        improvements.append("Every Becky response should end with a question")

    # Repetition
    if result.repetition_score < 0.20:
        findings.append(f"Low repetition: {result.repetition_score:.0%}")
    elif result.repetition_score < 0.35:
        flow_score -= 5
        findings.append(f"Some repetition: {result.repetition_score:.0%}")
        improvements.append("Vary language more — too much word overlap between responses")
    else:
        flow_score -= 12
        findings.append(f"HIGH repetition: {result.repetition_score:.0%}")
        improvements.append("CRITICAL: Becky is repeating herself — use completely different words")

    scores["conversation_flow"] = max(0, flow_score)

    # ── 3. Sales Effectiveness (25 pts) ──
    sales_score = 25.0

    # Outcome scoring by scenario
    if result.scenario == "easy_close":
        if result.end_reason == "transfer" and result.step_reached == 4:
            findings.append("PERFECT: Easy close → successful transfer")
        elif result.end_reason == "transfer":
            sales_score -= 5
            findings.append("Transfer initiated but steps incomplete")
        else:
            sales_score -= 15
            findings.append("FAIL: Easy close should always reach transfer")
            improvements.append("Easy close prospect is receptive — push to transfer")

    elif result.scenario == "confused_open":
        if result.end_reason == "transfer":
            findings.append("GREAT: Warmed up confused prospect to transfer")
        elif result.step_reached >= 2:
            sales_score -= 5
            findings.append("Good: Got confused prospect interested")
        else:
            sales_score -= 10
            findings.append("Didn't warm up the confused prospect enough")
            improvements.append("Be patient with confused prospects — explain clearly")

    elif result.scenario == "price_objection":
        if result.end_reason == "transfer":
            findings.append("EXCELLENT: Overcame price objection → transfer")
        elif result.end_reason == "goodbye" and result.step_reached >= 2:
            findings.append("GOOD: Handled price objection well, graceful exit after repeated objections")
        elif result.step_reached >= 2:
            sales_score -= 3
            findings.append("Good: Handled price objection, kept them engaged")
        else:
            sales_score -= 8
            improvements.append("Use 'dollar or two a day' affordability framing")

    elif result.scenario == "has_insurance":
        if result.end_reason == "transfer":
            findings.append("EXCELLENT: Differentiated final expense from life insurance → transfer")
        elif result.step_reached >= 2:
            sales_score -= 3
            findings.append("Good: Explained difference between coverage types")
        else:
            sales_score -= 8
            improvements.append("Explain that final expense is separate from employer life insurance")

    elif result.scenario == "not_interested":
        if result.end_reason == "goodbye":
            findings.append("GOOD: Graceful exit for not-interested prospect")
        elif result.end_reason == "transfer":
            findings.append("GREAT: Won over reluctant prospect")
            sales_score += 5  # Bonus
        else:
            sales_score -= 5
            findings.append("Should exit gracefully when prospect is clearly not interested")

    elif result.scenario == "wrong_number":
        if result.end_reason in ("goodbye", "prospect_hangup"):
            findings.append("CORRECT: Quick exit for wrong number")
        else:
            sales_score -= 10
            findings.append("FAIL: Should immediately exit for wrong number")
            improvements.append("Wrong number = immediate graceful exit")

    elif result.scenario == "silence":
        if result.end_reason == "goodbye":
            findings.append("PERFECT: Handled silence gracefully, checked in and exited")
        elif result.end_reason == "transfer":
            # Prospect eventually responded and Becky got the transfer — that's great
            findings.append("GREAT: Prospect responded after silence, Becky recovered → transfer")
        elif result.step_reached >= 2:
            findings.append("Good: Handled silence, continued conversation")
        else:
            sales_score -= 10
            findings.append("FAIL: Should check in with 'Are you still there?' after silence")
            improvements.append("For silence: wait, check in, then exit gracefully")

    elif result.scenario == "voicemail":
        # Voicemail: Becky should NOT leave a message — silent hangup is correct
        non_scripted_becky = [t for t in result.turns if t.speaker == "becky" and not t.is_scripted]
        all_becky_text = " ".join(t.text.lower() for t in non_scripted_becky)
        if "leave a message" in all_becky_text or len(non_scripted_becky) > 0:
            # Becky said something after detecting voicemail — bad
            if any(w in all_becky_text for w in ["voicemail", "leave a message", "message after"]):
                sales_score -= 15
                findings.append("CRITICAL COMPLIANCE FAIL: Left a voicemail (regulatory violation)")
                improvements.append("NEVER leave voicemail messages — hang up silently")
            elif len(non_scripted_becky) > 0:
                sales_score -= 5
                findings.append("Said something after voicemail detected — should be silent")
        else:
            # No non-scripted Becky turns after prospect = perfect silent hangup
            findings.append("PERFECT: Detected voicemail, hung up silently (compliance correct)")

    elif result.scenario == "skeptical_questions":
        if result.end_reason == "transfer":
            findings.append("EXCELLENT: Answered skeptical questions honestly, won trust → transfer")
        elif result.step_reached >= 2:
            findings.append("GOOD: Answered questions, moved toward transfer")
        elif result.step_reached >= 1:
            sales_score -= 3
            findings.append("OK: Answered questions but didn't advance far")
        else:
            sales_score -= 8
            findings.append("Should have answered skeptical questions honestly")
            improvements.append("Answer 'How did you get my number?', 'Are you real?' directly and briefly")

    elif result.scenario == "negative_hostile":
        # Check if Becky apologized and exited (any end reason is fine if she apologized fast)
        all_becky_text = " ".join(t.text.lower() for t in result.turns if t.speaker == "becky" and not t.is_scripted)
        apologized = any(w in all_becky_text for w in ["sorry", "apologize", "removed"])
        non_scripted_becky = [t for t in result.turns if t.speaker == "becky" and not t.is_scripted]

        if apologized and len(non_scripted_becky) <= 2:
            findings.append("PERFECT: Recognized hostility, apologized, and exited quickly")
        elif apologized:
            sales_score -= 3
            findings.append("Good: Apologized but took too many turns to exit")
            improvements.append("Exit faster after hostile response — one apology, done")
        elif result.end_reason in ("goodbye", "prospect_hangup", "prospect_silent"):
            sales_score -= 5
            findings.append("Exited but should have explicitly apologized")
        else:
            sales_score -= 15
            findings.append("CRITICAL FAIL: Didn't recognize hostile/DNC signal")
            improvements.append("If prospect says 'harassment' or 'take me off' → apologize and END immediately")

    elif result.scenario == "off_bounds":
        if result.end_reason == "transfer":
            findings.append("GREAT: Gently redirected off-topic, won them over → transfer")
        elif result.step_reached >= 2:
            findings.append("GOOD: Acknowledged off-topic, redirected gracefully")
        elif result.step_reached >= 1:
            sales_score -= 3
            findings.append("OK: Acknowledged but should have redirected better")
        else:
            sales_score -= 8
            improvements.append("For off-topic: acknowledge briefly ('Ha! Good question'), then redirect")

    # Objection handling quality (check if Becky acknowledged before redirecting)
    prospect_objections = [t for t in result.turns if t.speaker == "prospect"
                          and any(w in t.text.lower() for w in
                                  ["can't afford", "not interested", "already have", "how much", "busy"])]
    if prospect_objections:
        becky_after_objection = []
        for obj_turn in prospect_objections:
            next_becky = [t for t in result.turns
                         if t.speaker == "becky" and t.turn_number > obj_turn.turn_number]
            if next_becky:
                becky_after_objection.append(next_becky[0])

        acknowledge_words = ["hear you", "understand", "get it", "that's fair", "makes sense",
                           "no worries", "totally get", "i hear", "i get"]
        acknowledged = sum(1 for t in becky_after_objection
                         if any(a in t.text.lower() for a in acknowledge_words))
        if becky_after_objection:
            ack_rate = acknowledged / len(becky_after_objection)
            if ack_rate >= 0.5:
                findings.append(f"Good objection acknowledgment ({ack_rate:.0%})")
            else:
                sales_score -= 5
                improvements.append("Acknowledge objections before redirecting (LAER method)")

    scores["sales_effectiveness"] = min(25, max(0, sales_score))  # Cap at 25

    # ── 4. Compliance & Safety (25 pts) ──
    compliance_score = 25.0

    if result.ai_disclosure_present:
        findings.append("AI disclosure present in pitch")
    else:
        compliance_score -= 15
        findings.append("CRITICAL: Missing AI disclosure (FCC requirement)")
        improvements.append("MUST include AI disclosure per FCC rules")

    if result.banned_phrases_used:
        compliance_score -= 10
        findings.append(f"Banned phrases used: {result.banned_phrases_used}")
        improvements.append(f"Remove banned phrases: {result.banned_phrases_used}")
    else:
        findings.append("No banned phrases detected")

    if result.end_reason == "transfer" and not result.agent_name_correct:
        compliance_score -= 5
        findings.append(f"Agent name incorrect — should be '{TRANSFER_AGENT_NAME}'")
        improvements.append(f"Always use '{TRANSFER_AGENT_NAME}' when transferring")

    scores["compliance"] = max(0, compliance_score)

    # ── Overall ──
    overall = min(100, sum(scores.values()))

    # Letter grade
    if overall >= 95: letter = "A+"
    elif overall >= 90: letter = "A"
    elif overall >= 85: letter = "A-"
    elif overall >= 80: letter = "B+"
    elif overall >= 75: letter = "B"
    elif overall >= 70: letter = "B-"
    elif overall >= 65: letter = "C+"
    elif overall >= 60: letter = "C"
    elif overall >= 50: letter = "D"
    else: letter = "F"

    return {
        "overall_score": round(overall, 1),
        "overall_grade": letter,
        "categories": scores,
        "findings": findings,
        "improvements": improvements,
        "scenario": result.scenario,
        "scenario_name": result.scenario_name,
        "end_reason": result.end_reason,
        "step_reached": result.step_reached,
        "total_turns": result.total_turns,
        "avg_becky_words": round(result.avg_becky_words, 1),
        "repetition_score": round(result.repetition_score, 2),
    }


async def run_all_scenarios(direction: str = "outbound") -> dict:
    """
    Run all 6 scenarios and return aggregated results.

    Returns dict with per-scenario results and overall summary.
    """
    scenarios = [s.value for s in ProspectScenario]
    results = {}

    for scenario in scenarios:
        logger.info("text_sim_starting", scenario=scenario)
        result = await run_text_simulation(
            scenario_name=scenario,
            direction=direction,
        )
        results[scenario] = {
            "grade": result.grade,
            "transcript": result.full_transcript,
            "turns": result.total_turns,
            "end_reason": result.end_reason,
            "step_reached": result.step_reached,
        }

    # Aggregate
    scores = [r["grade"]["overall_score"] for r in results.values()]
    avg_score = sum(scores) / len(scores) if scores else 0

    failing = [name for name, r in results.items() if r["grade"]["overall_score"] < 70]
    all_improvements = []
    for r in results.values():
        all_improvements.extend(r["grade"].get("improvements", []))

    # Deduplicate improvements
    unique_improvements = list(dict.fromkeys(all_improvements))

    return {
        "summary": {
            "avg_score": round(avg_score, 1),
            "min_score": round(min(scores), 1) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
            "failing_scenarios": failing,
            "scenarios_run": len(results),
            "all_improvements": unique_improvements[:10],  # Top 10
        },
        "scenarios": results,
    }

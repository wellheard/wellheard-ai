# WellHeard AI — Transfer Qualification Gate

## Overview

The **Transfer Qualification Gate** is a production-ready multi-check system that validates call quality BEFORE transferring to a licensed agent. It prevents wasting agent time on:

- **Silent/dead-air calls** — No actual speech
- **Background noise only** — Rumbling, breathing, TV/radio
- **Unqualified prospects** — Didn't confirm interest or account
- **Non-human systems** — IVR, voicemail that slipped through early filters
- **Automated/robotic responses** — Suspiciously uniform patterns

## Architecture

The gate runs **8 independent checks** on call transcript and audio metrics.

### Decision Logic

```
if checks_passed >= 6 AND overall_score >= 70:
    → TRANSFER to agent ✓
elif checks_passed >= 5:
    → RE_QUALIFY (ask one more confirm question)
elif checks_passed >= 4:
    → END_CALL (hang up gracefully)
```

## The 8 Checks

### Check 1: Minimum Conversation Depth
**What:** Prospect must have participated meaningfully.
- Minimum 4 prospect turns (at least 4 "exchanges")
- Minimum 15 words total (rules out "yeah, okay, yeah, okay")

**Why:** Real qualified people say more than 2-3 word responses. Bots and voicemail don't.

**Thresholds:** Configurable via `gate_min_prospect_turns` and `gate_min_prospect_words`

---

### Check 2: Phase Completion Verification
**What:** All required phases completed + positive signals.
- Required phases: IDENTIFY, URGENCY_PITCH, QUALIFY_ACCOUNT
- Each phase must have ≥ 1 prospect response with positive signal
  - IDENTIFY: "Yes", "That's me", "Correct"
  - URGENCY_PITCH: "Yes", "Interested", "Go ahead"
  - QUALIFY_ACCOUNT: "I do", "I have", "Checking/Savings"

**Why:** If prospect didn't confirm identify/urgency/qualify, they're not ready.

**Thresholds:** Hard-coded required phases (customize in config if needed)

---

### Check 3: Speech Activity Verification
**What:** Prospect must be actually speaking, not just air.
- Prospect speech time / total prospect time ≥ 30%

**Why:** If prospect is 70% silent, they're not engaged or can't hear.

**Thresholds:** `gate_min_speech_ratio = 0.30` (30%)

---

### Check 4: Response Relevance Scoring
**What:** Each response must be relevant to the question asked.
- Each response scored 0-1:
  - 1.0 = Perfect answer with positive signal
  - 0.75 = Relevant, shows engagement
  - 0.5 = Vague but not negative
  - 0.25 = Off-topic but not negative
  - 0.0 = Non-response or silence
- Average across all responses must be ≥ 0.50

**Why:** Random words or off-topic responses = low intent or comprehension issue.

**Thresholds:** `gate_min_relevance_score = 0.50`

---

### Check 5: Audio Quality Check
**What:** Audio must be clean speech, not noise floor or constant tone.
- RMS energy ≥ -40 dBFS (above noise floor)
- Variance in RMS ≥ 5% of mean (not suspiciously constant)

**Why:** 
- Too quiet (-50 dBFS) = background noise, not speech
- Zero variance = TV/radio playing, not human speaking

**Thresholds:**
- `gate_min_audio_rms_dbfs = -40.0`
- `gate_max_audio_rms_variance_ratio = 0.5`

---

### Check 6: Human Speech Pattern Detection
**What:** Speech must have natural variation, not robotic timing.
- Coefficient of Variation in turn word counts ≥ 0.2
- CV = std_dev / mean_words_per_turn
- Low CV (< 0.2) = suspiciously uniform (IVR/voicemail)
- High CV = natural human variation

**Why:** Automated systems are unnaturally consistent. Humans vary: 1 word, 5 words, 2 words, 8 words...

**Thresholds:** `gate_max_turn_length_cv = 0.20`

---

### Check 7: Prospect Engagement Score
**What:** Combined engagement metric.
- Response latency 300-1500ms (human sweet spot, not <100ms or >5s)
- Average words per turn 2-10 (reasonable engagement)
- Combines latency score + word count score

**Why:**
- <100ms = likely automated response (too fast)
- >5s = prospect not paying attention or can't hear
- <2 words avg = disengaged
- >20 words avg = rambling (possible objection handling needed)

**Thresholds:**
- `gate_min_avg_response_latency_ms = 100.0`
- `gate_max_avg_response_latency_ms = 5000.0`
- `gate_min_engagement_score = 0.50`

---

### Check 8: Agent Feedback Loop (Self-Tuning)
**What:** Auto-adjusts thresholds based on real agent rejection rates.
- Tracks: total transfers, qualified transfers (agent stays 30s+), rejected transfers
- If rejection rate > 40%: TIGHTEN thresholds (be more selective)
- If rejection rate < 10%: LOOSEN thresholds (accept more)

**Why:** Data-driven self-improvement. If agents keep hanging up after 10s, our gate is letting too many bad calls through.

**Thresholds:**
- `gate_agent_rejection_tighten = 0.40`
- `gate_agent_rejection_loosen = 0.10`

---

## Usage

### Basic Evaluation

```python
from src.transfer_gate import TransferQualificationGate, CallContext

gate = TransferQualificationGate()

# Populate context from call data
context = CallContext(
    call_id="call_123",
    transcript_turns=[...],
    completed_phases=["identify", "urgency_pitch", "qualify_account"],
    phase_positive_signals={"identify": True, "urgency": True, "qualify": True},
    prospect_speech_seconds=45.0,
    prospect_total_seconds=60.0,
    avg_audio_rms=-20.0,
    audio_rms_variance=5.0,
    response_latencies_ms=[400, 350, 450, 380],
    turn_word_counts=[3, 5, 4, 6],
)

# Evaluate
result = gate.evaluate(context)

if result.approved:
    # Transfer to agent
    transfer_to_agent(context.call_id)
elif result.recommendation == "re_qualify":
    # Ask one more confirm
    say(RE_QUALIFY_PHRASES[0])
else:
    # End call gracefully
    say(END_CALL_PHRASES[0])
```

### Recording Agent Feedback

```python
# After transfer, monitor agent acceptance
gate.record_agent_feedback(
    call_id="call_123",
    qualified=True,  # Agent stayed > 30s
    agent_talk_seconds=45.0
)

# Gate auto-tunes if rejection rate > 40% or < 10%
```

### Manual Threshold Adjustment

```python
# Check current thresholds
thresholds = gate.get_current_thresholds()

# Manually adjust based on observed rejection rate
gate.adjust_thresholds(agent_rejection_rate=0.35)
```

---

## Configuration

All thresholds are configurable in `config/settings.py`:

```python
# Check 1: Conversation Depth
gate_min_prospect_turns: int = 4
gate_min_prospect_words: int = 15

# Check 3: Speech Activity
gate_min_speech_ratio: float = 0.30

# Check 4: Response Relevance
gate_min_relevance_score: float = 0.50

# Check 5: Audio Quality
gate_min_audio_rms_dbfs: float = -40.0

# Check 6: Human Speech Pattern
gate_max_turn_length_cv: float = 0.20

# Check 7: Prospect Engagement
gate_min_engagement_score: float = 0.50

# Overall Gates
gate_min_checks_passed: int = 6
gate_min_overall_score: int = 70

# Self-tuning
gate_agent_rejection_tighten: float = 0.40
gate_agent_rejection_loosen: float = 0.10
```

---

## Re-Qualification Phrases

Used when gate says `recommendation == "re_qualify"`:

1. "Just to make sure I have everything right — you said you're interested in getting that quote, correct?"
2. "And just to confirm, you do have a checking or savings account for the discounts, right?"
3. "So you're saying you'd like to move forward with the quote before that offer expires tomorrow?"

---

## End-Call Phrases

Used when gate says `recommendation == "end_call"`:

1. "I appreciate your time today. If you're ever interested in learning more, don't hesitate to give us a call back. Have a great day."
2. "Thanks for listening. Best of luck, and feel free to reach out anytime. Take care."
3. "I understand. Thanks for your time. Have a wonderful day."

---

## Logging

All gate evaluations are logged via `structlog`:

```
transfer_gate_evaluated call_id=abc123 approved=True checks_passed=7 
                        overall_score=82.5 recommendation=transfer
```

Post-transfer feedback:

```
agent_accepted_transfer call_id=abc123 agent_talk_seconds=45.2
agent_rejected_transfer call_id=def456 agent_talk_seconds=8.3
transfer_gate_auto_tighten rejection_rate=0.45 applied_tuning=0.80
```

---

## Data Structures

### CallContext
Contains all call data needed for evaluation:
- Transcript turns with speaker, text, timestamp
- Completed phases + positive signals per phase
- Audio metrics: speech time, RMS energy, variance
- Timing: response latencies, turn word counts
- Metadata: call duration, voicemail/silence flags

### TransferGateResult
Output of evaluation:
- `approved` (bool): Pass/fail
- `overall_score` (0-100): Weighted average of 8 check scores
- `checks_passed` / `checks_total`: 6/8, 7/8, etc.
- `failed_checks`: List of check names that failed
- `check_details`: Per-check breakdown with scores and details
- `recommendation`: TRANSFER, RE_QUALIFY, END_CALL, or FLAG_FOR_REVIEW
- `reason`: Human-readable explanation

### AgentFeedbackTracker
Tracks transfer outcomes for self-tuning:
- `total_transfers`: Cumulative count
- `qualified_transfers`: Agent stayed ≥30s
- `rejected_transfers`: Agent hung up <30s
- `rejection_rate()`: Current rejection rate (0-1)
- `should_tighten()`: Rejection rate > 40%?
- `should_loosen()`: Rejection rate < 10%?

---

## Production Considerations

### Latency
- Gate evaluation is fast: <5ms per call
- Runs synchronously before transfer initiation
- No external API calls

### Reliability
- Handles missing data gracefully (empty turns, zero audio, etc.)
- All checks have sensible defaults if data is incomplete
- Logging includes full details for debugging

### Tuning
- Start with default thresholds (based on aged-lead call data)
- Monitor agent rejection rate over first 100+ transfers
- Let self-tuning adjust automatically, OR manually adjust if needed
- Log all threshold changes for audit trail

### Testing
- Use provided test context to validate gate behavior
- Test edge cases: silent calls, short calls, low-quality audio
- Compare gate decisions to actual agent acceptance for calibration

---

## Example: From Call to Transfer Decision

```
Call starts
├─ GREETING: Prospect responds "Hello?"
├─ IDENTIFY: Prospect says "Yeah, that's me"
├─ URGENCY_PITCH: Prospect says "I'm interested"
├─ QUALIFY_ACCOUNT: Prospect says "I have checking"
│
├─ Gate collects: 4 turns, 11 words, 45s speech / 60s total, -25 dBFS RMS, 
│                 400ms avg latency, CV=0.25, all phases complete
│
├─ Gate evaluates:
│   ✓ Conversation Depth (4 turns, 11 words)
│   ✓ Phase Completion (3/3 phases + signals)
│   ✓ Speech Activity (75% speaking)
│   ✓ Response Relevance (0.85 avg)
│   ✓ Audio Quality (-25 dBFS, 8% variance)
│   ✓ Human Pattern (CV=0.25)
│   ✓ Engagement (400ms latency, 2.75 words/turn)
│   ✓ Feedback Loop (well-tuned)
│
├─ Result: 7/8 checks passed, score 92/100
├─ Recommendation: TRANSFER ✓
│
└─ Transfer to licensed agent → conversation records outcome
```

---

## Future Enhancements

- **Intent scoring**: NLP-based analysis of prospect intent beyond keyword matching
- **Sentiment analysis**: Detect frustration, hesitation, uncertainty
- **Caller history**: Cross-reference with past calls for pattern matching
- **A/B testing**: Compare gate performance across different threshold sets
- **ML-based tuning**: Use historical data to optimize thresholds automatically


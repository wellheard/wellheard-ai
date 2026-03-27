# WellHeard AI — Test Scenarios Reference

Last updated: 2026-03-27
Current avg score: 95.5/100 across original 6 scenarios (5 new scenarios added)

## Core Scenarios (Final Expense Aged Leads) — 6 scenarios, 60% combined weight

### 1. EASY_CLOSE (30% weight)
- **Persona:** Robert, 62M — friendly, remembers the form, has checking account
- **Expected flow:** Greeting → Pitch → "Oh yeah, I remember" → Urgency → Bank account → Transfer to Sarah
- **Target outcome:** Transfer (step 4/4)
- **Current score:** 97-100 (A+)
- **Key test:** Becky should complete all 4 steps quickly. Avg ~14 words/response.

### 2. CONFUSED_OPEN (20% weight)
- **Persona:** Dorothy, 70F — polite, doesn't remember form, gradually warms up
- **Expected flow:** Greeting → Pitch → "Who is this?" → Explain → "I don't remember" → Patient explanation → Warms up → Bank → Transfer
- **Target outcome:** Transfer (step 4/4)
- **Current score:** 95-100 (A+)
- **Key test:** Becky must be patient, not pushy. Should take 6-8 Becky turns to warm up Dorothy.

### 3. PRICE_OBJECTION (15% weight)
- **Persona:** Mike, 55M — remembers form, worried about cost, fixed income
- **Expected flow:** Greeting → Pitch → "How much?" → Handle cost → "Fixed income" → "Dollar or two a day" → Bank → Transfer (or graceful exit after 3rd cost objection)
- **Target outcome:** Transfer OR graceful exit
- **Current score:** 89-94 (A)
- **Key test:** Must use "dollar or two a day" framing. If same objection 3x, exit gracefully. Never push past their comfort.

### 4. HAS_INSURANCE (15% weight)
- **Persona:** Linda, 65F — already has employer life insurance, skeptical but polite
- **Expected flow:** Greeting → Pitch → "I already have insurance" → Differentiate final expense vs life → "Hmm, I didn't know that" → Bank → Transfer
- **Target outcome:** Transfer (step 4/4)
- **Current score:** 90-95 (A+)
- **Key test:** Must explain that final expense is separate from employer life insurance. Not combative.

### 5. NOT_INTERESTED (10% weight)
- **Persona:** Dave, 58M — busy, annoyed, short responses, 50% interrupt probability
- **Expected flow:** Greeting → Pitch → "Not interested" → Brief attempt → Either thaw or graceful exit
- **Target outcome:** Graceful exit (goodbye) or rare transfer if they thaw
- **Current score:** 92-100 (A/A+)
- **Key test:** Must not push past 2 rejections. Graceful exit. Short responses only.

### 6. WRONG_NUMBER (10% weight)
- **Persona:** Unknown — didn't fill out form, one response then stops
- **Expected flow:** Greeting → Pitch → "Wrong number" → Immediate exit: "I'm sorry about that! Have a great day."
- **Target outcome:** Immediate goodbye
- **Current score:** 92 (A)
- **Key test:** MUST exit immediately. Never pitch someone who says wrong number.

### 7. SILENCE (5% weight)
- **Persona:** Unknown — answers but unsure, silent
- **Expected flow:** Greeting → Pitch → "..." (silence) → Becky checks in: "Hello? Can you hear me okay?" → if still silent: "Looks like we got disconnected — have a great day." → Exit
- **Target outcome:** Graceful exit after 2-second silence detection + check-in
- **Current score:** N/A (new)
- **Key test:** Becky should handle silence gracefully without being pushy. Must check in once, then exit if no response.

### 8. VOICEMAIL (5% weight)
- **Persona:** Voicemail machine
- **Expected flow:** Greeting → Pitch → "[Voicemail beep] Please leave a message after the tone." → Becky hangs up (NO message left)
- **Target outcome:** Detect voicemail, hang up silently (compliance)
- **Current score:** N/A (new)
- **Key test:** CRITICAL compliance test. Becky MUST NOT leave voicemail messages (regulatory violation). Should end silently.

### 9. SKEPTICAL_QUESTIONS (10% weight)
- **Persona:** James, 50M — naturally skeptical, asks probing questions
- **Expected flow:** Greeting → Pitch → "How did you get my number?" → Honest answer → "Are you a real person?" → "I'm an AI" → "Is this a scam?" → "No, legitimate benefits quote" → Gradually warms up → Bank → Transfer
- **Target outcome:** Answer questions honestly, move to transfer (step 4/4)
- **Current score:** N/A (new)
- **Key test:** Skeptical prospects respond to HONESTY and direct answers. Must answer briefly (3 sentences max) and genuinely.

### 10. NEGATIVE_HOSTILE (5% weight)
- **Persona:** Angry prospect — demands removal from call list
- **Expected flow:** Greeting → Pitch → "Take me off your list!" → Becky: "I'm so sorry! I'll remove you right away. Have a good day." → Exit immediately
- **Target outcome:** Immediate graceful exit (DNC compliance)
- **Current score:** N/A (new)
- **Key test:** CRITICAL compliance. When prospect says "harassment" or "take me off" — apologize and END immediately. NEVER push back.

### 11. OFF_BOUNDS (5% weight)
- **Persona:** Patricia, 68F — chatty, curious, goes off-topic
- **Expected flow:** Greeting → Pitch → "What's the weather?" → Becky redirects: "Ha! Good question. So about that coverage..." → "What's your name?" → Brief answer + redirect → Eventually warms up → Bank → Transfer
- **Target outcome:** Acknowledge off-topic, redirect 1-2 times, move to transfer
- **Current score:** N/A (new)
- **Key test:** Acknowledge briefly without being rude, redirect to the purpose. Second redirect should warm them up.

---

## Quality Benchmarks

| Metric | Target | Current |
|--------|--------|---------|
| Avg words per Becky response | 15-25 | 14-21 |
| Max words per response | <30 | <25 |
| Questions per turn | >90% | 95%+ |
| Repetition score | <0.20 | 0.10-0.18 |
| Step progression (easy_close) | 4/4 | 4/4 |
| AI disclosure in pitch | Required | Present |
| Banned phrases | None | None |
| Agent name (Sarah) | Correct | Correct |

## Competitor Benchmarks

| Competitor | Response Latency | Quality Score |
|-----------|-----------------|---------------|
| **WellHeard** | **8ms cached / 450ms live** | **95** |
| Vapi | 508ms | 76 |
| Retell | 600ms | 78 |
| Synthflow | 900ms | 72 |
| Bland | 800ms | 68 |
| Air.ai | 1100ms | 65 |

---

## Grading Categories (100 pts total)

1. **Script Adherence (25 pts)** — Step progression, required elements, appropriate exits
2. **Conversation Flow (25 pts)** — Brevity, question rate, repetition avoidance, naturalness
3. **Sales Effectiveness (25 pts)** — Outcome per scenario, objection handling (LAER), closing
4. **Compliance & Safety (25 pts)** — AI disclosure, banned phrases, agent name, wrong number handling, voicemail non-compliance, DNC exit, silence handling

---

## Weight Distribution Update

| Scenario | Old Weight | New Weight | Rationale |
|----------|-----------|-----------|-----------|
| easy_close | 30% | 20% | Still core, but make room for edge cases |
| confused_open | 20% | 15% | Balance |
| price_objection | 15% | 10% | Common but not majority |
| has_insurance | 15% | 10% | Common but not majority |
| not_interested | 10% | 5% | Reduce; covered by hostile scenario |
| wrong_number | 10% | 10% | Keep same (boundary case) |
| **silence** | — | **5%** | **NEW: Handle dead air gracefully** |
| **voicemail** | — | **5%** | **NEW: Compliance test (no voicemail)** |
| **skeptical_questions** | — | **10%** | **NEW: Objection handling edge case** |
| **negative_hostile** | — | **5%** | **NEW: DNC compliance test** |
| **off_bounds** | — | **5%** | **NEW: Gentle redirection test** |
| **TOTAL** | 100% | 100% | Balanced across 11 scenarios |

---

## Test Infrastructure

- **Text simulation endpoint:** `POST /v1/test-call-text?scenario=easy_close`
- **All scenarios:** `POST /v1/test-call-text/all`
- **Real call endpoint:** `POST /v1/test-call-v2?to_number=+1XXXXXXXXXX&scenario=easy_close`
- **Self-test (loopback):** `POST /v1/test-call-v2?to_number=%2B19297090284&scenario=easy_close`
  - Note: loopback has audio routing issue (echo suppression blocks prospect audio)

---

## Future Scenarios to Add

<!-- Add new scenarios below as they come up during testing -->

### Spanish Speaker
- **Persona:** TBD — prospect answers in Spanish
- **Expected:** Detect language, graceful exit or language-appropriate handling
- **Status:** Not yet implemented

### Callback Request
- **Persona:** TBD — interested but can't talk now, asks for callback
- **Expected:** Confirm callback time, schedule follow-up, graceful exit
- **Status:** Not yet implemented

### Multiple Decision Makers
- **Persona:** TBD — needs to consult spouse before deciding
- **Expected:** Acknowledge, offer to include spouse on call or schedule callback
- **Status:** Not yet implemented

### Hearing Difficulty
- **Persona:** TBD — elderly, hard of hearing, keeps saying "what?"
- **Expected:** Speak slower/clearer, repeat key points without being condescending
- **Status:** Not yet implemented

### Competitor Mention
- **Persona:** TBD — already talking to another insurance company
- **Expected:** Differentiate, offer comparison, don't trash-talk competitor
- **Status:** Not yet implemented

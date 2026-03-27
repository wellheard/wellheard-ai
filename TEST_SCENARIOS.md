# WellHeard AI — Test Scenarios Reference

Last updated: 2026-03-27
Current avg score: 95.5/100 across all scenarios

## Core Scenarios (Final Expense Aged Leads)

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
4. **Compliance & Safety (25 pts)** — AI disclosure, banned phrases, agent name, wrong number handling

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

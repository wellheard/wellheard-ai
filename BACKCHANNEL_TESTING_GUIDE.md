# Backchannel Testing & Tuning Guide

## Quick Start

### 1. Enable Backchanneling (Default: Enabled in Phase 3)
The system automatically enables backchanneling when Phase 3 (converse mode) starts. No configuration needed.

### 2. Monitor Backchannel Activity
Check logs for:
```
backchannel_enabled
backchannel_audio_loaded (4 types)
backchannel_injectable_pause_detected (pause in 200-400ms window)
backchannel_selected (type: mmhmm/right/yeah/okay)
backchannel_injected (audio queued)
backchannel_suppressed_* (various constraint violations)
```

### 3. Collect Metrics
From `BackchannelManager.get_metrics()`:
```python
{
    "backchannels_injected": 5,           # Total played
    "backchannel_suppressed": 12,         # Total rejected
    "last_backchannel_time": 1234567890, # Unix timestamp
    "enabled": true,
}
```

## Testing Scenarios

### Test 1: Basic Pause Detection
**Objective:** Verify backchannels trigger at natural pauses

**Steps:**
1. Call your test agent
2. Complete pitch phase
3. Speak for 3+ seconds with 300-400ms pauses (natural speech rhythm)
4. Listen for "mm-hmm", "right", etc. during pauses

**Expected:**
- Hear 1-2 backchannels during long speech (8-15s cooldown)
- Backchannels appear during pauses, not during active speech
- No interruption of your speech

### Test 2: Grace Periods
**Objective:** Verify backchanneling respects timing constraints

**Test 2a: AI Grace Period (3 seconds)**
1. Call agent, complete pitch
2. Agent speaks a response
3. Immediately pause (within 0 seconds of agent finishing)
4. No backchannel should be injected

**Expected:**
- No backchannel for ~3 seconds after agent stops speaking
- Backchannel appears after 3-second grace period expires

**Test 2b: Prospect Grace Period (2 seconds)**
1. Call agent, complete pitch
2. Start speaking
3. Pause at <2 second mark
4. No backchannel should be injected

**Expected:**
- No backchannel within first 2 seconds of speaking
- After 2 seconds, pauses trigger backchannels normally

### Test 3: Cooldown Enforcement
**Objective:** Verify minimum 8-second cooldown between backchannels

**Steps:**
1. Speak with natural pauses (300-400ms every 1-2 seconds)
2. Wait for first backchannel injection
3. Continue speaking with pauses
4. Count backchannels

**Expected:**
- First backchannel after ~10-15 seconds of speech
- Subsequent backchannels appear only 8+ seconds later
- Never see rapid-fire "yeah yeah yeah"

### Test 4: Probability Distribution
**Objective:** Verify selection matches 60/20/15/5 distribution

**Steps (Statistical Test):**
1. Record 100+ calls with backchanneling
2. Count occurrences: mmhmm / right / yeah / okay
3. Calculate percentages

**Expected Distribution (±3%):**
- "Mm-hmm": 57-63% (60%)
- "Right": 17-23% (20%)
- "Yeah": 12-18% (15%)
- "Okay": 2-8% (5%)

### Test 5: Turn-End Suppression
**Objective:** Verify NO backchannels when prospect finishes speaking

**Steps:**
1. Make call, complete pitch
2. Speak for 5 seconds, then pause >500ms (end of turn)
3. Agent should respond
4. Verify no backchannel during silence before agent response

**Expected:**
- Silence >500ms triggers turn-end (speech_final)
- Agent response comes 100-200ms later
- No backchannel injection in this window
- Agent response should feel natural (not "interrupted" by backchannel)

## Tuning Parameters

All in `BackchannelManager.__init__()` in `src/call_bridge.py`:

### Pause Detection Windows
```python
self._pause_duration_min_ms = 200  # Minimum pause (ms)
self._pause_duration_max_ms = 400  # Maximum pause (ms)
```
- **If backchannels too frequent:** Increase min_ms (e.g., to 250ms)
- **If backchannels too rare:** Decrease max_ms (e.g., to 350ms)

### Cooldown Timing
```python
self._min_cooldown_s = 8.0   # Minimum between backchannels
self._max_cooldown_s = 15.0  # Maximum (randomized)
```
- **If too frequent:** Increase min/max (e.g., 10-20s)
- **If too rare:** Decrease min/max (e.g., 5-10s)
- **Note:** Random delay adds naturalness

### Grace Periods
```python
self._ai_speech_grace_period_s = 3.0   # After AI speaks
self._prospect_speech_grace_period_s = 2.0  # After prospect starts
```
- **If backchannels too early:** Increase (e.g., 3s → 4s)
- **If backchannels delayed:** Decrease (e.g., 2s → 1.5s)

### Probability Distribution
```python
self._probabilities = {
    "mmhmm": 0.60,   # Most natural
    "right": 0.20,   # Engaged
    "yeah": 0.15,    # Casual
    "okay": 0.05,    # Least frequent
}
```
- **If too many "okay"s:** Decrease to 0.02
- **If not enough "right"s:** Increase to 0.25

## Metrics to Monitor

### Key Metrics
1. **backchannels_injected**: Total played across all calls
   - Expected: ~1-3 per call (varies by call length)
   - Low values: Constraints too strict
   - High values: Constraints too loose

2. **backchannel_suppressed**: Total rejected
   - Should be >backchannels_injected (constraints are working)
   - If 0: Constraints may not be checked
   - If very high: May be too restrictive

3. **Suppression by Reason**:
   - `backchannel_suppressed_ai_grace_period`
   - `backchannel_suppressed_prospect_grace_period`
   - `backchannel_suppressed_cooldown`
   - `backchannel_suppressed_no_audio`

### Conversion Metrics (A/B Test)
Compare backchannel-enabled vs. disabled calls:

```
| Metric | Backchannel ON | Backchannel OFF |
|--------|----------------|-----------------|
| Avg Call Length (s) | Should be higher | Baseline |
| Conversions | Should be higher | Baseline |
| Prospect Feedback | More "human" | Baseline |
| Silence Manager Nudges | Should be lower | Baseline |
```

## Production Rollout Checklist

### Phase 1: Validation (First 100 calls)
- [ ] Monitor backchannel injection logs
- [ ] Verify no errors/exceptions in backchannel code
- [ ] Check audio quality (no artifacts, clear)
- [ ] Confirm no VAD/turn-timer interference
- [ ] Validate metrics collection

### Phase 2: Tuning (Next 500 calls)
- [ ] Analyze backchannel frequency distribution
- [ ] Check suppression reasons (any unexpected patterns?)
- [ ] Gather feedback on naturalness
- [ ] A/B test with disabled group (50/50 split)
- [ ] Tune parameters based on data

### Phase 3: Optimization (Full Rollout)
- [ ] Roll out to all calls
- [ ] Monitor daily metrics dashboard
- [ ] Set up alerts for anomalies
- [ ] Quarterly review of effectiveness

## Troubleshooting

### Problem: No backchannels injected
**Diagnosis:**
1. Check logs for `backchannel_enabled` message
   - Should appear when entering Phase 3
2. Check `backchannel_audio_loaded` messages
   - Should show 4 audio files loaded during dial
3. Look for `backchannel_suppressed_*` messages
   - If many suppressed: Constraints are working, but preventing injections

**Solutions:**
- Increase `_min_cooldown_s` to reduce cooldown threshold
- Increase `_pause_duration_max_ms` to broaden pause window
- Decrease grace periods to allow earlier backchannels

### Problem: Backchannels too frequent (sounds robotic)
**Diagnosis:**
1. Check backchannel frequency (should be 1-3 per call)
2. Look for multiple backchannels <8 seconds apart
3. Check `backchannel_suppressed_cooldown` count
   - High count means cooldown is working, try increasing

**Solutions:**
- Increase `_min_cooldown_s` (e.g., 8 → 10)
- Increase `_pause_duration_min_ms` (e.g., 200 → 250)
- Decrease `_max_cooldown_s` (e.g., 15 → 12)

### Problem: Backchannels interrupt speech
**Diagnosis:**
1. Listen for backchannel during prospect's active speech
2. Check if prospect's next words are affected
3. Look for backchannel RMS calculation errors

**Solutions:**
- Increase `_speech_energy_threshold` (e.g., 300 → 350)
- This makes speech detection stricter (fewer false-silence)

### Problem: Audio quality issues (clicks, artifacts)
**Diagnosis:**
1. Listen for pops/clicks at backchannel boundaries
2. Check if backchannel audio is clipping
3. Verify TTS synthesized correctly

**Solutions:**
- Pre-synthesized audio uses same voice/settings as main speech
- No additional processing (fades/compression) applied
- Should sound natural and clean

### Problem: Silence manager nudges still triggering
**Diagnosis:**
1. Prospect speaking >5 seconds without responding
2. Backchannel has no silence manager integration (correct!)
3. Nudge should still trigger if no substantive response

**Expected Behavior:**
- Backchannel does NOT reset silence manager clock
- If prospect only gives backchannels (no real response), nudge still fires
- This is correct — we want to keep conversation moving

## Performance Monitoring

### Add to Your Monitoring Dashboard
```python
# Backchannel metrics per call
backchannel_injected: histogram
backchannel_suppressed: histogram
backchannel_enabled: gauge
backchannels_by_type: counter (mmhmm/right/yeah/okay)

# Suppression reasons
suppressed_ai_grace_period: counter
suppressed_prospect_grace_period: counter
suppressed_cooldown: counter
suppressed_no_audio: counter
```

### Sample Query (for data analysis)
```sql
SELECT
    call_id,
    count(*) as backchannels_injected,
    max(backchannel_suppressed) as total_suppressed,
    COUNT(CASE WHEN type='mmhmm' THEN 1 END) as mmhmm_count,
    COUNT(CASE WHEN type='right' THEN 1 END) as right_count,
    COUNT(CASE WHEN type='yeah' THEN 1 END) as yeah_count,
    COUNT(CASE WHEN type='okay' THEN 1 END) as okay_count
FROM backchannel_events
GROUP BY call_id;
```

## References

- Implementation: `/sessions/gifted-vigilant-bohr/wellheard-push/BACKCHANNEL_IMPLEMENTATION.md`
- Class: `BackchannelManager` in `src/call_bridge.py`
- Integration: Lines 33-304 (class), 384-385 (init), 497-523 (pre-synth), 1180-1248 (injection)

# Conversation Recovery System

## Overview

The `ConversationRecovery` system is a production-grade failure handler for edge cases that cause calls to go silent, hang up unexpectedly, or get stuck. It proactively detects failure modes and injects pre-synthesized recovery prompts or intelligent fallbacks to keep calls alive and natural.

## Handled Failure Modes

### 1. **STT Transcript Drop**
- **Symptom**: Prospect spoke but no final transcript arrives (network glitch, audio quality issue)
- **Result**: AI goes silent indefinitely
- **Recovery**: Watchdog timer triggers recovery prompts after 8s (AI silent) + 5s (prospect silent)

### 2. **LLM Timeout**
- **Symptom**: Groq occasionally returns errors or takes >5 seconds
- **Result**: Turn hangs, no response generated
- **Recovery**: 4-second timeout triggers fallback response matched to call state

### 3. **TTS Synthesis Failure**
- **Symptom**: Cartesia WebSocket drops or returns empty audio
- **Result**: Prospect hears nothing despite LLM generating response
- **Recovery**: Retry chain - simplified text → semantic cache → generic fallback

### 4. **Double-Speak Guard**
- **Symptom**: AI starts talking, gets interrupted, both responses try to play simultaneously
- **Result**: Audio collision, unintelligible speech
- **Recovery**: Cancel older audio before playing newer (with 100ms gap)

### 5. **Transfer State Stuck**
- **Symptom**: Transfer state gets stuck in DIALING_AGENT (agent hangs up during conference setup)
- **Result**: Prospect waits indefinitely or call drops
- **Recovery**: 30-second timeout offers callback and exits gracefully

## Architecture

### Core Components

#### 1. **Watchdog Timer**
Monitors for prolonged silence (no AI response + no prospect input).

- **First Recovery** (after 8s/5s): "Hey, are you still there?"
- **Second Recovery** (after 10s more): "I think we may have lost the connection. I'll try calling back. Take care!"
- **Then**: End call gracefully

**Implementation**: Async loop checks every 500ms, runs during Phase 3 (CONVERSE).

#### 2. **LLM Timeout Fallback**
Fallback responses appropriate to current call state.

```python
# CONFIRM_INTEREST: "I'd love to help you with that — can you tell me a bit more?"
# BANK_ACCOUNT: "Quick question — do you have a checking or savings account?"
# TRANSFER: "Let me get Sarah on the line for you."
```

**Implementation**: 4-second timeout on LLM generation stream, uses `_recovery.get_llm_fallback_text(call_state)`.

#### 3. **TTS Failure Recovery Chain**
Intelligent fallback when TTS synthesis fails:

1. **Retry with simplified text**: Remove special chars, shorten (max 60 chars)
2. **Semantic cache fallback**: Use pre-synthesized response with high similarity match
3. **Generic fallback**: "Can you repeat that? I want to make sure I heard you correctly."

**Implementation**: Try/except in main TTS loop, each step logs attempt.

#### 4. **Anti-Double-Speak Guard**
Tracks active audio and prevents collision.

```python
# Before playing audio:
if not self._recovery.on_about_to_play_audio("audio_id"):
    # Audio already playing — wait gap then play
    await asyncio.sleep(0.1)
```

**Implementation**: Audio ID tracking with timestamps, 100ms gap between cancel/play.

#### 5. **Transfer State Recovery**
Monitors transfer dial state, times out after 30 seconds.

- **State**: idle → dialing_agent → connected
- **Timeout**: If stuck in DIALING_AGENT for 30s, cancel and offer callback
- **Recovery**: "I'm having trouble reaching Sarah right now. Let me schedule a callback for you."

**Implementation**: Separate watchdog loop per transfer attempt.

## Pre-Synthesized Audio

All recovery phrases are **synthesized during dial time** (alongside other caches) so they're available instantly if needed:

```python
await bridge.pre_synthesize_recovery_audio()
```

This happens during Phase 1 (DETECT) when waiting for the prospect to answer — no latency impact.

**Phrases synthesized:**
- `first_recovery`: "Hey, are you still there?"
- `second_recovery`: "I think we may have lost the connection. I'll try calling back. Take care!"
- `confirm_interest_fallback`: "I'd love to help you with that — can you tell me a bit more?"
- `bank_account_fallback`: "Quick question — do you have a checking or savings account?"
- `transfer_fallback`: "Let me get Sarah on the line for you."
- `tts_fallback`: "Can you repeat that? I want to make sure I heard you correctly."

## Integration Points

### 1. **CallBridge Initialization**
```python
self._recovery = ConversationRecovery(
    config=RecoveryConfig(),
    on_recovery_speak=self._recovery_speak,
    call_id=call_id,
)
```

### 2. **Phase 3 Audio Loop**
Start recovery monitoring when Phase 3 begins:
```python
self._recovery.start()
self._recovery.on_ai_response_end()
```

### 3. **Prospect Speech Tracking**
Signal when prospect speaks:
```python
if is_final and text:
    self._recovery.on_prospect_speech()
```

### 4. **LLM Processing**
Wrap LLM generation with timeout:
```python
try:
    async for chunk in active_llm.generate_stream(...):
        text = chunk.get("text", "")
        if text:
            response_text += text
except asyncio.TimeoutError:
    self._recovery.on_llm_timeout(self._call_state.current_step)
    response_text = self._recovery.get_llm_fallback_text(call_state)
except Exception as e:
    # ... fallback chain
```

### 5. **TTS Synthesis**
Wrap TTS with recovery fallbacks:
```python
try:
    # TTS synthesis
    async for audio_result in tts_generator:
        ...
except Exception as e:
    self._recovery.on_tts_failure(str(e))
    # Retry chain: simplified → cache → generic
```

### 6. **Audio Playback**
Check for double-speak before queueing:
```python
if not self._recovery.on_about_to_play_audio("audio_id"):
    await asyncio.sleep(0.1)  # Wait gap
self._queue_audio(audio)
```

## Configuration

All timeouts and phrases are configurable via `RecoveryConfig`:

```python
@dataclass
class RecoveryConfig:
    watchdog_ai_timeout_s: float = 8.0          # AI silence timeout
    watchdog_prospect_timeout_s: float = 5.0    # Prospect silence timeout
    second_recovery_delay_s: float = 10.0       # Delay before 2nd prompt
    llm_timeout_s: float = 4.0                  # LLM response timeout
    tts_retry_count: int = 1                    # Retry simplified text
    audio_cancel_gap_ms: float = 100.0          # Gap between cancel/play
    transfer_timeout_s: float = 30.0            # Transfer dial timeout

    recovery_phrases: Dict[str, str] = {
        "first_recovery": "Hey, are you still there?",
        "second_recovery": "I think we may have lost...",
        ...
    }
```

## Metrics

Recovery system tracks failure modes for monitoring:

```python
@dataclass
class RecoveryMetrics:
    watchdog_triggers: int = 0          # Silence watchdog fired
    first_recovery_sent: int = 0        # First prompt sent
    second_recovery_sent: int = 0       # Second prompt sent
    llm_timeouts: int = 0               # LLM didn't respond in time
    llm_fallbacks_used: int = 0         # Fallback response used
    tts_failures: int = 0               # TTS synthesis failed
    tts_retries_succeeded: int = 0      # Simplified text retry worked
    tts_cache_fallbacks: int = 0        # Semantic cache fallback used
    tts_generic_fallbacks: int = 0      # Generic "repeat that" fallback
    double_speak_prevented: int = 0     # Audio collision prevented
    transfer_timeouts: int = 0          # Transfer dial timed out
```

Access via:
```python
metrics = bridge._recovery.get_metrics()
logger.info("call_recovery_metrics", **metrics)
```

## Logging

The system logs at key decision points:

```
conversation_recovery_started        — Recovery monitoring started
watchdog_triggers                    — Silence threshold hit
recovery_phrase_sending              — Injecting recovery prompt
llm_timeout_detected                 — LLM response timeout
text_turn_llm_timeout_using_fallback — Using fallback response
tts_failure_detected                 — TTS synthesis failed
text_turn_tts_retry_simplified       — Retrying with simplified text
text_turn_tts_using_semantic_cache   — Using cached response
text_turn_tts_using_generic_fallback — Using "repeat that" fallback
double_speak_detected                — Audio collision detected
transfer_timeout                     — Transfer dial timed out
conversation_recovery_stopped        — Recovery monitoring ended
```

## Error Handling

The system is designed to fail gracefully:

1. **Pre-synthesis failures**: If recovery audio fails to synthesize during dial, individual phrases log warnings but don't block the call
2. **Watchdog exceptions**: Caught and logged, monitoring continues
3. **Recovery prompt failures**: Logged with fallback (synthesize on-demand if pre-synth unavailable)
4. **LLM/TTS failures**: Always have fallback chains — no dead-end failures

## Testing

Example test in server.py:
```python
await bridge.pre_synthesize_recovery_audio()
assert len(bridge._recovery._recovery_audio) > 0
# Test watchdog timeout
await asyncio.sleep(8)  # Trigger watchdog
# Verify recovery prompt was sent
```

## Performance Impact

- **Dial time**: ~500-800ms for 6 recovery phrases (parallel with other caches)
- **Runtime**: Watchdog loop runs every 500ms (negligible CPU)
- **Memory**: ~6KB per phrase × 6 = ~36KB recovery audio cache
- **Latency**: Zero — pre-synthesized audio used immediately

## Production Deployment

1. **Verify all recovery phrases sound natural** in your agent's voice
2. **Test watchdog timeouts** in staging (mute prospect audio, verify "Are you still there?" plays)
3. **Monitor recovery metrics** in logs and dashboards — spikes indicate systemic issues
4. **Adjust timeouts if needed**:
   - Slow network: Increase `llm_timeout_s` to 5-6s
   - Flaky TTS: Lower `tts_retry_count` to 0 (skip retry, go straight to cache)
   - Fast hang-ups: Decrease `watchdog_prospect_timeout_s` to 3s
5. **Set up alerts**:
   - `watchdog_triggers > 5%` of calls indicates STT or network issues
   - `llm_timeouts > 2%` indicates Groq health issues
   - `tts_failures > 1%` indicates Cartesia instability

## API Reference

### ConversationRecovery class

```python
class ConversationRecovery:
    def start()                             # Begin monitoring
    def stop()                              # Stop monitoring
    def set_recovery_audio(key, bytes)     # Load pre-synthesized audio
    def on_ai_response_start()              # AI started generating
    def on_ai_response_end()                # AI finished speaking
    def on_prospect_speech()                # Prospect spoke
    def on_llm_timeout(call_state)          # LLM didn't respond
    def on_tts_failure(error)               # TTS synthesis failed
    def on_about_to_play_audio(id)          # Check before audio playback
    def on_audio_finished()                 # Audio finished playing
    def on_transfer_state_change(state)    # Transfer state updated
    def get_llm_fallback_text(state)        # Get fallback response
    def get_tts_fallback_text()             # Get generic fallback
    def simplify_text_for_tts_retry(text)   # Simplify for retry
    def get_metrics()                       # Get metrics dict
```

## Files Modified

- `src/conversation_recovery.py` — New ConversationRecovery system
- `src/call_bridge.py` — Integration with CallBridge
- `src/api/server.py` — Pre-synthesis calls during dial time

## References

- Silence Manager: `src/silence_manager.py`
- Response Cache: `src/response_cache.py`
- Call State Tracker: `src/call_state.py`

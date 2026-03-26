# Conversation Recovery — Integration Examples

## Quick Start

### 1. System automatically starts during Phase 3

```python
# In call_bridge.py, Phase 3 audio loop
self._recovery.start()
self._recovery.on_ai_response_end()
```

Recovery monitoring now active.

### 2. Track prospect speech

```python
# When STT returns final transcript
if is_final and text:
    accumulated_transcript += " " + text
    self._recovery.on_prospect_speech()  # ← Signal prospect spoke
```

### 3. LLM timeout handling

```python
# In _process_text_turn
try:
    async for chunk in active_llm.generate_stream(...):
        text = chunk.get("text", "")
        if text:
            response_text += text
except asyncio.TimeoutError:
    # LLM timed out — use fallback
    self._recovery.on_llm_timeout(self._call_state.current_step)
    fallback_call_state = CallState(self._call_state.current_step.value)
    response_text = self._recovery.get_llm_fallback_text(fallback_call_state)
    logger.info("using_llm_fallback", response=response_text[:80])
```

### 4. TTS failure recovery

```python
# In _process_text_turn
try:
    async for audio_result in tts_generator:
        audio = audio_result.get("audio", b"")
        # ... process audio
except Exception as e:
    # TTS failed — try recovery chain
    self._recovery.on_tts_failure(str(e))

    # Try 1: Simplified text retry
    simplified = self._recovery.simplify_text_for_tts_retry(response_text)
    if simplified and simplified != response_text:
        try:
            # Retry TTS with simplified text
            new_audio = await active_tts.synthesize_single(simplified)
            if new_audio:
                self._recovery.metrics.tts_retries_succeeded += 1
                # Use new_audio...
        except:
            pass  # Fall through to Try 2

    # Try 2: Semantic cache fallback
    if not audio:  # Still no audio
        semantic_key, semantic_audio, similarity = (
            self._semantic_cache.find_best_match(response_text))
        if semantic_audio and similarity >= 0.65:
            self._recovery.metrics.tts_cache_fallbacks += 1
            audio = semantic_audio

    # Try 3: Generic fallback
    if not audio:
        fallback_text = self._recovery.get_tts_fallback_text()
        try:
            audio = await self.orchestrator.tts.synthesize_single(fallback_text)
            if audio:
                self._recovery.metrics.tts_generic_fallbacks += 1
                response_text = fallback_text
        except:
            pass  # Failed completely
```

### 5. Anti-double-speak guard

```python
# Before queuing any audio
if not self._recovery.on_about_to_play_audio("recovery"):
    # Audio already playing — wait gap before playing new
    logger.warning("double_speak_risk", call_id=self.call_id)
    await asyncio.sleep(0.1)

# Queue audio
queued = self._queue_audio(audio)
```

### 6. Recovery phrase injection

```python
# Called by recovery system when watchdog fires
async def _recovery_speak(self, text: str, audio_bytes: Optional[bytes] = None):
    try:
        # Use pre-synthesized audio if available
        if audio_bytes:
            pcm_data = audio_bytes
        else:
            # Fallback: synthesize on-demand
            pcm_data = await self.orchestrator.tts.synthesize_single(text)

        if pcm_data:
            # Queue with fades (avoid clicks)
            queued = self._queue_pcm_with_fades(pcm_data)

            # Update history
            self.orchestrator._conversation_history.append(
                {"role": "assistant", "content": text})

            # Mark AI speaking
            self._ai_speaking = True
            self._ai_speaking_started_at = time.time()

            # Wait for audio to finish
            play_time = len(pcm_data) / 32000
            await asyncio.sleep(play_time)

            # Clear
            self._ai_speaking = False
            self._recovery.on_audio_finished()

            logger.info("recovery_phrase_sent", text=text[:60])
    except Exception as e:
        logger.warning("recovery_speak_failed", text=text[:60], error=str(e))
```

### 7. Pre-synthesize recovery audio at dial time

```python
# In pre_synthesize_recovery_audio()
async def pre_synthesize_recovery_audio(self):
    logger.info("recovery_audio_synthesis_starting", call_id=self.call_id)
    t0 = time.time()

    for phrase_key, phrase_text in self._recovery.config.recovery_phrases.items():
        try:
            audio = await self.orchestrator.tts.synthesize_single(
                text=phrase_text, voice_id=self.agent_config.voice_id)
            if audio:
                self._recovery.set_recovery_audio(phrase_key, audio)
        except Exception as e:
            logger.warning("recovery_audio_synthesis_failed",
                phrase=phrase_key, error=str(e))

    elapsed = (time.time() - t0) * 1000
    logger.info("recovery_audio_synthesis_done", elapsed_ms=round(elapsed, 1))

# Called during dial time (Phase 1)
await asyncio.gather(
    bridge.pre_synthesize_pitch(),
    bridge.pre_synthesize_fillers(),
    bridge.pre_synthesize_recovery_audio(),  # ← Synthesize in parallel
)
```

### 8. Monitor recovery metrics

```python
# Get all recovery metrics
metrics = bridge._recovery.get_metrics()

# Log them
logger.info("call_recovery_summary",
    watchdog_triggers=metrics.watchdog_triggers,
    llm_timeouts=metrics.llm_timeouts,
    tts_failures=metrics.tts_failures,
    double_speak_prevented=metrics.double_speak_prevented,
    transfer_timeouts=metrics.transfer_timeouts,
)

# Or emit to monitoring system
send_to_datadog({
    "watchdog_triggers": metrics.watchdog_triggers,
    "llm_fallbacks_used": metrics.llm_fallbacks_used,
    "tts_retries_succeeded": metrics.tts_retries_succeeded,
})
```

### 9. Customize recovery config

```python
# Override defaults if needed
from conversation_recovery import RecoveryConfig

custom_config = RecoveryConfig(
    watchdog_ai_timeout_s=10.0,  # Longer silence threshold
    watchdog_prospect_timeout_s=6.0,
    llm_timeout_s=5.0,  # Slower LLM fallback
    transfer_timeout_s=45.0,  # Longer transfer wait
    recovery_phrases={
        "first_recovery": "Hello? Are you there?",  # Custom phrase
        "second_recovery": "We seem to have lost our connection...",
        # ... rest of phrases
    }
)

# Use custom config
self._recovery = ConversationRecovery(
    config=custom_config,
    on_recovery_speak=self._recovery_speak,
    call_id=call_id,
)
```

### 10. Handle transfer state changes

```python
# When initiating transfer
self._recovery.on_transfer_state_change("dialing_agent")

# When agent answers
self._recovery.on_transfer_state_change("connected")

# When agent hangs up or timeout
self._recovery.on_transfer_state_change("idle")
```

## Real-World Examples

### Example 1: Call Goes Silent

```
Timeline:
  T=0s:   Prospect answers
  T=1s:   AI says: "Hi, can you hear me ok?"
  T=3s:   Prospect: "Yeah"
  T=4s:   LLM generates response
  T=7s:   AI: "Great! So I'm calling about..."
  T=12s:  [Silence] No prospect speech detected
  T=13s:  [Silence] Still no prospect speech

Recovery fires:
  T=13s:  Watchdog detects 8s (AI) + 5s (prospect) silence
  T=13s:  Inject: "Hey, are you still there?"
           (using pre-synthesized audio)
  T=15s:  Prospect: "Yeah sorry, I'm here"
  T=15s:  Watchdog resets (prospect spoke)
  T=16s:  Normal LLM turn continues...

Logged:
  conversation_recovery_started
  watchdog_triggers (count=1)
  first_recovery_sent
  recovery_speak_sent (text="Hey, are you still there?", audio_bytes=2048)
```

### Example 2: LLM Times Out

```
Timeline:
  T=0s:   Turn 3 of conversation
  T=0s:   Prospect: "I have a checking account"
  T=0s:   Send to Groq LLM...
  T=4.5s: [Timeout] No response from Groq

Recovery fires:
  T=4.5s: asyncio.TimeoutError caught
  T=4.5s: Check call state: BANK_ACCOUNT
  T=4.5s: Get fallback: "Quick question — do you have..."
  T=4.5s: Use fallback response immediately
  T=5s:   TTS: "Quick question — do you have..."
  T=7s:   Prospect: "Checking account"
  T=8s:   Next LLM turn (hopefully Groq is back)

Logged:
  text_turn_llm_timeout (timeout_s=4.0)
  llm_timeout_detected (state=bank_account)
  text_turn_llm_timeout_using_fallback (response="Quick question...")
  on_llm_timeout (state=BANK_ACCOUNT)
```

### Example 3: TTS Fails, Retries, and Succeeds

```
Timeline:
  T=0s:   LLM generates: "Thank you for the information."
  T=0s:   Try TTS synthesis...
  T=0.5s: [Exception] Cartesia connection dropped

Recovery chain fires:
  Step 1: Simplified text retry
    T=0.5s: Simplify: "Thank you for information" (remove "the")
    T=0.5s: Retry TTS...
    T=1.2s: ✓ Success! Use simplified audio

  Metrics: tts_retries_succeeded += 1
  Logged: text_turn_tts_retry_simplified
          text_turn_tts_retry_succeeded
```

### Example 4: TTS Retry Fails, Use Cache

```
Timeline:
  T=0s:   LLM: "I appreciate your information about the account."
  T=0s:   TTS fails...
  T=0.5s: Simplified retry fails too...

Recovery chain continues:
  Step 2: Semantic cache lookup
    T=0.5s: Find similar cached response...
    T=0.5s: "I appreciate your details" (similarity=0.82)
    T=0.5s: ✓ Use cached audio

  Metrics: tts_cache_fallbacks += 1
  Logged: text_turn_tts_using_semantic_cache_fallback
          (key=appreciate_details, similarity=0.82)
```

### Example 5: Complete TTS Failure, Use Generic

```
Timeline:
  T=0s:   TTS fails...
  T=0.5s: Simplified retry fails...
  T=0.5s: No semantic cache match...

Recovery chain final step:
  Step 3: Generic fallback
    T=0.5s: Use: "Can you repeat that?"
    T=0.5s: Synthesize on-demand...
    T=1.2s: ✓ Fallback audio plays

  Metrics: tts_generic_fallbacks += 1
  Logged: text_turn_tts_using_generic_fallback
```

### Example 6: Anti-Double-Speak Prevention

```
Timeline:
  T=0s:   Queue AI response audio
  T=0s:   Prospect interrupts
  T=0.1s: Barge-in detected
  T=0.1s: Clear audio queue
  T=0.2s: Start generating new LLM response
  T=0.5s: First chunk of interruption response ready

Before playing:
  T=0.5s: on_about_to_play_audio() check
  T=0.5s: No other audio playing → Clear to play
  T=0.5s: Queue new response audio
  T=0.7s: Plays naturally, no collision

Logged:
  barge_in_detected
  barge_in_audio_cleared (cleared_chunks=3)
  recovery_phrase_using_presynthesized
```

### Example 7: Transfer Dial Timeout

```
Timeline:
  T=0s:   LLM: "Let me get Sarah on the line"
  T=1s:   Transfer initiated
  T=1s:   on_transfer_state_change("dialing_agent")
  T=1s:   Watchdog starts 30s timer
  T=5s:   Agent phone rings...
  T=25s:  Agent still hasn't answered
  T=30s:  Watchdog timeout fires

Recovery:
  T=30s:  Cancel transfer dial
  T=30s:  Send: "I'm having trouble reaching Sarah..."
  T=32s:  Offer: "Let me schedule a callback"
  T=33s:  Exit gracefully

Metrics: transfer_timeouts += 1
Logged: transfer_timeout (elapsed_s=30.0)
```

## Debugging

### Check if recovery audio is loaded

```python
# All 6 recovery phrases should be loaded
for key in bridge._recovery._recovery_audio:
    audio = bridge._recovery._recovery_audio[key]
    if audio:
        print(f"✓ {key}: {len(audio)} bytes")
    else:
        print(f"✗ {key}: NOT LOADED")
```

### Manually trigger watchdog

```python
# Simulate silence timeout
import time
bridge._recovery._last_ai_response_time = time.time() - 10
bridge._recovery._last_prospect_speech_time = time.time() - 10
# Watchdog should fire within 500ms
```

### Check metrics during call

```python
# Get live metrics
metrics = bridge._recovery.get_metrics()
print(f"Watchdog triggers: {metrics.watchdog_triggers}")
print(f"LLM timeouts: {metrics.llm_timeouts}")
print(f"Double-speak prevented: {metrics.double_speak_prevented}")
```

### Enable debug logging

```python
import structlog
logger = structlog.get_logger()
logger.setLevel("DEBUG")

# Now you'll see:
# - Every watchdog check
# - Every recovery state change
# - Every audio playback decision
```

## Common Issues & Fixes

### Recovery audio not synthesizing

**Problem**: Pre-synthesis fails during dial
```
recovery_audio_synthesis_failed (phrase=first_recovery)
```

**Fix**: Check TTS connection
```python
# Verify TTS is connected before pre-synthesis
await orchestrator.tts.connect()
await bridge.pre_synthesize_recovery_audio()
```

### Watchdog fires too often

**Problem**: Recovery prompts on every call
```
watchdog_triggers > 50% of calls
```

**Fix**: Increase timeout thresholds
```python
config = RecoveryConfig(
    watchdog_ai_timeout_s=12.0,  # Was 8.0
    watchdog_prospect_timeout_s=8.0,  # Was 5.0
)
```

### LLM fallback sounds robotic

**Problem**: Fallback phrase doesn't match agent's style

**Fix**: Customize phrases
```python
config = RecoveryConfig(
    recovery_phrases={
        "confirm_interest_fallback":
            "Got it — tell me more about what interests you",
        # ... etc
    }
)
```

### TTS retry happening too much

**Problem**: Lots of `tts_retries_succeeded` in metrics
```
tts_retries_succeeded > 5% of calls
```

**Fix**: Check Cartesia health, disable retry if flaky
```python
config = RecoveryConfig(
    tts_retry_count=0  # Skip simplification retry, go straight to cache
)
```

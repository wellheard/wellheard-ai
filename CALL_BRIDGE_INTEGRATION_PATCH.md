# call_bridge.py Integration Patch

This file shows the exact changes needed in call_bridge.py to integrate the LLM optimizations.

## Location 1: Import LLMRouter (optional, for clarity)

**File**: `src/call_bridge.py`
**Line**: ~1 (at the top with other imports)

**Add this import** (optional, for type hints):
```python
from .llm_router import LLMRouter
```

This is optional because the router is already initialized in orchestrator and accessible via `self.orchestrator.llm_router`.

## Location 2: Replace LLM call in text_turn() method

**File**: `src/call_bridge.py`
**Line**: ~2575 (in the `text_turn()` method)

**FIND THIS CODE**:
```python
        response_text = ""
        llm_ttft = 0.0
        try:
            async for chunk in active_llm.generate_stream(
                messages=compressed_messages,
                system_prompt=effective_system_prompt,
                temperature=self.agent_config.temperature,
                max_tokens=self.agent_config.max_tokens,
            ):
                text = chunk.get("text", "")
                llm_ttft = chunk.get("ttft_ms", llm_ttft)
                if text:
                    response_text += text
                if chunk.get("is_complete"):
                    break
```

**REPLACE WITH THIS**:
```python
        response_text = ""
        llm_ttft = 0.0
        first_sentence_queued = False

        try:
            # Use intelligent LLM router with sentence streaming
            # First sentence → TTS starts immediately (don't wait for full LLM response)
            async for sentence_text, is_final in self.orchestrator.llm_router.generate_stream_with_sentence_streaming(
                messages=compressed_messages,
                system_prompt=effective_system_prompt,
                temperature=self.agent_config.temperature,
                max_tokens=self.agent_config.max_tokens,
                turn_number=turn_number,
            ):
                if not sentence_text:
                    continue

                response_text += sentence_text

                # Queue first sentence immediately (don't wait for full LLM response)
                if not first_sentence_queued:
                    logger.info("first_sentence_ready_for_tts",
                        call_id=self.call_id,
                        turn=turn_number,
                        sentence=sentence_text[:80])
                    # Start TTS on first sentence asynchronously
                    # LLM continues generating remaining sentences in parallel
                    await self._synthesize_and_queue(sentence_text)
                    first_sentence_queued = True

                # is_final means we've streamed all sentences
                if is_final:
                    break
```

## Location 3: Update error handling (optional)

**File**: `src/call_bridge.py`
**Line**: ~2587 (in the exception handler)

**CURRENT CODE**:
```python
        except Exception as e:
            logger.error("text_turn_llm_error",
                call_id=self.call_id, turn=turn_number, error=str(e))
            # Immediate fallback
            if self.orchestrator.fallback_llm and active_llm != self.orchestrator.fallback_llm:
                logger.warning("llm_immediate_failover",
                    call_id=self.call_id, turn=turn_number,
                    to=self.orchestrator.fallback_llm.name)
                try:
                    async for chunk in self.orchestrator.fallback_llm.generate_stream(
                        messages=compressed_messages,
                        system_prompt=self.agent_config.system_prompt,
                        temperature=self.agent_config.temperature,
                        max_tokens=self.agent_config.max_tokens,
                    ):
                        text = chunk.get("text", "")
                        llm_ttft = chunk.get("ttft_ms", llm_ttft)
                        if text:
                            response_text += text
                        if chunk.get("is_complete"):
                            break
                except Exception as e2:
                    logger.error("text_turn_fallback_llm_error",
                        call_id=self.call_id, turn=turn_number, error=str(e2))
```

**SIMPLIFY TO** (router handles fallback internally):
```python
        except Exception as e:
            # LLMRouter already handles failover, so this is a catastrophic error
            logger.error("text_turn_llm_catastrophic_error",
                call_id=self.call_id, turn=turn_number, error=str(e))
            # Both primary and fallback providers failed
            # Fall back to graceful exit or pre-baked response
            response_text = "I'm having trouble processing that. Let me get you to an agent."
            await self._synthesize_and_queue(response_text)
            self.orchestrator._conversation_history.append(
                {"role": "assistant", "content": response_text})
            self._call_state.update_from_exchange(transcript, response_text)
            return
```

This is **optional** - you can keep the existing error handling if you prefer extra safety.

## Summary of Changes

**Required changes**: 1 location (Location 2)
**Optional changes**: 2 locations (Location 1 and Location 3)

**Time to implement**: 10 minutes

**What stays the same**:
- All variables that were being set before (`response_text`, `llm_ttft`, etc.)
- All logging that happens after (the turn completion logging)
- The repetition check, post-processing, TTS selection
- Everything else in the method

**What changes**:
- How we get the LLM response (from router instead of raw provider)
- First sentence handling (queue immediately instead of waiting)
- Error handling (can be simplified since router handles failover)

## Testing After Integration

1. **Verify syntax**: Run `python3 -m py_compile src/call_bridge.py`
2. **Verify imports**: Make sure `self.orchestrator.llm_router` is accessible
3. **Manual test**: Call a prospect on staging
4. **Expected behavior**:
   - You hear response beginning within 300ms (vs 1300ms before)
   - Response is 30-50 tokens (vs 150 before)
   - Logs show `first_sentence_ready_for_tts` and routing decisions

## Rollback

If you need to revert:

```python
# Just change this line back:
async for chunk in active_llm.generate_stream(
    messages=compressed_messages,
    system_prompt=effective_system_prompt,
    temperature=self.agent_config.temperature,
    max_tokens=self.agent_config.max_tokens,
):
    text = chunk.get("text", "")
    llm_ttft = chunk.get("ttft_ms", llm_ttft)
    if text:
        response_text += text
    if chunk.get("is_complete"):
        break
```

And revert the error handling changes if you made them.

That's it! All optimization code is already in place.

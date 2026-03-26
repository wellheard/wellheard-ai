"""
OpenAI LLM Provider
- GPT-4.1-nano: $0.10/$0.40 per 1M tokens, ~650ms TTFT, 132 tok/s
- GPT-4.1-mini: $0.40/$1.60 per 1M tokens, ~400ms TTFT
- GPT-4o-mini:  $0.15/$0.60 per 1M tokens, ~500ms TTFT (used by Dasha/Brightcall)
"""
import time
import structlog
from typing import AsyncIterator, Optional

from .base import LLMProvider, ProviderHealth, LatencyTrace

logger = structlog.get_logger()

# Cost tables per model
MODEL_COSTS = {
    "gpt-4.1-nano": (0.00010, 0.00040),    # $0.10/$0.40 per 1M
    "gpt-4.1-mini": (0.00040, 0.00160),    # $0.40/$1.60 per 1M
    "gpt-4o-mini": (0.00015, 0.00060),     # $0.15/$0.60 per 1M
    "gpt-4o-mini-2024-07-18": (0.00015, 0.00060),
    "gpt-4o": (0.00250, 0.01000),          # $2.50/$10.00 per 1M
}


class OpenAILLMProvider(LLMProvider):
    """OpenAI GPT models via the standard chat completions API."""

    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4.1-nano"):
        self.api_key = api_key
        self.model = model
        self._client = None
        self._health = ProviderHealth(provider_name=f"openai_{model}")

        # Set cost based on model
        costs = MODEL_COSTS.get(model, (0.00015, 0.00060))
        self.cost_per_1k_input_tokens = costs[0]
        self.cost_per_1k_output_tokens = costs[1]
        self.name = f"openai_{model}"

    def _get_client(self):
        if not self._client:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client

    async def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 256,
        tools: Optional[list[dict]] = None,
    ) -> AsyncIterator[dict]:
        """Stream tokens from OpenAI. Compatible with Groq interface."""
        trace = LatencyTrace(provider=self.name, operation="generate")
        client = self._get_client()

        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        try:
            kwargs = {
                "model": self.model,
                "messages": full_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            stream = await client.chat.completions.create(**kwargs)

            accumulated_text = ""
            tool_calls_buffer = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Handle text content
                if delta.content:
                    if not trace.first_result_time:
                        trace.mark_first_result()

                    accumulated_text += delta.content
                    self._health.record_success(trace.time_to_first_result_ms)

                    yield {
                        "text": delta.content,
                        "accumulated": accumulated_text,
                        "is_complete": False,
                        "ttft_ms": trace.time_to_first_result_ms,
                        "tool_call": None,
                    }

                # Handle tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {
                                "id": tc.id or "",
                                "function": {"name": "", "arguments": ""}
                            }
                        if tc.function:
                            if tc.function.name:
                                tool_calls_buffer[idx]["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["function"]["arguments"] += tc.function.arguments

                # Check for completion
                if chunk.choices[0].finish_reason:
                    trace.mark_complete()

                    # Emit any buffered tool calls
                    for tc_data in tool_calls_buffer.values():
                        yield {
                            "text": "",
                            "accumulated": accumulated_text,
                            "is_complete": False,
                            "ttft_ms": trace.time_to_first_result_ms,
                            "tool_call": tc_data,
                        }

                    yield {
                        "text": "",
                        "accumulated": accumulated_text,
                        "is_complete": True,
                        "ttft_ms": trace.time_to_first_result_ms,
                        "total_ms": trace.total_ms,
                        "tool_call": None,
                    }

        except Exception as e:
            self._health.record_error()
            error_str = str(e)
            # Mark as permanently unhealthy on quota/auth errors (fail fast)
            if "insufficient_quota" in error_str or "invalid_api_key" in error_str:
                self._health.consecutive_errors = 100  # Force unhealthy instantly
                logger.error("openai_permanently_unhealthy",
                    error=error_str[:100], model=self.model)
            else:
                logger.error("openai_generate_error", error=error_str, model=self.model)
            raise

    def get_health(self) -> ProviderHealth:
        return self._health

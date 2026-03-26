"""
Google Gemini Flash LLM Provider (Quality Mode)
- Gemini 2.5 Flash with ~192ms TTFT
- Excellent function calling support
- $0.15/$0.60 per 1M input/output tokens (text)
- Cost: ~$0.0009/min for voice conversations
"""
import time
import json
import structlog
from typing import AsyncIterator, Optional

from .base import LLMProvider, ProviderHealth, LatencyTrace

logger = structlog.get_logger()


class GeminiLLMProvider(LLMProvider):
    """Google Gemini Flash for quality mode with low latency."""

    name = "gemini_flash"
    cost_per_1k_input_tokens = 0.00015   # $0.15/1M
    cost_per_1k_output_tokens = 0.00060  # $0.60/1M

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self._client = None
        self._health = ProviderHealth(provider_name=self.name)

    def _get_client(self):
        if not self._client:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 256,
        tools: Optional[list[dict]] = None,
    ) -> AsyncIterator[dict]:
        """Stream tokens from Gemini Flash. Best latency among cloud LLMs."""
        trace = LatencyTrace(provider=self.name, operation="generate")
        client = self._get_client()

        # Convert OpenAI-format messages to Gemini format
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        try:
            config = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }
            if system_prompt:
                config["system_instruction"] = system_prompt

            # Use streaming generation
            response = client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=config,
            )

            accumulated_text = ""

            for chunk in response:
                if chunk.text:
                    if not trace.first_result_time:
                        trace.mark_first_result()

                    accumulated_text += chunk.text
                    self._health.record_success(trace.time_to_first_result_ms)

                    yield {
                        "text": chunk.text,
                        "accumulated": accumulated_text,
                        "is_complete": False,
                        "ttft_ms": trace.time_to_first_result_ms,
                        "tool_call": None,
                    }

            trace.mark_complete()
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
            logger.error("gemini_generate_error", error=str(e), model=self.model)
            raise

    def get_health(self) -> ProviderHealth:
        return self._health

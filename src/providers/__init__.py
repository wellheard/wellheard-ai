"""
Provider Abstraction Layer
Every external service implements a common interface.
Swapping providers requires ZERO application code changes.
"""
from .base import STTProvider, LLMProvider, TTSProvider, ProviderHealth
from .deepgram_stt import DeepgramSTTProvider
from .deepgram_tts import DeepgramTTSProvider
from .groq_llm import GroqLLMProvider
from .gemini_llm import GeminiLLMProvider
from .cartesia_tts import CartesiaTTSProvider

__all__ = [
    "STTProvider", "LLMProvider", "TTSProvider", "ProviderHealth",
    "DeepgramSTTProvider", "DeepgramTTSProvider",
    "GroqLLMProvider", "GeminiLLMProvider",
    "CartesiaTTSProvider",
]

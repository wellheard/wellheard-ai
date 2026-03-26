"""
Quality Pipeline: ~$0.032/min
Stack: Deepgram Nova-3 (STT) + Groq Llama (LLM) + Cartesia Sonic (TTS)

Groq as primary LLM: ~100-300ms TTFT on custom LPU hardware
  vs Gemini 2.5 Flash: 434-2800ms TTFT (and free tier = 20 req/day)
Gemini kept as fallback only.
"""
import structlog

from config.settings import settings
from ..providers.deepgram_stt import DeepgramSTTProvider
from ..providers.deepgram_tts import DeepgramTTSProvider
from ..providers.groq_llm import GroqLLMProvider
from ..providers.gemini_llm import GeminiLLMProvider
from ..providers.openai_llm import OpenAILLMProvider
from ..providers.cartesia_tts import CartesiaTTSProvider
from .orchestrator import VoicePipelineOrchestrator

logger = structlog.get_logger()


class QualityPipeline:
    """
    Quality pipeline: Deepgram STT + Groq LLM + Cartesia TTS

    Latency targets:
    - STT:  Deepgram Nova-3    ~3ms (persistent WebSocket)
    - LLM:  Groq Llama         ~100-300ms TTFT
    - TTS:  Cartesia Sonic-3   ~133ms TTFB
    - Total turn-around:       ~636ms (under 800ms target)
    """

    MODE = "quality"
    ESTIMATED_COST_PER_MINUTE = 0.032

    @staticmethod
    def create() -> VoicePipelineOrchestrator:
        """Create pipeline with Groq primary, Gemini fallback."""

        stt = DeepgramSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_stt_model,
            language=settings.deepgram_stt_language,
        )

        # PRIMARY LLM: Groq (fastest TTFT — ~490ms on LPU hardware)
        # OpenAI is fallback for reliability when Groq degrades
        if settings.groq_api_key:
            llm = GroqLLMProvider(
                api_key=settings.groq_api_key,
                model=settings.groq_model,
            )
        else:
            llm = OpenAILLMProvider(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
            )

        tts = CartesiaTTSProvider(
            api_key=settings.cartesia_api_key,
            voice_id=settings.cartesia_voice_id,
            model=settings.cartesia_model,
        )

        # FALLBACK LLM: OpenAI when Groq is primary
        fallback_llm = None
        if settings.groq_api_key and settings.openai_api_key:
            fallback_llm = OpenAILLMProvider(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
            )
        elif settings.google_api_key:
            fallback_llm = GeminiLLMProvider(
                api_key=settings.google_api_key,
                model=settings.gemini_model,
            )

        fallback_tts = None
        if settings.deepgram_api_key:
            fallback_tts = DeepgramTTSProvider(
                api_key=settings.deepgram_api_key,
                model=settings.deepgram_tts_model,
            )

        orchestrator = VoicePipelineOrchestrator(
            stt=stt,
            llm=llm,
            tts=tts,
            fallback_llm=fallback_llm,
            fallback_tts=fallback_tts,
        )

        logger.info("quality_pipeline_created",
            stt=stt.name, llm=llm.name, tts=tts.name,
            fallback_llm=fallback_llm.name if fallback_llm else "none",
        )

        return orchestrator

"""
Budget Pipeline: ~$0.021/min
Stack: Deepgram Nova-3 (STT) + Groq Llama (LLM) + Deepgram Aura-2 (TTS)
Failover: AssemblyAI (STT) + Gemini Flash (LLM) + Cartesia (TTS)
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


class BudgetPipeline:
    """
    Budget pipeline configuration: $0.021/min

    Cost breakdown:
    - STT:  Deepgram Nova-3 streaming    $0.0077/min
    - LLM:  Groq Llama 4 Scout          $0.0002/min
    - TTS:  Deepgram Aura-2             $0.0100/min
    - Telephony: Telnyx (separate)       $0.0070/min
    ─────────────────────────────────────────────────
    Total (excl. telephony):             $0.0179/min
    Total (incl. telephony):             $0.0249/min
    """

    MODE = "budget"
    ESTIMATED_COST_PER_MINUTE = 0.021

    @staticmethod
    def create() -> VoicePipelineOrchestrator:
        """Create a fully configured budget pipeline with failover."""

        # Primary providers
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

        tts = DeepgramTTSProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_tts_model,
        )

        # Fallback providers (auto-switch if primary degrades)
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
        if settings.cartesia_api_key:
            fallback_tts = CartesiaTTSProvider(
                api_key=settings.cartesia_api_key,
                voice_id=settings.cartesia_voice_id,
                model=settings.cartesia_model,
            )

        orchestrator = VoicePipelineOrchestrator(
            stt=stt,
            llm=llm,
            tts=tts,
            fallback_llm=fallback_llm,
            fallback_tts=fallback_tts,
        )

        logger.info("budget_pipeline_created",
            stt=stt.name,
            llm=llm.name,
            tts=tts.name,
            fallback_llm=fallback_llm.name if fallback_llm else "none",
            fallback_tts=fallback_tts.name if fallback_tts else "none",
            estimated_cost=BudgetPipeline.ESTIMATED_COST_PER_MINUTE,
        )

        return orchestrator

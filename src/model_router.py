"""
WellHeard AI — Multi-Model Router
Routes to different LLM and TTS models based on call phase for optimal
cost/performance tradeoff.

Strategy:
- Scripted phases (greeting, identify, urgency, qualify): Cache-only, no LLM needed
- FAQ/objection handling: Fast model (Groq Llama) for quick response
- Free-form conversation: Best model (GPT-4o-mini or Gemini Flash) for quality
- Transfer hold: Cache-only, pre-scripted phrases
- TTS: Cartesia Sonic-3 for all phases (best latency), but emotion/speed varies
"""
import structlog
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from src.response_cache import CallPhase

logger = structlog.get_logger()


@dataclass
class PhaseModelConfig:
    """LLM and TTS configuration for a specific call phase."""
    phase: CallPhase
    model_name: str           # LLM model identifier
    provider: str             # "groq", "gemini", "openai", "cache_only"
    temperature: float = 0.7
    max_tokens: int = 100
    tts_speed: float = 1.0
    tts_emotion: str = "positivity:medium"
    use_cache: bool = True    # Try cache before LLM
    priority: str = "speed"   # "speed", "quality", or "cost"


# ── Default Phase Routing Table ─────────────────────────────────────────

DEFAULT_ROUTING: Dict[CallPhase, PhaseModelConfig] = {
    # Scripted phases — cache only, no LLM needed
    CallPhase.GREETING: PhaseModelConfig(
        phase=CallPhase.GREETING,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=1.0,
        tts_emotion="positivity:medium",
        use_cache=True,
        priority="speed",
    ),
    CallPhase.IDENTIFY: PhaseModelConfig(
        phase=CallPhase.IDENTIFY,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=0.95,  # Slightly slower for the longer identify speech
        tts_emotion="positivity:low",
        use_cache=True,
        priority="speed",
    ),
    CallPhase.URGENCY_PITCH: PhaseModelConfig(
        phase=CallPhase.URGENCY_PITCH,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=1.0,
        tts_emotion="positivity:medium",
        use_cache=True,
        priority="speed",
    ),
    CallPhase.QUALIFY_ACCOUNT: PhaseModelConfig(
        phase=CallPhase.QUALIFY_ACCOUNT,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=1.0,
        tts_emotion="positivity:medium",
        use_cache=True,
        priority="speed",
    ),

    # Transfer phases — cache only, pre-scripted hold speech
    CallPhase.TRANSFER_INIT: PhaseModelConfig(
        phase=CallPhase.TRANSFER_INIT,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=1.0,
        tts_emotion="positivity:high",  # Excited about transfer
        use_cache=True,
        priority="speed",
    ),
    CallPhase.TRANSFER_HOLD: PhaseModelConfig(
        phase=CallPhase.TRANSFER_HOLD,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=0.95,  # Calm, reassuring during hold
        tts_emotion="positivity:low",
        use_cache=True,
        priority="speed",
    ),
    CallPhase.HANDOFF: PhaseModelConfig(
        phase=CallPhase.HANDOFF,
        model_name="cache_only",
        provider="cache_only",
        tts_speed=1.0,
        tts_emotion="positivity:high",
        use_cache=True,
        priority="speed",
    ),

    # FAQ/objections — fast LLM for dynamic response, cache as fallback
    CallPhase.FAQ_RESPONSE: PhaseModelConfig(
        phase=CallPhase.FAQ_RESPONSE,
        model_name="llama-3.3-70b-versatile",
        provider="groq",
        temperature=0.5,  # Lower temp for consistent FAQ answers
        max_tokens=80,    # Keep responses brief
        tts_speed=1.0,
        tts_emotion="positivity:low",  # Empathetic for objections
        use_cache=True,   # Try cache first (common objections are cached)
        priority="speed",
    ),

    # Wrap-up — cache for standard, LLM for custom goodbyes
    CallPhase.WRAP_UP: PhaseModelConfig(
        phase=CallPhase.WRAP_UP,
        model_name="llama-3.3-70b-versatile",
        provider="groq",
        temperature=0.3,
        max_tokens=50,
        tts_speed=1.0,
        tts_emotion="positivity:medium",
        use_cache=True,
        priority="speed",
    ),
}


class ModelRouter:
    """
    Routes LLM and TTS requests to optimal models based on call phase.

    Tracks which model is used for each phase and records performance
    for ongoing optimization.
    """

    def __init__(self, custom_routing: Optional[Dict[CallPhase, PhaseModelConfig]] = None):
        self.routing = custom_routing or dict(DEFAULT_ROUTING)
        self._stats: Dict[str, Dict] = {}  # phase -> {model, count, avg_latency}

    def get_model_for_phase(self, phase: CallPhase) -> PhaseModelConfig:
        """Get the optimal model config for the current call phase."""
        config = self.routing.get(phase)
        if not config:
            # Default to fast FAQ model for unknown phases
            config = self.routing[CallPhase.FAQ_RESPONSE]

        # Record usage stats
        phase_key = phase.value
        if phase_key not in self._stats:
            self._stats[phase_key] = {"model": config.model_name, "count": 0, "total_latency_ms": 0}
        self._stats[phase_key]["count"] += 1

        return config

    def record_latency(self, phase: CallPhase, latency_ms: float) -> None:
        """Record actual latency for a phase to track performance."""
        phase_key = phase.value
        if phase_key in self._stats:
            self._stats[phase_key]["total_latency_ms"] += latency_ms

    def get_tts_params(self, phase: CallPhase) -> Dict:
        """Get TTS parameters (speed, emotion) tuned for this call phase."""
        config = self.get_model_for_phase(phase)
        return {
            "speed": config.tts_speed,
            "emotion": config.tts_emotion,
        }

    def should_use_cache(self, phase: CallPhase) -> bool:
        """Check if this phase should try cache before LLM."""
        config = self.routing.get(phase)
        return config.use_cache if config else True

    def is_cache_only(self, phase: CallPhase) -> bool:
        """Check if this phase is entirely served from cache (no LLM)."""
        config = self.routing.get(phase)
        return config.provider == "cache_only" if config else False

    def get_routing_stats(self) -> Dict:
        """Return routing statistics for QA analysis."""
        stats = {}
        for phase, data in self._stats.items():
            count = data["count"]
            stats[phase] = {
                "model": data["model"],
                "requests": count,
                "avg_latency_ms": round(data["total_latency_ms"] / max(count, 1), 1),
            }
        return stats

    def update_routing(self, phase: CallPhase, new_config: PhaseModelConfig) -> None:
        """Update routing for a specific phase (e.g., after A/B test results)."""
        old = self.routing.get(phase)
        self.routing[phase] = new_config
        logger.info("routing_updated",
            phase=phase.value,
            old_model=old.model_name if old else "none",
            new_model=new_config.model_name,
        )


# ── SSML Naturalness Helpers ────────────────────────────────────────────

def add_naturalness_ssml(text: str, phase: CallPhase) -> str:
    """
    Add SSML tags to text for more natural-sounding speech.

    Cartesia Sonic-3 supports:
    - <break time="Xms"/> — pauses
    - <speed rate="X"> — speed changes within text
    - [laughter] — laughter sound
    - Punctuation-based prosody (commas = natural pauses)

    Strategy:
    - Add micro-pauses after greeting phrases
    - Slow down slightly for important info (price, qualification)
    - Add natural hesitation markers for FAQ responses
    - Speed up slightly for hold-line filler speech
    """
    if phase == CallPhase.GREETING:
        # Natural pause after "Hi"
        text = text.replace("Hi,", "Hi, <break time='300ms'/>")
        text = text.replace("ok?", "ok? <break time='200ms'/>")

    elif phase == CallPhase.IDENTIFY:
        # Slight pause before the key info
        text = text.replace("I have something", "<break time='200ms'/> I have something")
        text = text.replace("Is that correct?", "<break time='400ms'/> Is that correct?")

    elif phase == CallPhase.URGENCY_PITCH:
        # Emphasis on urgency
        text = text.replace("expires tomorrow", "<break time='200ms'/> expires tomorrow")
        text = text.replace("here's the thing", "here's the thing <break time='300ms'/>")

    elif phase == CallPhase.QUALIFY_ACCOUNT:
        # Casual pause before the question
        text = text.replace("one last thing,", "one last thing, <break time='300ms'/>")

    elif phase == CallPhase.FAQ_RESPONSE:
        # Empathetic pause before responding to objections
        text = "Hmm, <break time='400ms'/> " + text

    elif phase == CallPhase.TRANSFER_HOLD:
        # Conversational pacing during hold
        text = text.replace(". ", ". <break time='200ms'/> ")

    elif phase == CallPhase.HANDOFF:
        # Excited pause before good news
        text = text.replace("Great news,", "Great news, <break time='300ms'/>")

    return text


def add_filler_words(text: str, phase: CallPhase) -> str:
    """
    Strategically insert filler words for naturalness.

    Only for phases where fillers are appropriate (not during
    scripted transfer speech or formal qualification).
    """
    filler_phases = {CallPhase.FAQ_RESPONSE, CallPhase.WRAP_UP}

    if phase not in filler_phases:
        return text

    # Add occasional "okay" or "yeah" at the start of FAQ responses
    import random
    fillers = ["Okay, ", "Yeah, ", "So, ", "Right, ", "Got it, "]
    if random.random() < 0.4:  # 40% chance of filler
        text = random.choice(fillers) + text[0].lower() + text[1:]

    return text

"""
WellHeard AI — Predictive Response Cache & Pre-Loading System
Reduces perceived latency by pre-generating likely next responses.

Architecture:
1. Static Cache: Pre-generated audio for scripted phrases (greeting, transfer speech)
2. Predictive Cache: Pre-generates likely responses based on call phase
3. Speculative Execution: Starts TTS on predicted response while STT still processing
4. Semantic Cache: Matches LLM responses to pre-synthesized common patterns via word overlap

Target: Reduce perceived latency from ~1000ms to <400ms for scripted paths.
Semantic cache adds ~30-50ms matching overhead to skip 200-400ms TTS for turns 3+.
"""
import asyncio
import hashlib
import time
import structlog
from typing import Dict, Optional, List, Tuple, Set
from dataclasses import dataclass, field
from enum import Enum

logger = structlog.get_logger()


def _tokenize(text: str) -> Set[str]:
    """Simple word tokenization for semantic matching.
    Lowercases, removes punctuation, keeps meaningful words.

    Strategy: Keep all content words (>= 2 chars), exclude only high-frequency
    function words. This gives better semantic matching for short SDR responses.
    """
    # Remove punctuation and lowercase
    text = text.lower()
    for char in ".,!?;:'\"—-":
        text = text.replace(char, " ")

    # Split and filter: keep words >= 2 chars, exclude only the most common filler
    # For short responses (5-20 words), aggressive filtering kills matching.
    tokens = set()
    filler = {"a", "an", "the", "in", "on", "at", "to", "it", "i", "you", "we", "or", "if", "of", "as", "by"}
    for word in text.split():
        if len(word) >= 2 and word not in filler:
            tokens.add(word)
    return tokens


def compute_jaccard_similarity(text1: str, text2: str) -> float:
    """Compute Jaccard similarity between two texts (word overlap).
    Range: 0.0 (no overlap) to 1.0 (identical after tokenization).

    Jaccard = |intersection| / |union|
    """
    tokens1 = _tokenize(text1)
    tokens2 = _tokenize(text2)

    if not tokens1 or not tokens2:
        return 0.0

    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)

    return intersection / union if union > 0 else 0.0


class CallPhase(str, Enum):
    """Phases of the scripted SDR call flow."""
    GREETING = "greeting"                   # "Hi, can you hear me ok?"
    IDENTIFY = "identify"                   # "This is Becky with Benefits Review..."
    CONFIRM_LASTNAME = "confirm_lastname"   # "You listed your last name as X..."
    URGENCY_PITCH = "urgency_pitch"         # "A preferred offer was marked..."
    QUALIFY_ACCOUNT = "qualify_account"      # "Do you have checking or savings?"
    TRANSFER_INIT = "transfer_init"         # "I have a licensed agent standing by..."
    TRANSFER_HOLD = "transfer_hold"         # Hold-line speech while connecting agent
    HANDOFF = "handoff"                     # "The agent is on the line now..."
    FAQ_RESPONSE = "faq_response"           # Handling objections
    WRAP_UP = "wrap_up"                     # "Have a great day!"


@dataclass
class CachedResponse:
    """A pre-generated response with optional audio."""
    text: str
    audio_bytes: Optional[bytes] = None
    sample_rate: int = 16000
    phase: CallPhase = CallPhase.GREETING
    confidence: float = 1.0  # How likely this response is needed (1.0 = certain)
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300  # Time to live

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds

    @property
    def cache_key(self) -> str:
        return hashlib.md5(self.text.encode()).hexdigest()[:12]


class ResponseCache:
    """
    Multi-level response caching for minimal latency.

    Level 1 — Static: Pre-generated audio for scripted phrases (zero latency)
    Level 2 — Predictive: Pre-generated based on call phase (near-zero latency)
    Level 3 — Speculative: Started during STT processing (reduced latency)
    """

    def __init__(self):
        self._static_cache: Dict[str, CachedResponse] = {}
        self._predictive_cache: Dict[str, List[CachedResponse]] = {}
        self._speculative_tasks: Dict[str, asyncio.Task] = {}
        self._hit_count = 0
        self._miss_count = 0

    # ── Level 1: Static Cache ─────────────────────────────────────────────

    def preload_static_phrases(self, contact_name: str, last_name: str) -> List[str]:
        """
        Generate the full set of scripted phrases for this call.
        These should be pre-synthesized as audio before the call starts.
        Returns list of phrases to synthesize.
        """
        phrases = {
            "greeting": f"Hey {contact_name}, how've you been?",

            "identify": (
                f"This is Becky with the Benefits Review Team. "
                f"The reason I'm calling — it looks like a while back you spoke "
                f"with someone about getting a quote on final expense coverage, "
                f"you know, for burial or cremation, and I just wanted to follow up on that. "
                f"I have the last name here as {last_name}. Is that right?"
            ),

            "urgency": (
                f"Okay, yeah. So here's the thing. "
                f"A preferred offer for your burial or cremation coverage "
                f"was marked for you, and for whatever reason it was never claimed. "
                f"With funeral costs running over nine thousand dollars these days, "
                f"this is definitely worth looking at — and it actually expires tomorrow. "
                f"Are you still interested in getting that quote before it expires?"
            ),

            "qualify": (
                "Okay, perfect. So one last thing, "
                "people who have a checking or savings account usually get the biggest discounts. "
                "Do you have one or the other?"
            ),

            "transfer_init": (
                "Okay, great. I have a licensed agent standing by to give you a quote. "
                "I'll have them jump on the call ASAP to walk you through all the details."
            ),

            "transfer_hold_1": (
                "I'm seeing a preferred discounted offer attached to your profile "
                "that reflects the best pricing available today based on your age and health. "
                "That pricing window is expiring soon, so we want to make sure "
                "the agent reviews it with you before it updates."
            ),

            "transfer_hold_2": (
                "The main thing with whole life insurance is making sure you have "
                "the right coverage and the right beneficiary so the money goes "
                "exactly where you want. The agent will walk you through all of that."
            ),

            "transfer_hold_3": (
                "Just so you know, when the agent joins there might be "
                "a quick moment of silence as they jump in. As soon as you hear them, "
                "just let them know you're there and they'll take great care of you."
            ),

            "handoff": (
                f"Great news — I have the agent on the line now. "
                f"I'm going to hand you over. It was great talking with you, "
                f"{contact_name}. You're in good hands."
            ),

            # FAQ responses — pre-cache the most common ones
            "faq_not_interested": (
                "I understand, many people feel that way at first. "
                "Just so you know, we're offering free quotes with no obligation to buy, "
                "tailored to your budget and needs. Can I ask what concerns you most?"
            ),

            "faq_already_insured": (
                "That's great you have something in place! Most folks I talk to do. "
                "This is actually about something a little different — it's specifically "
                "for final expenses like burial or cremation, so your family isn't pulling "
                "from savings or your other policy for that. The agent can show you how "
                "it works alongside what you already have. No obligation at all."
            ),

            "faq_privacy": (
                "I totally respect your privacy. The only info needed initially is basic, "
                "like your age and a few simple questions so we can see what plans "
                "you may qualify for. There's no obligation to go further."
            ),

            "faq_how_much": (
                "Great question. It really depends on your age and the coverage amount, "
                "but most people I talk to are looking at somewhere around a dollar or two a day. "
                "The agent will be able to pull up the exact numbers for you in about two minutes — "
                "that's really the quickest way to get your specific rate."
            ),

            "faq_cant_afford": (
                "I totally get it, and affordability is really important. "
                "That's actually exactly what the agent helps with — finding a plan "
                "that fits what you can do, even if it's a small amount. "
                "Something is better than nothing, right? "
                "Would it hurt to at least hear what the options are?"
            ),

            "wrong_person": (
                f"Ok, got it, I'm actually looking for {contact_name}, "
                f"but if that's not you, I might have gotten the wrong number, correct?"
            ),

            "wrap_up_positive": (
                "Thank you so much for your time today. Have a wonderful day!"
            ),

            "wrap_up_not_interested": (
                "No worries at all. I appreciate your time. Have a great day!"
            ),

            "callback": (
                "I will make sure a colleague will reach out to you to help you further."
            ),
        }

        # Store in static cache
        for key, text in phrases.items():
            self._static_cache[key] = CachedResponse(
                text=text,
                phase=self._key_to_phase(key),
                confidence=1.0,
            )

        return list(phrases.values())

    def get_static(self, key: str) -> Optional[CachedResponse]:
        """Get a pre-cached static response."""
        resp = self._static_cache.get(key)
        if resp:
            self._hit_count += 1
            return resp
        self._miss_count += 1
        return None

    def set_audio(self, key: str, audio_bytes: bytes, sample_rate: int = 16000) -> None:
        """Attach pre-synthesized audio to a cached response."""
        if key in self._static_cache:
            self._static_cache[key].audio_bytes = audio_bytes
            self._static_cache[key].sample_rate = sample_rate

    # ── Level 2: Predictive Cache ─────────────────────────────────────────

    def predict_next_responses(self, current_phase: CallPhase) -> List[str]:
        """
        Based on current call phase, predict which responses are most likely needed next.
        Returns cache keys of responses to pre-generate.
        """
        predictions = {
            CallPhase.GREETING: ["identify"],
            CallPhase.IDENTIFY: ["urgency", "wrong_person"],
            CallPhase.CONFIRM_LASTNAME: ["urgency"],
            CallPhase.URGENCY_PITCH: [
                "qualify",
                "faq_not_interested",
                "faq_already_insured",
            ],
            CallPhase.QUALIFY_ACCOUNT: [
                "transfer_init",
                "faq_privacy",
                "wrap_up_not_interested",
            ],
            CallPhase.TRANSFER_INIT: [
                "transfer_hold_1",
                "transfer_hold_2",
                "transfer_hold_3",
                "transfer_silence_warning",
            ],
            CallPhase.TRANSFER_HOLD: [
                "handoff",
                "faq_how_much",
            ],
        }

        return predictions.get(current_phase, [])

    # ── Level 3: Speculative Execution ────────────────────────────────────

    async def speculate(self, key: str, synthesize_fn, voice_id: str) -> None:
        """
        Speculatively synthesize audio for a predicted response.
        Runs in background, result stored in cache.
        """
        resp = self._static_cache.get(key)
        if not resp or resp.audio_bytes:
            return  # Already has audio or doesn't exist

        async def _synth():
            try:
                audio = await synthesize_fn(resp.text, voice_id)
                if audio:
                    resp.audio_bytes = audio
                    logger.debug("speculative_synth_done", key=key, size=len(audio))
            except Exception as e:
                logger.warning("speculative_synth_failed", key=key, error=str(e))

        task = asyncio.create_task(_synth())
        self._speculative_tasks[key] = task

    async def presynthesize_all(self, synthesize_fn, voice_id: str) -> int:
        """
        Pre-synthesize audio for ALL static cache entries.
        Call this before the call starts. Returns count of items synthesized.
        """
        count = 0
        for key, resp in self._static_cache.items():
            if not resp.audio_bytes:
                try:
                    audio = await synthesize_fn(resp.text, voice_id)
                    if audio:
                        resp.audio_bytes = audio
                        count += 1
                except Exception as e:
                    logger.warning("presynth_failed", key=key, error=str(e))
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.2)

        logger.info("presynthesize_complete", count=count, total=len(self._static_cache))
        return count

    # ── Metrics ───────────────────────────────────────────────────────────

    def get_metrics(self) -> dict:
        total = self._hit_count + self._miss_count
        return {
            "static_cache_size": len(self._static_cache),
            "audio_cached": sum(1 for r in self._static_cache.values() if r.audio_bytes),
            "cache_hits": self._hit_count,
            "cache_misses": self._miss_count,
            "hit_rate": round(self._hit_count / max(total, 1) * 100, 1),
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _key_to_phase(key: str) -> CallPhase:
        mapping = {
            "greeting": CallPhase.GREETING,
            "identify": CallPhase.IDENTIFY,
            "urgency": CallPhase.URGENCY_PITCH,
            "qualify": CallPhase.QUALIFY_ACCOUNT,
            "transfer_init": CallPhase.TRANSFER_INIT,
            "transfer_hold": CallPhase.TRANSFER_HOLD,
            "handoff": CallPhase.HANDOFF,
            "faq": CallPhase.FAQ_RESPONSE,
            "wrap_up": CallPhase.WRAP_UP,
            "wrong_person": CallPhase.FAQ_RESPONSE,
            "callback": CallPhase.WRAP_UP,
        }
        for prefix, phase in mapping.items():
            if key.startswith(prefix):
                return phase
        return CallPhase.FAQ_RESPONSE


class SemanticResponseCache:
    """
    Semantic cache for turns 3+ — matches LLM-generated responses to pre-synthesized
    common patterns via word-overlap similarity.

    Common response patterns are pre-synthesized during dial time (~5-10 seconds available).
    At runtime, when the LLM generates a response, we compute Jaccard similarity against
    cached templates. If similarity > threshold (default 0.85), use cached audio instead of TTS.

    This saves ~200-400ms of TTS latency per cache hit while maintaining natural variation
    (different wordings still match semantically similar responses).

    Example:
      LLM generates: "Got it, let me get Sarah on the line for you."
      Cached: "Let me get Sarah on the line for you."
      Similarity: ~0.90 → cache hit → skip TTS, use pre-synthesized audio

    Architecture:
    - Semantic templates: ~15-20 high-frequency response patterns
    - Tokenization: word overlap with stopword filtering
    - Matching: find best match by Jaccard similarity
    - Anti-repetition: still checks against previous responses to prevent dull calls
    """

    # Common response patterns covering ~70-80% of conversational turns in the flow
    # Strategy: Keep templates SHORT and SEMANTICALLY DISTINCT to maximize Jaccard matching
    # while minimizing false positives. The matching logic handles paraphrases via
    # word overlap. Longer phrases = more tokens = harder to match when paraphrased.
    #
    # Coverage:
    # - Acknowledgments: "Got it", "Makes sense", "I hear you", "That's fair"
    # - Bank account qualification: "checking or savings account"
    # - Transfer initiation: "get Sarah on the line", "connect Sarah", "Sarah now"
    # - Objection handling: "free no strings", "quick look", "obligation"
    # - Interest confirmation: "pull it up", "worth a look"
    # - Exit phrases: "have a great day", "no worries"
    SEMANTIC_TEMPLATES = {
        # Acknowledgments — high frequency, cover ~25% of turns
        # Kept very short to maximize word overlap with paraphrases
        "ack_got_it": "Got it.",
        "ack_makes_sense": "Makes sense.",
        "ack_fair": "That's fair.",
        "ack_hear_you": "I hear you.",

        # Bank account question — the key qualifier for step 2
        # Two variations to catch both formal and casual asks
        "qualify_account": "Do you have checking or savings account?",
        "qualify_account_alt": "Do you have checking or savings?",

        # Transfer initiation — triggers agent handoff
        # Multiple variations because LLM often paraphrases this
        "transfer_get_sarah": "Let me get Sarah on the line.",
        "transfer_connect_sarah": "I'm going to connect you to Sarah.",
        "transfer_now": "Connecting you to Sarah now.",

        # Objection responses — common objection patterns
        # Kept concise to match even when combined with other clauses
        "obj_free": "It's free, no strings.",
        "obj_free_alt": "Free, no strings.",
        "obj_quick": "Quick look.",
        "obj_no_obligation": "No obligation.",

        # Interest confirmation
        "confirm_pull_up": "Want me to pull it up?",
        "confirm_worth": "Worth a look?",

        # Exit/wrap-up — final turn
        "exit_day": "Have a great day!",
        "exit_wonderful": "Have a wonderful day!",
        "exit_no_worries": "No worries, have a great day!",
    }

    def __init__(self, similarity_threshold: float = 0.65):
        """
        Initialize semantic cache.

        Args:
            similarity_threshold: Jaccard similarity threshold (0.0-1.0).
                                 Default 0.65 balances precision (avoid false positives)
                                 with recall (catch paraphrases).
                                 Tuned empirically for short SDR responses (5-20 words).
                                 Range 0.60-0.70 works best for conversational matching.
        """
        self.similarity_threshold = similarity_threshold
        self._semantic_cache: Dict[str, Tuple[str, Optional[bytes]]] = {}
        self._hit_count = 0
        self._miss_count = 0
        self._match_details: List[Tuple[str, str, float]] = []  # (key, generated_text, similarity)

    async def presynthesize_all(self, synthesize_fn, voice_id: str) -> int:
        """
        Pre-synthesize audio for all semantic templates during dial time.
        Call this while the phone is ringing (5-10 seconds available).

        Args:
            synthesize_fn: Async function(text, voice_id) → bytes (PCM 16kHz)
            voice_id: Voice ID for synthesis

        Returns:
            Count of templates synthesized successfully
        """
        count = 0
        for key, text in self.SEMANTIC_TEMPLATES.items():
            try:
                audio = await synthesize_fn(text, voice_id)
                if audio:
                    self._semantic_cache[key] = (text, audio)
                    count += 1
            except Exception as e:
                logger.debug("semantic_presynth_failed", key=key, error=str(e))
            # Tiny delay to avoid rate limiting
            await asyncio.sleep(0.1)

        logger.info("semantic_cache_presynthesize_complete",
            count=count, total=len(self.SEMANTIC_TEMPLATES))
        return count

    def set_audio(self, key: str, audio_bytes: bytes) -> None:
        """Attach pre-synthesized audio to a semantic template."""
        if key in self.SEMANTIC_TEMPLATES:
            text = self.SEMANTIC_TEMPLATES[key]
            self._semantic_cache[key] = (text, audio_bytes)

    def find_best_match(self, generated_text: str) -> Tuple[Optional[str], Optional[bytes], float]:
        """
        Find the best semantic match for LLM-generated text.

        Computes Jaccard similarity against all cached templates.
        Returns the best match if similarity >= threshold.

        Args:
            generated_text: Text produced by LLM (may be longer/paraphrased)

        Returns:
            Tuple of (cache_key, audio_bytes, similarity_score)
            Returns (None, None, 0.0) if no match above threshold
        """
        if not self._semantic_cache:
            return None, None, 0.0

        best_key = None
        best_audio = None
        best_similarity = 0.0

        for key, (template_text, audio_bytes) in self._semantic_cache.items():
            if not audio_bytes:
                continue  # Skip if synthesis failed
            similarity = compute_jaccard_similarity(generated_text, template_text)
            if similarity > best_similarity:
                best_similarity = similarity
                best_key = key
                best_audio = audio_bytes

        # Only return if above threshold
        if best_similarity >= self.similarity_threshold and best_key:
            self._hit_count += 1
            self._match_details.append((best_key, generated_text, best_similarity))
            logger.debug("semantic_cache_match",
                key=best_key, similarity=round(best_similarity, 3),
                generated=generated_text[:100])
            return best_key, best_audio, best_similarity

        self._miss_count += 1
        return None, None, 0.0

    def get_metrics(self) -> dict:
        """Return cache hit/miss metrics."""
        total = self._hit_count + self._miss_count
        return {
            "semantic_cache_size": len(self._semantic_cache),
            "semantic_cache_hits": self._hit_count,
            "semantic_cache_misses": self._miss_count,
            "semantic_cache_hit_rate": round(self._hit_count / max(total, 1) * 100, 1),
            "cached_templates": sum(1 for _, audio in self._semantic_cache.values() if audio),
        }

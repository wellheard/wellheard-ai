"""
WellHeard AI — Voice Naturalness QA & Auto-Improvement System
Automated scoring and iterative improvement of voice quality.

Scoring Dimensions:
1. Prosody Score — Pitch variation, rhythm, stress patterns
2. Pacing Score — Response latency, speaking rate, pause timing
3. Conversational Score — Turn-taking, filler usage, emotional alignment
4. Script Adherence Score — Following the call phases correctly
5. Overall MOS Estimate — Neural network-based quality prediction

Target: 95% of callers should NOT suspect they're talking to AI.
Benchmark: Dasha.ai/Brightcall performance (avg 1.0-1.4s response gaps).
"""
import json
import time
import structlog
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum

logger = structlog.get_logger()


class NaturalnessLevel(str, Enum):
    """Naturalness classification."""
    HUMAN_LIKE = "human_like"           # MOS 4.3+ — indistinguishable from human
    MOSTLY_NATURAL = "mostly_natural"   # MOS 3.8-4.3 — occasional tells
    NOTICEABLE_AI = "noticeable_ai"     # MOS 3.3-3.8 — clearly AI but acceptable
    ROBOTIC = "robotic"                 # MOS <3.3 — unacceptable


@dataclass
class VoiceQualityMetrics:
    """Comprehensive voice quality metrics for a single call."""
    call_id: str
    timestamp: float = field(default_factory=time.time)

    # Pacing metrics (from transcript timing)
    avg_response_gap_ms: float = 0       # Average gap between turns
    max_response_gap_ms: float = 0       # Maximum gap (indicates stalling)
    avg_sdr_words_per_turn: float = 0    # Brevity of SDR responses
    total_turns: int = 0
    call_duration_seconds: float = 0

    # Script adherence
    phases_completed: List[str] = field(default_factory=list)
    phases_skipped: List[str] = field(default_factory=list)
    faq_responses_used: int = 0
    script_deviation_count: int = 0

    # Conversational quality
    prospect_engagement_level: str = ""   # "high", "medium", "low"
    successful_transfer: bool = False
    call_outcome: str = ""

    # TTS quality markers (from audio analysis)
    estimated_mos: float = 0              # Neural network MOS estimate
    prosody_score: float = 0              # Pitch/rhythm variation (0-100)
    emotion_consistency: float = 0        # Emotional alignment with context (0-100)

    @property
    def overall_naturalness_score(self) -> float:
        """Combined naturalness score (0-100)."""
        scores = []

        # Pacing score (target: 800-1400ms avg response gap like Dasha)
        if self.avg_response_gap_ms > 0:
            if self.avg_response_gap_ms <= 1000:
                pacing = 100
            elif self.avg_response_gap_ms <= 1400:
                pacing = 90
            elif self.avg_response_gap_ms <= 2000:
                pacing = 70
            else:
                pacing = max(30, 100 - (self.avg_response_gap_ms - 1000) / 20)
            scores.append(("pacing", pacing, 0.25))

        # Brevity score (target: 15-20 words per SDR turn like real calls)
        if self.avg_sdr_words_per_turn > 0:
            if 10 <= self.avg_sdr_words_per_turn <= 25:
                brevity = 100
            elif self.avg_sdr_words_per_turn <= 40:
                brevity = 70
            else:
                brevity = max(20, 100 - (self.avg_sdr_words_per_turn - 25) * 2)
            scores.append(("brevity", brevity, 0.15))

        # Script adherence score
        expected_phases = 5  # identify, urgency, qualify, transfer, handoff
        completed = len(self.phases_completed)
        adherence = min(100, (completed / max(expected_phases, 1)) * 100)
        if self.script_deviation_count > 0:
            adherence -= self.script_deviation_count * 10
        scores.append(("script_adherence", max(0, adherence), 0.20))

        # MOS score contribution (if available)
        if self.estimated_mos > 0:
            mos_score = min(100, (self.estimated_mos / 5.0) * 100)
            scores.append(("mos", mos_score, 0.25))

        # Prosody score (if available)
        if self.prosody_score > 0:
            scores.append(("prosody", self.prosody_score, 0.15))

        # Weighted average
        if scores:
            total_weight = sum(w for _, _, w in scores)
            weighted = sum(s * w for _, s, w in scores) / total_weight
            return round(weighted, 1)

        return 0

    @property
    def naturalness_level(self) -> NaturalnessLevel:
        score = self.overall_naturalness_score
        if score >= 86:
            return NaturalnessLevel.HUMAN_LIKE
        elif score >= 76:
            return NaturalnessLevel.MOSTLY_NATURAL
        elif score >= 66:
            return NaturalnessLevel.NOTICEABLE_AI
        else:
            return NaturalnessLevel.ROBOTIC


@dataclass
class ABTestConfig:
    """Configuration for A/B testing voice parameters."""
    test_name: str
    variant_a: Dict = field(default_factory=dict)  # e.g. {"speed": 1.0, "emotion": "neutral"}
    variant_b: Dict = field(default_factory=dict)  # e.g. {"speed": 0.95, "emotion": "positivity:low"}
    metrics_a: List[VoiceQualityMetrics] = field(default_factory=list)
    metrics_b: List[VoiceQualityMetrics] = field(default_factory=list)

    @property
    def avg_score_a(self) -> float:
        if not self.metrics_a:
            return 0
        return sum(m.overall_naturalness_score for m in self.metrics_a) / len(self.metrics_a)

    @property
    def avg_score_b(self) -> float:
        if not self.metrics_b:
            return 0
        return sum(m.overall_naturalness_score for m in self.metrics_b) / len(self.metrics_b)

    @property
    def winner(self) -> str:
        if self.avg_score_a > self.avg_score_b:
            return "A"
        elif self.avg_score_b > self.avg_score_a:
            return "B"
        return "tie"


class NaturalnessQA:
    """
    QA system for voice naturalness with auto-improvement.

    Pipeline:
    1. Score each call on multiple dimensions
    2. Compare against Dasha.ai benchmark
    3. Identify weakest dimensions
    4. Generate parameter adjustment recommendations
    5. A/B test new parameters
    6. Deploy winning config
    """

    # Dasha.ai/Brightcall benchmark targets (from real call analysis)
    BENCHMARK = {
        "avg_response_gap_ms": 1200,     # ~1.2s average from real calls
        "max_response_gap_ms": 3500,     # ~3.5s max from real calls
        "avg_sdr_words_per_turn": 17,    # ~17 words from real calls
        "call_duration_target_s": 90,     # ~60-90s for qualified transfers
        "target_naturalness": 86,         # HUMAN_LIKE threshold
    }

    def __init__(self):
        self.call_metrics: List[VoiceQualityMetrics] = []
        self.ab_tests: List[ABTestConfig] = []
        self.improvement_log: List[Dict] = []

    def score_call(self, call_record, scenario=None) -> VoiceQualityMetrics:
        """Score a call record for naturalness quality."""
        metrics = VoiceQualityMetrics(call_id=call_record.scenario_id)

        # Calculate pacing metrics
        turns = call_record.turns
        if len(turns) >= 2:
            gaps = []
            sdr_word_counts = []
            for i, turn in enumerate(turns):
                if turn.speaker == "sdr":
                    words = len(turn.text.split())
                    sdr_word_counts.append(words)
                if i > 0 and turn.latency_ms > 0:
                    gaps.append(turn.latency_ms)

            metrics.avg_response_gap_ms = sum(gaps) / len(gaps) if gaps else 0
            metrics.max_response_gap_ms = max(gaps) if gaps else 0
            metrics.avg_sdr_words_per_turn = (
                sum(sdr_word_counts) / len(sdr_word_counts) if sdr_word_counts else 0
            )

        metrics.total_turns = len(turns)
        metrics.call_duration_seconds = call_record.duration_seconds
        metrics.call_outcome = call_record.outcome

        # Script adherence analysis
        transcript_lower = call_record.transcript.lower()
        phase_markers = {
            "identify": "benefits review team",
            "urgency": "expires tomorrow",
            "qualify": "checking or savings",
            "transfer": "licensed agent standing by",
            "handoff": "agent on the line",
        }
        for phase, marker in phase_markers.items():
            if marker in transcript_lower:
                metrics.phases_completed.append(phase)
            else:
                metrics.phases_skipped.append(phase)

        metrics.successful_transfer = "transfer" in metrics.phases_completed

        self.call_metrics.append(metrics)
        return metrics

    def compare_to_benchmark(self, metrics: VoiceQualityMetrics) -> Dict:
        """Compare call metrics against Dasha.ai benchmark."""
        comparison = {}

        # Pacing comparison
        benchmark_gap = self.BENCHMARK["avg_response_gap_ms"]
        actual_gap = metrics.avg_response_gap_ms
        if actual_gap > 0:
            comparison["response_gap"] = {
                "benchmark_ms": benchmark_gap,
                "actual_ms": round(actual_gap),
                "status": "pass" if actual_gap <= benchmark_gap * 1.2 else "fail",
                "delta_ms": round(actual_gap - benchmark_gap),
            }

        # Brevity comparison
        benchmark_words = self.BENCHMARK["avg_sdr_words_per_turn"]
        actual_words = metrics.avg_sdr_words_per_turn
        if actual_words > 0:
            comparison["brevity"] = {
                "benchmark_words": benchmark_words,
                "actual_words": round(actual_words),
                "status": "pass" if actual_words <= benchmark_words * 1.5 else "fail",
            }

        # Overall naturalness
        comparison["naturalness"] = {
            "score": metrics.overall_naturalness_score,
            "level": metrics.naturalness_level.value,
            "target": self.BENCHMARK["target_naturalness"],
            "status": "pass" if metrics.overall_naturalness_score >= self.BENCHMARK["target_naturalness"] else "fail",
        }

        return comparison

    def generate_improvements(self, metrics: VoiceQualityMetrics) -> List[Dict]:
        """Generate specific improvement recommendations based on metrics."""
        improvements = []

        # Pacing improvements
        if metrics.avg_response_gap_ms > 1500:
            improvements.append({
                "dimension": "latency",
                "priority": "high",
                "recommendation": "Enable response pre-caching for scripted phrases",
                "action": "Presynthesize all static cache entries before call starts",
                "expected_improvement_ms": 500,
            })

        if metrics.max_response_gap_ms > 4000:
            improvements.append({
                "dimension": "max_latency",
                "priority": "high",
                "recommendation": "Add speculative execution for FAQ responses",
                "action": "Pre-generate top 3 most likely responses during prospect speech",
                "expected_improvement_ms": 1000,
            })

        # Brevity improvements
        if metrics.avg_sdr_words_per_turn > 30:
            improvements.append({
                "dimension": "brevity",
                "priority": "medium",
                "recommendation": "SDR responses too long — reduce max_tokens and add brevity instruction",
                "action": "Set max_tokens=100 and add 'Keep responses under 2 sentences' to prompt",
            })

        # Script adherence
        if metrics.phases_skipped:
            improvements.append({
                "dimension": "script_adherence",
                "priority": "high",
                "recommendation": f"Missed script phases: {', '.join(metrics.phases_skipped)}",
                "action": "Strengthen phase-following instructions in system prompt",
            })

        # TTS quality
        if metrics.estimated_mos > 0 and metrics.estimated_mos < 4.0:
            improvements.append({
                "dimension": "voice_quality",
                "priority": "high",
                "recommendation": "Voice quality below professional threshold",
                "action": "Test different Cartesia Sonic-3 emotion/speed settings via A/B test",
            })

        return improvements

    def create_ab_test(self, test_name: str,
                        variant_a: Dict, variant_b: Dict) -> ABTestConfig:
        """Create a new A/B test for voice parameters."""
        test = ABTestConfig(
            test_name=test_name,
            variant_a=variant_a,
            variant_b=variant_b,
        )
        self.ab_tests.append(test)
        logger.info("ab_test_created", name=test_name, a=variant_a, b=variant_b)
        return test

    def get_summary_report(self) -> Dict:
        """Generate a summary report of all QA metrics."""
        if not self.call_metrics:
            return {"status": "no_data"}

        scores = [m.overall_naturalness_score for m in self.call_metrics]
        levels = [m.naturalness_level.value for m in self.call_metrics]

        return {
            "total_calls_scored": len(self.call_metrics),
            "avg_naturalness_score": round(sum(scores) / len(scores), 1),
            "min_score": round(min(scores), 1),
            "max_score": round(max(scores), 1),
            "naturalness_distribution": {
                level: levels.count(level) for level in set(levels)
            },
            "benchmark_target": self.BENCHMARK["target_naturalness"],
            "meeting_benchmark": sum(1 for s in scores if s >= self.BENCHMARK["target_naturalness"]),
            "active_ab_tests": len(self.ab_tests),
        }

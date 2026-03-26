"""
WellHeard AI — A/B Testing Framework

Systematic testing framework for prompt variants, speaking speeds, temperature,
max tokens, and other configurable parameters. Tracks statistical significance
and identifies winners with statistical confidence.

DESIGN:
- Assign each call_id to variant A or B (50/50 random)
- Track which variant each call belongs to
- Store results: grade scores, latency, turns, transfer rate
- Compute statistical significance (z-test for proportions, t-test for means)
- Return winner when p < 0.05 and min 20 calls per variant

Thread-safe with per-experiment locking. In-memory storage (upgradeable to Redis).
"""

import asyncio
import json
import random
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime, timedelta
import math
from functools import lru_cache


# ── Statistical Helper Functions (no scipy dependency) ────────────────────────
def _normal_cdf(x: float) -> float:
    """Approximate cumulative distribution function of standard normal distribution."""
    # Using error function approximation (Abramowitz and Stegun)
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911

    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)

    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t * math.exp(-x * x))

    return 0.5 * (1.0 + sign * y)


def _t_statistic(samples_a: List[float], samples_b: List[float]) -> Tuple[float, float]:
    """
    Compute Welch's t-statistic and approximate p-value.

    Returns:
        (t_statistic, p_value_two_tailed)
    """
    n_a = len(samples_a)
    n_b = len(samples_b)

    if n_a < 2 or n_b < 2:
        return 0.0, 1.0

    mean_a = sum(samples_a) / n_a
    mean_b = sum(samples_b) / n_b

    var_a = sum((x - mean_a) ** 2 for x in samples_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in samples_b) / (n_b - 1)

    se = math.sqrt(var_a / n_a + var_b / n_b)

    if se == 0:
        se = 1e-10

    t = (mean_a - mean_b) / se

    # Approximate degrees of freedom (Welch-Satterthwaite equation)
    numerator = (var_a / n_a + var_b / n_b) ** 2
    denominator = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)

    if denominator == 0:
        df = min(n_a, n_b) - 1
    else:
        df = numerator / denominator

    # Convert t-statistic to p-value using normal approximation for large df
    if df > 30:
        # Use normal approximation
        p_value = 2 * (1 - _normal_cdf(abs(t)))
    else:
        # Conservative estimate for small df
        p_value = min(1.0, 2 * (1 - _normal_cdf(abs(t) * 0.95)))

    return t, p_value


class Variant(str, Enum):
    """A/B test variants."""
    A = "variant_a"
    B = "variant_b"


@dataclass
class VariantConfig:
    """Configuration overrides for a test variant."""
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    speed: Optional[float] = None
    # Can extend: voice_id, emotion, etc.


@dataclass
class ExperimentConfig:
    """Configuration for an A/B test experiment."""
    name: str  # e.g., "speed_test", "prompt_length_test"
    description: str  # Human-readable description
    metric: str  # Metric to optimize: "transfer_rate", "grade_score", "latency_p95"
    variant_a: VariantConfig
    variant_b: VariantConfig
    min_samples_per_variant: int = 20
    significance_level: float = 0.05  # p < 0.05 for winner declaration
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


@dataclass
class CallResult:
    """Result from a single call."""
    call_id: str
    variant: Variant
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Grading metrics (from call_grader)
    grade_score: float = 0.0  # 0-100
    transfer_attempted: bool = False
    transfer_completed: bool = False

    # Performance metrics
    latency_p95_ms: float = 0.0
    latency_avg_ms: float = 0.0
    total_turns: int = 0
    duration_seconds: float = 0.0
    cost_usd: float = 0.0


@dataclass
class ExperimentResult:
    """Aggregated results for one variant in an experiment."""
    variant: Variant
    sample_count: int = 0

    # Metrics (aggregated)
    grade_scores: List[float] = field(default_factory=list)
    transfer_rates: List[bool] = field(default_factory=list)
    latencies_p95: List[float] = field(default_factory=list)
    latencies_avg: List[float] = field(default_factory=list)
    turn_counts: List[int] = field(default_factory=list)
    duration_seconds: List[float] = field(default_factory=list)
    costs: List[float] = field(default_factory=list)

    # Computed statistics
    grade_score_mean: float = 0.0
    grade_score_std: float = 0.0
    transfer_rate: float = 0.0  # % of calls that transferred
    latency_p95_mean: float = 0.0
    latency_avg_mean: float = 0.0
    avg_turns: float = 0.0
    avg_duration: float = 0.0
    total_cost: float = 0.0

    def update_stats(self):
        """Recalculate aggregated statistics."""
        self.sample_count = len(self.grade_scores)

        if self.grade_scores:
            self.grade_score_mean = sum(self.grade_scores) / len(self.grade_scores)
            if len(self.grade_scores) > 1:
                variance = sum((x - self.grade_score_mean) ** 2 for x in self.grade_scores) / (len(self.grade_scores) - 1)
                self.grade_score_std = math.sqrt(variance)
            else:
                self.grade_score_std = 0.0

        if self.transfer_rates:
            self.transfer_rate = sum(self.transfer_rates) / len(self.transfer_rates)

        if self.latencies_p95:
            self.latency_p95_mean = sum(self.latencies_p95) / len(self.latencies_p95)

        if self.latencies_avg:
            self.latency_avg_mean = sum(self.latencies_avg) / len(self.latencies_avg)

        if self.turn_counts:
            self.avg_turns = sum(self.turn_counts) / len(self.turn_counts)

        if self.duration_seconds:
            self.avg_duration = sum(self.duration_seconds) / len(self.duration_seconds)

        if self.costs:
            self.total_cost = sum(self.costs)


@dataclass
class WinnerResult:
    """Result of statistical significance test."""
    has_winner: bool
    winner: Optional[Variant] = None
    p_value: float = 1.0
    test_statistic: float = 0.0
    confidence: float = 0.0  # Winner likelihood (0-1)
    details: Dict[str, Any] = field(default_factory=dict)


class ABTestManager:
    """
    Manages A/B test experiments end-to-end.

    Thread-safe with per-experiment locks.
    In-memory storage (upgrade to Redis for persistence).
    """

    def __init__(self):
        self._experiments: Dict[str, ExperimentConfig] = {}
        self._results: Dict[str, Dict[str, List[CallResult]]] = {}  # {exp_name: {variant: [results]}}
        self._call_variant_map: Dict[str, Tuple[str, Variant]] = {}  # {call_id: (exp_name, variant)}
        self._locks: Dict[str, asyncio.Lock] = {}  # Per-experiment locks

    async def _get_lock(self, exp_name: str) -> asyncio.Lock:
        """Get or create a lock for an experiment."""
        if exp_name not in self._locks:
            self._locks[exp_name] = asyncio.Lock()
        return self._locks[exp_name]

    async def create_experiment(self, config: ExperimentConfig) -> bool:
        """
        Create a new experiment.

        Args:
            config: ExperimentConfig with name, variants, and metric

        Returns:
            True if created, False if already exists
        """
        if config.name in self._experiments:
            return False

        lock = await self._get_lock(config.name)
        async with lock:
            config.started_at = datetime.utcnow()
            self._experiments[config.name] = config
            self._results[config.name] = {
                Variant.A.value: [],
                Variant.B.value: [],
            }
            return True

    async def assign_variant(self, call_id: str, experiment_name: str) -> Optional[Variant]:
        """
        Assign a call to variant A or B (50/50 random).

        Returns:
            Variant.A or Variant.B, or None if experiment doesn't exist
        """
        if experiment_name not in self._experiments:
            return None

        lock = await self._get_lock(experiment_name)
        async with lock:
            if call_id in self._call_variant_map:
                # Already assigned
                _, variant = self._call_variant_map[call_id]
                return variant

            # Assign randomly (50/50)
            variant = random.choice([Variant.A, Variant.B])
            self._call_variant_map[call_id] = (experiment_name, variant)
            return variant

    async def record_result(
        self,
        call_id: str,
        experiment_name: str,
        grade_score: float,
        transfer_attempted: bool = False,
        transfer_completed: bool = False,
        latency_p95_ms: float = 0.0,
        latency_avg_ms: float = 0.0,
        total_turns: int = 0,
        duration_seconds: float = 0.0,
        cost_usd: float = 0.0,
    ) -> bool:
        """
        Record the result of a call in an experiment.

        Args:
            call_id: Call identifier
            experiment_name: Name of the experiment
            grade_score: Score from 0-100 (higher is better)
            transfer_attempted: Whether transfer was attempted
            transfer_completed: Whether transfer was successful
            latency_p95_ms: 95th percentile latency
            latency_avg_ms: Average latency
            total_turns: Number of conversation turns
            duration_seconds: Total call duration
            cost_usd: Cost of the call

        Returns:
            True if recorded, False if not found or already recorded
        """
        if call_id not in self._call_variant_map:
            return False

        exp_name, variant = self._call_variant_map[call_id]

        if exp_name != experiment_name:
            return False

        lock = await self._get_lock(experiment_name)
        async with lock:
            # Check if already recorded
            results_list = self._results[experiment_name][variant.value]
            if any(r.call_id == call_id for r in results_list):
                return False  # Already recorded

            result = CallResult(
                call_id=call_id,
                variant=variant,
                grade_score=grade_score,
                transfer_attempted=transfer_attempted,
                transfer_completed=transfer_completed,
                latency_p95_ms=latency_p95_ms,
                latency_avg_ms=latency_avg_ms,
                total_turns=total_turns,
                duration_seconds=duration_seconds,
                cost_usd=cost_usd,
            )
            results_list.append(result)
            return True

    async def get_experiment_status(self, experiment_name: str) -> Optional[Dict[str, Any]]:
        """
        Get current status and results for an experiment.

        Returns:
            Dict with variant_a, variant_b results, and winner info, or None if not found
        """
        if experiment_name not in self._experiments:
            return None

        lock = await self._get_lock(experiment_name)
        async with lock:
            exp_config = self._experiments[experiment_name]

            # Aggregate results for both variants
            results_a = self._aggregate_results(
                self._results[experiment_name][Variant.A.value]
            )
            results_b = self._aggregate_results(
                self._results[experiment_name][Variant.B.value]
            )

            # Check for winner
            winner_result = await self._compute_winner(
                exp_config,
                results_a,
                results_b,
            )

            return {
                "name": experiment_name,
                "description": exp_config.description,
                "metric": exp_config.metric,
                "status": self._get_status(exp_config, results_a, results_b),
                "variant_a": {
                    "config": asdict(exp_config.variant_a),
                    "results": self._serialize_results(results_a),
                },
                "variant_b": {
                    "config": asdict(exp_config.variant_b),
                    "results": self._serialize_results(results_b),
                },
                "winner": {
                    "has_winner": winner_result.has_winner,
                    "winner": winner_result.winner.value if winner_result.winner else None,
                    "p_value": round(winner_result.p_value, 6),
                    "confidence": round(winner_result.confidence, 3),
                    "details": winner_result.details,
                },
                "started_at": exp_config.started_at.isoformat() if exp_config.started_at else None,
                "ended_at": exp_config.ended_at.isoformat() if exp_config.ended_at else None,
            }

    def _aggregate_results(self, results_list: List[CallResult]) -> ExperimentResult:
        """Aggregate results from multiple calls."""
        agg = ExperimentResult(
            variant=Variant.A if not results_list else results_list[0].variant
        )

        for result in results_list:
            agg.grade_scores.append(result.grade_score)
            agg.transfer_rates.append(result.transfer_completed)
            agg.latencies_p95.append(result.latency_p95_ms)
            agg.latencies_avg.append(result.latency_avg_ms)
            agg.turn_counts.append(result.total_turns)
            agg.duration_seconds.append(result.duration_seconds)
            agg.costs.append(result.cost_usd)

        agg.update_stats()
        return agg

    async def _compute_winner(
        self,
        exp_config: ExperimentConfig,
        results_a: ExperimentResult,
        results_b: ExperimentResult,
    ) -> WinnerResult:
        """
        Compute statistical significance and identify winner.

        Tests based on metric:
        - transfer_rate: z-test for proportions
        - grade_score: t-test for independent samples
        - latency_*: t-test (lower is better)
        """
        min_n = exp_config.min_samples_per_variant

        # Not enough samples
        if results_a.sample_count < min_n or results_b.sample_count < min_n:
            return WinnerResult(
                has_winner=False,
                details={
                    "reason": f"Insufficient samples (need {min_n} per variant)",
                    "variant_a_n": results_a.sample_count,
                    "variant_b_n": results_b.sample_count,
                }
            )

        metric = exp_config.metric

        # Transfer rate: z-test for proportions
        if metric == "transfer_rate":
            p_a = results_a.transfer_rate
            p_b = results_b.transfer_rate
            n_a = results_a.sample_count
            n_b = results_b.sample_count

            winner, p_val, confidence, details = self._z_test_proportions(
                p_a, n_a, p_b, n_b,
                variant_a_label="A",
                variant_b_label="B",
                higher_is_better=True,
            )

        # Grade score: t-test (higher is better)
        elif metric == "grade_score":
            winner, p_val, confidence, details = self._t_test_means(
                results_a.grade_scores,
                results_b.grade_scores,
                higher_is_better=True,
            )

        # Latency metrics: t-test (lower is better)
        elif "latency" in metric:
            if metric == "latency_p95":
                samples_a = results_a.latencies_p95
                samples_b = results_b.latencies_p95
            else:  # latency_avg
                samples_a = results_a.latencies_avg
                samples_b = results_b.latencies_avg

            winner, p_val, confidence, details = self._t_test_means(
                samples_a,
                samples_b,
                higher_is_better=False,
            )

        else:
            # Unknown metric
            return WinnerResult(has_winner=False)

        # Declare winner if p < significance level
        has_winner = p_val < exp_config.significance_level

        return WinnerResult(
            has_winner=has_winner,
            winner=winner if has_winner else None,
            p_value=p_val,
            confidence=confidence,
            details=details,
        )

    def _z_test_proportions(
        self,
        p_a: float,
        n_a: int,
        p_b: float,
        n_b: int,
        variant_a_label: str = "A",
        variant_b_label: str = "B",
        higher_is_better: bool = True,
    ) -> Tuple[Optional[Variant], float, float, Dict]:
        """
        Two-proportion z-test.

        Returns:
            (winner_variant, p_value, confidence, details_dict)
        """
        # Pooled proportion
        p_pool = (p_a * n_a + p_b * n_b) / (n_a + n_b)
        se = math.sqrt(p_pool * (1 - p_pool) * (1/n_a + 1/n_b))

        if se == 0:
            se = 1e-10

        # Z statistic
        z = (p_a - p_b) / se

        # Two-tailed p-value
        p_value = 2 * (1 - stats.norm.cdf(abs(z)))

        # Determine winner
        if p_a > p_b:
            winner = Variant.A if higher_is_better else Variant.B
        elif p_b > p_a:
            winner = Variant.B if higher_is_better else Variant.A
        else:
            winner = None

        # Confidence = 1 - p_value
        confidence = max(0, 1 - p_value)

        details = {
            "test": "z-test (two proportions)",
            "variant_a_rate": round(p_a, 4),
            "variant_b_rate": round(p_b, 4),
            "z_statistic": round(z, 3),
            "difference": round(p_a - p_b, 4),
        }

        return winner, p_value, confidence, details

    def _t_test_means(
        self,
        samples_a: List[float],
        samples_b: List[float],
        higher_is_better: bool = True,
    ) -> Tuple[Optional[Variant], float, float, Dict]:
        """
        Two-sample t-test (independent samples).

        Returns:
            (winner_variant, p_value, confidence, details_dict)
        """
        if len(samples_a) < 2 or len(samples_b) < 2:
            return None, 1.0, 0.0, {"error": "Insufficient samples for t-test"}

        mean_a = sum(samples_a) / len(samples_a)
        mean_b = sum(samples_b) / len(samples_b)

        # Welch's t-test (doesn't assume equal variances)
        t_stat, p_value = _t_statistic(samples_a, samples_b)

        # Determine winner
        if mean_a > mean_b:
            winner = Variant.A if higher_is_better else Variant.B
        elif mean_b > mean_a:
            winner = Variant.B if higher_is_better else Variant.A
        else:
            winner = None

        confidence = max(0, 1 - p_value)

        std_a = math.sqrt(sum((x - mean_a) ** 2 for x in samples_a) / max(len(samples_a) - 1, 1)) if len(samples_a) > 1 else 0
        std_b = math.sqrt(sum((x - mean_b) ** 2 for x in samples_b) / max(len(samples_b) - 1, 1)) if len(samples_b) > 1 else 0

        details = {
            "test": "Welch's t-test (two samples)",
            "variant_a_mean": round(mean_a, 3),
            "variant_b_mean": round(mean_b, 3),
            "variant_a_std": round(std_a, 3),
            "variant_b_std": round(std_b, 3),
            "t_statistic": round(t_stat, 3),
            "difference": round(mean_a - mean_b, 3),
        }

        return winner, p_value, confidence, details

    def _get_status(
        self,
        exp_config: ExperimentConfig,
        results_a: ExperimentResult,
        results_b: ExperimentResult,
    ) -> str:
        """Get human-readable status of an experiment."""
        min_n = exp_config.min_samples_per_variant

        if results_a.sample_count == 0 and results_b.sample_count == 0:
            return "pending"
        elif results_a.sample_count < min_n or results_b.sample_count < min_n:
            return "running"
        else:
            return "complete"

    def _serialize_results(self, results: ExperimentResult) -> Dict[str, Any]:
        """Serialize experiment results to JSON-safe dict."""
        return {
            "sample_count": results.sample_count,
            "grade_score_mean": round(results.grade_score_mean, 2),
            "grade_score_std": round(results.grade_score_std, 2),
            "transfer_rate": round(results.transfer_rate, 4),
            "latency_p95_mean_ms": round(results.latency_p95_mean, 1),
            "latency_avg_mean_ms": round(results.latency_avg_mean, 1),
            "avg_turns": round(results.avg_turns, 1),
            "avg_duration_seconds": round(results.avg_duration, 1),
            "total_cost_usd": round(results.total_cost, 4),
        }

    async def list_experiments(self) -> List[Dict[str, Any]]:
        """List all experiments with their current status."""
        results = []
        for exp_name in self._experiments.keys():
            status = await self.get_experiment_status(exp_name)
            if status:
                results.append(status)
        return results

    async def stop_experiment(self, experiment_name: str) -> bool:
        """Stop an experiment and mark it as complete."""
        if experiment_name not in self._experiments:
            return False

        lock = await self._get_lock(experiment_name)
        async with lock:
            self._experiments[experiment_name].ended_at = datetime.utcnow()
            return True

    async def delete_experiment(self, experiment_name: str) -> bool:
        """Delete an experiment and all its results."""
        if experiment_name not in self._experiments:
            return False

        lock = await self._get_lock(experiment_name)
        async with lock:
            del self._experiments[experiment_name]
            del self._results[experiment_name]

            # Clean up call variant map
            self._call_variant_map = {
                call_id: (exp, var)
                for call_id, (exp, var) in self._call_variant_map.items()
                if exp != experiment_name
            }
            return True


# ── Pre-built Experiments ────────────────────────────────────────────────────

def create_speed_test() -> ExperimentConfig:
    """
    Test speaking speed: 0.97x (current) vs 1.0x (normal speed).

    Hypothesis: Slightly slower speech increases comprehension and trust.
    """
    return ExperimentConfig(
        name="speed_test",
        description="Speaking speed test: 0.97x (slower) vs 1.0x (normal)",
        metric="transfer_rate",
        variant_a=VariantConfig(speed=0.97),
        variant_b=VariantConfig(speed=1.0),
    )


def create_prompt_length_test() -> ExperimentConfig:
    """
    Test prompt length: current vs 30% shorter.

    Hypothesis: Shorter prompts reduce token overhead and may improve latency.
    Current prompt is approximately 1000 tokens; variant B is ~700 tokens.
    """
    # These are simplified examples; in production, you'd load actual prompts
    return ExperimentConfig(
        name="prompt_length_test",
        description="Prompt length test: full vs 30% shorter",
        metric="grade_score",
        variant_a=VariantConfig(
            system_prompt=None,  # Use default (full)
        ),
        variant_b=VariantConfig(
            system_prompt=None,  # Use shortened version
        ),
    )


def create_temperature_test() -> ExperimentConfig:
    """
    Test temperature: 0.7 (current) vs 0.8 (more creative).

    Hypothesis: Slightly higher temperature may lead to more natural responses
    while maintaining consistency.
    """
    return ExperimentConfig(
        name="temperature_test",
        description="Temperature test: 0.7 (consistent) vs 0.8 (creative)",
        metric="transfer_rate",
        variant_a=VariantConfig(temperature=0.7),
        variant_b=VariantConfig(temperature=0.8),
    )


def create_max_tokens_test() -> ExperimentConfig:
    """
    Test max tokens: 40 (current/concise) vs 50 (slightly longer).

    Hypothesis: Slightly longer responses may provide better context
    without sacrificing latency significantly.
    """
    return ExperimentConfig(
        name="max_tokens_test",
        description="Max tokens test: 40 (concise) vs 50 (slightly longer)",
        metric="grade_score",
        variant_a=VariantConfig(max_tokens=40),
        variant_b=VariantConfig(max_tokens=50),
    )


# Global singleton instance
_ab_test_manager: Optional[ABTestManager] = None


async def get_ab_test_manager() -> ABTestManager:
    """Get or create the global A/B test manager."""
    global _ab_test_manager
    if _ab_test_manager is None:
        _ab_test_manager = ABTestManager()
    return _ab_test_manager


async def initialize_default_experiments():
    """Initialize the pre-built experiments."""
    manager = await get_ab_test_manager()

    # Create all default experiments
    for config in [
        create_speed_test(),
        create_prompt_length_test(),
        create_temperature_test(),
        create_max_tokens_test(),
    ]:
        await manager.create_experiment(config)

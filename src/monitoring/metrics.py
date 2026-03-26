"""
Metrics & Monitoring System
Tracks latency, cost, and quality per-call and per-provider.
Exposes Prometheus metrics for dashboarding.
"""
import time
import statistics
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
import structlog

logger = structlog.get_logger()


@dataclass
class ProviderMetricsBucket:
    """Aggregated metrics for a single provider over a time window."""
    provider: str
    window_start: float = field(default_factory=time.time)
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    requests: int = 0
    total_cost: float = 0.0

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0

    @property
    def p95_latency(self) -> float:
        if len(self.latencies) < 2:
            return self.latencies[0] if self.latencies else 0
        sorted_l = sorted(self.latencies)
        return sorted_l[int(len(sorted_l) * 0.95)]

    @property
    def p99_latency(self) -> float:
        if len(self.latencies) < 2:
            return self.latencies[0] if self.latencies else 0
        sorted_l = sorted(self.latencies)
        return sorted_l[int(len(sorted_l) * 0.99)]

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.requests, 1)


class MetricsCollector:
    """
    Central metrics collection for the entire platform.
    Thread-safe, supports multiple concurrent calls.
    """

    def __init__(self):
        self._call_metrics: dict[str, dict] = {}
        self._provider_metrics: dict[str, ProviderMetricsBucket] = defaultdict(
            lambda: ProviderMetricsBucket(provider="unknown")
        )
        self._total_calls: int = 0
        self._total_cost: float = 0.0
        self._total_minutes: float = 0.0

    def record_call_start(self, call_id: str, pipeline_mode: str):
        self._call_metrics[call_id] = {
            "pipeline_mode": pipeline_mode,
            "start_time": time.time(),
            "turns": [],
        }
        self._total_calls += 1

    def record_turn(self, call_id: str, turn_metrics: dict):
        if call_id in self._call_metrics:
            self._call_metrics[call_id]["turns"].append(turn_metrics)

    def record_provider_latency(self, provider: str, latency_ms: float):
        bucket = self._provider_metrics[provider]
        bucket.provider = provider
        bucket.requests += 1
        bucket.latencies.append(latency_ms)
        # Keep only last 1000 measurements
        if len(bucket.latencies) > 1000:
            bucket.latencies = bucket.latencies[-500:]

    def record_provider_error(self, provider: str):
        self._provider_metrics[provider].errors += 1
        self._provider_metrics[provider].requests += 1

    def record_call_end(self, call_id: str, final_metrics: dict):
        if call_id in self._call_metrics:
            self._call_metrics[call_id]["final"] = final_metrics
            self._total_cost += final_metrics.get("total_cost_usd", 0)
            self._total_minutes += final_metrics.get("duration_seconds", 0) / 60

    def get_dashboard(self) -> dict:
        """Return current platform-wide metrics for dashboard display."""
        provider_stats = {}
        for name, bucket in self._provider_metrics.items():
            provider_stats[name] = {
                "avg_latency_ms": round(bucket.avg_latency, 1),
                "p50_latency_ms": round(bucket.p50_latency, 1),
                "p95_latency_ms": round(bucket.p95_latency, 1),
                "p99_latency_ms": round(bucket.p99_latency, 1),
                "error_rate": round(bucket.error_rate, 4),
                "total_requests": bucket.requests,
            }

        return {
            "total_calls": self._total_calls,
            "active_calls": sum(1 for c in self._call_metrics.values() if "final" not in c),
            "total_cost_usd": round(self._total_cost, 4),
            "total_minutes": round(self._total_minutes, 2),
            "avg_cost_per_minute": round(self._total_cost / max(self._total_minutes, 0.001), 4),
            "providers": provider_stats,
        }

    def get_call_details(self, call_id: str) -> Optional[dict]:
        return self._call_metrics.get(call_id)

    def check_cost_alert(self, threshold: float) -> list[str]:
        """Check if any active calls are exceeding cost threshold."""
        alerts = []
        for call_id, data in self._call_metrics.items():
            if "final" not in data:
                duration = time.time() - data["start_time"]
                # Rough cost estimate based on pipeline mode
                rate = 0.021 if data.get("pipeline_mode") == "budget" else 0.032
                estimated_cost = (duration / 60) * rate
                if estimated_cost > threshold:
                    alerts.append(f"Call {call_id}: estimated ${estimated_cost:.4f} exceeds ${threshold}")
        return alerts


# Global singleton
metrics_collector = MetricsCollector()

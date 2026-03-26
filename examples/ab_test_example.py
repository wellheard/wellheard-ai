#!/usr/bin/env python3
"""
Example: Running A/B Tests End-to-End

This demonstrates:
1. Creating an experiment
2. Simulating calls with variant assignment
3. Recording results
4. Checking for statistical significance
5. Declaring a winner
"""

import asyncio
import random
from typing import Tuple

# Add parent directory to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ab_testing import (
    ABTestManager, ExperimentConfig, VariantConfig,
    get_ab_test_manager, initialize_default_experiments,
)


async def main():
    """Run a complete A/B test simulation."""

    print("=" * 70)
    print("WellHeard A/B Testing Framework — Complete Example")
    print("=" * 70)

    manager = await get_ab_test_manager()

    # ── Part 1: Initialize Default Experiments ────────────────────────────
    print("\n[1] Initializing default experiments...")
    await initialize_default_experiments()
    experiments = await manager.list_experiments()
    print(f"    Created {len(experiments)} default experiments:")
    for exp in experiments:
        print(f"      • {exp['name']}: {exp['description']}")

    # ── Part 2: Create a Custom Experiment ─────────────────────────────────
    print("\n[2] Creating custom experiment: 'greeting_test'...")
    greeting_config = ExperimentConfig(
        name="greeting_test",
        description="Testing greeting: 'Hey, how's it going?' vs 'Hi, can you hear me?'",
        metric="transfer_rate",
        variant_a=VariantConfig(),  # Control (no override, use default)
        variant_b=VariantConfig(temperature=0.75),  # Slightly warmer
        min_samples_per_variant=10,  # Lower for demo
    )
    await manager.create_experiment(greeting_config)
    print(f"    ✓ Experiment 'greeting_test' created")

    # ── Part 3: Simulate Calls with Variant Assignment ────────────────────
    print("\n[3] Simulating 30 calls with variant assignment...")
    call_results = []

    for i in range(30):
        call_id = f"sim_call_{i:03d}"

        # Assign variant
        variant = await manager.assign_variant(call_id, "greeting_test")
        print(f"    Call {i+1:2d}: {call_id} → Variant {variant.value}")

        # Simulate call metrics (variant_b performs slightly better)
        if variant.value == "variant_a":
            grade = 70 + random.randint(-10, 15)  # Mean ~75
            transferred = random.random() < 0.65  # 65% transfer rate
        else:
            grade = 75 + random.randint(-10, 15)  # Mean ~80
            transferred = random.random() < 0.75  # 75% transfer rate

        call_results.append({
            "call_id": call_id,
            "variant": variant.value,
            "grade": grade,
            "transferred": transferred,
        })

    # ── Part 4: Record Results ────────────────────────────────────────────
    print("\n[4] Recording results for all calls...")
    for result in call_results:
        await manager.record_result(
            call_id=result["call_id"],
            experiment_name="greeting_test",
            grade_score=result["grade"],
            transfer_attempted=True,
            transfer_completed=result["transferred"],
            latency_p95_ms=400 + random.randint(-100, 100),
            latency_avg_ms=300 + random.randint(-80, 80),
            total_turns=5 + random.randint(-1, 3),
            duration_seconds=100 + random.randint(-20, 40),
            cost_usd=0.35,
        )
    print(f"    ✓ Recorded {len(call_results)} call results")

    # ── Part 5: Check Status ──────────────────────────────────────────────
    print("\n[5] Checking experiment status...")
    status = await manager.get_experiment_status("greeting_test")

    print(f"\n    Experiment: {status['name']}")
    print(f"    Status: {status['status']}")

    # Variant A
    print(f"\n    Variant A Results:")
    res_a = status["variant_a"]["results"]
    print(f"      Samples: {res_a['sample_count']}")
    print(f"      Transfer Rate: {res_a['transfer_rate']:.1%}")
    print(f"      Avg Grade: {res_a['grade_score_mean']:.1f}")

    # Variant B
    print(f"\n    Variant B Results:")
    res_b = status["variant_b"]["results"]
    print(f"      Samples: {res_b['sample_count']}")
    print(f"      Transfer Rate: {res_b['transfer_rate']:.1%}")
    print(f"      Avg Grade: {res_b['grade_score_mean']:.1f}")

    # Winner
    winner_info = status["winner"]
    print(f"\n    Winner Declaration:")
    print(f"      Has Winner: {winner_info['has_winner']}")
    if winner_info['has_winner']:
        print(f"      Winner: {winner_info['winner']}")
        print(f"      P-value: {winner_info['p_value']:.4f}")
        print(f"      Confidence: {winner_info['confidence']:.1%}")
        print(f"      Details: {winner_info['details']}")
    else:
        print(f"      P-value: {winner_info['p_value']:.4f} (need p < 0.05)")
        print(f"      → Not yet statistically significant")

    # ── Part 6: Check Default Experiments ──────────────────────────────────
    print("\n[6] Status of pre-built experiments...")
    all_status = await manager.list_experiments()

    for exp in all_status:
        winner = exp["winner"]
        if winner["has_winner"]:
            winner_str = f"✓ {winner['winner']} (p={winner['p_value']:.3f})"
        else:
            n_a = exp["variant_a"]["results"]["sample_count"]
            n_b = exp["variant_b"]["results"]["sample_count"]
            winner_str = f"⧖ Running (n_a={n_a}, n_b={n_b})"

        print(f"    {exp['name']:25s} {exp['status']:10s} {winner_str}")

    # ── Part 7: Stop the Experiment ──────────────────────────────────────
    print("\n[7] Stopping the experiment...")
    await manager.stop_experiment("greeting_test")
    print(f"    ✓ Experiment 'greeting_test' stopped")

    print("\n" + "=" * 70)
    print("Example complete!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

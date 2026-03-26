"""
WellHeard AI — Iterative QA Round Runner
Runs up to 10 rounds of test calls with QA analysis and improvements.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("config/.env")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_scenarios import SCENARIOS, SDR_SYSTEM_PROMPT
from tests.test_call_runner import CallTestRunner


async def run_qa_round(runner: CallTestRunner, round_number: int,
                        system_prompt: str, pipeline: str = "budget",
                        scenarios=None) -> tuple:
    """Run one QA round: all scenarios + analysis."""
    scenarios = scenarios or SCENARIOS
    records = []

    print(f"\n{'='*60}")
    print(f"  ROUND {round_number} — {pipeline.upper()} PIPELINE")
    print(f"{'='*60}")

    for i, scenario in enumerate(scenarios):
        print(f"\n  [{i+1}/{len(scenarios)}] Running: {scenario.name}...")

        scenario.sdr_system_prompt = system_prompt

        record = await runner.run_scenario(scenario, round_number, pipeline)

        # Rate limit pause between QA call and analysis
        await asyncio.sleep(3)

        print(f"  Analyzing quality...")
        qa = await runner.qa_analyze(record, scenario)
        record.qa_analysis = qa
        record.qa_score = qa.get("overall_score", 0)
        record.outcome = qa.get("detected_outcome", "unknown")

        records.append(record)

        print(f"  Score: {record.qa_score}/100 | Outcome: {record.outcome}")
        if qa.get("improvements_needed"):
            for imp in qa.get("improvements_needed", [])[:2]:
                print(f"    -> {imp}")
        if qa.get("critical_issues"):
            for issue in qa["critical_issues"]:
                print(f"    !! {issue}")

        # Rate limit pause between scenarios
        if i < len(scenarios) - 1:
            await asyncio.sleep(5)

    avg_score = sum(r.qa_score for r in records) / max(len(records), 1)
    summary = {
        "round": round_number,
        "avg_score": round(avg_score, 1),
        "scores": {r.scenario_id: r.qa_score for r in records},
        "outcomes": {r.scenario_id: r.outcome for r in records},
        "all_improvements": [],
        "all_critical": [],
    }

    for r in records:
        qa = r.qa_analysis
        summary["all_improvements"].extend(qa.get("improvements_needed", []))
        summary["all_critical"].extend(qa.get("critical_issues", []))

    summary["all_improvements"] = list(set(summary["all_improvements"]))
    summary["all_critical"] = list(set(summary["all_critical"]))

    return records, summary


async def generate_improved_prompt(runner: CallTestRunner, current_prompt: str,
                                    round_summary: dict, round_number: int) -> str:
    """Use AI to improve the system prompt based on QA findings."""
    improvement_prompt = f"""You are an expert call center trainer. Based on QA results from round {round_number}, improve the SDR system prompt.

CURRENT SYSTEM PROMPT:
{current_prompt}

QA RESULTS (Round {round_number}):
- Average Score: {round_summary['avg_score']}/100
- Improvements Needed: {json.dumps(round_summary['all_improvements'], indent=2)}
- Critical Issues: {json.dumps(round_summary['all_critical'], indent=2)}

RULES:
1. Keep the same overall structure and goal
2. Address EVERY critical issue and improvement point
3. Add specific behavioral guidance for weak areas
4. Keep responses concise — 1-3 sentences max
5. The prompt should feel natural
6. Don't remove existing good guidance — only ADD or REFINE
7. Return ONLY the improved system prompt

Improved system prompt:"""

    improved = await runner._generate_response(
        messages=[
            {"role": "system", "content": "You are a call center training expert. Output ONLY the improved system prompt."},
            {"role": "user", "content": improvement_prompt}
        ],
        provider="gemini"
    )

    return improved if improved else current_prompt


async def main():
    runner = CallTestRunner()
    current_prompt = SDR_SYSTEM_PROMPT
    pipeline = "budget"

    all_summaries = []
    max_rounds = 10
    target_score = 90

    print("=" * 60)
    print("  WellHeard AI — Iterative QA Testing")
    print(f"  Pipeline: {pipeline}")
    print(f"  Scenarios: {len(SCENARIOS)}")
    print(f"  Max Rounds: {max_rounds}")
    print(f"  Target Score: {target_score}/100")
    print("=" * 60)

    for round_num in range(1, max_rounds + 1):
        records, summary = await run_qa_round(
            runner, round_num, current_prompt, pipeline
        )
        all_summaries.append(summary)

        results_file = runner.save_results(records, round_num)
        print(f"\n  Round {round_num} Average Score: {summary['avg_score']}/100")
        print(f"  Results saved to: {results_file}")

        if summary['avg_score'] >= target_score:
            print(f"\n  Target score reached! ({summary['avg_score']}/100 >= {target_score})")
            break

        if round_num < max_rounds:
            print(f"\n  Generating improvements for round {round_num + 1}...")
            current_prompt = await generate_improved_prompt(
                runner, current_prompt, summary, round_num
            )

            prompt_file = f"{runner.results_dir}/prompt_r{round_num + 1}.txt"
            with open(prompt_file, "w") as f:
                f.write(current_prompt)
            print(f"  Improved prompt saved to: {prompt_file}")

    print("\n" + "=" * 60)
    print("  FINAL QA SUMMARY")
    print("=" * 60)
    for s in all_summaries:
        print(f"  Round {s['round']}: {s['avg_score']}/100")

    print(f"\n  Generating call recordings for final round...")
    for record in records:
        wav_file = await runner.create_call_recording(record, pipeline)
        print(f"  {record.scenario_name}: {wav_file}")

    final_prompt_file = f"{runner.results_dir}/final_sdr_prompt.txt"
    with open(final_prompt_file, "w") as f:
        f.write(current_prompt)
    print(f"\n  Final optimized prompt saved to: {final_prompt_file}")


if __name__ == "__main__":
    asyncio.run(main())

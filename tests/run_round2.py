"""
WellHeard AI — Optimization Round 2
Fixes applied: stricter phase ordering, better objection handling.
Runs same 3 scenarios to measure improvement.
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

from tests.test_scenarios import SCENARIOS, SDR_SYSTEM_PROMPT, SDR_GREETING
from tests.test_call_runner import CallTestRunner, CallRecord

TARGET_SCENARIOS = ["quick_qualifier", "not_interested", "already_insured"]


async def main():
    runner = CallTestRunner()
    results_dir = "test_results_round2"
    runner.results_dir = results_dir
    os.makedirs(results_dir, exist_ok=True)

    scenarios = [s for s in SCENARIOS if s.scenario_id in TARGET_SCENARIOS]

    print(f"\n{'='*65}")
    print(f"  WELLHEARD AI — Optimization Round 2")
    print(f"  Fixes: Strict phase ordering + better objection handling")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}")

    records = []
    for i, scenario in enumerate(scenarios):
        print(f"\n  [{i+1}/{len(scenarios)}] {scenario.name}")

        sdr_prompt = SDR_SYSTEM_PROMPT.replace("{last_name}", scenario.persona.last_name)
        sdr_prompt = sdr_prompt.replace("{contact_name}", scenario.persona.name)
        scenario.sdr_system_prompt = sdr_prompt
        scenario.sdr_greeting = SDR_GREETING

        record = await runner.run_scenario(scenario, round_number=2, pipeline="budget")
        record.sdr_name = "Vicky"
        record.prospect_name = scenario.persona.name

        print(f"  Turns: {len(record.turns)} | Duration: {record.duration_seconds:.1f}s | SDR Latency: {record.avg_turn_latency_ms:.0f}ms")
        print(f"\n  Transcript:")
        for turn in record.turns:
            speaker = "SDR" if turn.speaker == "sdr" else "Prospect"
            text = turn.text[:120] + "..." if len(turn.text) > 120 else turn.text
            print(f"    {speaker}: {text}")

        await asyncio.sleep(8)

        print(f"\n  QA Analysis...")
        qa = await runner.qa_analyze(record, scenario)
        record.qa_analysis = qa
        record.qa_score = qa.get("overall_score", 0)
        record.outcome = qa.get("detected_outcome", "unknown")
        records.append(record)

        print(f"  Score: {record.qa_score}/100 | Outcome: {record.outcome}")
        for s in qa.get("strengths", [])[:2]:
            print(f"    + {s}")
        for imp in qa.get("improvements_needed", [])[:2]:
            print(f"    - {imp}")

        if i < len(scenarios) - 1:
            print(f"\n  Cooldown 10s...")
            await asyncio.sleep(10)

    # Compare round 1 vs round 2
    avg_score = sum(r.qa_score for r in records) / max(len(records), 1)
    r1_scores = {"quick_qualifier": 90, "already_insured": 60, "not_interested": 90}  # from round 1

    print(f"\n{'='*65}")
    print(f"  ROUND 2 RESULTS vs ROUND 1")
    print(f"{'='*65}")
    print(f"  {'Scenario':40s} | {'R1':>5s} | {'R2':>5s} | {'Delta':>6s} | Outcome")
    print(f"  {'─'*75}")
    for r in records:
        r1 = r1_scores.get(r.scenario_id, 0)
        delta = r.qa_score - r1
        sign = "+" if delta > 0 else ""
        status = "PASS" if r.qa_score >= 70 else "FAIL"
        print(f"  [{status}] {r.scenario_name:36s} | {r1:5.0f} | {r.qa_score:5.0f} | {sign}{delta:5.0f} | {r.outcome}")
    print(f"  {'─'*75}")
    r1_avg = sum(r1_scores.values()) / len(r1_scores)
    print(f"  Average:{'':33s} | {r1_avg:5.0f} | {avg_score:5.0f} | {'+' if avg_score > r1_avg else ''}{avg_score - r1_avg:5.0f}")

    runner.save_results(records, round_number=2)

    # Save transcripts
    transcript_file = f"{results_dir}/transcripts_r2.txt"
    with open(transcript_file, "w") as f:
        for r in records:
            f.write(f"\n{'='*65}\n")
            f.write(f"SCENARIO: {r.scenario_name} | Score: {r.qa_score}/100 | Outcome: {r.outcome}\n")
            f.write(f"{'='*65}\n")
            f.write(r.transcript + "\n")
    print(f"\n  Saved: {transcript_file}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    asyncio.run(main())

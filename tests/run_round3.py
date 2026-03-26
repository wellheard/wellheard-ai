"""
WellHeard AI — Round 3: Full Updated Script Test
Changes applied:
  1. New greeting: "Hey {name}, how've you been?" (Gong 6.6x data)
  2. Reframed identify: follow-up frame, not cold pitch
  3. Cost anchor in urgency: "$9,000 funeral costs"
  4. Upgraded FAQ rebuttals: already insured, can't afford, pricing
  5. Fixed transfer hold: 3 scripted lines + handoff, then STOP
  6. Voicemail/silent call instructions in prompt

Runs 5 key scenarios including voicemail and silence.
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

# 5 key scenarios covering all critical paths
TARGET_SCENARIOS = [
    "quick_qualifier",    # Happy path → transfer
    "already_insured",    # Objection → recover → transfer
    "not_interested",     # Firm no → clean exit
    "price_asker",        # Price question during transfer → deflect
    "voicemail",          # VM detection → fast exit
]


async def main():
    runner = CallTestRunner()
    results_dir = "test_results_round3"
    runner.results_dir = results_dir
    os.makedirs(results_dir, exist_ok=True)

    scenarios = [s for s in SCENARIOS if s.scenario_id in TARGET_SCENARIOS]

    print(f"\n{'='*70}")
    print(f"  WELLHEARD AI — Round 3: Full Updated Script")
    print(f"  Changes: New greeting + reframed identify + cost anchor +")
    print(f"           upgraded FAQ + fixed transfer hold + VM/silence rules")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")

    # Round 1 and 2 scores for comparison
    prev_scores = {
        "quick_qualifier": {"r1": 90, "r2": 90},
        "already_insured": {"r1": 60, "r2": 90},
        "not_interested": {"r1": 90, "r2": 90},
        "price_asker": {"r1": 40, "r2": None},
        "voicemail": {"r1": 60, "r2": None},
    }

    records = []
    for i, scenario in enumerate(scenarios):
        print(f"\n  [{i+1}/{len(scenarios)}] {scenario.name}")
        print(f"  Expected: {scenario.persona.expected_outcome}")

        # Substitute prospect details
        sdr_prompt = SDR_SYSTEM_PROMPT.replace("{last_name}", scenario.persona.last_name)
        sdr_prompt = sdr_prompt.replace("{contact_name}", scenario.persona.name)
        scenario.sdr_system_prompt = sdr_prompt
        scenario.sdr_greeting = SDR_GREETING.replace("{contact_name}", scenario.persona.name)

        record = await runner.run_scenario(scenario, round_number=3, pipeline="budget")
        record.sdr_name = "Vicky"
        record.prospect_name = scenario.persona.name

        print(f"  Turns: {len(record.turns)} | Duration: {record.duration_seconds:.1f}s | Latency: {record.avg_turn_latency_ms:.0f}ms")

        # Show transcript
        print(f"\n  Transcript:")
        for turn in record.turns:
            speaker = "SDR" if turn.speaker == "sdr" else "Prospect"
            text = turn.text[:130] + "..." if len(turn.text) > 130 else turn.text
            print(f"    {speaker}: {text}")

        # Wait for rate limit
        await asyncio.sleep(8)

        # QA analysis
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

    # ── Results comparison ────────────────────────────────────────────────
    avg_score = sum(r.qa_score for r in records) / max(len(records), 1)

    print(f"\n{'='*70}")
    print(f"  ROUND 3 RESULTS — Full Script Update")
    print(f"{'='*70}")
    print(f"  {'Scenario':40s} | {'R1':>5s} | {'R2':>5s} | {'R3':>5s} | {'Δ R2→R3':>7s} | Outcome")
    print(f"  {'─'*85}")
    for r in records:
        prev = prev_scores.get(r.scenario_id, {})
        r1 = prev.get("r1", None)
        r2 = prev.get("r2", None)
        r1_str = f"{r1:5.0f}" if r1 is not None else "  N/A"
        r2_str = f"{r2:5.0f}" if r2 is not None else "  N/A"
        if r2 is not None:
            delta = r.qa_score - r2
            delta_str = f"{'+'if delta>0 else ''}{delta:5.0f}"
        else:
            delta_str = "  NEW"
        status = "PASS" if r.qa_score >= 70 else "FAIL"
        print(f"  [{status}] {r.scenario_name:36s} | {r1_str} | {r2_str} | {r.qa_score:5.0f} | {delta_str} | {r.outcome}")
    print(f"  {'─'*85}")
    print(f"  Average R3: {avg_score:.1f}/100")

    # Check specific improvements
    print(f"\n  Key checks:")
    for r in records:
        transcript = r.transcript.lower()
        if r.scenario_id == "quick_qualifier":
            has_greeting = "how've you been" in transcript or "how have you been" in transcript
            has_lastname = "barragan" in transcript
            has_cost = "nine thousand" in transcript or "9000" in transcript or "9,000" in transcript
            has_handoff = "hand you over" in transcript or "agent on the line" in transcript
            print(f"    Quick Qualifier: greeting={'✓' if has_greeting else '✗'} lastname={'✓' if has_lastname else '✗'} cost_anchor={'✓' if has_cost else '✗'} handoff={'✓' if has_handoff else '✗'}")
            # Check if transfer hold stayed within bounds
            sdr_turns_after_transfer = 0
            transfer_started = False
            for turn in r.turns:
                if turn.speaker == "sdr" and "licensed agent standing by" in turn.text.lower():
                    transfer_started = True
                if transfer_started and turn.speaker == "sdr":
                    sdr_turns_after_transfer += 1
            print(f"    Transfer hold SDR turns: {sdr_turns_after_transfer} (target: ≤4)")

        elif r.scenario_id == "already_insured":
            has_different = "something a little different" in transcript or "different" in transcript
            has_recover = "qualify" in r.outcome or "transfer" in r.outcome
            print(f"    Already Insured: new_rebuttal={'✓' if has_different else '✗'} recovered_to_transfer={'✓' if has_recover else '✗'}")

        elif r.scenario_id == "price_asker":
            has_dollar = "dollar" in transcript or "a day" in transcript
            print(f"    Price Asker: gave_anchor={'✓' if has_dollar else '✗'}")

        elif r.scenario_id == "voicemail":
            print(f"    Voicemail: turns={len(r.turns)} (target: ≤4)")

    # Save results
    runner.save_results(records, round_number=3)

    transcript_file = f"{results_dir}/transcripts_r3.txt"
    with open(transcript_file, "w") as f:
        for r in records:
            f.write(f"\n{'='*70}\n")
            f.write(f"SCENARIO: {r.scenario_name} | Score: {r.qa_score}/100 | Outcome: {r.outcome}\n")
            f.write(f"{'='*70}\n")
            f.write(r.transcript + "\n")

    print(f"\n  Saved: {transcript_file}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())

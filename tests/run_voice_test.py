"""
WellHeard AI — Voice Quality Test Runner (v2)
Runs AI-to-AI test calls using the real Becky production script.
Uses cloned voices (Vicky/Ben) on Cartesia Sonic-3.
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


async def run_voice_test(sdr_voice: str, prospect_voice: str,
                          sdr_name: str, scenarios_subset=None):
    """Run a full test round with specific voice pairing using real script."""
    runner = CallTestRunner()
    pipeline = "quality"  # Cartesia Sonic 3

    scenarios = scenarios_subset or SCENARIOS
    records = []

    config_label = f"{sdr_name}_SDR"
    results_dir = f"test_results_voices/{config_label}"
    runner.results_dir = results_dir
    os.makedirs(results_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  VOICE TEST: {sdr_voice.upper()} (SDR) vs {prospect_voice.upper()} (Prospect)")
    print(f"  Pipeline: {pipeline} (Sonic-3)")
    print(f"  Script: Production Becky script")
    print(f"  Scenarios: {len(scenarios)}")
    print(f"{'='*60}")

    for i, scenario in enumerate(scenarios):
        print(f"\n  [{i+1}/{len(scenarios)}] Running: {scenario.name}...")

        # Use the real production SDR prompt with prospect-specific last_name
        sdr_prompt = SDR_SYSTEM_PROMPT.replace("{last_name}", scenario.persona.last_name)
        sdr_prompt = sdr_prompt.replace("{contact_name}", scenario.persona.name)
        scenario.sdr_system_prompt = sdr_prompt

        # Use production greeting (substitute contact name)
        scenario.sdr_greeting = SDR_GREETING.replace("{contact_name}", scenario.persona.name)

        # Use Groq LLM for text generation, Cartesia for TTS
        record = await runner.run_scenario(scenario, round_number=1, pipeline="budget")
        record.sdr_name = sdr_name
        record.prospect_name = scenario.persona.name

        # Rate limit pause
        await asyncio.sleep(3)

        # QA analysis
        print(f"  Analyzing quality...")
        qa = await runner.qa_analyze(record, scenario)
        record.qa_analysis = qa
        record.qa_score = qa.get("overall_score", 0)
        record.outcome = qa.get("detected_outcome", "unknown")
        records.append(record)

        print(f"  Score: {record.qa_score}/100 | Outcome: {record.outcome}")
        print(f"  Turns: {len(record.turns)}")
        if qa.get("improvements_needed"):
            for imp in qa.get("improvements_needed", [])[:2]:
                print(f"    -> {imp}")

        # Rate limit between scenarios
        if i < len(scenarios) - 1:
            await asyncio.sleep(4)

    # Summary
    avg_score = sum(r.qa_score for r in records) / max(len(records), 1)
    print(f"\n{'='*60}")
    print(f"  RESULTS: {sdr_voice.upper()} (SDR) vs {prospect_voice.upper()} (Prospect)")
    print(f"  Average Score: {avg_score:.1f}/100")
    print(f"{'='*60}")
    for r in records:
        turns = len(r.turns)
        print(f"  {r.scenario_name:40s} | {r.qa_score:3.0f}/100 | {turns:2d} turns | {r.outcome}")

    # Save results
    runner.save_results(records, round_number=1)

    # Generate audio recordings for key scenarios
    audio_scenarios = [r for r in records if r.scenario_id in
                       ("quick_qualifier", "warm_with_questions", "already_insured",
                        "not_interested", "price_asker")]

    if audio_scenarios:
        print(f"\n  Generating {len(audio_scenarios)} call recordings...")
        for record in audio_scenarios:
            try:
                wav_file = await runner.create_call_recording(
                    record, pipeline,
                    sdr_voice=sdr_voice,
                    prospect_voice=prospect_voice,
                )
                print(f"  {record.scenario_name}: {wav_file}")
            except Exception as e:
                print(f"  {record.scenario_name}: AUDIO ERROR - {e}")
            await asyncio.sleep(1)

    # Save transcripts
    transcript_file = f"{results_dir}/transcripts.txt"
    with open(transcript_file, "w") as f:
        for r in records:
            f.write(f"\n{'='*60}\n")
            f.write(f"SCENARIO: {r.scenario_name} | Score: {r.qa_score}/100 | Turns: {len(r.turns)}\n")
            f.write(f"Expected: {[s for s in SCENARIOS if s.scenario_id == r.scenario_id][0].persona.expected_outcome}\n")
            f.write(f"Detected: {r.outcome}\n")
            f.write(f"{'='*60}\n")
            f.write(r.transcript + "\n")
    print(f"\n  Transcripts saved to: {transcript_file}")

    return records, avg_score


async def main():
    print("=" * 60)
    print("  WellHeard AI — Voice Quality Testing v2")
    print("  Production Becky Script + Cloned Voices")
    print("  Cartesia Sonic-3 with Emotion Controls")
    print("=" * 60)

    # Run with Vicky as SDR (Becky role) and Ben as prospect
    records, score = await run_voice_test(
        sdr_voice="vicky",
        prospect_voice="ben",
        sdr_name="Vicky",
        scenarios_subset=SCENARIOS,
    )

    print(f"\n\n{'='*60}")
    print(f"  FINAL SCORE: {score:.1f}/100 across {len(records)} scenarios")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())

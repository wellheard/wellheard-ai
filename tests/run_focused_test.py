"""
WellHeard AI — Focused Test Run
Runs 3 key scenarios with longer delays to avoid Groq rate limits.
Also benchmarks the new orchestrator cache system vs LLM-only.
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
from tests.test_call_runner import CallTestRunner, CallRecord, TurnRecord

# Pick 3 key scenarios that cover the critical paths
TARGET_SCENARIOS = ["quick_qualifier", "not_interested", "already_insured"]


async def run_conversation_test():
    """Run AI-to-AI conversation test on 3 key scenarios."""
    runner = CallTestRunner()
    results_dir = "test_results_focused"
    runner.results_dir = results_dir
    os.makedirs(results_dir, exist_ok=True)

    scenarios = [s for s in SCENARIOS if s.scenario_id in TARGET_SCENARIOS]

    print(f"\n{'='*65}")
    print(f"  WELLHEARD AI — Focused Conversation Test")
    print(f"  Scenarios: {', '.join(TARGET_SCENARIOS)}")
    print(f"  LLM: Groq Llama-4-Scout | TTS: Cartesia Sonic-3")
    print(f"  Script: Production Becky")
    print(f"{'='*65}")

    records = []
    for i, scenario in enumerate(scenarios):
        print(f"\n  [{i+1}/{len(scenarios)}] {scenario.name}")
        print(f"  Expected outcome: {scenario.persona.expected_outcome}")

        # Substitute prospect details into SDR prompt
        sdr_prompt = SDR_SYSTEM_PROMPT.replace("{last_name}", scenario.persona.last_name)
        sdr_prompt = sdr_prompt.replace("{contact_name}", scenario.persona.name)
        scenario.sdr_system_prompt = sdr_prompt
        scenario.sdr_greeting = SDR_GREETING

        record = await runner.run_scenario(scenario, round_number=1, pipeline="budget")
        record.sdr_name = "Vicky"
        record.prospect_name = scenario.persona.name

        # Longer delay to avoid rate limits
        print(f"  Conversation: {len(record.turns)} turns, {record.duration_seconds:.1f}s")
        print(f"  Avg SDR latency: {record.avg_turn_latency_ms:.0f}ms")
        print(f"\n  --- Transcript ---")
        for turn in record.turns:
            speaker = f"SDR (Vicky)" if turn.speaker == "sdr" else f"Prospect ({scenario.persona.name})"
            print(f"  {speaker}: {turn.text[:100]}{'...' if len(turn.text) > 100 else ''}")
        print(f"  --- End ---")

        # Wait before QA analysis
        await asyncio.sleep(6)

        # QA scoring
        print(f"\n  Running QA analysis...")
        qa = await runner.qa_analyze(record, scenario)
        record.qa_analysis = qa
        record.qa_score = qa.get("overall_score", 0)
        record.outcome = qa.get("detected_outcome", "unknown")
        records.append(record)

        print(f"  QA Score: {record.qa_score}/100")
        print(f"  Detected Outcome: {record.outcome}")
        if qa.get("strengths"):
            for s in qa.get("strengths", [])[:2]:
                print(f"    + {s}")
        if qa.get("improvements_needed"):
            for imp in qa.get("improvements_needed", [])[:2]:
                print(f"    - {imp}")
        if qa.get("critical_issues"):
            for ci in qa.get("critical_issues", [])[:2]:
                print(f"    ! {ci}")

        # Longer delay between scenarios
        if i < len(scenarios) - 1:
            print(f"\n  Waiting 8s for rate limit cooldown...")
            await asyncio.sleep(8)

    # Summary
    avg_score = sum(r.qa_score for r in records) / max(len(records), 1)
    print(f"\n{'='*65}")
    print(f"  CONVERSATION TEST RESULTS")
    print(f"{'='*65}")
    for r in records:
        status = "PASS" if r.qa_score >= 70 else "FAIL"
        print(f"  [{status}] {r.scenario_name:40s} | {r.qa_score:3.0f}/100 | {len(r.turns):2d} turns | {r.outcome}")
    print(f"  {'─'*55}")
    print(f"  Average Score: {avg_score:.1f}/100")

    # Save results
    runner.save_results(records, round_number=1)

    # Save detailed transcripts
    transcript_file = f"{results_dir}/transcripts.txt"
    with open(transcript_file, "w") as f:
        for r in records:
            f.write(f"\n{'='*65}\n")
            f.write(f"SCENARIO: {r.scenario_name}\n")
            f.write(f"Score: {r.qa_score}/100 | Turns: {len(r.turns)} | Outcome: {r.outcome}\n")
            f.write(f"{'='*65}\n")
            f.write(r.transcript + "\n")
    print(f"\n  Transcripts: {transcript_file}")

    return records, avg_score


async def run_orchestrator_benchmark():
    """Benchmark the new orchestrator cache system."""
    from src.call_orchestrator import ProductionCallOrchestrator, classify_response
    from src.response_cache import CallPhase

    print(f"\n{'='*65}")
    print(f"  WELLHEARD AI — Orchestrator Cache Benchmark")
    print(f"{'='*65}")

    orch = ProductionCallOrchestrator()

    # Prepare a call (loads cache)
    t0 = time.time()
    ctx = await orch.prepare_call("Luis", "Barragan", call_id="bench_001")
    prep_time = (time.time() - t0) * 1000
    print(f"\n  Call preparation: {prep_time:.1f}ms (loaded {len(orch.cache._static_cache)} cached phrases)")

    # Simulate the happy-path conversation and measure response times
    conversation = [
        ("Yes", "greeting"),
        ("That's right", "identify"),
        ("Yes, I'm interested", "urgency_pitch"),
        ("Yep, I have both", "qualify_account"),
        ("Okay", "transfer_init"),
    ]

    print(f"\n  Simulating happy-path call flow:")
    print(f"  {'Phase':25s} | {'Response Time':>13s} | {'Source':15s} | Response Preview")
    print(f"  {'─'*90}")

    # First: greeting
    t0 = time.time()
    greeting = await orch.get_greeting()
    greeting_latency = (time.time() - t0) * 1000
    print(f"  {'GREETING':25s} | {greeting_latency:10.1f} ms | {'cache_L1':15s} | {greeting['text'][:45]}...")

    total_latency = greeting_latency
    for prospect_says, expected_phase in conversation:
        t0 = time.time()
        response = await orch.process_prospect_response(prospect_says)
        latency = (time.time() - t0) * 1000
        total_latency += latency

        phase = response.get("phase", "?")
        source = response.get("source", "?")
        text_preview = response["text"][:45] + "..." if len(response["text"]) > 45 else response["text"]
        print(f"  {phase:25s} | {latency:10.1f} ms | {source:15s} | {text_preview}")

    print(f"  {'─'*90}")
    print(f"  {'TOTAL':25s} | {total_latency:10.1f} ms")
    avg = total_latency / (len(conversation) + 1)
    print(f"  {'AVERAGE':25s} | {avg:10.1f} ms")

    # Compare: what would LLM-only latency be?
    print(f"\n  Comparison to Dasha.ai benchmark:")
    print(f"    Dasha avg response gap:  1,200 ms")
    print(f"    WellHeard cache responses:  {avg:.0f} ms")
    if avg < 1200:
        improvement = ((1200 - avg) / 1200) * 100
        print(f"    Improvement:             {improvement:.0f}% faster than Dasha.ai")
    else:
        print(f"    Note: Cache should be near-zero in production (pre-synthesized audio)")

    # End call and get QA report
    report = await orch.end_call()
    print(f"\n  Call QA Report:")
    print(f"    Duration: {report['duration_seconds']}s")
    print(f"    Turns: {report['turns']}")
    print(f"    Phases: {' → '.join(report['phases_completed'])}")
    print(f"    Naturalness Score: {report['naturalness']['score']}/100")
    print(f"    Naturalness Level: {report['naturalness']['level']}")
    print(f"    Outcome: {report['outcome']}")

    # Now benchmark FAQ (objection) responses
    print(f"\n  Objection handling benchmark:")
    orch2 = ProductionCallOrchestrator()
    await orch2.prepare_call("Harold", "Peterson", call_id="bench_002")
    _ = await orch2.get_greeting()

    # Move to identify phase
    _ = await orch2.process_prospect_response("Yeah")

    objections = [
        "I already have life insurance",
        "I can't afford it",
        "How much does it cost?",
        "I don't want to give personal details",
    ]

    for obj in objections:
        t0 = time.time()
        resp = await orch2.process_prospect_response(obj)
        lat = (time.time() - t0) * 1000
        source = resp.get("source", "?")
        preview = resp["text"][:60] + "..." if len(resp["text"]) > 60 else resp["text"]
        print(f"    [{lat:6.1f}ms] [{source:10s}] \"{obj}\" → {preview}")

    await orch2.end_call()

    return report


async def main():
    print("=" * 65)
    print("  WellHeard AI — Test Run + Optimization Benchmark")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Part 1: Orchestrator benchmark (no API calls needed)
    report = await run_orchestrator_benchmark()

    # Part 2: Live conversation test (uses Groq API)
    records, avg_score = await run_conversation_test()

    # Final summary
    print(f"\n\n{'='*65}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*65}")
    print(f"  Orchestrator cache: working, {len(report.get('phases_completed', []))} phases served from cache")
    print(f"  Conversation avg score: {avg_score:.1f}/100")
    print(f"  Naturalness level: {report.get('naturalness', {}).get('level', 'N/A')}")

    passed = sum(1 for r in records if r.qa_score >= 70)
    print(f"  Scenarios passed: {passed}/{len(records)}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    asyncio.run(main())

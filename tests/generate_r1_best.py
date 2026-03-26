"""Generate audio for the best recording from Round 1 (Quick Qualifier, scored 90)."""
import asyncio
import json
import os
import sys
from dotenv import load_dotenv

load_dotenv("config/.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_call_runner import CallTestRunner, CallRecord, TurnRecord


async def main():
    runner = CallTestRunner()
    results_dir = "test_results_focused"
    runner.results_dir = results_dir

    results_file = f"{results_dir}/round_1_results.json"
    if not os.path.exists(results_file):
        print(f"No results file at {results_file}")
        return

    with open(results_file) as f:
        data = json.load(f)

    # Get the quick_qualifier scenario (best from R1 at 90/100)
    for scenario_data in data["scenarios"]:
        if scenario_data["scenario_id"] != "quick_qualifier":
            continue

        sid = scenario_data["scenario_id"]
        name = scenario_data["name"]
        transcript_lines = scenario_data["transcript"].split("\n")

        record = CallRecord(
            scenario_id=sid,
            scenario_name=name,
            round_number=1,
            pipeline="quality",
            sdr_name="Vicky",
            prospect_name="Luis",
        )

        for line in transcript_lines:
            if line.startswith("SDR ("):
                text = line.split(": ", 1)[1] if ": " in line else ""
                if text:
                    record.turns.append(TurnRecord(speaker="sdr", text=text))
            elif line.startswith("Prospect ("):
                text = line.split(": ", 1)[1] if ": " in line else ""
                if text:
                    record.turns.append(TurnRecord(speaker="prospect", text=text))

        print(f"  {name}: {len(record.turns)} turns")
        wav_file = await runner.create_call_recording(
            record, "quality",
            sdr_voice="vicky",
            prospect_voice="ben",
        )
        print(f"  Saved: {wav_file}")
        break

    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())

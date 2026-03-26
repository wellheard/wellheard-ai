"""Generate audio recordings from Round 2 test transcripts."""
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
    results_dir = "test_results_round2"
    runner.results_dir = results_dir

    # Load round 2 results
    results_file = f"{results_dir}/round_2_results.json"
    if not os.path.exists(results_file):
        print(f"No results file found at {results_file}")
        return

    with open(results_file) as f:
        data = json.load(f)

    print(f"Generating audio for {len(data['scenarios'])} scenarios...")

    for scenario_data in data["scenarios"]:
        sid = scenario_data["scenario_id"]
        name = scenario_data["name"]
        transcript_lines = scenario_data["transcript"].split("\n")

        # Rebuild turns from transcript
        record = CallRecord(
            scenario_id=sid,
            scenario_name=name,
            round_number=2,
            pipeline="quality",
            sdr_name="Vicky",
            prospect_name=sid,
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

        print(f"\n  {name}: {len(record.turns)} turns")
        try:
            wav_file = await runner.create_call_recording(
                record, "quality",
                sdr_voice="vicky",
                prospect_voice="ben",
            )
            print(f"  Saved: {wav_file}")
        except Exception as e:
            print(f"  ERROR: {e}")
        await asyncio.sleep(2)

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())

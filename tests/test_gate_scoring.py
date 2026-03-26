"""
Test transfer gate scoring across 3 scenarios:
1. Qualified prospect → must score 95+ and approve
2. Silent call → must score < 15 and end_call
3. TV noise → must score < 40 and end_call or re_qualify
"""
import sys
sys.path.insert(0, "/sessions/gifted-vigilant-bohr/mnt/Crown Academy/wellheard-ai")

from src.transfer_gate import (
    TransferQualificationGate,
    CallContext,
    CallTranscriptTurn,
    TransferRecommendation,
)


def build_qualified_prospect() -> CallContext:
    """A clearly qualified prospect who said yes to everything."""
    return CallContext(
        call_id="test-qualified",
        transcript_turns=[
            CallTranscriptTurn(speaker="sdr", text="Hey Sarah, how've you been?", timestamp=0.0),
            CallTranscriptTurn(speaker="prospect", text="I'm doing good, who's this?", timestamp=1.2),
            CallTranscriptTurn(speaker="sdr", text="This is Vicky from Crown Benefits. You filled out a form about health insurance, remember that?", timestamp=2.5),
            CallTranscriptTurn(speaker="prospect", text="Oh yeah, I remember doing that", timestamp=5.0),
            CallTranscriptTurn(speaker="sdr", text="Great! So the reason I'm calling is we found plans in your area starting at just thirty-nine dollars a month. Do you currently have health coverage?", timestamp=6.5),
            CallTranscriptTurn(speaker="prospect", text="No I don't have anything right now, that's why I filled out that form", timestamp=10.0),
            CallTranscriptTurn(speaker="sdr", text="Perfect, so you'd be interested in getting a quote for those plans?", timestamp=13.0),
            CallTranscriptTurn(speaker="prospect", text="Yes absolutely, that sounds good to me", timestamp=15.0),
            CallTranscriptTurn(speaker="sdr", text="And do you have a checking or savings account? That's how most people get the best discounts.", timestamp=17.0),
            CallTranscriptTurn(speaker="prospect", text="Yes I have a checking account", timestamp=19.5),
            CallTranscriptTurn(speaker="sdr", text="Perfect. Let me connect you with a licensed agent who can walk you through the exact plans and pricing.", timestamp=21.0),
            CallTranscriptTurn(speaker="prospect", text="Sure, go ahead", timestamp=23.0),
        ],
        completed_phases=["identify", "urgency_pitch", "qualify_account"],
        phase_positive_signals={"identify": True, "urgency_pitch": True, "qualify_account": True},
        prospect_speech_seconds=18.0,
        prospect_total_seconds=25.0,
        avg_audio_rms=-22.0,
        audio_rms_variance=5.0,
        response_latencies_ms=[800, 650, 900, 750, 600, 500],
        turn_word_counts=[5, 4, 12, 6, 5, 2],
        call_duration_seconds=25.0,
        voicemail_detected=False,
        silence_detected=False,
    )


def build_silent_call() -> CallContext:
    """A completely silent call — no prospect speech at all."""
    return CallContext(
        call_id="test-silent",
        transcript_turns=[
            CallTranscriptTurn(speaker="sdr", text="Hey there, how've you been?", timestamp=0.0),
            CallTranscriptTurn(speaker="sdr", text="Hello? Can you hear me?", timestamp=5.0),
            CallTranscriptTurn(speaker="sdr", text="I'll try you another time.", timestamp=10.0),
        ],
        completed_phases=[],
        phase_positive_signals={},
        prospect_speech_seconds=0.0,
        prospect_total_seconds=12.0,
        avg_audio_rms=-55.0,
        audio_rms_variance=0.5,
        response_latencies_ms=[],
        turn_word_counts=[],
        call_duration_seconds=12.0,
        voicemail_detected=False,
        silence_detected=True,  # Hard fail signal
    )


def build_tv_noise() -> CallContext:
    """TV or radio noise — audio present but not conversational."""
    return CallContext(
        call_id="test-tv-noise",
        transcript_turns=[
            CallTranscriptTurn(speaker="sdr", text="Hey there, how've you been?", timestamp=0.0),
            # STT picks up TV audio as "prospect" speech
            CallTranscriptTurn(speaker="prospect", text="and now for the weather forecast today", timestamp=2.0),
            CallTranscriptTurn(speaker="sdr", text="Hi, is this Michael?", timestamp=5.0),
            CallTranscriptTurn(speaker="prospect", text="temperatures expected to reach the mid seventies", timestamp=7.0),
            CallTranscriptTurn(speaker="sdr", text="Hello? Can you hear me?", timestamp=10.0),
            CallTranscriptTurn(speaker="prospect", text="sponsored by your local toyota dealer", timestamp=12.0),
            CallTranscriptTurn(speaker="sdr", text="Okay I'll try again later.", timestamp=15.0),
            CallTranscriptTurn(speaker="prospect", text="up next the latest headlines from around the nation", timestamp=17.0),
        ],
        completed_phases=[],
        phase_positive_signals={},
        prospect_speech_seconds=8.0,
        prospect_total_seconds=18.0,
        avg_audio_rms=-18.0,      # Strong audio (TV is loud)
        audio_rms_variance=1.2,    # Low variance (constant stream)
        response_latencies_ms=[200, 180, 210, 190],  # Too fast — automated
        turn_word_counts=[7, 7, 6, 9],  # Suspiciously uniform word counts
        call_duration_seconds=18.0,
        voicemail_detected=False,
        silence_detected=False,
    )


def run_tests():
    gate = TransferQualificationGate()

    print("=" * 70)
    print("TRANSFER GATE SCORING TESTS")
    print("=" * 70)

    # Test 1: Qualified prospect
    print("\n--- TEST 1: Qualified Prospect ---")
    ctx = build_qualified_prospect()
    result = gate.evaluate(ctx)
    print(f"  Score:          {result.overall_score}/100")
    print(f"  Checks passed:  {result.checks_passed}/8")
    print(f"  Approved:       {result.approved}")
    print(f"  Recommendation: {result.recommendation.value}")
    print(f"  Failed checks:  {result.failed_checks}")
    for name, detail in result.check_details.items():
        print(f"    {name}: passed={detail['passed']}, score={detail['score']:.3f}, weight={detail.get('weight', '?')}")
    assert result.approved, f"Qualified prospect should be approved! Got: {result.recommendation.value}"
    assert result.overall_score >= 95, f"Qualified prospect should score 95+, got {result.overall_score}"
    print(f"  ✅ PASS — Score {result.overall_score} >= 95, approved for transfer")

    # Test 2: Silent call
    print("\n--- TEST 2: Silent Call ---")
    ctx = build_silent_call()
    result = gate.evaluate(ctx)
    print(f"  Score:          {result.overall_score}/100")
    print(f"  Checks passed:  {result.checks_passed}/8")
    print(f"  Approved:       {result.approved}")
    print(f"  Recommendation: {result.recommendation.value}")
    print(f"  Failed checks:  {result.failed_checks}")
    assert not result.approved, "Silent call should NOT be approved!"
    assert result.overall_score < 15, f"Silent call should score < 15, got {result.overall_score}"
    assert result.recommendation == TransferRecommendation.END_CALL
    print(f"  ✅ PASS — Score {result.overall_score} < 15, end_call")

    # Test 3: TV noise
    print("\n--- TEST 3: TV Noise ---")
    ctx = build_tv_noise()
    result = gate.evaluate(ctx)
    print(f"  Score:          {result.overall_score}/100")
    print(f"  Checks passed:  {result.checks_passed}/8")
    print(f"  Approved:       {result.approved}")
    print(f"  Recommendation: {result.recommendation.value}")
    print(f"  Failed checks:  {result.failed_checks}")
    for name, detail in result.check_details.items():
        print(f"    {name}: passed={detail['passed']}, score={detail['score']:.3f}, weight={detail.get('weight', '?')}")
    assert not result.approved, "TV noise should NOT be approved!"
    assert result.overall_score < 40, f"TV noise should score < 40, got {result.overall_score}"
    print(f"  ✅ PASS — Score {result.overall_score} < 40, {result.recommendation.value}")

    print("\n" + "=" * 70)
    print("ALL 3 TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()

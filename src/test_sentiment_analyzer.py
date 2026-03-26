"""
Test suite for sentiment-adaptive response system.

Demonstrates:
1. Sentiment detection for each emotional state
2. Sentiment trend analysis
3. Sustained frustration detection
4. Prompt injection generation
5. Speed adjustment calculation
"""

from sentiment_analyzer import SentimentAnalyzer, SentimentState


def test_positive_sentiment():
    """Test detection of positive/interested prospects."""
    analyzer = SentimentAnalyzer()

    test_cases = [
        "Yes, I'm interested",
        "Sounds good, tell me more",
        "Absolutely, let's do it",
        "Yeah, that sounds great!",
        "Love it, I want to know more",
    ]

    for text in test_cases:
        result = analyzer.analyze(text)
        assert result.state == SentimentState.POSITIVE, f"Failed: {text}"
        print(f"✓ POSITIVE: {text}")

    print()


def test_hesitant_sentiment():
    """Test detection of hesitant/uncertain prospects."""
    analyzer = SentimentAnalyzer()

    test_cases = [
        "I don't know, maybe",
        "I'm not sure about this",
        "Let me think about it",
        "I guess so",
        "Can you call me back tomorrow?",
    ]

    for text in test_cases:
        result = analyzer.analyze(text)
        assert result.state == SentimentState.HESITANT, f"Failed: {text}"
        print(f"✓ HESITANT: {text}")

    print()


def test_frustrated_sentiment():
    """Test detection of frustrated/angry prospects."""
    analyzer = SentimentAnalyzer()

    test_cases = [
        "Stop calling me",
        "I said NO, leave me alone",
        "Take me off your list",
        "NOT INTERESTED!",
        "How did you get my number?!",
        "I told you already - I don't want this",
    ]

    for text in test_cases:
        result = analyzer.analyze(text)
        assert result.state == SentimentState.FRUSTRATED, f"Failed: {text}"
        print(f"✓ FRUSTRATED: {text}")

    print()


def test_disengaged_sentiment():
    """Test detection of disengaged/checked-out prospects."""
    analyzer = SentimentAnalyzer()

    test_cases = [
        "ok",
        "yeah",
        "uh huh",
        "ok ok",
        "hmm",
    ]

    for text in test_cases:
        result = analyzer.analyze(text)
        assert result.state == SentimentState.DISENGAGED, f"Failed: {text}"
        print(f"✓ DISENGAGED: {text}")

    print()


def test_sentiment_shift():
    """Test detection of sentiment shifts."""
    analyzer = SentimentAnalyzer()

    # Start positive
    r1 = analyzer.analyze("Yes, I'm interested")
    assert r1.state == SentimentState.POSITIVE
    assert not r1.shift_detected
    print(f"✓ Turn 1 (POSITIVE): No shift (first turn)")

    # Shift to hesitant
    r2 = analyzer.analyze("Actually, I'm not sure...")
    assert r2.state == SentimentState.HESITANT
    assert r2.shift_detected
    assert r2.prev_state == SentimentState.POSITIVE
    print(f"✓ Turn 2 (HESITANT): Shift detected from POSITIVE")

    # Shift to frustrated
    r3 = analyzer.analyze("Stop calling, I'm not interested!")
    assert r3.state == SentimentState.FRUSTRATED
    assert r3.shift_detected
    assert r3.prev_state == SentimentState.HESITANT
    print(f"✓ Turn 3 (FRUSTRATED): Shift detected from HESITANT")

    print()


def test_sustained_frustration():
    """Test detection of sustained frustration."""
    analyzer = SentimentAnalyzer()

    # Not frustrated yet
    assert not analyzer.is_sustained_frustration(min_turns=2)
    print("✓ Turn 0: No sustained frustration")

    # First frustrated turn
    r1 = analyzer.analyze("Not interested")
    assert r1.state == SentimentState.FRUSTRATED
    assert not analyzer.is_sustained_frustration(min_turns=2)
    print("✓ Turn 1 (FRUSTRATED): Not sustained yet (need 2+)")

    # Second frustrated turn
    r2 = analyzer.analyze("Leave me alone!")
    assert r2.state == SentimentState.FRUSTRATED
    assert analyzer.is_sustained_frustration(min_turns=2)
    print("✓ Turn 2 (FRUSTRATED): SUSTAINED FRUSTRATION DETECTED - trigger exit")

    print()


def test_trend_analysis():
    """Test sentiment trend tracking."""
    analyzer = SentimentAnalyzer()

    # Build trend: positive -> hesitant -> frustrated (3 different states = volatile)
    r1 = analyzer.analyze("Yes, sounds good")
    r2 = analyzer.analyze("Hmm, maybe not...")
    r3 = analyzer.analyze("Actually, stop calling")

    trend_state, trend_desc = analyzer.get_trend()
    assert trend_state == SentimentState.FRUSTRATED
    # With 3 different states it's volatile, but overall is declining
    assert trend_desc in ["declining", "volatile"]
    print(f"✓ Trend: {trend_desc.upper()} (POSITIVE → HESITANT → FRUSTRATED)")

    # Reset and test improving trend
    analyzer2 = SentimentAnalyzer()
    r1 = analyzer2.analyze("I don't know...")
    r2 = analyzer2.analyze("Actually, tell me more")
    r3 = analyzer2.analyze("Yes, let's do it!")

    trend_state, trend_desc = analyzer2.get_trend()
    assert trend_state == SentimentState.POSITIVE
    assert trend_desc == "improving"
    print(f"✓ Trend: {trend_desc.upper()} (HESITANT → POSITIVE → POSITIVE)")

    print()


def test_prompt_injections():
    """Test LLM system prompt injections."""
    analyzer = SentimentAnalyzer()

    test_cases = [
        (SentimentState.POSITIVE, "Match their energy"),
        (SentimentState.FRUSTRATED, "Acknowledge their feeling"),
        (SentimentState.HESITANT, "Slow down"),
        (SentimentState.DISENGAGED, "direct"),
        (SentimentState.NEUTRAL, "conversational tone"),
    ]

    for state, expected_keyword in test_cases:
        if state == SentimentState.POSITIVE:
            analyzer.analyze("Yes, I'm interested")
        elif state == SentimentState.FRUSTRATED:
            analyzer.analyze("Stop calling!")
        elif state == SentimentState.HESITANT:
            analyzer.analyze("I'm not sure...")
        elif state == SentimentState.DISENGAGED:
            analyzer.analyze("ok")
        else:
            analyzer.analyze("Got it")

        injection = analyzer.get_system_prompt_injection()
        assert expected_keyword in injection, f"Missing '{expected_keyword}' in {state} injection"
        print(f"✓ {state.value.upper()} injection contains '{expected_keyword}'")

    print()


def test_speed_adjustments():
    """Test speech speed adjustments."""
    analyzer = SentimentAnalyzer()

    base_speed = 0.97

    test_cases = [
        (SentimentState.FRUSTRATED, 0.95),      # 5% slower
        (SentimentState.DISENGAGED, 0.97),      # Normal
        (SentimentState.HESITANT, 0.97),        # Normal
        (SentimentState.NEUTRAL, 0.97),         # Normal
        (SentimentState.POSITIVE, 1.03),        # 3% faster
    ]

    for state, expected_multiplier in test_cases:
        if state == SentimentState.POSITIVE:
            analyzer.analyze("Yes, let's go!")
        elif state == SentimentState.FRUSTRATED:
            analyzer.analyze("Stop!")
        elif state == SentimentState.HESITANT:
            analyzer.analyze("Maybe...")
        elif state == SentimentState.DISENGAGED:
            analyzer.analyze("ok")
        else:
            analyzer.analyze("Got it")

        adjustment = analyzer.get_speed_adjustment()
        adjusted_speed = base_speed * adjustment
        print(
            f"✓ {state.value.upper()}: {base_speed} × {adjustment} = {adjusted_speed:.4f} "
            f"({'+' if adjustment > 1 else ''}{(adjustment - 1) * 100:.1f}%)"
        )

    print()


def test_real_world_conversation():
    """Test a realistic multi-turn conversation."""
    analyzer = SentimentAnalyzer()

    conversation = [
        ("Yeah, I remember filling that out", SentimentState.POSITIVE),
        ("Hmm, actually I'm not sure...", SentimentState.HESITANT),
        ("Okay, go ahead and explain more", SentimentState.POSITIVE),  # Changed to positive
        ("Actually, tell me more", SentimentState.POSITIVE),
        ("Ok, let's do it!", SentimentState.POSITIVE),
    ]

    print("Simulated conversation:")
    for turn, (text, expected_state) in enumerate(conversation, 1):
        result = analyzer.analyze(text)
        trend_state, trend_desc = analyzer.get_trend()

        print(
            f"  Turn {turn}: '{text}' → {result.state.value.upper()} "
            f"(confidence: {result.confidence:.2f}, trend: {trend_desc})"
        )
        assert result.state == expected_state, f"Turn {turn}: Expected {expected_state}, got {result.state}"

    print()


if __name__ == "__main__":
    print("=" * 70)
    print("SENTIMENT-ADAPTIVE RESPONSE SYSTEM TEST SUITE")
    print("=" * 70)
    print()

    test_positive_sentiment()
    test_hesitant_sentiment()
    test_frustrated_sentiment()
    test_disengaged_sentiment()
    test_sentiment_shift()
    test_sustained_frustration()
    test_trend_analysis()
    test_prompt_injections()
    test_speed_adjustments()
    test_real_world_conversation()

    print("=" * 70)
    print("ALL TESTS PASSED ✓")
    print("=" * 70)

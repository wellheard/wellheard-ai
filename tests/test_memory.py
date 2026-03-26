"""
Tests for the Conversation Memory system.
Verifies prompt injection, summarization, sentiment trending, and behavioral guidance.
"""
import asyncio
import json
import pytest
from src.memory import ConversationMemory


@pytest.fixture
def memory():
    return ConversationMemory()


# ── Prompt Injection Tests ───────────────────────────────────────────────

class TestMemoryPromptInjection:
    """Test that memory is correctly injected into system prompts."""

    def test_first_call_no_injection(self, memory):
        """First call (attempt_count=0) should return base prompt unchanged."""
        lead = {"attempt_count": 0, "last_call_summary": ""}
        base_prompt = "You are a sales agent."
        result = memory.build_memory_prompt(lead, base_prompt)
        assert result == base_prompt

    def test_second_call_injects_memory(self, memory):
        """Second call should inject prior call context."""
        lead = {
            "attempt_count": 1,
            "first_name": "Sarah",
            "last_call_summary": "Sarah was interested but had concerns about pricing.",
            "cumulative_context": "[Call #1] Sarah was interested but had pricing concerns.",
            "objection_types": ["price"],
            "behavior_notes": "Friendly but cautious",
            "rapport_points": ["has two kids", "lives in Austin"],
            "sentiment_trend": "neutral",
            "preferred_callback_time": "mornings",
            "total_talk_seconds": 120.0,
        }
        base_prompt = "You are a solar panel sales agent."
        result = memory.build_memory_prompt(lead, base_prompt)

        # Should contain memory elements
        assert "call #2 with Sarah" in result
        assert "pricing" in result.lower()
        assert "two kids" in result
        assert "Austin" in result
        assert "mornings" in result
        # Base prompt should still be there
        assert "solar panel sales agent" in result

    def test_behavioral_guidance_hostile(self, memory):
        """Hostile sentiment should generate cautious guidance."""
        lead = {
            "attempt_count": 2,
            "first_name": "Mike",
            "last_call_summary": "Mike was angry and asked to be removed.",
            "sentiment_trend": "hostile",
            "objection_types": [],
            "rapport_points": [],
            "behavior_notes": "",
            "cumulative_context": "",
            "preferred_callback_time": "",
            "total_talk_seconds": 30.0,
        }
        result = memory.build_memory_prompt(lead, "Be helpful.")
        assert "respectful" in result.lower() or "remove" in result.lower()

    def test_high_attempt_count_adjusts_approach(self, memory):
        """5+ attempts should advise a final-attempt approach."""
        lead = {
            "attempt_count": 5,
            "first_name": "Lisa",
            "last_call_summary": "Lisa didn't pick up again.",
            "sentiment_trend": "neutral",
            "objection_types": [],
            "rapport_points": [],
            "behavior_notes": "",
            "cumulative_context": "",
            "preferred_callback_time": "",
            "total_talk_seconds": 0.0,
        }
        result = memory.build_memory_prompt(lead, "Sell insurance.")
        assert "attempt #6" in result.lower() or "final" in result.lower()


# ── Summarization Tests ──────────────────────────────────────────────────

class TestSummarization:
    """Test post-call summarization and memory persistence."""

    @pytest.mark.asyncio
    async def test_summarize_short_call_skipped(self, memory):
        """Calls with <2 turns should be skipped."""
        result = await memory.summarize_and_save(
            lead_data={"attempt_count": 0},
            transcript=[{"role": "assistant", "content": "Hello?"}],
            call_duration_seconds=5.0,
            llm_generate_fn=None,  # Should never be called
        )
        assert result["lead_updates"] == {}
        assert result["call_log_updates"] == {}

    @pytest.mark.asyncio
    async def test_summarize_uses_llm(self, memory):
        """Verify LLM is called with the transcript and result is parsed."""
        mock_response = json.dumps({
            "summary": "Prospect was interested in the premium plan.",
            "objections": ["price", "contract_length"],
            "sentiment": "positive",
            "rapport_points": ["mentioned daughter's birthday"],
            "preferred_callback_time": "after 3pm",
            "next_action": "Send pricing sheet and call back Thursday",
            "behavior_notes": "Talkative and friendly",
        })

        async def mock_llm(messages, system_prompt):
            return mock_response

        lead = {
            "attempt_count": 0,
            "cumulative_context": "",
            "objection_types": [],
            "rapport_points": [],
            "sentiment_trend": "",
            "total_talk_seconds": 0.0,
        }
        transcript = [
            {"role": "assistant", "content": "Hi, this is Sarah from SolarCo."},
            {"role": "user", "content": "Oh hi, I was wondering about the premium plan."},
            {"role": "assistant", "content": "Great! The premium plan includes..."},
            {"role": "user", "content": "That sounds good but the price is a bit high."},
        ]

        result = await memory.summarize_and_save(
            lead_data=lead,
            transcript=transcript,
            call_duration_seconds=90.0,
            llm_generate_fn=mock_llm,
        )

        lu = result["lead_updates"]
        assert "premium plan" in lu["last_call_summary"]
        assert "price" in lu["objection_types"]
        assert "contract_length" in lu["objection_types"]
        assert any("daughter" in r for r in lu["rapport_points"])
        assert lu["sentiment_trend"] == "positive"
        assert lu["total_talk_seconds"] == 90.0

        cl = result["call_log_updates"]
        assert cl["sentiment"] == "positive"
        assert "pricing sheet" in cl["next_action"]

    @pytest.mark.asyncio
    async def test_summarize_merges_existing_objections(self, memory):
        """New objections should be merged with existing ones, deduplicated."""
        async def mock_llm(messages, system_prompt):
            return json.dumps({
                "summary": "Follow-up call about timing.",
                "objections": ["timing", "price"],  # "price" already exists
                "sentiment": "neutral",
                "rapport_points": [],
                "preferred_callback_time": "",
                "next_action": "",
                "behavior_notes": "",
            })

        lead = {
            "attempt_count": 1,
            "cumulative_context": "[Call #1] Initial outreach.",
            "objection_types": ["price"],
            "rapport_points": ["has a dog named Max"],
            "sentiment_trend": "neutral",
            "total_talk_seconds": 60.0,
        }
        transcript = [
            {"role": "user", "content": "Now isn't a good time."},
            {"role": "assistant", "content": "I understand, when would be better?"},
        ]

        result = await memory.summarize_and_save(lead, transcript, 30.0, mock_llm)
        lu = result["lead_updates"]

        # Should have both "price" and "timing" without duplicates
        assert "price" in lu["objection_types"]
        assert "timing" in lu["objection_types"]
        assert lu["objection_types"].count("price") == 1

        # Existing rapport should be preserved
        assert "has a dog named Max" in lu["rapport_points"]

        # Cumulative context should append
        assert "[Call #1]" in lu["cumulative_context"]
        assert "[Call #2]" in lu["cumulative_context"]


# ── Sentiment Trend Tests ────────────────────────────────────────────────

class TestSentimentTrend:
    """Test sentiment trajectory computation."""

    def test_first_call_uses_current(self, memory):
        result = memory._compute_sentiment_trend("positive", "")
        assert result == "positive"

    def test_improving_sentiment(self, memory):
        result = memory._compute_sentiment_trend("positive", "negative")
        assert result == "warming"

    def test_declining_sentiment(self, memory):
        result = memory._compute_sentiment_trend("negative", "positive")
        assert result == "cooling"

    def test_stable_positive(self, memory):
        result = memory._compute_sentiment_trend("positive", "positive")
        assert result == "positive"

    def test_hostile_stays_hostile(self, memory):
        result = memory._compute_sentiment_trend("hostile", "hostile")
        assert result == "hostile"


# ── JSON Parsing Robustness ──────────────────────────────────────────────

class TestJsonParsing:
    """Test that summary JSON parsing handles edge cases."""

    def test_clean_json(self, memory):
        raw = '{"summary": "test", "objections": [], "sentiment": "neutral"}'
        result = memory._parse_summary_json(raw)
        assert result["summary"] == "test"

    def test_markdown_fenced_json(self, memory):
        raw = '```json\n{"summary": "test", "objections": []}\n```'
        result = memory._parse_summary_json(raw)
        assert result["summary"] == "test"

    def test_invalid_json_fallback(self, memory):
        raw = "This is not JSON at all."
        result = memory._parse_summary_json(raw)
        assert result["summary"] == raw
        assert result["objections"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

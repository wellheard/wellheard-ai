"""
WellHeard AI — Conversation Memory System

Provides cross-call and within-call memory so the AI:
1. Remembers what happened on previous calls to the same prospect
2. Adjusts behavior based on prior outcomes (objections, sentiment, rapport)
3. Maintains continuity when switching between LLM providers mid-call

Architecture:
- Before call: load lead's memory → inject into system prompt
- During call: conversation history lives in orchestrator._conversation_history
- After call: LLM summarizes the call → persist to Lead + CallLog

The memory is injected as a structured block at the top of the system prompt,
so every LLM provider (Groq, Gemini, etc.) sees the same context regardless
of which one handles a given turn.
"""

import structlog
from typing import Optional
from datetime import datetime, timezone

logger = structlog.get_logger()

# ── Prompt Templates ─────────────────────────────────────────────────────

MEMORY_INJECTION_TEMPLATE = """
## PRIOR CALL HISTORY WITH THIS PROSPECT
{memory_block}

## BEHAVIORAL GUIDANCE BASED ON HISTORY
{behavioral_guidance}
"""

SUMMARIZE_CALL_PROMPT = """You are analyzing a sales/outreach phone call transcript. Produce a structured JSON summary.

TRANSCRIPT:
{transcript}

Respond with ONLY valid JSON (no markdown fences):
{{
  "summary": "2-3 sentence summary of what happened on this call",
  "objections": ["list", "of", "objections raised by prospect"],
  "sentiment": "positive|neutral|negative|hostile",
  "rapport_points": ["personal details mentioned: kids, pets, hobbies, etc."],
  "preferred_callback_time": "any stated preference for callback timing, or empty string",
  "next_action": "recommended next step for the next call",
  "behavior_notes": "brief notes on prospect personality/communication style"
}}"""


class ConversationMemory:
    """
    Manages persistent memory for voice AI conversations.

    Usage:
        memory = ConversationMemory()

        # Before a call starts:
        enhanced_prompt = memory.build_memory_prompt(lead, campaign_system_prompt)

        # After a call ends:
        await memory.save_call_memory(lead, call_log, transcript, llm_provider)
    """

    def build_memory_prompt(
        self,
        lead_data: dict,
        base_system_prompt: str,
    ) -> str:
        """
        Inject prior call memory into the system prompt.

        Args:
            lead_data: Dict with lead fields (from Lead model or API).
                Expected keys: last_call_summary, cumulative_context,
                objection_types, behavior_notes, rapport_points,
                sentiment_trend, attempt_count, first_name,
                preferred_callback_time, total_talk_seconds
            base_system_prompt: The campaign's base system prompt.

        Returns:
            Enhanced system prompt with memory context prepended.
        """
        attempt_count = lead_data.get("attempt_count", 0)

        # First call — no memory to inject
        if attempt_count == 0 or not lead_data.get("last_call_summary"):
            return base_system_prompt

        memory_block = self._format_memory_block(lead_data)
        behavioral_guidance = self._generate_behavioral_guidance(lead_data)

        memory_section = MEMORY_INJECTION_TEMPLATE.format(
            memory_block=memory_block,
            behavioral_guidance=behavioral_guidance,
        )

        # Prepend memory before the base prompt so the AI sees context first
        return memory_section.strip() + "\n\n" + base_system_prompt

    def _format_memory_block(self, lead_data: dict) -> str:
        """Format the raw memory fields into a readable block for the LLM."""
        lines = []
        first_name = lead_data.get("first_name", "the prospect")
        attempt_count = lead_data.get("attempt_count", 0)

        lines.append(f"- This is call #{attempt_count + 1} with {first_name or 'this prospect'}.")
        lines.append(f"- Total prior talk time: {lead_data.get('total_talk_seconds', 0):.0f} seconds.")

        if lead_data.get("last_call_summary"):
            lines.append(f"- Last call summary: {lead_data['last_call_summary']}")

        if lead_data.get("cumulative_context"):
            lines.append(f"- Full context across all calls: {lead_data['cumulative_context']}")

        if lead_data.get("objection_types"):
            objections = lead_data["objection_types"]
            if isinstance(objections, list) and objections:
                lines.append(f"- Known objections: {', '.join(objections)}")

        if lead_data.get("rapport_points"):
            points = lead_data["rapport_points"]
            if isinstance(points, list) and points:
                lines.append(f"- Rapport/personal details: {', '.join(points)}")

        if lead_data.get("behavior_notes"):
            lines.append(f"- Personality notes: {lead_data['behavior_notes']}")

        if lead_data.get("sentiment_trend"):
            lines.append(f"- Sentiment trend: {lead_data['sentiment_trend']}")

        if lead_data.get("preferred_callback_time"):
            lines.append(f"- Preferred callback time: {lead_data['preferred_callback_time']}")

        return "\n".join(lines)

    def _generate_behavioral_guidance(self, lead_data: dict) -> str:
        """
        Generate specific behavioral instructions based on call history.
        This tells the AI HOW to adjust its approach.
        """
        guidance = []
        sentiment = lead_data.get("sentiment_trend", "")
        objections = lead_data.get("objection_types", [])
        attempt_count = lead_data.get("attempt_count", 0)
        first_name = lead_data.get("first_name", "")

        # Acknowledge this isn't the first call
        if first_name:
            guidance.append(
                f"You have spoken with {first_name} before. Reference your prior conversation "
                f"naturally (e.g., 'Last time we spoke, you mentioned...'). Do NOT pretend this "
                f"is a first call."
            )
        else:
            guidance.append(
                "You have called this person before. Acknowledge that naturally."
            )

        # Sentiment-based adjustments
        if sentiment == "hostile":
            guidance.append(
                "The prospect was hostile last time. Be extra respectful, keep it brief, "
                "and offer to remove them from the call list if they seem annoyed."
            )
        elif sentiment == "negative":
            guidance.append(
                "The prospect was negative last time. Approach gently, acknowledge their "
                "concerns, and focus on what's changed since the last call."
            )
        elif sentiment == "warming":
            guidance.append(
                "The prospect is warming up over calls. Build on the positive momentum — "
                "reference what they responded well to previously."
            )
        elif sentiment == "positive":
            guidance.append(
                "The prospect was positive last time. Maintain energy and move toward "
                "next steps or qualification."
            )

        # Objection handling
        if objections and isinstance(objections, list):
            objection_str = ", ".join(objections)
            guidance.append(
                f"Known objections from prior calls: {objection_str}. "
                f"Proactively address these rather than waiting for the prospect to raise them again."
            )

        # Escalation based on attempt count
        if attempt_count >= 5:
            guidance.append(
                f"This is attempt #{attempt_count + 1}. If the prospect is still not interested, "
                f"respect that and don't push. Consider this a final-attempt approach."
            )
        elif attempt_count >= 3:
            guidance.append(
                f"This is attempt #{attempt_count + 1}. The prospect knows who you are. "
                f"Skip long introductions and get to the point faster."
            )

        # Rapport utilization
        rapport = lead_data.get("rapport_points", [])
        if rapport and isinstance(rapport, list):
            guidance.append(
                f"Use these personal details to build rapport: {', '.join(rapport)}. "
                f"Mention one naturally early in the conversation."
            )

        return "\n".join(f"- {g}" for g in guidance)

    async def summarize_and_save(
        self,
        lead_data: dict,
        transcript: list[dict],
        call_duration_seconds: float,
        llm_generate_fn,
    ) -> dict:
        """
        After a call ends, use the LLM to summarize the conversation
        and return fields to persist on the Lead and CallLog.

        Args:
            lead_data: Current lead fields (dict).
            transcript: List of {"role": "user"|"assistant", "content": "..."}.
            call_duration_seconds: How long the call lasted.
            llm_generate_fn: Async function that takes (messages, system_prompt)
                             and returns the full response text.

        Returns:
            Dict with fields to update on Lead and CallLog:
            {
                "lead_updates": { ... fields for Lead model ... },
                "call_log_updates": { ... fields for CallLog model ... },
            }
        """
        if not transcript or len(transcript) < 2:
            logger.info("memory_skip_short_call", turns=len(transcript))
            return {"lead_updates": {}, "call_log_updates": {}}

        # Format transcript for the summarizer
        transcript_text = self._format_transcript_for_summary(transcript)

        # Ask the LLM to summarize
        summary_prompt = SUMMARIZE_CALL_PROMPT.format(transcript=transcript_text)

        try:
            summary_json = await llm_generate_fn(
                messages=[{"role": "user", "content": summary_prompt}],
                system_prompt="You are a call analysis assistant. Respond with valid JSON only.",
            )
            parsed = self._parse_summary_json(summary_json)
        except Exception as e:
            logger.error("memory_summarize_failed", error=str(e))
            # Fallback: store raw transcript info
            parsed = {
                "summary": f"Call lasted {call_duration_seconds:.0f}s with {len(transcript)} turns.",
                "objections": [],
                "sentiment": "neutral",
                "rapport_points": [],
                "preferred_callback_time": "",
                "next_action": "",
                "behavior_notes": "",
            }

        # Build cumulative context (append to existing)
        existing_context = lead_data.get("cumulative_context", "")
        attempt_num = lead_data.get("attempt_count", 0) + 1
        new_context_entry = f"[Call #{attempt_num}] {parsed['summary']}"
        if existing_context:
            cumulative = existing_context + " | " + new_context_entry
        else:
            cumulative = new_context_entry

        # Trim cumulative context if it gets too long (keep last ~2000 chars)
        if len(cumulative) > 2500:
            cumulative = "..." + cumulative[-2000:]

        # Merge objections (deduplicate)
        existing_objections = lead_data.get("objection_types", []) or []
        new_objections = parsed.get("objections", []) or []
        all_objections = list(dict.fromkeys(existing_objections + new_objections))

        # Merge rapport points (deduplicate)
        existing_rapport = lead_data.get("rapport_points", []) or []
        new_rapport = parsed.get("rapport_points", []) or []
        all_rapport = list(dict.fromkeys(existing_rapport + new_rapport))

        # Determine sentiment trend
        sentiment_trend = self._compute_sentiment_trend(
            current_sentiment=parsed.get("sentiment", "neutral"),
            previous_trend=lead_data.get("sentiment_trend", ""),
        )

        # Total talk time
        total_talk = lead_data.get("total_talk_seconds", 0) + call_duration_seconds

        lead_updates = {
            "last_call_summary": parsed["summary"],
            "cumulative_context": cumulative,
            "objection_types": all_objections,
            "behavior_notes": parsed.get("behavior_notes", ""),
            "preferred_callback_time": parsed.get("preferred_callback_time", ""),
            "rapport_points": all_rapport,
            "sentiment_trend": sentiment_trend,
            "total_talk_seconds": total_talk,
        }

        call_log_updates = {
            "call_summary": parsed["summary"],
            "objections_detected": new_objections,
            "sentiment": parsed.get("sentiment", "neutral"),
            "next_action": parsed.get("next_action", ""),
        }

        logger.info("memory_saved",
            summary_len=len(parsed["summary"]),
            objections=len(new_objections),
            sentiment=parsed.get("sentiment"),
            trend=sentiment_trend,
        )

        return {
            "lead_updates": lead_updates,
            "call_log_updates": call_log_updates,
        }

    def _format_transcript_for_summary(self, transcript: list[dict]) -> str:
        """Convert transcript list to readable text for LLM summarization."""
        lines = []
        for turn in transcript:
            role = turn.get("role", "unknown")
            content = turn.get("content", "")
            if role == "user":
                lines.append(f"PROSPECT: {content}")
            elif role == "assistant":
                lines.append(f"AI AGENT: {content}")
        return "\n".join(lines)

    def _parse_summary_json(self, raw_text: str) -> dict:
        """Parse the LLM's JSON summary response, handling common issues."""
        import json

        # Strip markdown code fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]  # Remove first line
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("memory_json_parse_failed", raw=text[:200])
            return {
                "summary": text[:500],
                "objections": [],
                "sentiment": "neutral",
                "rapport_points": [],
                "preferred_callback_time": "",
                "next_action": "",
                "behavior_notes": "",
            }

        # Ensure all expected keys exist with defaults
        defaults = {
            "summary": "",
            "objections": [],
            "sentiment": "neutral",
            "rapport_points": [],
            "preferred_callback_time": "",
            "next_action": "",
            "behavior_notes": "",
        }
        for key, default in defaults.items():
            if key not in parsed:
                parsed[key] = default

        return parsed

    def _compute_sentiment_trend(
        self,
        current_sentiment: str,
        previous_trend: str,
    ) -> str:
        """
        Compute sentiment trajectory over time.
        Returns: warming, cooling, neutral, hostile, positive
        """
        sentiment_scores = {
            "positive": 3,
            "neutral": 2,
            "negative": 1,
            "hostile": 0,
        }

        trend_scores = {
            "positive": 3,
            "warming": 2.5,
            "neutral": 2,
            "cooling": 1.5,
            "negative": 1,
            "hostile": 0,
        }

        current_score = sentiment_scores.get(current_sentiment, 2)
        previous_score = trend_scores.get(previous_trend, 2)

        # No previous trend — just use current sentiment
        if not previous_trend:
            return current_sentiment

        # Determine direction
        diff = current_score - previous_score
        if diff > 0.3:
            return "warming"
        elif diff < -0.3:
            return "cooling"
        elif current_score >= 2.5:
            return "positive"
        elif current_score <= 0.5:
            return "hostile"
        else:
            return "neutral"

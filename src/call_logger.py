"""
WellHeard AI — File-based Call Logger

Writes structured call events to /tmp/wellheard_calls.log as JSON lines.
Fly.io's log buffer only holds ~100 lines, making it impossible to see
full conversation flows. This file persists for the machine's lifecycle.

Also configures structlog to write to both stdout AND the call log file.
"""

import json
import time
import os
import logging
from logging.handlers import RotatingFileHandler

import structlog

LOG_FILE = "/tmp/wellheard_calls.log"
MAX_BYTES = 50 * 1024 * 1024  # 50MB (was 10MB — too small, caused grading data loss)
BACKUP_COUNT = 3


def setup_file_logging():
    """Configure structlog to output JSON to both stdout and a rotating file."""

    # Create file handler
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT
    )
    file_handler.setLevel(logging.DEBUG)

    # Console handler (already exists via default, but we'll be explicit)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Remove any existing handlers to avoid duplicates
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Configure structlog to use standard logging
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_recent_logs(call_id: str = None, lines: int = 500) -> list[dict]:
    """Read recent log entries, optionally filtered by call_id."""
    if not os.path.exists(LOG_FILE):
        return []

    result = []
    try:
        with open(LOG_FILE, 'r') as f:
            # Read all lines, take last N
            all_lines = f.readlines()
            recent = all_lines[-lines:] if len(all_lines) > lines else all_lines

            for line in recent:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if call_id and entry.get("call_id") != call_id:
                        continue
                    result.append(entry)
                except json.JSONDecodeError:
                    # Structlog might output non-JSON in some edge cases
                    if call_id is None or (call_id and call_id in line):
                        result.append({"raw": line})
    except Exception:
        pass

    return result


def get_call_ids() -> list[dict]:
    """Get unique call IDs from the log with their first/last timestamps."""
    if not os.path.exists(LOG_FILE):
        return []

    calls = {}
    try:
        with open(LOG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    cid = entry.get("call_id")
                    ts = entry.get("timestamp", "")
                    event = entry.get("event", "")
                    if cid:
                        if cid not in calls:
                            calls[cid] = {
                                "call_id": cid,
                                "first_seen": ts,
                                "last_seen": ts,
                                "events": 0,
                                "key_events": [],
                            }
                        calls[cid]["last_seen"] = ts
                        calls[cid]["events"] += 1
                        # Track important events
                        if event in (
                            "phase1_detected_human", "phase2_pitch_complete",
                            "phase3_entering", "text_turn_complete",
                            "barge_in_detected", "call_conversation_summary",
                            "transfer_initiated", "call_ended",
                        ):
                            calls[cid]["key_events"].append(event)
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    return sorted(calls.values(), key=lambda x: x["first_seen"], reverse=True)

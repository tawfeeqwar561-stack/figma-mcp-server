"""
Audit logger.
Records every UI generation request to a local JSON-lines log file:
who asked (in a single-user local setup, just a timestamp), what they
asked for, whether it succeeded, and how many nodes were created.

This is the minimal, safe version of an audit trail. A production
deployment would extend this with a real user identity (from OAuth)
and write to a centralized log store instead of a local file.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).parent / "audit_log.jsonl"


def log_generation(prompt: str, result: dict) -> None:
    """Append one audit record for a generate_ui_from_prompt call."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "screen_name": result.get("screen_name"),
        "succeeded": result.get("succeeded"),
        "total_nodes": result.get("total_nodes"),
        "failed": result.get("failed", 0),
    }
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        # Audit logging failure should never break the actual feature.
        logger.warning("Could not write audit log: %s", exc)

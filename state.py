"""
state.py — Persistent JSON state for the Legal Intelligence Feed.

Tracks:
  - seen_urls: set of URLs already processed (prevents re-pushing)
  - last_scraped: ISO timestamp per source (for logging/debugging)
  - push_failures: list of items that failed to push (for retry/audit)

State is stored at DATA_DIR/feed_state.json.
On Railway, mount a persistent volume at /data and set DATA_DIR=/data.
"""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "feed_state.json"

_DEFAULT_STATE: dict[str, Any] = {
    "seen_urls": [],
    "last_scraped": {},
    "push_failures": [],
    "stats": {
        "total_pushed": 0,
        "total_skipped": 0,
        "total_failed": 0,
    },
}


def _load() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Ensure all keys exist (forward-compat for new fields)
            for k, v in _DEFAULT_STATE.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception as e:
            logger.error(f"[state] Failed to load state file: {e}. Starting fresh.")
    return dict(_DEFAULT_STATE)


def _save(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.error(f"[state] Failed to save state: {e}")
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ── Public API ────────────────────────────────────────────────────────────────

def is_seen(url: str) -> bool:
    state = _load()
    return url in state["seen_urls"]


def mark_seen(url: str) -> None:
    state = _load()
    if url not in state["seen_urls"]:
        state["seen_urls"].append(url)
        _save(state)


def get_last_scraped(source: str) -> str | None:
    state = _load()
    return state["last_scraped"].get(source)


def set_last_scraped(source: str) -> None:
    state = _load()
    state["last_scraped"][source] = datetime.now(timezone.utc).isoformat()
    _save(state)


def log_push_failure(item: dict) -> None:
    """Record a push failure for audit. Keeps last 100 failures."""
    state = _load()
    item["failed_at"] = datetime.now(timezone.utc).isoformat()
    state["push_failures"].append(item)
    state["push_failures"] = state["push_failures"][-100:]
    state["stats"]["total_failed"] = state["stats"].get("total_failed", 0) + 1
    _save(state)


def increment_pushed() -> None:
    state = _load()
    state["stats"]["total_pushed"] = state["stats"].get("total_pushed", 0) + 1
    _save(state)


def increment_skipped() -> None:
    state = _load()
    state["stats"]["total_skipped"] = state["stats"].get("total_skipped", 0) + 1
    _save(state)


def get_stats() -> dict:
    state = _load()
    return {
        "stats": state.get("stats", {}),
        "last_scraped": state.get("last_scraped", {}),
        "seen_url_count": len(state.get("seen_urls", [])),
        "pending_failures": len(state.get("push_failures", [])),
    }


def get_failures() -> list:
    state = _load()
    return state.get("push_failures", [])


def clear_failures() -> None:
    state = _load()
    state["push_failures"] = []
    _save(state)


def get_max_new_per_run() -> int:
    """Return the maximum number of new items to process per scrape run."""
    return int(os.environ.get("MAX_NEW_PER_RUN", "10"))

"""
Tracks which URLs have already been pushed to MutemoOS.
Persists to a local JSON file so restarts don't re-push old content.
"""
import json
import os
from datetime import datetime

STATE_FILE = os.environ.get("STATE_FILE", "data/feed_state.json")

def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_urls": [], "last_run": {}}
    with open(STATE_FILE) as f:
        return json.load(f)

def _save(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def is_seen(url: str) -> bool:
    return url in _load().get("seen_urls", [])

def mark_seen(url: str):
    state = _load()
    if url not in state["seen_urls"]:
        state["seen_urls"].append(url)
    _save(state)

def mark_run(source: str):
    state = _load()
    state["last_run"][source] = datetime.utcnow().isoformat()
    _save(state)

def get_last_run(source: str) -> str:
    return _load().get("last_run", {}).get(source)
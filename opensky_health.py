"""Notify on OpenSky API outage/recovery -- edge-triggered (only when the
failing/succeeding state actually changes), not once per cron cycle. Without
this, an extended outage (observed: a real multi-hour 503 outage, 100%
failure rate) would otherwise spam a notification every 2 minutes for its
whole duration.

State (currently failing or not, and since when) is tracked in a small local
JSON file, since main.py is a fresh process each cron cycle with no other way
to remember the previous cycle's outcome.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from notify import send_notification

HEALTH_STATE_PATH = Path(__file__).with_name("opensky_health_state.json")


def _load_health_state():
    if not HEALTH_STATE_PATH.exists():
        return {}
    return json.loads(HEALTH_STATE_PATH.read_text())


def _save_health_state(state):
    HEALTH_STATE_PATH.write_text(json.dumps(state))


def _format_duration(seconds):
    hours, seconds = divmod(int(seconds), 3600)
    minutes = seconds // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def check_opensky_health(success, error_text=None):
    """Call once per main.py cycle with whether the OpenSky API call
    succeeded (and, on failure, the error text from
    CachedOpenSkyClient.last_error). Sends a notification only on the
    down/up transition, not on every failing cycle in between.
    """
    state = _load_health_state()
    was_failing = state.get("failing", False)

    if not success:
        if not was_failing:
            state = {
                "failing": True,
                "since": datetime.now(timezone.utc).isoformat(),
                "last_error": error_text,
            }
            message = f"🔴 OpenSky API appears to be down: {error_text}"
            logging.info(f"OpenSky health: {message}")
            send_notification(message)
        else:
            state["last_error"] = error_text
        _save_health_state(state)
        return

    if was_failing:
        since = datetime.fromisoformat(state["since"])
        duration_text = _format_duration((datetime.now(timezone.utc) - since).total_seconds())
        message = f"🟢 OpenSky API is back up (was down for {duration_text})."
        logging.info(f"OpenSky health: {message}")
        send_notification(message)
        _save_health_state({"failing": False})

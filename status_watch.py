import html
import logging
from pathlib import Path

import yaml

from db_connection import get_connection
from notify import send_notification
from queries import (
    get_friendly_name,
    get_last_known_status,
    get_last_known_status_by_callsign,
    get_route_for_callsign,
)

WATCHLIST_PATH = Path(__file__).with_name("notify_watchlist.yaml")


def load_watchlist(path=WATCHLIST_PATH):
    """Return the watched icao24s in the same order they're listed in the YAML file
    (deduplicated, first occurrence wins)."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return list(dict.fromkeys(icao24.strip().lower() for icao24 in data.get("watched_aircraft", [])))


def load_callsign_watchlist(path=WATCHLIST_PATH):
    """Return the watched callsigns in the same order they're listed in the YAML
    file (deduplicated, first occurrence wins)."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return list(dict.fromkeys(callsign.strip().upper() for callsign in data.get("watched_callsigns", [])))


def _send_status_change_notification(icao24, callsign, new_on_ground, conn):
    """Build and send the "landed"/"airborne" message, shared by the icao24-based
    and callsign-based watchlists."""
    friendly_name = get_friendly_name(icao24, conn=conn) if icao24 else None
    identifier = f"{html.escape(callsign)} / {icao24}" if callsign else icao24
    label = f"<b>{html.escape(friendly_name)}</b> ({identifier})" if friendly_name else identifier

    message = (
        f"🛬 {label} has landed."
        if new_on_ground
        else f"🛫 {label} is now airborne."
    )

    route = get_route_for_callsign(callsign, conn=conn) if callsign else None
    if route and route["iata_origin"] and route["iata_destination"]:
        message += f" Route: {route['iata_origin']} → {route['iata_destination']}"

    logging.info(f"Watchlist status change: {message}")
    send_notification(message, parse_mode="HTML")


def check_status_changes(states, watchlist=None):
    """Notify for each watched icao24 in this snapshot whose on_ground flag flipped
    since its previously stored state. Must be called before the snapshot is inserted,
    since it compares against the last row already committed to `states`.
    """
    watchlist = load_watchlist() if watchlist is None else watchlist
    if not watchlist or not states.get("states"):
        return

    snapshot_by_icao24 = {s[0]: s for s in states["states"] if s[0] in watchlist}
    if not snapshot_by_icao24:
        return

    conn = get_connection()
    try:
        for icao24, state in snapshot_by_icao24.items():
            previous = get_last_known_status(icao24, conn=conn)
            new_on_ground = state[8]
            callsign = (state[1] or "").strip() or None

            if previous is None:
                logging.info(f"Watchlist: first sighting of {icao24} ({callsign or icao24}), currently {'on the ground' if new_on_ground else 'airborne'}.")
                continue

            previous_on_ground = previous["status"] == "on ground"
            if previous_on_ground == new_on_ground:
                continue

            _send_status_change_notification(icao24, callsign, new_on_ground, conn)
    finally:
        conn.close()


def check_callsign_status_changes(states, watchlist=None):
    """Notify for each watched callsign in this snapshot whose on_ground flag flipped
    since its previously stored state, tracking whichever aircraft currently flies it
    rather than a specific icao24. Must be called before the snapshot is inserted, for
    the same reason as check_status_changes.
    """
    watchlist = load_callsign_watchlist() if watchlist is None else watchlist
    if not watchlist or not states.get("states"):
        return

    snapshot_by_callsign = {}
    for state in states["states"]:
        callsign = (state[1] or "").strip().upper()
        if callsign in watchlist:
            snapshot_by_callsign[callsign] = state

    if not snapshot_by_callsign:
        return

    conn = get_connection()
    try:
        for callsign, state in snapshot_by_callsign.items():
            icao24 = state[0]
            previous = get_last_known_status_by_callsign(callsign, conn=conn)
            new_on_ground = state[8]

            if previous is None:
                logging.info(f"Callsign watchlist: first sighting of {callsign} ({icao24}), currently {'on the ground' if new_on_ground else 'airborne'}.")
                continue

            previous_on_ground = previous["status"] == "on ground"
            if previous_on_ground == new_on_ground:
                continue

            _send_status_change_notification(icao24, callsign, new_on_ground, conn)
    finally:
        conn.close()

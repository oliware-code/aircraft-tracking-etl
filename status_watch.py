import html
import json
import logging
import re
from pathlib import Path

import yaml

from db_connection import get_connection
from notify import send_notification
from queries import (
    DESTINATION_IATA,
    get_aircraft_info,
    get_airport_by_iata,
    get_friendly_name,
    get_last_known_status,
    get_last_known_status_by_callsign,
    get_route_for_callsign,
    haversine_km,
)

WATCHLIST_PATH = Path(__file__).with_name("notify_watchlist.yaml")
STALE_LANDING_STATE_PATH = Path(__file__).with_name("stale_landing_state.json")


def load_watchlist_with_names(path=WATCHLIST_PATH):
    """Return the watched_aircraft entries as an ordered list of (icao24,
    yaml_comment) tuples (deduplicated, first occurrence wins), preserving
    each entry's trailing "# comment" as a human-assigned name -- e.g.
    "- 8691aa  # ANA Pokemon" -> ("8691aa", "ANA Pokemon"). yaml_comment is
    None if an entry has no comment. PyYAML's safe_load discards comments
    entirely, so this reads the raw text instead; used as a fallback display
    name for aircraft the `aircraft` table doesn't have a friendly_name for
    yet (see get_named_aircraft_status)."""
    with open(path) as f:
        content = f.read()

    match = re.search(r"^watched_aircraft:\s*\n((?:[ \t]+.*\n?)*)", content, re.MULTILINE)
    block = match.group(1) if match else ""

    entries = []
    seen = set()
    for line in block.splitlines():
        item = re.match(r"\s*-\s*(\S+)\s*(?:#\s*(.*))?$", line)
        if not item:
            continue
        icao24 = item.group(1).strip().lower()
        comment = (item.group(2) or "").strip() or None
        if icao24 and icao24 not in seen:
            seen.add(icao24)
            entries.append((icao24, comment))
    return entries


def load_watchlist(path=WATCHLIST_PATH):
    """Return the watched icao24s in the same order they're listed in the YAML file
    (deduplicated, first occurrence wins)."""
    return [icao24 for icao24, _ in load_watchlist_with_names(path)]


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
    since its previously stored state -- but only for flights destined to MEX
    (DESTINATION_IATA). Departures are announced by check_aircraft_heading_to_mex
    instead (approach_alerts.py; richer message -- origin, ETA, emphatic non-AMX
    styling -- for the same takeoff), so this only ever sends the "has landed"
    confirmation, and only for MEX-bound arrivals. Non-MEX-bound flights on this
    watchlist stay silent here entirely. Must be called before the snapshot is
    inserted, since it compares against the last row already committed to `states`.
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

            if not new_on_ground:
                continue  # departure -- check_aircraft_heading_to_mex covers this

            route = get_route_for_callsign(callsign, conn=conn) if callsign else None
            if not route or route["iata_destination"] != DESTINATION_IATA:
                continue

            _send_status_change_notification(icao24, callsign, new_on_ground, conn)
    finally:
        conn.close()


def _send_first_detected_notification(icao24, callsign, on_ground, longitude, latitude, ground_speed, conn):
    """Build and send the "new flight instance" message for a watched
    callsign's new flight instance -- distinct wording/format from
    _send_status_change_notification since there's no prior state to
    describe a *change* from, just what we first saw it doing.

    ETA is a straight-line estimate to the route's destination based on
    current position/speed (same math as the approach alerts), not a
    turn-by-turn estimate -- shown as "--" whenever it can't be computed
    (already on the ground, missing position/speed, or no known route yet).
    """
    route = get_route_for_callsign(callsign, conn=conn) if callsign else None
    origin = route["iata_origin"] if route else None
    destination = route["iata_destination"] if route else None
    route_text = f"{origin}-{destination}" if origin and destination else "unknown route"

    info = get_aircraft_info(icao24, conn=conn) if icao24 else None
    registration = info["registration"] if info else None
    aircraft_text = f"{icao24}/{registration}" if registration else (icao24 or "?")

    eta_text = "--"
    if not on_ground and destination and longitude is not None and latitude is not None and ground_speed and ground_speed > 0:
        dest_airport = get_airport_by_iata(destination, conn=conn)
        if dest_airport and dest_airport["latitude"] is not None and dest_airport["longitude"] is not None:
            distance_km = haversine_km(latitude, longitude, dest_airport["latitude"], dest_airport["longitude"])
            eta_minutes = (distance_km / (ground_speed * 3.6)) * 60
            hours, minutes = divmod(int(round(eta_minutes)), 60)
            eta_text = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

    message = (
        f"🛫 New {html.escape(callsign)} instance ({route_text}) "
        f"| Aircraft {html.escape(aircraft_text)} | ETA: {eta_text}"
    )

    logging.info(f"Callsign watchlist: {message}")
    send_notification(message, parse_mode="HTML")


def check_callsign_status_changes(states, watchlist=None):
    """Notify for each watched callsign in this snapshot whose on_ground flag flipped
    since its previously stored state, tracking whichever aircraft currently flies it
    rather than a specific icao24. Must be called before the snapshot is inserted, for
    the same reason as check_status_changes.

    A callsign's very first flight instance -- where there's no prior state
    to compare against at all -- also gets a notification ("new flight
    detected", on the ground or airborne), rather than being silently
    skipped. Every flight after that keeps working via the normal
    landed/airborne transition detection below, so this only actually
    changes behavior for a genuinely new callsign (or right after a
    real gap where no prior state exists).
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
                longitude, latitude, ground_speed = state[5], state[6], state[9]
                _send_first_detected_notification(icao24, callsign, new_on_ground, longitude, latitude, ground_speed, conn)
                continue

            previous_on_ground = previous["status"] == "on ground"
            if previous_on_ground == new_on_ground:
                continue

            _send_status_change_notification(icao24, callsign, new_on_ground, conn)
    finally:
        conn.close()


def _load_stale_landing_state():
    if not STALE_LANDING_STATE_PATH.exists():
        return {}
    return json.loads(STALE_LANDING_STATE_PATH.read_text())


def _save_stale_landing_state(state):
    STALE_LANDING_STATE_PATH.write_text(json.dumps(state))


def check_stale_airborne_landings(icao24_watchlist=None, callsign_watchlist=None):
    """Notify for watched aircraft/callsigns that have effectively landed but
    will never trigger check_status_changes / check_callsign_status_changes,
    because their ADS-B feed went silent right at touchdown and never sent
    another state row with on_ground=true. Those two functions only ever look
    at whatever's in the *current* ingest snapshot, so an aircraft that stops
    transmitting entirely is invisible to them forever after its last report.

    This instead re-checks every watched icao24/callsign's last known reading
    each cycle, independent of the current snapshot, and relies on
    get_last_known_status's own stale/low-altitude inference (see
    _resolve_live_status in queries.py) to recognize a landing that on_ground
    itself never confirmed. Only fires for the *inferred* case
    (on_ground_raw is still False) -- a genuinely fresh on_ground=true row is
    already handled by the snapshot-driven checks above.

    Dedups via a local JSON file keyed on last-seen timestamp, since a silent
    aircraft's last row (and thus this exact reading) never changes -- without
    that, the same inferred landing would renotify every single cron cycle.
    """
    icao24_watchlist = load_watchlist() if icao24_watchlist is None else icao24_watchlist
    callsign_watchlist = load_callsign_watchlist() if callsign_watchlist is None else callsign_watchlist
    if not icao24_watchlist and not callsign_watchlist:
        return

    state = _load_stale_landing_state()
    changed = False
    conn = get_connection()
    try:
        for icao24 in icao24_watchlist:
            status = get_last_known_status(icao24, conn=conn)
            if _should_notify_stale_landing(state, f"icao24:{icao24}", status):
                changed = True
                # Same MEX-only restriction as check_status_changes for this
                # watchlist -- this is just a different detection path (inferred
                # silent landing) for the same "has landed" notification.
                route = get_route_for_callsign(status["callsign"], conn=conn) if status["callsign"] else None
                if route and route["iata_destination"] == DESTINATION_IATA:
                    _send_status_change_notification(icao24, status["callsign"], True, conn)

        for callsign in callsign_watchlist:
            status = get_last_known_status_by_callsign(callsign, conn=conn)
            if _should_notify_stale_landing(state, f"callsign:{callsign}", status):
                _send_status_change_notification(status["icao24"], callsign, True, conn)
                changed = True
    finally:
        conn.close()

    if changed:
        _save_stale_landing_state(state)


def _should_notify_stale_landing(state, key, status):
    if status is None or status["status"] != "on ground" or status["on_ground_raw"]:
        return False  # not currently seen, still genuinely airborne, or a real (not inferred) landing

    last_seen_iso = status["last_seen"].isoformat()
    if state.get(key) == last_seen_iso:
        return False  # already notified for this exact reading

    state[key] = last_seen_iso
    return True

import html
import logging

from db_connection import get_connection
from notify import send_notification
from queries import get_aircraft_info, get_airport_by_iata, get_friendly_name, get_route_for_callsign, get_status_since, haversine_km
from status_watch import load_callsign_watchlist, load_watchlist

APPROACH_THRESHOLD_MINUTES = 10
DESTINATION_IATA = "MEX"

# Reused from the old one-shot approach-alert dedup (removed when approach
# alerts became repeating) for a different purpose now: deduping the
# one-shot "heading to MEX" detection alert below. Same shape fits either
# use (icao24 + the flight instance's start timestamp), so no schema change.
UPSERT_MEX_BOUND_ALERT = """
    INSERT INTO approach_alerts_sent (icao24, flight_started_at, notified_at)
    VALUES (%s, %s, now())
    ON CONFLICT (icao24) DO UPDATE SET flight_started_at = EXCLUDED.flight_started_at, notified_at = now()
"""
GET_MEX_BOUND_ALERTED_FLIGHT = """
    SELECT flight_started_at FROM approach_alerts_sent WHERE icao24 = %s
"""


def _already_alerted_mex_bound(icao24, flight_started_at, conn):
    cur = conn.cursor()
    cur.execute(GET_MEX_BOUND_ALERTED_FLIGHT, (icao24,))
    row = cur.fetchone()
    return row is not None and row[0] == flight_started_at


def _is_non_amx(callsign):
    """True for any watched_aircraft callsign that isn't Aeromexico -- rarer/
    more noteworthy on this watchlist, so those alerts get emphatic styling."""
    return callsign is not None and not callsign.upper().startswith("AMX")


def _label_for(icao24, callsign, conn):
    friendly_name = get_friendly_name(icao24, conn=conn) if icao24 else None
    identifier = f"{html.escape(callsign)} / {icao24}" if callsign else icao24
    return f"<b>{html.escape(friendly_name)}</b> ({identifier})" if friendly_name else identifier


def check_aircraft_heading_to_mex(states, conn=None):
    """For each watched_aircraft (icao24 list) whose current callsign
    resolves to a route destined for MEX, send exactly one notification per
    flight instance -- as soon as the route is known, not gated on distance
    like the approach alerts below. Includes origin, destination, and a
    rough ETA if position/speed are already available. Non-AMX aircraft
    (i.e. not Aeromexico) get emphatic styling, since those are
    comparatively rarer on this watchlist.

    Deduped via approach_alerts_sent, keyed by icao24 + the flight's start
    timestamp from get_status_since, so it only ever fires once per flight
    even though this runs every cron cycle. Must be called before the
    snapshot is inserted, since it relies on get_status_since reflecting the
    state prior to this exact snapshot.
    """
    icao24_watchlist = load_watchlist()
    if not icao24_watchlist or not states.get("states"):
        return

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        for state in states["states"]:
            icao24 = state[0]
            callsign = (state[1] or "").strip().upper() or None
            on_ground = state[8]

            if on_ground or not callsign or icao24 not in icao24_watchlist:
                continue

            route = get_route_for_callsign(callsign, conn=conn)
            if not route or route["iata_destination"] != DESTINATION_IATA:
                continue

            since = get_status_since(icao24, conn=conn)
            if since is None:
                continue
            flight_started_at = since["since"]

            if _already_alerted_mex_bound(icao24, flight_started_at, conn):
                continue

            longitude, latitude, ground_speed = state[5], state[6], state[9]
            eta_text = ""
            if longitude is not None and latitude is not None and ground_speed and ground_speed > 0:
                destination = get_airport_by_iata(DESTINATION_IATA, conn=conn)
                if destination and destination["latitude"] is not None and destination["longitude"] is not None:
                    distance_km = haversine_km(latitude, longitude, destination["latitude"], destination["longitude"])
                    eta_minutes = (distance_km / (ground_speed * 3.6)) * 60
                    eta_text = f", ETA ~{eta_minutes:.0f} min"

            label = _label_for(icao24, callsign, conn)
            origin_text = route["iata_origin"] or "an unknown origin"
            prefix = "🚨 " if _is_non_amx(callsign) else ""
            message = f"{prefix}✈️ {label} is heading to {DESTINATION_IATA} from {origin_text}{eta_text}."

            logging.info(f"MEX-bound flight detected: {message}")
            send_notification(message, parse_mode="HTML")

            cur = conn.cursor()
            cur.execute(UPSERT_MEX_BOUND_ALERT, (icao24, flight_started_at))
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def _check_approach(state, destination_iata, emphatic, conn):
    """Core approach-alert check for one state against a specific
    destination airport. Sends+logs a notification if the state qualifies
    (airborne, valid position/speed, ETA within APPROACH_THRESHOLD_MINUTES).
    Caller is responsible for watchlist membership and resolving
    destination_iata -- this only handles the physical approach math."""
    icao24 = state[0]
    callsign = (state[1] or "").strip().upper() or None
    on_ground = state[8]
    if on_ground or not callsign:
        return

    longitude, latitude, ground_speed = state[5], state[6], state[9]
    if longitude is None or latitude is None or not ground_speed or ground_speed <= 0:
        return

    destination = get_airport_by_iata(destination_iata, conn=conn)
    if not destination or destination["latitude"] is None or destination["longitude"] is None:
        return

    distance_km = haversine_km(latitude, longitude, destination["latitude"], destination["longitude"])
    eta_minutes = (distance_km / (ground_speed * 3.6)) * 60
    if not (0 < eta_minutes <= APPROACH_THRESHOLD_MINUTES):
        return

    info = get_aircraft_info(icao24, conn=conn) if icao24 else None
    registration = info["registration"] if info else None
    aircraft_text = f"{icao24}/{registration}" if registration else (icao24 or "?")

    hours, minutes = divmod(int(round(eta_minutes)), 60)
    eta_text = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

    prefix = "🚨 " if emphatic and _is_non_amx(callsign) else ""
    message = f"{prefix}🛬 {html.escape(callsign)} is approaching {destination_iata} | {html.escape(aircraft_text)} | ETA {eta_text}"

    logging.info(f"Approach alert: {message}")
    send_notification(message, parse_mode="HTML")


def check_aircraft_approach_alerts(states, conn=None):
    """Approach alerts for watched_aircraft (icao24 list) -- destination is
    always MEX for this list. Non-AMX aircraft get emphatic styling. Fires
    on every qualifying state (ETA updates each message) from the first
    approach detection through touchdown, not just once.
    """
    icao24_watchlist = load_watchlist()
    if not icao24_watchlist or not states.get("states"):
        return

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        for state in states["states"]:
            if state[0] not in icao24_watchlist:
                continue
            callsign = (state[1] or "").strip().upper() or None
            if not callsign:
                continue
            route = get_route_for_callsign(callsign, conn=conn)
            if not route or route["iata_destination"] != DESTINATION_IATA:
                continue
            _check_approach(state, DESTINATION_IATA, emphatic=True, conn=conn)
    finally:
        if owns_conn:
            conn.close()


def check_callsign_approach_alerts(states, conn=None):
    """Approach alerts for watched_callsigns -- destination is whatever that
    callsign's currently-assigned route says (dynamic, unlike the icao24
    list above which is fixed to MEX), so this works for any callsign/
    destination without code changes. No emphatic styling. Fires on every
    qualifying state (ETA updates each message) from the first approach
    detection through touchdown, not just once.
    """
    callsign_watchlist = load_callsign_watchlist()
    if not callsign_watchlist or not states.get("states"):
        return

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        for state in states["states"]:
            callsign = (state[1] or "").strip().upper() or None
            if not callsign or callsign not in callsign_watchlist:
                continue
            route = get_route_for_callsign(callsign, conn=conn)
            if not route or not route.get("iata_destination"):
                continue
            _check_approach(state, route["iata_destination"], emphatic=False, conn=conn)
    finally:
        if owns_conn:
            conn.close()

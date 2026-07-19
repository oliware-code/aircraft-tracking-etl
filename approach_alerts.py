import html
import logging

from db_connection import get_connection
from notify import send_notification
from queries import get_airport_by_iata, get_friendly_name, get_route_for_callsign, get_status_since, haversine_km
from status_watch import load_callsign_watchlist, load_watchlist

APPROACH_THRESHOLD_MINUTES = 10
DESTINATION_IATA = "MEX"

UPSERT_ALERT = """
    INSERT INTO approach_alerts_sent (icao24, flight_started_at, notified_at)
    VALUES (%s, %s, now())
    ON CONFLICT (icao24) DO UPDATE SET flight_started_at = EXCLUDED.flight_started_at, notified_at = now()
"""
GET_ALERTED_FLIGHT = """
    SELECT flight_started_at FROM approach_alerts_sent WHERE icao24 = %s
"""


def _already_alerted(icao24, flight_started_at, conn):
    cur = conn.cursor()
    cur.execute(GET_ALERTED_FLIGHT, (icao24,))
    row = cur.fetchone()
    return row is not None and row[0] == flight_started_at


def check_approach_alerts(states, conn=None):
    """Notify when a watched aircraft or callsign is estimated to be within
    APPROACH_THRESHOLD_MINUTES of landing at DESTINATION_IATA, based on
    great-circle distance to that airport and current ground speed. Only ever
    fires once per flight (tracked via approach_alerts_sent, keyed by icao24 and
    the flight's start timestamp from get_status_since), not once per cron cycle
    for the whole descent.

    Must be called before the snapshot is inserted, since it relies on
    get_status_since reflecting the state prior to this exact snapshot.
    """
    icao24_watchlist = load_watchlist()
    callsign_watchlist = load_callsign_watchlist()
    if not icao24_watchlist and not callsign_watchlist:
        return
    if not states.get("states"):
        return

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        for state in states["states"]:
            icao24 = state[0]
            callsign = (state[1] or "").strip().upper() or None
            on_ground = state[8]

            if on_ground or not callsign:
                continue
            if icao24 not in icao24_watchlist and callsign not in callsign_watchlist:
                continue

            longitude, latitude, ground_speed = state[5], state[6], state[9]
            if longitude is None or latitude is None or not ground_speed or ground_speed <= 0:
                continue

            route = get_route_for_callsign(callsign, conn=conn)
            if not route or route["iata_destination"] != DESTINATION_IATA:
                continue

            destination = get_airport_by_iata(DESTINATION_IATA, conn=conn)
            if not destination or destination["latitude"] is None or destination["longitude"] is None:
                continue

            distance_km = haversine_km(latitude, longitude, destination["latitude"], destination["longitude"])
            eta_minutes = (distance_km / (ground_speed * 3.6)) * 60
            if not (0 < eta_minutes <= APPROACH_THRESHOLD_MINUTES):
                continue

            since = get_status_since(icao24, conn=conn)
            if since is None:
                continue
            flight_started_at = since["since"]

            if _already_alerted(icao24, flight_started_at, conn):
                continue

            friendly_name = get_friendly_name(icao24, conn=conn)
            identifier = f"{html.escape(callsign)} / {icao24}"
            label = f"<b>{html.escape(friendly_name)}</b> ({identifier})" if friendly_name else identifier
            message = f"🛬 {label} is approaching {DESTINATION_IATA} (ETA ~{eta_minutes:.0f} min)."

            logging.info(f"Approach alert: {message}")
            send_notification(message, parse_mode="HTML")

            cur = conn.cursor()
            cur.execute(UPSERT_ALERT, (icao24, flight_started_at))
            conn.commit()
    finally:
        if owns_conn:
            conn.close()

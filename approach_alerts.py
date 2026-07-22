import html
import logging

from db_connection import get_connection
from notify import send_notification
from queries import get_airport_by_iata, get_friendly_name, get_route_for_callsign, haversine_km
from status_watch import load_callsign_watchlist, load_watchlist

APPROACH_THRESHOLD_MINUTES = 10
DESTINATION_IATA = "MEX"


def check_approach_alerts(states, conn=None):
    """Notify on every snapshot where a watched aircraft or callsign is
    estimated to be within APPROACH_THRESHOLD_MINUTES of landing at
    DESTINATION_IATA, based on great-circle distance to that airport and
    current ground speed. Fires on every matching state (ETA updates each
    message) from the first one that crosses the threshold through the last
    one before touchdown, not just once -- naturally stops the moment the
    aircraft lands, since on_ground=true is skipped below.
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

            friendly_name = get_friendly_name(icao24, conn=conn)
            identifier = f"{html.escape(callsign)} / {icao24}"
            label = f"<b>{html.escape(friendly_name)}</b> ({identifier})" if friendly_name else identifier
            message = f"🛬 {label} is approaching {DESTINATION_IATA} (ETA ~{eta_minutes:.0f} min)."

            logging.info(f"Approach alert: {message}")
            send_notification(message, parse_mode="HTML")
    finally:
        if owns_conn:
            conn.close()

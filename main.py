import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

from approach_alerts import check_aircraft_approach_alerts, check_aircraft_heading_to_mex, check_callsign_approach_alerts
from credentials import CLIENT_ID, CLIENT_SECRET
from db_connection import get_connection
from opensky_health import check_opensky_health
from route_enrichment import resolve_aircraft, resolve_route
from status_watch import check_callsign_status_changes, check_stale_airborne_landings, check_status_changes

# --- CONFIG ---
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_URL = "https://opensky-network.org/api/states/all"
CACHE_FILE = "opensky_token.json"
LOG_DIR = "logs"

def epoch_to_utc(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


INSERT_AIRCRAFT = """ INSERT INTO aircraft(icao24, origin_country) VALUES (%s, %s) ON CONFLICT DO NOTHING; """
INSERT_CALLSIGN = """ INSERT INTO flight_routes(callsign) VALUES(%s) ON CONFLICT DO NOTHING """
INSERT_STATE = """ INSERT INTO states(timestamp, icao24, callsign, time_position, last_contact, longitude, latitude, barometric_altitude, on_ground, ground_speed, true_track, vertical_rate, sensors, geo_altitude, squawk, spi, position_source) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) """


def configure_logging():
    """Log to a weekly rotating file and to stdout (visible in cron mail / manual runs)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now()
    log_filename = f"{now.year}-{now.isocalendar().week:02d}.log"
    log_path = os.path.join(LOG_DIR, log_filename)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )


class CachedOpenSkyClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self.last_error = None

    def _get_new_token(self):
        """Perform the OAuth2 exchange and save to disk."""
        logging.info("🔑 Token expired or missing. Requesting new one...")
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "openid"
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = requests.post(TOKEN_URL, data=payload, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()
        token_info = {
            "access_token": data["access_token"],
            # Calculate absolute expiry time (Unix timestamp)
            "expires_at": time.time() + data["expires_in"]
        }

        with open(CACHE_FILE, "w") as f:
            json.dump(token_info, f)

        return token_info

    def get_valid_token(self):
        """Loads from cache or refreshes if necessary."""
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                token_info = json.load(f)

            # Use a 60-second buffer to be safe
            if time.time() < (token_info["expires_at"] - 60):
                return token_info["access_token"]

        return self._get_new_token()["access_token"]

    def get_all_states(self):
        token = self.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = self.session.get(API_URL, headers=headers, timeout=15)
            logging.info(f"📡 API Call: {response.status_code} | Credits: {response.headers.get('X-Rate-Limit-Remaining')}")

            if response.status_code == 200:
                self.last_error = None
                return response.json()
            elif response.status_code == 401:
                # Force a token refresh next time if we get a 401
                if os.path.exists(CACHE_FILE):
                    os.remove(CACHE_FILE)
                logging.warning("⚠️ Token rejected. Cache cleared.")

            response.raise_for_status()
        except Exception as e:
            logging.error(f"❌ API Error: {e}")
            self.last_error = str(e)
        return None


def ingest_snapshot(states, db_connection):
    """Insert one API snapshot: new aircraft, new callsigns, and all state vectors."""
    db_cursor = db_connection.cursor()
    aircraft_counter = 0
    callsign_counter = 0
    states_counter = 0
    for state in states["states"]:
        icao24 = state[0]
        callsign = (state[1] or "").strip() or None
        state_values = (
            epoch_to_utc(states["time"]),  # timestamp (unique for all states)
            icao24,
            callsign,
            epoch_to_utc(state[3]),  # time_position
            epoch_to_utc(state[4]),  # last_contact
            state[5],  # longitude
            state[6],  # latitude
            state[7],  # barometric_altitude
            state[8],  # on_ground
            state[9] if state[9] else None,  # ground_speed
            state[10],  # true_track
            state[11],  # vertical_rate
            state[12],  # sensors (psycopg2 handles list -> int[] automatically)
            state[13],  # geo_altitude
            state[14],  # squawk
            state[15],  # spi
            state[16],  # position_source
        )

        db_cursor.execute(INSERT_AIRCRAFT, (icao24, state[2]))
        if db_cursor.rowcount > 0:
            aircraft_counter += 1
            logging.info(f"Aircraft inserted: {icao24}, {state[2]}")
            # Only a genuinely new icao24 triggers an adsbdb lookup, same reasoning
            # as the callsign enrichment below.
            try:
                resolve_aircraft(icao24, db_connection)
            except Exception as e:
                logging.error(f"❌ Aircraft enrichment error for {icao24}: {e}")
        if callsign:
            db_cursor.execute(INSERT_CALLSIGN, (callsign,))
            if db_cursor.rowcount > 0:
                callsign_counter += 1
                logging.info(f"Route inserted: {callsign}")
                # Only a genuinely new callsign triggers an adsbdb lookup, so a normal
                # cycle (mostly already-known callsigns) makes zero extra API calls.
                try:
                    resolve_route(callsign, db_connection)
                except Exception as e:
                    logging.error(f"❌ Route enrichment error for {callsign}: {e}")
        db_cursor.execute(INSERT_STATE, state_values)
        if db_cursor.rowcount > 0:
            states_counter += 1
    # app.py's background thread LISTENs on this channel and pushes a live-refresh
    # event to connected browsers; committed in the same transaction as the insert
    # so listeners never fire ahead of the data actually being visible.
    db_cursor.execute("NOTIFY new_snapshot;")
    db_connection.commit()
    db_cursor.close()
    logging.info(f"New aircraft: {aircraft_counter}")
    logging.info(f"New callsign: {callsign_counter}")
    logging.info(f"States inserted: {states_counter}")


if __name__ == "__main__":
    configure_logging()
    client = CachedOpenSkyClient(CLIENT_ID, CLIENT_SECRET)
    states = client.get_all_states()
    try:
        check_opensky_health(states is not None, client.last_error)
    except Exception as e:
        logging.error(f"❌ OpenSky health-check error: {e}")
    if states and states["states"]:
        logging.info(f"Snapshot timestamp: {epoch_to_utc(states['time'])}")
        try:
            check_status_changes(states)
        except Exception as e:
            logging.error(f"❌ Watchlist notification error: {e}")
        try:
            check_callsign_status_changes(states)
        except Exception as e:
            logging.error(f"❌ Callsign watchlist notification error: {e}")
        try:
            # Independent of this snapshot's contents -- catches aircraft whose
            # ADS-B went silent right at touchdown, which the two checks above
            # structurally can't (they only react to rows in the current poll).
            check_stale_airborne_landings()
        except Exception as e:
            logging.error(f"❌ Stale-airborne-landing notification error: {e}")
        try:
            check_aircraft_heading_to_mex(states)
        except Exception as e:
            logging.error(f"❌ MEX-bound flight detection error: {e}")
        try:
            check_aircraft_approach_alerts(states)
        except Exception as e:
            logging.error(f"❌ Aircraft approach alert error: {e}")
        try:
            check_callsign_approach_alerts(states)
        except Exception as e:
            logging.error(f"❌ Callsign approach alert error: {e}")
        db_connection = get_connection()
        try:
            ingest_snapshot(states, db_connection)
        finally:
            db_connection.close()
    else:
        logging.warning("No states received; nothing to ingest.")

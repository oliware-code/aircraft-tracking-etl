import requests
import pause
import time
from psycopg2.errors import ForeignKeyViolation
import psycopg2
from datetime import datetime, timedelta
import json
import os
from credentials import CLIENT_ID, CLIENT_SECRET
from db_config import config


# --- CREDENTIALS ---
# CLIENT_ID = credentials.CLIENT_ID
# CLIENT_SECRET = credentials.CLIENT_SECRET

# --- CONFIG ---
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_URL = "https://opensky-network.org/api/states/all"
CACHE_FILE = "opensky_token.json"


def connect_database():
    connection = None
    try:
        params = config()
        # print("Connecting to database...")
        connection = psycopg2.connect(**params)
        return connection
        # cursor = connection.cursor()
        # print('PostgreSQL version: ')
        # cursor.execute('SELECT version();')
        # db_version = cursor.fetchone()
        # print(db_version)
        # cursor.close()
    except(Exception, psycopg2.DatabaseError) as error:
        print(error)
    finally:
        if connection is not None:
            pass
            # connection.close()
            # else:
            #     connection.close()
            # print('Database connection closed.')



class CachedOpenSkyClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()

    def _get_new_token(self):
        """Perform the OAuth2 exchange and save to disk."""
        print("🔑 Token expired or missing. Requesting new one...")
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

    def get_states(self):
        token = self.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = self.session.get(API_URL, headers=headers, timeout=15)
            # Log credits for your Debian system monitoring
            print(f"📡 API Call: {response.status_code} | Credits: {response.headers.get('X-Rate-Limit-Remaining')}")

            if response.status_code == 200:
                return response.json().get("states", [])
            elif response.status_code == 401:
                # Force a token refresh next time if we get a 401
                if os.path.exists(CACHE_FILE): os.remove(CACHE_FILE)
                print("⚠️ Token rejected. Cache cleared.")

            response.raise_for_status()
        except Exception as e:
            print(f"❌ API Error: {e}")
        return None


# --- LOOP LOGIC ---
if __name__ == "__main__":
    db_connection = connect_database()
    print(db_connection)
    db_cursor = db_connection.cursor()
    insert_aircraft_query = ''' INSERT INTO aircraft(icao24, origin_country) VALUES (%s, %s) ON CONFLICT DO NOTHING; '''
    insert_callsigh_query = ''' INSERT INTO flight_routes(callsign) VALUES(%s) ON CONFLICT DO NOTHING'''
    client = CachedOpenSkyClient(CLIENT_ID, CLIENT_SECRET)

    lufthansa747 = "3c4b2e"

    states = client.get_states()

    # Quick check for the Queen (D-ABYN)
    if states:
        for state in states:
            # print(state)
            db_cursor.execute(insert_aircraft_query, (state[0], state[2]))
            inserted_aircraft = db_cursor.rowcount
            if inserted_aircraft > 0:
                print(f"{inserted_aircraft} new aircraft inserted: {state[0]} {state[2]}")
            db_cursor.execute(insert_callsigh_query, (state[1].strip(),))
            inserted_route = db_cursor.rowcount
            if inserted_route > 0:
                print(f"{inserted_route}: new route inserted {state[1].strip()}")
        db_connection.commit()

        queen = next((s for s in states if s[0] == lufthansa747), None)
        status = f"AT {queen[6]}, {queen[5]}" if queen else "NOT SEEN"
        print(f"👑 D-ABYN Status: {status}")
    db_cursor.close()
    db_connection.close()

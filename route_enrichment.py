import time

import requests

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}"
# adsbdb's own rate limiter (per source IP) allows 512 requests per 60s before a
# temporary ban. 3/sec keeps this comfortably under that even if the live path
# (main.py, via cron) and a manually-run backfill happen to overlap, since each
# process throttles independently in-memory: 3/sec x 2 processes = 360/60s, still
# well under the 512 threshold.
MIN_SECONDS_BETWEEN_CALLS = 1.0 / 3

UPSERT_AIRLINE = """
    INSERT INTO airlines (icao, iata, name, radio_callsign, country, country_iso)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (icao) DO NOTHING
"""
UPSERT_AIRPORT = """
    INSERT INTO airports (iata, icao, name, municipality, country, country_iso, latitude, longitude, elevation)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (iata) DO NOTHING
"""
UPDATE_FLIGHT_ROUTE = """
    UPDATE flight_routes
    SET operator = %s,
        iata_origin = %s,
        iata_destination = %s,
        icao_origin = %s,
        icao_destination = %s,
        enrichment_checked_at = now()
    WHERE callsign = %s
"""
MARK_CHECKED = """
    UPDATE flight_routes SET enrichment_checked_at = now() WHERE callsign = %s
"""
UPDATE_AIRCRAFT = """
    UPDATE aircraft
    SET aircraft_type = %s,
        icao_aircraft_type = %s,
        manufacturer = %s,
        registration = %s,
        registered_owner_country_iso_name = %s,
        enrichment_checked_at = now()
    WHERE icao24 = %s
"""
MARK_AIRCRAFT_CHECKED = """
    UPDATE aircraft SET enrichment_checked_at = now() WHERE icao24 = %s
"""

_last_call_time = 0.0


def _throttle():
    """Block as needed so calls to adsbdb never happen faster than
    MIN_SECONDS_BETWEEN_CALLS, regardless of which caller (backfill script, live
    route enrichment, or live aircraft enrichment) is asking."""
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
    _last_call_time = time.monotonic()


def _fetch_flightroute(callsign):
    """Return the adsbdb flightroute dict, or None if adsbdb has no route for this callsign."""
    _throttle()
    response = requests.get(ADSBDB_URL.format(callsign=callsign), timeout=10)
    if response.status_code == 404:
        return None
    if response.status_code == 400:
        # adsbdb rejects malformed callsigns (e.g. embedded spaces) outright rather
        # than 404ing; treat that the same as "no data" so it doesn't retry forever.
        return None
    response.raise_for_status()
    data = response.json()
    envelope = data.get("response")
    if not isinstance(envelope, dict):
        # adsbdb returns a bare string like "unknown callsign" instead of 404 sometimes.
        return None
    return envelope.get("flightroute")


def _fetch_aircraft(icao24):
    """Return the adsbdb aircraft dict, or None if adsbdb has no record for this icao24."""
    _throttle()
    response = requests.get(ADSBDB_AIRCRAFT_URL.format(icao24=icao24), timeout=10)
    if response.status_code in (404, 400):
        return None
    response.raise_for_status()
    data = response.json()
    envelope = data.get("response")
    if not isinstance(envelope, dict):
        return None
    return envelope.get("aircraft")


def _airport_row(airport):
    if not airport or not airport.get("iata_code"):
        # airports.iata is NOT NULL; skip airports adsbdb didn't give an IATA code for.
        return None
    return (
        airport["iata_code"],
        airport.get("icao_code"),
        airport.get("name"),
        airport.get("municipality"),
        airport.get("country_name"),
        airport.get("country_iso_name"),
        airport.get("latitude"),
        airport.get("longitude"),
        airport.get("elevation"),
    )


def resolve_route(callsign, conn):
    """Look up `callsign` via adsbdb and store whatever it has: the airline, the
    origin/destination airports, and the route's own fields on flight_routes.

    Does not commit — the caller owns the transaction (main.py folds this into its
    per-snapshot commit; the backfill script commits once per callsign it processes).
    Returns a dict describing what was found, for callers that want to report it.
    """
    flightroute = _fetch_flightroute(callsign)
    cur = conn.cursor()

    if flightroute is None:
        cur.execute(MARK_CHECKED, (callsign,))
        return {"found": False}

    airline = flightroute.get("airline") or {}
    origin = flightroute.get("origin") or {}
    destination = flightroute.get("destination") or {}

    airline_inserted = False
    if airline.get("icao"):
        cur.execute(
            UPSERT_AIRLINE,
            (
                airline["icao"],
                airline.get("iata"),
                airline.get("name"),
                airline.get("callsign"),
                airline.get("country"),
                airline.get("country_iso"),
            ),
        )
        airline_inserted = cur.rowcount > 0

    origin_row = _airport_row(origin)
    origin_inserted = False
    if origin_row:
        cur.execute(UPSERT_AIRPORT, origin_row)
        origin_inserted = cur.rowcount > 0

    destination_row = _airport_row(destination)
    destination_inserted = False
    if destination_row:
        cur.execute(UPSERT_AIRPORT, destination_row)
        destination_inserted = cur.rowcount > 0

    cur.execute(
        UPDATE_FLIGHT_ROUTE,
        (
            airline.get("name"),
            origin.get("iata_code"),
            destination.get("iata_code"),
            origin.get("icao_code"),
            destination.get("icao_code"),
            callsign,
        ),
    )

    return {
        "found": True,
        "airline_name": airline.get("name"),
        "airline_inserted": airline_inserted,
        "origin_name": origin.get("name"),
        "origin_inserted": origin_inserted,
        "destination_name": destination.get("name"),
        "destination_inserted": destination_inserted,
    }


def resolve_aircraft(icao24, conn):
    """Look up `icao24` via adsbdb and store whatever it has: aircraft_type,
    icao_aircraft_type, manufacturer, registration, and
    registered_owner_country_iso_name on the aircraft table. Never touches
    friendly_name or origin_country -- friendly_name is user-curated and
    origin_country comes from OpenSky's own state vectors, not adsbdb.

    Does not commit -- caller owns the transaction, same as resolve_route.
    """
    aircraft = _fetch_aircraft(icao24)
    cur = conn.cursor()

    if aircraft is None:
        cur.execute(MARK_AIRCRAFT_CHECKED, (icao24,))
        return {"found": False}

    cur.execute(
        UPDATE_AIRCRAFT,
        (
            aircraft.get("type"),
            aircraft.get("icao_type"),
            aircraft.get("manufacturer"),
            aircraft.get("registration"),
            aircraft.get("registered_owner_country_iso_name"),
            icao24,
        ),
    )

    return {
        "found": True,
        "registration": aircraft.get("registration"),
        "manufacturer": aircraft.get("manufacturer"),
        "aircraft_type": aircraft.get("type"),
    }

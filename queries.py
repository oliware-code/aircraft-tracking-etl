from datetime import datetime, timezone

from db_connection import get_connection


def get_icao24_by_callsign(callsign):
    """Return all distinct icao24 addresses seen in `states` under the given callsign."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT icao24
                FROM states
                WHERE TRIM(UPPER(callsign)) = TRIM(UPPER(%s));
                """,
                (callsign,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def get_callsigns_by_icao24(icao24):
    """Return all distinct callsigns (routes/flights) seen in `states` for the given icao24."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT callsign
                FROM states
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                  AND callsign IS NOT NULL;
                """,
                (icao24,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def get_last_known_status(icao24, conn=None):
    """Return the most recent state row for the given icao24, or None if never seen.

    Pass an existing `conn` to reuse it (e.g. across several calls in one request)
    instead of opening a fresh connection.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, callsign, longitude, latitude, barometric_altitude,
                       geo_altitude, on_ground, ground_speed, vertical_rate, true_track,
                       squawk
                FROM states
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                ORDER BY timestamp DESC
                LIMIT 1;
                """,
                (icao24,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            (
                timestamp, callsign, longitude, latitude, barometric_altitude,
                geo_altitude, on_ground, ground_speed, vertical_rate, true_track,
                squawk,
            ) = row

            return {
                "icao24": icao24,
                "callsign": callsign.strip() if callsign else None,
                "last_seen": datetime.fromtimestamp(timestamp, tz=timezone.utc),
                "status": "on ground" if on_ground else "airborne",
                "longitude": longitude,
                "latitude": latitude,
                "altitude": geo_altitude if geo_altitude is not None else barometric_altitude,
                "ground_speed": ground_speed,
                "vertical_rate": vertical_rate,
                "true_track": true_track,
                "squawk": squawk,
            }
    finally:
        if owns_conn:
            conn.close()


def get_position_history(icao24, days=7, bucket_seconds=300):
    """Return a sampled position trail for icao24 covering `days` before its last known timestamp.

    Points are bucketed to one per `bucket_seconds` to keep the trail light enough to plot.
    """
    last_status = get_last_known_status(icao24)
    if last_status is None:
        return []

    end_ts = int(last_status["last_seen"].timestamp())
    start_ts = end_ts - days * 86400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (timestamp / %s)
                       timestamp, callsign, longitude, latitude, geo_altitude, on_ground
                FROM states
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                  AND timestamp BETWEEN %s AND %s
                  AND longitude IS NOT NULL
                  AND latitude IS NOT NULL
                ORDER BY timestamp / %s, timestamp;
                """,
                (bucket_seconds, icao24, start_ts, end_ts, bucket_seconds),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    rows.sort(key=lambda r: r[0])
    return [
        {
            "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc),
            "callsign": callsign.strip() if callsign else None,
            "longitude": longitude,
            "latitude": latitude,
            "altitude": altitude,
            "on_ground": on_ground,
        }
        for ts, callsign, longitude, latitude, altitude, on_ground in rows
    ]


def get_current_flight_trail(icao24, conn=None, status=None, since=None):
    """Return the position trail since the aircraft's last takeoff, if it is currently airborne.

    Empty list if the aircraft is on the ground or has never flown. Pass already-known
    `status` / `since` (and a shared `conn`) to avoid re-fetching them when the caller
    has just computed them itself.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        if status is None:
            status = get_last_known_status(icao24, conn=conn)
        if status is None or status["status"] == "on ground":
            return []

        if since is None:
            since = get_status_since(icao24, conn=conn)
        if since is None or since["on_ground"]:
            return []

        start_ts = int(since["since"].timestamp())
        end_ts = int(status["last_seen"].timestamp())

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, callsign, longitude, latitude, geo_altitude, on_ground
                FROM states
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                  AND timestamp BETWEEN %s AND %s
                  AND longitude IS NOT NULL
                  AND latitude IS NOT NULL
                ORDER BY timestamp;
                """,
                (icao24, start_ts, end_ts),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc),
            "callsign": callsign.strip() if callsign else None,
            "longitude": longitude,
            "latitude": latitude,
            "altitude": altitude,
            "on_ground": on_ground,
        }
        for ts, callsign, longitude, latitude, altitude, on_ground in rows
    ]


def get_status_since(icao24, conn=None):
    """Return when the aircraft's current on_ground status began (i.e. the last state flip)."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, on_ground
                FROM (
                    SELECT timestamp, on_ground,
                           on_ground IS DISTINCT FROM LAG(on_ground) OVER (ORDER BY timestamp) AS changed
                    FROM states
                    WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                ) t
                WHERE changed
                ORDER BY timestamp DESC
                LIMIT 1;
                """,
                (icao24,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None

    since_ts, on_ground = row
    return {
        "on_ground": on_ground,
        "since": datetime.fromtimestamp(since_ts, tz=timezone.utc),
    }


def get_named_aircraft_status(conn=None):
    """Return current status for every aircraft in `aircraft` with a non-null friendly_name."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT icao24, friendly_name, registration, aircraft_type, manufacturer
                FROM aircraft
                WHERE friendly_name IS NOT NULL
                ORDER BY friendly_name;
                """
            )
            aircraft_rows = cur.fetchall()

        results = []
        for icao24, friendly_name, registration, aircraft_type, manufacturer in aircraft_rows:
            status = get_last_known_status(icao24, conn=conn)
            results.append(
                {
                    "icao24": icao24,
                    "friendly_name": friendly_name,
                    "registration": registration,
                    "aircraft_type": aircraft_type,
                    "manufacturer": manufacturer,
                    "status": status,
                }
            )
    finally:
        if owns_conn:
            conn.close()
    return results


def get_route_for_callsign(callsign, conn=None):
    """Return {iata_origin, iata_destination, operator} for a callsign's route, or None."""
    if not callsign:
        return None

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT iata_origin, iata_destination, operator
                FROM flight_routes
                WHERE TRIM(UPPER(callsign)) = TRIM(UPPER(%s))
                LIMIT 1;
                """,
                (callsign,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None

    iata_origin, iata_destination, operator = row
    return {
        "iata_origin": iata_origin,
        "iata_destination": iata_destination,
        "operator": operator,
    }


def get_latest_snapshot():
    """Return every aircraft detected in the single most recent states timestamp.

    Returns (fetched_at, aircraft_list). fetched_at is None and the list empty
    if the states table has no rows.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM states;")
            max_ts = cur.fetchone()[0]
            if max_ts is None:
                return None, []

            cur.execute(
                """
                SELECT icao24, callsign, longitude, latitude, barometric_altitude,
                       geo_altitude, on_ground, ground_speed, true_track
                FROM states
                WHERE timestamp = %s
                  AND longitude IS NOT NULL
                  AND latitude IS NOT NULL;
                """,
                (max_ts,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    aircraft = [
        {
            "icao24": icao24,
            "callsign": callsign.strip() if callsign else None,
            "longitude": longitude,
            "latitude": latitude,
            "altitude": geo_altitude if geo_altitude is not None else barometric_altitude,
            "on_ground": on_ground,
            "ground_speed": ground_speed,
            "true_track": true_track,
        }
        for (
            icao24, callsign, longitude, latitude, barometric_altitude,
            geo_altitude, on_ground, ground_speed, true_track,
        ) in rows
    ]
    return datetime.fromtimestamp(max_ts, tz=timezone.utc), aircraft


if __name__ == "__main__":
    import sys

    usage = (
        "Usage: python queries.py callsign <CALLSIGN>"
        " | python queries.py icao24 <ICAO24>"
        " | python queries.py status <ICAO24>"
        " | python queries.py history <ICAO24>"
        " | python queries.py since <ICAO24>"
        " | python queries.py flight <ICAO24>"
        " | python queries.py named"
        " | python queries.py latest"
    )

    if len(sys.argv) == 2 and sys.argv[1] == "named":
        for entry in get_named_aircraft_status():
            print(entry)
        sys.exit(0)

    if len(sys.argv) == 2 and sys.argv[1] == "latest":
        fetched_at, aircraft = get_latest_snapshot()
        print(f"fetched_at: {fetched_at}")
        print(f"{len(aircraft)} aircraft")
        for entry in aircraft[:5]:
            print(entry)
        sys.exit(0)

    if len(sys.argv) != 3 or sys.argv[1] not in (
        "callsign", "icao24", "status", "history", "since", "flight",
    ):
        print(usage)
        sys.exit(1)

    mode, value = sys.argv[1], sys.argv[2]
    if mode == "callsign":
        result = get_icao24_by_callsign(value)
        print(f"icao24 addresses for callsign {value!r}: {result}")
    elif mode == "icao24":
        result = get_callsigns_by_icao24(value)
        print(f"callsigns for icao24 {value!r}: {result}")
    elif mode == "status":
        result = get_last_known_status(value)
        print(f"last known status for icao24 {value!r}: {result}")
    elif mode == "since":
        result = get_status_since(value)
        print(f"status flip for icao24 {value!r}: {result}")
    elif mode == "flight":
        result = get_current_flight_trail(value)
        print(f"{len(result)} points in current flight for icao24 {value!r}")
        for point in result:
            print(point)
    else:
        result = get_position_history(value)
        print(f"{len(result)} position points for icao24 {value!r}")
        for point in result:
            print(point)

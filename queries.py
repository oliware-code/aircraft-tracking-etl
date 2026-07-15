from datetime import datetime, timedelta, timezone

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
                "last_seen": timestamp,
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


def get_last_known_status_by_callsign(callsign, conn=None):
    """Return the most recent state row recorded under this callsign, or None if never seen.

    Unlike get_last_known_status, this follows a flight number rather than a specific
    aircraft — the icao24 in the result is whichever aircraft was flying it at the time.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, icao24, longitude, latitude, barometric_altitude,
                       geo_altitude, on_ground, ground_speed, vertical_rate, true_track,
                       squawk
                FROM states
                WHERE TRIM(UPPER(callsign)) = TRIM(UPPER(%s))
                ORDER BY timestamp DESC
                LIMIT 1;
                """,
                (callsign,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            (
                timestamp, icao24, longitude, latitude, barometric_altitude,
                geo_altitude, on_ground, ground_speed, vertical_rate, true_track,
                squawk,
            ) = row

            return {
                "icao24": icao24,
                "callsign": callsign,
                "last_seen": timestamp,
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

    end_ts = last_status["last_seen"]
    start_ts = end_ts - timedelta(days=days)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (EXTRACT(EPOCH FROM timestamp)::bigint / %s)
                       timestamp, callsign, longitude, latitude, geo_altitude, on_ground
                FROM states
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                  AND timestamp BETWEEN %s AND %s
                  AND longitude IS NOT NULL
                  AND latitude IS NOT NULL
                ORDER BY EXTRACT(EPOCH FROM timestamp)::bigint / %s, timestamp;
                """,
                (bucket_seconds, icao24, start_ts, end_ts, bucket_seconds),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    rows.sort(key=lambda r: r[0])
    return [
        {
            "timestamp": ts,
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

        start_ts = since["since"]
        end_ts = status["last_seen"]

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
            "timestamp": ts,
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
        "since": since_ts,
    }


def get_friendly_name(icao24, conn=None):
    """Return the aircraft's friendly_name (or None if unset/unknown)."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT friendly_name
                FROM aircraft
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s));
                """,
                (icao24,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()
    return row[0] if row else None


def get_aircraft_info(icao24, conn=None):
    """Return {friendly_name, registration, aircraft_type, manufacturer} for icao24,
    or None if this icao24 has never been recorded in `aircraft`."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT friendly_name, registration, aircraft_type, manufacturer
                FROM aircraft
                WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s));
                """,
                (icao24,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None

    friendly_name, registration, aircraft_type, manufacturer = row
    return {
        "friendly_name": friendly_name,
        "registration": registration,
        "aircraft_type": aircraft_type,
        "manufacturer": manufacturer,
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


def get_watched_callsign_status(callsigns, conn=None):
    """Return current status for each watched callsign (see
    status_watch.load_callsign_watchlist), resolved to whichever aircraft is
    currently flying it. friendly_name falls back to the callsign itself when the
    resolved aircraft has none (or hasn't been sighted at all yet)."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        results = []
        for callsign in callsigns:
            status = get_last_known_status_by_callsign(callsign, conn=conn)
            icao24 = status["icao24"] if status else None
            info = get_aircraft_info(icao24, conn=conn) if icao24 else None
            results.append(
                {
                    "icao24": icao24,
                    "friendly_name": (info["friendly_name"] if info else None) or callsign,
                    "registration": info["registration"] if info else None,
                    "aircraft_type": info["aircraft_type"] if info else None,
                    "manufacturer": info["manufacturer"] if info else None,
                    "status": status,
                }
            )
    finally:
        if owns_conn:
            conn.close()
    return results


def get_recent_flights_by_callsign(callsign, limit=4, conn=None):
    """Return up to `limit` most recent flight instances for this callsign, newest
    first. A "flight" is one contiguous airborne segment (states between an
    on_ground->airborne transition and the next airborne->on_ground one), so a
    callsign that flies the same route daily gets one row per day, not one per
    state vector. `in_progress` is true only for the single most recent flight if
    it hasn't been followed by a later on-ground segment yet.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH flagged AS (
                    SELECT
                        timestamp, icao24, on_ground, longitude, latitude,
                        on_ground IS DISTINCT FROM LAG(on_ground) OVER (ORDER BY timestamp) AS changed
                    FROM states
                    WHERE TRIM(UPPER(callsign)) = TRIM(UPPER(%s))
                ),
                segments AS (
                    SELECT
                        timestamp, icao24, on_ground, longitude, latitude,
                        SUM(CASE WHEN changed THEN 1 ELSE 0 END) OVER (ORDER BY timestamp) AS segment_id
                    FROM flagged
                ),
                overall_max AS (
                    SELECT MAX(segment_id) AS max_segment_id FROM segments
                ),
                airborne_segments AS (
                    SELECT segment_id, MIN(timestamp) AS departed_at, MAX(timestamp) AS last_seen
                    FROM segments
                    WHERE NOT on_ground
                    GROUP BY segment_id
                )
                SELECT
                    a.departed_at, a.last_seen,
                    (a.segment_id = overall_max.max_segment_id) AS in_progress,
                    s.icao24, s.longitude, s.latitude
                FROM airborne_segments a
                CROSS JOIN overall_max
                JOIN LATERAL (
                    SELECT icao24, longitude, latitude
                    FROM segments
                    WHERE segment_id = a.segment_id
                    ORDER BY timestamp DESC
                    LIMIT 1
                ) s ON true
                ORDER BY a.last_seen DESC
                LIMIT %s;
                """,
                (callsign, limit),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "departed_at": departed_at,
            "last_seen": last_seen,
            "in_progress": in_progress,
            "icao24": icao24,
            "longitude": longitude,
            "latitude": latitude,
        }
        for departed_at, last_seen, in_progress, icao24, longitude, latitude in rows
    ]


def get_watched_callsign_flights(callsigns, limit=4, conn=None):
    """Return up to `limit` recent flight instances per watched callsign (see
    get_recent_flights_by_callsign), each enriched with aircraft info and route,
    ready for table display. Newest flight first within each callsign's group."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        route_cache = {}
        results = []
        for callsign in callsigns:
            if callsign not in route_cache:
                route_cache[callsign] = get_route_for_callsign(callsign, conn=conn)
            route = route_cache[callsign]
            route_label = (
                f"{route['iata_origin']} → {route['iata_destination']}"
                if route and route["iata_origin"] and route["iata_destination"]
                else None
            )

            flights = get_recent_flights_by_callsign(callsign, limit=limit, conn=conn)
            for flight in flights:
                info = get_aircraft_info(flight["icao24"], conn=conn) if flight["icao24"] else None
                results.append(
                    {
                        "callsign": callsign,
                        "icao24": flight["icao24"],
                        "friendly_name": (info["friendly_name"] if info else None),
                        "registration": info["registration"] if info else None,
                        "aircraft_type": info["aircraft_type"] if info else None,
                        "manufacturer": info["manufacturer"] if info else None,
                        "route": route_label,
                        "departed_at": flight["departed_at"],
                        "last_seen": flight["last_seen"],
                        "in_progress": flight["in_progress"],
                        "longitude": flight["longitude"],
                        "latitude": flight["latitude"],
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


def get_all_airports(conn=None):
    """Return every airport currently stored with known coordinates, for map display."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT iata, icao, name, municipality, country, latitude, longitude
                FROM airports
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL;
                """
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "iata": iata,
            "icao": icao,
            "name": name,
            "municipality": municipality,
            "country": country,
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        for iata, icao, name, municipality, country, latitude, longitude in rows
    ]


def get_airport_by_iata(iata, conn=None):
    """Return {name, icao, municipality, country, latitude, longitude} for a single
    airport, or None if unknown/not yet enriched."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, icao, municipality, country, latitude, longitude
                FROM airports
                WHERE TRIM(UPPER(iata)) = TRIM(UPPER(%s));
                """,
                (iata,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None

    name, icao, municipality, country, latitude, longitude = row
    return {
        "name": name,
        "icao": icao,
        "municipality": municipality,
        "country": country,
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
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
    return max_ts, aircraft


def get_last_ingest_summary(conn=None):
    """Return {fetched_at, states_count} for the most recently ingested snapshot,
    or None if `states` is empty. states_count is every row sharing that exact
    timestamp (unfiltered by position), matching what main.py logs as
    "States inserted" for that cycle.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM states;")
            max_ts = cur.fetchone()[0]
            if max_ts is None:
                return None

            cur.execute("SELECT count(*) FROM states WHERE timestamp = %s;", (max_ts,))
            states_count = cur.fetchone()[0]
    finally:
        if owns_conn:
            conn.close()

    return {"fetched_at": max_ts, "states_count": states_count}


def get_states_ingest_history(hours=72, conn=None):
    """Return one {bucket_start, states_count} entry per hour for the last `hours`
    hours, oldest first. Not exact -- states_count is the size of a single
    representative fetch within that hour, not every fetch averaged -- traded
    deliberately for speed: aggregating every row in a 72-hour window (tens of
    millions of rows) took ~2 minutes in testing, whereas finding one
    representative timestamp per hour (a cheap indexed range+LIMIT 1 lookup) and
    counting just that one fetch takes well under 5 seconds for 72 buckets.
    Hours with no data at all are omitted.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            history = []
            for i in range(hours, -1, -1):
                bucket_start = now - timedelta(hours=i)
                bucket_end = bucket_start + timedelta(hours=1)
                cur.execute(
                    """
                    SELECT timestamp FROM states
                    WHERE timestamp >= %s AND timestamp < %s
                    ORDER BY timestamp LIMIT 1;
                    """,
                    (bucket_start, bucket_end),
                )
                row = cur.fetchone()
                if row is None:
                    continue

                cur.execute("SELECT count(*) FROM states WHERE timestamp = %s;", (row[0],))
                states_count = cur.fetchone()[0]
                history.append({"bucket_start": bucket_start, "states_count": states_count})
    finally:
        if owns_conn:
            conn.close()

    return history


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

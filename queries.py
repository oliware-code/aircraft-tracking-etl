import math
from datetime import datetime, timedelta, timezone

from db_connection import get_connection


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in kilometers."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Shared between approach_alerts.py and status_watch.py -- lives here (rather
# than in either of those, which import each other) to avoid a circular import.
DESTINATION_IATA = "MEX"

STALE_AIRBORNE_MINUTES = 30

# Some aircraft go completely silent while parked at the gate (transponder
# powered down), so a real ground stopover can show zero state rows and never
# record on_ground=true. Without this, flight segmentation (which otherwise
# only splits on an actual recorded on_ground change) stitches the two real
# flights either side of that stopover into one, e.g. a 13.5h GRU layover
# read as a single continuous airborne "flight" from FRA to GRU and back.
#
# A long gap alone isn't a safe signal on its own, though: a transoceanic
# flight can have a multi-hour real ADS-B coverage gap (observed: ~6.5h over
# the Atlantic) while genuinely still cruising the whole time. So a gap only
# counts as a segment boundary if the aircraft also barely moved across it
# (SEGMENT_GAP_STATIONARY_KM) -- distinguishing "sat at the gate" from "kept
# flying through a coverage gap" far more reliably than duration alone.
SEGMENT_GAP_HOURS = 3
SEGMENT_GAP_STATIONARY_KM = 50

# Shared "did this flight really pause here" boundary condition, used by every
# function that segments `states` into flight instances (get_status_since,
# get_recent_flights_by_callsign, get_recent_flights_by_icao24, and
# flight_report.py's full_history_flights). Expects only bare `timestamp`,
# `on_ground`, `longitude`, `latitude` columns in scope. A long gap only
# counts as a boundary given *positive* evidence of staying put (both
# positions known and close together) -- NOT by default when position is
# missing on either side. A real long-haul flight can have a stretch where
# altitude/speed still report but position briefly doesn't (observed: a
# genuine MEX-BCN flight with a few null-position rows mid-Atlantic); defaulting
# to "split" in that case wrongly cut a real single flight in two.
SEGMENT_CHANGED_SQL = f"""(on_ground IS DISTINCT FROM LAG(on_ground) OVER (ORDER BY timestamp))
    OR (
        timestamp - LAG(timestamp) OVER (ORDER BY timestamp) > INTERVAL '{SEGMENT_GAP_HOURS} hours'
        AND longitude IS NOT NULL AND latitude IS NOT NULL
        AND LAG(longitude) OVER (ORDER BY timestamp) IS NOT NULL
        AND LAG(latitude) OVER (ORDER BY timestamp) IS NOT NULL
        AND 2 * 6371 * ASIN(SQRT(
            POWER(SIN(RADIANS((latitude - LAG(latitude) OVER (ORDER BY timestamp)) / 2)), 2) +
            COS(RADIANS(LAG(latitude) OVER (ORDER BY timestamp))) * COS(RADIANS(latitude)) *
            POWER(SIN(RADIANS((longitude - LAG(longitude) OVER (ORDER BY timestamp)) / 2)), 2)
        )) < {SEGMENT_GAP_STATIONARY_KM}
    )"""


def _resolve_live_status(on_ground, callsign, latitude, longitude, geo_altitude, timestamp, conn):
    """Return "on ground" or "airborne" for a live status display. Normally just
    mirrors the raw on_ground flag, but if it claims airborne while stale (no
    contact in STALE_AIRBORNE_MINUTES) and the reading also looks like a landing
    (near the route's destination, low altitude above its elevation -- same
    check as classify_flight_status), returns "on ground" instead. ADS-B often
    drops out right at touchdown before a ground-contact squitter is ever
    received, so blindly trusting a stale on_ground=False reading understates
    what's actually happened.

    Display-only: status_watch.py's real-time notification logic reacts to
    fresh on_ground transitions, not this inference, so notifications aren't
    affected by this fallback.
    """
    if on_ground:
        return "on ground"

    minutes_since = (datetime.now(timezone.utc) - timestamp).total_seconds() / 60
    if minutes_since <= STALE_AIRBORNE_MINUTES:
        return "airborne"
    if geo_altitude is None or latitude is None or longitude is None or not callsign:
        return "airborne"

    route = get_route_for_callsign(callsign, conn=conn)
    destination = (
        get_airport_by_iata(route["iata_destination"], conn=conn)
        if route and route.get("iata_destination")
        else None
    )
    if not destination or destination["latitude"] is None or destination["longitude"] is None:
        return "airborne"

    distance_km = haversine_km(float(latitude), float(longitude), destination["latitude"], destination["longitude"])
    elevation_m = destination["elevation"] * FEET_TO_METERS if destination["elevation"] is not None else 0
    altitude_above_elevation_m = float(geo_altitude) - elevation_m

    if distance_km <= LANDING_MAX_DISTANCE_KM and altitude_above_elevation_m <= LANDING_MAX_ALTITUDE_ABOVE_ELEVATION_M:
        return "on ground"
    return "airborne"


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

            callsign = callsign.strip() if callsign else None
            return {
                "icao24": icao24,
                "callsign": callsign,
                "last_seen": timestamp,
                "status": _resolve_live_status(on_ground, callsign, latitude, longitude, geo_altitude, timestamp, conn),
                "on_ground_raw": on_ground,
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
                "status": _resolve_live_status(on_ground, callsign, latitude, longitude, geo_altitude, timestamp, conn),
                "on_ground_raw": on_ground,
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
    """Return when the aircraft's current on_ground status began (i.e. the last
    state flip, OR the last time it resumed reporting after a gap longer than
    SEGMENT_GAP_HOURS -- see that constant's comment for why a plain on_ground
    flip alone isn't enough)."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT timestamp, on_ground
                FROM (
                    SELECT timestamp, on_ground,
                           {SEGMENT_CHANGED_SQL} AS changed
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


def get_named_aircraft_status(watchlist_entries, conn=None):
    """Return current status for every aircraft in `watchlist_entries`
    (ordered (icao24, yaml_comment) tuples -- see
    status_watch.load_watchlist_with_names), in that exact order.

    notify_watchlist.yaml is the source of truth for which aircraft show up
    here, not the aircraft table: uses the aircraft table's friendly_name if
    set, otherwise falls back to the watchlist's YAML comment (or the bare
    icao24 if there isn't even that) as a display name, and flags
    needs_friendly_name=True so the frontend can prompt for a real one to be
    set in the database. Being in the watchlist alone used to get an aircraft
    notifications and a sort position but not visibility here, which was a
    confusing gap -- adding an icao24 to the watchlist now always makes it
    show up, with a fallback name at worst.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        icao24s = [icao24 for icao24, _ in watchlist_entries]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT icao24, friendly_name, registration, aircraft_type, manufacturer
                FROM aircraft
                WHERE icao24 = ANY(%s);
                """,
                (icao24s,),
            )
            by_icao24 = {row[0]: row[1:] for row in cur.fetchall()}

        results = []
        for icao24, yaml_comment in watchlist_entries:
            friendly_name, registration, aircraft_type, manufacturer = by_icao24.get(
                icao24, (None, None, None, None)
            )
            status = get_last_known_status(icao24, conn=conn)
            results.append(
                {
                    "icao24": icao24,
                    "friendly_name": friendly_name or yaml_comment or icao24,
                    "needs_friendly_name": not friendly_name,
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
    on_ground->airborne transition and the next airborne->on_ground one, OR a
    gap longer than SEGMENT_GAP_HOURS with no rows at all -- see that
    constant's comment), so a callsign that flies the same route daily gets
    one row per day, not one per state vector. `in_progress` is true only for
    the single most recent flight if it hasn't been followed by a later
    on-ground segment (or long gap) yet.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH flagged AS (
                    SELECT
                        timestamp, icao24, on_ground, longitude, latitude,
                        geo_altitude, ground_speed,
                        {SEGMENT_CHANGED_SQL} AS changed
                    FROM states
                    WHERE TRIM(UPPER(callsign)) = TRIM(UPPER(%s))
                ),
                segments AS (
                    SELECT
                        timestamp, icao24, on_ground, longitude, latitude,
                        geo_altitude, ground_speed,
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
                    s.icao24, s.longitude, s.latitude, s.geo_altitude, s.ground_speed
                FROM airborne_segments a
                CROSS JOIN overall_max
                JOIN LATERAL (
                    SELECT icao24, longitude, latitude, geo_altitude, ground_speed
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
            "geo_altitude": geo_altitude,
            "ground_speed": ground_speed,
        }
        for departed_at, last_seen, in_progress, icao24, longitude, latitude, geo_altitude, ground_speed in rows
    ]


def get_recent_flights_by_icao24(icao24, limit=4, offset=0, conn=None):
    """Same as get_recent_flights_by_callsign, but keyed on icao24 instead --
    for a tracked aircraft's own flight history, since the same aircraft can
    fly different callsigns/routes over time. Also returns each flight's own
    callsign (the segment's last known one), so its route can be looked up
    per-flight rather than assuming a single current callsign for the aircraft.
    `offset` supports paging further back ("show more") beyond the initial page.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH flagged AS (
                    SELECT
                        timestamp, callsign, on_ground, longitude, latitude,
                        geo_altitude, ground_speed,
                        {SEGMENT_CHANGED_SQL} AS changed
                    FROM states
                    WHERE TRIM(LOWER(icao24)) = TRIM(LOWER(%s))
                ),
                segments AS (
                    SELECT
                        timestamp, callsign, on_ground, longitude, latitude,
                        geo_altitude, ground_speed,
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
                    s.callsign, s.longitude, s.latitude, s.geo_altitude, s.ground_speed
                FROM airborne_segments a
                CROSS JOIN overall_max
                JOIN LATERAL (
                    SELECT callsign, longitude, latitude, geo_altitude, ground_speed
                    FROM segments
                    WHERE segment_id = a.segment_id
                    ORDER BY timestamp DESC
                    LIMIT 1
                ) s ON true
                ORDER BY a.last_seen DESC
                LIMIT %s OFFSET %s;
                """,
                (icao24, limit, offset),
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
            "callsign": callsign,
            "longitude": longitude,
            "latitude": latitude,
            "geo_altitude": geo_altitude,
            "ground_speed": ground_speed,
        }
        for departed_at, last_seen, in_progress, callsign, longitude, latitude, geo_altitude, ground_speed in rows
    ]


LANDING_MAX_DISTANCE_KM = 15
LANDING_MAX_ALTITUDE_ABOVE_ELEVATION_M = 500
CRUISE_ALTITUDE_THRESHOLD_M = 6000
CRUISE_SPEED_THRESHOLD_KMH = 400
FEET_TO_METERS = 0.3048
MAX_PLAUSIBLE_DURATION_HOURS = 16


def classify_flight_status(flight, route, conn=None):
    """Classify a flight instance (see get_recent_flights_by_callsign) as one of:
      - "in_progress": still airborne, no later segment has happened yet.
      - "landed": the last detection is consistent with a real landing at the
        route's destination airport (close by, low altitude above the airport's
        elevation).
      - "lost_signal": not the current segment, but either the segment's total
        duration is physically implausible (a mid-flight ingestion gap likely
        swallowed an on-ground period and stitched two real flights into one),
        or the last detection still looks like cruise (high altitude and/or
        high speed) -- either way this wasn't a completed landing, ADS-B
        coverage was simply lost. See flight_definition.txt (project root,
        untracked) for the reasoning behind this distinction.

    Falls back to "landed" (the original, simpler behavior) whenever there isn't
    enough data to tell confidently: no route/destination known yet, the
    destination airport hasn't been enriched with coordinates, or the last
    detection is missing altitude/speed/position. Known limitation: checks
    against the *current* route on file for this callsign, not necessarily the
    route that was actually flown at the time of an older flight instance --
    flight_routes has no per-instance history, only the latest known route.
    """
    if flight["in_progress"]:
        return "in_progress"

    duration_hours = (flight["last_seen"] - flight["departed_at"]).total_seconds() / 3600
    if duration_hours > MAX_PLAUSIBLE_DURATION_HOURS:
        return "lost_signal"

    destination = (
        get_airport_by_iata(route["iata_destination"], conn=conn)
        if route and route.get("iata_destination")
        else None
    )
    if not destination or destination["latitude"] is None or destination["longitude"] is None:
        return "landed"

    geo_altitude = flight.get("geo_altitude")
    ground_speed = flight.get("ground_speed")
    if (
        geo_altitude is None
        or ground_speed is None
        or flight["latitude"] is None
        or flight["longitude"] is None
    ):
        return "landed"

    distance_km = haversine_km(
        float(flight["latitude"]), float(flight["longitude"]),
        destination["latitude"], destination["longitude"],
    )
    elevation_m = (
        destination["elevation"] * FEET_TO_METERS if destination["elevation"] is not None else 0
    )
    altitude_above_elevation_m = float(geo_altitude) - elevation_m
    speed_kmh = float(ground_speed) * 3.6

    # Checked in this order deliberately: an "obviously still cruising" reading
    # (per flight_definition.txt's own examples -- 28,000ft or >400km/h) should
    # override a distance-based "near the destination" match, not the other way
    # around -- a fast, high pass near the destination's coordinates is still
    # clearly not a landing, coincidental proximity notwithstanding.
    if altitude_above_elevation_m > CRUISE_ALTITUDE_THRESHOLD_M or speed_kmh > CRUISE_SPEED_THRESHOLD_KMH:
        return "lost_signal"

    # Both a confident landing match and the remaining ambiguous middle (neither
    # clearly cruising nor confidently at the destination) resolve to "landed" --
    # kept as separate branches for clarity even though they return the same
    # value, since a future refinement might want to treat the ambiguous case
    # differently.
    if (
        distance_km <= LANDING_MAX_DISTANCE_KM
        and altitude_above_elevation_m <= LANDING_MAX_ALTITUDE_ABOVE_ELEVATION_M
    ):
        return "landed"  # confident landing

    return "landed"  # ambiguous middle, fallback


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
                status = classify_flight_status(flight, route, conn=conn)
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
                        "status": status,
                        "longitude": flight["longitude"],
                        "latitude": flight["latitude"],
                    }
                )
    finally:
        if owns_conn:
            conn.close()
    return results


def get_tracked_aircraft_flights(icao24, limit=4, offset=0, conn=None):
    """Return up to `limit` recent flight instances for a single tracked
    aircraft (see get_recent_flights_by_icao24), each enriched with the route
    flown at the time (looked up per-flight via that flight's own callsign,
    since a tracked aircraft can fly different routes over time -- unlike
    get_watched_callsign_flights, which has one fixed callsign per group).
    Newest flight first. `offset` supports "show more" paging."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        route_cache = {}
        results = []
        for flight in get_recent_flights_by_icao24(icao24, limit=limit, offset=offset, conn=conn):
            callsign = flight["callsign"]
            if callsign not in route_cache:
                route_cache[callsign] = get_route_for_callsign(callsign, conn=conn) if callsign else None
            route = route_cache[callsign]
            route_label = (
                f"{route['iata_origin']} → {route['iata_destination']}"
                if route and route["iata_origin"] and route["iata_destination"]
                else None
            )
            status = classify_flight_status(flight, route, conn=conn)
            results.append(
                {
                    "callsign": callsign,
                    "route": route_label,
                    "departed_at": flight["departed_at"],
                    "last_seen": flight["last_seen"],
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
    """Return {name, icao, municipality, country, latitude, longitude, elevation}
    for a single airport, or None if unknown/not yet enriched. elevation is in
    feet (as adsbdb provides it), or None if not known."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, icao, municipality, country, latitude, longitude, elevation
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

    name, icao, municipality, country, latitude, longitude, elevation = row
    return {
        "name": name,
        "icao": icao,
        "municipality": municipality,
        "country": country,
        "elevation": elevation,
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


NEARBY_TRAFFIC_RADIUS_KM = 30


def get_aircraft_near_airport(iata, radius_km=NEARBY_TRAFFIC_RADIUS_KM, conn=None):
    """Return every aircraft in the most recent snapshot within radius_km of the
    given airport (by IATA code) -- any traffic, not just the watched_aircraft/
    watched_callsigns lists. [] if the airport is unknown/not yet enriched or
    there's no current snapshot. Distance filtering happens in SQL (same
    haversine formula as SEGMENT_CHANGED_SQL above) rather than pulling every
    aircraft on the planet back to Python just to discard most of them."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        airport = get_airport_by_iata(iata, conn=conn)
        if not airport or airport["latitude"] is None or airport["longitude"] is None:
            return []

        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM states;")
            max_ts = cur.fetchone()[0]
            if max_ts is None:
                return []

            cur.execute(
                """
                SELECT icao24, callsign, longitude, latitude, on_ground, ground_speed, true_track, distance_km
                FROM (
                    SELECT icao24, callsign, longitude, latitude, on_ground, ground_speed, true_track,
                           2 * 6371 * ASIN(SQRT(
                               POWER(SIN(RADIANS((latitude - %(lat)s) / 2)), 2) +
                               COS(RADIANS(%(lat)s)) * COS(RADIANS(latitude)) *
                               POWER(SIN(RADIANS((longitude - %(lon)s) / 2)), 2)
                           )) AS distance_km
                    FROM states
                    WHERE timestamp = %(ts)s
                      AND longitude IS NOT NULL
                      AND latitude IS NOT NULL
                ) nearby
                WHERE distance_km <= %(radius)s
                ORDER BY distance_km;
                """,
                {"lat": airport["latitude"], "lon": airport["longitude"], "ts": max_ts, "radius": radius_km},
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "icao24": icao24,
            "callsign": callsign.strip() if callsign else None,
            "latitude": float(latitude),
            "longitude": float(longitude),
            "on_ground": on_ground,
            "ground_speed": float(ground_speed) if ground_speed is not None else None,
            "true_track": float(true_track) if true_track is not None else None,
            "distance_km": round(float(distance_km), 1),
        }
        for icao24, callsign, longitude, latitude, on_ground, ground_speed, true_track, distance_km in rows
    ]


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

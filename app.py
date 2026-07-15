import logging
import os
import queue
import select
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

from db_connection import get_connection
from queries import (
    get_all_airports,
    get_callsigns_by_icao24,
    get_current_flight_trail,
    get_icao24_by_callsign,
    get_last_known_status,
    get_last_ingest_summary,
    get_latest_snapshot,
    get_named_aircraft_status,
    get_position_history,
    get_route_for_callsign,
    get_states_ingest_history,
    get_status_since,
    get_watched_callsign_flights,
    get_watched_callsign_status,
)
from status_watch import load_callsign_watchlist, load_watchlist

app = Flask(__name__)

HISTORY_DAYS = 7
_snapshot_subscribers = set()
_snapshot_subscribers_lock = threading.Lock()


def _broadcast_new_snapshot():
    with _snapshot_subscribers_lock:
        subscribers = list(_snapshot_subscribers)
    for client_queue in subscribers:
        client_queue.put_nowait("refresh")


def _listen_for_snapshots():
    """Background thread: LISTENs on the channel main.py NOTIFYs after each
    committed ingest, and wakes every connected /events client when it fires.
    Runs for the lifetime of the process; reconnects on connection loss.
    """
    while True:
        try:
            conn = get_connection()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("LISTEN new_snapshot;")
            while True:
                if not select.select([conn], [], [], 60)[0]:
                    continue
                conn.poll()
                while conn.notifies:
                    conn.notifies.pop()
                    _broadcast_new_snapshot()
        except Exception:
            logging.exception("Snapshot listener lost its connection; reconnecting in 5s")
            time.sleep(5)
GAP_SECONDS = 600  # trail segments spanning >10 minutes between detections are drawn differently


def _segment_trail(trail_points):
    """Split a trail into runs, flagging the connector whenever two consecutive
    detections are more than GAP_SECONDS apart (the aircraft stayed airborne but
    wasn't seen in between, so that stretch of line is an interpolation, not a
    tracked path).
    """
    segments = []
    current = []
    for i, p in enumerate(trail_points):
        point = {"lat": float(p["latitude"]), "lon": float(p["longitude"])}
        if i == 0:
            current = [point]
            continue
        gap = (p["timestamp"] - trail_points[i - 1]["timestamp"]).total_seconds()
        if gap > GAP_SECONDS:
            segments.append({"gap": False, "points": current})
            prev_point = {
                "lat": float(trail_points[i - 1]["latitude"]),
                "lon": float(trail_points[i - 1]["longitude"]),
            }
            segments.append({"gap": True, "points": [prev_point, point]})
            current = [point]
        else:
            current.append(point)
    if len(current) > 1:
        segments.append({"gap": False, "points": current})
    return segments


def _with_epoch(status):
    if status:
        status["last_seen_epoch"] = int(status["last_seen"].timestamp())
    return status


def _status_duration(icao24):
    """Return (label, epoch) describing how long the aircraft has held its current status."""
    since = get_status_since(icao24)
    if since is None:
        return None, None
    return "for", int(since["since"].timestamp())


@app.route("/", methods=["GET", "POST"])
def index():
    results = None
    status = None
    history = None
    status_duration_label = None
    status_duration_epoch = None
    error = None
    mode = request.form.get("mode", "callsign")
    query = ""

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not query:
            error = "Please enter a value."
        else:
            try:
                if mode == "callsign":
                    results = get_icao24_by_callsign(query)
                elif mode == "icao24":
                    results = get_callsigns_by_icao24(query)
                else:
                    status = _with_epoch(get_last_known_status(query))
                    if status is None:
                        error = f"No data found for icao24 {query!r}."
                    else:
                        status_duration_label, status_duration_epoch = _status_duration(query)
                        history = [
                            {
                                "lat": float(p["latitude"]),
                                "lon": float(p["longitude"]),
                                "timestamp": p["timestamp"].isoformat(),
                                "callsign": p["callsign"],
                                "on_ground": p["on_ground"],
                            }
                            for p in get_position_history(query, days=HISTORY_DAYS)
                        ]
            except Exception as exc:
                error = f"Query failed: {exc}"

    return render_template(
        "index.html",
        results=results,
        status=status,
        history=history,
        history_days=HISTORY_DAYS,
        status_duration_label=status_duration_label,
        status_duration_epoch=status_duration_epoch,
        error=error,
        mode=mode,
        query=query,
    )


def _build_named_data():
    conn = get_connection()
    try:
        aircraft = get_named_aircraft_status(conn=conn)
        for a in aircraft:
            a["tracked_by"] = "icao24"

        # Display order follows notify_watchlist.yaml's list order rather than the
        # alphabetical-by-friendly_name order the query returns; anything with a
        # friendly_name but not in the YAML falls back to the end, in its existing order.
        icao24_order = {icao24: i for i, icao24 in enumerate(load_watchlist())}
        aircraft.sort(key=lambda a: icao24_order.get(a["icao24"], len(icao24_order)))

        named_icao24s = {a["icao24"] for a in aircraft}
        watched_callsigns = load_callsign_watchlist()
        if watched_callsigns:
            for entry in get_watched_callsign_status(watched_callsigns, conn=conn):
                # Skip if this callsign is currently flown by an aircraft already
                # shown via the icao24 watchlist, to avoid a duplicate marker.
                if entry["icao24"] and entry["icao24"] in named_icao24s:
                    continue
                entry["tracked_by"] = "callsign"
                aircraft.append(entry)

        markers = []
        for a in aircraft:
            a["status"] = _with_epoch(a["status"])

            since = get_status_since(a["icao24"], conn=conn) if a["status"] else None
            if since is None:
                a["status_duration_label"], a["status_duration_epoch"] = None, None
            else:
                a["status_duration_label"] = "for"
                a["status_duration_epoch"] = int(since["since"].timestamp())

            if a["status"] and a["status"]["latitude"] is not None and a["status"]["longitude"] is not None:
                trail = (
                    get_current_flight_trail(a["icao24"], conn=conn, status=a["status"], since=since)
                    if a["status"]["status"] != "on ground"
                    else []
                )
                route = get_route_for_callsign(a["status"]["callsign"], conn=conn)
                route_label = (
                    f"{route['iata_origin']} → {route['iata_destination']}"
                    if route and route["iata_origin"] and route["iata_destination"]
                    else None
                )
                markers.append(
                    {
                        "lat": float(a["status"]["latitude"]),
                        "lon": float(a["status"]["longitude"]),
                        "friendly_name": a["friendly_name"],
                        "callsign": a["status"]["callsign"],
                        "tracked_by": a["tracked_by"],
                        "on_ground": a["status"]["status"] == "on ground",
                        "last_seen_epoch": a["status"]["last_seen_epoch"],
                        "ground_speed": (
                            float(a["status"]["ground_speed"])
                            if a["status"]["ground_speed"] is not None
                            else None
                        ),
                        "route": route_label,
                        "heading": (
                            0
                            if a["status"]["status"] == "on ground"
                            else float(a["status"]["true_track"]) if a["status"]["true_track"] is not None else 0
                        ),
                        "trail": [
                            {"lat": float(p["latitude"]), "lon": float(p["longitude"])}
                            for p in trail
                        ],
                        "trail_segments": _segment_trail(trail),
                    }
                )

        callsign_flights = (
            get_watched_callsign_flights(watched_callsigns, conn=conn) if watched_callsigns else []
        )
        for f in callsign_flights:
            f["departed_epoch"] = int(f["departed_at"].timestamp())
            f["last_seen_epoch"] = int(f["last_seen"].timestamp())
    finally:
        conn.close()

    return aircraft, markers, callsign_flights


def _serialize_named_aircraft(aircraft):
    """JSON-safe view of the aircraft list (drops the raw `last_seen` datetime, keeps the epoch)."""
    serialized = []
    for a in aircraft:
        status = a["status"]
        serialized.append(
            {
                "icao24": a["icao24"],
                "friendly_name": a["friendly_name"],
                "registration": a["registration"],
                "aircraft_type": a["aircraft_type"],
                "manufacturer": a["manufacturer"],
                "tracked_by": a["tracked_by"],
                "status_duration_label": a["status_duration_label"],
                "status_duration_epoch": a["status_duration_epoch"],
                "status": None
                if status is None
                else {
                    "callsign": status["callsign"],
                    "status": status["status"],
                    "latitude": float(status["latitude"]) if status["latitude"] is not None else None,
                    "longitude": float(status["longitude"]) if status["longitude"] is not None else None,
                    "altitude": float(status["altitude"]) if status["altitude"] is not None else None,
                    "ground_speed": float(status["ground_speed"]) if status["ground_speed"] is not None else None,
                    "last_seen_epoch": status["last_seen_epoch"],
                },
            }
        )
    return serialized


def _serialize_callsign_flights(callsign_flights):
    """JSON-safe view of the callsign flight list (drops the raw datetimes, keeps the epochs)."""
    return [
        {
            "callsign": f["callsign"],
            "icao24": f["icao24"],
            "friendly_name": f["friendly_name"],
            "registration": f["registration"],
            "aircraft_type": f["aircraft_type"],
            "manufacturer": f["manufacturer"],
            "route": f["route"],
            "in_progress": f["in_progress"],
            "departed_epoch": f["departed_epoch"],
            "last_seen_epoch": f["last_seen_epoch"],
            "latitude": float(f["latitude"]) if f["latitude"] is not None else None,
            "longitude": float(f["longitude"]) if f["longitude"] is not None else None,
        }
        for f in callsign_flights
    ]


@app.route("/named")
def named():
    aircraft, markers, callsign_flights = _build_named_data()
    airports = get_all_airports()
    last_ingest = get_last_ingest_summary()
    # Deliberately not part of /named/data's live refresh: this is a coarse
    # (hourly-bucketed) 72h history, cheap once per page load but not worth
    # recomputing on every ~2-minute SSE-triggered refresh.
    ingest_history = [
        {
            "bucket_epoch": int(h["bucket_start"].timestamp()),
            "bucket_label": h["bucket_start"].strftime("%a %H:%M UTC"),
            "states_count": h["states_count"],
        }
        for h in get_states_ingest_history(hours=72)
    ]
    return render_template(
        "named.html",
        aircraft=aircraft,
        markers=markers,
        airports=airports,
        callsign_flights=callsign_flights,
        last_ingest=last_ingest,
        ingest_history=ingest_history,
    )


@app.route("/named/data")
def named_data():
    aircraft, markers, callsign_flights = _build_named_data()
    airports = get_all_airports()
    last_ingest = get_last_ingest_summary()
    return jsonify(
        aircraft=_serialize_named_aircraft(aircraft),
        markers=markers,
        airports=airports,
        callsign_flights=_serialize_callsign_flights(callsign_flights),
        last_ingest=None
        if last_ingest is None
        else {
            "states_count": last_ingest["states_count"],
            "fetched_at_epoch": int(last_ingest["fetched_at"].timestamp()),
        },
    )


@app.route("/events")
def events():
    """Server-Sent Events stream: emits "refresh" the moment a cron-triggered
    ingest cycle commits, so pages can re-fetch instead of polling on a timer.
    """
    def stream():
        client_queue = queue.Queue()
        with _snapshot_subscribers_lock:
            _snapshot_subscribers.add(client_queue)
        try:
            while True:
                try:
                    client_queue.get(timeout=25)
                    yield "data: refresh\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with _snapshot_subscribers_lock:
                _snapshot_subscribers.discard(client_queue)

    return Response(stream(), mimetype="text/event-stream")


@app.route("/latest")
def latest():
    fetched_at, aircraft = get_latest_snapshot()
    markers = [
        {
            "lat": float(a["latitude"]),
            "lon": float(a["longitude"]),
            "icao24": a["icao24"],
            "callsign": a["callsign"],
            "on_ground": a["on_ground"],
            "altitude": float(a["altitude"]) if a["altitude"] is not None else None,
            "ground_speed": (
                float(a["ground_speed"]) * 3.6 if a["ground_speed"] is not None else None
            ),
            "heading": (
                0
                if a["on_ground"]
                else float(a["true_track"]) if a["true_track"] is not None else 0
            ),
        }
        for a in aircraft
    ]
    return render_template(
        "latest.html",
        fetched_at=fetched_at,
        markers=markers,
        count=len(markers),
    )


if __name__ == "__main__":
    threading.Thread(target=_listen_for_snapshots, daemon=True).start()
    # Enable the debugger explicitly with FLASK_DEBUG=1; never leave it on by default.
    # threaded=True: /events holds a connection open per client, which would
    # otherwise block every other request behind it on the dev server.
    app.run(host="0.0.0.0", debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)

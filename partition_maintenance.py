"""Daily maintenance for the partitioned `states` table (daily partitions,
states_YYYY_MM_DD). Two jobs, both idempotent and safe to run repeatedly:

  1. Ensure partitions exist for today through --days-ahead days out, so
     ingestion never hits a missing-partition error.
  2. Detach partitions entirely older than --retention-days and move them
     into the `archive` schema (DETACH PARTITION + SET SCHEMA) -- never DROP.
     Archived data still exists and is queryable at archive.states_YYYY_MM_DD,
     it's just out of the way of day-to-day queries against the live `states`
     table.

Retention is a runtime flag, not a hardcoded constant, so it's easy to change
on demand:
    venv/bin/python partition_maintenance.py --retention-days 14
    venv/bin/python partition_maintenance.py --retention-days 7 --days-ahead 3

See DAILY_PARTITIONING_MIGRATION.txt for the migration this continues. Meant
to run daily via cron once wired up; each run is safe to re-run (missing-
partition creation and archiving are both no-ops once done).

Assumes `states` has already been cut over to daily partitions -- see
DAILY_PARTITIONING_MIGRATION.txt for that one-time migration.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_connection import get_connection

DEFAULT_DAYS_AHEAD = 7
DEFAULT_RETENTION_DAYS = 14


def _today(conn=None):
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def existing_partitions(conn, parent="states"):
    cur = conn.cursor()
    cur.execute("""
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = %s
        ORDER BY c.relname;
    """, (parent,))
    return [row[0] for row in cur.fetchall()]


def ensure_future_partitions(conn, days_ahead=DEFAULT_DAYS_AHEAD, verbose=True):
    """Create any missing daily partitions from today through `days_ahead`
    days out. Safe no-op if they already exist."""
    cur = conn.cursor()
    existing = set(existing_partitions(conn, "states"))
    if verbose:
        print(f"[ensure_future_partitions] {len(existing)} partition(s) currently attached to states")

    today = _today()
    created = []
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        next_day = day + timedelta(days=1)
        name = f"states_{day.year}_{day.month:02d}_{day.day:02d}"
        if name in existing:
            if verbose:
                print(f"[ensure_future_partitions] {name} already exists, skipping")
            continue
        cur.execute(
            f"CREATE TABLE {name} PARTITION OF states "
            f"FOR VALUES FROM ('{day.date()}') TO ('{next_day.date()}');"
        )
        conn.commit()
        created.append(name)
        print(f"[ensure_future_partitions] created {name} [{day.date()}, {next_day.date()})")

    if not created:
        print("[ensure_future_partitions] No new partitions needed.")
    else:
        print(f"[ensure_future_partitions] Created {len(created)} new partition(s): {', '.join(created)}")
    return created


def archive_old_partitions(conn, retention_days=DEFAULT_RETENTION_DAYS, verbose=True):
    """Detach daily partitions entirely older than `retention_days` days ago
    and move them into the `archive` schema. Never drops data."""
    cur = conn.cursor()
    partitions = existing_partitions(conn, "states")

    cutoff = _today() - timedelta(days=retention_days)
    print(f"[archive_old_partitions] retention={retention_days} days, cutoff={cutoff.date()} "
          f"(partitions strictly before this are archived, kept otherwise)")

    archived = []
    for name in partitions:
        try:
            _, year, month, day = name.rsplit("_", 3)
            partition_start = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
        except ValueError:
            if verbose:
                print(f"[archive_old_partitions] {name}: not a states_YYYY_MM_DD partition, leaving alone")
            continue

        if partition_start >= cutoff:
            if verbose:
                print(f"[archive_old_partitions] {name} ({partition_start.date()}): within retention window, keeping")
            continue

        cur.execute("CREATE SCHEMA IF NOT EXISTS archive;")
        cur.execute(f"ALTER TABLE states DETACH PARTITION {name};")
        cur.execute(f"ALTER TABLE {name} SET SCHEMA archive;")
        conn.commit()
        archived.append(name)
        print(f"[archive_old_partitions] archived {name} ({partition_start.date()}) -> archive.{name}")

    if not archived:
        print("[archive_old_partitions] No partitions old enough to archive yet.")
    else:
        print(f"[archive_old_partitions] Archived {len(archived)} partition(s): {', '.join(archived)}")
    return archived


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                         help=f"Keep this many days live in `states` before archiving (default: {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD,
                         help=f"Pre-create partitions this many days ahead of today (default: {DEFAULT_DAYS_AHEAD})")
    args = parser.parse_args()

    conn = get_connection()
    try:
        ensure_future_partitions(conn, days_ahead=args.days_ahead)
        archive_old_partitions(conn, retention_days=args.retention_days)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

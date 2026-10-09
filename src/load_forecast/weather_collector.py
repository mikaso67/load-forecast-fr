"""Collect raw GFS points or scan D11 availability without downloading global fields."""

import argparse
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.types.json import Jsonb

from load_forecast.cutoff import forecast_cutoff
from load_forecast.eco2mix import connect_database
from load_forecast.weather import (
    POINTS,
    S3_URL,
    VARIABLES,
    ObjectInfo,
    Run,
    extract_run,
    select_run,
)

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE IF NOT EXISTS raw.gfs_days (
    delivery_day date NOT NULL,
    selection_hash text NOT NULL,
    cutoff timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('primary', 'fallback', 'weather missing')),
    run_init_time timestamptz,
    fallback boolean,
    source_url text,
    original_metadata jsonb NOT NULL,
    ingested_at timestamptz NOT NULL,
    PRIMARY KEY (delivery_day, selection_hash)
);
CREATE TABLE IF NOT EXISTS raw.gfs_points (
    run_init_time timestamptz NOT NULL,
    end_step integer NOT NULL,
    variable text NOT NULL,
    point text NOT NULL,
    message_hash text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    value double precision NOT NULL,
    units text NOT NULL,
    start_step integer NOT NULL,
    valid_time timestamptz NOT NULL,
    source_url text NOT NULL,
    file_last_modified timestamptz NOT NULL,
    index_last_modified timestamptz NOT NULL,
    original_metadata jsonb NOT NULL,
    ingested_at timestamptz NOT NULL,
    PRIMARY KEY (run_init_time, end_step, variable, point, message_hash)
);
"""
INSERT_DAY = """
INSERT INTO raw.gfs_days
    (delivery_day, selection_hash, cutoff, status, run_init_time, fallback,
     source_url, original_metadata, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (delivery_day, selection_hash) DO NOTHING
"""
INSERT_POINT = """
INSERT INTO raw.gfs_points
    (run_init_time, end_step, variable, point, message_hash, latitude, longitude,
     value, units, start_step, valid_time, source_url, file_last_modified,
     index_last_modified, original_metadata, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_init_time, end_step, variable, point, message_hash) DO NOTHING
"""


def inspect_day(delivery_day: date, *, now: datetime | None = None) -> tuple:
    """An HTTP session per worker avoids sharing mutable session state."""
    with requests.Session() as session:
        session.headers["User-Agent"] = "load-forecast-fr/gfs-collector"
        run, objects, failures = select_run(session, delivery_day, now=now)
    return delivery_day, run, objects, failures


def inspect_days(
    pool: ThreadPoolExecutor, days: list[date], now: datetime, batch_size: int,
) -> Iterator[tuple]:
    """Bound queued HTTP work so Ctrl+C does not wait for the entire historical range."""
    if batch_size < 1:
        raise ValueError("Expected a positive batch size")
    for offset in range(0, len(days), batch_size):
        yield from pool.map(partial(inspect_day, now=now), days[offset:offset + batch_size])


def day_row(
    delivery_day: date, run: Run | None, objects: dict[str, ObjectInfo], failures: dict,
    ingested_at: datetime,
) -> tuple:
    """Retain selected object metadata and missing/late keys in an append-only manifest."""
    manifest = {"failures": failures, "objects": {
        key: {"last_modified": objects[key].modified.isoformat(),
              "size": objects[key].size, "etag": objects[key].etag}
        for key in run.keys
    } if run else {}}
    status = "weather missing" if run is None else ("fallback" if run.fallback else "primary")
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return (delivery_day, digest, forecast_cutoff(delivery_day), status,
            run.init_time if run else None, run.fallback if run else None,
            S3_URL + "/" + run.prefix if run else None, Jsonb(manifest), ingested_at)


def point_row(run: Run, point: dict, ingested_at: datetime) -> tuple:
    """The key preserves GRIB revisions; source values and their interval stay unchanged."""
    metadata = {key: point[key] for key in ("index_line", "byte_start", "byte_end")}
    return (run.init_time, point["end_step"], point["variable"], point["point"],
            point["message_hash"], point["latitude"], point["longitude"], point["value"],
            point["units"], point["start_step"],
            run.init_time + timedelta(hours=point["end_step"]), point["source_url"],
            point["file_last_modified"], point["index_last_modified"],
            Jsonb(metadata), ingested_at)


def radiation_sanity(run: Run, points: list[dict]) -> dict:
    """Check raw means, including intervals wholly within 00-02 UTC night in France."""
    radiation = [point for point in points if point["variable"] == "DSWRF"]
    if not radiation or any(point["value"] < 0 for point in radiation):
        raise ValueError("Missing or negative raw DSWRF")
    night = []
    for point in radiation:
        start = run.init_time + timedelta(hours=point["start_step"])
        end = run.init_time + timedelta(hours=point["end_step"])
        if start.date() == end.date() and start.hour == 0 and end.hour <= 2:
            night.append(point["value"])
    if not night or any(value != 0 for value in night):
        raise ValueError("Raw DSWRF is not zero during the checked night intervals")
    return {"dswrf_min": min(point["value"] for point in radiation),
            "dswrf_max": max(point["value"] for point in radiation),
            "night_points": len(night), "night_max": max(night)}


def store_day(connection: psycopg.Connection, selection: tuple, points: list[dict]) -> tuple[int, int]:
    """Insert a validated day atomically; reruns preserve existing rows and timestamps."""
    delivery_day, run, objects, failures = selection
    ingested_at = datetime.now(UTC)
    raw_day = day_row(delivery_day, run, objects, failures, ingested_at)
    raw_points = [point_row(run, point, ingested_at) for point in points]
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(INSERT_DAY, raw_day)
        days_inserted = cursor.rowcount
        if raw_points:
            cursor.executemany(INSERT_POINT, raw_points)
            return days_inserted, cursor.rowcount
        return days_inserted, 0


def stored_days(
    connection: psycopg.Connection, start: date, end: date, variables: tuple[str, ...],
) -> set[date]:
    """Require every step/point of each requested variable, not just a scan manifest."""
    rows = connection.execute("""
        SELECT delivery_day FROM (
            SELECT d.delivery_day, d.selection_hash, p.variable
            FROM raw.gfs_days d JOIN raw.gfs_points p USING (run_init_time)
            WHERE d.delivery_day >= %s AND d.delivery_day < %s
              AND d.status IN ('primary', 'fallback')
              AND p.variable = ANY(%s) AND p.point = ANY(%s)
              AND p.end_step BETWEEN CASE WHEN d.fallback THEN 27 ELSE 21 END
                                 AND CASE WHEN d.fallback THEN 53 ELSE 47 END
            GROUP BY d.delivery_day, d.selection_hash, p.variable
            HAVING count(DISTINCT (p.end_step, p.point)) = %s
        ) complete_variables
        GROUP BY delivery_day, selection_hash HAVING count(*) = %s
    """, (start, end, list(variables), list(POINTS), 27 * len(POINTS), len(variables))).fetchall()
    return {row[0] for row in rows}


def stored_row_bytes(
    connection: psycopg.Connection, run: Run, variables: tuple[str, ...],
) -> int:
    """Logical PostgreSQL tuple sizes; exclude indexes, pages and the shared manifest."""
    return connection.execute("""
        SELECT coalesce(sum(pg_column_size(p)), 0) FROM raw.gfs_points p
        WHERE run_init_time = %s AND variable = ANY(%s)
          AND end_step BETWEEN %s AND %s AND point = ANY(%s)
    """, (run.init_time, list(variables), run.first_step, run.first_step + 26,
          list(POINTS))).fetchone()[0]


def database_bytes(connection: psycopg.Connection) -> int:
    """Allocated heap, TOAST and index bytes for both weather tables."""
    return connection.execute("""
        SELECT pg_total_relation_size('raw.gfs_points') + pg_total_relation_size('raw.gfs_days')
    """).fetchone()[0]


def collect(
    start: date, end: date, scan_only: bool = False, workers: int = 4, *,
    variables: tuple[str, ...] = tuple(VARIABLES), resume: bool = False,
    measure: bool = False, now: datetime | None = None,
) -> dict:
    """End is exclusive; missing weather is recorded, never fabricated or substituted."""
    if start >= end or not 1 <= workers <= 8:
        raise ValueError("Expected increasing dates and 1 to 8 workers")
    if not variables or len(set(variables)) != len(variables) or not set(variables) <= VARIABLES.keys():
        raise ValueError("Expected distinct known GFS variables")
    if scan_only and (resume or measure):
        raise ValueError("Resume and measurement require point collection")
    started = time.perf_counter()
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    counts = {"days": len(days), "primary": 0, "fallback": 0, "weather missing": 0,
              "fallback_days": [], "missing_days": [], "skipped_days": [], "stored_days": []}
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Collection time must be aware")
    eligible_days = []
    for delivery_day in days:
        cutoff = forecast_cutoff(delivery_day)
        if now < cutoff:
            counts["skipped_days"].append(delivery_day.isoformat())
            print(f"{delivery_day} skipped: current time {now.isoformat()} is before "
                  f"cutoff {cutoff.isoformat()}", flush=True)
        else:
            eligible_days.append(delivery_day)
    if not eligible_days:
        return counts
    with connect_database() as connection, ThreadPoolExecutor(max_workers=workers) as pool:
        with connection.transaction():
            connection.execute(DDL)
        if measure:
            counts["database_bytes_before"] = database_bytes(connection)
        if resume:
            complete = stored_days(connection, start, end, variables)
            counts["stored_days"] = [day.isoformat() for day in eligible_days if day in complete]
            eligible_days = [day for day in eligible_days if day not in complete]
        measurements = []
        for selection in inspect_days(pool, eligible_days, now, workers):
            delivery_day, run, objects, _ = selection
            points = []
            status = "weather missing" if run is None else (
                "fallback" if run.fallback else "primary")
            counts[status] += 1
            if status != "primary":
                counts["fallback_days" if run else "missing_days"].append(delivery_day.isoformat())
            if not scan_only and run:
                volume = {"grib_bytes": 0, "index_bytes": 0}
                day_started = time.perf_counter()
                with requests.Session() as session:
                    points = extract_run(session, run, objects, variables, volume=volume)
                if "DSWRF" in variables:
                    print(json.dumps({"day": delivery_day.isoformat(),
                                      "sanity": radiation_sanity(run, points)}), flush=True)
            inserted_days, inserted_points = store_day(connection, selection, points)
            if measure and run:
                measurements.append({"day": delivery_day.isoformat(), **volume,
                                     "total_bytes": sum(volume.values()),
                                     "seconds": round(time.perf_counter() - day_started, 3),
                                     "point_rows": len(points),
                                     "stored_row_bytes": stored_row_bytes(connection, run, variables)})
            print(f"{delivery_day} {status} days_inserted={inserted_days} "
                  f"points_inserted={inserted_points}", flush=True)
        if measure:
            counts["measurements"] = measurements
            counts["seconds"] = round(time.perf_counter() - started, 3)
            counts["database_bytes_after"] = database_bytes(connection)
    return counts


def main() -> None:
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=today + timedelta(days=1))
    parser.add_argument("--end", type=date.fromisoformat, default=today + timedelta(days=2))
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--variables", nargs="+", choices=tuple(VARIABLES), default=tuple(VARIABLES))
    parser.add_argument("--resume", action="store_true", help="Skip fully stored requested variables")
    parser.add_argument("--measure", action="store_true", help="Report transfer, duration and row sizes")
    args = parser.parse_args()
    try:
        counts = collect(args.start, args.end, args.scan_only, args.workers,
                         variables=tuple(args.variables), resume=args.resume, measure=args.measure)
    except (psycopg.Error, requests.RequestException, KeyError, ValueError, RuntimeError,
            ET.ParseError):
        parser.exit(1, "Weather collector failed; check configuration and source availability.\n")
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()

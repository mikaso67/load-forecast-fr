"""Append-only ODRE eCO2mix ingestion. Run with python -m load_forecast.eco2mix."""

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.types.json import Jsonb

DATASETS = ("eco2mix-national-cons-def", "eco2mix-national-tr")
BASE_URL = "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets"
MONTHLY_CALL_LIMIT = 45_000

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE IF NOT EXISTS raw.eco2mix (
    source_dataset text NOT NULL,
    value_time timestamptz NOT NULL,
    payload_hash text NOT NULL,
    original_values jsonb NOT NULL,
    consommation integer,
    prevision_j1 integer,
    prevision_j integer,
    ingested_at timestamptz NOT NULL,
    PRIMARY KEY (source_dataset, value_time, payload_hash)
);
CREATE TABLE IF NOT EXISTS raw.eco2mix_api_calls (
    call_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_dataset text NOT NULL,
    range_start timestamptz NOT NULL,
    range_end timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS eco2mix_api_calls_time
ON raw.eco2mix_api_calls (attempted_at);
CREATE TABLE IF NOT EXISTS raw.eco2mix_first_appearance (
    source_dataset text NOT NULL,
    month date NOT NULL,
    first_ingested_at timestamptz NOT NULL,
    record_count integer NOT NULL,
    PRIMARY KEY (source_dataset, month)
);
"""

INSERT = """
INSERT INTO raw.eco2mix
    (source_dataset, value_time, payload_hash, original_values,
     consommation, prevision_j1, prevision_j, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_dataset, value_time, payload_hash) DO NOTHING
"""

INSERT_FIRST_APPEARANCE = """
INSERT INTO raw.eco2mix_first_appearance
    (source_dataset, month, first_ingested_at, record_count)
VALUES (%s, %s, %s, %s)
ON CONFLICT (source_dataset, month) DO NOTHING
RETURNING 1
"""


def utc_timestamp(value: str) -> datetime:
    """Reject ambiguous times rather than infer a Paris DST offset."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("ODRE date_heure must have an explicit UTC offset")
    if parsed.second or parsed.microsecond or parsed.minute % 15:
        raise ValueError("ODRE date_heure must lie on a quarter-hour boundary")
    return parsed.astimezone(UTC)


def monthly_windows(start: date, end: date) -> Iterator[tuple[datetime, datetime]]:
    """Partition half-open Paris calendar dates into UTC monthly requests."""
    if start >= end:
        raise ValueError("Start must precede exclusive end")
    paris = ZoneInfo("Europe/Paris")
    current = start
    while current < end:
        following = date(current.year + current.month // 12, current.month % 12 + 1, 1)
        stop = min(end, following)
        yield (
            datetime.combine(current, datetime.min.time(), paris).astimezone(UTC),
            datetime.combine(stop, datetime.min.time(), paris).astimezone(UTC),
        )
        current = stop


def prepare_row(dataset: str, record: dict, ingested_at: datetime) -> tuple:
    """Retain the full source object and identify distinct revisions deterministically."""
    if dataset not in DATASETS:
        raise ValueError("Unknown source dataset")
    if ingested_at.tzinfo is None:
        raise ValueError("Ingestion timestamp must be timezone aware")
    value_time = utc_timestamp(record["date_heure"])
    values = [record.get(name) for name in ("consommation", "prevision_j1", "prevision_j")]
    if any(value is not None and type(value) is not int for value in values):
        raise ValueError("Expected integer MW values or null")
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return dataset, value_time, digest, Jsonb(record), *values, ingested_at.astimezone(UTC)


def connect_database() -> psycopg.Connection:
    """Use environment credentials without constructing a printable connection URI."""
    database = os.environ.get("POSTGRES_DB", "load_forecast")
    if database != "load_forecast":
        raise ValueError("The collector must target load_forecast")
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=database,
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        options="-c timezone=UTC",
        connect_timeout=5,
        autocommit=True,
    )


class ReadOnlyRoleError(PermissionError, RuntimeError):
    """Raised when writer mode is attempted under a read-only role or without write privilege."""


def assert_writer_role(connection: psycopg.Connection) -> None:
    """Ensure the database role has write privileges before attempting any mutating SQL."""
    env_user = os.environ.get("POSTGRES_USER", "")
    if env_user == "lff_agent":
        raise ReadOnlyRoleError(
            f"Collector writer mode refused under read-only role '{env_user}'"
        )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            row = cursor.fetchone()
            current_user = row[0] if row else ""
            if current_user == "lff_agent":
                raise ReadOnlyRoleError(
                    f"Collector writer mode refused under read-only role '{current_user}'"
                )
            cursor.execute("""
                SELECT CASE
                    WHEN to_regclass('raw.eco2mix') IS NOT NULL
                    THEN has_table_privilege(current_user, 'raw.eco2mix', 'INSERT')
                    ELSE has_database_privilege(current_database(), current_user, 'CREATE')
                         OR has_schema_privilege(current_user, 'raw', 'CREATE')
                END
            """)
            priv_row = cursor.fetchone()
            if priv_row and not priv_row[0]:
                raise ReadOnlyRoleError(
                    f"Collector writer mode refused: user '{current_user}' lacks write privilege on raw.eco2mix"
                )
    except ReadOnlyRoleError:
        raise
    except Exception as exc:
        raise ReadOnlyRoleError(
            f"Collector writer mode privilege check failed: {exc}"
        ) from exc


def can_reserve_quota(connection: psycopg.Connection) -> bool:
    """Check if the connection has write privileges on raw.eco2mix_api_calls."""
    env_user = os.environ.get("POSTGRES_USER", "")
    if env_user == "lff_agent":
        return False
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT CASE
                    WHEN to_regclass('raw.eco2mix_api_calls') IS NOT NULL
                    THEN has_table_privilege(current_user, 'raw.eco2mix_api_calls', 'INSERT')
                    ELSE False
                END
            """)
            row = cursor.fetchone()
            return bool(row and row[0])
    except Exception:
        return False


def _dry_run_reservation_context() -> tuple[psycopg.Connection | None, int]:
    """Return an optional database connection for quota reservation and max_attempts."""
    conn = None
    try:
        candidate_conn = connect_database()
        if can_reserve_quota(candidate_conn):
            conn = candidate_conn
        else:
            candidate_conn.close()
    except Exception:
        conn = None
    max_attempts = 5 if conn is not None else 1
    return conn, max_attempts


def initialize(connection: psycopg.Connection) -> None:
    assert_writer_role(connection)
    with connection.transaction():
        connection.execute(DDL)


def reserve_call(
    connection: psycopg.Connection, dataset: str, start: datetime, end: datetime
) -> None:
    """Commit the quota reservation before HTTP, including retries and failed calls."""
    with connection.transaction():
        # Serialize reservations across collector processes using this database.
        connection.execute("SELECT pg_advisory_xact_lock(20210323, 50000)")
        count = connection.execute("""
            SELECT count(*) FROM raw.eco2mix_api_calls
            WHERE attempted_at >= date_trunc('month', clock_timestamp(), 'UTC')
        """).fetchone()[0]
        if count >= MONTHLY_CALL_LIMIT:
            raise RuntimeError("Monthly collector call budget exhausted")
        connection.execute(
            """INSERT INTO raw.eco2mix_api_calls (source_dataset, range_start, range_end)
               VALUES (%s, %s, %s)""",
            (dataset, start, end),
        )


def fetch_records(
    session: requests.Session,
    dataset: str,
    start: datetime,
    end: datetime,
    reserve: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 5,
) -> list[dict]:
    """Fetch one monthly export with bounded exponential backoff and no redirects."""
    if dataset not in DATASETS:
        raise ValueError("Unknown source dataset")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Expected an aware, increasing time range")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    params = {
        "where": f"date_heure >= date'{start.isoformat()}' "
        f"AND date_heure < date'{end.isoformat()}'",
        "order_by": "date_heure",
        "timezone": "UTC",
    }
    for attempt in range(max_attempts):
        reserve()
        try:
            with session.get(
                f"{BASE_URL}/{dataset}/exports/json",
                params=params,
                timeout=(10, 120),
                allow_redirects=False,
            ) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.ConnectionError("Retryable ODRE response")
                if response.status_code != 200:
                    raise ValueError(f"ODRE export failed with HTTP {response.status_code}")
                records = response.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == max_attempts - 1:
                msg = (
                    "ODRE export failed after five attempts"
                    if max_attempts == 5
                    else f"ODRE export failed after {max_attempts} attempt{'s' if max_attempts > 1 else ''}"
                )
                raise RuntimeError(msg) from None
            sleep(2 ** attempt)
            continue
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError("Unexpected ODRE export JSON structure")
        if any(not start <= utc_timestamp(row["date_heure"]) < end for row in records):
            raise ValueError("ODRE returned a timestamp outside the requested range")
        return records
    raise AssertionError("Unreachable")


def store_records(
    connection: psycopg.Connection, dataset: str, records: list[dict], ingested_at: datetime
) -> int:
    """Insert a complete validated batch atomically, preserving existing versions."""
    rows = [prepare_row(dataset, record, ingested_at) for record in records]
    if not rows:
        return 0
    with connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(INSERT, rows)
        return cursor.rowcount


def get_earliest_ingestion_time(
    connection: psycopg.Connection,
    dataset: str,
    start: datetime,
    end: datetime,
) -> datetime | None:
    """Return the earliest ingested_at timestamp for stored records in the given range."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT min(ingested_at)
            FROM raw.eco2mix
            WHERE source_dataset = %s
              AND value_time >= %s
              AND value_time < %s
            """,
            (dataset, start, end),
        )
        row = cursor.fetchone()
        if row and isinstance(row[0], datetime):
            return row[0].astimezone(UTC)
        return None


def record_first_appearance(
    connection: psycopg.Connection,
    dataset: str,
    month: date,
    ingested_at: datetime,
    record_count: int,
) -> bool:
    """Record the first ingestion time of a newly appeared month.

    Returns True if this was the first appearance, False if already recorded or empty.
    """
    if dataset not in DATASETS:
        raise ValueError("Unknown source dataset")
    if record_count <= 0:
        return False
    if ingested_at.tzinfo is None:
        raise ValueError("Ingestion timestamp must be timezone aware")
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            INSERT_FIRST_APPEARANCE,
            (dataset, month, ingested_at.astimezone(UTC), record_count),
        )
        return cursor.fetchone() is not None


def get_first_appearances(
    connection: psycopg.Connection,
    dataset: str = "eco2mix-national-cons-def",
) -> list[tuple[date, datetime, int]]:
    """Return recorded first appearances for a dataset ordered by month."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT month, first_ingested_at, record_count
            FROM raw.eco2mix_first_appearance
            WHERE source_dataset = %s
            ORDER BY month
            """,
            (dataset,),
        )
        return cursor.fetchall()


def collect(
    start: date,
    end: date,
    datasets: tuple[str, ...] = DATASETS,
    dry_run: bool = False,
) -> None:
    """Load both sources independently, including their overlap and null values."""
    if dry_run:
        conn, max_attempts = _dry_run_reservation_context()
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = "load-forecast-fr/eco2mix-collector"
                if key := os.environ.get("ODRE_API_KEY"):
                    session.headers["Authorization"] = f"Apikey {key}"
                for dataset in datasets:
                    for lower, upper in monthly_windows(start, end):
                        reserve = (
                            partial(reserve_call, conn, dataset, lower, upper)
                            if conn is not None
                            else lambda: None
                        )
                        records = fetch_records(
                            session, dataset, lower, upper, reserve,
                            max_attempts=max_attempts,
                        )
                        print(
                            f"{dataset} {lower.isoformat()} {upper.isoformat()} "
                            f"received={len(records)} [dry-run]",
                            flush=True,
                        )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return

    with connect_database() as connection, requests.Session() as session:
        assert_writer_role(connection)
        initialize(connection)
        session.headers["User-Agent"] = "load-forecast-fr/eco2mix-collector"
        if key := os.environ.get("ODRE_API_KEY"):
            session.headers["Authorization"] = f"Apikey {key}"
        for dataset in datasets:
            for lower, upper in monthly_windows(start, end):
                records = fetch_records(
                    session, dataset, lower, upper,
                    partial(reserve_call, connection, dataset, lower, upper),
                )
                inserted = store_records(connection, dataset, records, datetime.now(UTC))
                print(f"{dataset} {lower.isoformat()} {upper.isoformat()} "
                      f"received={len(records)} inserted={inserted}", flush=True)


def collect_consolidated_weekly(
    start: date = date(2026, 7, 1),
    end: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Weekly request of the consolidated eCO2mix dataset from 2026-07-01 onward.

    Queries monthly windows of eco2mix-national-cons-def, applies the standard
    retry and quota rules, stores new records, and records for each month the
    first ingestion time at which that month appears.
    """
    dataset = "eco2mix-national-cons-def"
    paris = ZoneInfo("Europe/Paris")
    if end is None:
        end = datetime.now(paris).date() + timedelta(days=1)
    if start >= end:
        raise ValueError("Start must precede exclusive end")

    results: list[dict] = []
    if dry_run:
        conn, max_attempts = _dry_run_reservation_context()
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = "load-forecast-fr/eco2mix-collector"
                if key := os.environ.get("ODRE_API_KEY"):
                    session.headers["Authorization"] = f"Apikey {key}"
                for lower, upper in monthly_windows(start, end):
                    month_start = lower.astimezone(paris).date()
                    reserve = (
                        partial(reserve_call, conn, dataset, lower, upper)
                        if conn is not None
                        else lambda: None
                    )
                    records = fetch_records(
                        session, dataset, lower, upper, reserve,
                        max_attempts=max_attempts,
                    )
                    results.append({
                        "month": month_start.isoformat(),
                        "range_start": lower.isoformat(),
                        "range_end": upper.isoformat(),
                        "received": len(records),
                        "inserted": 0,
                        "first_appearance": len(records) > 0,
                        "status": "available" if records else "not_published",
                    })
                    print(
                        f"{dataset} {lower.isoformat()} {upper.isoformat()} "
                        f"received={len(records)} [dry-run]",
                        flush=True,
                    )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return {"dataset": dataset, "mode": "weekly-consolidated", "months": results}

    with connect_database() as connection, requests.Session() as session:
        assert_writer_role(connection)
        initialize(connection)
        session.headers["User-Agent"] = "load-forecast-fr/eco2mix-collector"
        if key := os.environ.get("ODRE_API_KEY"):
            session.headers["Authorization"] = f"Apikey {key}"
        for lower, upper in monthly_windows(start, end):
            month_start = lower.astimezone(paris).date()
            records = fetch_records(
                session, dataset, lower, upper,
                partial(reserve_call, connection, dataset, lower, upper),
            )
            now = datetime.now(UTC)
            earliest = get_earliest_ingestion_time(connection, dataset, lower, upper)
            first_time = earliest if (earliest is not None and earliest < now) else now
            with connection.transaction():
                inserted = store_records(connection, dataset, records, now)
                is_first = record_first_appearance(
                    connection, dataset, month_start, first_time, len(records)
                )
            status = "not_published" if not records else ("first_appearance" if is_first else "stored")
            results.append({
                "month": month_start.isoformat(),
                "range_start": lower.isoformat(),
                "range_end": upper.isoformat(),
                "received": len(records),
                "inserted": inserted,
                "first_appearance": is_first,
                "status": status,
            })
            log_msg = (
                f"{dataset} {lower.isoformat()} {upper.isoformat()} "
                f"received={len(records)} inserted={inserted}"
            )
            if is_first:
                log_msg += f" (first appearance at {first_time.isoformat()})"
            elif not records:
                log_msg += " (not published yet)"
            print(log_msg, flush=True)

    return {"dataset": dataset, "mode": "weekly-consolidated", "months": results}


def collect_realtime_30min(
    days_back: int = 2,
    start: date | None = None,
    end: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Collect recent real-time consumption records meant to run every 30 minutes.

    Appends new and revised records of eco2mix-national-tr with ingested_at timestamp,
    providing an upper bound on the publication delay.
    """
    if days_back < 0:
        raise ValueError("days_back must be non-negative")
    dataset = "eco2mix-national-tr"
    paris = ZoneInfo("Europe/Paris")
    now_local = datetime.now(paris)
    if start is None:
        start = now_local.date() - timedelta(days=days_back)
    if end is None:
        end = now_local.date() + timedelta(days=2)
    if start >= end:
        raise ValueError("Start must precede exclusive end")

    start_dt = datetime.combine(start, datetime.min.time(), paris).astimezone(UTC)
    end_dt = datetime.combine(end, datetime.min.time(), paris).astimezone(UTC)

    if dry_run:
        conn, max_attempts = _dry_run_reservation_context()
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = "load-forecast-fr/eco2mix-collector"
                if key := os.environ.get("ODRE_API_KEY"):
                    session.headers["Authorization"] = f"Apikey {key}"
                reserve = (
                    partial(reserve_call, conn, dataset, start_dt, end_dt)
                    if conn is not None
                    else lambda: None
                )
                records = fetch_records(
                    session, dataset, start_dt, end_dt, reserve,
                    max_attempts=max_attempts,
                )
                print(
                    f"{dataset} {start_dt.isoformat()} {end_dt.isoformat()} "
                    f"received={len(records)} [dry-run]",
                    flush=True,
                )
                return {
                    "dataset": dataset,
                    "mode": "realtime-30min",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "received": len(records),
                    "inserted": 0,
                }
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    with connect_database() as connection, requests.Session() as session:
        assert_writer_role(connection)
        initialize(connection)
        session.headers["User-Agent"] = "load-forecast-fr/eco2mix-collector"
        if key := os.environ.get("ODRE_API_KEY"):
            session.headers["Authorization"] = f"Apikey {key}"
        records = fetch_records(
            session, dataset, start_dt, end_dt,
            partial(reserve_call, connection, dataset, start_dt, end_dt),
        )
        now = datetime.now(UTC)
        inserted = store_records(connection, dataset, records, now)
        print(
            f"{dataset} {start_dt.isoformat()} {end_dt.isoformat()} "
            f"received={len(records)} inserted={inserted}",
            flush=True,
        )
        return {
            "dataset": dataset,
            "mode": "realtime-30min",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "received": len(records),
            "inserted": inserted,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["all", "weekly-consolidated", "realtime-30min"],
        default="all",
        help="Collection mode: 'all' (default, backfill/custom range), "
             "'weekly-consolidated' (weekly refresh from 2026-07-01), or "
             "'realtime-30min' (30-minute real-time collection)",
    )
    parser.add_argument("--weekly-consolidated", action="store_true",
                        help="Shorthand for --mode weekly-consolidated")
    parser.add_argument("--realtime-30min", action="store_true",
                        help="Shorthand for --mode realtime-30min")
    parser.add_argument("--start", type=date.fromisoformat,
                        help="Start Paris calendar date (defaults depend on mode)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Exclusive Paris calendar date; defaults to tomorrow")
    parser.add_argument("--dataset", choices=DATASETS, action="append")
    parser.add_argument("--days-back", type=int, default=2,
                        help="Days back for realtime-30min mode (default: 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch records and report counts without database storage")
    args = parser.parse_args()

    mode = args.mode
    if args.weekly_consolidated:
        mode = "weekly-consolidated"
    elif args.realtime_30min:
        mode = "realtime-30min"

    try:
        if mode == "weekly-consolidated":
            start = args.start or date(2026, 7, 1)
            collect_consolidated_weekly(start=start, end=args.end, dry_run=args.dry_run)
        elif mode == "realtime-30min":
            collect_realtime_30min(
                days_back=args.days_back,
                start=args.start,
                end=args.end,
                dry_run=args.dry_run,
            )
        else:
            start = args.start or date(2021, 3, 23)
            end = args.end or (datetime.now(ZoneInfo("Europe/Paris")).date() + timedelta(days=1))
            collect(start, end, tuple(args.dataset) if args.dataset else DATASETS, dry_run=args.dry_run)
    except (psycopg.Error, requests.RequestException, KeyError, ValueError, RuntimeError, ReadOnlyRoleError):
        # Driver/HTTP exceptions may include credentials or request headers.
        parser.exit(1, "Collector failed; check configuration, quota and source availability.\n")


if __name__ == "__main__":
    main()

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
"""

INSERT = """
INSERT INTO raw.eco2mix
    (source_dataset, value_time, payload_hash, original_values,
     consommation, prevision_j1, prevision_j, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_dataset, value_time, payload_hash) DO NOTHING
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


def initialize(connection: psycopg.Connection) -> None:
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
) -> list[dict]:
    """Fetch one monthly export with bounded exponential backoff and no redirects."""
    if dataset not in DATASETS:
        raise ValueError("Unknown source dataset")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Expected an aware, increasing time range")
    params = {
        "where": f"date_heure >= date'{start.isoformat()}' "
        f"AND date_heure < date'{end.isoformat()}'",
        "order_by": "date_heure",
        "timezone": "UTC",
    }
    for attempt in range(5):
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
            if attempt == 4:
                raise RuntimeError("ODRE export failed after five attempts") from None
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


def collect(start: date, end: date, datasets: tuple[str, ...] = DATASETS) -> None:
    """Load both sources independently, including their overlap and null values."""
    with connect_database() as connection, requests.Session() as session:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 3, 23))
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Exclusive Paris calendar date; defaults to tomorrow")
    parser.add_argument("--dataset", choices=DATASETS, action="append")
    args = parser.parse_args()
    end = args.end or (datetime.now(ZoneInfo("Europe/Paris")).date() + timedelta(days=1))
    try:
        collect(args.start, end, tuple(args.dataset) if args.dataset else DATASETS)
    except (psycopg.Error, requests.RequestException, KeyError, ValueError, RuntimeError):
        # Driver/HTTP exceptions may include credentials or request headers.
        parser.exit(1, "Collector failed; check configuration, quota and source availability.\n")


if __name__ == "__main__":
    main()

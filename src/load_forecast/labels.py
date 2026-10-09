"""Observed SYNOP temperatures for evaluation labels only, never model features."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import time
from collections.abc import Callable
from datetime import UTC, date, datetime

import pandas as pd
import psycopg
import requests
from psycopg.types.json import Jsonb

from load_forecast.eco2mix import connect_database

STATION_IDS = (
    "07149", "07015", "07190", "07481", "07650", "07690",
    "07630", "07510", "07222", "07130", "07280", "07460",
)
BASE_URL = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP"
COLD_THRESHOLD_C = 4.84
DDL = """
CREATE SCHEMA IF NOT EXISTS labels;
CREATE TABLE IF NOT EXISTS labels.synop (
    station_id text NOT NULL,
    value_time timestamptz NOT NULL,
    temperature_k double precision,
    source_file text NOT NULL,
    payload_hash text NOT NULL,
    original_values jsonb NOT NULL,
    ingested_at timestamptz NOT NULL,
    PRIMARY KEY (source_file, station_id, value_time, payload_hash)
);
CREATE INDEX IF NOT EXISTS synop_value_time ON labels.synop (value_time);
"""
INSERT = """
INSERT INTO labels.synop
    (station_id, value_time, temperature_k, source_file, payload_hash,
     original_values, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_file, station_id, value_time, payload_hash) DO NOTHING
"""


def parse_archive(content: bytes, year: int, ingested_at: datetime) -> list[tuple]:
    """Keep original CSV strings, including null markers and non-synoptic reports."""
    if ingested_at.tzinfo is None or ingested_at.utcoffset() is None:
        raise ValueError("Ingestion time must be timezone aware")
    reader = csv.DictReader(io.StringIO(gzip.decompress(content).decode("utf-8-sig")),
                            delimiter=";")
    if not {"geo_id_wmo", "validity_time", "t"}.issubset(reader.fieldnames or []):
        raise ValueError("Unexpected SYNOP archive schema")
    rows = []
    for original in reader:
        if original["geo_id_wmo"] not in STATION_IDS:
            continue
        if None in original or any(value is None for value in original.values()):
            raise ValueError("Malformed SYNOP CSV row")
        timestamp = datetime.fromisoformat(original["validity_time"])
        if timestamp.tzinfo is None:
            raise ValueError("SYNOP timestamps must include an offset")
        timestamp = timestamp.astimezone(UTC)
        if timestamp.year != year:
            raise ValueError("SYNOP timestamp outside requested archive year")
        text = original["t"].strip()
        temperature = None if text in ("", "mq", "NaN", "nan") else float(text)
        if temperature is not None and not math.isfinite(temperature):
            raise ValueError("Non-finite SYNOP temperature")
        canonical = json.dumps(original, sort_keys=True, ensure_ascii=False)
        rows.append((original["geo_id_wmo"], timestamp, temperature,
                     f"{BASE_URL}/synop_{year}.csv.gz",
                     hashlib.sha256(canonical.encode()).hexdigest(),
                     Jsonb(original), ingested_at.astimezone(UTC)))
    if not rows:
        raise ValueError("Archive contains none of the 12 expected stations")
    return rows


def fetch_archive(
    session: requests.Session, year: int, sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Retry transient errors; a missing annual file is an explicit failure."""
    if not 2022 <= year <= datetime.now(UTC).year:
        raise ValueError("Expected an archive year from 2022 through the current year")
    for attempt in range(5):
        try:
            with session.get(f"{BASE_URL}/synop_{year}.csv.gz", timeout=(10, 120),
                             allow_redirects=False) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.ConnectionError("Transient SYNOP download failure")
                if response.status_code != 200:
                    raise ValueError(f"SYNOP download HTTP {response.status_code}")
                return response.content
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 4:
                raise RuntimeError("SYNOP download failed after five attempts") from None
            sleep(2 ** attempt)
    raise AssertionError("Unreachable")


def store_archive(connection: psycopg.Connection, rows: list[tuple]) -> int:
    """One transaction per validated archive; repetitions never replace source rows."""
    with connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(INSERT, rows)
        return cursor.rowcount


def latest_versions(rows: pd.DataFrame) -> pd.DataFrame:
    """Keep one stored version per station and time: the latest one (decision D18).

    The order is the source insert time, then the source reference time, then our ingestion time,
    then the payload hash, so the choice never depends on row order. A missing or unreadable stamp
    ranks below any readable one. The latest version is kept even if its temperature is missing.
    Labels are evaluation ground truth, never model inputs.
    """
    stamps = ["insert_time", "reference_time", "ingested_at"]
    ranked = rows.copy()
    for name in stamps:
        ranked[name] = pd.to_datetime(ranked[name], utc=True, errors="coerce", format="ISO8601")
    ranked = ranked.sort_values([*stamps, "payload_hash"], na_position="first", kind="stable")
    kept = ranked.drop_duplicates(["station_id", "value_time"], keep="last")
    return kept.sort_index()[["station_id", "value_time", "temperature_k"]]


def daily_labels(observations: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Half-open Paris dates, eight unique UTC synoptic hours and >=10 stations.

    Extra reports (for example hourly archives) are retained by ingestion but do not
    enter the eight-report mean. Conflicting temperatures for a station/time fail
    explicitly until a revision policy is approved. Missing labels remain nullable.
    """
    if start >= end:
        raise ValueError("Start must precede exclusive end")
    frame = observations[["station_id", "value_time", "temperature_k"]].copy()
    times = pd.DatetimeIndex(frame["value_time"])
    if len(times) and times.tz is None:
        raise ValueError("Observation timestamps must be timezone aware")
    times = pd.to_datetime(times, utc=True)
    frame["value_time"] = times
    frame["date"] = times.tz_convert("Europe/Paris").date
    synoptic = (times.hour % 3 == 0) & (times.minute == 0) & (times.second == 0)
    synoptic &= times.microsecond == 0
    frame = frame.loc[synoptic & frame.station_id.isin(STATION_IDS)
                      & (frame.date >= start) & (frame.date < end)]
    frame = frame.drop_duplicates(["station_id", "value_time", "temperature_k"])
    if frame.duplicated(["station_id", "value_time"]).any():
        raise ValueError("Conflicting SYNOP temperature revisions require a decision")
    frame["temperature_k"] = pd.to_numeric(frame.temperature_k, errors="raise")
    if not frame.temperature_k.dropna().map(math.isfinite).all():
        raise ValueError("Non-finite temperature")
    station_days = frame.groupby(["date", "station_id"]).temperature_k.agg(["count", "mean"])
    complete = station_days[station_days["count"] == 8]
    national = complete.groupby("date")["mean"].agg(["count", "mean"])
    index = pd.Index(pd.date_range(start, end, inclusive="left").date, name="date")
    result = national.reindex(index).rename(columns={"count": "counting_stations",
                                                    "mean": "national_temperature_c"})
    result["counting_stations"] = result.counting_stations.fillna(0).astype(int)
    # Kelvin subtraction must not turn a mean exactly at 4.84 C into a cold day.
    result["national_temperature_c"] = (result.national_temperature_c - 273.15).round(10)
    missing = result.counting_stations < 10
    result.loc[missing, "national_temperature_c"] = float("nan")
    result["cold"] = (result.national_temperature_c < COLD_THRESHOLD_C).astype("boolean")
    result.loc[missing, "cold"] = pd.NA
    return result


def read_labels(connection: psycopg.Connection, start: date, end: date) -> pd.DataFrame:
    """Read observed labels through an explicitly separate evaluation-only module."""
    if start >= end:
        raise ValueError("Start must precede exclusive end")
    lower = pd.Timestamp(start, tz="Europe/Paris").tz_convert("UTC").to_pydatetime()
    upper = pd.Timestamp(end, tz="Europe/Paris").tz_convert("UTC").to_pydatetime()
    rows = connection.execute(
        "SELECT station_id, value_time, temperature_k, "
        "original_values->>'insert_time', original_values->>'reference_time', "
        "ingested_at, payload_hash FROM labels.synop "
        "WHERE value_time >= %s AND value_time < %s", (lower, upper),
    ).fetchall()
    frame = pd.DataFrame(rows, columns=["station_id", "value_time", "temperature_k", "insert_time",
                                        "reference_time", "ingested_at", "payload_hash"])
    return daily_labels(latest_versions(frame), start, end)


def collect(years: list[int]) -> None:
    """Developer-run load with an administrator environment, with writer credentials."""
    with connect_database() as connection, requests.Session() as session:
        with connection.transaction():
            connection.execute(DDL)
        for year in years:
            content = fetch_archive(session, year)
            rows = parse_archive(content, year, datetime.now(UTC))
            inserted = store_archive(connection, rows)
            print(f"SYNOP {year}: received={len(rows)} inserted={inserted}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="+", required=True)
    arguments = parser.parse_args()
    collect(arguments.years)


if __name__ == "__main__":
    main()

"""Execute the rendered cleaning SQL locally, without shared database writes."""

import json
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from jinja2 import Environment, StrictUndefined

from load_forecast.clean_snapshot import build_clean_snapshot
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.database import clean_table

ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")


def render_model(path, variables, reference=None):
    return Environment(undefined=StrictUndefined).from_string(
        (ROOT / "dbt/models" / path).read_text(encoding="utf-8")
    ).render(
        config=lambda **kwargs: "",
        var=lambda name, default=None: variables.get(name, default),
        source=lambda *args: "eco2mix",
        ref=lambda name: f"({reference})",
    )


def execute_clean(rows, freeze=None, provenance=False):
    variables = {} if freeze is None else {"ingested_before": freeze.isoformat()}
    if provenance:
        variables['snapshot_relation'] = 'local_snapshot'
    intermediate = render_model("intermediate/int_load_30min_filtered.sql", variables)
    query = render_model("clean/load_30min.sql", variables, intermediate)
    # SQLite executes the CTE, predicates and window unchanged. Only PostgreSQL
    # timestamp syntax is adapted; Python zoneinfo supplies the same Paris clock.
    query = query.replace(
        "extract(minute FROM value_time AT TIME ZONE 'UTC')::int", "utc_minute(value_time)"
    ).replace(
        "(value_time AT TIME ZONE 'Europe/Paris')::date", "local_date(value_time)"
    ).replace(
        "to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')", "local_time(value_time)"
    ).replace("::timestamptz", "")
    with sqlite3.connect(":memory:") as connection:
        connection.create_function("utc_minute", 1, lambda t: datetime.fromisoformat(t).minute)
        connection.create_function(
            "local_date", 1,
            lambda t: datetime.fromisoformat(t).astimezone(PARIS).date().isoformat(),
        )
        connection.create_function(
            "local_time", 1,
            lambda t: datetime.fromisoformat(t).astimezone(PARIS).strftime("%H:%M"),
        )
        connection.execute(
            "CREATE TABLE eco2mix (value_time TEXT, consommation REAL, prevision_j1 REAL, "
            "prevision_j REAL, source_dataset TEXT, original_values TEXT, ingested_at TEXT, "
            "payload_hash TEXT)"
        )
        connection.executemany("INSERT INTO eco2mix VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return connection.execute(query).fetchall()


def raw_row(slot, ingested, value=45000, source="eco2mix-national-tr", hour=None):
    return (
        slot.isoformat(), value, 44000, 44500, source,
        json.dumps({"heure": hour or slot.astimezone(PARIS).strftime("%H:%M")}),
        ingested.isoformat(), str(value),
    )


def test_snapshot_keeps_selected_ingestion_and_payload_identity():
    freeze = forecast_cutoff(date(2026, 10, 9))
    slot = datetime(2026, 10, 7, 8, tzinfo=UTC)
    row = raw_row(slot, freeze - timedelta(seconds=1))
    assert execute_clean([row], freeze, provenance=True)[0][-2:] == (row[6], row[7])


@pytest.mark.parametrize("day,count", [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_all_known_snapshot_matches_unfiltered_bytes_and_d12(day, count):
    freeze = forecast_cutoff(day + timedelta(days=2))
    rows = [raw_row(slot, freeze - timedelta(hours=1)) for slot in delivery_slots(day, "30min")]
    if day.month == 3:
        rows.append(raw_row(
            datetime(2024, 3, 31, 1, tzinfo=UTC),
            freeze - timedelta(minutes=1), hour="02:00",
        ))
    expected = execute_clean(rows)
    snapshot = execute_clean(rows, freeze)
    assert len(snapshot) == count
    assert json.dumps(snapshot).encode() == json.dumps(expected).encode()


def test_truncation_excludes_boundary_and_later_revisions_before_source_priority():
    freeze = forecast_cutoff(date(2026, 10, 9))
    slot = datetime(2026, 10, 7, 8, tzinfo=UTC)
    known = [raw_row(slot, freeze - timedelta(minutes=1))]
    rows = known + [
        raw_row(slot, freeze + timedelta(seconds=1), 47000, "eco2mix-national-cons-def"),
        raw_row(slot + timedelta(minutes=30), freeze),
        raw_row(slot + timedelta(hours=1), freeze + timedelta(seconds=1)),
        raw_row(slot, freeze, 46000),
    ]
    snapshot = execute_clean(rows, freeze)
    assert snapshot == execute_clean(known, freeze) == execute_clean(known)
    assert snapshot[0][1] == 45000
    assert execute_clean(rows)[0][1] == 47000


@pytest.mark.parametrize("day,hour", [
    (date(2024, 3, 31), 11), (date(2024, 4, 1), 10),
    (date(2024, 10, 27), 10), (date(2024, 10, 28), 11),
])
def test_helper_builds_isolated_relation_and_selects_after_success(monkeypatch, day, hour):
    monkeypatch.setenv("LFF_CLEAN_TABLE", "sandbox.load_30min")
    run = Mock()
    monkeypatch.setattr("load_forecast.clean_snapshot.subprocess.run", run)
    table = build_clean_snapshot(day, schema="sandbox", now=forecast_cutoff(day))
    args, kwargs = run.call_args
    command = args[0]
    variables = json.loads(command[command.index("--vars") + 1])
    assert datetime.fromisoformat(variables["ingested_before"]).hour == hour
    assert command[command.index("--target") + 1] == "dev"
    assert kwargs["env"]["LFF_DEV_SCHEMA"] == "sandbox"
    assert kwargs["check"] is True
    assert table == f"sandbox.load_30min_snapshot_{day:%Y%m%d}"
    assert clean_table() == f'"sandbox"."load_30min_snapshot_{day:%Y%m%d}"'


def test_failed_build_does_not_switch_table(monkeypatch):
    monkeypatch.setenv("LFF_CLEAN_TABLE", "sandbox.load_30min")
    monkeypatch.setattr("load_forecast.clean_snapshot.subprocess.run", Mock(
        side_effect=subprocess.CalledProcessError(1, "dbt")
    ))
    with pytest.raises(subprocess.CalledProcessError):
        build_clean_snapshot(date(2024, 1, 2), schema="sandbox")
    assert clean_table() == '"sandbox"."load_30min"'


@pytest.mark.parametrize("schema", ["analytics", "raw", "public", "a.b", "pg_catalog", "x;drop"])
def test_helper_rejects_unsafe_schema_before_build(monkeypatch, schema):
    run = Mock()
    monkeypatch.setattr("load_forecast.clean_snapshot.subprocess.run", run)
    with pytest.raises(ValueError, match="schema"):
        build_clean_snapshot(date(2024, 1, 2), schema=schema)
    run.assert_not_called()


@pytest.mark.parametrize("now", [
    datetime(2024, 1, 1, 10, 59, tzinfo=UTC), datetime(2024, 1, 1, 12),
])
def test_helper_refuses_before_freeze_or_naive_time(monkeypatch, now):
    run = Mock()
    monkeypatch.setattr("load_forecast.clean_snapshot.subprocess.run", run)
    with pytest.raises(ValueError):
        build_clean_snapshot(date(2024, 1, 2), schema="sandbox", now=now)
    run.assert_not_called()

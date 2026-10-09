import os
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
import requests

from load_forecast.eco2mix import (
    DATASETS,
    DDL,
    INSERT,
    MONTHLY_CALL_LIMIT,
    collect,
    connect_database,
    fetch_records,
    initialize,
    monthly_windows,
    prepare_row,
    reserve_call,
    store_records,
    utc_timestamp,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(days=1)
RECORD = {"date_heure": START.isoformat(), "consommation": 0,
          "prevision_j1": None, "prevision_j": 42000, "nature": "original"}


@pytest.mark.parametrize("value", ["2024-01-01T00:00", "2024-01-01T00:01Z",
                                   "2024-01-01T00:00:01Z"])
def test_timestamp_rejects_naive_or_off_grid(value):
    with pytest.raises(ValueError):
        utc_timestamp(value)


@pytest.mark.parametrize("value,expected", [
    ("2024-10-27T02:00:00+02:00", "2024-10-27T00:00:00+00:00"),
    ("2024-10-27T02:00:00+01:00", "2024-10-27T01:00:00+00:00"),
    ("2024-03-31T03:00:00+02:00", "2024-03-31T01:00:00+00:00"),
])
def test_explicit_offsets_distinguish_dst(value, expected):
    assert utc_timestamp(value).isoformat() == expected


@pytest.mark.parametrize("start,hours", [(date(2024, 3, 31), 23),
                                        (date(2024, 10, 27), 25)])
def test_dst_request_days(start, hours):
    lower, upper = list(monthly_windows(start, start + timedelta(days=1)))[0]
    assert upper - lower == timedelta(hours=hours)
    assert lower.tzinfo is UTC and upper.tzinfo is UTC


def test_monthly_windows_are_contiguous_and_clip_end():
    windows = list(monthly_windows(date(2023, 12, 23), date(2024, 3, 2)))
    assert len(windows) == 4
    assert windows[0][0] == datetime(2023, 12, 22, 23, tzinfo=UTC)
    assert windows[-1][1] == datetime(2024, 3, 1, 23, tzinfo=UTC)
    assert all(left[1] == right[0] for left, right in zip(windows, windows[1:], strict=False))


def test_monthly_windows_rejects_empty_range():
    with pytest.raises(ValueError):
        list(monthly_windows(date(2024, 1, 1), date(2024, 1, 1)))


def test_original_null_zero_fields_and_stable_revision_hash():
    row = prepare_row(DATASETS[0], RECORD, START)
    reordered = prepare_row(DATASETS[0], dict(reversed(list(RECORD.items()))), END)
    assert row[:3] == reordered[:3]
    assert row[3].obj == RECORD
    assert row[4:7] == (0, None, 42000)
    assert row[7] == START
    assert prepare_row(DATASETS[0], RECORD | {"consommation": 1}, START)[2] != row[2]


@pytest.mark.parametrize("value", [True, 1.5, "42000"])
def test_non_integer_measurements_rejected(value):
    with pytest.raises(ValueError):
        prepare_row(DATASETS[0], RECORD | {"consommation": value}, START)


def test_prepare_row_requires_aware_ingestion_and_known_source():
    with pytest.raises(ValueError):
        prepare_row(DATASETS[0], RECORD, datetime(2024, 1, 1))
    with pytest.raises(ValueError):
        prepare_row("wrong", RECORD, START)


def response(status=200, body=None):
    result = Mock(status_code=status)
    result.json.return_value = [RECORD] if body is None else body
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def test_export_is_half_open_preserves_all_fields_and_counts_call():
    session, reserve = Mock(), Mock()
    session.get.return_value = response()
    assert fetch_records(session, DATASETS[0], START, END, reserve) == [RECORD]
    reserve.assert_called_once()
    args, kwargs = session.get.call_args
    assert args[0].endswith("/exports/json")
    assert "select" not in kwargs["params"]
    assert "date_heure >= date'" in kwargs["params"]["where"]
    assert "date_heure < date'" in kwargs["params"]["where"]
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (10, 120)


def test_transient_failure_retries_reserve_every_attempt():
    session, reserve, sleep = Mock(), Mock(), Mock()
    session.get.side_effect = [requests.Timeout(), response(429), response(503), response()]
    assert fetch_records(session, DATASETS[0], START, END, reserve, sleep) == [RECORD]
    assert reserve.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]


def test_retry_exhaustion_and_quota_stop_before_http():
    session, reserve, sleep = Mock(), Mock(), Mock()
    session.get.side_effect = requests.ConnectionError("private details")
    with pytest.raises(RuntimeError, match="five attempts"):
        fetch_records(session, DATASETS[0], START, END, reserve, sleep)
    assert session.get.call_count == 5 and reserve.call_count == 5
    session.reset_mock()
    reserve.side_effect = RuntimeError("budget")
    with pytest.raises(RuntimeError, match="budget"):
        fetch_records(session, DATASETS[0], START, END, reserve, sleep)
    session.get.assert_not_called()


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404])
def test_permanent_status_not_retried(status):
    session, sleep = Mock(), Mock()
    session.get.return_value = response(status)
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        fetch_records(session, DATASETS[0], START, END, Mock(), sleep)
    sleep.assert_not_called()


@pytest.mark.parametrize("body", [{"results": [RECORD]}, [1],
                                  [RECORD | {"date_heure": END.isoformat()}]])
def test_malformed_and_out_of_range_exports_rejected(body):
    session = Mock()
    session.get.return_value = response(body=body)
    with pytest.raises(ValueError):
        fetch_records(session, DATASETS[0], START, END, Mock())


def test_fetch_rejects_bad_inputs_before_http():
    session = Mock()
    for dataset, start, end in [("bad", START, END), (DATASETS[0], END, START),
                                (DATASETS[0], datetime(2024, 1, 1), END)]:
        with pytest.raises(ValueError):
            fetch_records(session, dataset, start, end, Mock())
    session.get.assert_not_called()


def connection_mock():
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    return connection


def test_initialize_has_utc_storage_and_revision_key():
    connection = connection_mock()
    initialize(connection)
    connection.execute.assert_called_once_with(DDL)
    assert "value_time timestamptz NOT NULL" in DDL
    assert "ingested_at timestamptz NOT NULL" in DDL
    assert "PRIMARY KEY (source_dataset, value_time, payload_hash)" in DDL


def test_reservation_serializes_and_records_calls():
    connection = connection_mock()
    connection.execute.return_value.fetchone.return_value = (MONTHLY_CALL_LIMIT - 1,)
    reserve_call(connection, DATASETS[0], START, END)
    assert "pg_advisory_xact_lock" in connection.execute.call_args_list[0].args[0]
    assert connection.execute.call_args_list[-1].args[1] == (DATASETS[0], START, END)


def test_reservation_refuses_exhausted_month():
    connection = connection_mock()
    connection.execute.return_value.fetchone.return_value = (MONTHLY_CALL_LIMIT,)
    with pytest.raises(RuntimeError, match="budget"):
        reserve_call(connection, DATASETS[0], START, END)
    assert connection.execute.call_count == 2


def test_store_is_atomic_skip_only_and_validates_before_database():
    connection = connection_mock()
    cursor = Mock(rowcount=1)
    connection.cursor.return_value = nullcontext(cursor)
    assert store_records(connection, DATASETS[0], [RECORD], START) == 1
    cursor.executemany.assert_called_once()
    assert cursor.executemany.call_args.args[0] == INSERT
    assert "DO NOTHING" in INSERT and "DO UPDATE" not in INSERT
    connection.reset_mock()
    assert store_records(connection, DATASETS[0], [], START) == 0
    with pytest.raises(ValueError):
        store_records(connection, DATASETS[0], [RECORD | {"consommation": "bad"}], START)
    connection.transaction.assert_not_called()


def test_connection_reads_environment_and_pins_target(monkeypatch):
    connect = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.psycopg.connect", connect)
    monkeypatch.setenv("POSTGRES_USER", "test-user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only")
    monkeypatch.setenv("POSTGRES_DB", "load_forecast")
    connect_database()
    assert connect.call_args.kwargs["dbname"] == "load_forecast"
    assert connect.call_args.kwargs["options"] == "-c timezone=UTC"
    assert connect.call_args.kwargs["password"] == "test-only"
    monkeypatch.setenv("POSTGRES_DB", "mlflow")
    with pytest.raises(ValueError, match="load_forecast"):
        connect_database()


def test_collect_writes_both_sources_and_reserves_each_request(monkeypatch, capsys):
    connection, session = Mock(), Mock()
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda: nullcontext(connection))
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr("load_forecast.eco2mix.initialize", Mock())
    reserve = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", reserve)
    seen = []

    def fetch(session, dataset, lower, upper, reserve):
        reserve()
        seen.append((dataset, lower, upper))
        return [RECORD]

    monkeypatch.setattr("load_forecast.eco2mix.fetch_records", fetch)
    store = Mock(return_value=1)
    monkeypatch.setattr("load_forecast.eco2mix.store_records", store)
    session.headers = {}
    collect(date(2024, 1, 1), date(2024, 1, 2))
    assert [entry[0] for entry in seen] == list(DATASETS)
    assert reserve.call_count == 2 and store.call_count == 2
    assert all(call.args[3].tzinfo is UTC for call in store.call_args_list)
    assert capsys.readouterr().out.count("received=1 inserted=1") == 2


@pytest.mark.skipif(os.environ.get("TEST_ECO2MIX_DATABASE") != "1",
                    reason="Requires an already ingested 2024 record and database credentials")
def test_postgresql_idempotence_and_utc_storage():
    """Reuse a real source record; no synthetic data, deletion or external calls."""
    with connect_database() as connection:
        original = connection.execute("""
            SELECT original_values, ingested_at FROM raw.eco2mix
            WHERE source_dataset = 'eco2mix-national-cons-def'
              AND value_time = '2024-01-01 00:00 UTC'::timestamptz
            ORDER BY payload_hash LIMIT 1
        """).fetchone()
        assert original is not None
        before = connection.execute("""
            SELECT count(*) FROM raw.eco2mix
            WHERE source_dataset = 'eco2mix-national-cons-def'
              AND value_time >= '2024-01-01 00:00 Europe/Paris'::timestamptz
              AND value_time < '2025-01-01 00:00 Europe/Paris'::timestamptz
        """).fetchone()[0]
        for _ in range(2):
            assert store_records(connection, DATASETS[0], [original[0]], END) == 0
        after = connection.execute("""
            SELECT count(*) FROM raw.eco2mix
            WHERE source_dataset = 'eco2mix-national-cons-def'
              AND value_time >= '2024-01-01 00:00 Europe/Paris'::timestamptz
              AND value_time < '2025-01-01 00:00 Europe/Paris'::timestamptz
        """).fetchone()[0]
        assert after == before
        retained = connection.execute("""
            SELECT ingested_at, pg_typeof(value_time)::text, pg_typeof(ingested_at)::text
            FROM raw.eco2mix WHERE source_dataset = 'eco2mix-national-cons-def'
              AND value_time = '2024-01-01 00:00 UTC'::timestamptz
            ORDER BY payload_hash LIMIT 1
        """).fetchone()
        assert retained[0] == original[1]
        assert retained[0].utcoffset() == timedelta(0)
        assert retained[1:] == ("timestamp with time zone", "timestamp with time zone")
        assert connection.execute("SHOW TimeZone").fetchone()[0] == "UTC"

import os
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import requests

from load_forecast.eco2mix import (
    DATASETS,
    DDL,
    INSERT,
    INSERT_FIRST_APPEARANCE,
    MONTHLY_CALL_LIMIT,
    ReadOnlyRoleError,
    assert_writer_role,
    collect,
    collect_consolidated_weekly,
    collect_realtime_30min,
    connect_database,
    fetch_records,
    get_earliest_ingestion_time,
    get_first_appearances,
    initialize,
    main,
    monthly_windows,
    prepare_row,
    record_first_appearance,
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


def connection_mock(current_user: str = "writer_user", has_privilege: bool = True):
    connection = Mock()
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    cursor.fetchall.return_value = []
    cursor.rowcount = 1

    def fake_fetchone():
        if cursor.execute.call_args:
            sql = str(cursor.execute.call_args[0][0])
            if "SELECT current_user" in sql:
                return (current_user,)
            if "has_table_privilege" in sql or "has_database_privilege" in sql:
                return (has_privilege,)
        return (True,)

    cursor.fetchone.side_effect = fake_fetchone
    connection.cursor.return_value = cursor
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
    connection, session = connection_mock(), Mock()
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


def test_ddl_includes_first_appearance_table():
    assert "CREATE TABLE IF NOT EXISTS raw.eco2mix_first_appearance" in DDL
    assert "PRIMARY KEY (source_dataset, month)" in DDL


def test_record_first_appearance_inserted_and_idempotent():
    connection = connection_mock()
    cursor = Mock()
    connection.cursor.return_value = nullcontext(cursor)
    cursor.fetchone.return_value = (1,)

    # First attempt: month newly appeared
    assert record_first_appearance(connection, DATASETS[0], date(2026, 7, 1), START, 2976) is True
    cursor.execute.assert_called_once()
    assert cursor.execute.call_args.args[0] == INSERT_FIRST_APPEARANCE
    assert cursor.execute.call_args.args[1] == (DATASETS[0], date(2026, 7, 1), START, 2976)

    # Second attempt: already recorded
    cursor.reset_mock()
    cursor.fetchone.return_value = None
    assert record_first_appearance(connection, DATASETS[0], date(2026, 7, 1), END, 2976) is False
    cursor.execute.assert_called_once()


def test_record_first_appearance_validations():
    connection = connection_mock()
    # 0 records means month not published yet: returns False without database call
    assert record_first_appearance(connection, DATASETS[0], date(2026, 9, 1), START, 0) is False
    connection.cursor.assert_not_called()

    # Naive timestamp rejected
    with pytest.raises(ValueError, match="timezone aware"):
        record_first_appearance(connection, DATASETS[0], date(2026, 7, 1), datetime(2026, 7, 1), 100)

    # Unknown dataset rejected
    with pytest.raises(ValueError, match="Unknown source dataset"):
        record_first_appearance(connection, "invalid-dataset", date(2026, 7, 1), START, 100)


def test_get_first_appearances():
    connection = connection_mock()
    cursor = Mock()
    connection.cursor.return_value = nullcontext(cursor)
    cursor.fetchall.return_value = [(date(2026, 7, 1), START, 2976)]

    result = get_first_appearances(connection, DATASETS[0])
    assert result == [(date(2026, 7, 1), START, 2976)]
    cursor.execute.assert_called_once()
    assert "raw.eco2mix_first_appearance" in cursor.execute.call_args.args[0]


def test_collect_consolidated_weekly(monkeypatch, capsys):
    connection, session = connection_mock(), Mock()
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda: nullcontext(connection))
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr("load_forecast.eco2mix.initialize", Mock())
    reserve = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", reserve)

    def fake_fetch(session, dataset, lower, upper, reserve):
        reserve()
        paris = ZoneInfo("Europe/Paris")
        if lower.astimezone(paris).month == 7:
            return [RECORD] * 10
        return []

    monkeypatch.setattr("load_forecast.eco2mix.fetch_records", fake_fetch)
    monkeypatch.setattr("load_forecast.eco2mix.store_records", lambda conn, ds, recs, ingested: len(recs))

    recorded_appearances = []

    def fake_record(conn, ds, month, ingested, count):
        recorded_appearances.append((ds, month, count))
        return count > 0

    monkeypatch.setattr("load_forecast.eco2mix.record_first_appearance", fake_record)

    session.headers = {}
    summary = collect_consolidated_weekly(start=date(2026, 7, 1), end=date(2026, 9, 1))

    assert summary["dataset"] == "eco2mix-national-cons-def"
    assert len(summary["months"]) == 2
    # July appeared
    assert summary["months"][0]["month"] == "2026-07-01"
    assert summary["months"][0]["received"] == 10
    assert summary["months"][0]["inserted"] == 10
    assert summary["months"][0]["first_appearance"] is True
    # August did not appear (0 records)
    assert summary["months"][1]["month"] == "2026-08-01"
    assert summary["months"][1]["received"] == 0
    assert summary["months"][1]["first_appearance"] is False
    assert summary["months"][1]["status"] == "not_published"

    assert len(recorded_appearances) == 2
    assert recorded_appearances[0] == ("eco2mix-national-cons-def", date(2026, 7, 1), 10)
    assert recorded_appearances[1] == ("eco2mix-national-cons-def", date(2026, 8, 1), 0)


def test_collect_consolidated_weekly_dry_run(monkeypatch, capsys):
    session = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr(
        "load_forecast.eco2mix.fetch_records",
        lambda sess, ds, low, up, res, *a, **kw: [RECORD] if low.astimezone(ZoneInfo("Europe/Paris")).month == 7 else [],
    )
    session.headers = {}

    summary = collect_consolidated_weekly(start=date(2026, 7, 1), end=date(2026, 9, 1), dry_run=True)
    assert summary["dataset"] == "eco2mix-national-cons-def"
    assert summary["months"][0]["received"] == 1
    assert summary["months"][0]["inserted"] == 0
    assert summary["months"][1]["received"] == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out


def test_collect_realtime_30min(monkeypatch, capsys):
    connection, session = connection_mock(), Mock()
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda: nullcontext(connection))
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr("load_forecast.eco2mix.initialize", Mock())
    reserve = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", reserve)
    monkeypatch.setattr(
        "load_forecast.eco2mix.fetch_records",
        lambda sess, ds, start, end, res, *a, **kw: [RECORD] * 48,
    )
    store = Mock(return_value=48)
    monkeypatch.setattr("load_forecast.eco2mix.store_records", store)
    session.headers = {}

    summary = collect_realtime_30min(days_back=1)
    assert summary["dataset"] == "eco2mix-national-tr"
    assert summary["mode"] == "realtime-30min"
    assert summary["received"] == 48
    assert summary["inserted"] == 48
    store.assert_called_once()
    assert store.call_args.args[1] == "eco2mix-national-tr"
    assert store.call_args.args[3].tzinfo is UTC


def test_collect_realtime_30min_dry_run(monkeypatch, capsys):
    session = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr(
        "load_forecast.eco2mix.fetch_records",
        lambda sess, ds, start, end, res, *a, **kw: [RECORD] * 12,
    )
    session.headers = {}

    summary = collect_realtime_30min(days_back=1, dry_run=True)
    assert summary["received"] == 12
    assert summary["inserted"] == 0
    assert "[dry-run]" in capsys.readouterr().out


def test_idempotence_simulation_two_requests_give_same_rows():
    """Simulate append-only storage and confirm identical requests insert no duplicate rows."""
    stored_rows = {}
    first_appearances = {}

    class SimCursor:
        def __init__(self):
            self.rowcount = 0
            self._last_first_seen = None

        def executemany(self, sql, rows):
            inserted = 0
            for row in rows:
                key = (row[0], row[1], row[2])  # (source_dataset, value_time, payload_hash)
                if key not in stored_rows:
                    stored_rows[key] = row
                    inserted += 1
            self.rowcount = inserted

        def execute(self, sql, params):
            if "raw.eco2mix_first_appearance" in sql:
                key = (params[0], params[1])  # (source_dataset, month)
                if key not in first_appearances:
                    first_appearances[key] = params
                    self._last_first_seen = (1,)
                else:
                    self._last_first_seen = None

        def fetchone(self):
            return self._last_first_seen

    sim_cursor = SimCursor()
    conn = Mock()
    conn.transaction.return_value = nullcontext()
    conn.cursor.return_value = nullcontext(sim_cursor)

    batch = [
        {"date_heure": "2026-07-01T00:00:00+00:00", "consommation": 45000, "prevision_j1": 44000, "prevision_j": 44000},
        {"date_heure": "2026-07-01T00:30:00+00:00", "consommation": 43000, "prevision_j1": 42000, "prevision_j": 42000},
    ]

    t1 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 7, 1, 10, 30, tzinfo=UTC)

    # First request
    ins1 = store_records(conn, DATASETS[0], batch, t1)
    app1 = record_first_appearance(conn, DATASETS[0], date(2026, 7, 1), t1, len(batch))
    assert ins1 == 2
    assert app1 is True
    assert len(stored_rows) == 2
    assert first_appearances[(DATASETS[0], date(2026, 7, 1))][2] == t1

    # Second request with identical payload
    ins2 = store_records(conn, DATASETS[0], batch, t2)
    app2 = record_first_appearance(conn, DATASETS[0], date(2026, 7, 1), t2, len(batch))
    assert ins2 == 0
    assert app2 is False
    # Rows in storage are identical: count is still 2, and ingestion time is original t1
    assert len(stored_rows) == 2
    for row in stored_rows.values():
        assert row[7] == t1
    assert first_appearances[(DATASETS[0], date(2026, 7, 1))][2] == t1


def test_main_cli_dispatch(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "load_forecast.eco2mix.collect_consolidated_weekly",
        lambda **kw: calls.append(("weekly", kw)),
    )
    monkeypatch.setattr(
        "load_forecast.eco2mix.collect_realtime_30min",
        lambda **kw: calls.append(("realtime", kw)),
    )
    monkeypatch.setattr(
        "load_forecast.eco2mix.collect",
        lambda *args, **kw: calls.append(("collect", args, kw)),
    )

    monkeypatch.setattr("sys.argv", ["eco2mix", "--mode", "weekly-consolidated"])
    main()
    assert calls[-1][0] == "weekly"

    monkeypatch.setattr("sys.argv", ["eco2mix", "--realtime-30min"])
    main()
    assert calls[-1][0] == "realtime"

    monkeypatch.setattr("sys.argv", ["eco2mix", "--start", "2024-01-01", "--end", "2024-01-02"])
    main()
    assert calls[-1][0] == "collect"


def test_dry_run_quota_reservation_exhaustion_and_single_attempt(monkeypatch):
    start, end = date(2026, 7, 1), date(2026, 7, 2)
    session = Mock(headers={})
    resp_503 = Mock(status_code=503)
    resp_503.__enter__ = Mock(return_value=resp_503)
    resp_503.__exit__ = Mock(return_value=False)
    session.get.return_value = resp_503
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    orig_fetch = fetch_records
    monkeypatch.setattr(
        "load_forecast.eco2mix.fetch_records",
        lambda *a, **kw: orig_fetch(*a, **(kw | {"sleep": lambda _: None})),
    )

    # Case A: DB reservation available, but monthly budget exhausted
    reserve_exhausted = Mock(side_effect=RuntimeError("Monthly collector call budget exhausted"))
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", reserve_exhausted)
    conn_mock = connection_mock()
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda: conn_mock)
    monkeypatch.setattr("load_forecast.eco2mix.can_reserve_quota", lambda c: True)

    session.get.reset_mock()
    with pytest.raises(RuntimeError, match="budget exhausted"):
        collect_consolidated_weekly(start=start, end=end, dry_run=True)
    # Refuses before making HTTP attempt:
    assert session.get.call_count == 0
    assert reserve_exhausted.call_count == 1

    # Case B: DB reservation available, 503 retry loop reserves on every attempt
    reserve_normal = Mock()
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", reserve_normal)
    session.get.reset_mock()
    with pytest.raises(RuntimeError, match="five attempts"):
        collect_consolidated_weekly(start=start, end=end, dry_run=True)
    assert session.get.call_count == 5
    assert reserve_normal.call_count == 5

    # Case C: DB cannot reserve (e.g. no DB connection), dry-run makes at most one attempt
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", Mock(side_effect=RuntimeError("no db")))
    session.get.reset_mock()
    with pytest.raises(RuntimeError, match="after 1 attempt"):
        collect_consolidated_weekly(start=start, end=end, dry_run=True)
    assert session.get.call_count == 1


def test_first_appearance_recovers_earliest_ingestion_time_and_transaction_atomicity(monkeypatch):
    start, end = date(2026, 7, 1), date(2026, 7, 2)
    session = Mock(headers={})
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr("load_forecast.eco2mix.initialize", Mock())
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", Mock())
    monkeypatch.setattr("load_forecast.eco2mix.fetch_records", lambda *a, **kw: [RECORD])

    # Earlier rows exist from prior interrupted run or other ingestion at t1
    t1 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    conn = connection_mock()
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda: nullcontext(conn))
    monkeypatch.setattr("load_forecast.eco2mix.get_earliest_ingestion_time", lambda c, ds, s, e: t1)
    monkeypatch.setattr("load_forecast.eco2mix.datetime", Mock(wraps=datetime, now=Mock(return_value=t2)))

    recorded = []

    def fake_record(connection, dataset, month, ingested_at, record_count):
        recorded.append((month, ingested_at, record_count))
        return True

    monkeypatch.setattr("load_forecast.eco2mix.record_first_appearance", fake_record)
    collect_consolidated_weekly(start=start, end=end)
    assert len(recorded) == 1
    # Must record t1 (earliest existing ingestion), not t2
    assert recorded[0][1] == t1

    # Atomicity: if record_first_appearance fails, transaction rolls back
    transaction_entered = []

    class TrackingTransaction:
        def __enter__(self):
            transaction_entered.append("enter")
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            transaction_entered.append(f"exit_{exc_type is not None}")
            return False

    conn.transaction = Mock(side_effect=lambda: TrackingTransaction())
    monkeypatch.setattr("load_forecast.eco2mix.record_first_appearance", Mock(side_effect=RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        collect_consolidated_weekly(start=start, end=end)
    assert "enter" in transaction_entered
    assert "exit_True" in transaction_entered


def test_writer_modes_refuse_read_only_role_zero_ddl_dml(monkeypatch):
    start, end = date(2026, 7, 1), date(2026, 7, 2)
    session = Mock(headers={})
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))

    for _mode_name, func in [
        ("collect", lambda: collect(start, end)),
        ("weekly", lambda: collect_consolidated_weekly(start=start, end=end)),
        ("realtime", lambda: collect_realtime_30min(start=start, end=end)),
    ]:
        # Case A: POSTGRES_USER=lff_agent
        conn = Mock()
        conn.transaction.return_value = nullcontext()
        conn.execute.side_effect = RuntimeError("Mutating SQL attempt on read-only role")
        monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda c=conn: nullcontext(c))
        monkeypatch.setenv("POSTGRES_USER", "lff_agent")

        with pytest.raises(ReadOnlyRoleError, match="Collector writer mode refused under read-only role 'lff_agent'"):
            func()
        # Zero DDL or DML attempts:
        assert conn.execute.call_count == 0

        # Case B: Connection query shows lacks write privilege
        monkeypatch.setenv("POSTGRES_USER", "reader_user")
        conn_b = Mock()
        conn_b.transaction.return_value = nullcontext()
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchone.side_effect = [("reader_role",), (False,)]
        conn_b.cursor.return_value = cursor
        conn_b.execute.side_effect = RuntimeError("Mutating SQL attempt")
        monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda c=conn_b: nullcontext(c))

        with pytest.raises(ReadOnlyRoleError, match="lacks write privilege"):
            func()
        assert conn_b.execute.call_count == 0


def test_assert_writer_role(monkeypatch):
    """assert_writer_role verifies write privileges using cursor queries and refuses read-only role."""
    # 1. Writer role with write privilege passes
    conn_writer = connection_mock(current_user="app_writer", has_privilege=True)
    assert_writer_role(conn_writer)

    # 2. Environment variable POSTGRES_USER=lff_agent refused immediately
    monkeypatch.setenv("POSTGRES_USER", "lff_agent")
    conn_agent = connection_mock()
    with pytest.raises(ReadOnlyRoleError, match="Collector writer mode refused under read-only role 'lff_agent'"):
        assert_writer_role(conn_agent)
    conn_agent.cursor.assert_not_called()
    monkeypatch.delenv("POSTGRES_USER", raising=False)

    # 3. Database user 'lff_agent' refused from SELECT current_user
    conn_db_agent = connection_mock(current_user="lff_agent")
    with pytest.raises(ReadOnlyRoleError, match="Collector writer mode refused under read-only role 'lff_agent'"):
        assert_writer_role(conn_db_agent)

    # 4. User lacking write privilege refused
    conn_nopriv = connection_mock(current_user="reader_role", has_privilege=False)
    with pytest.raises(ReadOnlyRoleError, match="lacks write privilege on raw.eco2mix"):
        assert_writer_role(conn_nopriv)

    # 5. Database error during privilege check wraps in ReadOnlyRoleError
    conn_err = Mock()
    cursor_err = Mock()
    cursor_err.__enter__ = Mock(return_value=cursor_err)
    cursor_err.__exit__ = Mock(return_value=False)
    cursor_err.execute.side_effect = RuntimeError("database connection failure")
    conn_err.cursor.return_value = cursor_err
    with pytest.raises(ReadOnlyRoleError, match="privilege check failed"):
        assert_writer_role(conn_err)


def test_get_earliest_ingestion_time_propagates_database_errors():
    """get_earliest_ingestion_time returns timestamps or propagates database errors without swallowing."""
    t0 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

    # 1. Existing row returns parsed UTC datetime
    conn_ok = connection_mock()
    cursor_ok = Mock()
    cursor_ok.__enter__ = Mock(return_value=cursor_ok)
    cursor_ok.__exit__ = Mock(return_value=False)
    cursor_ok.fetchone.return_value = (t0,)
    conn_ok.cursor.return_value = cursor_ok
    assert get_earliest_ingestion_time(conn_ok, DATASETS[0], START, END) == t0

    # 2. Empty table returns None
    cursor_ok.fetchone.return_value = (None,)
    assert get_earliest_ingestion_time(conn_ok, DATASETS[0], START, END) is None

    # 3. Database error is not swallowed: exception propagates
    conn_err = connection_mock()
    cursor_err = Mock()
    cursor_err.__enter__ = Mock(return_value=cursor_err)
    cursor_err.__exit__ = Mock(return_value=False)
    cursor_err.execute.side_effect = RuntimeError("relation raw.eco2mix does not exist")
    conn_err.cursor.return_value = cursor_err
    with pytest.raises(RuntimeError, match="relation raw.eco2mix does not exist"):
        get_earliest_ingestion_time(conn_err, DATASETS[0], START, END)


def test_collect_consolidated_weekly_aborts_on_earliest_ingestion_database_error(monkeypatch):
    """Database failure in get_earliest_ingestion_time propagates and does not record now."""
    start, end = date(2026, 7, 1), date(2026, 7, 2)
    session = Mock(headers={})
    monkeypatch.setattr("load_forecast.eco2mix.requests.Session", lambda: nullcontext(session))
    monkeypatch.setattr("load_forecast.eco2mix.initialize", Mock())
    monkeypatch.setattr("load_forecast.eco2mix.reserve_call", Mock())
    monkeypatch.setattr("load_forecast.eco2mix.fetch_records", lambda *a, **kw: [RECORD])

    conn = connection_mock()
    monkeypatch.setattr("load_forecast.eco2mix.connect_database", lambda: nullcontext(conn))

    record_called = []
    monkeypatch.setattr(
        "load_forecast.eco2mix.record_first_appearance",
        lambda *a, **kw: record_called.append(True),
    )

    # get_earliest_ingestion_time raises database error
    monkeypatch.setattr(
        "load_forecast.eco2mix.get_earliest_ingestion_time",
        Mock(side_effect=RuntimeError("database read error")),
    )

    with pytest.raises(RuntimeError, match="database read error"):
        collect_consolidated_weekly(start=start, end=end)

    # Must abort before recording current time as first appearance
    assert len(record_called) == 0

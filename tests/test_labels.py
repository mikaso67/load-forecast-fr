import ast
import gzip
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from load_forecast import labels


def archive(text):
    return gzip.compress(text.encode())


def observations(day, stations=labels.STATION_IDS):
    return pd.DataFrame([
        (station, datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=hour),
         273.15 + hour / 3)
        for station in stations for hour in range(0, 24, 3)
    ], columns=["station_id", "value_time", "temperature_k"])


def test_parser_preserves_original_values_nulls_and_stable_identity():
    payload = archive("geo_id_wmo;validity_time;t;name\n"
                      "07149;2023-01-01T00:00:00Z;273.15;ORLY\n"
                      "07015;2023-01-01T03:00:00Z;mq;LILLE\n"
                      "99999;2023-01-01T03:00:00Z;280;OTHER\n")
    now = datetime(2024, 1, 1, tzinfo=UTC)
    rows = labels.parse_archive(payload, 2023, now)
    assert len(rows) == 2
    assert rows[0][1] == datetime(2023, 1, 1, tzinfo=UTC)
    assert rows[0][5].obj["t"] == "273.15"
    assert rows[1][2] is None and rows[1][5].obj["t"] == "mq"
    assert rows[0][6] == now
    assert rows[0][:5] == labels.parse_archive(payload, 2023, now + timedelta(days=1))[0][:5]
    different = archive(gzip.decompress(payload).decode().replace("273.15", "274.15"))
    assert labels.parse_archive(different, 2023, now)[0][4] != rows[0][4]


@pytest.mark.parametrize("text", [
    "unexpected;t\n07149;280\n",
    "geo_id_wmo;validity_time;t\n07149;2022-01-01T00:00:00Z;280\n",
    "geo_id_wmo;validity_time;t\n07149;2023-01-01T00:00:00;280\n",
    "geo_id_wmo;validity_time;t\n07149;2023-01-01T00:00:00Z;inf\n",
    "geo_id_wmo;validity_time;t\n07149;2023-01-01T00:00:00Z;unknown\n",
    "geo_id_wmo;validity_time;t\n99999;2023-01-01T00:00:00Z;280\n",
    "geo_id_wmo;validity_time;t\n07149;2023-01-01T00:00:00Z\n",
])
def test_parser_rejects_invalid_archive(text):
    with pytest.raises(ValueError):
        labels.parse_archive(archive(text), 2023, datetime.now(UTC))


def test_parser_rejects_naive_ingestion_time():
    with pytest.raises(ValueError, match="timezone aware"):
        labels.parse_archive(b"", 2023, datetime(2023, 1, 1))


@pytest.mark.parametrize("day,hours", [(date(2024, 3, 31), 23), (date(2024, 10, 27), 25)])
def test_dst_days_have_eight_synoptic_reports_within_paris_day(day, hours):
    end = day + timedelta(days=1)
    assert pd.Timestamp(end, tz="Europe/Paris") - pd.Timestamp(day, tz="Europe/Paris") == pd.Timedelta(hours=hours)
    result = labels.daily_labels(observations(day), day, end)
    assert result.loc[day, "counting_stations"] == 12
    assert result.loc[day, "national_temperature_c"] == pytest.approx(3.5)
    assert result.loc[day, "cold"]


def test_coverage_equal_station_weights_missing_day_and_nullable_flag():
    day = date(2023, 1, 1)
    frame = observations(day)
    frame.loc[(frame.station_id == labels.STATION_IDS[0]), "temperature_k"] = 283.15
    frame = frame.drop(frame[(frame.station_id == labels.STATION_IDS[-1])].index[0])
    frame.loc[frame.station_id == labels.STATION_IDS[-2], "temperature_k"] = None
    result = labels.daily_labels(frame, day, day + timedelta(days=2))
    assert result.loc[day, "counting_stations"] == 10
    assert result.loc[day, "national_temperature_c"] == pytest.approx((10 + 9 * 3.5) / 10)
    assert result.cold.dtype == pd.BooleanDtype()
    missing = result.loc[day + timedelta(days=1)]
    assert missing.counting_stations == 0
    assert pd.isna(missing.national_temperature_c) and pd.isna(missing.cold)
    frame = frame[frame.station_id != labels.STATION_IDS[1]]
    missing = labels.daily_labels(frame, day, day + timedelta(days=1)).loc[day]
    assert missing.counting_stations == 9 and pd.isna(missing.cold)


def test_duplicates_cannot_make_incomplete_station_count_and_revisions_fail():
    day = date(2023, 1, 1)
    frame = observations(day)
    frame = frame.drop(0)
    duplicate = pd.concat([frame, frame.iloc[[0]]])
    result = labels.daily_labels(duplicate, day, day + timedelta(days=1))
    assert result.loc[day, "counting_stations"] == 11
    conflicting = frame.iloc[[0]].copy()
    conflicting["temperature_k"] += 1
    with pytest.raises(ValueError, match="revisions"):
        labels.daily_labels(pd.concat([frame, conflicting]), day, day + timedelta(days=1))


def test_hourly_reports_do_not_change_mean_and_threshold_is_strict():
    day = date(2023, 1, 1)
    frame = observations(day)
    frame["temperature_k"] = 273.15 + 4.84
    extra = frame.iloc[[0]].copy()
    extra["value_time"] += pd.Timedelta(hours=1)
    extra["temperature_k"] = 300
    result = labels.daily_labels(pd.concat([frame, extra]), day, day + timedelta(days=1))
    assert result.loc[day, "national_temperature_c"] == pytest.approx(4.84)
    assert not result.loc[day, "cold"]


def test_empty_input_naive_times_invalid_range_and_nonfinite_values():
    day = date(2023, 1, 1)
    frame = observations(day)
    result = labels.daily_labels(frame.iloc[:0], day, day + timedelta(days=1))
    assert result.counting_stations.iloc[0] == 0 and pd.isna(result.cold.iloc[0])
    frame["value_time"] = frame.value_time.dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone aware"):
        labels.daily_labels(frame, day, day + timedelta(days=1))
    with pytest.raises(ValueError, match="Start"):
        labels.daily_labels(frame, day, day)
    frame = observations(day)
    frame.loc[0, "temperature_k"] = float("inf")
    with pytest.raises(ValueError, match="Non-finite"):
        labels.daily_labels(frame, day, day + timedelta(days=1))


def test_download_retry_and_fatal_http():
    response = Mock(status_code=200, content=b"gzip")
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session, sleep = Mock(), Mock()
    session.get.side_effect = [requests.Timeout(), response]
    assert labels.fetch_archive(session, 2023, sleep) == b"gzip"
    sleep.assert_called_once_with(1)
    response.status_code = 404
    session.get.side_effect = None
    session.get.return_value = response
    with pytest.raises(ValueError, match="404"):
        labels.fetch_archive(session, 2023, sleep)
    response.status_code = 503
    with pytest.raises(RuntimeError, match="five attempts"):
        labels.fetch_archive(session, 2023, sleep)
    with pytest.raises(ValueError, match="archive year"):
        labels.fetch_archive(session, 2021, sleep)


def test_store_is_atomic_insert_only():
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    cursor = Mock(rowcount=1)
    connection.cursor.return_value = nullcontext(cursor)
    assert labels.store_archive(connection, [("test",)]) == 1
    cursor.executemany.assert_called_once_with(labels.INSERT, [("test",)])
    assert "DO NOTHING" in labels.INSERT and "UPDATE" not in labels.INSERT


def test_reader_uses_local_bounds_and_retains_missing_days():
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = []
    day = date(2024, 3, 31)
    result = labels.read_labels(connection, day, day + timedelta(days=1))
    sql, (lower, upper) = connection.execute.call_args.args
    assert "FROM labels.synop" in sql
    assert upper - lower == timedelta(hours=23)
    assert lower.tzinfo is UTC and upper.tzinfo is UTC
    assert pd.isna(result.cold.iloc[0])
    with pytest.raises(ValueError):
        labels.read_labels(connection, day, day)


def test_collect_initializes_and_stores_only_validated_archives(monkeypatch):
    connection, session = Mock(), Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.transaction.return_value = nullcontext()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(labels, "connect_database", lambda: connection)
    monkeypatch.setattr(labels.requests, "Session", lambda: session)
    monkeypatch.setattr(labels, "fetch_archive", lambda *args: b"archive")
    parse, store = Mock(return_value=[("row",)]), Mock(return_value=1)
    monkeypatch.setattr(labels, "parse_archive", parse)
    monkeypatch.setattr(labels, "store_archive", store)
    labels.collect([2023])
    connection.execute.assert_called_once_with(labels.DDL)
    store.assert_called_once_with(connection, [("row",)])
    parse.side_effect = ValueError("bad archive")
    store.reset_mock()
    with pytest.raises(ValueError):
        labels.collect([2023])
    store.assert_not_called()


def test_no_feature_module_queries_or_imports_labels():
    root = Path(__file__).resolve().parents[1] / "src" / "load_forecast"
    for path in root.glob("*.py"):
        if path.name == "labels.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "labels." not in node.value.lower(), path.name
            if isinstance(node, ast.ImportFrom):
                assert node.module != "load_forecast.labels", path.name
                assert not (node.module == "load_forecast"
                            and any(alias.name == "labels" for alias in node.names)), path.name
            if isinstance(node, ast.Import):
                assert all(alias.name != "load_forecast.labels" for alias in node.names), path.name


def versions(*rows):
    """Rows of (station, hour, kelvin, insert_time, reference_time, ingested_at, hash)."""
    base = datetime(2025, 3, 1, tzinfo=UTC)
    return pd.DataFrame([
        (station, base + timedelta(hours=hour), kelvin, insert, reference, ingested, digest)
        for station, hour, kelvin, insert, reference, ingested, digest in rows
    ], columns=["station_id", "value_time", "temperature_k", "insert_time", "reference_time",
                "ingested_at", "payload_hash"])


def kept_temperature(rows):
    kept = labels.latest_versions(rows)
    assert not kept.duplicated(["station_id", "value_time"]).any()
    return kept.temperature_k.tolist()


@pytest.mark.parametrize("flip", [False, True])
def test_latest_version_wins_whatever_the_row_order(flip):
    old = ("07149", 0, 270.0, "2025-03-01T01:00:00+00:00", "2025-03-01T00:50:00+00:00",
           "2026-01-01T00:00:00+00:00", "b")
    new = ("07149", 0, 275.0, "2025-03-02T01:00:00+00:00", "2025-03-01T00:50:00+00:00",
           "2026-01-01T00:00:00+00:00", "a")
    assert kept_temperature(versions(*((new, old) if flip else (old, new)))) == [275.0]


def test_ties_are_broken_by_reference_time_then_ingestion_time_then_hash():
    same = "2025-03-01T01:00:00+00:00"
    by_reference = versions(
        ("07149", 0, 270.0, same, "2025-03-01T00:50:00+00:00", "2026-01-01T00:00:00+00:00", "b"),
        ("07149", 0, 275.0, same, "2025-03-01T00:55:00+00:00", "2025-01-01T00:00:00+00:00", "a"))
    assert kept_temperature(by_reference) == [275.0]
    by_ingestion = versions(
        ("07149", 0, 270.0, same, same, "2026-01-01T00:00:00+00:00", "a"),
        ("07149", 0, 275.0, same, same, "2026-02-01T00:00:00+00:00", "b"))
    assert kept_temperature(by_ingestion) == [275.0]
    by_hash = versions(("07149", 0, 270.0, same, same, same, "a"), ("07149", 0, 275.0, same, same, same, "b"))
    assert kept_temperature(by_hash) == [275.0]
    assert kept_temperature(by_hash.iloc[::-1]) == [275.0]


def test_unreadable_stamps_rank_below_readable_ones_and_a_later_null_is_kept():
    stamp = "2025-03-01T01:00:00+00:00"
    rows = versions(
        ("07149", 0, 270.0, None, None, stamp, "a"),
        ("07149", 0, 275.0, "not a date", None, stamp, "b"),
        ("07149", 0, 280.0, stamp, None, stamp, "c"))
    assert kept_temperature(rows) == [280.0]
    later_null = versions(
        ("07149", 0, 270.0, stamp, None, stamp, "a"),
        ("07149", 0, float("nan"), "2025-03-02T01:00:00+00:00", None, stamp, "b"))
    assert pd.isna(kept_temperature(later_null)[0])


def test_rows_without_a_second_version_are_unchanged_and_other_keys_stay_apart():
    stamp = "2025-03-01T01:00:00+00:00"
    rows = versions(
        ("07149", 0, 270.0, stamp, stamp, stamp, "a"),
        ("07149", 3, 271.0, stamp, stamp, stamp, "b"),
        ("07015", 0, 272.0, stamp, stamp, stamp, "c"))
    kept = labels.latest_versions(rows)
    pd.testing.assert_frame_equal(kept.reset_index(drop=True),
                                  rows[["station_id", "value_time", "temperature_k"]])


def test_two_versions_that_would_conflict_give_one_label_from_the_latest():
    day = date(2025, 3, 1)
    frame = observations(day)
    stamp = "2025-03-01T01:00:00+00:00"
    frame = frame.assign(insert_time=stamp, reference_time=stamp, ingested_at=stamp,
                         payload_hash="a")
    revised = frame.iloc[[0]].assign(temperature_k=frame.temperature_k.iloc[0] + 30.0,
                                     insert_time="2025-03-05T01:00:00+00:00", payload_hash="b")
    both = pd.concat([frame, revised], ignore_index=True)
    with pytest.raises(ValueError, match="Conflicting"):
        labels.daily_labels(both[["station_id", "value_time", "temperature_k"]], day,
                            day + timedelta(days=1))
    expected = labels.daily_labels(
        pd.concat([frame.iloc[1:], revised], ignore_index=True)[
            ["station_id", "value_time", "temperature_k"]], day, day + timedelta(days=1))
    result = labels.daily_labels(labels.latest_versions(both), day, day + timedelta(days=1))
    pd.testing.assert_frame_equal(result, expected)
    assert result.national_temperature_c.iloc[0] != pytest.approx(
        labels.daily_labels(frame[["station_id", "value_time", "temperature_k"]], day,
                            day + timedelta(days=1)).national_temperature_c.iloc[0])


def test_read_labels_applies_the_latest_version_policy():
    day = date(2025, 3, 1)
    connection = Mock()
    rows = []
    for station, moment, kelvin in observations(day).itertuples(index=False):
        rows.append((station, moment, kelvin, "2025-03-01T01:00:00+00:00", None,
                     datetime(2026, 1, 1, tzinfo=UTC), "a"))
    first = rows[0]
    rows.append((first[0], first[1], first[2] + 30.0, "2025-03-09T01:00:00+00:00", None,
                 datetime(2026, 1, 1, tzinfo=UTC), "b"))
    connection.execute.return_value.fetchall.return_value = rows
    result = labels.read_labels(connection, day, day + timedelta(days=1))
    assert len(result) == 1 and result.counting_stations.iloc[0] == 12
    plain = observations(day)
    plain.loc[0, "temperature_k"] += 30.0
    expected = labels.daily_labels(plain, day, day + timedelta(days=1))
    assert result.national_temperature_c.iloc[0] == pytest.approx(
        expected.national_temperature_c.iloc[0])

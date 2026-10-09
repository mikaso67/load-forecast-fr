"""Subset collection, cutoff boundaries and complete-day resumption."""

import os
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest

from load_forecast import weather as w
from load_forecast import weather_collector as c
from load_forecast.cutoff import forecast_cutoff


@pytest.mark.parametrize("day", [date(2024, 3, 31), date(2024, 4, 1),
                                     date(2024, 10, 27), date(2024, 10, 28)])
def test_selection_itself_rejects_early_callers_without_http(monkeypatch, day):
    listing = Mock()
    monkeypatch.setattr(w, "list_objects", listing)
    with pytest.raises(ValueError, match="cutoff"):
        w.select_run(Mock(), day, now=forecast_cutoff(day) - timedelta(microseconds=1))
    listing.assert_not_called()
    run = w.candidate_runs(day)[0]
    metadata = dict.fromkeys(run.keys, w.ObjectInfo(run.init_time, 100, "etag"))
    listing.return_value = metadata
    assert w.select_run(Mock(), day, now=forecast_cutoff(day))[0] == run


def test_selection_rejects_naive_time():
    with pytest.raises(ValueError, match="aware"):
        w.select_run(Mock(), date(2024, 7, 16), now=datetime(2024, 7, 15))


def test_temperature_subset_does_not_require_other_fields():
    run = w.candidate_runs(date(2024, 7, 16))[0]
    index = "1:0:d=2024071500:TMP:2 m above ground:22 hour fcst:\n"
    assert w.index_ranges(index, run, 22, 100, ("TMP",))["TMP"][:2] == (0, 99)
    with pytest.raises(ValueError, match="missing"):
        w.index_ranges(index, run, 22, 100)
    with pytest.raises(ValueError, match="known"):
        w.index_ranges(index, run, 22, 100, ("UNKNOWN",))


def test_subset_download_counts_response_bytes(monkeypatch):
    run = w.candidate_runs(date(2024, 7, 16))[0]
    info = w.ObjectInfo(run.init_time, 100, "etag")
    reply = Mock(status_code=200, content=b"index bytes", text="index")
    monkeypatch.setattr(w, "request", lambda *args, **kwargs: nullcontext(reply))
    monkeypatch.setattr(w, "check_object", Mock())
    parse = Mock(return_value={"TMP": (0, 99, "original")})
    monkeypatch.setattr(w, "index_ranges", parse)
    fetch = Mock(return_value=b"field bytes")
    monkeypatch.setattr(w, "fetch_message", fetch)
    monkeypatch.setattr(w, "decode_points", lambda *args: [{"variable": "TMP"}])
    volume = {"index_bytes": 0, "grib_bytes": 0}
    rows = w.extract_run(Mock(), run, dict.fromkeys(run.keys, info), ("TMP",), volume=volume)
    assert len(rows) == fetch.call_count == 27
    assert all(call.args[-1] == ("TMP",) for call in parse.call_args_list)
    assert volume == {"index_bytes": 27 * len(reply.content),
                      "grib_bytes": 27 * len(b"field bytes")}


def test_resume_checks_all_requested_variables_and_complete_step_point_grid():
    day = date(2024, 7, 16)
    db = Mock()
    db.execute.return_value.fetchall.return_value = [(day,), (day,)]
    assert c.stored_days(db, day, day + timedelta(days=2), ("TMP", "RH")) == {day}
    sql, params = db.execute.call_args.args
    assert params == (day, day + timedelta(days=2), ["TMP", "RH"], list(w.POINTS), 324, 2)
    assert "count(DISTINCT (p.end_step, p.point))" in sql
    assert "GROUP BY delivery_day, selection_hash" in sql
    assert "'weather missing'" not in sql


def test_resume_skips_complete_day_and_retries_partial_day(monkeypatch):
    day = date(2024, 7, 16)
    partial_day = day + timedelta(days=1)
    db = Mock()
    db.transaction.return_value = nullcontext()
    monkeypatch.setattr(c, "connect_database", lambda: nullcontext(db))
    complete = Mock(return_value={day})
    monkeypatch.setattr(c, "stored_days", complete)
    run = w.candidate_runs(partial_day)[0]
    inspect = Mock(return_value=(partial_day, run, {}, {}))
    monkeypatch.setattr(c, "inspect_day", inspect)
    monkeypatch.setattr(c.requests, "Session", lambda: nullcontext(Mock()))
    extract = Mock(return_value=[{"variable": "TMP"}])
    monkeypatch.setattr(c, "extract_run", extract)
    sanity = Mock()
    monkeypatch.setattr(c, "radiation_sanity", sanity)
    store = Mock(return_value=(0, 324))
    monkeypatch.setattr(c, "store_day", store)
    sizes = Mock(return_value=123456)
    monkeypatch.setattr(c, "stored_row_bytes", sizes)
    monkeypatch.setattr(c, "database_bytes", Mock(return_value=8192))
    result = c.collect(day, day + timedelta(days=2), variables=("TMP",), resume=True,
                       workers=1, measure=True, now=datetime(2024, 8, 1, tzinfo=UTC))
    assert result["stored_days"] == [day.isoformat()]
    assert result["primary"] == 1
    inspect.assert_called_once_with(partial_day, now=datetime(2024, 8, 1, tzinfo=UTC))
    assert extract.call_args.args[-1] == ("TMP",)
    assert result["measurements"][0]["stored_row_bytes"] == 123456
    sanity.assert_not_called()
    store.assert_called_once()
    complete.return_value = {day, partial_day}
    inspect.reset_mock()
    result = c.collect(day, day + timedelta(days=2), variables=("TMP",), resume=True)
    assert result["primary"] == 0 and len(result["stored_days"]) == 2
    inspect.assert_not_called()


def test_row_size_query_limits_run_variables_and_steps():
    run = w.candidate_runs(date(2024, 7, 16))[1]
    db = Mock()
    db.execute.return_value.fetchone.return_value = (1000,)
    assert c.stored_row_bytes(db, run, ("TMP",)) == 1000
    assert db.execute.call_args.args[1] == (run.init_time, ["TMP"], 27, 53, list(w.POINTS))


def test_database_size_includes_both_weather_relations():
    db = Mock()
    db.execute.return_value.fetchone.return_value = (8192,)
    assert c.database_bytes(db) == 8192
    assert "pg_total_relation_size('raw.gfs_points')" in db.execute.call_args.args[0]
    assert "pg_total_relation_size('raw.gfs_days')" in db.execute.call_args.args[0]


def test_inspection_queues_only_one_bounded_batch_until_consumed():
    days = [date(2024, 7, 8) + timedelta(days=i) for i in range(7)]
    pool = Mock()
    pool.map.side_effect = lambda function, batch: iter(batch)
    selections = c.inspect_days(pool, days, datetime(2024, 8, 1, tzinfo=UTC), 2)
    assert next(selections) == days[0]
    assert pool.map.call_count == 1 and pool.map.call_args.args[1] == days[:2]
    assert list(selections) == days[1:]
    assert [call.args[1] for call in pool.map.call_args_list] == [days[:2], days[2:4],
                                                               days[4:6], days[6:]]
    with pytest.raises(ValueError, match="positive"):
        list(c.inspect_days(pool, days, datetime.now(UTC), 0))


@pytest.mark.parametrize("kwargs", [{"variables": ()}, {"variables": ("TMP", "TMP")},
                                    {"variables": ("other",)}, {"scan_only": True, "resume": True},
                                    {"scan_only": True, "measure": True},
                                    {"now": datetime(2024, 7, 15)}])
def test_backfill_rejects_invalid_options(kwargs):
    with pytest.raises(ValueError):
        c.collect(date(2024, 7, 16), date(2024, 7, 17), **kwargs)


@pytest.mark.skipif(os.environ.get("TEST_WEATHER_BACKFILL_DATABASE") != "1",
                    reason="Requires the seven real TMP sample days")
def test_real_resume_skips_tmp_but_not_the_uncollected_second_pass(monkeypatch):
    start, end = date(2024, 7, 8), date(2024, 7, 15)
    expected = {start + timedelta(days=i) for i in range(7)}
    with c.connect_database() as db:
        assert c.stored_days(db, start, end, ("TMP",)) == expected
        assert c.stored_days(db, start, end, ("TMP", "RH")) == set()
        before = db.execute("SELECT count(*) FROM raw.gfs_points").fetchone()[0]
    monkeypatch.setattr(c, "inspect_day", Mock(side_effect=AssertionError("Resume made HTTP calls")))
    result = c.collect(start, end, variables=("TMP",), resume=True)
    assert len(result["stored_days"]) == 7 and result["primary"] == 0
    with c.connect_database() as db:
        assert db.execute("SELECT count(*) FROM raw.gfs_points").fetchone()[0] == before

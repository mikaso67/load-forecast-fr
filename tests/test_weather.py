import os
import runpy
import sys
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, PropertyMock

import pytest
import requests

from load_forecast import weather as w
from load_forecast import weather_collector as c
from load_forecast.cutoff import forecast_cutoff

DAY = date(2024, 7, 16)
RUN = w.candidate_runs(DAY)[0]
MODIFIED = RUN.init_time + timedelta(hours=4)
INFO = w.ObjectInfo(MODIFIED, 1000, '"test-etag"')


def objects(run=RUN):
    return dict.fromkeys(run.keys, INFO)


def response(status=200, content=b"", headers=None):
    result = Mock(status_code=status, content=content, headers=headers or {})
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


@pytest.mark.parametrize("day,hour", [
    (date(2024, 3, 31), 11), (date(2024, 4, 1), 10),
    (date(2024, 10, 27), 10), (date(2024, 10, 28), 11),
])
def test_runs_and_cutoff_use_previous_local_day(day, hour):
    primary, fallback = w.candidate_runs(day)
    assert primary.init_time.date() == day - timedelta(days=1)
    assert primary.init_time.tzinfo is UTC
    assert primary.init_time - fallback.init_time == timedelta(hours=6)
    assert fallback.init_time.hour == 18
    assert primary.first_step == 21 and fallback.first_step == 27
    assert not primary.fallback and fallback.fallback
    assert len(primary.keys) == len(fallback.keys) == 54
    assert primary.keys[-1].endswith("f047.idx")
    assert fallback.keys[-1].endswith("f053.idx")
    assert primary.init_time + timedelta(hours=21) == (
        fallback.init_time + timedelta(hours=27))
    assert forecast_cutoff(day).hour == hour


@pytest.mark.parametrize("missing", ["file", "index", "late", "empty"])
def test_completeness_rejects_a_missing_or_unavailable_object(missing):
    metadata = objects()
    key = RUN.keys[0 if missing == "file" else 1]
    if missing in ("file", "index"):
        del metadata[key]
    else:
        metadata[key] = w.ObjectInfo(
            forecast_cutoff(DAY) if missing == "late" else MODIFIED,
            0 if missing == "empty" else 1000, INFO.etag)
    assert w.unavailable_keys(RUN, metadata, forecast_cutoff(DAY)) == [key]
    assert w.unavailable_keys(RUN, objects(), forecast_cutoff(DAY)) == []
    with pytest.raises(ValueError, match="aware"):
        w.unavailable_keys(RUN, metadata, datetime(2024, 7, 15, 10))


def test_selection_falls_back_for_missing_file_and_reports_both_missing(monkeypatch):
    primary, fallback = w.candidate_runs(DAY)
    metadata = objects(primary)
    del metadata[primary.keys[0]]
    listing = Mock(side_effect=[metadata, objects(fallback)])
    monkeypatch.setattr(w, "list_objects", listing)
    run, selected, failures = w.select_run(Mock(), DAY)
    assert run == fallback and selected == objects(fallback)
    assert failures == {primary.init_time.isoformat(): [primary.keys[0]]}
    listing.side_effect = [{}, {}]
    run, selected, failures = w.select_run(Mock(), DAY)
    assert run is None and not selected and len(failures) == 2


def test_primary_selection_does_not_query_fallback(monkeypatch):
    listing = Mock(return_value=objects())
    monkeypatch.setattr(w, "list_objects", listing)
    assert w.select_run(Mock(), DAY) == (RUN, objects(), {})
    listing.assert_called_once()


def test_request_retries_only_transient_failures():
    session, sleep = Mock(), Mock()
    session.get.side_effect = [requests.Timeout(), response(429), response(503), response()]
    assert w.request(session, w.S3_URL, sleep=sleep).status_code == 200
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]
    assert session.get.call_args.kwargs["allow_redirects"] is False
    session.get.side_effect = None
    session.get.return_value = response(403)
    with pytest.raises(ValueError, match="403"):
        w.request(session, w.S3_URL, sleep=sleep)
    session.get.side_effect = requests.ConnectionError("private details")
    with pytest.raises(RuntimeError, match="five attempts"):
        w.request(session, w.S3_URL, sleep=sleep)


def xml_page(key, truncated, token=""):
    return f'''<ListBucketResult xmlns="{w.NS['s3']}">
<Contents><Key>{key}</Key><LastModified>2024-07-15T04:00:00.000Z</LastModified>
<Size>1000</Size><ETag>"test-etag"</ETag></Contents>
<IsTruncated>{truncated}</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>
</ListBucketResult>'''.encode()


def test_s3_listing_retains_metadata_and_follows_pagination():
    session = Mock()
    session.get.side_effect = [response(content=xml_page("one", "true", "next")),
                               response(content=xml_page("two", "false"))]
    assert w.list_objects(session, RUN) == {"one": INFO, "two": INFO}
    assert session.get.call_args.kwargs["params"]["continuation-token"] == "next"


def index(step=22, duplicate=False):
    reset = 6 * ((step - 1) // 6)
    lines = [f"{i + 1}:{i * 100}:d=2024071500:{name}:{level}:"
             f"{f'{reset}-{step} hour ave fcst' if name == 'DSWRF' else f'{step} hour fcst'}:"
             for i, (name, (level, _)) in enumerate(w.VARIABLES.items())]
    lines.append(f"7:600:d=2024071500:TCDC:entire atmosphere:18-{step} hour ave fcst:")
    if duplicate:
        lines.append(f"8:700:d=2024071500:TMP:2 m above ground:{step} hour fcst:")
    return "\n".join(lines)


def test_index_selects_instantaneous_cloud_and_exact_averaging_interval():
    ranges = w.index_ranges(index(), RUN, 22, 1000)
    assert set(ranges) == set(w.VARIABLES)
    assert ranges["TCDC"][:2] == (500, 599)
    assert ranges["DSWRF"][2].endswith("18-22 hour ave fcst:")
    for invalid in (index(duplicate=True), index().replace("RH:", "other:"),
                    index().replace("d=2024071500", "d=2024071600"),
                    index().replace("2:100:", "2:0:")):
        with pytest.raises(ValueError):
            w.index_ranges(invalid, RUN, 22, 1000)


def headers():
    return {"Last-Modified": "Mon, 15 Jul 2024 04:00:00 GMT", "ETag": INFO.etag,
            "Content-Range": "bytes 0-19/1000"}


def test_volume_sums_selected_ranges_once_per_field_without_grib_downloads():
    session = Mock()
    replies = []
    for step in range(RUN.first_step, RUN.first_step + 27):
        payload = index(step).encode()
        reply = response(content=payload, headers=headers())
        reply.text = payload.decode()
        replies.append(reply)
    session.get.side_effect = replies
    volume = w.measure_run_volume(session, RUN, objects())
    assert volume["grib_bytes"] == 27 * 6 * 100
    assert volume["index_bytes"] == sum(len(reply.content) for reply in replies)
    assert volume["total_bytes"] == volume["grib_bytes"] + volume["index_bytes"]
    assert session.get.call_count == 27
    assert all(call.args[0].endswith(".idx") for call in session.get.call_args_list)
    assert all("Range" not in call.kwargs["headers"] for call in session.get.call_args_list)
    session.get.side_effect = [response(404)]
    with pytest.raises(ValueError, match="disappeared"):
        w.measure_run_volume(session, RUN, objects())
    session.get.side_effect = [response(headers=headers() | {"ETag": "changed"})]
    with pytest.raises(ValueError, match="changed"):
        w.measure_run_volume(session, RUN, objects())


def test_volume_cli_reports_measurement_and_rejects_unready_days(monkeypatch, capsys):
    script = Path(__file__).resolve().parents[1] / "scripts" / "measure_weather_volume.py"
    monkeypatch.setattr(sys, "argv", [str(script), DAY.isoformat()])
    select = Mock(return_value=(RUN, objects(), {}))
    measure = Mock(return_value={"grib_bytes": 10, "index_bytes": 2, "total_bytes": 12})
    monkeypatch.setattr(w, "select_run", select)
    monkeypatch.setattr(w, "measure_run_volume", measure)
    monkeypatch.setattr(w.requests, "Session", lambda: nullcontext(Mock()))
    runpy.run_path(str(script), run_name="__main__")
    assert '"total_bytes": 12' in capsys.readouterr().out
    measure.assert_called_once()
    select.return_value = (None, {}, {})
    with pytest.raises(SystemExit, match="2"):
        runpy.run_path(str(script), run_name="__main__")
    measure.assert_called_once()
    select.reset_mock()
    monkeypatch.setattr(sys, "argv", [str(script), "9999-01-01"])
    with pytest.raises(SystemExit, match="2"):
        runpy.run_path(str(script), run_name="__main__")
    select.assert_not_called()


def test_object_revision_and_range_integrity_are_required():
    payload = b"GRIB" + bytes(4) + (20).to_bytes(8) + b"7777"
    session = Mock()
    session.get.return_value = response(206, payload, headers())
    assert w.fetch_message(session, "key", INFO, 0, 19) == payload
    assert session.get.call_args.kwargs["headers"]["Range"] == "bytes=0-19"
    assert session.get.call_args.kwargs["stream"] is True
    for status, body, meta in (
        (200, payload, headers()), (206, payload[:-1], headers()),
        (206, payload, headers() | {"ETag": "changed"}),
        (206, payload, headers() | {"Content-Range": "bytes 1-20/1000"}),
    ):
        session.get.return_value = response(status, body, meta)
        with pytest.raises(ValueError):
            w.fetch_message(session, "key", INFO, 0, 19)


def test_range_body_read_failure_retries_without_accepting_full_download():
    payload = b"GRIB" + bytes(4) + (20).to_bytes(8) + b"7777"
    interrupted = response(206, headers=headers())
    type(interrupted).content = PropertyMock(side_effect=requests.exceptions.ChunkedEncodingError())
    session, sleep = Mock(), Mock()
    session.get.side_effect = [interrupted, response(206, payload, headers())]
    assert w.fetch_message(session, "key", INFO, 0, 19, sleep=sleep) == payload
    assert session.get.call_count == 2
    interrupted.close.assert_called_once()
    sleep.assert_called_once_with(1)


def test_decode_retains_raw_mean_and_releases_handle_on_error(monkeypatch):
    metadata = {"dataDate": 20240715, "dataTime": 0, "endStep": 22, "stepUnits": 1,
                "units": "W m**-2", "startStep": 18, "stepType": "avg", "missingValue": 9999}
    monkeypatch.setattr(w.eccodes, "codes_new_from_message", Mock(return_value=7))
    monkeypatch.setattr(w.eccodes, "codes_get", lambda handle, key: metadata[key])
    monkeypatch.setattr(w.eccodes, "codes_grib_find_nearest_multiple", Mock(return_value=[
        {"value": 123.0, "lat": 48.75, "lon": 2.25}] * len(w.POINTS)))
    release = Mock()
    monkeypatch.setattr(w.eccodes, "codes_release", release)
    points = w.decode_points(b"payload", RUN, 22, "DSWRF")
    assert len(points) == len(w.POINTS)
    assert points[0]["value"] == 123.0 and points[0]["start_step"] == 18
    assert points[0]["units"] == "W m**-2"
    metadata["stepType"] = "instant"
    with pytest.raises(ValueError, match="interval"):
        w.decode_points(b"payload", RUN, 22, "DSWRF")
    assert release.call_count == 2


def point(variable="DSWRF", start=24, end=26, value=0):
    return {"point": "paris", "latitude": 48.75, "longitude": 2.25, "value": value,
            "units": "W m**-2", "start_step": start, "end_step": end,
            "variable": variable, "message_hash": "digest", "source_url": "source",
            "file_last_modified": MODIFIED, "index_last_modified": MODIFIED,
            "index_line": "original", "byte_start": 0, "byte_end": 19}


def test_extraction_uses_each_index_and_each_variable_range(monkeypatch):
    session = Mock()
    session.get.return_value = response(headers=headers())
    session.get.return_value.text = index()
    parse = Mock(return_value={name: (0, 19, "original") for name in w.VARIABLES})
    monkeypatch.setattr(w, "index_ranges", parse)
    fetch = Mock(return_value=b"payload")
    monkeypatch.setattr(w, "fetch_message", fetch)
    monkeypatch.setattr(w, "decode_points", lambda payload, run, step, var: [point(var, end=step)])
    rows = w.extract_run(session, RUN, objects())
    assert len(rows) == fetch.call_count == 27 * 6
    assert parse.call_count == 27
    assert rows[-1]["source_url"].endswith("f047")
    assert len(rows[-1]["message_hash"]) == 64


def test_day_and_point_rows_preserve_missing_status_and_utc_provenance():
    first = c.day_row(DAY, RUN, objects(), {}, MODIFIED)
    second = c.day_row(DAY, RUN, objects(), {}, MODIFIED + timedelta(days=1))
    assert first[:7] == second[:7]
    assert first[3:6] == ("primary", RUN.init_time, False)
    missing = c.day_row(DAY, None, {}, {"absent": ["file"]}, MODIFIED)
    assert missing[3:7] == ("weather missing", None, None, None)
    row = c.point_row(RUN, point(), MODIFIED)
    assert row[7] == 0 and row[9] == 24
    assert row[10] == RUN.init_time + timedelta(hours=26)
    assert row[14].obj["index_line"] == "original"


def test_raw_radiation_sanity_checks_negative_and_wholly_night_intervals():
    assert c.radiation_sanity(RUN, [point()])["night_max"] == 0
    for invalid in ([point(value=-1)], [point(value=1)], [point(variable="TMP")]):
        with pytest.raises(ValueError):
            c.radiation_sanity(RUN, invalid)
    # A running mean that includes sunset can be positive even at a night endpoint.
    assert c.radiation_sanity(RUN, [point(), point(start=18, end=24, value=50)])["dswrf_max"] == 50


def connection():
    db = Mock()
    db.transaction.return_value = nullcontext()
    cursor = Mock(rowcount=1)
    db.cursor.return_value = nullcontext(cursor)
    return db, cursor


def test_store_is_atomic_append_only_with_revision_key():
    db, cursor = connection()
    assert c.store_day(db, (DAY, RUN, objects(), {}), [point()]) == (1, 1)
    assert cursor.execute.call_args.args[0] == c.INSERT_DAY
    assert cursor.executemany.call_args.args[0] == c.INSERT_POINT
    assert "DO NOTHING" in c.INSERT_POINT and "DO UPDATE" not in c.INSERT_POINT
    assert "valid_time timestamptz NOT NULL" in c.DDL
    assert "fallback boolean" in c.DDL
    assert c.store_day(db, (DAY, None, {}, {}), []) == (1, 0)


def test_inspect_day_uses_dedicated_session(monkeypatch):
    session = Mock(headers={})
    monkeypatch.setattr(c.requests, "Session", lambda: nullcontext(session))
    monkeypatch.setattr(c, "select_run", Mock(return_value=(RUN, objects(), {})))
    assert c.inspect_day(DAY) == (DAY, RUN, objects(), {})


@pytest.mark.parametrize("day", [
    DAY, date(2024, 3, 31), date(2024, 4, 1),
    date(2024, 10, 27), date(2024, 10, 28),
])
@pytest.mark.parametrize("scan_only", [False, True])
def test_collect_before_cutoff_skips_without_selection_or_writes(monkeypatch, capsys, day,
                                                               scan_only):
    clock = Mock()
    clock.now.return_value = forecast_cutoff(day) - timedelta(microseconds=1)
    monkeypatch.setattr(c, "datetime", clock)
    inspect, connect, extract, store = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(c, "inspect_day", inspect)
    monkeypatch.setattr(c, "connect_database", connect)
    monkeypatch.setattr(c, "extract_run", extract)
    monkeypatch.setattr(c, "store_day", store)
    counts = c.collect(day, day + timedelta(days=1), scan_only)
    assert counts["skipped_days"] == [day.isoformat()]
    assert counts["primary"] == counts["fallback"] == counts["weather missing"] == 0
    for operation in (inspect, connect, extract, store):
        operation.assert_not_called()
    assert f"before cutoff {forecast_cutoff(day).isoformat()}" in capsys.readouterr().out


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(microseconds=1)])
@pytest.mark.parametrize("day", [DAY, date(2024, 3, 31), date(2024, 10, 27)])
def test_collect_at_or_after_cutoff_stores_primary_and_skips_future_day(monkeypatch, day,
                                                                     offset):
    clock = Mock()
    clock.now.return_value = forecast_cutoff(day) + offset
    monkeypatch.setattr(c, "datetime", clock)
    db, cursor = connection()
    monkeypatch.setattr(c, "connect_database", lambda: nullcontext(db))
    run = w.candidate_runs(day)[0]
    metadata = dict.fromkeys(run.keys, w.ObjectInfo(
        run.init_time + timedelta(hours=4), INFO.size, INFO.etag))
    monkeypatch.setattr(w, "list_objects", Mock(return_value=metadata))
    monkeypatch.setattr(c.requests, "Session", lambda: nullcontext(Mock(headers={})))
    extract = Mock(return_value=[point()])
    monkeypatch.setattr(c, "extract_run", extract)
    counts = c.collect(day, day + timedelta(days=2), workers=1)
    assert counts["primary"] == 1
    assert counts["skipped_days"] == [(day + timedelta(days=1)).isoformat()]
    assert cursor.execute.call_count == cursor.executemany.call_count == 1
    raw_day = cursor.execute.call_args.args[1]
    assert raw_day[0] == day and raw_day[3] == "primary" and raw_day[4] == run.init_time
    extract.assert_called_once()


@pytest.mark.parametrize("scan_only", [False, True])
def test_collect_reports_fallback_and_missing_without_inventing_points(monkeypatch, scan_only):
    db, _ = connection()
    monkeypatch.setattr(c, "connect_database", lambda: nullcontext(db))
    fallback = w.candidate_runs(DAY)[1]
    selections = iter([(DAY, fallback, objects(fallback), {}),
                       (DAY + timedelta(days=1), None, {}, {})])
    monkeypatch.setattr(c, "inspect_day", lambda day, **kwargs: next(selections))
    monkeypatch.setattr(c.requests, "Session", lambda: nullcontext(Mock()))
    extract = Mock(return_value=[point(start=30, end=32)])
    monkeypatch.setattr(c, "extract_run", extract)
    store = Mock(return_value=(1, 0))
    monkeypatch.setattr(c, "store_day", store)
    counts = c.collect(DAY, DAY + timedelta(days=2), scan_only, workers=1)
    assert counts["fallback"] == counts["weather missing"] == 1
    assert counts["fallback_days"] == [DAY.isoformat()]
    assert extract.call_count == (0 if scan_only else 1)
    assert store.call_args.args[2] == []
    with pytest.raises(ValueError):
        c.collect(DAY, DAY, scan_only)


def test_cli_defaults_to_next_paris_delivery_day_and_suppresses_private_errors(monkeypatch):
    clock = Mock()
    clock.now.return_value = datetime(2024, 7, 15, 12, tzinfo=UTC)
    monkeypatch.setattr(c, "datetime", clock)
    monkeypatch.setattr(sys, "argv", ["weather_collector"])
    collect = Mock(return_value={"days": 1})
    monkeypatch.setattr(c, "collect", collect)
    c.main()
    collect.assert_called_once_with(DAY, DAY + timedelta(days=1), False, 4,
                                   variables=tuple(w.VARIABLES), resume=False, measure=False)
    collect.side_effect = ValueError("private configuration details")
    with pytest.raises(SystemExit) as error:
        c.main()
    assert error.value.code == 1


@pytest.mark.skipif(os.environ.get("TEST_WEATHER_DATABASE") != "1",
                    reason="Requires the real July 2024 sample and database credentials")
def test_postgresql_raw_weather_idempotence_and_utc():
    """Reinsert existing originals only; no synthetic rows or external requests."""
    with c.connect_database() as db:
        raw_day = db.execute("""
            SELECT original_metadata, ingested_at FROM raw.gfs_days
            WHERE delivery_day = '2024-07-16' AND status = 'primary'
            ORDER BY ingested_at LIMIT 1
        """).fetchone()
        assert raw_day is not None
        metadata, first_seen = raw_day
        manifest = {key: w.ObjectInfo(datetime.fromisoformat(value["last_modified"]),
                                    value["size"], value["etag"])
                    for key, value in metadata["objects"].items()}
        original = db.execute("""
            SELECT end_step, variable, point, message_hash, latitude, longitude,
                   value, units, start_step, source_url, file_last_modified,
                   index_last_modified, original_metadata, ingested_at
            FROM raw.gfs_points WHERE run_init_time = '2024-07-15 00:00 UTC'
            ORDER BY end_step, variable, point LIMIT 1
        """).fetchone()
        names = ("end_step", "variable", "point", "message_hash", "latitude", "longitude",
                 "value", "units", "start_step", "source_url", "file_last_modified",
                 "index_last_modified", "original_metadata", "ingested_at")
        source = dict(zip(names, original, strict=True))
        source.update(source.pop("original_metadata"))
        before = db.execute("SELECT count(*) FROM raw.gfs_points").fetchone()[0]
        for _ in range(2):
            assert c.store_day(db, (DAY, RUN, manifest, metadata["failures"]), [source]) == (0, 0)
        assert db.execute("SELECT count(*) FROM raw.gfs_points").fetchone()[0] == before
        assert db.execute("""
            SELECT ingested_at FROM raw.gfs_days WHERE delivery_day = '2024-07-16'
            AND status = 'primary' ORDER BY ingested_at LIMIT 1
        """).fetchone()[0] == first_seen
        retained = db.execute("""
            SELECT ingested_at, pg_typeof(valid_time)::text, pg_typeof(run_init_time)::text
            FROM raw.gfs_points WHERE run_init_time = '2024-07-15 00:00 UTC'
            ORDER BY end_step, variable, point LIMIT 1
        """).fetchone()
        assert retained[0] == source["ingested_at"]
        assert retained[0].utcoffset() == timedelta(0)
        assert retained[1:] == ("timestamp with time zone", "timestamp with time zone")
        assert db.execute("SHOW TimeZone").fetchone()[0] == "UTC"

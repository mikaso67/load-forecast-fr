"""Exercise the real SQL tables and filesystem protocol without shared writes."""

import copy
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DatabaseError, IntegrityError

import load_forecast.serving as serving
from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.serving import (
    ForecastStore,
    ServingAuthority,
    delivery_deadline,
    validate_values,
)

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2024, 4, 3)


def values(day=DAY, point=45000.0, intervals=True):
    return [{
        "value_time": slot.isoformat(),
        "local_slot": slot.astimezone(PARIS).hour * 2 + slot.astimezone(PARIS).minute // 30,
        "point": point, "lower": point - 100 if intervals else None,
        "upper": point + 200 if intervals else None, "n_pool": 60 if intervals else None,
    } for slot in delivery_slots(day, "30min")]


def run_archive(run_id="run1", day=DAY, kind="primary", variant="B"):
    freeze = forecast_cutoff(day)
    start = {"run_id": run_id, "delivery_day": day.isoformat(), "kind": kind,
             "variant": variant,
             "freeze_time": freeze.isoformat(), "started_at": freeze.isoformat(),
             "code_commit": "abc123"}
    return {
        "kind": kind, "id": run_id, "delivery_day": day.isoformat(), "start": start,
        "variant": variant,
        "result": {"run_id": run_id, "completed_at": freeze.isoformat(),
                   "status": "complete", "refit_day": "2024-04-01", "mlflow_run_id": "tracking1",
                   "kalman_quarter": "2024Q2", "weather_status": "primary",
                   "last_consumption_slot": (freeze - timedelta(hours=2, minutes=30)).isoformat(),
                   "max_ingested_at": (freeze - timedelta(seconds=1)).isoformat(),
                   "feature_hash": "features", "flags": {"stale_models": False}},
        "values": values(day),
        "experts": [{"value_time": row["value_time"], "expert": expert,
                     "value": row["point"], "backup": None}
                    for row in values(day) for expert in ("gam", "gbm", "kalman", "linear")],
        "weather": [{"selection_hash": "selection", "run_init_time":
                     freeze.replace(hour=0, minute=0).isoformat(),
                     "file_last_modified": (freeze - timedelta(minutes=1)).isoformat(),
                     "index_last_modified": (freeze - timedelta(minutes=1)).isoformat(),
                     "end_step": 24, "variable": "TMP", "point": "Paris", "message_hash": "msg"}],
        "states": [{"component": "absorbed", "state_key": "2024-04-01", "payload": {
                "snapshot": "snapshot1"}},
            {"component": "aggregation", "state_key": "shared", "payload": {
            "weights": [0.25] * 4, "regret": [1.0] * 4, "squared_regret": [1.0] * 4}},
            {"component": "kalman", "state_key": "0", "payload": {
                "mean": [1.0], "covariance": [[2.0]]}}],
    }


def fallback_archive(day=DAY, fallback_id="fallback1"):
    return {"kind": "fallback", "id": fallback_id, "delivery_day": day.isoformat(),
            "prepared_at": (forecast_cutoff(day) - timedelta(hours=23)).isoformat(),
            "models": ["calendar"], "completeness": {"complete": True},
            "values": values(day, point=44000.0, intervals=False)}


@pytest.fixture
def store(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    database = tmp_path / "forecast.sqlite"

    @event.listens_for(engine, "connect")
    def attach(connection, _):
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("ATTACH DATABASE ? AS forecast", (str(database),))
        connection.execute("PRAGMA busy_timeout = 10000")

    sql = (ROOT / "infra/postgres/forecast.sql").read_text(encoding="utf-8")
    tables = sql.split("-- BEGIN TABLES", 1)[1].split("\n", 1)[1].split("-- END TABLES")[0]
    with engine.connect() as connection:
        connection.connection.driver_connection.executescript(tables)
    yield ForecastStore(engine)
    engine.dispose()


def count(store, table):
    with store.engine.connect() as connection:
        return connection.execute(text(f'SELECT count(*) FROM forecast."{table}"')).scalar_one()


def test_variants_keep_independent_first_state_runs(store):
    first = [run_archive(f"{variant}1", variant=variant) for variant in ("A", "B")]
    for archive in first:
        store.start_run(archive["start"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(store.complete_run, first))
    for variant in ("A", "B"):
        archive = run_archive(f"{variant}2", variant=variant)
        store.start_run(archive["start"])
        store.complete_run(archive)
        assert store.replay_run(archive["id"]) == archive
    with store.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT variant, run_id FROM forecast.state_runs ORDER BY variant"
        )).all()
    assert rows == [("A", "A1"), ("B", "B1")]
    for archive in first:
        assert store.replay_run(archive["id"]) == archive


def test_reader_refuses_a_forged_shadow_choice(tmp_path):
    authority = ServingAuthority(tmp_path, clock=lambda: forecast_cutoff(DAY))
    reference = authority.prepare_values(run_archive(variant="A"))
    choice = {**reference, "chosen_at": forecast_cutoff(DAY).isoformat()}
    choice["choice_hash"] = serving._hash(choice)
    path = authority._choice_path(DAY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(choice), encoding="utf-8")
    with pytest.raises(ValueError, match="must reference variant B"):
        authority.read_served(DAY)


def test_database_rejects_a_state_run_with_the_wrong_variant(store):
    archive = run_archive(kind="replay", variant="A")
    store.start_run(archive["start"])
    store.complete_run(archive)
    with pytest.raises(IntegrityError), store.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO forecast.state_runs (delivery_day, variant, run_id) "
            "VALUES (:day, 'B', :run)"
        ), {"day": DAY.isoformat(), "run": archive["id"]})


def test_database_rejects_forecast_rows_with_the_wrong_variant(store):
    archive = run_archive(variant="A")
    store.start_run(archive["start"])
    store.complete_run(archive)
    row = archive["values"][0]
    with pytest.raises(IntegrityError), store.engine.begin() as connection:
        connection.execute(text(
            'UPDATE forecast."values" SET variant = :variant '
            'WHERE run_id = :run AND value_time = :instant'
        ), {"variant": "B", "run": archive["id"], "instant": row["value_time"]})


@pytest.mark.parametrize("variant", ["", "C", None])
def test_unknown_variant_refused_before_start_event(store, variant):
    archive = run_archive(variant=variant)
    with pytest.raises(ValueError, match="Expected variant"):
        store.start_run(archive["start"])
    assert count(store, "run_starts") == 0


def test_nested_forecast_variant_is_not_silently_overwritten(store):
    archive = run_archive()
    archive["values"][0]["variant"] = "A"
    store.start_run(archive["start"])
    with pytest.raises(ValueError, match="inherit variant"):
        store.complete_run(archive)
    assert count(store, "run_results") == 0


def test_shadow_can_never_publish_choice(tmp_path):
    authority = ServingAuthority(tmp_path, clock=lambda: forecast_cutoff(DAY))
    with pytest.raises(ValueError, match="Only variant B"):
        authority.publish_primary(run_archive(variant="A"))
    assert not (tmp_path / "choice").exists()


def test_variant_mismatch_refused_before_storage(store):
    archive = run_archive(variant="A")
    archive["start"]["variant"] = "B"
    store.start_run(archive["start"])
    with pytest.raises(ValueError, match="same A/B"):
        store.complete_run(archive)
    assert count(store, "run_results") == 0


def test_migration_grants_insert_select_only_and_no_ownership():
    sql = (ROOT / "infra/postgres/forecast.sql").read_text(encoding="utf-8")
    assert "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA forecast TO lff_forecast_writer;" in sql
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO lff_forecast_reader;" in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA forecast" in sql
    assert "GRANT UPDATE" not in sql and "GRANT DELETE" not in sql
    assert "GRANT ALL" not in sql and "OWNER TO lff_forecast" not in sql
    assert "SET LOCAL search_path = forecast, pg_catalog;" in sql


def test_sqlite_runtime_role_cannot_update_delete_or_drop(store):
    # SQLite has no roles; its authorizer enforces the migration's runtime grant
    # set. PostgreSQL GRANT execution remains a deployment check.
    archive = run_archive()
    store.start_run(archive["start"])
    with store.engine.connect() as connection:
        raw = connection.connection.driver_connection
        denied = {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE,
                  sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_TABLE}
        raw.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in denied
                           else sqlite3.SQLITE_OK)
        try:
            for statement in (
                "UPDATE forecast.run_starts SET code_commit = 'changed'",
                "DELETE FROM forecast.run_starts", "DROP TABLE forecast.run_starts",
            ):
                with pytest.raises(DatabaseError):
                    connection.execute(text(statement))
            assert connection.execute(text("SELECT count(*) FROM forecast.run_starts")).scalar() == 1
        finally:
            raw.set_authorizer(None)
    store.complete_run(archive)
    assert count(store, "run_results") == 1


def test_replay_of_full_committed_run_and_failed_or_crashed_events(store):
    archive = run_archive()
    store.start_run(archive["start"])
    with pytest.raises(ValueError, match="no complete result"):
        store.replay_run(archive["id"])
    store.complete_run(archive)
    assert store.replay_run("run1") == archive
    with pytest.raises(IntegrityError):
        store.complete_run(archive)
    failed = run_archive("failed")
    store.start_run(failed["start"])
    store.fail_run("failed", forecast_cutoff(DAY), {"error": "missing input"})
    with pytest.raises(ValueError, match="no complete result"):
        store.replay_run("failed")
    assert count(store, "state_runs") == 1


def test_failed_result_transaction_rolls_back_all_outputs(store):
    archive = run_archive()
    store.start_run(archive["start"])
    archive["experts"].append(copy.deepcopy(archive["experts"][0]))
    with pytest.raises(IntegrityError):
        store.complete_run(archive)
    assert count(store, "run_results") == count(store, "values") == count(store, "state_runs") == 0
    assert count(store, "run_starts") == 1


@pytest.mark.parametrize("day,size", [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_dst_full_file_and_database_replay(store, tmp_path, day, size):
    archive = run_archive(day=day)
    store.start_run(archive["start"])
    store.complete_run(archive)
    authority = ServingAuthority(tmp_path / "serving", clock=lambda: forecast_cutoff(day))
    choice = authority.publish_primary(store.replay_run(archive["id"]))
    assert choice["kind"] == "primary"
    assert len(authority.read_served(day)["values"]) == size
    store.reconcile(authority, day)
    assert store.served_for_scoring(authority, day) == archive


@pytest.mark.parametrize("day,hour", [(date(2024, 4, 1), 10), (date(2024, 10, 28), 11)])
def test_deadline_paris_conversion_and_guards(tmp_path, day, hour):
    assert delivery_deadline(day).hour == hour
    assert delivery_deadline(day).minute == 30
    before = ServingAuthority(tmp_path, lambda: delivery_deadline(day) - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="before 12:30"):
        before.deadline_check(day)
    at = ServingAuthority(tmp_path, lambda: delivery_deadline(day))
    with pytest.raises(TimeoutError):
        at.publish_primary(run_archive(day=day))
    assert at.deadline_check(day)["kind"] == "none"
    assert at.read_served(day) is None
    assert at.publish_primary(run_archive(day=day))["kind"] == "none"


def _publish_process(root, archive, barrier, queue):
    authority = ServingAuthority(Path(root), lambda: forecast_cutoff(DAY))
    original = serving._no_replace

    def simultaneous(source, target):
        if target.parent.name == "choice":
            barrier.wait(timeout=15)
        original(source, target)

    serving._no_replace = simultaneous
    try:
        if archive is None:
            authority.clock = lambda: delivery_deadline(DAY)
            choice = authority.deadline_check(DAY)
        else:
            choice = authority.publish_primary(archive)
        queue.put(("ok", choice["choice_hash"]))
    except Exception as error:
        queue.put(("error", repr(error)))


def test_primary_and_deadline_race_in_separate_processes(tmp_path):
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY))
    authority.prepare_fallback(fallback_archive())
    archive = run_archive()
    authority.prepare_values(archive)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_publish_process, args=(str(tmp_path), item, barrier, queue))
                 for item in (archive, None)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join()
        assert process.exitcode == 0
    results = [queue.get(timeout=5) for _ in processes]
    assert all(status == "ok" for status, _ in results), results
    assert len({digest for _, digest in results}) == 1
    assert len(list((tmp_path / "choice").glob("*.json"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))
    assert authority.read_served(DAY)["kind"] in ("primary", "fallback")


def test_database_unreachable_after_primary_commit_does_not_change_choice(store, tmp_path, monkeypatch):
    archive = run_archive()
    store.start_run(archive["start"])
    store.complete_run(archive)
    committed = store.replay_run("run1")

    def down(*args, **kwargs):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(store.engine, "connect", down)
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY))
    authority.prepare_fallback(fallback_archive())
    primary = authority.publish_primary(committed)
    authority.clock = lambda: delivery_deadline(DAY)
    assert authority.deadline_check(DAY) == primary
    assert authority.read_served(DAY) == archive
    with pytest.raises(ConnectionError):
        store.reconcile(authority, DAY)


def test_database_unreachable_without_choice_serves_prepared_fallback(store, tmp_path, monkeypatch):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    authority.prepare_fallback(fallback_archive())
    monkeypatch.setattr(store.engine, "connect", lambda: (_ for _ in ()).throw(ConnectionError()))
    assert authority.deadline_check(DAY)["kind"] == "fallback"
    assert authority.read_served(DAY) == fallback_archive()


def test_actual_process_exit_after_fsync_before_rename_is_ignored(tmp_path):
    archive = run_archive()
    source = tmp_path / "archive.json"
    source.write_text(json.dumps(archive), encoding="utf-8")
    program = '''
import json, os, sys
from datetime import date
from pathlib import Path
import load_forecast.serving as serving
from load_forecast.cutoff import forecast_cutoff
original = serving._no_replace
def die(source, target):
    if target.parent.name == 'choice':
        os._exit(73)
    original(source, target)
serving._no_replace = die
authority = serving.ServingAuthority(Path(sys.argv[1]), lambda: forecast_cutoff(date(2024, 4, 3)))
authority.publish_primary(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')))
'''
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path), str(source)],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, result.stderr
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    with pytest.raises(FileNotFoundError):
        authority.read_choice(DAY)
    abandoned = list((tmp_path / "choice").glob("*.tmp"))
    assert len(abandoned) == 1
    authority.prepare_fallback(fallback_archive())
    choice = authority.deadline_check(DAY)
    assert choice["kind"] == "fallback"
    old = delivery_deadline(DAY).timestamp() - 3601
    os.utime(abandoned[0], (old, old))
    assert authority.cleanup_temporary() == 1
    assert authority.read_choice(DAY) == choice


def test_paused_writer_resumes_after_fallback_wins(tmp_path, monkeypatch):
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY))
    authority.prepare_fallback(fallback_archive())
    ready, resume = threading.Event(), threading.Event()
    original = serving._no_replace

    def pause(source, target):
        if threading.current_thread().name == "primary" and target.parent.name == "choice":
            ready.set()
            assert resume.wait(timeout=10)
        original(source, target)

    monkeypatch.setattr(serving, "_no_replace", pause)
    results = []
    thread = threading.Thread(name="primary", target=lambda: results.append(
        authority.publish_primary(run_archive())))
    thread.start()
    assert ready.wait(timeout=10)
    deadline = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    winner = deadline.deadline_check(DAY)
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results == [winner]
    assert winner["kind"] == "fallback"


def test_reconcile_imports_absent_rows_is_idempotent_and_scores_by_file_hash(store, tmp_path):
    first = ServingAuthority(tmp_path / "older", lambda: forecast_cutoff(DAY))
    first.publish_primary(run_archive())
    mirror = store.reconcile(first, DAY)
    assert store.replay_run("run1") == run_archive()
    authority = ServingAuthority(tmp_path / "current", lambda: delivery_deadline(DAY))
    authority.prepare_fallback(fallback_archive())
    choice = authority.deadline_check(DAY)
    with pytest.raises(ValueError, match="not been reconciled"):
        store.served_for_scoring(authority, DAY)
    assert store.reconcile(authority, DAY) == choice
    store.reconcile(authority, DAY)
    assert count(store, "choices") == 2
    assert count(store, "delivery_incidents") == 1
    with store.engine.connect() as connection:
        assert connection.execute(text("SELECT choice_hash FROM forecast.deliveries")).scalar_one() == (
            mirror["choice_hash"])
    assert store.served_for_scoring(authority, DAY) == fallback_archive()
    assert authority.read_served(DAY) == fallback_archive()
    assert count(store, "state_runs") == 1


def test_reconcile_rejects_conflicting_run_data_and_rolls_back(store, tmp_path):
    archive = run_archive()
    store.start_run(archive["start"])
    store.complete_run(archive)
    archive["states"][1]["payload"]["weights"] = [1.0, 0.0, 0.0, 0.0]
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY))
    authority.publish_primary(archive)
    with pytest.raises(ValueError, match="Conflicting immutable states"):
        store.reconcile(authority, DAY)
    assert count(store, "choices") == 0


def test_two_primary_completions_choose_one_state_run_atomically(store):
    archives = [run_archive("first"), run_archive("second")]
    for archive in archives:
        store.start_run(archive["start"])
    barrier = threading.Barrier(2)

    def complete(archive):
        barrier.wait(timeout=10)
        store.complete_run(archive)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(complete, archives))
    assert count(store, "run_results") == 2
    assert count(store, "state_runs") == 1
    with store.engine.connect() as connection:
        selected = connection.execute(text("SELECT run_id FROM forecast.state_runs")).scalar_one()
    assert selected in {"first", "second"}
    assert store.replay_run(selected)["id"] == selected


def test_replay_run_does_not_feed_online_states(store):
    archive = run_archive(kind="replay")
    store.start_run(archive["start"])
    store.complete_run(archive)
    assert store.replay_run("run1") == archive
    assert count(store, "state_runs") == 0


@pytest.mark.parametrize("damage", ["choice_hash", "json", "values", "path", "day"])
def test_corrupt_authority_is_never_served(tmp_path, damage):
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY))
    choice = authority.publish_primary(run_archive())
    path = authority._choice_path(DAY)
    if damage == "json":
        path.write_text("{", encoding="utf-8")
    elif damage == "values":
        (tmp_path / choice["values_path"]).write_text("{}", encoding="utf-8")
    else:
        if damage == "path":
            choice["values_path"] = "../../outside.json"
        elif damage == "day":
            choice["delivery_day"] = "2024-04-04"
        choice.pop("choice_hash")
        choice["choice_hash"] = "bad" if damage == "choice_hash" else serving._hash(choice)
        path.write_text(json.dumps(choice), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        authority.read_served(DAY)
    authority.clock = lambda: delivery_deadline(DAY)
    with pytest.raises((ValueError, KeyError)):
        authority.deadline_check(DAY)


@pytest.mark.parametrize("damage", ["absent", "incomplete", "corrupt"])
def test_unusable_fallback_produces_final_none_choice(store, tmp_path, damage):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    if damage != "absent":
        reference = authority.prepare_fallback(fallback_archive())
        if damage == "incomplete":
            (tmp_path / reference["values_path"]).unlink()
        else:
            (tmp_path / "fallback" / f"{DAY}.json").write_text("{", encoding="utf-8")
    assert authority.deadline_check(DAY)["kind"] == "none"
    store.reconcile(authority, DAY)
    assert store.served_for_scoring(authority, DAY) is None


@pytest.mark.parametrize("damage", ["missing", "duplicate", "nan", "interval", "slot", "naive"])
def test_value_completeness_and_validity(damage):
    rows = values()
    if damage == "missing":
        rows.pop()
    elif damage == "duplicate":
        rows[-1] = rows[0]
    elif damage == "nan":
        rows[0]["point"] = float("nan")
    elif damage == "interval":
        rows[0]["lower"] = rows[0]["upper"] + 1
    elif damage == "slot":
        rows[0]["local_slot"] = -1
    else:
        rows[0]["value_time"] = "2024-04-02T22:00:00"
    with pytest.raises(ValueError):
        validate_values(DAY, rows)


def test_cleanup_preserves_recent_temporary_files_and_all_final_files(tmp_path):
    authority = ServingAuthority(tmp_path, lambda: datetime.now(UTC))
    folder = tmp_path / "choice"
    folder.mkdir()
    for name in ("recent.tmp", "old.tmp", "final.json"):
        (folder / name).write_text("{}", encoding="utf-8")
    old = datetime.now(UTC).timestamp() - 3601
    os.utime(folder / "old.tmp", (old, old))
    os.utime(folder / "final.json", (old, old))
    assert authority.cleanup_temporary() == 1
    assert (folder / "recent.tmp").exists() and (folder / "final.json").exists()


def test_unsorted_and_integer_predictions_keep_replayed_content_hash(store, tmp_path):
    archive = run_archive()
    archive["values"].reverse()
    archive["experts"].reverse()
    archive["states"].reverse()
    for row in archive["values"]:
        row["point"] = int(row["point"])
    for row in archive["experts"]:
        row["value"] = int(row["value"])
    store.start_run(archive["start"])
    store.complete_run(archive)
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY))
    authority.publish_primary(archive)
    store.reconcile(authority, DAY)
    assert serving._hash(store.served_for_scoring(authority, DAY)) == serving._hash(archive)


def test_no_primary_before_freeze_or_with_future_completion(tmp_path):
    archive = run_archive()
    authority = ServingAuthority(tmp_path, lambda: forecast_cutoff(DAY) - timedelta(seconds=1))
    with pytest.raises(ValueError, match="before freeze"):
        authority.publish_primary(archive)
    authority.clock = lambda: forecast_cutoff(DAY)
    archive["result"]["completed_at"] = (forecast_cutoff(DAY) + timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        authority.publish_primary(archive)
    assert not authority._choice_path(DAY).exists()


def test_result_cannot_redirect_output_to_another_run(store):
    archive = run_archive()
    store.start_run(archive["start"])
    archive["values"][0]["run_id"] = "another"
    with pytest.raises(ValueError, match="override run_id"):
        store.complete_run(archive)
    assert count(store, "run_results") == 0


def test_non_utc_timestamp_rejected_before_storage(store):
    archive = run_archive()
    archive["start"]["started_at"] = forecast_cutoff(DAY).astimezone(PARIS).isoformat()
    with pytest.raises(ValueError, match="UTC ISO"):
        store.start_run(archive["start"])
    assert count(store, "run_starts") == 0


@pytest.mark.parametrize("failed_folder", ["fallback", "values"])
def test_transient_fallback_read_error_retries(tmp_path, monkeypatch, failed_folder):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    authority.prepare_fallback(fallback_archive())
    original = Path.read_text
    attempts = []

    def transient(path, *args, **kwargs):
        if path.parent.name == failed_folder:
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient)
    assert authority.deadline_check(DAY)["kind"] == "fallback"
    assert len(attempts) >= 2


def test_persistent_fallback_read_error_leaves_choice_open(tmp_path, monkeypatch, caplog):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    authority.prepare_fallback(fallback_archive())
    original = Path.read_text
    attempts = []

    def denied(path, *args, **kwargs):
        if path.parent.name == "fallback":
            attempts.append(path)
            raise PermissionError("sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        authority.deadline_check(DAY)
    assert len(attempts) == 3
    assert not authority._choice_path(DAY).exists()
    assert "fallback read failed" in caplog.text


def test_none_choice_logs_reason(tmp_path, caplog):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    assert authority.deadline_check(DAY)["kind"] == "none"
    assert "fallback unavailable" in caplog.text


@pytest.mark.parametrize('delay,kind', [(59, 'fallback'), (60, 'none'), (5400, 'none')])
def test_deadline_window_never_serves_a_late_fallback(tmp_path, caplog, delay, kind):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY) + timedelta(seconds=delay))
    authority.prepare_fallback(fallback_archive())
    choice = authority.deadline_check(DAY)
    assert choice['kind'] == kind
    if kind == 'none':
        assert 'guarantee unmet' in choice['reason']
        assert 'guarantee unmet' in caplog.text
        assert authority.read_served(DAY) is None
    assert authority.deadline_check(DAY) == choice


def test_fallback_paused_before_publication_records_none(tmp_path, monkeypatch):
    now = delivery_deadline(DAY)
    authority = ServingAuthority(tmp_path, lambda: now)
    authority.prepare_fallback(fallback_archive())
    original = serving._publish

    def pause(target, document, *, before_publish):
        nonlocal now
        if target.parent.name == 'choice':
            now = delivery_deadline(DAY) + timedelta(minutes=1)
        return original(target, document, before_publish=before_publish)
    monkeypatch.setattr(serving, '_publish', pause)
    choice = authority.deadline_check(DAY)
    assert choice['kind'] == 'none'
    assert 'publication missed' in choice['reason']
    assert authority.read_served(DAY) is None


def test_fallback_read_retry_budget_exhaustion(tmp_path, monkeypatch):
    authority = ServingAuthority(tmp_path, lambda: delivery_deadline(DAY))
    authority.prepare_fallback(fallback_archive())
    ticks = iter([0.0, 0.25])
    monkeypatch.setattr(serving, "monotonic", lambda: next(ticks))
    original = Path.read_text
    attempts = []

    def delayed_error(path, *args, **kwargs):
        if path.parent.name == "fallback":
            attempts.append(path)
            raise PermissionError("sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", delayed_error)
    with pytest.raises(PermissionError):
        authority.deadline_check(DAY)
    assert len(attempts) == 1
    assert not authority._choice_path(DAY).exists()


@pytest.mark.parametrize("kind", ["primary", "replay"])
@pytest.mark.parametrize("damage", ["experts", "states", "weather", "expert_slot",
                                  "expert_set", "weather_status", "weather_identity"])
def test_incomplete_run_rejected_before_state_claim(store, kind, damage, caplog):
    archive = run_archive(kind=kind)
    store.start_run(archive["start"])
    if damage in ("experts", "states", "weather"):
        archive[damage] = []
    elif damage == "expert_slot":
        archive["experts"][0]["value_time"] = forecast_cutoff(DAY).isoformat()
    elif damage == "expert_set":
        archive["experts"].pop()
    elif damage == "weather_status":
        archive["result"]["weather_status"] = "not_collected"
    else:
        row = {**archive["weather"][0], "point": "Lyon", "selection_hash": "other"}
        archive["weather"].append(row)
    with pytest.raises(ValueError):
        store.complete_run(archive)
    assert count(store, "state_runs") == count(store, "run_results") == 0
    assert "archive rejected" in caplog.text


@pytest.mark.parametrize("field", ["max_ingested_at", "last_consumption_slot", "run_init_time",
                                 "file_last_modified", "index_last_modified"])
@pytest.mark.parametrize("kind", ["primary", "replay"])
def test_forecast_time_boundary_rejected_before_storage(store, field, kind, caplog):
    archive = run_archive(kind=kind)
    store.start_run(archive["start"])
    freeze = forecast_cutoff(DAY)
    row = archive["result"] if field in archive["result"] else archive["weather"][0]
    boundary = freeze - timedelta(hours=2, minutes=30) if field == "last_consumption_slot" else freeze
    row[field] = (boundary + timedelta(microseconds=1) if field == "last_consumption_slot"
                  else boundary).isoformat()
    with pytest.raises(ValueError):
        store.complete_run(archive)
    assert count(store, "state_runs") == count(store, "run_results") == 0
    assert "archive rejected" in caplog.text


@pytest.mark.parametrize("field", ["max_ingested_at", "last_consumption_slot", "run_init_time",
                                 "file_last_modified", "index_last_modified"])
def test_forecast_time_just_before_boundary_allowed(store, field):
    archive = run_archive()
    freeze = forecast_cutoff(DAY)
    row = archive["result"] if field in archive["result"] else archive["weather"][0]
    row[field] = ((freeze - timedelta(hours=2, minutes=30)) if field == "last_consumption_slot"
                  else freeze - timedelta(microseconds=1)).isoformat()
    store.start_run(archive["start"])
    store.complete_run(archive)
    assert store.replay_run(archive["id"]) == archive


@pytest.mark.parametrize("field", ["max_ingested_at", "last_consumption_slot"])
def test_missing_consumption_metadata_requires_explicit_flag(store, field):
    archive = run_archive()
    store.start_run(archive["start"])
    archive["result"][field] = None
    with pytest.raises(ValueError):
        store.complete_run(archive)
    archive["result"]["flags"]["consumption_missing"] = True
    store.complete_run(archive)
    assert count(store, "state_runs") == 1


def test_weather_missing_status_allows_empty_weather(store):
    archive = run_archive()
    archive["weather"] = []
    archive["result"]["weather_status"] = "weather missing"
    store.start_run(archive["start"])
    store.complete_run(archive)
    assert store.replay_run(archive["id"]) == archive


def test_record_score_inserts_new_vintage_rows_for_the_file_choice_only(store, tmp_path):
    authority = ServingAuthority(tmp_path / "serving", lambda: delivery_deadline(DAY))
    authority.prepare_fallback(fallback_archive())
    choice = authority.deadline_check(DAY)
    store.reconcile(authority, DAY)
    row = {"delivery_day": DAY.isoformat(), "choice_hash": choice["choice_hash"],
           "actual_vintage": "realtime", "actual_max_ingested_at": "2024-04-04T06:00:00+00:00",
           "scored_at": "2024-04-04T06:00:05+00:00",
           "metrics": {"vintage": "realtime", "served": {"mae": 812.5, "coverage": None}}}
    store.record_score(authority, row)
    store.record_score(authority, {**row, "actual_vintage": "consolidated",
                                   "scored_at": "2024-05-20T06:00:00+00:00"})
    with store.engine.connect() as connection:
        stored = connection.execute(text(
            "SELECT actual_vintage, metrics FROM forecast.scores ORDER BY scored_at"
        )).all()
    assert [vintage for vintage, _ in stored] == ["realtime", "consolidated"]
    assert json.loads(stored[0][1]) == row["metrics"]
    with pytest.raises(ValueError, match="choice file"):
        store.record_score(authority, {**row, "choice_hash": "other"})
    with pytest.raises(IntegrityError):
        store.record_score(authority, row)
    with pytest.raises(ValueError):
        store.record_score(authority, {**row, "metrics": {"mae": float("nan")}})
    assert count(store, "scores") == 2

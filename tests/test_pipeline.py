"""Offline operational checks use real serving files and private SQL tables."""

import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from load_forecast import flows, pipeline, production_tracking
from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.deadline import deadline_check
from load_forecast.pipeline import FrozenInputs, Runtime, fallback_values
from load_forecast.pipeline_artifacts import read_artifact, write_artifact
from load_forecast.pipeline_dry_run import dry_run, local_store
from load_forecast.serving import delivery_deadline

DAY = date(2026, 10, 9)
ROOT = Path(__file__).resolve().parents[1]


def test_unconfigured_runtime_is_refused_before_any_write():
    with pytest.raises(ValueError, match='absolute configured path'):
        Runtime(Path(''), Mock())
    with pytest.raises(ValueError, match='absolute configured path'):
        deadline_check(Path(''), DAY)


def fallback(day=DAY):
    index = pd.DatetimeIndex(delivery_slots(day, '30min')) - pd.Timedelta(days=7)
    return {'kind': 'fallback', 'id': 'prepared', 'delivery_day': str(day),
            'prepared_at': (forecast_cutoff(day) - timedelta(hours=23)).isoformat(),
            'models': {}, 'completeness': {'complete': True},
            'values': fallback_values(day, pd.Series(45000., index=index), None, (10000., 100000.))}


def test_artifact_retry_conflict_and_integrity(tmp_path):
    path = tmp_path / 'state.pkl'
    digest = write_artifact(path, {'state': [1, 2]})
    assert write_artifact(path, {'state': [1, 2]}) == digest
    assert read_artifact(path, digest) == {'state': [1, 2]}
    with pytest.raises(ValueError, match='Conflicting'):
        write_artifact(path, {'state': [3]})
    with pytest.raises(ValueError, match='hash mismatch'):
        read_artifact(path, 'wrong')


def test_mapping_changes_only_real_time_rows():
    index = pd.date_range('2026-10-01', periods=3, freq='30min', tz='UTC')
    inputs = FrozenInputs(DAY, pd.Series([10., 20., 30.], index=index), pd.DataFrame(),
                          pd.Series([True, False, True], index=index),
                          pd.Series(5., index=range(24)), None, {}, [], 'hash')
    assert inputs.mapped('A').tolist() == [10, 20, 30]
    assert inputs.mapped('B').tolist() == [15, 20, 35]
    with pytest.raises(ValueError):
        inputs.mapped('C')


@pytest.mark.parametrize('day,count', [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
def test_fallback_dst_complete_and_no_interval(day, count):
    archive = fallback(day)
    assert len(archive['values']) == count
    assert all(row['point'] == 45000 and row['lower'] is None for row in archive['values'])


def test_fallback_uses_four_local_weekdays_then_refuses_missing():
    indexes = [pd.DatetimeIndex(delivery_slots(DAY - timedelta(days=7*n), '30min'))
               for n in (2, 3, 4)]
    history = pd.Series([40000.] * len(indexes[0]) + [50000.] * len(indexes[1])
                        + [60000.] * len(indexes[2]), index=indexes[0].append(indexes[1:]))
    assert all(v['point'] == 50000 for v in fallback_values(DAY, history, None, (1, 100000)))
    with pytest.raises(ValueError, match='incomplete'):
        fallback_values(DAY, history.iloc[:0], None, (1, 100000))


def test_fallback_calendar_precedes_naive(monkeypatch):
    archive = fallback()
    index = pd.DatetimeIndex([v['value_time'] for v in archive['values']])
    monkeypatch.setattr(pipeline, 'forecast_day', lambda *a: pd.DataFrame({'model': 47000.}, index=index))
    models = SimpleNamespace(slots={'calendar': object()}, refit=DAY)
    history = pd.Series(45000., index=index - pd.Timedelta(days=7))
    assert all(v['point'] == 47000 for v in fallback_values(DAY, history, models, (1, 100000)))


def test_failed_fallback_alert_and_none_choice(tmp_path, caplog):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: delivery_deadline(DAY))
    runtime.prepare_fallback = Mock(side_effect=ValueError('missing inputs'))
    try:
        with flows.use_runtime(runtime), pytest.raises(ValueError):
            flows.prepare_fallback.fn(DAY)
        assert 'fallback_preparation_failed' in caplog.text
        assert deadline_check(tmp_path, DAY, clock=runtime.clock)['kind'] == 'none'
        assert len(list((tmp_path / 'incidents').glob('*.pkl'))) == 2
    finally:
        store.engine.dispose()


def test_fallback_retry_obeys_existing_complete_file_without_database(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store)
    runtime.authority.prepare_fallback(fallback())
    connect = Mock(side_effect=AssertionError('retry opened database'))
    monkeypatch.setattr(pipeline.eco2mix, 'connect_database', connect)
    try:
        assert runtime.prepare_fallback(DAY)['status'] == 'already_prepared'
        connect.assert_not_called()
    finally:
        store.engine.dispose()


def test_deadline_in_fresh_process_without_prefect_or_database(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store)
    runtime.authority.prepare_fallback(fallback())
    store.engine.dispose()
    script = '''import sys
from datetime import date
class RefuseServices:
    def find_spec(self, fullname, *args):
        if fullname == 'prefect' or fullname.startswith('prefect.'):
            raise AssertionError('deadline imported Prefect')
sys.meta_path.insert(0, RefuseServices())
import sqlalchemy
sqlalchemy.create_engine = lambda *a, **k: (_ for _ in ()).throw(AssertionError('database opened'))
from load_forecast import deadline
from load_forecast.serving import delivery_deadline
original = deadline.deadline_check
deadline.deadline_check = lambda root, day: original(root, day, clock=lambda: delivery_deadline(day))
from load_forecast.deadline import main
main()
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), LFF_RUNTIME_DIR=str(tmp_path),
               PREFECT_API_URL='http://127.0.0.1:1/api', POSTGRES_HOST='unreachable.invalid')
    result = subprocess.run([sys.executable, '-c', script, '--day', str(DAY)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"kind": "fallback"' in result.stdout


def test_killed_daily_process_leaves_start_only_and_deadline_serves(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store)
    runtime.authority.prepare_fallback(fallback())
    script = '''import os, time
from pathlib import Path
from datetime import date
from load_forecast.pipeline import Runtime
from load_forecast.pipeline_dry_run import local_store
from load_forecast.cutoff import forecast_cutoff
from load_forecast.flows import use_runtime, daily_forecast
root=Path(os.environ['LFF_RUNTIME_DIR']); day=date(2026,10,9)
runtime=Runtime(root,local_store(root),clock=lambda:forecast_cutoff(day))
def hanging(start):
    (root/('started-'+start['variant'])).write_text(start['run_id'])
    time.sleep(300)
runtime.start_tracking=hanging
with use_runtime(runtime):
    daily_forecast.fn(day)
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), LFF_RUNTIME_DIR=str(tmp_path), LFF_CODE_COMMIT='test')
    process = subprocess.Popen([sys.executable, '-c', script], env=env,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    import time
    try:
        end = time.monotonic() + 30
        while len(list(tmp_path.glob('started-*'))) < 2 and time.monotonic() < end:
            if process.poll() is not None:
                pytest.fail('Forecast child exited before start event')
            time.sleep(.05)
        assert len(list(tmp_path.glob('started-*'))) == 2
    finally:
        process.kill()
        process.wait(timeout=10)
    try:
        with store.engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM forecast.run_starts')).scalar() == 2
            assert connection.execute(text('SELECT count(*) FROM forecast.run_results')).scalar() == 0
        assert deadline_check(tmp_path, DAY, clock=lambda: delivery_deadline(DAY))['kind'] == 'fallback'
    finally:
        store.engine.dispose()


def test_flow_bodies_routes_and_independent_variants():
    runtime = Mock()
    runtime.authority.read_choice.return_value = {'kind': 'primary'}
    with flows.use_runtime(runtime):
        flows.collect_consumption.fn(DAY)
        flows.stage_weather.fn(DAY)
        flows.daily_forecast.fn(DAY)
        flows.reconcile_deliveries.fn()
        flows.score_and_monitor.fn(DAY)
        flows.consolidated_refresh.fn(DAY)
    runtime.collect.assert_called_once_with(DAY)
    runtime.stage.assert_called_once_with(DAY)
    assert {call.args for call in runtime.forecast.call_args_list} == {(DAY, 'A'), (DAY, 'B')}
    runtime.refresh.assert_called_once_with(DAY)
    assert runtime.score.call_count == 2
    assert runtime.publish_site.call_count == 2


def test_late_weekly_artifact_is_adopted_without_replacing_issued_history(tmp_path):
    store = local_store(tmp_path)
    tuesday = date(2026, 10, 6)
    monday = tuesday - timedelta(days=1)
    runtime = Runtime(tmp_path, store, clock=lambda: forecast_cutoff(tuesday))
    state = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday - timedelta(days=7)),
                            kalman='old filter', issued=['stale Monday'], mlpoly='issued losses')
    prepared = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday, identity='late'),
                               kalman='reset filter',
                               completed_at=forecast_cutoff(monday) + timedelta(hours=1))
    try:
        write_artifact(runtime.refit_path(monday, 'B'), prepared)
        assert runtime.choose_models(tuesday, 'B', state) is False
        assert state.models.identity == 'late' and state.kalman == 'reset filter'
        assert state.issued == ['stale Monday'] and state.mlpoly == 'issued losses'
    finally:
        store.engine.dispose()


def test_refit_timeout_uses_old_models_and_refuses_attempt_at_1220(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    monday = date(2026, 10, 5)
    freeze = forecast_cutoff(monday)
    runtime = Runtime(tmp_path, store, clock=lambda: freeze + timedelta(minutes=20))
    runtime.start_refit = Mock()
    state = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday - timedelta(days=7)), kalman='old')
    try:
        assert runtime.choose_models(monday, 'B', state) is True
        assert state.kalman == 'old'
        with pytest.raises(TimeoutError):
            runtime.forecast(monday, 'B')
        with store.engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM forecast.run_starts')).scalar() == 0
    finally:
        store.engine.dispose()


def test_no_refit_before_next_freeze_fits_again_from_sunday_inputs(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    tuesday = date(2026, 10, 6)
    monday = tuesday - timedelta(days=1)
    runtime = Runtime(tmp_path, store, clock=lambda: forecast_cutoff(tuesday) + timedelta(minutes=1))
    state = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday - timedelta(days=7)), kalman='old')
    late = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday, identity='too late'),
                           kalman='late', completed_at=forecast_cutoff(tuesday))
    fresh = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday, identity='fresh'),
                            kalman='fresh', completed_at=runtime.clock())
    inputs = SimpleNamespace(mapped=Mock(return_value='Sunday actuals'),
                             temperature='Sunday weather', snapshot_hash='Sunday snapshot')
    runtime.inputs = Mock(return_value=inputs)
    runtime.load_state = Mock(return_value='Sunday state')
    prepare = Mock(return_value=fresh)
    monkeypatch.setattr(pipeline.production, 'prepare_weekly_refit', prepare)
    try:
        write_artifact(runtime.refit_path(monday, 'B'), late)
        assert runtime.choose_models(tuesday, 'B', state) is False
        runtime.inputs.assert_called_once_with(monday)
        runtime.load_state.assert_called_once_with(monday, 'B')
        assert prepare.call_args.args[:5] == (
            'Sunday state', 'Sunday actuals', 'Sunday weather', monday, 'Sunday snapshot')
        assert state.models.identity == 'fresh'
        assert read_artifact(runtime.refit_path(monday, 'B')).models.identity == 'too late'
    finally:
        store.engine.dispose()


def test_seven_deployments_have_paris_schedules_without_registration():
    items = flows.deployments()
    assert len(items) == 7
    assert all(schedule.schedule.timezone == 'Europe/Paris'
               for item in items for schedule in item.schedules)


def test_production_tracking_protocol(monkeypatch):
    monkeypatch.setenv('LFF_PRODUCTION_EXPERIMENT_ID', '42')
    response = Mock()
    response.json.return_value = {'run': {'info': {'run_id': 'tracked',
                                     'artifact_uri': 'mlflow-artifacts:/42/run/artifacts'}}}
    for name in ('get', 'post', 'put'):
        monkeypatch.setattr(production_tracking.requests, name, Mock(return_value=response))
    assert production_tracking.start_tracking({'delivery_day': str(DAY), 'variant': 'B',
        'run_id': 'local', 'code_commit': 'commit'}) == 'tracked'
    production_tracking.finish_tracking('tracked', models={'identity': 'refit'}, kalman={})
    assert production_tracking.requests.put.call_count == 1
    assert production_tracking.requests.post.call_args.kwargs['json']['status'] == 'FINISHED'


def test_all_flows_offline_real_forecasts_and_sql():
    result = dry_run(DAY)
    assert result['state_variants'] == ['A', 'B']
    assert result['deadline_check'] == 'primary'
    assert result['score_and_monitor']['status'] == 'scored'
    assert result['score_and_monitor']['scores_inserted'] == 2
    assert result['score_and_monitor']['monitoring_inserted'] == 6
    assert result['consolidated_refresh']['scoring']['scores_inserted'] == 0


@pytest.mark.parametrize('day', [date(2026, 1, 12), date(2026, 7, 12),
                               date(2026, 3, 30), date(2026, 10, 26)])
def test_runtime_inputs_exclude_late_ingestions_and_slots_before_forecast(tmp_path, monkeypatch, day):
    freeze = forecast_cutoff(day)
    boundary = freeze - timedelta(hours=2, minutes=30)
    stamps = [boundary - timedelta(minutes=90), boundary - timedelta(minutes=60),
              boundary - timedelta(minutes=30), boundary, boundary + timedelta(minutes=30)]
    ingestion = [freeze - timedelta(microseconds=1), freeze,
                 freeze + timedelta(microseconds=1), freeze - timedelta(seconds=1),
                 freeze - timedelta(seconds=1)]
    rows = [dict(value_time=t, ingested_at=i, consommation=v,
                 source_dataset='eco2mix-national-tr')
            for t, i, v in zip(stamps, ingestion, [40000., 90000., 91000., 45000., 92000.], strict=True)]
    connection, cursor = Mock(), Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    connection.cursor.return_value = cursor
    cursor.fetchall.side_effect = [rows, []]
    monkeypatch.setattr(pipeline.eco2mix, 'connect_database', lambda: connection)
    monkeypatch.setenv('LFF_SNAPSHOT_SCHEMA', 'dev')
    monkeypatch.setenv('LFF_CODE_COMMIT', 'fixture')
    monkeypatch.setenv('LFF_CLEAN_TABLE', 'dev.previous')

    def snapshot(selected_day, **kwargs):
        assert selected_day == day and kwargs['now'] == freeze
        os.environ['LFF_CLEAN_TABLE'] = 'dev.frozen_fixture'
    monkeypatch.setattr(pipeline, 'build_clean_snapshot', snapshot)
    monkeypatch.setattr(pipeline.production, 'FROZEN', SimpleNamespace(training_start=day))
    monkeypatch.setattr(pipeline.weather_production, 'select_staged',
                        lambda *a, **k: {'status': 'weather_missing'})
    monkeypatch.setattr(pipeline.weather_production, 'replay_points', lambda *a: pd.DataFrame())
    monkeypatch.setattr(pipeline.weather_production, 'temperature_day',
                        lambda *a: pd.DataFrame({'temperature': [10.]}))
    monkeypatch.setattr(pipeline, 'estimate_revision_mapping',
                        lambda *a: SimpleNamespace(offsets=pd.Series(0., index=range(24))))
    runtime = Runtime(tmp_path, Mock(), clock=lambda: freeze)
    runtime.start_tracking = Mock(return_value=None)
    runtime.load_state = Mock(return_value=SimpleNamespace())
    runtime.choose_models = Mock(return_value=False)
    seen = []

    def forecast(selected_day, history, temperature, state, **kwargs):
        seen.append(history)
        raise ValueError('stop after inspecting forecast inputs')
    monkeypatch.setattr(pipeline.production, 'forecast_production_day', forecast)
    with pytest.raises(ValueError, match='inspecting forecast inputs'):
        runtime.forecast(day, 'A')
    assert seen[0].tolist() == [40000., 45000.]
    assert seen[0].index.tolist() == [stamps[0], boundary]
    assert '"dev"."frozen_fixture"' in cursor.execute.call_args_list[0].args[0].as_string()
    frozen = runtime.inputs(day)
    assert frozen.max_ingested_at == ingestion[0].isoformat()
    assert frozen.history.equals(seen[0])


@pytest.mark.parametrize('minute,stale', [(19, False), (20, True), (25, True)])
def test_monday_refit_adoption_stops_at_1220(tmp_path, minute, stale, monkeypatch):
    monday = date(2026, 10, 5)
    runtime = Runtime(tmp_path, Mock(), clock=lambda: forecast_cutoff(monday) + timedelta(minutes=26))
    state = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday - timedelta(days=7)),
                            kalman='old', issued=['history'], mlpoly='weights')
    prepared = SimpleNamespace(variant='B', models=SimpleNamespace(refit=monday), kalman='new',
                               completed_at=forecast_cutoff(monday) + timedelta(minutes=minute))
    write_artifact(runtime.refit_path(monday, 'B'), prepared)
    adopt = Mock(wraps=pipeline.production.adopt_refit)
    monkeypatch.setattr(pipeline.production, 'adopt_refit', adopt)
    assert runtime.choose_models(monday, 'B', state) is stale
    assert adopt.call_count == (0 if stale else 1)
    assert state.kalman == ('old' if stale else 'new')
    assert state.issued == ['history'] and state.mlpoly == 'weights'


def test_deadline_cli_reports_actual_cause(monkeypatch, capsys):
    from load_forecast import deadline
    monkeypatch.setattr(sys, 'argv', ['deadline', '--day', str(DAY)])
    monkeypatch.delenv('LFF_RUNTIME_DIR', raising=False)
    with pytest.raises(SystemExit) as error:
        deadline.main()
    assert error.value.code == 1
    assert 'LFF_RUNTIME_DIR' in capsys.readouterr().err


@pytest.mark.parametrize('mode', ['late', 'race', 'lost'])
def test_unserved_primary_is_logged(tmp_path, monkeypatch, caplog, mode):
    freeze = forecast_cutoff(DAY)
    runtime = Runtime(tmp_path, Mock(), clock=lambda: freeze)
    monkeypatch.setenv('LFF_CODE_COMMIT', 'fixture')
    runtime.start_tracking = Mock(return_value=None)
    runtime.inputs = Mock(return_value=SimpleNamespace(mapped=lambda v: pd.Series(), temperature=None))
    runtime.load_state = Mock(return_value=SimpleNamespace())
    runtime.choose_models = Mock(return_value=False)
    monkeypatch.setattr(pipeline.production, 'forecast_production_day', Mock(return_value=pd.DataFrame()))
    artifact = tmp_path / 'state.pkl'
    artifact.write_bytes(b'state')
    monkeypatch.setattr(pipeline.production, 'StateStore', lambda p: SimpleNamespace(save=lambda s: artifact))
    monkeypatch.setattr(pipeline, 'archive_run', lambda *a, **k: {'result': {}})
    if mode == 'late':
        runtime.store.complete_run.side_effect = lambda a: setattr(runtime, 'clock', lambda: delivery_deadline(DAY))
        runtime.authority.publish_primary = Mock(side_effect=TimeoutError('Primary publication missed the deadline'))
    elif mode == 'race':
        runtime.authority.publish_primary = Mock(side_effect=TimeoutError('Primary publication missed the deadline'))
    else:
        runtime.authority.publish_primary = Mock(return_value={'kind': 'fallback', 'id': 'prepared'})
    runtime.forecast(DAY, 'B')
    assert str(DAY) in caplog.text and 'variant B' in caplog.text
    assert ('choice already claimed' if mode == 'lost' else 'missed the deadline') in caplog.text


@pytest.mark.parametrize('day', [date(2026, 10, 21), date(2026, 10, 22)])
@pytest.mark.parametrize('alert_fails', [False, True])
@pytest.mark.parametrize('quality_fails', [False, True])
def test_incident_archive_flag_and_alert_preserve_primary(
        tmp_path, monkeypatch, caplog, day, alert_fails, quality_fails):
    from tests.test_serving import run_archive

    template = run_archive(day=day)
    index = pd.DatetimeIndex(delivery_slots(day, '30min'))
    output = pd.DataFrame({'model': 45000., 'lower': 44000., 'upper': 46000., 'n_pool': 60},
                          index=index)
    for name in pipeline.production.EXPERTS:
        output[name], output[f'{name}_backup'] = 45000., True
    mlpoly = SimpleNamespace(weights=lambda: np.ones(4) / 4,
                              regret=np.zeros(4), squares=np.zeros(4))
    kalman = SimpleNamespace(fitted_quarter='2026Q4', params=np.zeros(4), filter=SimpleNamespace(
        delta=np.zeros(48), covariance=np.ones(48), last_day=day - timedelta(days=2)))
    state = SimpleNamespace(variant='B', mlpoly=mlpoly, kalman=kalman, absorbed_through=None,
                             models=SimpleNamespace(refit=date(2026, 10, 19), identity='frozen'))
    history = pd.Series([45000.], index=pd.to_datetime(['2026-10-19T11:30Z'], utc=True))
    inputs = FrozenInputs(day, history, pd.DataFrame(), pd.Series(True, index=history.index),
        pd.Series(0., index=range(24)), (forecast_cutoff(day) - timedelta(seconds=1)).isoformat(),
        {'status': 'weather missing'}, [], 'frozen')
    monkeypatch.setattr(pipeline.production, 'feature_builders', lambda _: {'fixture': object()})
    monkeypatch.setattr(pipeline, 'day_features', lambda *a: pd.DataFrame({'lag': 45000.}, index=index))
    if quality_fails:
        monkeypatch.setattr(pipeline, 'degraded_input_reasons',
                            Mock(side_effect=ValueError('fixture quality error')))
    archive = pipeline.archive_run(template['start'], output, state, inputs,
        forecast_cutoff(day), tmp_path, 'hash', stale=False)
    reasons = ['unknown'] if quality_fails else ['expert_backup_chain', 'stale_consumption']
    assert archive['result']['flags']['degraded_inputs'] == reasons
    if quality_fails:
        assert 'fixture quality error' in caplog.text
    assert archive['result']['last_consumption_slot'] == '2026-10-19T11:30:00+00:00'
    assert archive['kind'] == 'primary'
    assert all(v['point'] == 45000. for v in archive['values'])
    fixture_store = local_store(tmp_path / 'archive')
    try:
        fixture_store.start_run(archive['start'])
        fixture_store.complete_run(archive)
        assert fixture_store.replay_run(archive['id'])['result']['flags'] == archive['result']['flags']
    finally:
        fixture_store.engine.dispose()

    runtime = Runtime(tmp_path, Mock(), clock=lambda: forecast_cutoff(day))
    monkeypatch.setenv('LFF_CODE_COMMIT', 'fixture')
    runtime.start_tracking = Mock(return_value=None)
    runtime.inputs = Mock(return_value=inputs)
    runtime.load_state = Mock(return_value=state)
    runtime.choose_models = Mock(return_value=False)
    monkeypatch.setattr(pipeline.production, 'forecast_production_day', lambda *a, **k: output)
    artifact = tmp_path / 'state.pkl'
    artifact.write_bytes(b'frozen')
    monkeypatch.setattr(pipeline.production, 'StateStore', lambda p: SimpleNamespace(save=lambda s: artifact))
    calls = []
    runtime.authority.publish_primary = lambda a: calls.append('publish') or {
        'kind': 'primary', 'id': a['id']}

    def alert(event, target):
        calls.append('alert')
        assert target == day and event == 'degraded_inputs:' + ','.join(reasons)
        if alert_fails:
            raise OSError('Incident storage unavailable')
    runtime.alert = alert
    runtime.forecast(day, 'B')
    assert calls == ['publish', 'alert']
    saved = runtime.store.complete_run.call_args.args[0]
    assert saved['values'] == archive['values']
    runtime.store.fail_run.assert_not_called()
    assert saved['result']['flags']['degraded_inputs'] == reasons
    if quality_fails:
        assert 'forecast_failed' not in caplog.text


@pytest.mark.parametrize('month,hour', [('01', '11'), ('07', '10')])
def test_documented_deadline_boundary_tracks_paris_clock(month, hour):
    docs = (ROOT / 'docs/operations.md').read_text(encoding='utf-8')
    lines = '\n'.join(line for line in docs.splitlines() if line.startswith('$DeadlineTrigger'))
    lines = lines.replace('(Get-Date)', f"(Get-Date '2026-{month}-10')")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                             lines + '\n$DeadlineTrigger.StartBoundary'],
                            capture_output=True, text=True, check=True, timeout=30)
    boundary = result.stdout.strip()
    assert re.fullmatch(r'2026-\d{2}-10T12:30:00', boundary)
    assert datetime.fromisoformat(boundary).replace(tzinfo=PARIS).astimezone(
        UTC).strftime('%H:%M') == f'{hour}:30'
    assert '-StartWhenAvailable' not in docs

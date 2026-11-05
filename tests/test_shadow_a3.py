"""Morning cutoff, independent scales and exact serving regression for the unserved shadow."""

import copy
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from tests.test_production_scoring import actual_frame
from tests.test_serving import fallback_archive, run_archive

from load_forecast import pipeline
from load_forecast.aggregation import known_morning, morning_level_shift
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.pipeline import FrozenInputs, Runtime
from load_forecast.pipeline_artifacts import read_artifact
from load_forecast.pipeline_dry_run import local_store
from load_forecast.serving import delivery_deadline
from load_forecast.shadow_a3 import build_shadow

DAY = date(2026, 11, 6)


def frozen(day=DAY):
    grid = pd.DatetimeIndex(delivery_slots(day - timedelta(days=1), '30min'))
    return FrozenInputs(day, pd.Series(46000., index=grid), pd.DataFrame(),
                        pd.Series(True, index=grid), pd.Series(1000., index=range(24)),
                        None, {}, [], 'snapshot')


def archives(day=DAY, variant='B'):
    return (run_archive('current', day, variant=variant),
            run_archive('previous', day - timedelta(days=1), variant=variant))


@pytest.mark.parametrize('variant,error,shift,gap', [('A', -1000., 300., 0.),
                                                   ('B', -2000., 600., 1000.)])
@pytest.mark.parametrize('day,morning_count,delivery_count', [
    (DAY, 20, 48), (date(2026, 3, 30), 18, 48), (date(2026, 10, 26), 22, 48),
    (date(2026, 3, 29), 20, 46), (date(2026, 10, 25), 20, 50)])
def test_scales_cutoff_dst_and_validated_formula(day, morning_count, delivery_count,
                                               variant, error, shift, gap):
    inputs = frozen(day)
    archive, previous = archives(day, variant)
    original = copy.deepcopy((archive, previous))
    history = inputs.history.copy()
    morning = history.index[known_morning(history.index, day)]
    # A poison value at 10:00 and later cannot enter the morning mean.
    inputs.history.loc[~known_morning(history.index, day)] = 1e9
    result = build_shadow(archive, previous, inputs.history, inputs.mapped(variant),
                          inputs.realtime, inputs.snapshot_hash)
    assert result['reason'] is None
    assert len(result['morning']) == morning_count
    assert pd.Timestamp(result['morning'][-1]['value_time']).tz_convert('Europe/Paris').strftime(
        '%H:%M') == '09:30'
    assert result['morning_error_mw'] == error
    assert result['shift_mw'] == shift
    assert result['mapped_minus_realtime_mw'] == gap
    assert len(result['values']) == delivery_count
    assert all(r['point'] == 45000. + shift and r['lower'] is None for r in result['values'])
    assert (archive, previous) == original
    pd.testing.assert_series_equal(inputs.history.loc[morning], history.loc[morning])
    index = history.index.append(pd.DatetimeIndex(delivery_slots(day, '30min')))
    base = pd.Series(45000., index=index)
    expected = morning_level_shift(base, inputs.mapped(variant), .3)
    assert [r['point'] for r in result['values']] == expected.loc[index[len(history):]].tolist()


@pytest.mark.parametrize('missing', ['actual', 'nan', 'inf', 'source', 'forecast', 'previous',
                                     'late', 'variant'])
@pytest.mark.parametrize('day', [DAY, date(2026, 3, 30), date(2026, 10, 26)])
def test_missing_morning_fails_closed_with_reason(day, missing):
    inputs = frozen(day)
    archive, previous = archives(day)
    stamp = inputs.history.index[0]
    if missing == 'actual':
        inputs.history = inputs.history.drop(stamp)
    elif missing in ('nan', 'inf'):
        inputs.history.loc[stamp] = np.nan if missing == 'nan' else np.inf
    elif missing == 'source':
        inputs.realtime.loc[stamp] = False
    elif missing == 'forecast':
        previous['values'] = previous['values'][1:]
    elif missing == 'previous':
        previous = None
    elif missing == 'late':
        previous['result']['completed_at'] = forecast_cutoff(day).isoformat()
    else:
        previous['variant'] = 'A'
    result = build_shadow(archive, previous, inputs.history, inputs.mapped('B'),
                          inputs.realtime, inputs.snapshot_hash)
    assert result['reason'] is not None
    assert result['morning_error_mw'] is None
    assert result['shift_mw'] == 0.
    assert [r['point'] for r in result['values']] == [r['point'] for r in archive['values']]


def test_mapping_diagnostic_uses_frozen_hourly_offsets_and_degraded_flag():
    inputs = frozen()
    inputs.offsets = pd.Series(np.arange(24) * 100., index=range(24))
    archive, previous = archives()
    archive['result']['flags']['degraded_inputs'] = ['stale_consumption']
    before = copy.deepcopy(inputs)
    result = build_shadow(archive, previous, inputs.history, inputs.mapped('B'),
                          inputs.realtime, inputs.snapshot_hash)
    assert result['mapped_minus_realtime_mw'] == 450.
    assert result['morning_error_mw'] == -1450.
    assert result['shift_mw'] == 435.
    assert not result['clean_delivery']
    assert result['snapshot_hash'] == 'snapshot'
    for row in result['morning']:
        hour = pd.Timestamp(row['value_time']).tz_convert('Europe/Paris').hour
        assert row['mapped_mw'] - row['realtime_mw'] == hour * 100.
    pd.testing.assert_series_equal(inputs.history, before.history)
    pd.testing.assert_series_equal(inputs.realtime, before.realtime)
    pd.testing.assert_series_equal(inputs.offsets, before.offsets)


@pytest.mark.parametrize('variant', ['A', 'B'])
@pytest.mark.parametrize('choice', ['primary', 'fallback', 'none'])
@pytest.mark.parametrize('failure', [None, 'storage', 'alert'])
def test_forecast_serving_bytes_and_sql_unchanged(tmp_path, monkeypatch, variant, choice, failure):
    monkeypatch.setenv('LFF_CODE_COMMIT', 'abc123')
    monkeypatch.setattr(pipeline, 'uuid4', lambda: SimpleNamespace(hex='current'))
    archive, previous = archives(variant=variant)
    monkeypatch.setattr(pipeline, 'archive_run', lambda *a, **k: copy.deepcopy(archive))
    monkeypatch.setattr(pipeline.production, 'forecast_production_day',
                        lambda *a, **k: pd.DataFrame())
    state_path = tmp_path / 'state.pkl'
    state_path.write_bytes(b'unchanged state')
    monkeypatch.setattr(pipeline.production, 'StateStore',
                        lambda p: SimpleNamespace(save=lambda s: state_path))
    snapshots = []
    for enabled in (False, True):
        root = tmp_path / str(enabled)
        root.mkdir()
        store = local_store(root)
        runtime = Runtime(root, store, clock=lambda: forecast_cutoff(DAY))
        runtime.inputs = lambda d: frozen(d)
        runtime.start_tracking = lambda s: None
        runtime.load_state = lambda *a: SimpleNamespace()
        runtime.choose_models = lambda *a: False
        store.start_run(previous['start'])
        store.complete_run(previous)
        if variant == 'A' and choice == 'primary':
            runtime.authority.publish_primary(run_archive('served_b', DAY))
        if choice != 'primary':
            if choice == 'fallback':
                runtime.authority.prepare_fallback(fallback_archive(DAY))
            runtime.authority.clock = lambda: delivery_deadline(DAY)
            runtime.authority.deadline_check(DAY)
        if not enabled:
            runtime.store_shadow = lambda *a: None
        elif failure:
            def fail(*a):
                raise OSError('diagnostic failure')
            runtime.store_shadow = fail
            if failure == 'alert':
                runtime.alert = fail
        try:
            result = runtime.forecast(DAY, variant)
            files = {p.relative_to(runtime.authority.root).as_posix(): p.read_bytes()
                     for p in runtime.authority.root.rglob('*.json')}
            with store.engine.connect() as connection:
                rows = [list(connection.execute(text(f'SELECT * FROM forecast."{table}" ORDER BY 1')))
                        for table in ('run_starts', 'run_results', 'values', 'state_runs', 'states')]
            snapshots.append((result, files, rows))
            if enabled and not failure:
                shadow = read_artifact(root / 'runs/current/shadow_a3.pkl')
                assert shadow['shift_mw'] == (300. if variant == 'A' else 600.)
        finally:
            store.engine.dispose()
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize('day', [DAY, date(2026, 3, 29), date(2026, 10, 25)])
def test_daily_storage_and_scoring_complete_only_idempotent(tmp_path, day):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: forecast_cutoff(day))
    for variant in ('A', 'B'):
        archive, previous = archives(day, variant)
        archive['id'] = archive['start']['run_id'] = archive['result']['run_id'] = variant
        previous['id'] = previous['start']['run_id'] = previous['result']['run_id'] = 'prev_' + variant
        for item in (previous, archive):
            store.start_run(item['start'])
            store.complete_run(item)
        runtime.store_shadow(archive, frozen(day))
        runtime.store_shadow(archive, frozen(day))
        if variant == 'B':
            runtime.authority.publish_primary(archive)
    runtime.reconcile()
    runtime.clock = lambda: datetime(2026, 11, 10, tzinfo=UTC)
    actual = actual_frame(day, actual=46000.)
    frames = {'realtime': actual.iloc[1:]}
    runtime.scoring_actuals = lambda *a: frames
    try:
        assert runtime.score(day)['scores_inserted'] == 0
        frames['realtime'] = actual
        assert runtime.score(day)['scores_inserted'] == 1
        assert runtime.score(day)['scores_inserted'] == 0
        frames['consolidated'] = actual_frame(day, actual=47000.)
        assert runtime.score(day)['scores_inserted'] == 1
        with store.engine.connect() as connection:
            metrics = [json.loads(r[0]) for r in connection.execute(
                text('SELECT metrics FROM forecast.scores'))]
        for m in metrics:
            for variant, shift in (('a', 300.), ('b', 600.)):
                a3 = m['a3_' + variant]
                assert a3['shadow']['n_slots'] == len(actual)
                assert a3['baseline'] == a3['served'] == m['served']
                assert a3['shadow']['mae'] == m['served']['mae'] - shift
                assert a3['clean_delivery']
                assert a3['shadow']['coverage'] is None
    finally:
        store.engine.dispose()


@pytest.mark.parametrize('damage', ['identity', 'incomplete'])
def test_bad_shadow_never_blocks_served_scoring(tmp_path, caplog, damage):
    from load_forecast.pipeline_artifacts import write_artifact
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: forecast_cutoff(DAY))
    archive, previous = archives()
    store.start_run(archive['start'])
    store.complete_run(archive)
    runtime.authority.publish_primary(archive)
    runtime.reconcile()
    shadow = build_shadow(archive, previous, frozen().history, frozen().mapped('B'),
                          frozen().realtime, 'snapshot')
    if damage == 'identity':
        shadow['run_id'] = 'different'
    else:
        shadow['values'] = shadow['values'][1:]
    write_artifact(tmp_path / 'runs/current/shadow_a3.pkl', shadow)
    runtime.clock = lambda: datetime(2026, 11, 10, tzinfo=UTC)
    runtime.scoring_actuals = lambda *a: {'realtime': actual_frame(DAY)}
    try:
        assert runtime.score(DAY)['scores_inserted'] == 1
        with store.engine.connect() as connection:
            m = json.loads(connection.execute(text('SELECT metrics FROM forecast.scores')).scalar())
        assert m['served']['mae'] == 1000.
        assert 'a3_b' not in m
        assert 'Shadow scoring skipped' in caplog.text
    finally:
        store.engine.dispose()

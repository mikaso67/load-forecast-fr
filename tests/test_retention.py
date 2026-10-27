"""Retention verifies recoverability before removing any model copy."""

import hashlib
import json
import os
import pickle
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import text

from load_forecast.backtest import day_features, forecast_day
from load_forecast.pipeline import Runtime
from load_forecast.pipeline_artifacts import read_artifact, write_artifact
from load_forecast.pipeline_dry_run import OfflineRuntime, local_store
from load_forecast.production import (
    PreparedRefit,
    ProductionState,
    StateStore,
    WeeklyModels,
    feature_builders,
)
from load_forecast.retention import kept_models, model_signature, prune_artifacts

NOW = datetime(2026, 10, 13, 8, tzinfo=UTC)


def selected_run(root, name, day, variant='B', keep_refit=True):
    models = WeeklyModels(day - timedelta(days=day.weekday()), {}, None, identity=f'model_{name}')
    state = ProductionState('production', last_day=day, models=models, variant=variant)
    folder = root / 'runs' / name
    state_path = StateStore(folder).save(state)
    if keep_refit:
        prepared = PreparedRefit(variant, 'snapshot', NOW, models, state.kalman)
        # Different weeks can share this helper's monday, so use immutable attempts here.
        write_artifact(root / 'refit_attempts' / f'{models.identity}.pkl', prepared)
    return {'run_id': name, 'day': day, 'variant': variant,
            'sha256': hashlib.sha256(state_path.read_bytes()).hexdigest()}


def old_folder(root, name):
    folder = root / 'runs' / name
    folder.mkdir(parents=True)
    (folder / 'unused.pkl').write_bytes(b'fixture')
    stamp = (NOW - timedelta(days=29)).timestamp()
    for path in (folder / 'unused.pkl', folder):
        os.utime(path, (stamp, stamp))
    return folder


def test_prune_dry_run_real_run_and_resume(tmp_path):
    rows = [selected_run(tmp_path, 'old', date(2026, 9, 1)),
            selected_run(tmp_path, 'latest_b', date(2026, 9, 2)),
            selected_run(tmp_path, 'latest_a', date(2026, 8, 1), 'A')]
    duplicate = old_folder(tmp_path, 'duplicate')
    active = old_folder(tmp_path, 'active')
    for day in ('2026-09-01', '2026-10-05', '2026-10-12', '2026-10-13'):
        write_artifact(tmp_path / 'inputs' / f'{day}.pkl', {'day': day})
    for name in ('serving', 'incidents', 'checkpoints', 'checkpoints/history'):
        write_artifact(tmp_path / name / 'keep.pkl', {'keep': True})
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    plan = prune_artifacts(tmp_path, rows, NOW, protected={'active'})
    assert plan['count'] == 3
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = prune_artifacts(tmp_path, rows, NOW, protected={'active'}, dry_run=False)
    assert result['paths'] == plan['paths']
    assert not duplicate.exists() and active.exists()
    assert list((tmp_path / 'runs/latest_b').glob('models_*.pkl'))
    assert list((tmp_path / 'runs/latest_a').glob('models_*.pkl'))
    store = StateStore(tmp_path / 'runs/old')
    state = store.load(rows[0]['day'], model_source=lambda refit, identity, digest:
                       kept_models(tmp_path, 'B', refit, identity, digest,
                                   reference=store.folder / f'refit_{identity}.pkl'))
    assert state.models.identity == 'model_old'
    for name in ('serving', 'incidents', 'checkpoints', 'checkpoints/history'):
        assert read_artifact(tmp_path / name / 'keep.pkl') == {'keep': True}
    assert prune_artifacts(tmp_path, rows, NOW, protected={'active'}, dry_run=False)['count'] == 0


def test_no_kept_refit_refuses_all_deletions(tmp_path):
    rows = [selected_run(tmp_path, 'old', date(2026, 9, 1), keep_refit=False),
            selected_run(tmp_path, 'latest', date(2026, 10, 12))]
    duplicate = old_folder(tmp_path, 'duplicate')
    with pytest.raises(FileNotFoundError, match='No kept refit'):
        prune_artifacts(tmp_path, rows, NOW, dry_run=False)
    assert duplicate.exists()
    assert list((tmp_path / 'runs/old').glob('models_*.pkl'))


def test_mismatched_kept_models_refuse_pruning(tmp_path):
    rows = [selected_run(tmp_path, 'old', date(2026, 9, 1)),
            selected_run(tmp_path, 'latest', date(2026, 10, 12))]
    path = tmp_path / 'refit_attempts/model_old.pkl'
    prepared = read_artifact(path)
    prepared.models.slots['changed'] = {}
    path.write_bytes(pickle.dumps(prepared))
    with pytest.raises(ValueError, match='prediction parameters'):
        prune_artifacts(tmp_path, rows, NOW, dry_run=False)


def test_retention_rejects_links_before_mutation(tmp_path, monkeypatch):
    folder = old_folder(tmp_path, 'duplicate')
    # Exercise Windows junction safety without requiring elevated symlink privileges.
    monkeypatch.setattr(type(folder), 'is_junction', lambda p: p == folder)
    with pytest.raises(ValueError, match='linked'):
        prune_artifacts(tmp_path, [], NOW, dry_run=False)
    assert folder.exists()


def test_retention_requires_aware_time_and_valid_model_identity(tmp_path):
    with pytest.raises(ValueError, match='aware'):
        prune_artifacts(tmp_path, [], NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match='identity'):
        kept_models(tmp_path, 'B', date(2026, 10, 5), '..', '')


@pytest.mark.parametrize('now,today', [
    (datetime(2026, 3, 29, 22, 30, tzinfo=UTC), date(2026, 3, 30)),
    (datetime(2026, 10, 25, 23, 30, tzinfo=UTC), date(2026, 10, 26)),
])
def test_input_retention_uses_paris_day_across_clock_changes(tmp_path, now, today):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    expired = inputs / f'{today - timedelta(days=15)}.pkl'
    retained = inputs / f'{today - timedelta(days=14)}.pkl'
    expired.write_bytes(b'old')
    retained.write_bytes(b'keep')
    plan = prune_artifacts(tmp_path, [], now, dry_run=False)
    assert plan['paths'] == [str(expired)]
    assert retained.exists() and not expired.exists()


def test_canonical_weekly_refit_is_kept(tmp_path):
    models = WeeklyModels(date(2026, 10, 5), {}, None, identity='weekly')
    prepared = SimpleNamespace(variant='A', models=models)
    path = tmp_path / 'refits/A/2026-10-05.pkl'
    write_artifact(path, prepared)
    digest = hashlib.sha256(pickle.dumps(models, protocol=pickle.HIGHEST_PROTOCOL)).hexdigest()
    assert kept_models(tmp_path, 'A', models.refit, models.identity, digest).identity == 'weekly'
    prune_artifacts(tmp_path, [], NOW, dry_run=False)
    assert path.exists()


def test_runtime_queries_selected_states_and_resolves_pruned_models(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: NOW)
    rows = [selected_run(tmp_path, 'old', date(2026, 9, 1)),
            selected_run(tmp_path, 'latest', date(2026, 10, 12))]
    active = old_folder(tmp_path, 'active')
    try:
        with store.engine.begin() as conn:
            for row in [*rows, {'run_id': 'active', 'day': date(2026, 9, 1), 'variant': 'A'}]:
                params = {**row, 'day': str(row['day']), 'stamp': NOW.isoformat()}
                conn.execute(text('''INSERT INTO forecast.run_starts
                    (run_id, delivery_day, variant, freeze_time, kind, started_at, code_commit)
                    VALUES (:run_id, :day, :variant, :stamp, 'primary', :stamp, 'fixture')'''), params)
                if row['run_id'] == 'active':
                    continue
                conn.execute(text('''INSERT INTO forecast.run_results
                    (run_id, completed_at, status, flags) VALUES (:run_id, :stamp, 'complete', '{}')'''), params)
                conn.execute(text('''INSERT INTO forecast.state_runs (delivery_day, variant, run_id)
                    VALUES (:day, :variant, :run_id)'''), params)
                payload = {'folder': str(tmp_path / 'runs' / row['run_id']),
                           'sha256': row['sha256'], 'day': str(row['day'])}
                conn.execute(text('''INSERT INTO forecast.states (run_id, component, state_key, payload)
                    VALUES (:run_id, 'runtime', 'artifact', :payload)'''),
                             {**params, 'payload': json.dumps(payload)})
        assert runtime.prune(NOW.date(), dry_run=False)['count'] == 1
        assert active.exists()
        assert runtime.load_state(date(2026, 9, 2), 'B').models.identity == 'model_old'
    finally:
        store.engine.dispose()


def test_fitted_model_recovery_and_pinned_archive_integrity(tmp_path, monkeypatch):
    day = date(2026, 10, 9)
    monkeypatch.setenv('LFF_CODE_COMMIT', 'fixture')
    runtime = OfflineRuntime(tmp_path, day)
    try:
        runtime.forecast(day, 'B')
        folder = next((tmp_path / 'runs').iterdir())
        original = StateStore(folder).load(day)
        signature = model_signature(original.models)
        later = StateStore(tmp_path / 'runs/later_fixture').save(
            replace(original, last_day=day + timedelta(days=1)))
        rows = [{'run_id': p.parent.name, 'variant': 'B', 'day': d,
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p, d in [(folder / f'state_{day}.pkl', day),
                             (later, day + timedelta(days=1))]]
        now = NOW + timedelta(days=30)
        assert prune_artifacts(tmp_path, rows, now)['count'] == 1
        assert prune_artifacts(tmp_path, rows, now, dry_run=False)['count'] == 1
        def resolve(refit, identity, digest):
            return kept_models(tmp_path, 'B', refit, identity, digest,
                               reference=folder / f'refit_{identity}.pkl')
        restored = StateStore(folder).load(day, model_source=resolve)
        assert model_signature(restored.models) == signature
        inputs = runtime.inputs(day)
        builders = feature_builders(inputs.temperature)
        for family in original.models.slots:
            before = forecast_day(original.models.slots[family], inputs.history, day,
                                  original.models.refit, builders[family])
            after = forecast_day(restored.models.slots[family], inputs.history, day,
                                 restored.models.refit, builders[family])
            pd.testing.assert_frame_equal(before, after, check_exact=True)
        features = day_features(inputs.history, day, builders['gam'])
        assert (original.models.gbm.predict(features) == restored.models.gbm.predict(features)).all()
        source = next((tmp_path / 'refits/B').glob('*.pkl'))
        prepared = read_artifact(source)
        prepared.models.slots['calendar'][0].coef_[0] += 1
        source.write_bytes(pickle.dumps(prepared))
        with pytest.raises(ValueError, match='pinned retention reference'):
            StateStore(folder).load(day, model_source=resolve)
    finally:
        runtime.store.engine.dispose()

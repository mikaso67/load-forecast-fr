"""Weekly retention and publication checks use disposable local resources only."""

import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from load_forecast import flows, pipeline, site_export
from load_forecast.pipeline import Runtime
from load_forecast.pipeline_artifacts import read_artifact
from load_forecast.pipeline_dry_run import local_store
from load_forecast.serving import delivery_deadline

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 13)


@pytest.fixture
def local_publication(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    (workspace / 'scripts').mkdir(parents=True)
    (workspace / 'site').mkdir()
    (workspace / 'site/index.html').write_text('<html>Fixture</html>')
    shutil.copy2(ROOT / 'scripts/publish_site.ps1', workspace / 'scripts/publish_site.ps1')
    monkeypatch.setattr(pipeline, '__file__', str(workspace / 'src/load_forecast/pipeline.py'))
    remote, clone = tmp_path / 'remote.git', tmp_path / 'clone'
    subprocess.run(['git', 'init', '--bare', '-b', 'gh-pages', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', 'clone', str(remote), str(clone)], check=True, capture_output=True)
    for key, value in [('user.name', 'Tester'), ('user.email', 'tester@example.com')]:
        subprocess.run(['git', '-C', str(clone), 'config', key, value], check=True)
    monkeypatch.setenv('LFF_SITE_CLONE', str(clone))
    replay = tmp_path / 'replay'
    replay.mkdir()
    pd.DataFrame([{'variant': 'B', 'value_time': pd.Timestamp('2026-10-14', tz='UTC'),
                   'point': 45000., 'lower': 44000., 'upper': 46000.}]).to_parquet(
                       replay / 'replay_forecasts.parquet')
    monkeypatch.setenv('LFF_REPLAY_DIR', str(replay))
    monkeypatch.setattr(site_export, 'get_reader_engine', lambda: create_engine('sqlite://'))
    runtime = Runtime(tmp_path / 'runtime', SimpleNamespace(),
                      clock=lambda: datetime(2026, 10, 13, tzinfo=UTC))
    return runtime, clone, remote


def test_publish_against_local_bare_remote_without_push(local_publication):
    runtime, clone, remote = local_publication
    assert runtime.publish_site(DAY, dry_run=True)['status'] == 'local_commit_only'
    payload = json.loads((clone / 'data/latest.json').read_text())
    assert payload['status'] == 'ok'
    assert payload['tomorrow']['slots'][0]['forecast_mw'] == 45000
    assert runtime.publish_site(DAY, dry_run=True)['status'] == 'local_commit_only'
    assert not subprocess.check_output(['git', '-C', str(clone), 'status', '--porcelain'])
    refs = subprocess.run(['git', '--git-dir', str(remote), 'show-ref'], capture_output=True)
    assert refs.returncode == 1 and not refs.stdout


def test_publication_script_failure_records_incident(local_publication):
    runtime, clone, _ = local_publication
    (clone / 'unrelated.txt').write_text('Uncommitted work')
    assert runtime.publish_site(DAY, dry_run=True)['status'] == 'failed'
    assert (clone / 'unrelated.txt').read_text() == 'Uncommitted work'
    incident = next((runtime.root / 'incidents').glob('*.pkl'))
    assert read_artifact(incident)['event'] == 'site_publication_failed:CalledProcessError'


def test_daily_scoring_survives_connection_failure(tmp_path, monkeypatch, caplog):
    runtime = Runtime(tmp_path, SimpleNamespace())
    runtime.reconcile = lambda: {}
    runtime.score = lambda day: {'scored': 3}
    monkeypatch.setenv('LFF_SITE_CLONE', str(tmp_path / 'unused'))
    def fail():
        raise OperationalError('connect', None, RuntimeError('private-error-detail'))
    monkeypatch.setattr(site_export, 'get_reader_engine', fail)
    with flows.use_runtime(runtime):
        assert flows.score_and_monitor.fn(DAY) == {'scored': 3}
    assert 'site_publication_failed:OperationalError' in caplog.text
    assert 'private-error-detail' not in caplog.text
    assert len(list((tmp_path / 'incidents').glob('*.pkl'))) == 1


@pytest.mark.parametrize('kind', ['primary', 'fallback'])
def test_noon_forecast_publishes_after_choice_and_reconciliation(kind):
    calls = []
    def read_choice(day):
        assert day == DAY
        assert calls == ['reconcile', 'reconcile']
        return {'kind': kind}
    runtime = SimpleNamespace(
        reconcile=lambda: calls.append('reconcile'),
        forecast=lambda day, variant: {'variant': variant},
        authority=SimpleNamespace(read_choice=read_choice),
        publish_site=lambda day: calls.append(('publish', day)))
    with flows.use_runtime(runtime):
        result = flows.daily_forecast.fn(DAY)
    assert result == [{'variant': 'B'}, {'variant': 'A'}]
    assert calls[-1] == ('publish', DAY)


@pytest.mark.parametrize('kind', ['missing', 'none'])
def test_noon_forecast_skips_publication_without_served_choice(kind):
    read_choice = Mock(return_value={'kind': kind})
    if kind == 'missing':
        read_choice.side_effect = FileNotFoundError
    runtime = Mock()
    runtime.authority.read_choice = read_choice
    with flows.use_runtime(runtime):
        flows.daily_forecast.fn(DAY)
    runtime.publish_site.assert_not_called()


@pytest.mark.parametrize('kind', ['primary', 'fallback'])
def test_noon_publication_failure_preserves_real_choice_and_flow(
        tmp_path, monkeypatch, caplog, kind):
    from load_forecast.pipeline_dry_run import OfflineRuntime

    monkeypatch.setenv('LFF_CODE_COMMIT', 'fixture')
    monkeypatch.setenv('LFF_SITE_CLONE', str(tmp_path / 'unused'))
    runtime = OfflineRuntime(tmp_path, DAY)
    runtime.publish_site = Runtime.publish_site.__get__(runtime)
    if kind == 'fallback':
        runtime.prepare_fallback(DAY)
        runtime.now = delivery_deadline(DAY)
        runtime.authority.deadline_check(DAY)
        runtime.forecast = lambda day, variant: {'variant': variant, 'status': 'late'}
    def fail():
        raise OperationalError('connect', None, RuntimeError('private-error-detail'))
    monkeypatch.setattr(site_export, 'get_reader_engine', fail)
    try:
        with flows.use_runtime(runtime):
            results = flows.daily_forecast.fn(DAY)
        assert len(results) == 2
        choice = runtime.authority.read_choice(DAY)
        assert choice['kind'] == kind
        assert runtime.store.served_for_scoring(runtime.authority, DAY)['kind'] == kind
        incident = next((runtime.root / 'incidents').glob('*.pkl'))
        assert read_artifact(incident)['event'] == 'site_publication_failed:OperationalError'
        assert 'private-error-detail' not in caplog.text
    finally:
        runtime.store.engine.dispose()


def test_weekly_flow_prunes_after_refresh_and_scoring():
    calls = []
    runtime = SimpleNamespace(
        refresh=lambda d: calls.append('refresh'), reconcile=lambda: calls.append('reconcile'),
        score=lambda d: calls.append('score'),
        prune=lambda d, dry_run: calls.append(('prune', dry_run)))
    with flows.use_runtime(runtime):
        flows.consolidated_refresh.fn(DAY, dry_run=True)
    assert calls == ['refresh', 'reconcile', 'score', ('prune', True)]


def test_runtime_prune_empty_database_and_input_tree(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: datetime(2026, 10, 13, tzinfo=UTC))
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    old = inputs / '2026-09-01.pkl'
    old.write_bytes(b'old inputs')
    try:
        assert runtime.prune(DAY)['count'] == 1 and old.exists()
        assert runtime.prune(DAY, dry_run=False)['count'] == 1 and not old.exists()
    finally:
        store.engine.dispose()

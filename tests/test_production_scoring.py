"""Real serving files, fixture actual vintages and disposable SQL scoring persistence."""

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from tests.test_serving import fallback_archive, run_archive

from load_forecast import flows, monitoring
from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.pipeline import Runtime
from load_forecast.pipeline_artifacts import read_artifact
from load_forecast.pipeline_dry_run import local_store
from load_forecast.production_scoring import (
    ACTUALS_QUERY,
    day_metrics,
    forecast_frame,
    monitor_history,
    read_actuals,
)
from load_forecast.serving import ServingAuthority, delivery_deadline

DAY = date(2026, 10, 9)


def actual_frame(day=DAY, actual=46000., ingested='2026-10-10T06:00:00Z'):
    index = pd.DatetimeIndex(delivery_slots(day, '30min'))
    return pd.DataFrame({'consommation': actual, 'prevision_j1': 45500.,
                         'ingested_at': pd.Timestamp(ingested), 'payload_hash': str(actual)},
                        index=index)


def persisted(store):
    with store.engine.connect() as connection:
        return [list(connection.execute(text(f'SELECT * FROM forecast.{table} ORDER BY 1, 2')))
                for table in ('scores', 'monitoring')]


@pytest.mark.parametrize('kind,headline', [('primary', 1000.), ('fallback', 2000.), ('none', None)])
def test_flow_scores_choice_and_both_models_idempotently(tmp_path, kind, headline):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: forecast_cutoff(DAY))
    for variant in ('A', 'B'):
        archive = run_archive(variant, DAY, variant=variant)
        if variant == 'A':
            for value in archive['values']:
                value['point'] = 47000.
        store.start_run(archive['start'])
        store.complete_run(archive)
        if kind == 'primary' and variant == 'B':
            runtime.authority.publish_primary(archive)
    if kind == 'fallback':
        mirror = ServingAuthority(tmp_path / 'other-serving', lambda: forecast_cutoff(DAY))
        mirror.publish_primary(archive)
        store.reconcile(mirror, DAY)
        runtime.authority.prepare_fallback(fallback_archive(DAY))
    runtime.clock = lambda: datetime(2026, 10, 10, 8, tzinfo=UTC)
    runtime.authority.clock = lambda: delivery_deadline(DAY)
    runtime.authority.deadline_check(DAY)
    runtime.scoring_actuals = lambda *a: {'realtime': actual_frame()}
    try:
        with flows.use_runtime(runtime):
            first = flows.score_and_monitor.fn(DAY)
            before = persisted(store)
            second = flows.score_and_monitor.fn(DAY)
        assert first['scores_inserted'] == 1
        assert first['monitoring_inserted'] == 3
        assert second['scores_inserted'] == second['monitoring_inserted'] == 0
        assert persisted(store) == before
        metrics = json.loads(before[0][0][5])
        assert (metrics['served']['mae'] if metrics['served'] else None) == headline
        assert metrics['model']['mae'] == metrics['model_a']['mae'] == 1000.
        assert metrics['rte_d1']['mae'] == 500.
        assert metrics['rte_star'] is None
        if kind == 'fallback':
            assert metrics['served']['coverage'] is None
            with store.engine.connect() as connection:
                assert connection.execute(text('SELECT count(*) FROM forecast.delivery_incidents'))\
                    .scalar_one() == 1
        for row in before[1]:
            state = json.loads(row[-1])
            assert state['h'] == monitoring.H == 11.46
            assert state['k'] == monitoring.K == .5
            assert state['cusum_alert'] is False
            if row[3] == 'served' and kind == 'none':
                assert state['missing'] is True
    finally:
        store.engine.dispose()


def test_provisional_then_final_and_actual_revision_append(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: delivery_deadline(DAY))
    runtime.authority.prepare_fallback(fallback_archive(DAY))
    runtime.authority.deadline_check(DAY)
    runtime.reconcile()
    actuals = {'realtime': actual_frame()}
    cutoffs = []

    def reader(connection, start, end, as_of):
        cutoffs.append(as_of)
        return actuals

    runtime.scoring_actuals = reader
    runtime.clock = lambda: datetime(2026, 10, 10, 8, tzinfo=UTC)
    try:
        runtime.score(DAY)
        original = persisted(store)
        actuals['consolidated'] = actual_frame(actual=47000., ingested='2026-11-15T06:00:00Z')
        runtime.clock = lambda: datetime(2026, 11, 15, 8, tzinfo=UTC)
        assert runtime.score(DAY)['scores_inserted'] == 1
        updated = persisted(store)
        assert len(updated[0]) == 2 and len(updated[1]) == 6
        assert original[0][0] in updated[0]
        final = next(json.loads(r[5]) for r in updated[0] if r[2] == 'consolidated')
        assert forecast_cutoff(DAY) in cutoffs
        assert final['status'] == 'final' and final['served']['mae'] == 3000.
        assert final['rte_star_unavailable'].startswith('RTE* prior Paris year')
        actuals['consolidated'] = actual_frame(actual=48000., ingested='2026-11-16T06:00:00Z')
        runtime.clock = lambda: datetime(2026, 11, 16, 8, tzinfo=UTC)
        assert runtime.score(DAY)['scores_inserted'] == 1
        revised = persisted(store)
        assert len(revised[0]) == 3 and len(revised[1]) == 9
        assert runtime.score(DAY)['monitoring_inserted'] == 0
    finally:
        store.engine.dispose()


@pytest.mark.parametrize('day,count', [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
def test_dst_common_slots_and_missing_values(day, count):
    frame = actual_frame(day)
    frame.iloc[0, 0] = np.nan
    model = forecast_frame(run_archive(day=day))
    a = model.copy()
    a.iloc[1, 0] = np.nan
    metrics = day_metrics(day, frame, 'realtime', model, {'A': a, 'B': model})
    assert metrics['n_common_slots'] == count - 2
    assert all(metrics[key]['n_slots'] == count - 2
               for key in ('served', 'model', 'model_a', 'rte_d1'))


def test_calendar_monitoring_uses_reviewed_functions_and_revises_downstream():
    rows = []
    days = pd.date_range('2026-01-05', periods=30)
    for i, day in enumerate(days):
        m = {'n_slots': 48, 'mean_error': -4000., 'mae': 4000.}
        rows.append({'delivery_day': str(day.date()), 'actual_vintage': 'consolidated',
                     'score_id': str(i), 'metrics': {'served': m, 'model': m, 'model_a': m,
                                                   'rte_d1': {'mae': 100.}}})
    out = monitor_history(rows, days[-1].date())
    served = [json.loads(r['state']) for r in out if r['series'] == 'served']
    errors = pd.Series(4000., index=days)
    expected = monitoring.cusum(monitoring.standardize(errors))
    assert [r['s_plus'] for r in served] == expected.s_plus.tolist()
    assert any(r['cusum_alert'] for r in served)
    assert not served[19]['skill_alert'] and served[20]['skill_alert']
    assert served[-1]['skill_n_days'] == 28
    rows[0]['score_id'] = 'revised'
    revised = monitor_history(rows, days[-1].date())
    assert {r['monitor_id'] for r in out}.isdisjoint({r['monitor_id'] for r in revised})
    missing = monitor_history(rows[:1], (days[0] + timedelta(days=8)).date())
    last = json.loads(missing[8]['state'])
    assert last['event'] == 'restart_missing' and last['missing_run'] == 8


def test_read_actuals_contract_and_dst_boundaries():
    class Result:
        def mappings(self):
            return self

        def all(self):
            return [{'value_time': '2026-03-28T23:00:00Z', 'consommation': 1,
                     'prevision_j1': 2, 'source_dataset': 'eco2mix-national-tr',
                     'ingested_at': '2026-03-30T00:00:00Z', 'payload_hash': 'a'}]

    class Connection:
        def execute(self, query, parameters):
            assert str(query) == ACTUALS_QUERY
            assert parameters['start'] == datetime(2024, 12, 31, 23, tzinfo=UTC)
            assert parameters['end'] == datetime(2026, 3, 29, 22, tzinfo=UTC)
            return Result()

    frames = read_actuals(Connection(), date(2026, 3, 29), date(2026, 3, 29),
                          datetime(2026, 3, 31, tzinfo=UTC))
    assert str(frames['realtime'].index.tz) == 'UTC'
    assert frames['consolidated'].empty
    assert 'ingested_at < :as_of' in ACTUALS_QUERY
    assert "original_values->>'heure'" in ACTUALS_QUERY
    assert 'ORDER BY ingested_at DESC, payload_hash DESC' in ACTUALS_QUERY


def test_empty_runtime_does_not_read_inputs_or_write(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store)
    runtime.scoring_actuals = lambda *a: pytest.fail('empty runtime read actuals')
    try:
        assert runtime.score(DAY)['status'] == 'no_deliveries'
        assert persisted(store) == [[], []]
    finally:
        store.engine.dispose()


def test_unfinished_delivery_day_is_not_scored(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: delivery_deadline(DAY))
    runtime.authority.deadline_check(DAY)
    runtime.reconcile()
    runtime.scoring_actuals = lambda *a: pytest.fail('unfinished day read actuals')
    try:
        assert runtime.score(DAY)['status'] == 'no_deliveries'
        assert persisted(store) == [[], []]
    finally:
        store.engine.dispose()


def test_final_rte_star_uses_provided_issue_time_reference():
    prior = pd.date_range('2025-01-01', '2026-01-01', inclusive='left',
                          freq='30min', tz='Europe/Paris').tz_convert('UTC')
    reference = pd.DataFrame({'consommation': 45000., 'prevision_j1': 45500.}, index=prior)
    frame = actual_frame(actual=47000.)
    model = forecast_frame(run_archive(day=DAY))
    result = day_metrics(DAY, frame, 'consolidated', model, {'A': model, 'B': model}, reference)
    assert result['rte_star']['mae'] == 2000.
    assert result['rte_star_unavailable'] is None


@pytest.mark.parametrize('day,count', [(date(2026, 10, 19), 48), (date(2026, 10, 20), 48),
                                      (date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
@pytest.mark.parametrize('missing', ['row', 'nan', 'inf', 'ingestion'])
def test_partial_actuals_wait_then_score_complete_day(tmp_path, day, count, missing):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: delivery_deadline(day))
    runtime.authority.deadline_check(day)
    runtime.reconcile()
    runtime.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    complete = actual_frame(day)
    assert len(complete) == count
    partial = complete.copy()
    if missing == 'row':
        partial = partial.drop(partial.index[count // 2])
    elif missing == 'ingestion':
        partial.iloc[0, partial.columns.get_loc('ingested_at')] = pd.NaT
    else:
        partial.iloc[0, 0] = np.nan if missing == 'nan' else np.inf
    frames = {'realtime': partial}
    runtime.scoring_actuals = lambda *a: frames
    try:
        assert runtime.score(day)['scores_inserted'] == 0
        assert persisted(store) == [[], []]
        frames['realtime'] = complete
        assert runtime.score(day)['scores_inserted'] == 1
        assert json.loads(persisted(store)[0][0][5])['n_common_slots'] == count
        assert runtime.score(day)['scores_inserted'] == 0
    finally:
        store.engine.dispose()


@pytest.mark.parametrize('day', [DAY, date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize('missing', ['row', 'empty', 'nan', 'ingestion'])
def test_persistent_incomplete_day_records_one_incident_then_recovers(tmp_path, caplog, day, missing):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: delivery_deadline(day))
    runtime.authority.deadline_check(day)
    runtime.reconcile()
    complete = actual_frame(day)
    partial = complete.copy()
    if missing == 'row':
        partial = partial.iloc[:-1]
    elif missing == 'empty':
        partial = partial.iloc[:0]
    elif missing == 'nan':
        partial.iloc[0, 0] = np.nan
    else:
        partial.iloc[0, partial.columns.get_loc('ingested_at')] = pd.NaT
    # No consolidated publication is expected within three days.
    frames = {'realtime': partial, 'consolidated': complete.iloc[:0]}
    runtime.scoring_actuals = lambda *a: frames
    try:
        runtime.clock = lambda: datetime.combine(day + timedelta(days=3), datetime.max.time(), PARIS)
        assert runtime.score(day)['scores_inserted'] == 0
        assert not list((tmp_path / 'incidents').glob('*'))
        runtime.clock = lambda: datetime.combine(day + timedelta(days=4), datetime.min.time(), PARIS)
        assert runtime.score(day)['scores_inserted'] == 0
        incidents = list((tmp_path / 'incidents').glob('*.pkl'))
        assert len(incidents) == 1
        assert read_artifact(incidents[0]) == {'day': day, 'event': 'scoring_incomplete'}
        assert 'incomplete' in caplog.text and str(day) in caplog.text
        original = incidents[0].read_bytes()
        runtime = Runtime(tmp_path, store, clock=lambda: datetime.combine(
            day + timedelta(days=10), datetime.min.time(), PARIS))
        runtime.scoring_actuals = lambda *a: frames
        assert runtime.score(day)['scores_inserted'] == 0
        assert list((tmp_path / 'incidents').glob('*.pkl')) == incidents
        assert incidents[0].read_bytes() == original
        assert persisted(store) == [[], []]
        frames['realtime'] = complete
        assert runtime.score(day)['scores_inserted'] == 1
        assert runtime.score(day)['scores_inserted'] == 0
        assert len(list((tmp_path / 'incidents').glob('*.pkl'))) == 1
    finally:
        store.engine.dispose()


def test_complete_realtime_day_does_not_report_absent_consolidated_as_gap(tmp_path):
    store = local_store(tmp_path)
    runtime = Runtime(tmp_path, store, clock=lambda: delivery_deadline(DAY))
    runtime.authority.deadline_check(DAY)
    runtime.reconcile()
    runtime.clock = lambda: datetime(2026, 11, 15, tzinfo=UTC)
    runtime.scoring_actuals = lambda *a: {'realtime': actual_frame(),
                                       'consolidated': actual_frame().iloc[:0]}
    try:
        assert runtime.score(DAY)['scores_inserted'] == 1
        assert not list((tmp_path / 'incidents').glob('*'))
    finally:
        store.engine.dispose()

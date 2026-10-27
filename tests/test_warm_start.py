from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from load_forecast import production
from load_forecast import warm_start as ws
from load_forecast.aggregation import aggregate
from load_forecast.cutoff import delivery_slots
from load_forecast.gbm_model import GBMConfig
from load_forecast.pipeline import Runtime
from load_forecast.production import EXPERTS, Settings

SETTINGS = Settings(training_start=date(2025, 11, 1), gam_smoothing_weight=1e3,
                    gbm=GBMConfig(learning_rate=0.1, num_leaves=7, min_data_in_leaf=20, rounds=30))
SEED_FIRST, SEED_LAST = date(2025, 12, 1), date(2025, 12, 28)
# Monday 2026-01-05 is the cold start (first quarter, cold variance fit); Monday 2026-01-12 is
# the next refit. The split falls between the two refits and before an offset change.
FIRST, SPLIT, LAST = date(2026, 1, 5), date(2026, 1, 7), date(2026, 1, 13)
OFFSET_CHANGE = date(2026, 1, 9)


def slots_between(first: date, last: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(np.concatenate([delivery_slots(d.date(), '30min')
                                            for d in pd.date_range(first, last)]))


def inputs():
    index = pd.date_range('2025-10-20', '2026-01-20', freq='30min', tz='UTC')
    hours = np.arange(len(index)) / 2.0
    rng = np.random.default_rng(5)
    history = pd.Series(50000 + 4000 * np.sin(2 * np.pi * hours / 24) - 30 * (hours % 168)
                        + rng.normal(0, 300, len(index)), index=index)
    temperature = pd.DataFrame({'temperature_c': 8. + 6. * np.sin(hours / 97.)}, index=index)
    temperature = temperature.loc[temperature.index >= pd.Timestamp('2025-10-31 23:00', tz='UTC')]
    realtime = pd.Series(history.index >= pd.Timestamp('2025-12-20', tz='UTC'), index=index)
    return history, temperature, realtime


def offsets_for(day: date) -> pd.Series:
    return pd.Series(100.0 if day < OFFSET_CHANGE else 250.0, index=range(24))


def archive(history: pd.Series) -> ws.Archive:
    index = slots_between(SEED_FIRST, SEED_LAST)
    rng = np.random.default_rng(6)
    actual = history.reindex(index).to_numpy()
    experts = pd.DataFrame({name: actual + bias + rng.normal(0, 400, len(index))
                            for name, bias in zip(EXPERTS, (300, -200, 50, 0), strict=True)},
                           index=index)
    batch = aggregate(experts, history, per_slot=False)
    weights = batch.weights.xs(0, level='group')
    return ws.Archive(experts, weights, batch.forecast, history.reindex(index))


def seeded(history: pd.Series) -> production.ProductionState:
    state, _ = ws.reconstruct(archive(history), history, SETTINGS)
    return state


@pytest.fixture(scope='module')
def runs(tmp_path_factory):
    history, temperature, realtime = inputs()
    root = tmp_path_factory.mktemp('runtime')
    result = {'history': history, 'temperature': temperature, 'realtime': realtime, 'root': root}
    for variant in ws.VARIANTS:
        state = ws.for_variant(seeded(history), variant)
        result[f'{variant}_full'] = ws.replay_days(state, FIRST, LAST, history, realtime,
                                                   temperature, offsets_for)
        result[f'{variant}_full_state'] = state
        first_part = ws.for_variant(seeded(history), variant)
        before = ws.replay_days(first_part, FIRST, SPLIT, history, realtime, temperature,
                                offsets_for)
        ws.publish_checkpoint(root, first_part, commit='test')
        resumed = ws.load_checkpoint(root, variant)
        after = ws.replay_days(resumed, SPLIT + timedelta(days=1), LAST, history, realtime,
                               temperature, offsets_for)
        result[f'{variant}_split'] = pd.concat([before, after])
        result[f'{variant}_split_state'] = resumed
    return result


@pytest.mark.parametrize('variant', ws.VARIANTS)
def test_a_checkpoint_resumes_the_next_day_identically_to_an_uninterrupted_run(runs, variant):
    pd.testing.assert_frame_equal(runs[f'{variant}_split'], runs[f'{variant}_full'])
    full, split = runs[f'{variant}_full_state'], runs[f'{variant}_split_state']
    np.testing.assert_array_equal(split.mlpoly.regret, full.mlpoly.regret)
    np.testing.assert_array_equal(split.mlpoly.squares, full.mlpoly.squares)
    np.testing.assert_array_equal(split.kalman.filter.delta, full.kalman.filter.delta)
    np.testing.assert_array_equal(split.kalman.params, full.kalman.params)
    assert split.issued.keys() == full.issued.keys() and split.last_day == full.last_day == LAST
    assert (runs[f'{variant}_full']['status'] == ws.REPLAY).all()


def test_the_pipeline_reads_the_checkpoint_and_continues_like_the_replay(runs):
    class NoStateRuns:
        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    runtime = Runtime(runs['root'], SimpleNamespace(engine=NoStateRuns()))
    state = runtime.load_state(SPLIT + timedelta(days=1), 'B')
    mapped = ws.mapped_history(runs['history'], runs['realtime'],
                               offsets_for(SPLIT + timedelta(days=1)), 'B')
    out = production.forecast_production_day(SPLIT + timedelta(days=1), mapped,
                                             runs['temperature'], state)
    expected = runs['B_full'].loc[out.index, out.columns]
    pd.testing.assert_frame_equal(out, expected)
    with pytest.raises(ValueError, match='precede'):
        runtime.load_state(SPLIT, 'A')


def test_variants_differ_only_through_the_mapping_of_real_time_values(runs):
    a, b = runs['A_full'], runs['B_full']
    assert not np.allclose(a['model'], b['model'])
    history, realtime = runs['history'], runs['realtime']
    zero = pd.Series(0.0, index=range(24))
    pd.testing.assert_series_equal(ws.mapped_history(history, realtime, zero, 'B'), history)
    mapped = ws.mapped_history(history, realtime, offsets_for(FIRST), 'B')
    assert (mapped[~realtime] == history[~realtime]).all()
    assert np.allclose(mapped[realtime] - history[realtime], 100.0)
    assert ws.mapped_history(history, realtime, offsets_for(FIRST), 'A').equals(history)


def test_the_reconstruction_equals_seed_state_and_rejects_a_changed_archive():
    history, _, _ = inputs()
    saved = archive(history)
    state, report = ws.reconstruct(saved, history, SETTINGS)
    reference = production.seed_state(saved.experts, history, 'production', SETTINGS)
    np.testing.assert_array_equal(state.mlpoly.regret, reference.mlpoly.regret)
    assert state.issued.keys() == reference.issued.keys()
    assert report['days'] == 28 and report['max_weight_difference'] <= ws.WEIGHT_TOLERANCE
    weights = saved.weights.copy()
    weights.iloc[10, 0] += 1e-6
    with pytest.raises(RuntimeError, match='does not match'):
        ws.reconstruct(ws.Archive(saved.experts, weights, saved.aggregate, saved.actual_2025),
                       history, SETTINGS)
    actual = saved.actual_2025.copy()
    actual.iloc[5] += 1.0
    with pytest.raises(RuntimeError, match='differ from the test'):
        ws.reconstruct(ws.Archive(saved.experts, saved.weights, saved.aggregate, actual),
                       history, SETTINGS)


def test_the_replay_refuses_the_declared_gap_and_checkpoints_only_move_forward(runs, tmp_path):
    history, temperature, realtime = runs['history'], runs['temperature'], runs['realtime']
    state = ws.for_variant(seeded(history), 'A')
    with pytest.raises(ValueError, match='declared without forecast'):
        ws.replay_days(state, date(2026, 1, 4), FIRST, history, realtime, temperature, offsets_for)
    full = runs['A_full_state']
    ws.publish_checkpoint(tmp_path, full, commit='x')
    with pytest.raises(ValueError, match='move forward'):
        ws.publish_checkpoint(tmp_path, full, commit='x')
    manifest = (tmp_path / 'checkpoints' / 'A.manifest.json').read_text(encoding='utf-8')
    assert '"last_day": "2026-01-13"' in manifest and '"first_scheduled_day": "2026-01-14"' in manifest


def year_of_slots(first: str, last: str) -> pd.DatetimeIndex:
    return pd.date_range(first, last, freq='30min', tz='UTC', inclusive='left')


def test_scores_use_the_day_vintage_and_rte_star_only_on_consolidated_days():
    index = year_of_slots('2024-12-31 23:00', '2026-01-03 23:00')
    actual = pd.Series(50000.0, index=index)
    clean = pd.DataFrame({'consommation': actual, 'prevision_j1': actual + 100.0,
                          'source_dataset': 'eco2mix-national-cons-def'})
    clean.loc[slots_between(date(2026, 1, 2), date(2026, 1, 2)),
              'source_dataset'] = 'eco2mix-national-tr'
    slots = slots_between(date(2026, 1, 1), date(2026, 1, 2))
    forecasts = pd.DataFrame({'value_time': slots, 'variant': 'B',
                              'delivery_day': [s.tz_convert('Europe/Paris').date() for s in slots],
                              'point': 50200.0, 'lower': 49000.0, 'upper': 51000.0})
    scores = ws.score_rows(forecasts, clean).set_index('delivery_day')
    first, second = scores.loc[date(2026, 1, 1)], scores.loc[date(2026, 1, 2)]
    assert first['actual_vintage'] == 'consolidated' and second['actual_vintage'] == 'realtime'
    assert first['model_mae'] == pytest.approx(200.0) and first['rte_d1_mae'] == pytest.approx(100.0)
    assert first['rte_star_mae'] == pytest.approx(0.0) and pd.isna(second['rte_star_mae'])
    assert first['model_coverage'] == 1.0 and (scores['status'] == ws.REPLAY).all()
    clean.loc[slots[:3], 'source_dataset'] = 'eco2mix-national-tr'
    with pytest.raises(ValueError, match='mixes'):
        ws.score_rows(forecasts, clean)


def test_the_last_complete_day_needs_the_morning_lag_and_a_selected_run():
    index = year_of_slots('2026-09-30 22:00', '2026-10-03 07:30')
    history = pd.Series(1.0, index=index)
    status = pd.Series({date(2026, 10, 2): 'primary', date(2026, 10, 3): 'primary',
                        date(2026, 10, 4): 'primary'})
    # The last stored slot is 09:00 Paris on 2026-10-03, before its 09:30 lag slot.
    assert ws.last_complete_day(history, status) == date(2026, 10, 3)
    status[date(2026, 10, 3)] = 'weather missing'
    assert ws.last_complete_day(history, status) == date(2026, 10, 2)

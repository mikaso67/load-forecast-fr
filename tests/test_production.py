import copy
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast import production
from load_forecast.aggregation import aggregate
from load_forecast.conformal import seasonal_conformal_intervals
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.gbm_model import GBMConfig
from load_forecast.periods import PRODUCTION_FIRST, PRODUCTION_FIRST_FORECAST, last_allowed_day
from load_forecast.production import (
    EXPERTS,
    ProductionState,
    Settings,
    StateStore,
    check_period,
    forecast_experiment_day,
    forecast_production_day,
    replay,
    seed_state,
)

SETTINGS = Settings(training_start=date(2023, 11, 1), gam_smoothing_weight=1e3,
                    gbm=GBMConfig(learning_rate=0.1, num_leaves=7, min_data_in_leaf=20, rounds=30))
SEED_FIRST, SEED_LAST = date(2023, 12, 1), date(2023, 12, 28)
# Friday to Tuesday: a refit on Monday 2023-12-25 (fourth quarter, cold variance fit) before the
# first day, and one on Monday 2024-01-01 (first quarter, warm start).
FIRST, LAST = date(2023, 12, 29), date(2024, 1, 2)


def slots_between(first: date, last: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(np.concatenate([delivery_slots(d.date(), '30min')
                                            for d in pd.date_range(first, last)]))


def inputs():
    index = pd.date_range('2023-10-20', '2024-01-10', freq='30min', tz='UTC')
    hours = np.arange(len(index)) / 2.0
    rng = np.random.default_rng(3)
    history = pd.Series(50000 + 4000 * np.sin(2 * np.pi * hours / 24) - 30 * (hours % 168)
                        + rng.normal(0, 300, len(index)), index=index)
    temperature = pd.DataFrame({'temperature_c': 8. + 6. * np.sin(hours / 97.)}, index=index)
    temperature = temperature.loc[temperature.index >= pd.Timestamp('2023-10-31 23:00', tz='UTC')]
    return history, temperature


def archived_experts(history: pd.Series) -> pd.DataFrame:
    index = slots_between(SEED_FIRST, SEED_LAST)
    rng = np.random.default_rng(4)
    actual = history.reindex(index).to_numpy()
    return pd.DataFrame({name: actual + bias + rng.normal(0, 400, len(index))
                         for name, bias in zip(EXPERTS, (300, -200, 50, 0), strict=True)},
                        index=index)


@pytest.fixture(scope='module')
def runs(tmp_path_factory):
    history, temperature = inputs()
    experts = archived_experts(history)
    uninterrupted, _ = replay(FIRST, LAST, history, temperature,
                              seed_state(experts, history, 'experiment', SETTINGS))
    folder = tmp_path_factory.mktemp('states')
    before, _ = replay(FIRST, date(2024, 1, 1), history, temperature,
                       seed_state(experts, history, 'experiment', SETTINGS),
                       store=StateStore(folder))
    reloaded = StateStore(folder).load(date(2024, 1, 1))
    after, _ = replay(LAST, LAST, history, temperature, reloaded)
    return {'history': history, 'temperature': temperature, 'experts': experts,
            'uninterrupted': uninterrupted, 'stored': pd.concat([before, after]), 'folder': folder}


def test_stored_and_reloaded_states_give_the_same_days_as_an_uninterrupted_run(runs):
    pd.testing.assert_frame_equal(runs['stored'], runs['uninterrupted'])
    out = runs['uninterrupted']
    assert set(ev.local_dates(out.index)) == {d.date() for d in pd.date_range(FIRST, LAST)}
    assert out[list(EXPERTS)].notna().all().all() and out[ev.MODEL].notna().all()
    assert set(out['refit_date']) == {date(2023, 12, 25), date(2024, 1, 1)}


def test_the_kalman_variances_are_fitted_cold_then_warm_at_each_new_quarter(runs):
    state = StateStore(runs['folder']).load(date(2024, 1, 1))
    assert [fit['refit'] for fit in state.kalman.fits] == [date(2023, 12, 25), date(2024, 1, 1)]
    assert state.kalman.fits[0]['params'].shape == (48, 5)


def test_the_aggregate_and_intervals_equal_the_backtest_functions(runs):
    history, out = runs['history'], runs['uninterrupted']
    experts = pd.concat([runs['experts'], out[list(EXPERTS)]])
    batch = aggregate(experts, history, per_slot=False).forecast
    np.testing.assert_allclose(out[ev.MODEL], batch.loc[out.index], rtol=0, atol=1e-9)
    intervals = seasonal_conformal_intervals(batch, history).loc[out.index]
    np.testing.assert_allclose(out[['lower', 'upper']], intervals[['lower', 'upper']],
                               rtol=0, atol=1e-9)
    assert (out['n_pool'] == intervals['n_pool']).all() and out['lower'].notna().all()


def test_consumption_after_the_d2_point_does_not_change_the_forecast(runs):
    history, temperature = runs['history'], runs['temperature']
    expected = runs['uninterrupted'].loc[slots_between(LAST, LAST)]
    late = history.copy()
    late.loc[late.index > ev.last_usable_slot_start(LAST)] += 5000.0
    state = StateStore(runs['folder']).load(date(2024, 1, 1))
    pd.testing.assert_frame_equal(forecast_experiment_day(LAST, late, temperature, state), expected)
    known = history.copy()
    known.loc[slots_between(LAST - timedelta(days=2), LAST - timedelta(days=2))] += 5000.0
    state = StateStore(runs['folder']).load(date(2024, 1, 1))
    moved = forecast_experiment_day(LAST, known, temperature, state)
    assert not np.allclose(moved[ev.MODEL], expected[ev.MODEL])


def test_the_input_mapping_is_applied_to_the_day_it_is_given(runs):
    calls = []

    def shifted(history: pd.Series, day: date) -> pd.Series:
        calls.append(day)
        return history + 1000.0

    state = StateStore(runs['folder']).load(date(2024, 1, 1))
    out = forecast_experiment_day(LAST, runs['history'], runs['temperature'], state,
                                  mapping=shifted)
    expected = runs['uninterrupted'].loc[out.index]
    assert calls == [LAST]
    assert not np.allclose(out['EXP-004'], expected['EXP-004'])


def test_days_must_move_forward_and_the_store_rejects_other_models(runs):
    folder = runs['folder']
    state = StateStore(folder).load(date(2024, 1, 1))
    with pytest.raises(ValueError, match='move forward'):
        forecast_experiment_day(date(2024, 1, 1), runs['history'], runs['temperature'], state)
    models = StateStore(folder).models_path(date(2024, 1, 1), state.models.identity)
    original = models.read_bytes()
    try:
        models.write_bytes(original + b'x')
        with pytest.raises(ValueError, match='do not match'):
            StateStore(folder).load(date(2024, 1, 1))
    finally:
        models.write_bytes(original)


def test_no_entry_point_reaches_2025_and_the_experiment_lock_is_unchanged():
    assert last_allowed_day() == date(2024, 12, 31) and PRODUCTION_FIRST == date(2026, 1, 1)
    assert PRODUCTION_FIRST_FORECAST == date(2026, 1, 5)
    check_period(date(2024, 12, 31), 'experiment')
    check_period(PRODUCTION_FIRST_FORECAST, 'production')
    for day, period in ((date(2025, 1, 1), 'experiment'), (date(2025, 12, 31), 'production'),
                        (date(2026, 6, 1), 'experiment')):
        with pytest.raises(ValueError):
            check_period(day, period)
    history, temperature = inputs()
    for entry, period, day in ((forecast_experiment_day, 'experiment', date(2025, 1, 6)),
                               (forecast_production_day, 'production', date(2025, 12, 29)),
                               (forecast_production_day, 'experiment', date(2026, 1, 5))):
        with pytest.raises(ValueError):
            entry(day, history, temperature, ProductionState(period=period, settings=SETTINGS))


def test_the_declared_gap_is_refused_before_any_fit_or_state_update(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('A declared day reached the refit')
    monkeypatch.setattr(production, 'refit', forbidden)
    history, temperature = inputs()
    for day in pd.date_range(PRODUCTION_FIRST, PRODUCTION_FIRST_FORECAST - timedelta(days=1)).date:
        state = ProductionState(period='production', settings=SETTINGS)
        with pytest.raises(ValueError, match='declared without forecast'):
            forecast_production_day(day, history, temperature, state)
        with pytest.raises(ValueError, match='declared without forecast'):
            replay(day, PRODUCTION_FIRST_FORECAST, history, temperature, state)
        assert state.models is None and state.last_day is None and not state.issued
    with pytest.raises(ValueError, match='declared without forecast'):
        check_period(date(2026, 1, 4), 'production')
    check_period(date(2026, 1, 5), 'production')


def test_the_seed_refuses_incomplete_or_reordered_experts():
    history, _ = inputs()
    experts = archived_experts(history)
    with pytest.raises(ValueError):
        seed_state(experts[list(reversed(EXPERTS))], history, 'experiment')
    experts.iloc[3, 1] = float('nan')
    with pytest.raises(ValueError):
        seed_state(experts, history, 'experiment')


def test_late_refit_keeps_stale_monday_history_and_adopts_reset_filter_on_tuesday(runs, tmp_path):
    monday, tuesday = date(2024, 1, 1), date(2024, 1, 2)
    sunday = StateStore(runs['folder']).load(date(2023, 12, 31))
    prepared = production.prepare_weekly_refit(
        sunday, runs['history'], runs['temperature'], monday, 'sunday-snapshot',
        clock=lambda: forecast_cutoff(monday) + timedelta(hours=1))
    assert sunday.models.refit == date(2023, 12, 25)
    stale = copy.deepcopy(sunday)
    forecast_experiment_day(monday, runs['history'], runs['temperature'], stale, reuse_models=True)
    assert stale.models.refit == date(2023, 12, 25) and stale.last_day == monday
    store = StateStore(tmp_path)
    store.save(stale)
    issued = copy.deepcopy(stale.issued)
    regret, squares = stale.mlpoly.regret.copy(), stale.mlpoly.squares.copy()
    production.adopt_refit(stale, prepared, tuesday)
    assert stale.models.refit == monday and stale.models.identity == prepared.models.identity
    assert stale.kalman.filter.last_day == monday - timedelta(days=2)
    np.testing.assert_array_equal(stale.mlpoly.regret, regret)
    np.testing.assert_array_equal(stale.mlpoly.squares, squares)
    for day in issued:
        pd.testing.assert_series_equal(stale.issued[day].point, issued[day].point)
    out = forecast_experiment_day(tuesday, runs['history'], runs['temperature'], stale)
    expected = runs['uninterrupted'].loc[out.index]
    np.testing.assert_allclose(out[list(EXPERTS)], expected[list(EXPERTS)], rtol=0, atol=1e-9)
    assert stale.kalman.filter.last_day == tuesday - timedelta(days=2)
    assert store.load(monday).models.refit == date(2023, 12, 25)
    assert store.load(monday).models.identity != stale.models.identity
    other = copy.deepcopy(sunday)
    other.variant = 'A'
    with pytest.raises(ValueError, match='variant'):
        production.adopt_refit(other, prepared, tuesday)
    with pytest.raises(ValueError, match='before'):
        production.adopt_refit(stale, production.replace(
            prepared, completed_at=forecast_cutoff(tuesday)), tuesday)

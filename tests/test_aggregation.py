import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.aggregation import MLPoly, aggregate
from load_forecast.cutoff import delivery_slots


def synthetic(first: str = '2024-03-01', days: int = 24, seed: int = 3):
    """Three experts with different noise levels around a smooth load, on the real delivery grid."""
    slots = pd.DatetimeIndex([s for d in pd.date_range(first, periods=days).date
                              for s in delivery_slots(d, '30min')])
    rng = np.random.default_rng(seed)
    base = 50_000 + 8_000 * np.sin(np.arange(len(slots)) / 24.0)
    actual = pd.Series(base + rng.normal(0, 300, len(slots)), index=slots)
    forecasts = pd.DataFrame({'sharp': base + rng.normal(0, 200, len(slots)),
                              'flat': base + rng.normal(0, 900, len(slots)),
                              'biased': base + 1_500 + rng.normal(0, 300, len(slots))}, index=slots)
    return forecasts, actual


def test_no_regret_gives_uniform_weights():
    assert np.allclose(MLPoly(2, 4).weights(), 0.25)


def test_one_update_matches_hand_computation():
    state = MLPoly(1, 2)
    # Aggregate 100 (uniform), actual 90: gradient +1, regret_k = 100 - f_k = (-20, +20).
    state.update(np.array([0]), np.array([[120.0, 80.0]]), np.array([90.0]), state.weights())
    assert np.allclose(state.regret, [[-20.0, 20.0]])
    assert np.allclose(state.squares, [[400.0, 400.0]])
    assert np.allclose(state.weights(), [[0.0, 1.0]])


def test_two_updates_use_the_inverse_squared_regret_learning_rate():
    state = MLPoly(1, 2)
    state.regret[:] = [[30.0, 10.0]]
    state.squares[:] = [[899.0, 99.0]]
    # eta = 1/900 and 1/100, so raw weights 30/900 = 1/30 and 10/100 = 1/10, i.e. 0.25 and 0.75.
    assert np.allclose(state.weights(), [[0.25, 0.75]])


def test_weights_are_a_convex_combination():
    forecasts, actual = synthetic()
    for per_slot in (True, False):
        result = aggregate(forecasts, actual, per_slot=per_slot)
        assert (result.weights >= 0).all().all()
        assert np.allclose(result.weights.sum(axis=1), 1.0)
        low, high = forecasts.min(axis=1), forecasts.max(axis=1)
        assert ((result.forecast >= low - 1e-6) & (result.forecast <= high + 1e-6)).all()


def test_first_two_days_are_uniform_then_weights_move():
    forecasts, actual = synthetic()
    result = aggregate(forecasts, actual, per_slot=False).weights
    days = sorted(set(result.index.get_level_values('day')))
    assert np.allclose(result.loc[days[0]], 1 / 3) and np.allclose(result.loc[days[1]], 1 / 3)
    assert not np.allclose(result.loc[days[2]], 1 / 3)


def test_the_sharp_expert_ends_with_the_largest_weight():
    forecasts, actual = synthetic(days=40)
    for per_slot in (True, False):
        weights = aggregate(forecasts, actual, per_slot=per_slot).weights
        last = weights.xs(weights.index.get_level_values('day').max(), level='day').mean()
        assert last.idxmax() == 'sharp'


@pytest.mark.parametrize('per_slot', [True, False])
def test_forecast_of_day_d_ignores_actuals_after_d_minus_2(per_slot):
    forecasts, actual = synthetic()
    full = aggregate(forecasts, actual, per_slot=per_slot).forecast
    local = pd.Series(ev.local_dates(forecasts.index), index=forecasts.index)
    for target in sorted(set(local))[3:]:
        cut = actual.copy()
        cut[local > target - pd.Timedelta(days=2)] = np.nan
        truncated = aggregate(forecasts, cut, per_slot=per_slot).forecast
        mask = local == target
        assert np.array_equal(full[mask], truncated[mask]), target


def test_truncation_test_fails_for_an_update_with_day_d_minus_1():
    forecasts, actual = synthetic()
    local = pd.Series(ev.local_dates(forecasts.index), index=forecasts.index)
    full = aggregate(forecasts, actual, per_slot=False, update_lag_days=1).forecast
    differs = 0
    for target in sorted(set(local))[3:]:
        cut = actual.copy()
        cut[local > target - pd.Timedelta(days=2)] = np.nan
        truncated = aggregate(forecasts, cut, per_slot=False, update_lag_days=1).forecast
        mask = local == target
        differs += not np.array_equal(full[mask], truncated[mask])
    assert differs > 0


def test_missing_actual_slots_are_skipped_not_imputed():
    forecasts, actual = synthetic()
    gap = actual.copy()
    gap.iloc[100:130] = np.nan
    result = aggregate(forecasts, gap, per_slot=True)
    assert result.forecast.notna().all()


def test_incomplete_forecasts_are_refused():
    forecasts, actual = synthetic()
    forecasts.iloc[5, 1] = np.nan
    with pytest.raises(ValueError, match='complete'):
        aggregate(forecasts, actual, per_slot=True)


def test_daylight_saving_days_keep_the_slot_grid():
    forecasts, actual = synthetic(first='2024-03-29', days=5)  # spring change on 2024-03-31
    spring = aggregate(forecasts, actual, per_slot=True)
    assert spring.forecast.notna().all() and len(spring.forecast) == len(forecasts)
    forecasts, actual = synthetic(first='2024-10-24', days=6)  # autumn change on 2024-10-27
    autumn = aggregate(forecasts, actual, per_slot=True)
    assert autumn.forecast.notna().all() and len(autumn.forecast) == len(forecasts)
    assert set(autumn.weights.index.get_level_values('group')) == set(range(48))

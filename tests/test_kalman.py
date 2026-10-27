from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast import kalman
from load_forecast.backtest import day_features, fit_slot_models, local_slots, walk_forward
from load_forecast.cutoff import delivery_slots
from load_forecast.gam_model import TEMPERATURE_COLUMN, SlotGAM, lagged_gam_features
from load_forecast.kalman import (
    N_STATE,
    build_window,
    extend_window,
    fit_variances,
    initial_state,
    log_likelihood,
    update_step,
    walk_forward_kalman,
)

TRAINING_START = date(2023, 11, 1)
ALPHA = (1e3,)


def slots_of(day: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(delivery_slots(day, '30min'))


def history_between(first: str, last: str) -> pd.Series:
    """Value = hours elapsed since 2024-01-01 UTC, so every value identifies its own timestamp."""
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    hours = (index - pd.Timestamp('2024-01-01', tz='UTC')) / pd.Timedelta(hours=1)
    return pd.Series(np.asarray(hours, dtype=float), index=index)


def temperature_frame(first: str, last: str) -> pd.DataFrame:
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    return pd.DataFrame({'temperature_c': 8. + 6. * np.sin(np.arange(len(index)) / 97.)},
                        index=index)


def gam_factory() -> SlotGAM:
    return SlotGAM([TEMPERATURE_COLUMN], alpha_grid=ALPHA, missing_as_nan=True)


def setup(seed: int = 5):
    temperature = temperature_frame('2023-10-31 23:00', '2024-03-26 23:30')
    history = history_between('2023-10-20', '2024-03-26')
    history = history + np.random.default_rng(seed).normal(0, 3, len(history))
    return temperature, history


def run(history, temperature, first, last, **kwargs):
    return walk_forward_kalman(history, first, last, gam_factory,
                               builder=lagged_gam_features(temperature),
                               training_start=TRAINING_START, **kwargs)


def reference_filter(x, r, noise, drift, prior):
    """Scalar textbook Kalman filter, one slot, one observation per step."""
    delta, covariance = np.zeros(len(drift)), prior * np.eye(len(drift))
    log_like = 0.0
    for xt, rt in zip(x, r, strict=True):
        covariance = covariance + np.diag(drift)
        if not np.isfinite(rt):
            continue
        variance = xt @ covariance @ xt + noise
        error = rt - xt @ delta
        gain = covariance @ xt / variance
        delta = delta + gain * error
        covariance = covariance - np.outer(gain, xt @ covariance)
        log_like += -0.5 * (np.log(2 * np.pi * variance) + error ** 2 / variance)
    return delta, log_like


def random_problem(n_days=60, n_slots=3, seed=0):
    rng = np.random.default_rng(seed)
    design = np.ones((n_days, 2, n_slots, N_STATE))
    design[..., 1:] = rng.normal(size=(n_days, 2, n_slots, N_STATE - 1))
    design[:, 1] = np.nan
    residual = rng.normal(0, 5, (n_days, 2, n_slots))
    residual[:, 1] = np.nan
    residual[min(7, n_days - 1), 0, 1] = np.nan
    return design, residual


def test_log_likelihood_matches_a_scalar_textbook_filter():
    design, residual = random_problem()
    rng = np.random.default_rng(1)
    params = rng.normal(0, 0.5, (2, 3, 1 + N_STATE))
    got = log_likelihood(design, residual, params)
    for replica in range(2):
        for slot in range(3):
            _, expected = reference_filter(design[:, 0, slot], residual[:, 0, slot],
                                           np.exp(params[replica, slot, 0]),
                                           np.exp(params[replica, slot, 1:]), kalman.PRIOR_VARIANCE)
            assert got[replica, slot] == pytest.approx(expected, rel=1e-9)


def test_without_drift_and_with_a_diffuse_prior_the_filter_is_recursive_least_squares():
    design, residual = random_problem(n_days=80, n_slots=2, seed=3)
    state = initial_state(2)
    state.covariance *= 1e6 / kalman.PRIOR_VARIANCE
    noise = np.full(2, 25.0)
    for t in range(80):
        update_step(state.delta, state.covariance, design[t, 0], residual[t, 0], noise)
    for slot in range(2):
        rows = np.isfinite(residual[:, 0, slot])
        ols = np.linalg.lstsq(design[rows, 0, slot], residual[rows, 0, slot], rcond=None)[0]
        np.testing.assert_allclose(state.delta[slot], ols, rtol=1e-4, atol=1e-4)


def test_missing_observations_leave_the_state_untouched():
    design, residual = random_problem(n_days=3, n_slots=2)
    state = initial_state(2)
    before = state.delta.copy(), state.covariance.copy()
    contribution = update_step(state.delta, state.covariance, design[0, 1], residual[0, 1],
                               np.ones(2))
    np.testing.assert_array_equal(state.delta, before[0])
    np.testing.assert_array_equal(state.covariance, before[1])
    assert (contribution == 0).all()


def test_fitted_variances_recover_a_drifting_level():
    rng = np.random.default_rng(7)
    n_days = 500
    level = np.cumsum(rng.normal(0, 2.0, n_days))
    design = np.zeros((n_days, 2, 1, N_STATE))
    design[:, 0, 0, 0] = 1.0
    design[:, 1] = np.nan
    residual = np.full((n_days, 2, 1), np.nan)
    residual[:, 0, 0] = level + rng.normal(0, 10.0, n_days)
    params, info = fit_variances(design, residual)
    assert info['converged'] or info['nit'] > 5
    assert 40 < np.exp(params[0, 0]) < 200
    assert 1.5 < np.exp(params[0, 1]) < 10


def test_partial_effects_sum_to_the_prediction_tails_included():
    rng = np.random.default_rng(2)
    x = pd.DataFrame({TEMPERATURE_COLUMN: rng.uniform(0, 25, 300),
                      'lag_d2': rng.normal(3000, 100, 300), 'weekend': rng.integers(0, 2, 300) * 1.0})
    y = pd.Series(3000 + 8 * (x[TEMPERATURE_COLUMN] - 12) ** 2 + 0.5 * x['lag_d2']
                  + 40 * x['weekend'] + rng.normal(0, 15, 300))
    model = SlotGAM([TEMPERATURE_COLUMN], alpha_grid=ALPHA, missing_as_nan=True).fit(x, y)
    probe = x.iloc[:20].copy()
    probe.loc[probe.index[:4], TEMPERATURE_COLUMN] = [-15, -5, 40, 60]
    probe.iloc[5, 1] = np.nan
    effects = model.partial_effects(probe, {'lags': ['lag_d2']})
    assert list(effects.columns) == ['base', 'lags', 'smooth']
    assert effects.iloc[5].isna().all() and effects.drop(index=probe.index[5]).notna().all().all()
    np.testing.assert_allclose(effects.sum(axis=1).drop(index=probe.index[5]),
                               pd.Series(model.predict(probe), index=probe.index).drop(
                                   index=probe.index[5]), rtol=1e-9)
    coefficient = model.result_.params[1 + model.linear_cols_.index('lag_d2')]
    np.testing.assert_allclose(effects['lags'].dropna(), coefficient * probe['lag_d2'].dropna())
    with pytest.raises(ValueError, match='Group columns'):
        model.partial_effects(probe, {'lags': ['nope']})


def window_for(first: date, last: date):
    temperature, history = setup()
    builder = lagged_gam_features(temperature)
    cache: dict = {}
    models = fit_slot_models(history, first, gam_factory, builder=builder, published_at=None,
                             training_start=TRAINING_START, cache=cache)
    days = [d.date() for d in pd.date_range(TRAINING_START, last)]
    for d in days:
        cache.setdefault(d, day_features(history, d, builder))
    training = [d for d in days if d <= first]
    return history, models, cache, days, build_window(models, cache, days, training)


def test_window_effects_add_up_to_the_gam_and_are_standardized_on_training_days():
    history, models, cache, days, window = window_for(date(2024, 3, 11), date(2024, 3, 17))
    t = window.day_index(date(2024, 3, 14))
    x = cache[date(2024, 3, 14)]
    expected = np.concatenate([models[s].predict(x.loc[local_slots(x.index) == s])
                               for s in range(48)])
    order = np.argsort(np.concatenate([np.flatnonzero(local_slots(x.index) == s)
                                       for s in range(48)]))
    np.testing.assert_allclose(window.gam[window.rows[t]], expected[order], rtol=1e-9)
    train_rows = np.concatenate([window.rows[window.day_index(d)] for d in days
                                 if d <= date(2024, 3, 11)])
    slot = np.asarray(local_slots(window.index))[train_rows]
    block = window.design[train_rows][slot == 5]
    np.testing.assert_allclose(block.mean(axis=0), 0, atol=1e-9)
    np.testing.assert_allclose(block.std(axis=0), 1, atol=1e-9)


def test_a_dst_day_has_a_second_occurrence_and_a_short_day_misses_slots():
    class Stub:
        def partial_effects(self, x, groups):
            n = len(x)
            return pd.DataFrame({'base': np.arange(n, dtype=float), 'lags': np.zeros(n),
                                 'smooth': np.ones(n)}, index=x.index)

    def builder(slots, history, day):
        return pd.DataFrame({'a': 1.0}, index=slots)

    days = [date(2024, 3, 30), date(2024, 3, 31), date(2024, 10, 26), date(2024, 10, 27)]
    features = {d: day_features(pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC')), d,
                                builder) for d in days}
    window = build_window({s: Stub() for s in range(48)}, features, days, days[:1])
    spring, autumn = window.pos[1], window.pos[3]
    assert (spring[1] >= 0).sum() == 0 and (spring[0] < 0).sum() == 2
    assert (autumn[1] >= 0).sum() == 2 and (autumn[0] < 0).sum() == 0
    assert np.isfinite(window.gam[window.rows[2]]).all()
    flat = window.design[:, 2]
    assert np.isfinite(flat).all() and (flat == 0).all()


def test_days_appended_one_by_one_equal_the_window_built_at_once():
    class Stub:
        def partial_effects(self, x, groups):
            values = np.asarray(local_slots(x.index), dtype=float) + x['a'].to_numpy()
            return pd.DataFrame({'base': values, 'lags': values ** 2, 'smooth': -values},
                                index=x.index)

    def builder(slots, history, day):
        return pd.DataFrame({'a': float(day.day)}, index=slots)

    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC'))
    days = [date(2024, 10, 24) + timedelta(days=k) for k in range(5)]  # 27 October has 50 slots
    features = {d: day_features(empty, d, builder) for d in days}
    models = {s: Stub() for s in range(48)}
    whole = build_window(models, features, days, days[:2])
    grown = build_window(models, features, days[:2], days[:2])
    for d in days[2:]:
        extend_window(grown, models, d, features[d])
    assert grown.days == whole.days and grown.index.equals(whole.index)
    np.testing.assert_array_equal(grown.pos, whole.pos)
    np.testing.assert_array_equal(grown.gam, whole.gam)
    np.testing.assert_array_equal(grown.design, whole.design)
    for a, b in zip(grown.rows, whole.rows, strict=True):
        np.testing.assert_array_equal(a, b)
    with pytest.raises(ValueError, match='calendar order'):
        extend_window(grown, models, days[-1] + timedelta(days=2), features[days[0]])


def test_first_week_is_the_same_in_both_modes_and_later_weeks_differ():
    temperature, history = setup()
    result = run(history, temperature, date(2024, 3, 11), date(2024, 3, 24))
    restart, keep = result.corrected['restart'], result.corrected['keep']
    week_one = restart.index < pd.Timestamp('2024-03-17 23:00', tz='UTC')
    np.testing.assert_allclose(restart.loc[week_one, ev.MODEL], keep.loc[week_one, ev.MODEL])
    assert not np.allclose(restart.loc[~week_one, ev.MODEL], keep.loc[~week_one, ev.MODEL])
    assert len(result.fits) == 1 and result.gam.index.equals(restart.index)


def test_the_correction_moves_the_forecast_but_nan_features_stay_nan():
    temperature, history = setup()
    day = date(2024, 3, 20)
    damaged = history.drop(slots_of(day)[10] - pd.Timedelta(days=7))
    broken = run(damaged, temperature, day, day)
    assert broken.corrected['keep'][ev.MODEL].isna().all()
    fine = run(history, temperature, day, day)
    assert fine.corrected['keep'][ev.MODEL].notna().all()
    assert not np.allclose(fine.corrected['keep'][ev.MODEL], fine.gam[ev.MODEL])


def perturbed(history, day, first_slot, last_slot, amount=5000.0):
    out = history.copy()
    slots = slots_of(day)[first_slot:last_slot]
    out.loc[slots] = out.loc[slots] + amount
    return out


def test_forecast_is_unchanged_by_consumption_after_the_d2_update_point():
    temperature, history = setup()
    day = date(2024, 3, 20)
    boundary = ev.last_usable_slot_start(day)
    base = run(history, temperature, day, day)
    previous_day = day - timedelta(days=1)
    early_d1 = history.loc[(history.index >= slots_of(previous_day)[0])
                           & (history.index < boundary)].index
    damaged = history.copy()
    damaged.loc[early_d1] += 5000.0
    other = run(damaged, temperature, day, day)
    for mode in ('restart', 'keep'):
        pd.testing.assert_frame_equal(base.corrected[mode], other.corrected[mode])
    shortened = run(history.loc[history.index <= boundary], temperature, day, day)
    for mode in ('restart', 'keep'):
        pd.testing.assert_frame_equal(base.corrected[mode], shortened.corrected[mode])


def test_a_filter_updated_with_d1_is_detected_and_d2_does_change_the_forecast(monkeypatch):
    temperature, history = setup()
    day = date(2024, 3, 20)
    boundary = ev.last_usable_slot_start(day)
    previous_day = day - timedelta(days=1)
    early_d1 = history.loc[(history.index >= slots_of(previous_day)[0])
                           & (history.index < boundary)].index
    damaged = history.copy()
    damaged.loc[early_d1] += 5000.0
    base = run(history, temperature, day, day)
    monkeypatch.setattr(kalman, 'UPDATE_LAG_DAYS', 1)
    leaking_base = run(history, temperature, day, day)
    leaking = run(damaged, temperature, day, day)
    assert not np.allclose(leaking_base.corrected['keep'][ev.MODEL], leaking.corrected['keep'][ev.MODEL])
    assert not np.allclose(base.corrected['keep'][ev.MODEL], leaking_base.corrected['keep'][ev.MODEL])
    monkeypatch.undo()
    d2 = day - timedelta(days=2)
    changed = run(perturbed(history, d2, 0, 48), temperature, day, day)
    before = base.corrected['keep'][ev.MODEL] - base.gam[ev.MODEL]
    after = changed.corrected['keep'][ev.MODEL] - changed.gam[ev.MODEL]
    assert not np.allclose(before, after)


def test_gam_output_equals_the_plain_runner():
    temperature, history = setup()
    day = date(2024, 3, 21)
    plain = walk_forward(history, day, day, gam_factory, builder=lagged_gam_features(temperature),
                         training_start=TRAINING_START)
    pd.testing.assert_frame_equal(run(history, temperature, day, day).gam, plain)


def test_unknown_mode_and_bad_period_are_refused():
    temperature, history = setup()
    with pytest.raises(ValueError, match='Unknown modes'):
        run(history, temperature, date(2024, 3, 20), date(2024, 3, 20), modes=('none',))
    with pytest.raises(ValueError, match='after training_start'):
        run(history, temperature, date(2025, 1, 2), date(2025, 1, 3))

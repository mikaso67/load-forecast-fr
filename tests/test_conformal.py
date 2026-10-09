import functools
from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import local_slots
from load_forecast.conformal import (
    adaptive_conformal_intervals,
    adaptive_ranks,
    conformal_intervals,
    coverage_bootstrap,
    coverage_difference_bootstrap,
    first_full_window_day,
    interval_report,
    order_statistic_ranks,
    pinball_loss,
    scored_slots,
    seasonal_conformal_intervals,
    window_quantiles,
)
from load_forecast.cutoff import delivery_slots


def synthetic(first: str = '2024-03-01', days: int = 40, seed: int = 5):
    """Point forecast and actuals on the real delivery grid, noise of about 500 MW."""
    slots = pd.DatetimeIndex([s for d in pd.date_range(first, periods=days).date
                              for s in delivery_slots(d, '30min')])
    rng = np.random.default_rng(seed)
    base = 50_000 + 8_000 * np.sin(np.arange(len(slots)) / 24.0)
    forecast = pd.Series(base, index=slots)
    actual = pd.Series(base + rng.normal(0, 500, len(slots)), index=slots)
    return forecast, actual


def test_ranks_for_small_and_large_windows():
    # n = 28: floor(29 * 0.1) = 2 and ceil(29 * 0.9) = 27.
    assert order_statistic_ranks(28) == (2, 27)
    # n = 29 hits 30 * 0.1 = 3 and 30 * 0.9 = 27 exactly, where float noise could move a rank.
    assert order_statistic_ranks(29) == (3, 27)
    assert order_statistic_ranks(9) == (1, 9)
    # Too few residuals for a tail: no rank, the interval is undefined.
    assert order_statistic_ranks(8) == (None, None)
    assert order_statistic_ranks(0) == (None, None)


def test_window_quantiles_by_hand():
    residuals = np.arange(1.0, 29.0)[::-1]  # 28 values, 1 to 28 in decreasing order
    low, high = window_quantiles(residuals)
    assert (low, high) == (2.0, 27.0)
    # NaN residuals (missing actuals) are not counted: 27 values 1 to 27, ranks (2, 26).
    assert window_quantiles(np.append(residuals[1:], np.nan)) == (2.0, 26.0)


def test_interval_is_the_point_forecast_plus_slot_residual_quantiles_of_the_window():
    forecast, actual = synthetic()
    window = 12
    result = conformal_intervals(forecast, actual, window_days=window)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    target = date(2024, 3, 30)
    residual = actual - forecast
    first, last = date(2024, 3, 30 - 2 - window + 1), date(2024, 3, 28)
    slot = 7
    today = forecast.index[(local == target)][slot]
    past = forecast.index[(local >= first) & (local <= last)][slot::48]
    low, high = window_quantiles(residual.loc[past].to_numpy())
    assert result.loc[today, 'lower'] == pytest.approx(forecast[today] + low)
    assert result.loc[today, 'upper'] == pytest.approx(forecast[today] + high)


def test_first_days_without_a_full_enough_window_are_undefined():
    forecast, actual = synthetic()
    result = conformal_intervals(forecast, actual, window_days=12)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    assert result[local == date(2024, 3, 3)].isna().all().all()
    assert result[local == date(2024, 3, 30)].notna().all().all()
    # The first usable day is the one where the window holds the 9 residuals the tails need.
    assert first_full_window_day(date(2024, 3, 1), 12) == date(2024, 3, 14)


def test_interval_of_day_d_ignores_actuals_after_d_minus_2():
    forecast, actual = synthetic()
    full = conformal_intervals(forecast, actual, window_days=14)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    for target in sorted(set(local))[16:]:
        cut = actual.copy()
        cut[local > target - pd.Timedelta(days=2)] = np.nan
        truncated = conformal_intervals(forecast, cut, window_days=14)
        mask = local == target
        assert np.array_equal(full[mask].to_numpy(), truncated[mask].to_numpy()), target


def test_a_variant_that_absorbs_d_minus_1_fails_the_truncation_check():
    forecast, actual = synthetic()
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    leaky = conformal_intervals(forecast, actual, window_days=14, update_lag_days=1)
    target = sorted(set(local))[20]
    cut = actual.copy()
    cut[local > target - pd.Timedelta(days=2)] = np.nan
    truncated = conformal_intervals(forecast, cut, window_days=14, update_lag_days=1)
    mask = local == target
    assert not np.array_equal(leaky[mask].to_numpy(), truncated[mask].to_numpy())


def test_window_length_changes_the_interval():
    forecast, actual = synthetic()
    short = conformal_intervals(forecast, actual, window_days=10)
    long = conformal_intervals(forecast, actual, window_days=30)
    both = short.dropna().index.intersection(long.dropna().index)
    assert len(both) > 0
    assert not np.array_equal(short.loc[both].to_numpy(), long.loc[both].to_numpy())


@pytest.mark.parametrize(('first', 'day', 'n_slots'), [
    ('2024-03-10', '2024-03-31', 46), ('2024-10-05', '2024-10-27', 50)])
def test_dst_days_get_an_interval_on_every_slot(first, day, n_slots):
    # The repeated autumn local slots share one calibration pool, the missing spring ones have none.
    forecast, actual = synthetic(first=first, days=30)
    result = conformal_intervals(forecast, actual, window_days=14)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    rows = result[local == date.fromisoformat(day)]
    assert len(rows) == n_slots
    assert rows.notna().all().all()
    assert (rows.upper > rows.lower).all()


def test_a_nan_actual_slot_changes_nothing_but_its_own_residual():
    forecast, actual = synthetic()
    broken = actual.copy()
    broken.iloc[100] = np.nan
    result = conformal_intervals(forecast, broken, window_days=14)
    assert result.notna().sum().sum() > 0
    assert np.isfinite(result.dropna().to_numpy()).all()


def test_coverage_of_noise_exchangeable_residuals_is_near_the_level():
    rng = np.random.default_rng(1)
    slots = pd.DatetimeIndex([s for d in pd.date_range('2023-01-01', periods=400).date
                              for s in delivery_slots(d, '30min')])
    forecast = pd.Series(50_000.0, index=slots)
    actual = pd.Series(50_000.0 + rng.normal(0, 700, len(slots)), index=slots)
    result = conformal_intervals(forecast, actual, window_days=60)
    frame = scored_slots(result, forecast, actual)
    assert 0.77 < frame.covered.mean() < 0.86


def test_pinball_loss_by_hand():
    y = np.array([10.0, 20.0])
    # tau 0.9: under-forecast 8 costs 0.9 * 2, over-forecast 25 costs 0.1 * 5.
    assert pinball_loss(y, np.array([8.0, 25.0]), 0.9) == pytest.approx((0.9 * 2 + 0.1 * 5) / 2)
    assert pinball_loss(y, y, 0.5) == 0.0


def test_scored_slots_drop_undefined_intervals_and_missing_actuals():
    forecast, actual = synthetic(days=20)
    actual.iloc[400] = np.nan
    result = conformal_intervals(forecast, actual, window_days=12)
    frame = scored_slots(result, forecast, actual)
    assert not frame.isna().any().any()
    assert forecast.index[400] not in frame.index
    assert len(frame) < len(forecast)


def test_coverage_bootstrap_interval_contains_the_point_and_uses_slot_weights():
    forecast, actual = synthetic(days=60)
    frame = scored_slots(conformal_intervals(forecast, actual, window_days=12), forecast, actual)
    report = coverage_bootstrap(frame, n_draws=500)
    assert report['low'] <= report['coverage'] <= report['high']
    assert report['coverage'] == pytest.approx(frame.covered.mean())
    assert report['n_slots'] == len(frame)


def test_interval_report_has_seasons_day_types_and_pinball():
    forecast, actual = synthetic(first='2024-02-01', days=120)
    frame = scored_slots(conformal_intervals(forecast, actual, window_days=14), forecast, actual)
    report = interval_report(frame, cold={date(2024, 3, 12): True})
    assert set(report['by_season']) == {'winter', 'spring'}
    assert 'cold' in report['by_day_type'] and 'ordinary' in report['by_day_type']
    assert report['by_day_type']['cold_any']['n_days'] == 1
    assert set(report['pinball']) == {'0.1', '0.5', '0.9'}
    assert sum(s['n_slots'] for s in report['by_season'].values()) == len(frame)


# Adaptive conformal inference (EXP-011)


def shifted(first: str = '2024-03-01', days: int = 60, jump_day: int = 30, jump: float = 4_000.0):
    """Actuals that jump upwards after `jump_day`, like a cold spell the window has not seen."""
    forecast, actual = synthetic(first=first, days=days)
    local = np.asarray(ev.local_dates(forecast.index))
    cut = date.fromisoformat(first).toordinal() + jump_day
    after = np.array([d.toordinal() >= cut for d in local])
    return forecast, actual + np.where(after, jump, 0.0)


def test_adaptive_ranks_match_the_plain_ranks_and_are_clipped():
    assert adaptive_ranks(28, 0.2) == (2, 27)
    assert adaptive_ranks(29, 0.2) == (3, 27)
    # A negative state gives the widest finite interval, a state above one a non-inverted one.
    assert adaptive_ranks(28, -3.0) == (1, 28)
    low, high = adaptive_ranks(28, 1.7)
    assert 1 <= low <= high <= 28
    assert adaptive_ranks(9, 0.2) == (1, 9)


def test_gamma_zero_is_the_plain_rolling_conformal_interval():
    forecast, actual = shifted()
    plain = conformal_intervals(forecast, actual, window_days=14)
    adaptive = adaptive_conformal_intervals(forecast, actual, window_days=14, gamma=0.0)
    assert np.array_equal(plain.to_numpy(), adaptive[['lower', 'upper']].to_numpy(), equal_nan=True)
    assert np.allclose(adaptive.alpha_t.dropna(), 0.2)


def test_state_is_alpha_plus_gamma_times_the_errors_of_days_up_to_d_minus_2():
    forecast, actual = shifted()
    gamma = 0.05
    result = adaptive_conformal_intervals(forecast, actual, window_days=14, gamma=gamma)
    local = np.asarray(ev.local_dates(forecast.index))
    slots = np.asarray(local_slots(forecast.index))
    missed = ((actual.to_numpy() < result.lower.to_numpy())
              | (actual.to_numpy() > result.upper.to_numpy())).astype(float)
    missed[result.lower.isna().to_numpy()] = np.nan
    target = sorted(set(local))[40]
    row = np.flatnonzero(local == target)[11]
    earlier = [i for i in np.flatnonzero(slots == slots[row])
               if local[i].toordinal() <= target.toordinal() - 2 and not np.isnan(missed[i])]
    expected = 0.2 + gamma * sum(0.2 - missed[i] for i in earlier)
    assert result.alpha_t.iloc[row] == pytest.approx(expected)
    assert expected != pytest.approx(0.2)


def test_adaptive_interval_of_day_d_ignores_actuals_after_d_minus_2():
    forecast, actual = shifted()
    full = adaptive_conformal_intervals(forecast, actual, window_days=14, gamma=0.05)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    for target in sorted(set(local))[16:]:
        cut = actual.copy()
        cut[local > target - pd.Timedelta(days=2)] = np.nan
        truncated = adaptive_conformal_intervals(forecast, cut, window_days=14, gamma=0.05)
        mask = local == target
        assert np.array_equal(full[mask].to_numpy(), truncated[mask].to_numpy(), equal_nan=True), target


def test_an_adaptive_variant_that_absorbs_d_minus_1_fails_the_truncation_check():
    forecast, actual = shifted()
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    leaky = adaptive_conformal_intervals(forecast, actual, window_days=14, gamma=0.05,
                                         update_lag_days=1)
    failed = 0
    for target in sorted(set(local))[20:50]:
        cut = actual.copy()
        cut[local > target - pd.Timedelta(days=2)] = np.nan
        truncated = adaptive_conformal_intervals(forecast, cut, window_days=14, gamma=0.05,
                                                 update_lag_days=1)
        mask = local == target
        failed += not np.array_equal(leaky[mask].to_numpy(), truncated[mask].to_numpy(),
                                     equal_nan=True)
    assert failed > 0


def test_after_a_level_shift_the_adaptive_interval_covers_more_than_the_plain_one():
    forecast, actual = shifted(days=70, jump_day=35, jump=4_000.0)
    plain = scored_slots(conformal_intervals(forecast, actual, window_days=28), forecast, actual)
    adaptive = scored_slots(adaptive_conformal_intervals(forecast, actual, window_days=28, gamma=0.05),
                            forecast, actual)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    late = local[local >= date(2024, 4, 15)].index
    both = plain.index.intersection(adaptive.index).intersection(late)
    assert adaptive.loc[both].covered.mean() > plain.loc[both].covered.mean()


def test_adaptive_dst_days_get_an_interval_on_every_slot():
    for first, day, n_slots in (('2024-03-10', '2024-03-31', 46), ('2024-10-05', '2024-10-27', 50)):
        forecast, actual = synthetic(first=first, days=30)
        result = adaptive_conformal_intervals(forecast, actual, window_days=14, gamma=0.01)
        local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
        rows = result[local == date.fromisoformat(day)]
        assert len(rows) == n_slots
        assert rows.notna().all().all()


def test_a_missing_actual_gives_no_update_for_its_slot_and_day():
    forecast, actual = synthetic()
    broken = actual.copy()
    broken.iloc[48 * 20 + 5] = np.nan
    result = adaptive_conformal_intervals(forecast, broken, window_days=14, gamma=0.05)
    assert np.isfinite(result.dropna().to_numpy()).all()


def test_coverage_difference_of_identical_frames_is_zero_and_sign_is_correct():
    forecast, actual = shifted(days=70, jump_day=35)
    plain = scored_slots(conformal_intervals(forecast, actual, window_days=28), forecast, actual)
    adaptive = scored_slots(adaptive_conformal_intervals(forecast, actual, window_days=28, gamma=0.05),
                            forecast, actual)
    zero = coverage_difference_bootstrap(plain, plain, n_draws=200)
    assert zero['difference'] == 0.0 and zero['low'] == 0.0 and zero['high'] == 0.0
    gain = coverage_difference_bootstrap(adaptive, plain, n_draws=200)
    both = plain.index.intersection(adaptive.index)
    assert gain['difference'] == pytest.approx(
        adaptive.loc[both].covered.mean() - plain.loc[both].covered.mean())
    assert gain['low'] <= gain['difference'] <= gain['high']


# Seasonal group conformal (EXP-012)


@functools.cache
def _seasonal_data(first: str, days: int):
    """Noise of 1500 MW in winter and 300 MW elsewhere, so that the seasonal pools really differ."""
    forecast, _ = synthetic(first=first, days=days, seed=11)
    months = forecast.index.tz_convert('Europe/Paris').month
    scale = np.where(np.isin(months, (12, 1, 2)), 1500.0, 300.0)
    noise = np.random.default_rng(12).normal(0, 1, len(forecast)) * scale
    return forecast, forecast + noise


def seasonal_data(first: str = '2023-01-01', days: int = 520):
    forecast, actual = _seasonal_data(first, days)
    return forecast.copy(), actual.copy()


def pool_positions(forecast: pd.Series, target: date, slot: int, window: int = 365):
    local = np.asarray(ev.local_dates(forecast.index))
    slots = np.asarray(local_slots(forecast.index))
    season = ev.get_season(target.month)
    last = target - pd.Timedelta(days=2)
    first = target - pd.Timedelta(days=2 + window - 1)
    keep = [first <= d <= last and ev.get_season(d.month) == season for d in local]
    return np.flatnonzero(np.asarray(keep) & (slots == slot))


def test_seasonal_interval_is_the_point_plus_same_season_slot_residual_quantiles():
    forecast, actual = seasonal_data()
    result = seasonal_conformal_intervals(forecast, actual)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    for target, slot in ((date(2024, 4, 10), 7), (date(2024, 5, 20), 30), (date(2024, 1, 15), 0)):
        past = pool_positions(forecast, target, slot)
        low, high = window_quantiles((actual - forecast).to_numpy()[past])
        today = forecast.index[local == target][slot]
        assert result.loc[today, 'lower'] == pytest.approx(forecast[today] + low)
        assert result.loc[today, 'upper'] == pytest.approx(forecast[today] + high)
        assert result.loc[today, 'n_pool'] == len(past)


def test_actuals_of_other_seasons_never_enter_the_pool():
    forecast, actual = seasonal_data()
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    months = pd.Series([d.month for d in local], index=forecast.index)
    base = seasonal_conformal_intervals(forecast, actual)
    broken = actual.copy()
    broken[months.isin((12, 1, 2))] += 50_000.0
    shifted_winter = seasonal_conformal_intervals(forecast, broken)
    spring = (local >= date(2024, 3, 1)) & (local <= date(2024, 4, 15))
    assert np.array_equal(base[spring].to_numpy(), shifted_winter[spring].to_numpy())
    winter = local == date(2024, 1, 15)
    assert not np.array_equal(base[winter].to_numpy(), shifted_winter[winter].to_numpy())


def test_pool_at_a_season_start_holds_the_previous_year_of_that_season_only():
    forecast, actual = seasonal_data()
    result = seasonal_conformal_intervals(forecast, actual)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    first_march = result[local == date(2024, 3, 1)]
    # 1 March 2023 to 28 February 2024 holds exactly the 92 spring days of 2023.
    assert (first_march.n_pool.iloc[[0, 10, 30]] == 92).all()


def test_season_with_too_few_days_in_the_pool_is_undefined_whatever_the_rest_of_the_window():
    forecast, actual = seasonal_data(first='2023-09-01', days=300)
    result = seasonal_conformal_intervals(forecast, actual)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    march = result[local == date(2024, 3, 2)]
    assert march[['lower', 'upper']].isna().all().all()
    assert (march.n_pool == 0).all()
    assert result[local == date(2024, 3, 25)][['lower', 'upper']].notna().all().all()


def test_with_one_season_in_the_window_it_equals_the_plain_rolling_interval():
    forecast, actual = synthetic(first='2024-06-01', days=92)
    seasonal = seasonal_conformal_intervals(forecast, actual, window_days=60)
    plain = conformal_intervals(forecast, actual, window_days=60)
    assert np.array_equal(seasonal[['lower', 'upper']].to_numpy(), plain.to_numpy(), equal_nan=True)


@pytest.mark.parametrize('lag', [2])
def test_seasonal_interval_of_day_d_ignores_actuals_after_d_minus_2(lag):
    forecast, actual = seasonal_data()
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    full = seasonal_conformal_intervals(forecast, actual, update_lag_days=lag)
    # Mid-season days and the first two days of each season, where D-1 and D-2 sit in another season.
    for target in (date(2024, 3, 1), date(2024, 3, 2), date(2024, 3, 3), date(2024, 4, 10),
                   date(2024, 6, 1), date(2024, 6, 2), date(2024, 9, 1), date(2024, 12, 1),
                   date(2024, 1, 2)):
        cut = actual.copy()
        cut[local > target - pd.Timedelta(days=2)] = np.nan
        truncated = seasonal_conformal_intervals(forecast, cut, update_lag_days=lag)
        mask = local == target
        assert np.array_equal(full[mask].to_numpy(), truncated[mask].to_numpy(), equal_nan=True), target


def test_a_seasonal_variant_that_absorbs_d_minus_1_fails_the_truncation_check():
    forecast, actual = seasonal_data()
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    leaky = seasonal_conformal_intervals(forecast, actual, update_lag_days=1)
    target = date(2024, 4, 10)
    cut = actual.copy()
    cut[local > target - pd.Timedelta(days=2)] = np.nan
    truncated = seasonal_conformal_intervals(forecast, cut, update_lag_days=1)
    mask = local == target
    assert not np.array_equal(leaky[mask].to_numpy(), truncated[mask].to_numpy(), equal_nan=True)


@pytest.mark.parametrize(('day', 'n_slots'), [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_seasonal_dst_days_get_an_interval_on_every_slot(day, n_slots):
    forecast, actual = seasonal_data(days=670)
    result = seasonal_conformal_intervals(forecast, actual)
    local = pd.Series(ev.local_dates(forecast.index), index=forecast.index)
    rows = result[local == day]
    assert len(rows) == n_slots
    assert rows[['lower', 'upper']].notna().all().all()
    assert (rows.upper > rows.lower).all()


def test_seasonal_window_rejects_a_non_positive_length():
    forecast, actual = seasonal_data(days=60)
    with pytest.raises(ValueError, match='window_days'):
        seasonal_conformal_intervals(forecast, actual, window_days=0)

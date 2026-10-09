"""Tests of the evaluation harness on synthetic data. Rules in docs/protocol.md."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.cutoff import available_at, delivery_slots, forecast_cutoff


def _frame(day: date, model_err: float = 10.0, rte_err: float = 20.0) -> pd.DataFrame:
    slots = pd.DatetimeIndex(delivery_slots(day, "30min"))
    actual = pd.Series(50_000.0, index=slots)
    return pd.DataFrame(
        {ev.ACTUAL: actual, ev.MODEL: actual + model_err, ev.RTE: actual - rte_err}, index=slots
    )


# 1. Metrics on common slots


def test_metrics_values():
    index = pd.date_range("2024-01-10", periods=4, freq="30min", tz="UTC")
    frame = pd.DataFrame(
        {
            ev.ACTUAL: [100.0, 200.0, 100.0, 200.0],
            ev.MODEL: [110.0, 190.0, 100.0, 200.0],
            ev.RTE: [90.0, 200.0, 120.0, 200.0],
        },
        index=index,
    )
    result = ev.score(frame)
    assert result.model.mae == pytest.approx(5.0)
    assert result.model.rmse == pytest.approx(np.sqrt(50.0))
    assert result.model.mape == pytest.approx(100 * np.mean([0.1, 0.05, 0, 0]))
    assert result.rte.mae == pytest.approx(7.5)
    assert result.n_excluded == 0


def test_missing_values_excluded_for_both_and_counted():
    frame = _frame(date(2024, 1, 10))
    frame.iloc[3, frame.columns.get_loc(ev.RTE)] = np.nan
    frame.iloc[5, frame.columns.get_loc(ev.MODEL)] = np.nan
    frame.iloc[7, frame.columns.get_loc(ev.ACTUAL)] = np.nan
    result = ev.score(frame)
    assert result.n_excluded == 3
    assert result.model.n_slots == result.rte.n_slots == 45
    assert list(result.excluded) == [frame.index[3], frame.index[5], frame.index[7]]


def test_score_rejects_duplicate_slot_timestamps():
    frame = _frame(date(2024, 1, 10))
    frame = pd.concat([frame, frame.iloc[:1]])
    with pytest.raises(ValueError, match="Duplicate"):
        ev.score(frame)


def test_score_requires_utc_index():
    frame = _frame(date(2024, 1, 10))
    with pytest.raises(ValueError):
        ev.score(frame.tz_convert("Europe/Paris"))
    with pytest.raises(ValueError):
        ev.score(frame.tz_localize(None))


def test_mape_rejects_non_positive_actual():
    frame = _frame(date(2024, 1, 10))
    frame[ev.ACTUAL] = 0.0
    with pytest.raises(ValueError):
        ev.score(frame)


@pytest.mark.parametrize(
    ("day", "slots"), [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50), (date(2024, 6, 5), 48)]
)
def test_daily_mae_slot_counts_on_dst_days(day, slots):
    daily = ev.daily_mae(_frame(day))
    assert list(daily.index) == [day]
    assert daily.loc[day, "n_slots"] == slots
    assert daily.loc[day, ev.MODEL] == pytest.approx(10.0)
    assert daily.loc[day, ev.RTE] == pytest.approx(20.0)


def test_daily_mae_uses_local_date():
    # 23:00 UTC in winter is 00:00 local on the next day
    index = pd.DatetimeIndex(["2024-01-10 22:30", "2024-01-10 23:00"], tz="UTC")
    frame = pd.DataFrame(
        {ev.ACTUAL: [1.0, 1.0], ev.MODEL: [2.0, 3.0], ev.RTE: [1.0, 1.0]}, index=index
    )
    daily = ev.daily_mae(frame)
    assert list(daily.index) == [date(2024, 1, 10), date(2024, 1, 11)]
    assert list(daily[ev.MODEL]) == [1.0, 2.0]


# 2. Day types


def test_holidays_2025():
    assert ev.holidays(2025) == {
        date(2025, 1, 1),
        date(2025, 4, 21),
        date(2025, 5, 1),
        date(2025, 5, 8),
        date(2025, 5, 29),
        date(2025, 6, 9),
        date(2025, 7, 14),
        date(2025, 8, 15),
        date(2025, 11, 1),
        date(2025, 11, 11),
        date(2025, 12, 25),
    }


def test_easter_2024_and_2026():
    assert date(2024, 4, 1) in ev.holidays(2024)
    assert date(2026, 4, 6) in ev.holidays(2026)


def test_bridges_2025_match_protocol():
    year = [date(2025, 1, 1) + timedelta(days=i) for i in range(365)]
    assert [d for d in year if ev.is_bridge(d)] == [
        date(2025, 5, 2),
        date(2025, 5, 9),
        date(2025, 5, 30),
        date(2025, 11, 10),
        date(2025, 12, 26),
    ]


def test_dst_days():
    assert ev.local_day_hours(date(2024, 3, 31)) == 23
    assert ev.local_day_hours(date(2024, 10, 27)) == 25
    assert ev.local_day_hours(date(2024, 6, 5)) == 24
    assert ev.is_dst_day(date(2025, 3, 30)) and ev.is_dst_day(date(2025, 10, 26))


def test_day_type_precedence():
    assert ev.day_type(date(2024, 3, 31), cold=True) == "dst"
    assert ev.day_type(date(2025, 5, 1), cold=True) == "holiday"
    assert ev.day_type(date(2025, 5, 2), cold=True) == "bridge"
    assert ev.day_type(date(2025, 1, 15), cold=True) == "cold"
    assert ev.day_type(date(2025, 1, 15)) == "ordinary"
    assert ev.day_type(date(2025, 1, 18)) == "ordinary"  # weekends are ordinary


def test_is_cold_takes_threshold_as_parameter():
    assert ev.is_cold(1.0, 2.0)
    assert not ev.is_cold(2.0, 2.0)
    assert not ev.is_cold(1.0, 0.0)


def test_mae_by_day_type_and_cold_flag():
    days = [
        date(2024, 3, 31),
        date(2024, 5, 1),
        date(2024, 5, 10),
        date(2024, 1, 10),
        date(2024, 1, 11),
    ]
    parts = [_frame(d, model_err=10.0 + i) for i, d in enumerate(days)]
    frame = pd.concat(parts)
    cold = {date(2024, 5, 1): True, date(2024, 1, 11): True}
    table = ev.mae_by_day_type(frame, cold)
    assert table.loc["dst", "n_days"] == 1
    assert table.loc["holiday", ev.MODEL] == pytest.approx(11.0)
    assert table.loc["bridge", ev.MODEL] == pytest.approx(12.0)  # 2024-05-10 follows Ascension
    assert table.loc["ordinary", "n_days"] == 1
    assert table.loc["cold", "n_days"] == 1  # the cold holiday is a holiday here
    assert table.loc["cold_any", "n_days"] == 2
    assert table.loc["cold_any", ev.MODEL] == pytest.approx(12.5)


def test_mae_by_day_type_is_slot_weighted_not_mean_of_daily_maes():
    short = _frame(date(2024, 1, 10), model_err=10.0).iloc[:46]  # 46 slots, MAE 10
    frame = pd.concat([short, _frame(date(2024, 1, 11), model_err=40.0)])
    cold = {date(2024, 1, 10): True, date(2024, 1, 11): True}
    table = ev.mae_by_day_type(frame, cold)
    n_short, n_long = 46, 48
    expected = (46 * 10.0 + 48 * 40.0) / (n_short + n_long)
    assert table.loc["cold", "n_slots"] == n_short + n_long
    assert table.loc["cold", ev.MODEL] == pytest.approx(expected)
    assert table.loc["cold", ev.MODEL] != pytest.approx((10.0 + 40.0) / 2)


def test_mae_by_day_type_weights_a_25_hour_day_by_its_50_slots():
    dst = _frame(date(2024, 10, 27), model_err=40.0)
    ordinary = _frame(date(2024, 10, 28), model_err=10.0).iloc[:46]
    assert len(dst) == 50
    cold = {date(2024, 10, 27): True, date(2024, 10, 28): True}
    table = ev.mae_by_day_type(pd.concat([dst, ordinary]), cold)
    expected = (50 * 40.0 + 46 * 10.0) / 96
    assert table.loc["cold_any", ev.MODEL] == pytest.approx(expected)
    assert table.loc["cold_any", ev.MODEL] != pytest.approx(25.0)
    assert table.loc["dst", "n_slots"] == 50


# 3. Block bootstrap


def test_bootstrap_interval_contains_zero_for_noise():
    noise = np.random.default_rng(1).normal(0.0, 50.0, 366)
    result = ev.block_bootstrap(noise - noise.mean() + 0.5)  # sample mean close to 0
    assert result.low < 0 < result.high
    assert result.verdict == "not significant"


def test_bootstrap_interval_excludes_zero_for_clear_gain():
    rng = np.random.default_rng(2)
    result = ev.block_bootstrap(rng.normal(-40.0, 50.0, 366))
    assert result.high < 0
    assert result.significant and result.verdict == "significant"
    assert result.mean < 0


def test_bootstrap_is_reproducible_and_seed_dependent():
    d = np.random.default_rng(3).normal(0.0, 1.0, 100)
    assert ev.block_bootstrap(d) == ev.block_bootstrap(d)
    assert ev.block_bootstrap(d, seed=1) != ev.block_bootstrap(d, seed=2)


def test_bootstrap_defaults_follow_d10():
    result = ev.block_bootstrap(np.arange(30, dtype=float))
    assert (result.block_days, result.n_draws) == (7, 10_000)


def test_bootstrap_constant_difference_has_degenerate_interval():
    result = ev.block_bootstrap(np.full(50, -3.0))
    assert result.low == pytest.approx(-3.0) and result.high == pytest.approx(-3.0)


def test_bootstrap_blocks_keep_autocorrelation():
    # a slow wave: iid resampling would give a narrower interval than 7-day blocks
    wave = 100.0 * np.sin(np.arange(364) * 2 * np.pi / 28)
    iid = ev.block_bootstrap(wave, block_days=1)
    blocked = ev.block_bootstrap(wave, block_days=7)
    assert (blocked.high - blocked.low) > (iid.high - iid.low)


def test_bootstrap_rejects_short_or_nearly_empty_input():
    with pytest.raises(ValueError):
        ev.block_bootstrap(np.arange(5, dtype=float))
    with pytest.raises(ValueError):
        ev.block_bootstrap(np.array([1.0] + [np.nan] * 20))


def test_bootstrap_rejects_series_with_gaps_duplicates_or_disorder():
    days = pd.date_range("2024-01-01", periods=30, freq="D")
    d = pd.Series(np.random.default_rng(0).normal(size=30), index=days)
    ev.block_bootstrap(d)  # a clean calendar is accepted
    with pytest.raises(ValueError, match="gaps"):
        ev.block_bootstrap(d.drop(days[10]))
    with pytest.raises(ValueError, match="unique and sorted"):
        ev.block_bootstrap(d.iloc[::-1])
    duplicated = pd.concat([d, d.iloc[:1]])
    with pytest.raises(ValueError, match="unique and sorted"):
        ev.block_bootstrap(duplicated)


def _manual_calendar_bootstrap(d: np.ndarray, block_days: int) -> tuple[float, float]:
    rng = np.random.default_rng(ev.BOOTSTRAP_SEED)
    n = len(d)
    n_blocks = -(-n // block_days)
    starts = rng.integers(0, n - block_days + 1, size=(ev.BOOTSTRAP_DRAWS, n_blocks))
    means = []
    for row in starts:
        drawn = np.concatenate([d[s : s + block_days] for s in row])[:n]
        means.append(np.nanmean(drawn))
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def test_missing_day_stays_in_the_calendar_and_is_not_compressed():
    days = pd.date_range("2024-01-01", periods=35, freq="D")
    rng = np.random.default_rng(5)
    model = pd.Series(100.0 + rng.normal(0, 5, 35), index=days)
    reference = pd.Series(110.0 + rng.normal(0, 5, 35), index=days)
    gap = days[7]  # 2024-01-08

    as_nan = model.copy()
    as_nan[gap] = np.nan
    dropped = model.drop(gap)

    result_nan = ev.compare_daily(as_nan, reference, last=days[-1])
    result_dropped = ev.compare_daily(dropped, reference, last=days[-1])
    assert result_nan == result_dropped  # NaN and absent are the same missing day
    assert (result_nan.n_days, result_nan.n_missing) == (34, 1)

    diff = (as_nan - reference).to_numpy()
    low, high = _manual_calendar_bootstrap(diff, 7)
    assert (result_nan.low, result_nan.high) == (pytest.approx(low), pytest.approx(high))

    compressed = ev.block_bootstrap(diff[~np.isnan(diff)])  # what the rejected code did
    assert (compressed.low, compressed.high) != (pytest.approx(low), pytest.approx(high))


def test_blocks_never_bridge_a_missing_day():
    # 14 days of 0 then 14 days of 100, one missing day in the middle of the first half.
    # A compressed series has windows mixing both levels at different places than the calendar.
    days = pd.date_range("2024-01-01", periods=28, freq="D")
    d = pd.Series([0.0] * 14 + [100.0] * 14, index=days)
    d[days[6]] = np.nan
    calendar = ev.block_bootstrap(d, block_days=7)
    compressed = ev.block_bootstrap(d.dropna().to_numpy(), block_days=7)
    assert calendar.n_missing == 1 and compressed.n_missing == 0
    assert (calendar.low, calendar.high) != (compressed.low, compressed.high)


def test_compare_daily_counts_missing_days_at_the_edges():
    days = pd.date_range("2024-01-02", periods=30, freq="D")
    rng = np.random.default_rng(6)
    model = pd.Series(rng.normal(0, 1, 30), index=days)
    reference = pd.Series(rng.normal(0, 1, 30), index=days)
    result = ev.compare_daily(model, reference, first=date(2024, 1, 1), last=date(2024, 2, 3))
    assert result.n_missing == 1 + 3  # 2024-01-01 and 2024-02-01 to 02-03
    assert result.n_days == 30


def test_compare_daily_rejects_duplicate_days():
    days = pd.date_range("2024-01-01", periods=20, freq="D")
    series = pd.Series(np.arange(20.0), index=days)
    with pytest.raises(ValueError):
        ev.compare_daily(pd.concat([series, series.iloc[:1]]), series)


def test_compare_daily_pairs_by_date_and_checks_14_days():
    days = pd.Index([date(2024, 1, 1) + timedelta(days=i) for i in range(60)])
    rng = np.random.default_rng(4)
    model = pd.Series(100.0 + rng.normal(0, 5, 60), index=days)
    reference = pd.Series(120.0 + rng.normal(0, 5, 60), index=days).iloc[
        ::-1
    ]  # order must not matter
    main, check = ev.compare_with_check(model, reference)
    assert (main.block_days, check.block_days) == (7, 14)
    assert main.n_days == 60
    assert main.mean == pytest.approx(float((model - reference.sort_index()).mean()))
    assert main.significant and main.mean < 0


# 4. Seasonal naive baseline


def _history(first: date, days: int) -> pd.Series:
    index = pd.date_range(
        datetime(first.year, first.month, first.day, tzinfo=UTC), periods=48 * days, freq="30min"
    )
    return pd.Series(np.arange(len(index), dtype=float), index=index)


def test_seasonal_naive_is_value_seven_days_earlier():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    forecast = ev.seasonal_naive(history, day)
    assert len(forecast) == 48
    expected = history.reindex(forecast.index - timedelta(days=7))
    np.testing.assert_array_equal(forecast.to_numpy(), expected.to_numpy())
    assert not forecast.isna().any()


@pytest.mark.parametrize(("day", "slots"), [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_seasonal_naive_on_dst_days_has_local_day_length(day, slots):
    forecast = ev.seasonal_naive(_history(date(2024, 2, 1), 300), day)
    assert len(forecast) == slots
    assert not forecast.isna().any()


def test_seasonal_naive_ignores_everything_after_the_usable_limit():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    limit = ev.last_usable_slot_start(day)
    assert limit + timedelta(minutes=30) == forecast_cutoff(day) - timedelta(hours=2)
    poisoned = history.copy()
    poisoned[poisoned.index > limit] = 1e9
    truncated = history[history.index <= limit]
    full = ev.seasonal_naive(history, day)
    pd.testing.assert_series_equal(ev.seasonal_naive(poisoned, day), full)
    pd.testing.assert_series_equal(ev.seasonal_naive(truncated, day), full)


def test_seasonal_naive_source_slots_are_published_before_cutoff():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    forecast = ev.seasonal_naive(history, day)
    sources = forecast.index - timedelta(days=7)
    published = pd.DataFrame({"published_at": sources + timedelta(hours=2)})
    kept = available_at(published, forecast_cutoff(day))
    assert len(kept) == len(published)


def test_seasonal_naive_with_short_lag_returns_nan_instead_of_leaking():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    forecast = ev.seasonal_naive(history, day, lag_days=1)
    local_evening = forecast.index[-1]
    assert np.isnan(forecast[local_evening])  # D-1 23:30 is after the cutoff
    assert forecast.isna().sum() > 0
    assert ev.seasonal_naive(history, day, lag_days=2).notna().all()


def test_seasonal_naive_does_not_fill_missing_source_slots():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    hole = forecast_cutoff(day).replace(hour=0, minute=0) - timedelta(days=6)
    history = history.drop(history.index[history.index == pd.Timestamp(hole)])
    forecast = ev.seasonal_naive(history, day)
    assert forecast.isna().sum() == 1


def _published_after(history: pd.Series, delay: timedelta = timedelta(hours=2)) -> pd.Series:
    return pd.Series(history.index + delay, index=history.index)


def test_publication_times_before_the_cutoff_change_nothing():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    plain = ev.seasonal_naive(history, day)
    aware = ev.seasonal_naive(history, day, published_at=_published_after(history))
    pd.testing.assert_series_equal(aware, plain)


def test_late_publication_removes_the_value_exactly_at_or_after_the_cutoff():
    day = date(2024, 6, 12)
    cutoff = forecast_cutoff(day)
    history = _history(date(2024, 5, 1), 60)
    published = _published_after(history)
    sources = ev.seasonal_naive(history, day).index - timedelta(days=7)
    published[sources[0]] = pd.Timestamp(cutoff) - pd.Timedelta(seconds=1)  # before: kept
    published[sources[1]] = pd.Timestamp(cutoff)  # exactly at the cutoff: excluded
    published[sources[2]] = pd.Timestamp(cutoff) + pd.Timedelta(seconds=1)  # after: excluded
    plain = ev.seasonal_naive(history, day)
    aware = ev.seasonal_naive(history, day, published_at=published)
    assert aware.iloc[0] == plain.iloc[0]
    assert aware.iloc[1:3].isna().all()
    pd.testing.assert_series_equal(aware.iloc[3:], plain.iloc[3:])


def test_publication_times_never_add_values_beyond_the_d2_limit():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    published = _published_after(history, timedelta(0))  # claims instant publication
    short = ev.seasonal_naive(history, day, lag_days=1, published_at=published)
    plain = ev.seasonal_naive(history, day, lag_days=1)
    pd.testing.assert_series_equal(short, plain)
    assert short.isna().sum() > 0


def test_missing_or_naive_publication_times_are_not_trusted():
    day = date(2024, 6, 12)
    history = _history(date(2024, 5, 1), 60)
    published = _published_after(history)
    source = ev.seasonal_naive(history, day).index[0] - timedelta(days=7)
    published[source] = pd.NaT
    forecast = ev.seasonal_naive(history, day, published_at=published)
    assert np.isnan(forecast.iloc[0]) and forecast.iloc[1:].notna().all()
    naive = pd.Series(published.dt.tz_localize(None).to_numpy(), index=history.index)
    with pytest.raises(ValueError, match="timezone-aware"):
        ev.seasonal_naive(history, day, published_at=naive)


def test_get_season_valid():
    assert ev.get_season(1) == "winter"
    assert ev.get_season(2) == "winter"
    assert ev.get_season(12) == "winter"
    assert ev.get_season(3) == "spring"
    assert ev.get_season(4) == "spring"
    assert ev.get_season(5) == "spring"
    assert ev.get_season(6) == "summer"
    assert ev.get_season(7) == "summer"
    assert ev.get_season(8) == "summer"
    assert ev.get_season(9) == "autumn"
    assert ev.get_season(10) == "autumn"
    assert ev.get_season(11) == "autumn"


def test_get_season_invalid():
    with pytest.raises(ValueError, match="Month must be between 1 and 12"):
        ev.get_season(0)
    with pytest.raises(ValueError, match="Month must be between 1 and 12"):
        ev.get_season(13)


def test_public_metrics_helper():
    idx = pd.date_range("2024-01-10", periods=4, freq="30min", tz="UTC")
    actual = pd.Series([100.0, 200.0, 100.0, 200.0], index=idx)
    forecast = pd.Series([110.0, 190.0, 100.0, 200.0], index=idx)

    res = ev.metrics(actual, forecast)
    assert res.mae == pytest.approx(5.0)
    assert res.rmse == pytest.approx(np.sqrt(50.0))
    assert res.mape == pytest.approx(100 * np.mean([0.1, 0.05, 0, 0]))
    assert res.n_slots == 4

    # Non-Series raises TypeError
    with pytest.raises(TypeError):
        ev.metrics([100.0], [110.0])

    # Naive index raises ValueError
    naive_act = pd.Series([100.0], index=pd.date_range("2024-01-10", periods=1))
    naive_fc = pd.Series([110.0], index=pd.date_range("2024-01-10", periods=1))
    with pytest.raises(ValueError):
        ev.metrics(naive_act, naive_fc)

    # Missing values handling
    act_nan = pd.Series([100.0, np.nan, 200.0], index=pd.date_range("2024-01-10", periods=3, tz="UTC"))
    fc_nan = pd.Series([110.0, 190.0, np.nan], index=pd.date_range("2024-01-10", periods=3, tz="UTC"))
    res_nan = ev.metrics(act_nan, fc_nan)
    assert res_nan.n_slots == 1
    assert res_nan.mae == 10.0

    # Duplicate timestamps raise ValueError
    dup_idx = pd.DatetimeIndex([pd.Timestamp("2024-01-10 00:00", tz="UTC"), pd.Timestamp("2024-01-10 00:00", tz="UTC")])
    dup_act = pd.Series([100.0, 200.0], index=dup_idx)
    dup_fc = pd.Series([110.0, 190.0], index=dup_idx)
    with pytest.raises(ValueError, match="duplicate"):
        ev.metrics(dup_act, dup_fc)



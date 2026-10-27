"""Scoring and drift monitoring rules of docs/production_design.md sections 4.2 and 5."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast import monitoring as mon
from load_forecast.cutoff import delivery_slots

DAY = date(2024, 4, 3)


def slots(day=DAY):
    return pd.DatetimeIndex(delivery_slots(day, "30min"))


def days(n, first="2024-01-01"):
    return pd.date_range(first, periods=n, freq="D")


def forecast(point, lower=np.nan, upper=np.nan):
    index = point.index
    return pd.DataFrame({"point": point, "lower": lower, "upper": upper}, index=index)


def test_score_day_uses_the_same_slots_for_every_forecast():
    index = slots()
    actual = pd.Series(50_000.0, index=index)
    actual.iloc[0] = np.nan
    model_point = actual.fillna(50_000.0) + 100.0
    model_point.iloc[1] = np.nan
    served = forecast(actual.fillna(50_000.0) - 200.0)
    model = forecast(model_point, model_point - 150.0, model_point + 150.0)
    model.loc[index[2:4], "lower"] = 50_200.0
    rte = pd.Series(51_000.0, index=index)
    rte_star = pd.Series(50_500.0, index=index)
    result = mon.score_day(actual, rte, served, model, rte_star, "consolidated")
    assert result["n_common_slots"] == 46
    assert result["served"] == {"n_slots": 46, "mae": 200.0, "rmse": 200.0, "mape": pytest.approx(0.4),
                                "mean_error": -200.0, "coverage": None, "width": None}
    assert result["model"]["mean_error"] == 100.0
    assert result["model"]["coverage"] == pytest.approx(44 / 46)
    assert result["rte_d1"]["mae"] == 1000.0 and result["rte_d1"]["n_slots"] == 46
    assert result["rte_star"]["mean_error"] == 500.0
    assert "coverage" not in result["rte_d1"]


def test_score_day_vintage_rules_and_missing_parts():
    index = slots()
    actual = pd.Series(50_000.0, index=index)
    rte = pd.Series(50_400.0, index=index)
    served = forecast(actual + 10.0)
    with pytest.raises(ValueError, match="real-time"):
        mon.score_day(actual, rte, served, None, rte, "realtime")
    with pytest.raises(ValueError, match="vintage"):
        mon.score_day(actual, rte, served, None, None, "final")
    missing_star = pd.Series(np.nan, index=index)
    result = mon.score_day(actual, rte, served, None, missing_star, "consolidated")
    assert result["rte_star"] is None and result["model"] is None
    assert result["served"]["mae"] == 10.0
    no_delivery = mon.score_day(actual, rte, None, forecast(actual), None, "realtime")
    assert no_delivery["served"] is None and no_delivery["model"]["mae"] == 0.0
    with pytest.raises(ValueError, match="UTC"):
        mon.score_day(actual.tz_convert("Europe/Paris"), rte.tz_convert("Europe/Paris"),
                      None, None, None, "realtime")


def test_score_row_and_daily_error_missing_rules():
    index = slots()
    actual = pd.Series(50_000.0, index=index)
    metrics = mon.score_day(actual, actual, forecast(actual + 300.0), None, None, "realtime")
    row = mon.score_row(DAY, "hash", metrics, "2024-04-04T07:00:00+00:00",
                        "2024-04-04T07:05:00+00:00")
    assert row["actual_vintage"] == "realtime" and row["delivery_day"] == "2024-04-03"
    assert mon.daily_error(metrics) == -300.0
    short = mon.score_day(actual.iloc[:39], actual.iloc[:39], forecast(actual.iloc[:39]),
                          None, None, "realtime")
    assert np.isnan(mon.daily_error(short))
    exactly = mon.score_day(actual.iloc[:40], actual.iloc[:40], forecast(actual.iloc[:40]),
                            None, None, "realtime")
    assert mon.daily_error(exactly) == 0.0
    assert np.isnan(mon.daily_error({"served": None}))


def test_cusum_recurrences_start_from_zero_and_reset_after_an_alarm():
    z = pd.Series([1.0, 2.0, -0.5, 3.0, 0.0, -2.0, -2.0], index=days(7))
    out = mon.cusum(z, h=4.0, k=0.5)
    assert out["s_plus"].tolist() == [0.5, 2.0, 1.0, 3.5, 3.0, 0.5, 0.0]
    assert out["s_minus"].tolist() == [0.0, 0.0, 0.0, 0.0, 0.0, 1.5, 3.0]
    assert not out["alarm"].any()
    alarm = mon.cusum(pd.Series([3.0, 3.0, 1.0, -5.0, -1.0], index=days(5)), h=4.0)
    assert alarm["s_plus"].tolist() == [2.5, 5.0, 0.5, 0.0, 0.0]
    assert alarm["s_minus"].tolist() == [0.0, 0.0, 0.0, 4.5, 0.5]
    assert alarm["alarm"].tolist() == [False, True, False, True, False]
    assert alarm["event"].tolist() == ["", "alarm_up", "", "alarm_down", ""]


def test_cusum_missing_days_carry_over_then_restart_after_seven():
    seven = [2.0, 2.0] + [np.nan] * 7 + [2.0]
    out = mon.cusum(pd.Series(seven, index=days(10)), h=100.0)
    assert out["s_plus"].tolist() == [1.5, 3.0] + [3.0] * 7 + [4.5]
    assert out["missing"].sum() == 7 and out["missing_run"].max() == 7
    assert (out["event"] == "").all()
    eight = [2.0, 2.0] + [np.nan] * 8 + [2.0]
    out = mon.cusum(pd.Series(eight, index=days(11)), h=100.0)
    assert out["s_plus"].tolist() == [1.5, 3.0] + [3.0] * 7 + [0.0, 1.5]
    assert out["event"].tolist()[9] == "restart_missing"
    above = mon.cusum(pd.Series([5.0, np.nan], index=days(2)), h=4.0)
    assert above["alarm"].tolist() == [True, False]
    with pytest.raises(ValueError, match="gaps"):
        mon.cusum(pd.Series([1.0, 1.0], index=pd.DatetimeIndex(["2024-01-01", "2024-01-03"])))


@pytest.mark.parametrize("direction", [1.0, -1.0])
def test_one_sigma_shift_in_mw_is_detected_in_every_season(direction):
    rng = np.random.default_rng(7)
    calendar = days(731, "2024-01-01")
    seasons = [mon.season_of(day.date()) for day in calendar]
    mu = np.array([mon.SEASON_MEAN[s] for s in seasons])
    sigma = np.array([mon.SEASON_STD[s] for s in seasons])
    errors = mu + sigma * rng.standard_normal(len(calendar))
    in_control = mon.cusum(mon.standardize(pd.Series(errors, index=calendar)))
    assert not in_control["alarm"].any()
    for start in (0, 120, 240, 400, 600):
        shifted = errors.copy()
        shifted[start:] += direction * sigma[start:]
        out = mon.cusum(mon.standardize(pd.Series(shifted, index=calendar)))
        alarms = np.flatnonzero(out["alarm"].to_numpy())
        assert alarms.size and start <= alarms[0] <= start + 60
        assert out["event"].iloc[alarms[0]] == ("alarm_up" if direction > 0 else "alarm_down")


def test_vectorised_cusum_matches_the_daily_rule():
    rng = np.random.default_rng(3)
    paths = rng.standard_normal((50, 200)) + 0.3
    maxima = mon.cusum_maxima(paths)
    alarms = mon.first_alarm(paths, 6.0)
    for row in range(50):
        z = pd.Series(paths[row], index=days(200))
        free = mon.cusum(z, h=np.inf)
        assert maxima[row] == pytest.approx(free[["s_plus", "s_minus"]].to_numpy().max())
        rule = np.flatnonzero(mon.cusum(z, h=6.0)["alarm"].to_numpy())
        assert alarms[row] == (rule[0] + 1 if rule.size else 0)


def test_smallest_h_meets_the_target_on_a_grid():
    maxima = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert mon.smallest_h(maxima, 0.2) == 4.0
    assert mon.smallest_h(maxima, 0.4) == 3.0
    h = mon.smallest_h(np.random.default_rng(1).exponential(size=10_000), 0.2)
    sample = np.random.default_rng(1).exponential(size=10_000)
    assert np.mean(sample > h) <= 0.2 < np.mean(sample > h - 0.01)


def test_calendar_positions_put_29_february_with_28_february():
    stamps = pd.DatetimeIndex(["2024-01-01", "2024-02-28", "2024-02-29", "2024-03-01",
                               "2024-12-31", "2025-03-01", "2025-12-31"])
    assert mon.calendar_positions(stamps).tolist() == [0, 58, 58, 59, 364, 59, 364]


@pytest.mark.parametrize("block", [7, 28])
def test_block_sampler_draws_consecutive_days_near_the_target_position(block):
    calendar = days(731, "2024-01-01")
    z = pd.Series(np.arange(731, dtype=float), index=calendar)
    sampler = mon.BlockSampler.build(z, block)
    positions = mon.calendar_positions(calendar)
    paths = sampler.years(300, np.random.default_rng(0))
    assert paths.shape == (300, 365)
    for path in paths:
        starts = path[::block].astype(int)
        for j, start in enumerate(starts):
            chunk = path[j * block:(j + 1) * block]
            assert (chunk == start + np.arange(len(chunk))).all()
        targets = (np.arange(365)[:, None] + block * np.arange(len(starts))) % 365
        gap = np.abs(positions[starts][None, :] - targets) % 365
        near = np.minimum(gap, 365 - gap) <= 15
        assert near.all(axis=1).any()
    z.iloc[3] = np.nan
    with pytest.raises(ValueError, match="missing"):
        mon.BlockSampler.build(z, block)


def test_autocorrelation_and_bartlett_variance():
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(20_001)
    ma = pd.Series(noise[1:] + noise[:-1], index=days(20_000))
    acf = mon.autocorrelation(ma, 3)
    assert acf[0] == 1.0 and acf[1] == pytest.approx(0.5, abs=0.03)
    assert abs(acf[2]) < 0.03
    assert mon.bartlett_variance(ma, 1) == pytest.approx(ma.var(ddof=0))
    assert mon.bartlett_variance(ma, 50) == pytest.approx(4.0, rel=0.1)


def test_relative_skill_band_needs_enough_days_and_a_positive_mean():
    calendar = days(40)
    rte = pd.Series(1000.0, index=calendar)
    served = pd.Series(900.0, index=calendar)
    served.iloc[20:] = 1200.0
    out = mon.relative_skill(served, rte)
    assert not out["alert"].iloc[:29].any()
    first = int(np.flatnonzero(out["alert"].to_numpy())[0])
    assert out["mean_difference"].iloc[first] > 0 >= out["mean_difference"].iloc[first - 1]
    sparse = served.copy()
    sparse.iloc[::2] = np.nan
    assert not mon.relative_skill(sparse, rte)["alert"].any()

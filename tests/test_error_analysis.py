from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast import error_analysis as ea
from load_forecast.evaluation import holidays


def slots(day: str, n: int = 48) -> pd.DatetimeIndex:
    start = pd.Timestamp(day, tz='Europe/Paris').tz_convert('UTC')
    return pd.date_range(start, periods=n, freq='30min')


def frame_for(index: pd.DatetimeIndex, model: float, rte: float, star: float,
              covered: bool = True) -> pd.DataFrame:
    return pd.DataFrame({'model': model, 'actual': 100.0, 'rte_d1': rte, 'rte_star': star,
                         'lower': 90.0, 'upper': 110.0, 'covered': covered, 'width': 20.0,
                         'n_pool': 90}, index=index)


def test_holiday_names_cover_the_eleven_holidays() -> None:
    names = {ea.holiday_name(d) for year in (2024, 2025) for d in holidays(year)}
    assert '' not in names
    assert len(names) == 11
    assert ea.holiday_name(date(2024, 4, 1)) == 'Easter Monday'
    assert ea.holiday_name(date(2025, 5, 29)) == 'Ascension'
    assert ea.holiday_name(date(2025, 6, 9)) == 'Whit Monday'
    assert ea.holiday_name(date(2025, 5, 1)) == 'Labour Day'
    assert ea.holiday_name(date(2025, 5, 2)) == ''


def test_daily_table_uses_common_slots_and_the_local_day() -> None:
    day = frame_for(slots('2025-03-30', 46), 110.0, 130.0, 105.0)
    other = frame_for(slots('2025-04-01'), 98.0, 100.0, 100.0, covered=False)
    frame = pd.concat([day, other])
    frame.loc[frame.index[10], 'rte_star'] = np.nan
    table = ea.daily_table(frame, cold={date(2025, 4, 1): True}, temperature={date(2025, 4, 1): 3.0})
    assert table.loc[date(2025, 3, 30), 'slots'] == 45
    assert table.loc[date(2025, 3, 30), 'model'] == pytest.approx(10.0)
    assert table.loc[date(2025, 3, 30), 'rte_d1'] == pytest.approx(30.0)
    assert table.loc[date(2025, 3, 30), 'type'] == 'dst'
    assert table.loc[date(2025, 4, 1), 'bias'] == pytest.approx(-2.0)
    assert table.loc[date(2025, 4, 1), 'coverage'] == 0.0
    assert table.loc[date(2025, 4, 1), 'type'] == 'cold'
    assert table.loc[date(2025, 4, 1), 'type_d2'] == 'dst'
    assert np.isnan(table.loc[date(2025, 3, 30), 'temperature'])


def test_worst_days_share_sums_over_the_year() -> None:
    daily = pd.DataFrame({'model': [100.0, 300.0, 200.0], 'slots': [48, 48, 48]},
                         index=[date(2025, 1, d) for d in (1, 2, 3)])
    top = ea.worst_days(daily, 2)
    assert list(top.index) == [date(2025, 1, 2), date(2025, 1, 3)]
    assert top.share_of_abs_error.sum() == pytest.approx(500 / 600)


def test_profile_groups_by_paris_hour() -> None:
    index = slots('2025-07-01')
    frame = frame_for(index, 101.0, 100.0, 100.0)
    frame.loc[index[:2], 'model'] = 110.0
    hourly = ea.profile(frame, 'hour')
    assert hourly.loc[0, 'model'] == pytest.approx(10.0)
    assert hourly.loc[1, 'model'] == pytest.approx(1.0)
    assert ea.profile(frame, 'month').loc[7, 'rte_d1'] == pytest.approx(0.0)


def test_holidays_in_pool_counts_same_season_holidays_ending_at_d_minus_2() -> None:
    # Pool of 2025-05-01: 2024-05-01 to 2025-04-29, spring days only.
    assert ea.holidays_in_pool(date(2025, 5, 1)) == 5  # May 1, May 8, Ascension, Whit Monday 2024, Easter Monday 2025
    # Winter 2025-01-01: window 2024-01-01 to 2024-12-30 keeps Jan 1 and Dec 25 of 2024.
    assert ea.holidays_in_pool(date(2025, 1, 1)) == 2
    # The pool stops two days before: Dec 25 2024 is not in the pool of 2024-12-26's neighbours.
    assert ea.holidays_in_pool(date(2024, 12, 26)) == 1  # Jan 1 2024; Dec 25 2024 is D-1, excluded
    assert ea.holidays_in_pool(date(2024, 12, 27)) == 2


def test_tail_rank_matches_the_conformal_ranks() -> None:
    assert ea.tail_rank(90) == 9
    assert ea.tail_rank(365) == 36


def test_temperature_bins_and_boundary_coverage() -> None:
    daily = pd.DataFrame({'model': [1.0, 3.0, 5.0], 'rte_star': 1.0, 'rte_d1': 1.0, 'bias': 0.0,
                          'coverage': [1.0, 0.0, 0.5], 'temperature': [-1.0, 3.0, np.nan]})
    bins = ea.temperature_bins(daily, (-np.inf, 0.0, 5.0, np.inf))
    assert bins.days.sum() == 2
    assert bins.model.dropna().tolist() == [1.0, 3.0]
    inside = frame_for(slots('2025-06-05'), 100.0, 100.0, 100.0, covered=False)
    outside = frame_for(slots('2025-07-20'), 100.0, 100.0, 100.0, covered=True)
    result = ea.boundary_coverage(pd.concat([inside, outside]))
    assert result.near_boundary == 0.0 and result.elsewhere == 1.0


def test_january_is_not_a_season_boundary() -> None:
    january = frame_for(slots('2025-01-02'), 100.0, 100.0, 100.0, covered=False)
    december = frame_for(slots('2024-12-10'), 100.0, 100.0, 100.0, covered=True)
    result = ea.boundary_coverage(pd.concat([january, december]))
    assert result.near_days == 1 and result.other_days == 1
    assert result.near_boundary == 1.0 and result.elsewhere == 0.0


def test_daily_band_is_in_error_coordinates() -> None:
    frame = frame_for(slots('2025-06-05'), 105.0, 100.0, 100.0)
    daily = ea.daily_table(frame, {}, {})
    # lower 90, upper 110, model 105, actual 100: the error is +5 and the band is [-5, +15].
    assert daily.band_low.iloc[0] == -5.0 and daily.band_high.iloc[0] == 15.0


def test_special_week_and_leave_one_out() -> None:
    assert ea.special_week(date(2024, 12, 23)) and ea.special_week(date(2024, 12, 29))
    assert not ea.special_week(date(2024, 12, 30)) and not ea.special_week(date(2024, 11, 25))
    residuals = pd.DataFrame({0: np.arange(1.0, 22.0), 1: np.arange(1.0, 22.0) * 2},
                             index=pd.Index([date(2025, 1, d) for d in range(1, 22)], name='day'))
    result = ea.leave_one_out_interval(residuals)
    # Day 10 left out: n=20, ranks 2 and 19 of 1..9, 11..21 are 2 and 20, so slot widths 18 and 36.
    assert result.loc[date(2025, 1, 10), 'coverage'] == 1.0
    assert result.loc[date(2025, 1, 1), 'coverage'] == 0.0
    assert result.loc[date(2025, 1, 10), 'width'] == pytest.approx(27.0)
    assert ea.tail_rank(90) == 9

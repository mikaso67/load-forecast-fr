from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast.cutoff import delivery_slots
from load_forecast.lag_model import LAG_COLUMNS
from load_forecast.revision_gap import (
    daily_means,
    forecast_shift,
    lag_shifts,
    summarize,
    summarize_by,
)


def gap_between(first: str, last: str) -> pd.Series:
    """Value = hours since 2026-01-01 UTC, so every value identifies its own timestamp."""
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    hours = (index - pd.Timestamp('2026-01-01', tz='UTC')) / pd.Timedelta(hours=1)
    return pd.Series(np.asarray(hours, dtype=float), index=index)


def value_at(timestamp: str) -> float:
    return (pd.Timestamp(timestamp, tz='UTC') - pd.Timestamp('2026-01-01', tz='UTC')
            ) / pd.Timedelta(hours=1)


def test_constant_gap_has_a_degenerate_interval():
    gap = pd.Series(500., index=pd.date_range('2026-06-30 22:00', '2026-07-31 21:30',
                                              freq='30min', tz='UTC'))
    result = summarize(gap)
    assert result['n_days'] == 31 and result['n_slots'] == 31 * 48
    assert result['mean'] == result['ci7_low'] == result['ci14_high'] == 500.


def test_daily_means_use_paris_dates_and_keep_missing_days():
    index = pd.DatetimeIndex(['2026-07-01 21:30', '2026-07-01 22:00', '2026-07-04 08:00'],
                             tz='UTC')
    daily = daily_means(pd.Series([1., 3., 5.], index=index))
    assert list(daily.index.date) == [date(2026, 7, d) for d in (1, 2, 3, 4)]
    assert daily.iloc[0] == 1. and daily.iloc[1] == 3. and daily.iloc[3] == 5.
    assert np.isnan(daily.iloc[2])


def test_hours_are_paris_clock_hours():
    gap = gap_between('2026-07-01 22:00', '2026-07-15 21:30')
    by_hour = summarize_by(gap, 'hour')
    assert list(by_hour.index) == list(range(24))
    midnight = gap[gap.index.hour == 22]
    assert by_hour.loc[0, 'mean'] == pytest.approx(midnight.mean())
    with pytest.raises(ValueError):
        summarize_by(gap, 'week')


def test_lag_shifts_read_minus_gap_at_each_lag_timestamp():
    gap = gap_between('2026-07-01 00:00', '2026-07-20 00:00')
    shifts = lag_shifts(gap, [date(2026, 7, 6), date(2026, 7, 15)])
    assert set(shifts.index.date) == {date(2026, 7, 14), date(2026, 7, 15)}
    slot = pd.Timestamp('2026-07-15 12:00', tz='UTC')
    assert shifts.loc[slot, 'lag_d2'] == -value_at('2026-07-13 12:00')
    assert shifts.loc[slot, 'lag_d7'] == -value_at('2026-07-08 12:00')
    assert shifts.loc[slot, 'lag_last'] == -value_at('2026-07-14 07:30')


def test_no_complete_day_raises():
    gap = gap_between('2026-07-01 00:00', '2026-07-03 00:00')
    with pytest.raises(ValueError):
        lag_shifts(gap, [date(2026, 7, 3)])


def test_gap_must_be_utc_and_complete():
    naive = pd.Series([1.], index=pd.DatetimeIndex(['2026-07-01 00:00']))
    with pytest.raises(ValueError):
        summarize(naive)
    with pytest.raises(ValueError):
        summarize(pd.Series([np.nan], index=pd.DatetimeIndex(['2026-07-01'], tz='UTC')))


def test_forecast_shift_uses_the_coefficients_of_the_local_slot():
    slots = pd.DatetimeIndex(delivery_slots(date(2026, 7, 15), '30min'))
    shifts = pd.DataFrame({'lag_d2': 100., 'lag_d7': 10., 'lag_last': 1.}, index=slots)
    coefficients = pd.DataFrame({'lag_d2': np.arange(48.), 'lag_d7': 1., 'lag_last': 0.},
                                columns=LAG_COLUMNS)
    shift = forecast_shift(shifts, coefficients)
    local_noon = pd.Timestamp('2026-07-15 10:00', tz='UTC')
    assert shift.loc[local_noon] == 100. * 24 + 10.
    with pytest.raises(ValueError):
        forecast_shift(shifts, coefficients.iloc[:47])


def test_autumn_change_day_keeps_both_two_oclock_hours():
    gap = pd.Series(1., index=pd.date_range('2026-10-17 22:00', '2026-11-01 22:30',
                                            freq='30min', tz='UTC'))
    change_day = gap[(gap.index >= '2026-10-24 22:00') & (gap.index < '2026-10-25 23:00')]
    assert len(change_day) == 50
    assert daily_means(change_day).index.date.tolist() == [date(2026, 10, 25)]
    by_hour = summarize_by(gap, 'hour')
    assert by_hour.loc[2, 'n_slots'] == by_hour.loc[3, 'n_slots'] + 2

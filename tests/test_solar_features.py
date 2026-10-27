from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.solar_features import SOLAR_COLUMN, hourly_flux, solar_day, with_solar
from load_forecast.weather import POINTS, candidate_runs


def flux(step: int) -> float:
    return 10.0 * step


def inputs(day, fallback=False, published=None):
    """Raw DSWRF rows as GFS stores them: the mean flux since the last 6-hour reset."""
    run = candidate_runs(day)[int(fallback)]
    published = published or forecast_cutoff(day) - timedelta(hours=1)
    manifest = pd.Series({'status': 'fallback' if fallback else 'primary',
                          'run_init_time': run.init_time, 'fallback': fallback})
    rows = []
    for step in range(run.first_step, run.first_step + 27):
        reset = 6 * ((step - 1) // 6)
        average = np.mean([flux(s) for s in range(reset + 1, step + 1)])
        rows += [{'run_init_time': run.init_time, 'variable': 'DSWRF', 'point': point,
                  'valid_time': run.init_time + timedelta(hours=step), 'start_step': reset,
                  'end_step': step, 'value': average, 'units': 'W m**-2',
                  'file_last_modified': published, 'index_last_modified': published}
                 for point in POINTS]
    return manifest, pd.DataFrame(rows)


def test_deaccumulation_recovers_hourly_flux_at_mid_hour():
    day = date(2024, 6, 3)
    manifest, points = inputs(day)
    one = points.loc[points['point'] == 'paris']
    result = hourly_flux(one)
    run = manifest['run_init_time']
    # f021 is the third hour of its window and has no previous step: it is dropped.
    assert result.index[0] == run + timedelta(hours=21, minutes=30)
    for t, value in result.items():
        step = (t - run + timedelta(minutes=30)) / timedelta(hours=1)
        assert value == pytest.approx(flux(int(step)))


@pytest.mark.parametrize('day,n,fallback', [(date(2024, 3, 31), 46, False),
                                          (date(2024, 10, 27), 50, True)])
def test_dst_days_and_fallback_are_fully_covered(day, n, fallback):
    manifest, points = inputs(day, fallback)
    frame = solar_day(day, manifest, points)
    assert len(frame) == n and frame[SOLAR_COLUMN].notna().all()
    run = manifest['run_init_time']
    t = frame.index[1]
    # Hourly mean stamped at mid-hour: at a half-hour slot the flux of the hour ending at t + 30 min.
    step = (t - run) / timedelta(hours=1) + 0.5
    assert frame.loc[t, SOLAR_COLUMN] == pytest.approx(flux(int(round(step))))


def test_missing_day_and_missing_step_give_nan_not_zero():
    day = date(2024, 1, 2)
    manifest, points = inputs(day)
    missing = manifest.copy()
    missing['status'], missing['run_init_time'] = 'weather missing', None
    assert solar_day(day, missing, points)[SOLAR_COLUMN].isna().all()
    gap = points.loc[~((points['point'] == 'lyon') & (points['end_step'] == 30))]
    frame = solar_day(day, manifest, gap)
    assert frame[SOLAR_COLUMN].isna().any() and frame[SOLAR_COLUMN].notna().any()


def test_radiation_published_at_cutoff_is_refused():
    day = date(2024, 1, 2)
    manifest, points = inputs(day, published=forecast_cutoff(day))
    with pytest.raises(ValueError, match='after cutoff'):
        solar_day(day, manifest, points)


def test_wrapper_encodings_ignore_consumption_history():
    day = date(2024, 6, 3)
    manifest, points = inputs(day)
    solar = solar_day(day, manifest, points)
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))

    def base(s, history, d):
        return pd.DataFrame({'x': 1.0}, index=s)

    full = pd.Series(1.0, index=pd.date_range('2024-01-01', periods=10, freq='30min', tz='UTC'))
    for encoding in ('slot', 'daily'):
        builder = with_solar(base, solar, encoding)
        with_history = builder(slots, full, day)[SOLAR_COLUMN]
        without = builder(slots, full.iloc[:0], day)[SOLAR_COLUMN]
        pd.testing.assert_series_equal(with_history, without)
    daily = with_solar(base, solar, 'daily')(slots, full, day)[SOLAR_COLUMN]
    assert daily.nunique() == 1 and daily.iloc[0] == pytest.approx(solar[SOLAR_COLUMN].mean())
    with pytest.raises(ValueError):
        with_solar(base, solar, 'hourly')

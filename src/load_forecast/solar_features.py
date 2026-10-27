"""Forecast solar radiation (GFS DSWRF) from the selected D11 run, aggregated like temperature.

GFS stores DSWRF as a mean flux since the last 6-hour reset. The hourly mean flux of the hour ending
at step t is k A_k - (k - 1) A_{k-1}, k = t - reset (docs/weather_sources.md). It is stamped at the
middle of its hour, interpolated to the half-hour grid inside one run, then averaged over the 12
points with equal weights, as `temperature_c`.
"""

from datetime import date

import numpy as np
import pandas as pd
import psycopg

from load_forecast.backtest import FeatureBuilder
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.periods import last_allowed_day, locked_message
from load_forecast.weather import POINTS, candidate_runs

SOLAR_COLUMN = 'solar_wm2'
ENCODINGS = ('slot', 'daily')


def hourly_flux(rows: pd.DataFrame) -> pd.Series:
    """Hourly mean flux of one point and one run, indexed by the middle of its hour (UTC).

    The first step after a reset needs no differencing; any other step needs the previous step of
    the same reset window, otherwise it is dropped. Small negative values from GRIB packing are
    set to 0, the physical bound.
    """
    rows = rows.sort_values('end_step')
    k = (rows['end_step'] - rows['start_step']).to_numpy()
    previous = rows.shift(1)
    same_window = ((previous['end_step'] == rows['end_step'] - 1)
                   & (previous['start_step'] == rows['start_step'])).to_numpy()
    flux = np.where(k == 1, rows['value'], k * rows['value'] - (k - 1) * previous['value'])
    keep = (k == 1) | same_window
    index = pd.DatetimeIndex(rows['valid_time'])[keep] - pd.Timedelta(minutes=30)
    return pd.Series(np.maximum(flux[keep].astype(float), 0.0), index=index)


def solar_day(day: date, manifest: pd.Series, points: pd.DataFrame) -> pd.DataFrame:
    """`solar_wm2` on the delivery slots of `day`; NaN on a day without weather or a missing step."""
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))
    frame = pd.DataFrame({SOLAR_COLUMN: float('nan')}, index=slots)
    if manifest['status'] == 'weather missing':
        return frame
    if manifest['status'] not in ('primary', 'fallback'):
        raise ValueError('Unknown weather selection status')
    run = manifest['run_init_time']
    if run != candidate_runs(day)[int(manifest['status'] == 'fallback')].init_time:
        raise ValueError('Manifest does not select the expected D11 run')
    selected = points.loc[(points['run_init_time'] == run) & (points['variable'] == 'DSWRF')]
    if selected.duplicated(['point', 'end_step']).any():
        raise ValueError('Ambiguous raw radiation versions')
    if ((selected['file_last_modified'] >= forecast_cutoff(day))
            | (selected['index_last_modified'] >= forecast_cutoff(day))).any():
        raise ValueError('Radiation published after cutoff')
    if (selected['units'] != 'W m**-2').any():
        raise ValueError('Expected W m**-2 radiation')
    columns = {}
    for point in POINTS:
        flux = hourly_flux(selected.loc[selected['point'] == point])
        if flux.empty:
            columns[point] = pd.Series(float('nan'), index=slots)
            continue
        grid = pd.date_range(flux.index.min(), flux.index.max(), freq='30min')
        values = flux.reindex(grid)
        # Half-way points between two hourly means; a missing hour leaves its neighbours NaN.
        between = ~grid.isin(flux.index)
        values.loc[between] = ((values.shift(1) + values.shift(-1)) / 2).loc[between]
        columns[point] = values.reindex(slots)
    frame[SOLAR_COLUMN] = pd.DataFrame(columns).mean(axis=1, skipna=False)
    return frame


def read_solar(connection: psycopg.Connection, first: date, last: date, *,
               final_test: bool = False) -> pd.DataFrame:
    """Read manifests and DSWRF in one query, as `read_temperature`; requires lff_agent."""
    if first > last or last > last_allowed_day(final_test):
        raise ValueError(f'Expected an ordered period {locked_message(final_test)}')
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_user')
        if cursor.fetchone()[0] != 'lff_agent':
            raise ValueError('Weather features require lff_agent')
        cursor.execute('''WITH days AS (
                            SELECT delivery_day, status, run_init_time, fallback,
                                   COUNT(*) OVER (PARTITION BY delivery_day) AS selection_count
                            FROM raw.gfs_days WHERE delivery_day BETWEEN %s AND %s
                          )
                          SELECT d.*, p.variable, p.point, p.valid_time, p.start_step,
                                 p.end_step, p.value, p.units,
                                 p.file_last_modified, p.index_last_modified
                          FROM days d LEFT JOIN raw.gfs_points p
                          ON p.run_init_time = d.run_init_time AND p.variable = 'DSWRF' ''',
                       (first, last))
        raw = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    if raw['selection_count'].gt(1).any():
        raise ValueError('Ambiguous raw day selections')
    manifests = raw[['delivery_day', 'status', 'run_init_time', 'fallback']].drop_duplicates()
    manifests = manifests.set_index('delivery_day')
    points = raw.loc[raw['point'].notna()]
    runs = dict(tuple(points.groupby('run_init_time', sort=False)))
    results = []
    for day in pd.date_range(first, last).date:
        if day not in manifests.index:
            raise ValueError(f'No weather selection recorded for {day}')
        manifest = manifests.loc[day]
        results.append(solar_day(day, manifest, runs.get(manifest['run_init_time'], points.iloc[:0])))
    return pd.concat(results)


def with_solar(builder: FeatureBuilder, solar: pd.DataFrame, encoding: str = 'slot') -> FeatureBuilder:
    """Add `solar_wm2` of the delivery day: the value at each slot, or the mean of the day."""
    if encoding not in ENCODINGS:
        raise ValueError(f'Unknown encoding {encoding}')
    column = solar[SOLAR_COLUMN]

    def wrapped(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
        frame = builder(slots, history, day)
        values = column.reindex(slots)
        # skipna=False: one missing slot makes the daily mean NaN, so the backup applies.
        frame[SOLAR_COLUMN] = values if encoding == 'slot' else values.mean(skipna=False)
        return frame

    return wrapped


"""Selected-run Celsius temperature features without temporal extrapolation."""

from datetime import date

import pandas as pd
import psycopg

from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.periods import last_allowed_day, locked_message
from load_forecast.weather import POINTS, candidate_runs


def temperature_day(day: date, manifest: pd.Series, points: pd.DataFrame) -> pd.DataFrame:
    """Interpolate the D11 run in Celsius; status distinguishes absent TMP backfill.

    Partial TMP coverage keeps the primary/fallback status and leaves affected slots
    NaN. ``not_collected`` means no TMP rows for the selected run.
    """
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))
    frame = pd.DataFrame(float('nan'), index=slots, columns=list(POINTS))
    run = manifest['run_init_time']
    frame['run_init_time'] = pd.Series(run, index=slots, dtype='datetime64[ns, UTC]')
    frame['fallback'] = False
    frame['weather_status'] = 'weather_missing'
    if manifest['status'] == 'weather missing':
        frame.columns = [f'{c}_c' if c in POINTS else c for c in frame.columns]
        frame['temperature_c'] = float('nan')
        return frame
    if manifest['status'] not in ('primary', 'fallback'):
        raise ValueError('Unknown weather selection status')
    fallback = manifest['status'] == 'fallback'
    expected = candidate_runs(day)[int(fallback)].init_time
    if run != expected or bool(manifest['fallback']) != fallback:
        raise ValueError('Manifest does not select the expected D11 run')
    selected = points.loc[(points['run_init_time'] == run) & (points['variable'] == 'TMP')]
    frame['fallback'] = fallback
    frame['weather_status'] = 'not_collected' if selected.empty else manifest['status']
    if selected.duplicated(['point', 'valid_time']).any():
        raise ValueError('Ambiguous raw temperature versions')
    for point in POINTS:
        rows = selected.loc[selected['point'] == point].sort_values('valid_time')
        if rows.empty:
            continue
        times = pd.DatetimeIndex(rows['valid_time'])
        if str(times.tz) != 'UTC' or (times != run + pd.to_timedelta(rows['end_step'], unit='h')).any():
            raise ValueError('Invalid temperature valid times')
        if (rows['units'] != 'K').any():
            raise ValueError('Expected kelvin temperatures')
        if ((rows['file_last_modified'] >= forecast_cutoff(day))
                | (rows['index_last_modified'] >= forecast_cutoff(day))).any():
            raise ValueError('Temperature published after cutoff')
        hourly = pd.Series(rows['value'].to_numpy(), index=times) - 273.15
        grid = pd.date_range(times.min(), times.max(), freq='30min')
        # A missing hourly step breaks interpolation on both neighbouring half-hours.
        values = hourly.reindex(grid)
        halfway = grid.minute == 30
        values.loc[halfway] = ((values.shift(1) + values.shift(-1)) / 2).loc[halfway]
        frame[point] = values.reindex(slots)
    frame['temperature_c'] = frame[list(POINTS)].mean(axis=1, skipna=False)
    frame.columns = [f'{c}_c' if c in POINTS else c for c in frame.columns]
    return frame


def read_temperature(connection: psycopg.Connection, first: date, last: date, *,
                     final_test: bool = False) -> pd.DataFrame:
    """Read manifests and TMP in one query per range, then group points by run.

    Require lff_agent and reject unresolved raw revisions. No query runs per day.
    """
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
                          SELECT d.*, p.variable, p.point, p.valid_time,
                                 p.end_step, p.value, p.units,
                                 p.file_last_modified, p.index_last_modified
                          FROM days d LEFT JOIN raw.gfs_points p
                          ON p.run_init_time = d.run_init_time AND p.variable = 'TMP' ''',
                       (first, last))
        raw = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    if raw['selection_count'].gt(1).any():
        raise ValueError('Ambiguous raw day selections')
    days = raw[['delivery_day', 'status', 'run_init_time', 'fallback']].drop_duplicates()
    points = raw.loc[raw['point'].notna()].drop(
        columns=['delivery_day', 'status', 'fallback', 'selection_count'])
    manifests = days.set_index('delivery_day')
    runs = dict(tuple(points.groupby('run_init_time', sort=False)))
    empty_points = points.iloc[:0]
    results = []
    for day in pd.date_range(first, last).date:
        if day not in manifests.index:
            raise ValueError(f'No weather selection recorded for {day}')
        manifest = manifests.loc[day]
        selected = runs.get(manifest['run_init_time'], empty_points)
        results.append(temperature_day(day, manifest, selected))
    return pd.concat(results)

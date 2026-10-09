"""Bounded seven-day database check with an independent midpoint calculation."""

from datetime import date
from time import perf_counter

import pandas as pd

from load_forecast.eco2mix import connect_database
from load_forecast.weather_features import read_temperature


def main() -> None:
    with connect_database() as connection:
        frame = read_temperature(connection, date(2021, 3, 23), date(2021, 3, 29))
        timestamp = pd.Timestamp('2021-03-24 00:30', tz='UTC')
        run = frame.loc[timestamp, 'run_init_time']
        rows = connection.execute('''SELECT valid_time, value FROM raw.gfs_points
                                    WHERE run_init_time = %s AND point = 'paris'
                                    AND variable = 'TMP' AND valid_time IN (%s, %s)
                                    ORDER BY valid_time''',
                                  (run, timestamp.floor('h'), timestamp.ceil('h'))).fetchall()
        assert len(rows) == 2
        expected = (rows[0][1] + rows[1][1]) / 2 - 273.15
        actual = frame.loc[timestamp, 'paris_c']
        assert abs(actual - expected) < 1e-10
        assert frame.loc['2021-03-22 23:00':'2021-03-23 22:30', 'temperature_c'].isna().all()
        print(f'Rows={len(frame)}, available={frame.temperature_c.notna().sum()}')
        print(f'Paris {timestamp}: ({rows[0][1]} + {rows[1][1]}) / 2 - 273.15 = {actual} C')
        start = perf_counter()
        year = read_temperature(connection, date(2022, 1, 1), date(2022, 12, 31))
        elapsed = perf_counter() - start
        assert len(year) == 17520
        print(f'2022 full-year read: {len(year)} slots in {elapsed:.3f} seconds')
        print(year.groupby('weather_status').size().to_string())


if __name__ == '__main__':
    main()

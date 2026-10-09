from datetime import date, timedelta

import pandas as pd
import pytest

from load_forecast.cutoff import forecast_cutoff
from load_forecast.weather import POINTS, candidate_runs
from load_forecast.weather_features import read_temperature, temperature_day


def inputs(day, fallback=False):
    run = candidate_runs(day)[int(fallback)]
    manifest = pd.Series({'status': 'fallback' if fallback else 'primary',
                          'run_init_time': run.init_time, 'fallback': fallback})
    points = pd.DataFrame([{'run_init_time': run.init_time, 'variable': 'TMP', 'point': point,
                            'valid_time': run.init_time + timedelta(hours=step), 'end_step': step,
                            'value': 270. + step, 'units': 'K',
                            'file_last_modified': forecast_cutoff(day) - timedelta(hours=1),
                            'index_last_modified': forecast_cutoff(day) - timedelta(hours=1)}
                           for step in range(run.first_step, run.first_step + 27)
                           for point in POINTS])
    return manifest, points


@pytest.mark.parametrize('day,n,fallback', [(date(2024, 3, 31), 46, False),
                                          (date(2024, 10, 27), 50, True)])
def test_dst_and_fallback(day, n, fallback):
    manifest, points = inputs(day, fallback)
    frame = temperature_day(day, manifest, points)
    assert len(frame) == n
    assert str(frame.index.tz) == 'UTC'
    assert frame['fallback'].eq(fallback).all()
    assert frame['run_init_time'].eq(manifest['run_init_time']).all()
    t = frame.index[1]
    expected = 270 + (t - manifest['run_init_time']).total_seconds()/3600 - 273.15
    assert frame.loc[t, 'temperature_c'] == pytest.approx(expected)
    assert frame.loc[t, 'paris_c'] == pytest.approx(expected)
    assert frame['weather_status'].eq('fallback' if fallback else 'primary').all()
    assert 'temperature' not in frame and 'paris' not in frame


def test_missing_day_and_hour_are_not_filled():
    day = date(2024, 1, 2)
    manifest, points = inputs(day)
    missing = manifest.copy()
    missing['status'] = 'weather missing'
    missing['run_init_time'] = None
    missing['fallback'] = None
    missing_frame = temperature_day(day, missing, points)
    assert missing_frame['temperature_c'].isna().all()
    assert missing_frame['weather_status'].eq('weather_missing').all()
    assert missing_frame['fallback'].dtype == bool
    assert not missing_frame['fallback'].any()
    points = points.loc[~((points['point'] == 'paris') & (points['end_step'] == 25))]
    frame = temperature_day(day, manifest, points)
    assert frame.loc['2024-01-02 00:30':'2024-01-02 01:30', 'temperature_c'].isna().all()
    assert frame['weather_status'].eq('primary').all()


def test_later_runs_and_revisions():
    day = date(2024, 1, 2)
    manifest, points = inputs(day)
    later = points.copy()
    later['run_init_time'] += pd.Timedelta(hours=6)
    later['value'] = 999
    pd.testing.assert_frame_equal(temperature_day(day, manifest, points),
                                  temperature_day(day, manifest, pd.concat([points, later])))
    with pytest.raises(ValueError, match='Ambiguous'):
        temperature_day(day, manifest, pd.concat([points, points.iloc[:1]]))
    points.loc[0, 'file_last_modified'] = forecast_cutoff(day)
    with pytest.raises(ValueError, match='cutoff'):
        temperature_day(day, manifest, points)


def test_reader_bounds_and_identity():
    from unittest.mock import MagicMock

    connection = MagicMock()
    with pytest.raises(ValueError, match='before 2025'):
        read_temperature(connection, date(2024, 1, 1), date(2025, 1, 1))
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = ('lff',)
    with pytest.raises(ValueError, match='lff_agent'):
        read_temperature(connection, date(2021, 3, 23), date(2021, 3, 29))


@pytest.mark.parametrize('fallback', [False, True])
def test_not_collected_preserves_selected_run(fallback):
    day = date(2024, 1, 2)
    manifest, points = inputs(day, fallback)
    later = points.copy()
    later['run_init_time'] += pd.Timedelta(hours=6)
    frame = temperature_day(day, manifest, later)
    assert frame['weather_status'].eq('not_collected').all()
    assert frame['temperature_c'].isna().all()
    assert frame['run_init_time'].eq(manifest['run_init_time']).all()
    assert frame['fallback'].eq(fallback).all()
    assert frame['fallback'].dtype == bool


def test_manifest_run_and_index_publication_are_checked():
    day = date(2024, 1, 2)
    manifest, points = inputs(day)
    wrong_run = manifest.copy()
    wrong_run['run_init_time'] += pd.Timedelta(hours=6)
    with pytest.raises(ValueError, match='expected D11 run'):
        temperature_day(day, wrong_run, points)
    points.loc[0, 'index_last_modified'] = forecast_cutoff(day)
    with pytest.raises(ValueError, match='cutoff'):
        temperature_day(day, manifest, points)


@pytest.mark.parametrize('duplicate_manifest', [False, True])
def test_range_reader_groups_runs_in_one_query(duplicate_manifest):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    first, last = date(2024, 1, 2), date(2024, 1, 4)
    manifest, points = inputs(first)
    fallback, _ = inputs(last, fallback=True)
    missing = pd.Series({'status': 'weather missing', 'run_init_time': None, 'fallback': None})
    days = pd.DataFrame([dict(delivery_day=first, **manifest),
                         dict(delivery_day=date(2024, 1, 3), **missing),
                         dict(delivery_day=last, **fallback)])
    days['selection_count'] = 2 if duplicate_manifest else 1
    raw = days.merge(points, on='run_init_time', how='left')
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ('lff_agent',)
    cursor.fetchall.return_value = list(raw.itertuples(index=False, name=None))
    cursor.description = [SimpleNamespace(name=c) for c in raw.columns]
    if duplicate_manifest:
        with pytest.raises(ValueError, match='Ambiguous raw day'):
            read_temperature(connection, first, last)
        return
    frame = read_temperature(connection, first, last)
    expected = pd.concat([temperature_day(first, manifest, points),
                          temperature_day(date(2024, 1, 3), missing, points.iloc[:0]),
                          temperature_day(last, fallback, points.iloc[:0])])
    pd.testing.assert_frame_equal(frame, expected)
    assert cursor.execute.call_count == 2  # Identity plus one range query.
    for call in cursor.execute.call_args_list[1:]:
        assert call.args[1] == (first, last)

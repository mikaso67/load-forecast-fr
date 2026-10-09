from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, walk_forward
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.temperature_model import (
    BACKUP_FLAG,
    COOLING_BASE_C,
    HEATING_BASE_C,
    degree_features,
    temperature_ols_features,
    with_calendar_backup,
)
from load_forecast.weather import POINTS, candidate_runs
from load_forecast.weather_features import read_temperature

NO_HISTORY = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC'))


def day_rows(day: date) -> pd.DataFrame:
    """Raw rows of the D11 primary run of one delivery day, every row published before its cutoff.

    The temperature level depends on the delivery date, so a builder that reads another day's
    run changes the features.
    """
    run = candidate_runs(day)[0]
    published = forecast_cutoff(day) - timedelta(hours=1)
    level = 278. + 6. * np.sin((day - date(2024, 3, 1)).days / 3.)
    points = pd.DataFrame([{
        'delivery_day': day, 'status': 'primary', 'run_init_time': run.init_time, 'fallback': False,
        'selection_count': 1, 'variable': 'TMP', 'point': point,
        'valid_time': run.init_time + timedelta(hours=step), 'end_step': step,
        'value': level + 0.2 * step, 'units': 'K',
        'file_last_modified': published, 'index_last_modified': published}
        for step in range(run.first_step, run.first_step + 27) for point in POINTS])
    return points


def connection_returning(raw: pd.DataFrame) -> MagicMock:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ('lff_agent',)
    cursor.fetchall.return_value = list(raw.itertuples(index=False, name=None))
    cursor.description = [SimpleNamespace(name=c) for c in raw.columns]
    return connection


def archive(first: date, last: date) -> pd.DataFrame:
    return pd.concat([day_rows(d) for d in pd.date_range(first, last).date], ignore_index=True)


def truncated_at(raw: pd.DataFrame, day: date) -> pd.DataFrame:
    """Delete every raw row published at or after the cutoff of `day`, and the later selections."""
    cutoff = forecast_cutoff(day)
    kept = raw.loc[(raw['file_last_modified'] < cutoff) & (raw['index_last_modified'] < cutoff)]
    return kept.reset_index(drop=True)


def test_degrees_use_fixed_bases_and_never_turn_nan_into_zero():
    temperature = pd.Series([0., HEATING_BASE_C, 17., COOLING_BASE_C, 30., np.nan])
    degrees = degree_features(temperature)
    assert (HEATING_BASE_C, COOLING_BASE_C) == (15.0, 20.0)
    assert degrees['hdd'].iloc[:5].tolist() == [15., 0., 0., 0., 0.]
    assert degrees['cdd'].iloc[:5].tolist() == [0., 0., 0., 0., 10.]
    assert degrees.iloc[5].isna().all()


@pytest.mark.parametrize('day,n', [(date(2024, 3, 10), 48), (date(2024, 3, 31), 46),
                                   (date(2024, 10, 27), 50)])
def test_features_extend_the_calendar_ones_on_the_exact_grid(day, n):
    first = min(day, date(2024, 3, 1))
    temperature = read_temperature(connection_returning(archive(first, day)), first, day)
    builder = temperature_ols_features(temperature)
    features = day_features(NO_HISTORY, day, builder)
    assert len(features) == n
    pd.testing.assert_frame_equal(features[calendar_ols_features(features.index, NO_HISTORY,
                                                                   day).columns],
                                  calendar_ols_features(features.index, NO_HISTORY, day))
    expected = temperature.loc[features.index, 'temperature_c']
    np.testing.assert_allclose(features['hdd'], (HEATING_BASE_C - expected).clip(lower=0))
    np.testing.assert_allclose(features['cdd'], (expected - COOLING_BASE_C).clip(lower=0))


def test_weather_feature_truncation_without_late_gfs_rows():
    """Features of D do not change when every GFS row published after cutoff(D) is deleted."""
    first, last = date(2024, 3, 1), date(2024, 3, 20)
    raw = archive(first, last)
    for day in (date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 17)):
        full = read_temperature(connection_returning(raw), first, last)
        short = read_temperature(connection_returning(truncated_at(raw, day)), first, day)
        assert short.index.max() < full.index.max()
        features_full = day_features(NO_HISTORY, day, temperature_ols_features(full))
        features_short = day_features(NO_HISTORY, day, temperature_ols_features(short))
        pd.testing.assert_frame_equal(features_full, features_short)
        assert features_full['hdd'].notna().all()


def test_builder_reads_only_the_requested_day():
    first, last = date(2024, 3, 1), date(2024, 3, 10)
    temperature = read_temperature(connection_returning(archive(first, last)), first, last)
    builder = temperature_ols_features(temperature)
    day = date(2024, 3, 5)
    base = day_features(NO_HISTORY, day, builder)
    changed = temperature.copy()
    other = pd.DatetimeIndex(delivery_slots(date(2024, 3, 6), '30min'))
    changed.loc[other, 'temperature_c'] = -40.
    pd.testing.assert_frame_equal(base, day_features(NO_HISTORY, day, temperature_ols_features(
        changed)))


def test_coverage_rules_before_inside_and_after_the_archive():
    first, last = date(2024, 3, 2), date(2024, 3, 10)
    temperature = read_temperature(connection_returning(archive(first, last)), first, last)
    builder = temperature_ols_features(temperature)
    before = day_features(NO_HISTORY, date(2024, 3, 1), builder)
    assert before[['hdd', 'cdd']].isna().all().all()
    assert before.drop(columns=['hdd', 'cdd']).notna().all().all()
    with pytest.raises(ValueError, match='No temperature'):
        day_features(NO_HISTORY, date(2024, 3, 11), builder)
    gap = temperature.loc[[i for i in temperature.index
                           if ev.local_dates(pd.DatetimeIndex([i]))[0] != date(2024, 3, 6)]]
    with pytest.raises(ValueError, match='No temperature'):
        day_features(NO_HISTORY, date(2024, 3, 6), temperature_ols_features(gap))


def test_weather_missing_slots_are_left_out_of_the_fit_not_filled():
    first, last = date(2024, 3, 1), date(2024, 3, 12)
    temperature = read_temperature(connection_returning(archive(first, last)), first, last)
    slots = pd.DatetimeIndex(delivery_slots(date(2024, 3, 8), '30min'))
    temperature.loc[slots, 'temperature_c'] = float('nan')
    index = pd.date_range('2024-03-01', '2024-03-13', freq='30min', tz='UTC', inclusive='left')
    history = pd.Series(np.arange(len(index), dtype=float), index=index)
    calls = []

    class Recording(SlotOLS):
        def fit(self, x, y):
            calls.append(x)
            return super().fit(x, y)

    walk_forward(history, date(2024, 3, 11), date(2024, 3, 11), Recording,
                 builder=temperature_ols_features(temperature),
                 training_start=date(2024, 3, 1))
    assert calls
    assert all(x.notna().all().all() for x in calls)
    missing_day = ev.local_dates(slots)[0]
    assert all(missing_day not in set(ev.local_dates(x.index)) for x in calls)


def test_walk_forward_truncation_with_weather():
    """Predictions for D are unchanged when later history and later GFS rows are removed."""
    first, last = date(2024, 3, 1), date(2024, 3, 25)
    raw = archive(first, last)
    index = pd.date_range('2024-02-20', '2024-03-26', freq='30min', tz='UTC', inclusive='left')
    rng = np.random.default_rng(3)
    history = pd.Series(50000. + rng.normal(0, 500, len(index)), index=index)
    for day in (date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 20)):
        full_weather = read_temperature(connection_returning(raw), first, last)
        short_weather = read_temperature(connection_returning(truncated_at(raw, day)), first, day)
        boundary = ev.last_usable_slot_start(day)
        kwargs = {'training_start': date(2024, 2, 21)}
        full = walk_forward(history, day, day, SlotOLS,
                            builder=temperature_ols_features(full_weather), **kwargs)
        short = walk_forward(history.loc[history.index <= boundary], day, day, SlotOLS,
                             builder=temperature_ols_features(short_weather), **kwargs)
        pd.testing.assert_frame_equal(full, short)
        assert full[ev.MODEL].notna().all()


def test_temperature_changes_the_forecast_when_it_matters():
    first, last = date(2024, 1, 1), date(2024, 3, 25)
    temperature = read_temperature(connection_returning(archive(first, last)), first, last)
    index = pd.date_range('2023-12-20', '2024-03-26', freq='30min', tz='UTC', inclusive='left')
    degrees = degree_features(temperature['temperature_c'].reindex(index))
    history = pd.Series(40000. + 900. * degrees['hdd'].fillna(0.), index=index)
    day = date(2024, 3, 22)
    kwargs = {'training_start': first}
    weather = walk_forward(history, day, day, SlotOLS,
                           builder=temperature_ols_features(temperature), **kwargs)
    calendar = walk_forward(history, day, day, SlotOLS, builder=calendar_ols_features, **kwargs)
    actual = history.reindex(weather.index)
    assert (weather[ev.MODEL] - actual).abs().max() < 1.
    assert (calendar[ev.MODEL] - actual).abs().mean() > 100.


def test_calendar_backup_replaces_only_missing_slots_and_flags_them():
    index = pd.date_range('2024-03-01', periods=4, freq='30min', tz='UTC')
    weather = pd.DataFrame({ev.MODEL: [1., np.nan, 3., np.nan], 'refit_date': date(2024, 2, 26)},
                           index=index)
    calendar = pd.DataFrame({ev.MODEL: [10., 20., 30., 40.], 'refit_date': date(2024, 2, 26)},
                            index=index)
    combined = with_calendar_backup(weather, calendar)
    assert combined[ev.MODEL].tolist() == [1., 20., 3., 40.]
    assert combined[BACKUP_FLAG].tolist() == [False, True, False, True]
    assert weather[ev.MODEL].isna().sum() == 2  # the input is not modified
    with pytest.raises(ValueError, match='same slots'):
        with_calendar_backup(weather, calendar.iloc[:3])

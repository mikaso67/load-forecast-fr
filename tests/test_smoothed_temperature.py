from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, walk_forward
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.gam_model import TEMPERATURE_COLUMN, SlotGAM, lagged_gam_features
from load_forecast.smoothed_temperature import (
    SMOOTH_COLUMN,
    smooth_temperature,
    smoothed_gam_features,
    smoothing_constant,
)
from load_forecast.weather import POINTS, candidate_runs
from load_forecast.weather_features import read_temperature

NO_HISTORY = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC'))
A = 0.97
TRUNCATION_DAYS = (date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 17))


def day_rows(day: date) -> pd.DataFrame:
    """Raw rows of the primary run selected for `day`, all published before its cutoff.

    The level moves from one delivery day to the next, so a chain that reads another day's run
    changes the smoothed value.
    """
    run = candidate_runs(day)[0]
    published = forecast_cutoff(day) - timedelta(hours=1)
    level = 278. + 6. * np.sin((day - date(2024, 3, 1)).days / 2.)
    return pd.DataFrame([{
        'delivery_day': day, 'status': 'primary', 'run_init_time': run.init_time, 'fallback': False,
        'selection_count': 1, 'variable': 'TMP', 'point': point,
        'valid_time': run.init_time + timedelta(hours=step), 'end_step': step,
        'value': level + 0.2 * step, 'units': 'K',
        'file_last_modified': published, 'index_last_modified': published}
        for step in range(run.first_step, run.first_step + 27) for point in POINTS])


def archive(first: date, last: date) -> pd.DataFrame:
    return pd.concat([day_rows(d) for d in pd.date_range(first, last).date], ignore_index=True)


def connection_returning(raw: pd.DataFrame) -> MagicMock:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ('lff_agent',)
    cursor.fetchall.return_value = list(raw.itertuples(index=False, name=None))
    cursor.description = [SimpleNamespace(name=c) for c in raw.columns]
    return connection


def truncated_at(raw: pd.DataFrame, day: date) -> pd.DataFrame:
    """Delete every raw row published at or after the cutoff of `day`."""
    cutoff = forecast_cutoff(day)
    kept = raw.loc[(raw['file_last_modified'] < cutoff) & (raw['index_last_modified'] < cutoff)]
    return kept.reset_index(drop=True)


def frames(first: date, last: date, day: date):
    raw = archive(first, last)
    full = read_temperature(connection_returning(raw), first, last)
    short = read_temperature(connection_returning(truncated_at(raw, day)), first, day)
    return full, short


def slots_of(day: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(delivery_slots(day, '30min'))


def test_recursion_matches_the_formula_and_starts_at_the_first_value():
    index = pd.date_range('2024-03-01', periods=5, freq='30min', tz='UTC')
    celsius = pd.Series([10., 12., 8., 8., 20.], index=index)
    out = smooth_temperature(celsius, 0.5)
    assert out.tolist() == [10., 11., 9.5, 8.75, 14.375]
    assert smooth_temperature(celsius, 0.0).tolist() == celsius.tolist()


def test_missing_slots_give_nan_and_carry_the_state_without_decay():
    index = pd.date_range('2024-03-01', periods=6, freq='30min', tz='UTC')
    celsius = pd.Series([10., 12., np.nan, np.nan, 8., 20.], index=index)
    out = smooth_temperature(celsius, 0.5)
    assert out.isna().tolist() == [False, False, True, True, False, False]
    removed = smooth_temperature(celsius.dropna(), 0.5)
    np.testing.assert_allclose(out.dropna(), removed)


def test_a_missing_day_leaves_the_following_days_as_if_it_did_not_exist():
    first, last = date(2024, 3, 1), date(2024, 3, 8)
    frame = read_temperature(connection_returning(archive(first, last)), first, last)
    missing = date(2024, 3, 4)
    holed = frame['temperature_c'].copy()
    holed.loc[slots_of(missing)] = np.nan
    out = smooth_temperature(holed, A)
    assert out.loc[slots_of(missing)].isna().all()
    without = smooth_temperature(frame['temperature_c'].drop(slots_of(missing)), A)
    after = slots_of(date(2024, 3, 5))
    np.testing.assert_allclose(out.loc[after], without.loc[after])


def test_half_life_converts_to_the_per_half_hour_constant():
    a = smoothing_constant(24.0)
    assert a ** 48 == pytest.approx(0.5)
    assert 0 < smoothing_constant(6.0) < a < smoothing_constant(96.0) < 1
    with pytest.raises(ValueError):
        smoothing_constant(0.0)


@pytest.mark.parametrize('bad', [-0.1, 1.0, 1.5])
def test_constant_outside_zero_one_is_refused(bad):
    index = pd.date_range('2024-03-01', periods=3, freq='30min', tz='UTC')
    with pytest.raises(ValueError, match='constant'):
        smooth_temperature(pd.Series([1., 2., 3.], index=index), bad)


def test_unsorted_naive_or_duplicated_index_is_refused():
    index = pd.date_range('2024-03-01', periods=3, freq='30min', tz='UTC')
    series = pd.Series([1., 2., 3.], index=index)
    for bad in (series.iloc[::-1], series.tz_localize(None),
                pd.concat([series, series.iloc[:1]])):
        with pytest.raises(ValueError, match='sorted unique UTC'):
            smooth_temperature(bad, A)


def test_features_add_the_chained_value_to_the_gam_features():
    first, last = date(2024, 3, 1), date(2024, 3, 12)
    frame = read_temperature(connection_returning(archive(first, last)), first, last)
    day = date(2024, 3, 10)
    features = day_features(NO_HISTORY, day, smoothed_gam_features(frame, A))
    plain = day_features(NO_HISTORY, day, lagged_gam_features(frame))
    pd.testing.assert_frame_equal(features.drop(columns=SMOOTH_COLUMN), plain)
    expected = smooth_temperature(frame['temperature_c'], A).loc[features.index]
    np.testing.assert_allclose(features[SMOOTH_COLUMN], expected)
    # the chain really carries earlier days: it differs from the temperature of the day itself
    assert (features[SMOOTH_COLUMN] - features[TEMPERATURE_COLUMN]).abs().max() > 1.0


def test_chain_equals_manual_recursion_over_each_days_own_run():
    first, last = date(2024, 3, 1), date(2024, 3, 6)
    frame = read_temperature(connection_returning(archive(first, last)), first, last)
    state, manual = None, []
    for day in pd.date_range(first, last).date:
        for value in frame.loc[slots_of(day), 'temperature_c']:
            state = value if state is None else A * state + (1 - A) * value
            manual.append(state)
    features = pd.concat([day_features(NO_HISTORY, d, smoothed_gam_features(frame, A))
                          for d in pd.date_range(first, last).date])
    np.testing.assert_allclose(features[SMOOTH_COLUMN], manual)


def test_days_before_the_archive_have_nan_and_days_after_it_raise():
    first, last = date(2024, 3, 3), date(2024, 3, 10)
    frame = read_temperature(connection_returning(archive(first, last)), first, last)
    builder = smoothed_gam_features(frame, A)
    assert day_features(NO_HISTORY, date(2024, 3, 2), builder)[SMOOTH_COLUMN].isna().all()
    with pytest.raises(ValueError, match='No temperature'):
        day_features(NO_HISTORY, date(2024, 3, 11), builder)


def leaks(factory) -> bool:
    """True when the features of a day change after every GFS row published after its cutoff is deleted."""
    first, last = date(2024, 3, 1), date(2024, 3, 20)
    for day in TRUNCATION_DAYS:
        full, short = frames(first, last, day)
        try:
            features_short = day_features(NO_HISTORY, day, factory(short))
        except ValueError:
            return True
        features_full = day_features(NO_HISTORY, day, factory(full))
        try:
            pd.testing.assert_frame_equal(features_full, features_short)
        except AssertionError:
            return True
    return False


def with_series(temperature: pd.DataFrame, mutant) -> object:
    """Real GAM features with the smoothed column replaced by the output of a mutant smoother."""
    base = lagged_gam_features(temperature)
    series = mutant(temperature['temperature_c'])

    def builder(slots, history, day):
        frame = base(slots, history, day)
        frame[SMOOTH_COLUMN] = series.reindex(slots)
        return frame

    return builder


def next_days_run(celsius: pd.Series) -> pd.Series:
    """Mutant: each slot takes the value of the next delivery day's run (a later download)."""
    shifted = pd.Series(celsius.shift(-48).to_numpy(), index=celsius.index)
    return smooth_temperature(shifted.fillna(celsius), A)


def centered(celsius: pd.Series) -> pd.Series:
    """Mutant: mean of the forward and the backward recursion, so tomorrow reaches today."""
    forward = smooth_temperature(celsius, A)
    backward = smooth_temperature(celsius.iloc[::-1], A).iloc[::-1]
    return (forward + backward) / 2


def test_features_do_not_change_when_gfs_rows_after_the_cutoff_are_deleted():
    assert not leaks(lambda frame: smoothed_gam_features(frame, A))


@pytest.mark.parametrize('mutant', [next_days_run, centered])
def test_the_truncation_check_catches_leaking_smoothers(mutant):
    assert leaks(lambda frame: with_series(frame, mutant))


def test_builder_never_reads_a_later_day_at_the_requested_slots():
    first, last = date(2024, 3, 1), date(2024, 3, 12)
    frame = read_temperature(connection_returning(archive(first, last)), first, last)
    day = date(2024, 3, 5)
    base = day_features(NO_HISTORY, day, smoothed_gam_features(frame, A))
    changed = frame.copy()
    changed.loc[slots_of(date(2024, 3, 6)), 'temperature_c'] = -40.
    pd.testing.assert_frame_equal(base, day_features(NO_HISTORY, day,
                                                    smoothed_gam_features(changed, A)))


def two_smooth_gam():
    return SlotGAM([TEMPERATURE_COLUMN, SMOOTH_COLUMN], df=5, alpha_grid=(1e3,),
                   missing_as_nan=True)


def test_walk_forward_gam_with_the_smoothed_effect_is_unchanged_without_later_rows():
    first, last = date(2023, 12, 1), date(2024, 3, 25)
    raw = archive(first, last)
    index = pd.date_range('2023-11-20', '2024-03-26', freq='30min',
                          tz='UTC', inclusive='left')
    rng = np.random.default_rng(4)
    history = pd.Series(50000. + rng.normal(0, 500, len(index)), index=index)
    for day in (date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 20)):
        full_weather = read_temperature(connection_returning(raw), first, last)
        short_weather = read_temperature(connection_returning(truncated_at(raw, day)), first, day)
        boundary = ev.last_usable_slot_start(day)
        kwargs = {'training_start': date(2023, 12, 2)}
        full = walk_forward(history, day, day, two_smooth_gam,
                            builder=smoothed_gam_features(full_weather, A), **kwargs)
        short = walk_forward(history.loc[history.index <= boundary], day, day, two_smooth_gam,
                             builder=smoothed_gam_features(short_weather, A), **kwargs)
        pd.testing.assert_frame_equal(full, short)
        assert full[ev.MODEL].notna().all()

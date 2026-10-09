"""The 2025 test year is locked everywhere unless a caller passes final_test."""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import SeasonalNaive, naive_features, walk_forward
from load_forecast.gbm_model import GBMConfig, walk_forward_global
from load_forecast.kalman import walk_forward_kalman
from load_forecast.periods import TEST_LAST, VALIDATION_LAST, last_allowed_day
from load_forecast.weather_features import read_temperature


def history_around(day: date) -> pd.Series:
    index = pd.date_range(day - timedelta(days=40), day + timedelta(days=2), freq='30min',
                          tz='UTC', inclusive='left')
    return pd.Series(range(len(index)), index=index, dtype=float)


def test_the_default_limit_is_the_validation_year_and_the_unlocked_one_is_the_test_year():
    assert last_allowed_day() == VALIDATION_LAST == date(2024, 12, 31)
    assert last_allowed_day(final_test=True) == TEST_LAST == date(2025, 12, 31)


def test_every_entry_point_refuses_2025_by_default():
    history = history_around(date(2025, 1, 5))
    day = date(2024, 12, 20)
    with pytest.raises(ValueError, match='before 2025'):
        walk_forward(history, day, date(2025, 1, 1), SeasonalNaive)
    with pytest.raises(ValueError, match='before 2025'):
        walk_forward_kalman(history, day, date(2025, 1, 1), SeasonalNaive, builder=naive_features)
    with pytest.raises(ValueError, match='before 2025'):
        walk_forward_global(history, day, date(2025, 1, 1), GBMConfig(0.1, 15, 100, 10), builder=naive_features)
    with pytest.raises(ValueError, match='before 2025'):
        read_temperature(MagicMock(), date(2024, 12, 20), date(2025, 1, 1))


def test_every_entry_point_refuses_2026_even_when_unlocked():
    history = history_around(date(2026, 1, 5))
    day = date(2025, 12, 20)
    with pytest.raises(ValueError, match='before 2026'):
        walk_forward(history, day, date(2026, 1, 1), SeasonalNaive, final_test=True)
    with pytest.raises(ValueError, match='before 2026'):
        walk_forward_kalman(history, day, date(2026, 1, 1), SeasonalNaive,
                            builder=naive_features, final_test=True)
    with pytest.raises(ValueError, match='before 2026'):
        walk_forward_global(history, day, date(2026, 1, 1), GBMConfig(0.1, 15, 100, 10), builder=naive_features,
                            final_test=True)
    with pytest.raises(ValueError, match='before 2026'):
        read_temperature(MagicMock(), day, date(2026, 1, 1), final_test=True)


def test_unlocked_walk_forward_issues_the_last_day_of_the_test_year_and_nothing_later():
    day = TEST_LAST
    history = history_around(day)
    predictions = walk_forward(history, day, day, SeasonalNaive, builder=naive_features,
                               training_start=day - timedelta(days=30), final_test=True)
    assert len(predictions) == 48
    assert predictions.index.max() <= pd.Timestamp('2025-12-31 22:30', tz='UTC')
    pd.testing.assert_series_equal(predictions[ev.MODEL], ev.seasonal_naive(history, day),
                                   check_names=False, check_freq=False)

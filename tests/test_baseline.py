from datetime import date

import pandas as pd
import pytest

from load_forecast.baseline import END, HISTORY_START, START, evaluate_baseline, seasonal_naive
from load_forecast.cutoff import forecast_cutoff


def fixture_data():
    index = pd.date_range(HISTORY_START, END, freq='30min', inclusive='left')
    return pd.DataFrame({'consommation': 100., 'prevision_j1': 90.,
                         'source_dataset': 'eco2mix-national-cons-def'}, index=index)


def test_full_calendar_and_same_slot_comparison():
    data = fixture_data()
    # One absent day stays in the bootstrap calendar; missing history affects D+7 too.
    missing = pd.date_range(START, START + pd.Timedelta(days=1), freq='30min', inclusive='left')
    result, excluded = evaluate_baseline(data.drop(index=missing))
    assert result['n_expected'] == 366 * 48
    assert result['n_excluded'] == 96
    assert len(excluded) == 96
    assert result['model']['mae'] == 0
    assert result['rte']['mae'] == 10
    assert result['model']['n_slots'] == result['rte']['n_slots']
    assert result['bootstrap_7']['n_missing'] == 2
    assert result['bootstrap_14']['mean'] == -10
    assert result['day_types']['dst']['n_slots'] == 96
    assert result['day_types']['holiday']['n_days'] == 10


def test_reject_later_data_and_duplicates():
    data = fixture_data()
    later = pd.DataFrame({'consommation': [1]}, index=pd.DatetimeIndex([END]))
    with pytest.raises(ValueError, match='warmup'):
        evaluate_baseline(pd.concat([data, later]))
    with pytest.raises(ValueError, match='Duplicate'):
        evaluate_baseline(pd.concat([data, data.iloc[:1]]))
    with pytest.raises(ValueError, match='UTC'):
        evaluate_baseline(data.tz_convert('Europe/Paris'))


@pytest.mark.parametrize('day', [date(2024, 4, 7), date(2024, 11, 3)])
def test_local_lag_rejects_nonexistent_and_repeated_source_hour(day):
    history = fixture_data()['consommation']
    forecast = seasonal_naive(history, day, time_basis='local')
    local = forecast.index.tz_convert('Europe/Paris')
    assert forecast.loc[local.hour == 2].isna().all()
    assert forecast.loc[local.hour != 2].eq(100).all()
    assert seasonal_naive(history, day).eq(100).all()


@pytest.mark.parametrize('day,n', [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_local_lag_target_dst_and_publication(day, n):
    history = fixture_data()['consommation']
    published = pd.Series(pd.Timestamp(forecast_cutoff(day)), index=history.index)
    assert len(seasonal_naive(history, day, time_basis='local')) == n
    assert seasonal_naive(history, day, time_basis='local', published_at=published).isna().all()


def test_local_lag_follows_wall_clock_and_d2():
    history = fixture_data()['consommation']
    history[:] = history.index.hour
    day = date(2024, 4, 2)
    local = seasonal_naive(history, day, time_basis='local')
    utc = seasonal_naive(history, day)
    assert (local == (utc + 1) % 24).all()
    with pytest.raises(ValueError, match='time_basis'):
        seasonal_naive(history, day, time_basis='other')

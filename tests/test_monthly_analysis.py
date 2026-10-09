import pandas as pd
import pytest

from load_forecast.monthly_analysis import monthly_comparison


def test_common_slots_local_month_and_gain():
    index = pd.to_datetime(['2023-12-31T23:00Z', '2024-01-01T00:00Z',
                            '2024-01-02T00:00Z', '2024-01-31T23:00Z'])
    frame = pd.DataFrame({'actual': [100.] * 4, 'rte_d1': [90., 90., None, 90.],
                          'exp005': [110., 130., 1000., 80.],
                          'exp006': [100., 110., 1000., 90.],
                          'exp007': [105., None, 1000., 110.]}, index=index)
    result = monthly_comparison(frame)
    assert result.index.tolist() == ['2024-01', '2024-02']
    assert result.slots.tolist() == [1, 1]
    assert result.loc['2024-01', 'exp006_gain'] == 10
    assert result.loc['2024-02', 'exp005_bias'] == -20
    assert result.loc['2024-02', 'exp007_gain'] == 10


def test_dst_days_are_slot_weighted():
    spring = pd.date_range('2024-03-31', '2024-04-01', freq='30min',
                           tz='Europe/Paris', inclusive='left').tz_convert('UTC')
    ordinary = pd.date_range('2024-03-30', periods=48, freq='30min', tz='Europe/Paris')
    autumn = pd.date_range('2024-10-27', '2024-10-28', freq='30min',
                           tz='Europe/Paris', inclusive='left').tz_convert('UTC')
    index = spring.append(ordinary.tz_convert('UTC')).append(autumn)
    frame = pd.DataFrame({'actual': 100., 'rte_d1': 100., 'exp005': 100.,
                          'exp006': 100., 'exp007': 100.}, index=index)
    frame.loc[spring, 'exp005'] = 194.
    result = monthly_comparison(frame)
    assert len(spring) == 46 and len(autumn) == 50
    assert result.loc['2024-03', 'slots'] == 94
    assert result.loc['2024-03', 'exp005_mae'] == 46.
    assert result.loc['2024-10', 'slots'] == 50


def test_invalid_index_and_empty_common_set():
    frame = pd.DataFrame(columns=['actual', 'rte_d1', 'exp005', 'exp006', 'exp007'])
    with pytest.raises(ValueError, match='timezone-aware'):
        monthly_comparison(frame)
    frame.index = pd.DatetimeIndex([], tz='UTC')
    with pytest.raises(ValueError, match='No common slots'):
        monthly_comparison(frame)


def test_duplicate_timestamps_and_infinite_predictions():
    index = pd.date_range('2024-01-01', periods=2, freq='30min', tz='UTC')
    frame = pd.DataFrame({'actual': 100., 'rte_d1': 100., 'exp005': 110.,
                          'exp006': [100., float('inf')], 'exp007': 105.}, index=index)
    assert monthly_comparison(frame).slots.sum() == 1
    frame.index = pd.DatetimeIndex([index[0], index[0]])
    with pytest.raises(ValueError, match='Duplicate timestamps'):
        monthly_comparison(frame)

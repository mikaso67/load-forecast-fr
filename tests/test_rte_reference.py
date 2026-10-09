from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.cutoff import delivery_slots, forecast_cutoff
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference


@pytest.mark.parametrize('day', [date(2024, 1, 1), date(2024, 3, 31), date(2024, 10, 27)])
def test_prior_year_hour_mapping_and_truncation(day):
    # Cover the full calendar on the production half-hour grid.
    history = pd.date_range('2023-01-01', '2025-01-01', freq='30min',
                            inclusive='left', tz='Europe/Paris')
    utc = history.tz_convert('UTC')
    actual = pd.Series(1000., index=utc)
    rte = actual + history.hour + 1
    # Older and target-year errors must not enter the 2024 correction.
    older = pd.Series(1e6, index=pd.date_range('2022-01-01', periods=48, freq='h', tz='UTC'))
    actual = pd.concat([older, actual])
    rte = pd.concat([older + 1e5, rte])
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))
    target = pd.Series(1200., index=slots)
    expected = pd.Series(1200. - slots.tz_convert('Europe/Paris').hour - 1,
                         index=slots, name=RTE_STAR)
    full = prior_year_hourly_reference(actual, rte, target)
    pd.testing.assert_series_equal(full, expected)
    changed = actual.copy()
    changed.loc[changed.index.tz_convert('Europe/Paris').year == 2024] = -1e9
    pd.testing.assert_series_equal(full, prior_year_hourly_reference(changed, rte, target))
    before = actual.index < forecast_cutoff(day)
    pd.testing.assert_series_equal(full,
                                  prior_year_hourly_reference(actual[before], rte[before], target))


def test_local_year_boundary_and_invalid_indices():
    index = pd.date_range('2023-01-01', '2024-01-01', freq='30min',
                          inclusive='left', tz='Europe/Paris').tz_convert('UTC')
    actual = pd.Series(100., index=index)
    rte = actual + 10.
    target = pd.Series(200., index=pd.DatetimeIndex(['2023-12-31 23:00Z']))
    assert prior_year_hourly_reference(actual, rte, target).iloc[0] == 190
    with pytest.raises(ValueError, match='UTC'):
        prior_year_hourly_reference(actual.tz_localize(None), rte, target)
    with pytest.raises(ValueError, match='unique'):
        prior_year_hourly_reference(pd.concat([actual, actual]), rte, target)


@pytest.mark.parametrize('gap', ['month', 'hour', 'nan', 'infinite', 'off_grid'])
def test_prior_year_coverage_rejects_incomplete_or_unusable_pairs(gap):
    local = pd.date_range('2023-01-01', '2024-01-01', freq='30min',
                          inclusive='left', tz='Europe/Paris')
    actual = pd.Series(100., index=local.tz_convert('UTC'))
    rte = actual + 10.
    if gap == 'month':
        actual = actual.loc[local.month != 6]
    elif gap == 'hour':
        actual = actual.loc[local.hour != 2]
    elif gap == 'nan':
        rte.loc[local.month == 6] = float('nan')
    elif gap == 'infinite':
        actual.loc[local.month == 6] = float('inf')
    else:
        actual.index += pd.Timedelta(minutes=15)
        rte.index += pd.Timedelta(minutes=15)
    target = pd.Series(200., index=pd.DatetimeIndex(['2024-06-01 10:00Z']))
    with pytest.raises(ValueError, match='prior Paris year 2023 needs at least 95%'):
        prior_year_hourly_reference(actual, rte, target)


def test_coverage_threshold_applies_to_each_hour():
    local = pd.date_range('2023-01-01', '2024-01-01', freq='30min',
                          inclusive='left', tz='Europe/Paris')
    actual = pd.Series(100., index=local.tz_convert('UTC'))
    rte = actual + 10.
    target = pd.Series(200., index=pd.DatetimeIndex(['2024-06-01 10:00Z']))
    hour_slots = actual.index[local.hour == 12]
    # 36 missing out of 730 passes; 37 fails despite high overall coverage.
    actual.loc[hour_slots[:36]] = float('nan')
    assert prior_year_hourly_reference(actual, rte, target).iloc[0] == 190
    actual.loc[hour_slots[36]] = float('nan')
    with pytest.raises(ValueError, match='12:'):
        prior_year_hourly_reference(actual, rte, target)


def test_leap_year_coverage_threshold_uses_732_slots_per_hour():
    local = pd.date_range('2024-01-01', '2025-01-01', freq='30min',
                          inclusive='left', tz='Europe/Paris')
    counts = pd.Series(local.hour).value_counts().sort_index()
    assert counts.to_list() == [732] * 24
    actual = pd.Series(100., index=local.tz_convert('UTC'))
    rte = actual + 10.
    target = pd.Series(200., index=pd.DatetimeIndex(['2025-06-01 10:00Z']))
    hour_slots = actual.index[local.hour == 12]
    # 696/732 passes; 695/732 fails, while a hardcoded 695/730 would pass.
    actual.loc[hour_slots[:36]] = float('nan')
    assert prior_year_hourly_reference(actual, rte, target).iloc[0] == 190
    actual.loc[hour_slots[36]] = float('nan')
    with pytest.raises(ValueError, match='prior Paris year 2024.*12:'):
        prior_year_hourly_reference(actual, rte, target)


def test_dual_scores_common_slots_and_logged_metrics():
    index = pd.date_range('2023-12-31 23:00', periods=48 * 14, freq='30min', tz='UTC')
    frame = pd.DataFrame({ev.ACTUAL: 100., ev.MODEL: 104., ev.RTE: 102., RTE_STAR: 101.},
                         index=index)
    frame.iloc[0, frame.columns.get_loc(RTE_STAR)] = float('nan')
    frame.iloc[1, frame.columns.get_loc(ev.RTE)] = float('nan')
    result = score_backtest(frame, frame[ev.ACTUAL], frame[ev.RTE], frame[RTE_STAR])
    assert result['n_excluded'] == 2
    assert result['model']['n_slots'] == result['rte']['n_slots'] == result[RTE_STAR]['n_slots']
    assert result['model']['mae'] == 4
    assert result['rte']['mae'] == 2
    assert result[RTE_STAR]['mae'] == 1
    assert result[RTE_STAR]['rmse'] == 1
    assert result[RTE_STAR]['mape'] == pytest.approx(1)
    assert result['bootstrap_7']['mean'] == 2
    assert result['rte_star_bootstrap_7']['mean'] == 3
    assert result['rte_star_bootstrap_7']['verdict'] == 'significant'
    response = MagicMock()
    response.json.return_value = {'run': {'info': {
        'run_id': 'dual', 'artifact_uri': 'mlflow-artifacts:/0/dual/artifacts'}}}
    with patch('load_forecast.backtest.requests.Session') as session, \
            patch('load_forecast.backtest.save_backtest_plots', return_value=[]):
        post = session.return_value.__enter__.return_value.post
        post.return_value = response
        log_backtest(result, date(2024, 1, 1), date(2024, 1, 14), exp='test', frame=frame)
        batch = next(c.kwargs['json'] for c in post.call_args_list if 'metrics' in c.kwargs['json'])
        metrics = {m['key']: m['value'] for m in batch['metrics']}
        assert metrics['rte_star_mae'] == 1
        assert metrics['rte_star_bootstrap_7_mean'] == 3
        assert metrics['bootstrap_7_mean'] == 2

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import (
    SeasonalNaive,
    calendar_features,
    day_features,
    known_history,
    local_slots,
    log_backtest,
    naive_features,
    score_backtest,
    walk_forward,
)
from load_forecast.cutoff import forecast_cutoff


def history_for(first='2024-03-01', last='2024-04-09'):
    index = pd.date_range(first, last, freq='30min', tz='UTC', inclusive='left')
    return pd.Series(range(len(index)), index=index, dtype=float)


@pytest.mark.parametrize('day,n', [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_dst_naive_reproduces_evaluation(day, n):
    history = history_for(str(day - timedelta(days=30)), str(day + timedelta(days=1)))
    predictions = walk_forward(history, day, day, SeasonalNaive, builder=naive_features,
                               training_start=day - timedelta(days=30))
    assert len(predictions) == n
    assert str(predictions.index.tz) == 'UTC'
    pd.testing.assert_series_equal(predictions[ev.MODEL], ev.seasonal_naive(history, day),
                                   check_names=False, check_freq=False)
    assert predictions.refit_date.eq(day - timedelta(days=day.weekday())).all()
    if n == 50:
        assert (local_slots(predictions.index) == 4).sum() == 2


def test_truncation_daily_features_and_predictions():
    history = history_for()
    published = pd.Series(history.index + pd.Timedelta(hours=3), index=history.index)
    day = date(2024, 4, 7)
    # Include a late revision to an otherwise old, usable lag.
    published.iloc[100] = pd.Timestamp(forecast_cutoff(day))
    truncated = history.loc[published < forecast_cutoff(day)]
    kwargs = {'builder': naive_features, 'published_at': published,
              'training_start': date(2024, 3, 1)}
    full = walk_forward(history, day, day, SeasonalNaive, **kwargs)
    short = walk_forward(truncated, day, day, SeasonalNaive, **kwargs)
    pd.testing.assert_frame_equal(full, short)
    pd.testing.assert_frame_equal(day_features(history, day, naive_features, published),
                                  day_features(truncated, day, naive_features, published))


def test_weekly_fit_is_expanding_and_uses_d2():
    history = history_for()
    fits = []

    class MeanModel:
        def fit(self, x, y):
            fits.append(y.index)
            self.mean = y.mean()

        def predict(self, x):
            return [self.mean] * len(x)

    first, last = date(2024, 4, 1), date(2024, 4, 8)
    output = walk_forward(history, first, last, MeanModel, training_start=date(2024, 3, 1))
    assert len(fits) == 96
    for batch, day in [(fits[:48], first), (fits[48:], last)]:
        for index in batch:
            assert index.max() <= ev.last_usable_slot_start(day)
    assert sum(map(len, fits[48:])) > sum(map(len, fits[:48]))
    assert output.loc['2024-04-02', 'refit_date'].eq(first).all()
    assert output.refit_cutoff.max() == forecast_cutoff(last)


def test_year_naive_matches_evaluation_and_scoring():
    history = history_for('2023-12-01', '2024-12-31 23:00')
    first, last = date(2024, 1, 1), date(2024, 12, 31)
    output = walk_forward(history, first, last, SeasonalNaive, builder=naive_features,
                          training_start=date(2023, 12, 1))
    expected = pd.concat([ev.seasonal_naive(history, day)
                          for day in pd.date_range(first, last).date])
    pd.testing.assert_series_equal(output[ev.MODEL], expected, check_names=False, check_freq=False)
    result = score_backtest(output, history, history - 1)
    assert result['n_excluded'] == 0
    assert result['model']['n_slots'] == result['rte']['n_slots'] == 17568
    assert result['rte']['mae'] == 1


def test_input_and_feature_contract():
    history = history_for()
    day = date(2024, 4, 1)
    cutoff = pd.Timestamp(forecast_cutoff(day))
    published = pd.Series(cutoff, index=history.index)
    assert known_history(history, day, published).empty
    pd.testing.assert_frame_equal(day_features(history, day, calendar_features),
                                  day_features(history.iloc[:1], day, calendar_features))
    with pytest.raises(ValueError, match='before 2025'):
        walk_forward(history, day, date(2025, 1, 1), SeasonalNaive)
    with pytest.raises(ValueError, match='unique UTC'):
        known_history(history.tz_localize(None), day, None)
    with pytest.raises(ValueError, match='forbidden'):
        day_features(history, day, lambda slots, h, d: pd.DataFrame({'rte': 1}, index=slots))


@pytest.mark.parametrize('author', [None, 'portfolio-owner'])
@pytest.mark.parametrize('ticket', [None, 'example'])
def test_mlflow_tags_and_metrics(author, ticket, monkeypatch):
    monkeypatch.delenv('LFF_AUTHOR', raising=False)
    if author is not None:
        monkeypatch.setenv('LFF_AUTHOR', author)
    result = {'model': {'mae': 2}, 'rte': {'mae': 1}, 'n_excluded': 0,
              'day_types': {'ordinary': {'model': 2, 'rte': 1}},
              'bootstrap_7': {'mean': 1, 'verdict': 'significant'},
              'bootstrap_14': {'mean': 1, 'verdict': 'significant'}}
    response = MagicMock()
    response.json.return_value = {'run': {'info': {'run_id': 'test-run',
                                                 'artifact_uri':
                                                 'mlflow-artifacts:/0/test-run/artifacts'}}}
    index = pd.date_range('2024-01-01', periods=48 * 14, freq='30min', tz='UTC')
    frame = pd.DataFrame({ev.ACTUAL: 100, ev.MODEL: 102, ev.RTE: 101}, index=index)
    with patch('load_forecast.backtest.requests.Session') as session:
        post = session.return_value.__enter__.return_value.post
        post.return_value = response
        assert log_backtest(result, date(2024, 1, 1), date(2024, 1, 15), frame=frame,
                            ticket=ticket, exp='EXP-001',
                            params={'model': 'ols', 'seed': 7}) == 'test-run'
        tags = {t['key']: t['value'] for t in post.call_args_list[0].kwargs['json']['tags']}
        expected = {'author': author or 'developer', 'exp': 'EXP-001',
                    'backtest_start': '2024-01-01', 'backtest_end': '2024-01-15'}
        if ticket is not None:
            expected['ticket'] = ticket
        assert tags == expected
        batch = next(c.kwargs['json'] for c in post.call_args_list if 'metrics' in c.kwargs['json'])
        assert batch['params'] == [{'key': 'model', 'value': 'ols'}, {'key': 'seed', 'value': '7'}]
        assert not any('verdict' in m['key'] for m in batch['metrics'])
        assert post.call_args_list[-1].kwargs['json']['status'] == 'FINISHED'


@pytest.mark.parametrize('day', [date(2024, 4, 1), date(2024, 4, 3)])
def test_truncation_of_fitted_model(day):
    history = history_for()
    published = pd.Series(history.index + pd.Timedelta(hours=3), index=history.index)
    published.iloc[500] = pd.Timestamp(forecast_cutoff(day)) + pd.Timedelta(hours=1)

    def latest_features(slots, known, target_day):
        value = float('nan') if known.empty else known.iloc[-1]
        return pd.DataFrame({'last_known': value}, index=slots)

    class FittedModel:
        def fit(self, x, y):
            self.level = y.mean() + x.last_known.mean()

        def predict(self, x):
            return self.level + x.last_known

    kwargs = {'builder': latest_features, 'published_at': published,
              'training_start': date(2024, 3, 1)}
    truncated = history.loc[published < forecast_cutoff(day)]
    pd.testing.assert_frame_equal(walk_forward(history, day, day, FittedModel, **kwargs),
                                  walk_forward(truncated, day, day, FittedModel, **kwargs))


@pytest.mark.parametrize('day,n', [
    (date(2024, 4, 1), 48), (date(2024, 4, 3), 48),
    (date(2024, 3, 31), 46), (date(2024, 10, 27), 50),
])
def test_truncation_without_publication_times(day, n):
    start = day - timedelta(days=30)
    history = history_for(str(start), str(day + timedelta(days=1)))
    boundary = ev.last_usable_slot_start(day)
    truncated = history.loc[history.index <= boundary]

    def latest_features(slots, known, target_day):
        value = float('nan') if known.empty else known.iloc[-1]
        return pd.DataFrame({'last_known': value}, index=slots)

    class FittedModel:
        def fit(self, x, y):
            self.level = y.mean() + x.last_known.mean()

        def predict(self, x):
            return self.level + x.last_known

    kwargs = {'builder': latest_features, 'published_at': None, 'training_start': start}
    full = walk_forward(history, day, day, FittedModel, **kwargs)
    short = walk_forward(truncated, day, day, FittedModel, **kwargs)
    assert len(full) == n
    assert str(full.index.tz) == 'UTC'
    assert full[ev.MODEL].notna().all()
    pd.testing.assert_frame_equal(full, short)
    features = day_features(history, day, latest_features, published_at=None)
    pd.testing.assert_frame_equal(features,
                                  day_features(truncated, day, latest_features, published_at=None))
    assert features.last_known.eq(history.loc[boundary]).all()

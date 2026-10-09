from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from load_forecast import evaluation as ev
from load_forecast.api import app as routes
from load_forecast.api import service
from load_forecast.backtest import walk_forward
from load_forecast.cutoff import forecast_cutoff
from load_forecast.linear_model import SlotOLS, calendar_ols_features

client = TestClient(routes.app)


def synthetic_history():
    index = pd.date_range('2023-01-01', '2024-11-01', freq='30min', tz='UTC')
    return pd.Series(40000 + 3000 * np.sin(np.arange(len(index)) / 48), index=index)


@pytest.mark.parametrize('day,count', [
    (date(2024, 1, 17), 48), (date(2024, 3, 31), 46), (date(2024, 10, 27), 50),
])
def test_forecast_grid_and_runner_equivalence(day, count):
    history = synthetic_history()
    with patch.object(routes, 'read_history', return_value=history):
        response = client.get(f'/forecast/{day}')
    assert response.status_code == 200
    result = response.json()
    assert result['model_name'] == service.MODEL_NAME
    assert result['refit_date'] == str(service.refit_date(day))
    assert pd.Timestamp(result['cutoff_utc']) == forecast_cutoff(day)
    assert pd.Timestamp(result['refit_cutoff_utc']) == forecast_cutoff(service.refit_date(day))
    slots = result['forecasts']
    assert len(slots) == count
    utc = pd.DatetimeIndex([row['timestamp_utc'] for row in slots])
    assert utc.is_unique and str(utc.tz) == 'UTC'
    for row in slots:
        assert pd.Timestamp(row['timestamp_utc']) == pd.Timestamp(row['timestamp_paris'])
        assert pd.Timestamp(row['timestamp_paris']).date() == day
    expected = walk_forward(history, day, day, SlotOLS, builder=calendar_ols_features)
    np.testing.assert_allclose([row['forecast_mw'] for row in slots], expected[ev.MODEL])
    if count == 50:
        repeated = [row['timestamp_paris'] for row in slots if 'T02:' in row['timestamp_paris']]
        assert len(repeated) == 4
        assert any(stamp.endswith('+02:00') for stamp in repeated)
        assert any(stamp.endswith('+01:00') for stamp in repeated)


def test_d2_truncation_and_post_cutoff_poison():
    history = synthetic_history()
    day = date(2024, 3, 31)
    limit = ev.last_usable_slot_start(service.refit_date(day))
    restricted = history.loc[:limit]
    poisoned = history.copy()
    poisoned.loc[poisoned.index > limit] = 1e12
    pd.testing.assert_frame_equal(service.predict(day, restricted), service.predict(day, poisoned))


@pytest.mark.parametrize('table', ['analytics.load_30min', 'sandbox.load_30min'])
def test_reader_query_bounds_and_columns(table, monkeypatch):
    monkeypatch.setenv('LFF_CLEAN_TABLE', table)
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [
        (datetime(2023, 12, 1, tzinfo=UTC), 45000),
    ]
    day = date(2024, 1, 17)
    with patch.object(service, 'connect_database', return_value=connection):
        history = service.read_history(day)
    query, params = connection.execute.call_args.args
    query_text = query.as_string()
    schema, name = table.split('.')
    assert f'FROM "{schema}"."{name}"' in query_text
    assert 'prevision' not in query_text and 'labels' not in query_text
    assert params['last_slot'] == ev.last_usable_slot_start(date(2024, 1, 15))
    assert params['start'] == pd.Timestamp(service.TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
    assert str(history.index.tz) == 'UTC' and history.iloc[0] == 45000


@pytest.mark.parametrize('table', ['load_30min', '.load_30min', 'schema.', 'a.b.c'])
def test_invalid_table_never_connects(table, monkeypatch):
    monkeypatch.setenv('LFF_CLEAN_TABLE', table)
    with patch.object(service, 'connect_database') as connect:
        with pytest.raises(ValueError, match='schema.table'):
            service.read_history(date(2024, 1, 17))
        connect.assert_not_called()


def test_table_is_quoted_as_identifiers(monkeypatch):
    monkeypatch.setenv('LFF_CLEAN_TABLE', 'schema.table; SELECT secret')
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = []
    with patch.object(service, 'connect_database', return_value=connection):
        service.read_history(date(2024, 1, 17))
    query = connection.execute.call_args.args[0].as_string()
    assert 'FROM "schema"."table; SELECT secret"' in query


@pytest.mark.parametrize('database,mlflow', [(True, True), (True, False), (False, True)])
def test_health(database, mlflow):
    with (patch.object(routes, 'database_reachable', return_value=database),
          patch.object(routes, 'mlflow_reachable', return_value=mlflow)):
        response = client.get('/health')
    assert response.status_code == (200 if database and mlflow else 503)
    assert response.json()['database'] == ('ok' if database else 'unavailable')
    assert response.json()['mlflow'] == ('ok' if mlflow else 'unavailable')


def test_dependency_errors_do_not_expose_secrets():
    secret = 'sensitive connection details'
    with (patch.object(routes, 'connect_database', side_effect=RuntimeError(secret)),
          patch.object(routes.requests, 'get', side_effect=RuntimeError(secret))):
        response = client.get('/health')
    assert response.status_code == 503 and secret not in response.text
    with patch.object(routes, 'read_history', side_effect=RuntimeError(secret)):
        response = client.get('/forecast/2024-01-17')
    assert response.status_code == 503 and response.json() == {'detail': 'Forecast unavailable'}


def test_successful_dependency_probes():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (1,)
    with patch.object(routes, 'connect_database', return_value=connection):
        assert routes.database_reachable()
    with patch.object(routes.requests, 'get', return_value=MagicMock(status_code=200)) as get:
        assert routes.mlflow_reachable()
        assert get.call_args.kwargs['timeout'] == 5


def test_invalid_future_and_early_dates_do_not_read_database():
    with (patch.object(routes, 'now_utc', return_value=datetime(2024, 1, 1, tzinfo=UTC)),
          patch.object(routes, 'read_history') as read):
        assert client.get('/forecast/2024-01-03').status_code == 400
        assert client.get('/forecast/2021-03-23').status_code == 400
        assert client.get('/forecast/0001-01-01').status_code == 400
        assert client.get('/forecast/not-a-date').status_code == 422
        read.assert_not_called()


def test_cutoff_equality_is_allowed_and_clock_is_utc():
    day = date(2024, 1, 17)
    assert routes.now_utc().tzinfo == UTC
    with (patch.object(routes, 'now_utc', return_value=forecast_cutoff(day)),
          patch.object(routes, 'read_history', return_value=synthetic_history())):
        assert client.get(f'/forecast/{day}').status_code == 200


def test_empty_incomplete_and_nonfinite_history():
    day = date(2024, 1, 17)
    with pytest.raises(ValueError, match='No training'):
        service.predict(day, pd.Series([], index=pd.DatetimeIndex([], tz='UTC'), dtype=float))
    with pytest.raises(ValueError, match='Insufficient'):
        service.predict(day, synthetic_history().iloc[:1])
    poisoned = synthetic_history()
    poisoned.iloc[0] = float('inf')
    with pytest.raises(ValueError):
        service.predict(day, poisoned)

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from load_forecast.api import app as routes
from load_forecast.api import service
from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.serving import ServingAuthority, delivery_deadline

client = TestClient(routes.app)


def synthetic_history():
    index = pd.date_range('2023-01-01', '2024-11-01', freq='30min', tz='UTC')
    return pd.Series(40000 + 3000 * np.sin(np.arange(len(index)) / 48), index=index)


def make_values(day: date, point: float = 45000.0, intervals: bool = True):
    slots = list(delivery_slots(day, '30min'))
    return [{
        'value_time': slot.isoformat(),
        'local_slot': slot.astimezone(PARIS).hour * 2 + slot.astimezone(PARIS).minute // 30,
        'point': point,
        'lower': point - 500.0 if intervals else None,
        'upper': point + 500.0 if intervals else None,
        'n_pool': 60 if intervals else None,
    } for slot in slots]


def make_primary_archive(run_id: str = 'run1', day: date = date(2024, 4, 3)):
    freeze = forecast_cutoff(day)
    return {
        'kind': 'primary', 'id': run_id, 'delivery_day': day.isoformat(), 'variant': 'B',
        'start': {
            'run_id': run_id, 'delivery_day': day.isoformat(), 'kind': 'primary', 'variant': 'B',
            'freeze_time': freeze.isoformat(), 'started_at': freeze.isoformat(),
            'code_commit': 'abc123',
        },
        'result': {
            'run_id': run_id, 'completed_at': freeze.isoformat(), 'status': 'complete',
            'refit_day': '2024-04-01', 'mlflow_run_id': 'tracking1',
            'kalman_quarter': '2024Q2', 'weather_status': 'primary',
            'last_consumption_slot': (freeze - timedelta(hours=2, minutes=30)).isoformat(),
            'max_ingested_at': (freeze - timedelta(seconds=1)).isoformat(),
            'feature_hash': 'features', 'flags': {'stale_models': False},
            'model_name': 'EXP-009 ML-Poly aggregate',
        },
        'values': make_values(day),
        'experts': [{'value_time': row['value_time'], 'expert': expert,
                     'value': row['point'], 'backup': None}
                    for row in make_values(day) for expert in ('gam', 'gbm', 'kalman', 'linear')],
        'weather': [{'selection_hash': 'selection', 'run_init_time':
                     freeze.replace(hour=0, minute=0).isoformat(),
                     'file_last_modified': (freeze - timedelta(minutes=1)).isoformat(),
                     'index_last_modified': (freeze - timedelta(minutes=1)).isoformat(),
                     'end_step': 24, 'variable': 'TMP', 'point': 'Paris', 'message_hash': 'msg'}],
        'states': [{'component': 'absorbed', 'state_key': '2024-04-01', 'payload': {'snapshot': 's1'}},
                   {'component': 'aggregation', 'state_key': 'shared',
                    'payload': {'weights': [0.25] * 4, 'regret': [0.0] * 4, 'squared_regret': [0.0] * 4}},
                   {'component': 'kalman', 'state_key': '0', 'payload': {'mean': [0.0], 'covariance': [[1.0]]}}],
    }


def make_fallback_archive(day: date = date(2024, 4, 3), fallback_id: str = 'fallback1'):
    return {
        'kind': 'fallback', 'id': fallback_id, 'delivery_day': day.isoformat(),
        'prepared_at': (forecast_cutoff(day) - timedelta(hours=23)).isoformat(),
        'models': ['EXP-002 calendar OLS'],
        'completeness': {'complete': True},
        'values': make_values(day, point=44000.0, intervals=False),
    }


@pytest.mark.parametrize('day,count', [
    (date(2024, 1, 17), 48),
    (date(2024, 3, 31), 46),
    (date(2024, 10, 27), 50),
])
def test_forecast_served_primary(tmp_path, monkeypatch, day, count):
    monkeypatch.setenv('LFF_SERVING_DIR', str(tmp_path))
    authority = ServingAuthority(tmp_path, clock=lambda: forecast_cutoff(day))
    archive = make_primary_archive(run_id='run_primary', day=day)
    choice = authority.publish_primary(archive)

    response = client.get(f'/forecast/{day}')
    assert response.status_code == 200
    data = response.json()

    assert data['delivery_date'] == str(day)
    assert data['kind'] == 'primary'
    assert data['model_or_fallback'] == 'EXP-009 ML-Poly aggregate'
    assert data['choice_hash'] == choice['choice_hash']
    assert data['interval'] == {'level': 0.8, 'type': 'seasonal_split_conformal'}

    slots = data['values']
    assert len(slots) == count
    assert len(data['forecasts']) == count

    utc = pd.DatetimeIndex([row['timestamp_utc'] for row in slots])
    assert utc.is_unique and str(utc.tz) == 'UTC'
    for row in slots:
        assert pd.Timestamp(row['timestamp_utc']) == pd.Timestamp(row['timestamp_paris'])
        assert pd.Timestamp(row['timestamp_paris']).date() == day
        assert row['forecast_mw'] == 45000.0
        assert row['lower_mw'] == 44500.0
        assert row['upper_mw'] == 45500.0
        assert row['n_pool'] == 60

    if count == 50:
        repeated = [row['timestamp_paris'] for row in slots if 'T02:' in row['timestamp_paris']]
        assert len(repeated) == 4
        assert any(stamp.endswith('+02:00') for stamp in repeated)
        assert any(stamp.endswith('+01:00') for stamp in repeated)


def test_forecast_served_fallback(tmp_path, monkeypatch):
    day = date(2024, 4, 3)
    monkeypatch.setenv('LFF_SERVING_DIR', str(tmp_path))
    authority = ServingAuthority(tmp_path, clock=lambda: delivery_deadline(day))
    fallback = make_fallback_archive(day=day)
    authority.prepare_fallback(fallback)
    choice = authority.deadline_check(day)

    response = client.get(f'/forecast/{day}')
    assert response.status_code == 200
    data = response.json()

    assert data['delivery_date'] == str(day)
    assert data['kind'] == 'fallback'
    assert data['model_or_fallback'] == 'EXP-002 calendar OLS'
    assert data['choice_hash'] == choice['choice_hash']
    assert data['interval'] is None
    assert len(data['values']) == 48
    assert all(row['lower_mw'] is None and row['upper_mw'] is None for row in data['values'])


def test_forecast_served_none(tmp_path, monkeypatch):
    day = date(2024, 4, 3)
    monkeypatch.setenv('LFF_SERVING_DIR', str(tmp_path))
    authority = ServingAuthority(tmp_path, clock=lambda: delivery_deadline(day))
    choice = authority.deadline_check(day)
    assert choice['kind'] == 'none'

    response = client.get(f'/forecast/{day}')
    assert response.status_code == 200
    data = response.json()

    assert data['delivery_date'] == str(day)
    assert data['kind'] == 'none'
    assert data['model_or_fallback'] is None
    assert data['choice_hash'] == choice['choice_hash']
    assert data['interval'] is None
    assert data['values'] == []


def test_forecast_missing_day(tmp_path, monkeypatch):
    monkeypatch.setenv('LFF_SERVING_DIR', str(tmp_path))
    response = client.get('/forecast/2024-04-03')
    assert response.status_code == 404
    assert response.json() == {'detail': 'No forecast found for delivery date'}


def test_forecast_corrupt_choice(tmp_path, monkeypatch):
    day = date(2024, 4, 3)
    monkeypatch.setenv('LFF_SERVING_DIR', str(tmp_path))
    choice_dir = tmp_path / 'choice'
    choice_dir.mkdir(parents=True)
    (choice_dir / f'{day.isoformat()}.json').write_text('{"bad": "json', encoding='utf-8')

    response = client.get(f'/forecast/{day}')
    assert response.status_code == 500
    assert response.json() == {'detail': 'Serving choice corrupted or invalid'}


def test_runs_endpoint(monkeypatch):
    day = date(2024, 4, 3)
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.description = [
        ('run_id',), ('delivery_day',), ('freeze_time',), ('kind',), ('started_at',),
        ('code_commit',), ('completed_at',), ('status',), ('refit_day',),
        ('mlflow_run_id',), ('kalman_quarter',), ('weather_status',),
        ('last_consumption_slot',), ('max_ingested_at',), ('feature_hash',), ('flags',),
    ]
    mock_cursor.fetchall.return_value = [
        (
            'run1', day, datetime(2024, 4, 2, 10, 0, tzinfo=UTC), 'primary',
            datetime(2024, 4, 2, 10, 1, tzinfo=UTC), 'abc123',
            datetime(2024, 4, 2, 10, 5, tzinfo=UTC), 'complete', date(2024, 4, 1),
            'tracking1', '2024Q2', 'primary',
            datetime(2024, 4, 2, 7, 30, tzinfo=UTC),
            datetime(2024, 4, 2, 9, 59, tzinfo=UTC), 'features',
            '{"stale_models": false}',
        ),
    ]

    with patch.object(service, 'connect_database', return_value=mock_conn):
        response = client.get(f'/runs/{day}')
    assert response.status_code == 200
    result = response.json()
    assert len(result) == 1
    assert result[0]['run_id'] == 'run1'
    assert result[0]['status'] == 'complete'
    assert result[0]['flags'] == {'stale_models': False}


def test_runs_endpoint_empty(monkeypatch):
    day = date(2024, 4, 3)
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.description = [
        ('run_id',), ('delivery_day',), ('freeze_time',), ('kind',), ('started_at',),
        ('code_commit',), ('completed_at',), ('status',), ('refit_day',),
        ('mlflow_run_id',), ('kalman_quarter',), ('weather_status',),
        ('last_consumption_slot',), ('max_ingested_at',), ('feature_hash',), ('flags',),
    ]
    mock_cursor.fetchall.return_value = []

    with patch.object(service, 'connect_database', return_value=mock_conn):
        response = client.get(f'/runs/{day}')
    assert response.status_code == 200
    assert response.json() == []


def test_runs_endpoint_database_error():
    with patch.object(service, 'connect_database', side_effect=RuntimeError('db secret')):
        response = client.get('/runs/2024-04-03')
    assert response.status_code == 503
    assert response.json() == {'detail': 'Database unavailable'}


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
    with patch.object(service, 'connect_database', side_effect=RuntimeError(secret)):
        response = client.get('/runs/2024-01-17')
    assert response.status_code == 503 and response.json() == {'detail': 'Database unavailable'}


def test_successful_dependency_probes():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (1,)
    with patch.object(routes, 'connect_database', return_value=connection):
        assert routes.database_reachable()
    with patch.object(routes.requests, 'get', return_value=MagicMock(status_code=200)) as get:
        assert routes.mlflow_reachable()
        assert get.call_args.kwargs['timeout'] == 5


# Legacy helper unit tests


def test_d2_truncation_and_post_cutoff_poison():
    history = synthetic_history()
    day = date(2024, 3, 31)
    limit = service.ev.last_usable_slot_start(service.refit_date(day))
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
    assert params['last_slot'] == service.ev.last_usable_slot_start(date(2024, 1, 15))
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

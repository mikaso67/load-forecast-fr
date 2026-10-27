"""Production MLflow events and model artifacts, using the existing REST client dependency."""

import os
import pickle
import time

import requests

from load_forecast.tracking import mlflow_tracking_uri


def start_tracking(start: dict) -> str:
    """Create a production run before work; a killed process leaves a visible running record."""
    tags = {'author': os.environ.get('LFF_AUTHOR', 'developer'), 'exp': 'production',
            'backtest_start': start['delivery_day'], 'backtest_end': start['delivery_day'],
            'delivery_day': start['delivery_day'], 'variant': start['variant'],
            'forecast_run_id': start['run_id'], 'code_commit': start['code_commit'],
            'status': 'RUNNING'}
    response = requests.post(f'{mlflow_tracking_uri()}/api/2.0/mlflow/runs/create', json={
        'experiment_id': os.environ['LFF_PRODUCTION_EXPERIMENT_ID'],
        'start_time': int(time.time() * 1000),
        'tags': [{'key': key, 'value': value} for key, value in tags.items()]}, timeout=5)
    response.raise_for_status()
    return response.json()['run']['info']['run_id']


def finish_tracking(run_id: str, *, models=None, kalman=None, status: str = 'FINISHED') -> None:
    """Log the model/filter identity and terminate a production run."""
    uri = mlflow_tracking_uri()
    if models is not None:
        response = requests.get(f'{uri}/api/2.0/mlflow/runs/get',
                                params={'run_id': run_id}, timeout=5)
        response.raise_for_status()
        artifact_uri = response.json()['run']['info']['artifact_uri']
        if not artifact_uri.startswith('mlflow-artifacts:/'):
            raise ValueError('Production MLflow must proxy artifacts')
        folder = artifact_uri.removeprefix('mlflow-artifacts:').strip('/')
        payload = pickle.dumps({'models': models, 'kalman': kalman}, protocol=pickle.HIGHEST_PROTOCOL)
        response = requests.put(f'{uri}/api/2.0/mlflow-artifacts/artifacts/{folder}/models.pkl',
                                data=payload, timeout=30)
        response.raise_for_status()
    response = requests.post(f'{uri}/api/2.0/mlflow/runs/set-tag', json={
        'run_id': run_id, 'key': 'status', 'value': status}, timeout=5)
    response.raise_for_status()
    response = requests.post(f'{uri}/api/2.0/mlflow/runs/update', json={
        'run_id': run_id, 'status': status, 'end_time': int(time.time() * 1000)}, timeout=5)
    response.raise_for_status()

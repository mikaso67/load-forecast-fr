"""replay frozen existing choices, without selection or post-2024 reads."""

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from run_exp002 import QUERY, TRAINING_START
from run_exp003 import WEATHER_FIRST
from run_exp006 import HALF_LIFE_HOURS, make_gam1, make_gam2
from run_exp007 import FILTER_MODE, check_preregistration

from load_forecast.backtest import walk_forward
from load_forecast.database import clean_query, clean_table
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import lagged_gam_features
from load_forecast.kalman import walk_forward_kalman
from load_forecast.lag_model import lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.monthly_analysis import FORECASTS, monthly_comparison
from load_forecast.smoothed_temperature import smoothed_gam_features, smoothing_constant
from load_forecast.temperature_model import temperature_ols_features, with_calendar_backup
from load_forecast.tracking import backtest_tags, mlflow_tracking_uri
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/analysis/comparison_047')


def main() -> None:
    check_preregistration()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for year in (2023, 2024):
        first, last = date(year, 1, 1), date(year, 12, 31)
        end = pd.Timestamp(last + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        start = pd.Timestamp(TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
        with connect_database() as connection:
            cursor = connection.execute(clean_query(QUERY), {'start': start, 'end': end})
            data = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
            temperature = read_temperature(connection, WEATHER_FIRST, last)
        data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('value_time'), utc=True))
        assert data.index.max() < end
        if (temperature.weather_status == 'not_collected').any():
            raise RuntimeError('Uncollected weather')
        cache = OUTPUT / f'predictions_{year}.csv'
        if cache.exists():
            frame = pd.read_csv(cache, index_col=0, parse_dates=True)
            frame.index = pd.to_datetime(frame.index, utc=True)
            print('Using diagnostic cache', cache, flush=True)
        else:
            print('Replaying frozen models', year, flush=True)
            kalman = walk_forward_kalman(data.consommation, first, last, make_gam1,
                                         builder=lagged_gam_features(temperature),
                                         training_start=TRAINING_START, modes=(FILTER_MODE,))
            print('GAM and Kalman complete', year, flush=True)
            smooth = walk_forward(data.consommation, first, last, make_gam2,
                                  builder=smoothed_gam_features(
                                      temperature, smoothing_constant(HALF_LIFE_HOURS)),
                                  training_start=TRAINING_START)
            plain, corrected = kalman.gam, kalman.corrected[FILTER_MODE]
            if year == 2024:
                calendar = walk_forward(data.consommation, first, last, SlotOLS,
                                        builder=calendar_ols_features, training_start=TRAINING_START)
                weather = walk_forward(data.consommation, first, last, SlotOLS,
                                       builder=temperature_ols_features(temperature),
                                       training_start=TRAINING_START)
                exp003 = with_calendar_backup(weather, calendar)
                lagged = walk_forward(data.consommation, first, last, SlotOLS,
                                      builder=lagged_ols_features(temperature),
                                      training_start=TRAINING_START)
                exp004 = with_calendar_backup(lagged, exp003)
                plain = with_calendar_backup(plain, exp004)
                smooth = with_calendar_backup(smooth, plain)
                corrected = with_calendar_backup(corrected, plain)
            frame = pd.DataFrame({'actual': data.consommation.reindex(plain.index),
                                  'rte_d1': data.prevision_j1.reindex(plain.index),
                                  'exp005': plain.model, 'exp006': smooth.model,
                                  'exp007': corrected.model})
            frame.to_csv(cache, index_label='value_time_utc')
        if not ((frame.index >= pd.Timestamp(first, tz='Europe/Paris').tz_convert('UTC'))
                & (frame.index < end)).all():
            raise RuntimeError('Predictions outside diagnostic year')
        table = monthly_comparison(frame)
        table.to_csv(OUTPUT / f'monthly_{year}.csv')
        print(table.round(2).to_string(), flush=True)
        kept = frame.dropna()
        summary = {'slots': len(kept), 'excluded_slots': len(frame) - len(kept)}
        for name in FORECASTS:
            error = kept[name] - kept.actual
            summary[name] = {'mae': float(error.abs().mean()), 'bias': float(error.mean())}
            url = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
            tags = backtest_tags(first, last, name.upper().replace('EXP', 'EXP-'))
            tags['purpose'] = 'diagnostic replay, no tuning'
            response = requests.post(f'{url}/runs/create', json={
                'experiment_id': '0', 'start_time': int(time.time() * 1000),
                'tags': [{'key': k, 'value': v} for k, v in tags.items()]}, timeout=30)
            response.raise_for_status()
            info = response.json()['run']['info']
            run_id = info['run_id']
            params = {'half_life_hours': HALF_LIFE_HOURS, 'filter_mode': FILTER_MODE,
                      'smoothing_weight': 100, 'coverage': 'inner' if year == 2023
                      else 'full backup chain', 'source': clean_table() + '; raw.gfs_days; raw.gfs_points'}
            response = requests.post(f'{url}/runs/log-batch', json={
                'run_id': run_id, 'params': [{'key': k, 'value': str(v)} for k, v in params.items()],
                'metrics': [{'key': k, 'value': v, 'timestamp': int(time.time() * 1000), 'step': 0}
                            for k, v in (summary[name] | {'n_slots': len(kept)}).items()]}, timeout=30)
            response.raise_for_status()
            uri = info['artifact_uri']
            if not uri.startswith('mlflow-artifacts:/'):
                raise RuntimeError('Expected local MLflow artifact proxy')
            artifact_path = uri.removeprefix('mlflow-artifacts:').strip('/')
            for path in (cache, OUTPUT / f'monthly_{year}.csv'):
                with path.open('rb') as content:
                    response = requests.put(
                        f'{mlflow_tracking_uri()}/api/2.0/mlflow-artifacts/artifacts/'
                        f'{artifact_path}/{path.name}', data=content, timeout=30)
                response.raise_for_status()
            response = requests.post(f'{url}/runs/update', json={
                'run_id': run_id, 'status': 'FINISHED', 'end_time': int(time.time() * 1000)}, timeout=30)
            response.raise_for_status()
            summary[name]['run_id'] = run_id
        (OUTPUT / f'summary_{year}.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()

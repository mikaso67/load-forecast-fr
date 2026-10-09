"""EXP-005: EXP-004 with a smooth temperature effect instead of HDD and CDD (GAM), 2024 only."""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from run_exp002 import TRAINING_START, load, paired_against_reference
from run_exp003 import SOURCE_QUERY, WEATHER_FIRST, day_type_table, monthly_diagnostic

from load_forecast import evaluation as ev
from load_forecast.backtest import (
    day_features,
    known_history,
    local_slots,
    log_backtest,
    score_backtest,
    walk_forward,
)
from load_forecast.backtest_plots import BLUE, GREY
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import TEMPERATURE_COLUMN, SlotGAM, lagged_gam_features
from load_forecast.labels import read_labels
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.temperature_model import (
    COOLING_BASE_C,
    HEATING_BASE_C,
    temperature_ols_features,
    with_calendar_backup,
)
from load_forecast.tracking import mlflow_tracking_uri
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/experiments/EXP-005')
FIGURE = Path('docs/experiments/EXP-005_temperature_curves.png')
EXP003_MAE, EXP004_MAE = 1719.06, 1015.70
DEGREES_OF_FREEDOM = 8
# Penalty weight shared by the 48 slot models, chosen on 2023 only by scripts/select_gam_smoothing.py.
SMOOTHING_WEIGHT = 100.0  # 2023 inner MAE 1057.0 MW, best of the decade grid 0.1 to 1e6
CURVE_SLOTS = {'night 03:00': 6, 'noon 12:00': 24, 'evening 19:00': 38}
CURVE_REFIT_DAY = date(2024, 12, 30)
BACKUP_FLAG_005 = 'backup_exp004'


def make_gam(alpha: float | None = None) -> SlotGAM:
    weight = SMOOTHING_WEIGHT if alpha is None else alpha
    if weight is None:
        raise RuntimeError('Smoothing weight not chosen yet: run scripts/select_gam_smoothing.py')
    return SlotGAM([TEMPERATURE_COLUMN], df=DEGREES_OF_FREEDOM, alpha_grid=(float(weight),),
                   missing_as_nan=True)


def training_rows(history: pd.Series, builder, refit_day: date):
    """Training matrix exactly as the runner builds it at a refit (same cutoff, same filters)."""
    usable = known_history(history, refit_day, None).dropna()
    start = pd.Timestamp(TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
    usable = usable.loc[usable.index >= start]
    days = sorted(set(ev.local_dates(usable.index)))
    x = pd.concat([day_features(history, d, builder) for d in days]).reindex(usable.index)
    valid = x.notna().all(axis=1)
    return x.loc[valid], usable.loc[valid]


def curve_figure(history: pd.Series, temperature: pd.DataFrame) -> dict:
    """Fitted temperature effect of the GAM and of the HDD/CDD model at three slots, last 2024 refit."""
    gam_x, gam_y = training_rows(history, lagged_gam_features(temperature), CURVE_REFIT_DAY)
    ols_x, ols_y = training_rows(history, lagged_ols_features(temperature), CURVE_REFIT_DAY)
    figure = Figure(figsize=(13, 4), facecolor='white')
    FigureCanvasAgg(figure)
    axes = figure.subplots(1, 3, sharey=True)
    summary = {}
    for axis, (name, slot) in zip(axes, CURVE_SLOTS.items(), strict=True):
        rows = np.asarray(local_slots(gam_x.index) == slot)
        model = make_gam().fit(gam_x.loc[rows], gam_y.loc[rows])
        low, high = model.lower_[TEMPERATURE_COLUMN], model.upper_[TEMPERATURE_COLUMN]
        grid = np.linspace(low, high, 120)
        values = pd.DataFrame({TEMPERATURE_COLUMN: np.append(grid, 15.0)})
        effect = model._smooth_effect(gam_x.loc[rows], values)
        smooth = effect[:-1] - effect[-1]
        ols = SlotOLS().fit(ols_x.loc[rows], ols_y.loc[rows])
        coefficient = dict(zip(ols.columns_, ols.coef_[1:], strict=True))
        linear = (coefficient['hdd'] * np.maximum(0, HEATING_BASE_C - grid)
                  + coefficient['cdd'] * np.maximum(0, grid - COOLING_BASE_C))
        axis.plot(grid, linear, color=GREY, lw=1.8, label='EXP-004: HDD and CDD (bases 15, 20 C)')
        axis.plot(grid, smooth, color=BLUE, lw=2.2, label='EXP-005: smooth GAM effect')
        axis.axvline(15, color='#d8d8d8', lw=0.8)
        axis.set_title(name)
        axis.set_xlabel('forecast national temperature (deg C)')
        axis.grid(color='#eeeeee')
        summary[name] = {'n_training_rows': int(rows.sum()),
                         'range_c': [float(low), float(high)],
                         'slope_lower_mw_per_c': model.slope_lower_[TEMPERATURE_COLUMN],
                         'slope_upper_mw_per_c': model.slope_upper_[TEMPERATURE_COLUMN],
                         'effect_at_low_end_mw': float(smooth[0]),
                         'effect_at_high_end_mw': float(smooth[-1]),
                         'hdd_slope_mw_per_c': float(coefficient['hdd']),
                         'cdd_slope_mw_per_c': float(coefficient['cdd'])}
    axes[0].set_ylabel('effect on load relative to 15 C (MW)')
    axes[0].legend(frameon=False, fontsize=8, loc='upper center')
    figure.suptitle('The GAM keeps the degree-day shape, rounds the corner at 15 C and dips below zero near 20 C')
    figure.tight_layout()
    FIGURE.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(FIGURE, dpi=130, facecolor='white')
    return summary


def main() -> None:
    data = load()
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, LAST)
        labels = read_labels(connection, FIRST, LAST + timedelta(days=1))
        start = pd.Timestamp(FIRST, tz='Europe/Paris').tz_convert('UTC')
        end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': end}).fetchall()
    days = pd.Series(ev.local_dates(temperature.index), index=temperature.index)
    status = temperature['weather_status'].groupby(days).first().value_counts().to_dict()
    if status.get('not_collected'):
        raise RuntimeError(f'Days without collected TMP rows: {status}')

    calendar = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                            builder=calendar_ols_features, training_start=TRAINING_START)
    weather = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                           builder=temperature_ols_features(temperature),
                           training_start=TRAINING_START)
    exp003 = with_calendar_backup(weather, calendar)
    lagged = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                          builder=lagged_ols_features(temperature), training_start=TRAINING_START)
    exp004 = with_calendar_backup(lagged, exp003, flag=BACKUP_FLAG)
    for name, forecast, expected in (('EXP-003', exp003, EXP003_MAE), ('EXP-004', exp004, EXP004_MAE)):
        frame = forecast.assign(actual=data.consommation, rte_d1=data.prevision_j1)
        mae = ev.score(frame[[ev.MODEL, ev.ACTUAL, ev.RTE]]).model.mae
        if abs(mae - expected) > 0.01:
            raise RuntimeError(f'{name} did not reproduce: {mae:.2f} instead of {expected}')
        print(name, 'reproduced, MAE', round(mae, 2))

    began = time.perf_counter()
    gam = walk_forward(data.consommation, FIRST, LAST, make_gam,
                       builder=lagged_gam_features(temperature), training_start=TRAINING_START)
    seconds = round(time.perf_counter() - began, 1)
    predictions = with_calendar_backup(gam, exp004, flag=BACKUP_FLAG_005)
    flagged = predictions[BACKUP_FLAG_005]
    flagged_days = sorted(set(ev.local_dates(predictions.index[flagged])))
    print('GAM walk-forward seconds', seconds, '| slots on the EXP-004 backup', int(flagged.sum()),
          '| days', [str(d) for d in flagged_days])
    if predictions[ev.MODEL].isna().any():
        raise RuntimeError('Forecast slots without any model')

    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    result['vs_exp004'] = paired_against_reference(predictions[ev.MODEL], exp004[ev.MODEL], data)
    result['gam_walk_forward_seconds'] = seconds

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    tables = {
        'exp005_vs_rte': day_type_table(predictions[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold),
        'exp005_vs_rte_star': day_type_table(predictions[ev.MODEL], data.consommation,
                                             frame[RTE_STAR], cold),
        'exp004_vs_rte': day_type_table(exp004[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold)}
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_types'] = tables['exp005_vs_rte'].to_dict(orient='index')
    result['day_types_rte_star'] = tables['exp005_vs_rte_star'].to_dict(orient='index')
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['cold'] = 'D9 SYNOP labels, 12 stations, 4.84 C (labels.synop), applied in this script'
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing
    result['n_backup_slots'] = int(flagged.sum())
    result['backup_days'] = [str(d) for d in flagged_days]

    monthly = {name: monthly_diagnostic(pd.DataFrame({
        ev.MODEL: forecast[ev.MODEL], ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}))
        for name, forecast in (('exp005', predictions), ('exp004', exp004))}
    table = pd.concat(monthly, axis=1)
    print(table.to_string())
    result['monthly'] = json.loads(table.to_json())

    both = pd.DataFrame({'exp005': predictions[ev.MODEL], 'exp004': exp004[ev.MODEL],
                         ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}).dropna()
    daily = both[['exp005', 'exp004']].sub(both[ev.ACTUAL], axis=0).abs().groupby(
        ev.local_dates(both.index)).mean()
    cold_days = [d for d in daily.index if cold.get(d, False)]
    result['cold_days_descriptive'] = {
        'n_days': len(cold_days),
        'mean_daily_mae_exp005': float(daily.loc[cold_days, 'exp005'].mean()),
        'mean_daily_mae_exp004': float(daily.loc[cold_days, 'exp004'].mean())}
    print('cold days, mean daily MAE', result['cold_days_descriptive'])

    result['curves'] = curve_figure(data.consommation, temperature)
    print(json.dumps(result['curves'], indent=2))

    keys = ('model', 'rte', RTE_STAR, 'bootstrap_7', 'bootstrap_14', 'rte_star_bootstrap_7',
            'rte_star_bootstrap_14', 'vs_exp004')
    if '--dry' in sys.argv:
        print(json.dumps({k: result[k] for k in keys}, indent=2, default=str))
        return
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-005', frame=frame,
        cold=cold, reference=exp004[ev.MODEL], reference_name='EXP-004', params={
            'model': 'Gaussian GLMGam per local half-hour slot (48 models), penalized cubic '
                     'B-spline of the temperature, linear terms for the rest',
            'features': 'EXP-004 features with hdd and cdd replaced by the D11 GFS national '
                        'temperature as a smooth term; lag_d2, lag_d7, lag_last and special_* '
                        'unchanged',
            'spline_df': DEGREES_OF_FREEDOM, 'spline_degree': 3,
            'smoothing_weight': SMOOTHING_WEIGHT,
            'smoothing_rule': 'one weight for all slots, chosen on the 2023 inner walk-forward '
                              '(scripts/select_gam_smoothing.py), frozen before 2024',
            'tails': 'linear extrapolation with the boundary slope',
            'missing_rule': 'missing lag or weather: NaN, EXP-004 forecast, flagged backup_exp004',
            'training_start': TRAINING_START, 'refit': 'weekly, Monday cutoff, expanding window',
            'seed': 'none (deterministic penalized least squares)',
            'bootstrap_seed': ev.BOOTSTRAP_SEED, 'reference': 'EXP-004',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour'})
    stamp = int(time.time() * 1000)
    metrics = {'n_backup_slots': flagged.sum(), 'n_backup_days': len(flagged_days),
               'n_label_missing_days': len(label_missing),
               'gam_walk_forward_seconds': seconds,
               'vs_exp004_n_slots': result['vs_exp004']['n_slots'],
               'vs_exp004_reference_mae': result['vs_exp004']['reference_mae']}
    for name, label in (('exp005_vs_rte', 'rte_d1'), ('exp005_vs_rte_star', 'rte_star')):
        for kind in ('ordinary', 'cold', 'cold_any', 'holiday', 'bridge', 'dst'):
            if kind in tables[name].index:
                row = tables[name].loc[kind]
                metrics[f'lab_{kind}_model_vs_{label}'] = row[ev.MODEL]
                metrics[f'lab_{kind}_{label}'] = row[ev.RTE]
                metrics[f'lab_{kind}_n_days'] = row['n_days']
    for block in (7, 14):
        for key, value in result['vs_exp004'][f'bootstrap_{block}'].items():
            if key != 'verdict':
                metrics[f'vs_exp004_bootstrap_{block}_{key}'] = value
    mlflow = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
    requests.post(f'{mlflow}/runs/log-batch', json={
        'run_id': result['mlflow_run_id'],
        'metrics': [{'key': k, 'value': float(v), 'timestamp': stamp, 'step': 1}
                    for k, v in metrics.items()]}, timeout=30).raise_for_status()
    got = requests.get(f'{mlflow}/runs/get', params={'run_id': result['mlflow_run_id']}, timeout=30)
    got.raise_for_status()
    info = got.json()['run']['info']
    artifact_path = info['artifact_uri'].removeprefix('mlflow-artifacts:').strip('/')
    with FIGURE.open('rb') as content:
        requests.put(f'{mlflow_tracking_uri()}/api/2.0/mlflow-artifacts/artifacts/'
                     f'{artifact_path}/plots/{FIGURE.name}', data=content,
                     headers={'Content-Type': 'image/png'}, timeout=30).raise_for_status()
    result['mlflow_status'] = info['status']
    if result['mlflow_status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(OUTPUT / 'backtest_predictions.csv', index_label='value_time_utc')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str),
                                         encoding='utf-8')
    print(json.dumps({k: result[k] for k in (*keys, 'mlflow_run_id', 'mlflow_status')},
                     indent=2, default=str))


if __name__ == '__main__':
    main()

"""Measure the consolidated minus real-time consumption gap on every stored shared half-hour (P5).

Read-only. The lag coefficients come from the EXP-004 linear model fitted at the Monday refit of
2024-12-30, the last refit inside the validation period, so no 2025 or 2026 value enters a fit.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from run_exp002 import TRAINING_START
from run_exp003 import WEATHER_FIRST

from load_forecast import evaluation as ev
from load_forecast import revision_gap as rg
from load_forecast.backtest import fit_slot_models
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.lag_model import LAG_COLUMNS, lagged_ols_features
from load_forecast.linear_model import SlotOLS
from load_forecast.periods import VALIDATION_LAST
from load_forecast.weather_features import read_temperature

FIRST = date(2026, 7, 1)
LAST = date(2026, 8, 31)
REFIT = date(2024, 12, 30)
OUTPUT = Path('data/revision_gap')
HISTORY_QUERY = """
SELECT value_time, consommation FROM {table}
WHERE value_time >= %(start)s AND value_time < %(end)s ORDER BY value_time
"""


def paris_start(day: date) -> pd.Timestamp:
    return pd.Timestamp(day, tz='Europe/Paris').tz_convert('UTC')


def read_pairs() -> tuple[pd.DataFrame, list, list]:
    statements = [s for s in Path(__file__).with_name('revision_gap_measure.sql')
                  .read_text().split(';') if s.strip()]
    params = {'start': paris_start(FIRST), 'end': paris_start(LAST + timedelta(days=1))}
    with connect_database() as connection, connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        results = []
        for statement in statements:
            cursor = connection.execute(statement, params if '%(' in statement else None)
            results.append((cursor.fetchall(), [c.name for c in cursor.description]))
    (pairs, columns), (appearances, _), (calls, _) = results
    frame = pd.DataFrame(pairs, columns=columns)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop('value_time'), utc=True))
    return frame, appearances, calls


def lag_coefficients() -> pd.DataFrame:
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, VALIDATION_LAST)
        cursor = connection.execute(clean_query(HISTORY_QUERY),
                                    {'start': paris_start(TRAINING_START),
                                     'end': paris_start(REFIT)})
        rows = cursor.fetchall()
    history = pd.Series([float(v) if v is not None else float('nan') for _, v in rows],
                        index=pd.DatetimeIndex(pd.to_datetime([t for t, _ in rows], utc=True)))
    models = fit_slot_models(history, REFIT, SlotOLS, builder=lagged_ols_features(temperature),
                             published_at=None, training_start=TRAINING_START, cache={})
    return pd.DataFrame(
        [[model.coef_[model.columns_.index(name) + 1] for name in LAG_COLUMNS]
         for _, model in sorted(models.items())], columns=LAG_COLUMNS)


def ingestion_lag_days(stamps: pd.Series, index: pd.DatetimeIndex) -> dict:
    days = (pd.to_datetime(stamps, utc=True).to_numpy() - index.to_numpy()) / pd.Timedelta(days=1)
    return {'min': float(days.min()), 'median': float(pd.Series(days).median()),
            'max': float(days.max())}


def main() -> None:
    pairs, appearances, calls = read_pairs()
    gap = (pairs['consolidated'] - pairs['real_time']).astype(float).rename('gap')
    overall = rg.summarize(gap)
    by_month = rg.summarize_by(gap, 'month')
    by_hour = rg.summarize_by(gap, 'hour')
    local = gap.index.tz_convert('Europe/Paris')
    month_hour = gap.groupby([local.strftime('%Y-%m'), local.hour]).mean().unstack(0)
    relative = 100 * gap / pairs['consolidated']

    days = list(pd.date_range(FIRST, LAST + timedelta(days=2)).date)
    shifts = rg.lag_shifts(gap, days)
    coefficients = lag_coefficients()
    forecast = rg.forecast_shift(shifts, coefficients)
    rte_rows_differ = int((pairs['rte_d1_real_time_row']
                           != pairs['rte_d1_consolidated_row']).sum())
    rte_error_rt = (pairs['rte_d1_real_time_row'] - pairs['real_time']).astype(float)
    rte_error_cons = (pairs['rte_d1_real_time_row'] - pairs['consolidated']).astype(float)

    result = {
        'sign': 'gap = consolidated - real_time (MW); production lag shift = -gap',
        'window_paris': [str(FIRST), str(LAST)],
        'n_overlap_slots': len(gap), 'expected_slots': 48 * ((LAST - FIRST).days + 1),
        'versions_max': {'real_time': int(pairs['real_time_versions'].max()),
                         'consolidated': int(pairs['consolidated_versions'].max())},
        'ingestion_lag_days': {
            'real_time': ingestion_lag_days(pairs['real_time_ingested_at'], gap.index),
            'consolidated': ingestion_lag_days(pairs['consolidated_ingested_at'], gap.index)},
        'first_appearance': [[str(d), str(m), str(t), n] for d, m, t, n in appearances],
        'consolidated_requests': [[str(a), str(s), str(e)] for a, _, s, e in calls],
        'overall': overall,
        'relative_percent': {'mean': float(relative.mean()), 'sd': float(relative.std())},
        'by_month': by_month.to_dict(orient='index'),
        'lag_feature_shift': {
            'delivery_days': len(set(ev.local_dates(shifts.index))),
            'first_day': str(min(ev.local_dates(shifts.index))),
            'last_day': str(max(ev.local_dates(shifts.index))),
            **{name: rg.summarize(shifts[name]) for name in LAG_COLUMNS}},
        'exp004_refit': str(REFIT),
        'lag_coefficient_sum': {'mean': float(coefficients.sum(axis=1).mean()),
                                'min': float(coefficients.sum(axis=1).min()),
                                'max': float(coefficients.sum(axis=1).max())},
        'lag_coefficient_mean': coefficients.mean().to_dict(),
        'exp004_forecast_shift': rg.summarize(forecast),
        'rte_d1': {'rows_with_different_d1_value': rte_rows_differ,
                   'mean_error_vs_real_time': float(rte_error_rt.mean()),
                   'mean_error_vs_consolidated': float(rte_error_cons.mean()),
                   'mae_vs_real_time': float(rte_error_rt.abs().mean()),
                   'mae_vs_consolidated': float(rte_error_cons.abs().mean())},
        'bootstrap': {'seed': ev.BOOTSTRAP_SEED, 'draws': ev.BOOTSTRAP_DRAWS,
                      'blocks': [ev.BLOCK_DAYS, ev.CHECK_BLOCK_DAYS], 'unit': 'Paris day mean'},
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'summary.json').write_text(json.dumps(result, indent=2, default=str))
    by_hour.to_csv(OUTPUT / 'by_hour.csv')
    month_hour.to_csv(OUTPUT / 'month_hour_mean.csv')
    coefficients.to_csv(OUTPUT / 'exp004_lag_coefficients.csv', index_label='slot')
    pd.set_option('display.width', 200)
    print(json.dumps(result, indent=2, default=str))
    print(by_hour.round(1).to_string())
    print(month_hour.round(0).to_string())


if __name__ == '__main__':
    sys.exit(main())

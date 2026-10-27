"""Live level shift after the 2026-10-19 refit: the numbers of docs/analysis_live_level_shift.md.

Read-only on production. Run states, refits and frozen inputs are read from the runtime folder
given by --runtime; states that are replayed are first copied into --work-dir, which must lie
outside the runtime folder. Nothing is written to the database or to the runtime folder.
No 2025 value is read: actuals start on 2026-01-01, issued days before 2026 are skipped, the
history section reads the 2023 and 2024 archives only and drops 2025 temperatures, the SYNOP
observations and the GFS series start in 2026.
"""

import argparse
import copy
import pickle
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from load_forecast import labels, production
from load_forecast.backtest import forecast_day
from load_forecast.cutoff import PARIS, delivery_slots
from load_forecast.eco2mix import connect_database
from load_forecast.gbm_model import AtRounds
from load_forecast.lag_model import BACKUP_FLAG
from load_forecast.temperature_model import with_calendar_backup
from load_forecast.weather import POINTS

REFIT = date(2026, 10, 19)
CHAIN_START = date(2026, 10, 17)
CHAIN_DAYS = [date(2026, 10, d) for d in (19, 20, 21, 22, 23, 24)]
BEFORE = [date(2026, 10, d) for d in (13, 14, 15, 16, 17)]
AFTER = [date(2026, 10, d) for d in (19, 20, 23, 24)]  # 10-21 and 10-22 ran on the backup chain
EXPERTS = list(production.EXPERTS)
ACTUALS_QUERY = """
SELECT DISTINCT ON (source_dataset, value_time) source_dataset, value_time, consommation,
       prevision_j1
FROM raw.eco2mix
WHERE value_time >= %(first)s AND value_time < %(last)s
  AND extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
  AND original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
  AND source_dataset = ANY(%(datasets)s)
ORDER BY source_dataset, value_time, ingested_at DESC
"""
VINTAGE_QUERY = """
SELECT value_time, ingested_at, consommation FROM raw.eco2mix
WHERE source_dataset = 'eco2mix-national-tr' AND consommation IS NOT NULL
  AND value_time >= '2026-10-05' AND value_time < '2026-10-25'
  AND extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
"""
REAL_TIME, CONSOLIDATED = 'eco2mix-national-tr', 'eco2mix-national-cons-def'


def local_daily(series: pd.Series) -> pd.Series:
    return series.groupby(series.index.tz_convert(PARIS).date).mean()


def read_actuals(first: str, last: str, datasets: list[str]) -> pd.DataFrame:
    with connect_database() as connection, connection.cursor() as cursor:
        cursor.execute(ACTUALS_QUERY, {'first': first, 'last': last, 'datasets': datasets})
        frame = pd.DataFrame(cursor.fetchall(), columns=[d.name for d in cursor.description])
    frame['value_time'] = pd.to_datetime(frame['value_time'], utc=True)
    frame[['consommation', 'prevision_j1']] = frame[['consommation', 'prevision_j1']].astype(float)
    return frame


def stored_runs(runtime: Path) -> pd.DataFrame:
    rows = []
    for folder in (runtime / 'runs').iterdir():
        for path in folder.glob('state_*.pkl'):
            payload = pickle.loads(path.read_bytes())
            rows.append({'folder': folder, 'variant': payload['state'].variant,
                         'day': payload['state'].last_day, 'refit': payload['refit']})
    return pd.DataFrame(rows).sort_values(['variant', 'day'])


def issued_forecasts(runs: pd.DataFrame) -> pd.DataFrame:
    """Experts and aggregate of every 2026 day, from the latest stored state of each variant."""
    frames = []
    for variant, group in runs.groupby('variant'):
        last = group.iloc[-1]
        state = production.StateStore(last['folder']).load(last['day'])
        for day, issued in state.issued.items():
            if day.year < 2026:
                continue
            frame = issued.experts.copy()
            frame['agg'], frame['variant'], frame['day'] = issued.point, variant, day
            frames.append(frame)
    return pd.concat(frames)


def bias_table(issued: pd.DataFrame, real_time: pd.DataFrame) -> pd.DataFrame:
    joined = issued.join(real_time[['consommation', 'prevision_j1']])
    rows = []
    for (variant, day), g in joined.groupby(['variant', 'day']):
        g = g[g['consommation'].notna()]
        if len(g) < 46:
            continue
        rows.append({'variant': variant, 'day': day,
                     **{c: (g[c] - g['consommation']).mean() for c in [*EXPERTS, 'agg']},
                     'rte': (g['prevision_j1'] - g['consommation']).mean()})
    return pd.DataFrame(rows).set_index(['variant', 'day'])


def model_sets(runtime: Path, runs: pd.DataFrame) -> dict[tuple[str, date], production.WeeklyModels]:
    sets = {}
    for _, run in runs.drop_duplicates(['variant', 'refit']).iterrows():
        path = next(run['folder'].glob('models_*.pkl'))
        sets[(run['variant'], run['refit'])] = pickle.loads(path.read_bytes())
    return sets


def experts_with(models: production.WeeklyModels, inputs, variant: str,
                 temperature_shift: float = 0.0) -> pd.DataFrame:
    """Stateless experts of one day with given weekly models (EXP-007 needs the chain)."""
    temperature = inputs.temperature.copy()
    for column in [c for c in temperature.columns if c.endswith('_c')]:
        temperature[column] += temperature_shift
    builders = production.feature_builders(temperature)
    history = inputs.mapped(variant)
    plain = {n: forecast_day(models.slots[n], history, inputs.day, models.refit, builders[n])
             for n in production.SLOT_FAMILIES}
    view = AtRounds(models.gbm, production.FROZEN.gbm.rounds)
    boosted = forecast_day({s: view for s in range(48)}, history, inputs.day, models.refit,
                           builders['gam'])
    exp003 = with_calendar_backup(plain['weather'], plain['calendar'])
    exp004 = with_calendar_backup(plain['lagged'], exp003, flag=BACKUP_FLAG)
    exp005 = with_calendar_backup(plain['gam'], exp004, flag='backup_exp004')
    exp008 = with_calendar_backup(boosted, exp005, flag='backup_exp004')
    return pd.DataFrame({'calendar': plain['calendar']['model'], 'EXP-003': exp003['model'],
                         'EXP-004': exp004['model'], 'EXP-005': exp005['model'],
                         'EXP-008': exp008['model'], 'backup': exp004[BACKUP_FLAG]})


def counterfactual(runtime: Path, runs: pd.DataFrame, issued: pd.DataFrame) -> pd.DataFrame:
    """Every stored input day forecast with every weekly model set allowed on that day."""
    sets = model_sets(runtime, runs)
    frames = []
    for path in sorted((runtime / 'inputs').glob('*.pkl')):
        inputs = pickle.loads(path.read_bytes())
        for (variant, refit), models in sets.items():
            if refit > inputs.day:
                continue
            frame = experts_with(models, inputs, variant)
            frame['variant'], frame['refit'], frame['day'] = variant, refit, inputs.day
            frames.append(frame)
            stored = issued[(issued['variant'] == variant) & (issued['day'] == inputs.day)]
            in_force = runs[(runs['variant'] == variant) & (runs['day'] == inputs.day)]
            if not stored.empty and not in_force.empty and in_force['refit'].iloc[0] == refit:
                gap = max(float((frame[e] - stored[e].reindex(frame.index)).abs().max())
                          for e in ('EXP-004', 'EXP-005', 'EXP-008'))
                print(f'reproduce {variant} {inputs.day} refit {refit}: max |diff| {gap:.6f} MW')
    return pd.concat(frames)


def chain(runtime: Path, work: Path, runs: pd.DataFrame, issued: pd.DataFrame,
          scenarios: dict[str, tuple[bool, dict[date, pd.DataFrame] | None]] | None = None
          ) -> pd.DataFrame:
    """Full production chain from the stored 10-17 state.

    A scenario is (adopt the 10-19 refit, temperature frames by day or None for the stored ones).
    """
    scenarios = scenarios or {'factual': (True, None), 'no_refit': (False, None)}
    frames = []
    inputs = {d: pickle.loads((runtime / 'inputs' / f'{d}.pkl').read_bytes()) for d in CHAIN_DAYS}
    for variant in ('A', 'B'):
        start = runs[(runs['variant'] == variant) & (runs['day'] == CHAIN_START)].iloc[0]
        folder = work / 'runs' / start['folder'].name
        if not folder.exists():
            shutil.copytree(start['folder'], folder)
        refit_copy = work / 'refits' / variant / f'{REFIT}.pkl'
        refit_copy.parent.mkdir(parents=True, exist_ok=True)
        if not refit_copy.exists():
            shutil.copy2(runtime / 'refits' / variant / f'{REFIT}.pkl', refit_copy)
        base = production.StateStore(folder).load(CHAIN_START)
        prepared = pickle.loads(refit_copy.read_bytes())
        for scenario, (adopt, temperatures) in scenarios.items():
            state = copy.deepcopy(base)
            for day in CHAIN_DAYS:
                if adopt and day == REFIT:
                    production.adopt_refit(state, prepared, day, available_before=(
                        prepared.completed_at + timedelta(seconds=1)))
                temperature = inputs[day].temperature if temperatures is None else temperatures[day]
                out = production.run_day(day, inputs[day].mapped(variant), temperature, state,
                                         reuse_models=True)
                out['variant'], out['scenario'], out['day'] = variant, scenario, day
                frames.append(out)
                if scenario == 'factual':
                    stored = issued[(issued['variant'] == variant) & (issued['day'] == day)]
                    gap = max([float((out[e] - stored[e].reindex(out.index)).abs().max())
                               for e in EXPERTS]
                              + [float((out['model'] - stored['agg'].reindex(out.index)).abs().max())])
                    print(f'reproduce chain {variant} {day}: max |diff| {gap:.6f} MW')
    return pd.concat(frames)


def stepwise(runtime: Path, work: Path, runs: pd.DataFrame, issued: pd.DataFrame) -> None:
    """One production step from the stored state of the previous delivery day, no chain.

    Covers the days the chain from 10-17 does not: 10-16, 10-17, 10-26 (refit of 10-26 adopted)
    and 10-27. Every expert and the aggregate are compared with the stored ones.
    """
    steps = [(date(2026, 10, 15), date(2026, 10, 16)), (date(2026, 10, 16), date(2026, 10, 17)),
             (date(2026, 10, 24), date(2026, 10, 26)), (date(2026, 10, 26), date(2026, 10, 27))]
    for variant in ('A', 'B'):
        for previous, day in steps:
            start = runs[(runs['variant'] == variant) & (runs['day'] == previous)].iloc[0]
            folder = work / 'runs' / start['folder'].name
            if not folder.exists():
                shutil.copytree(start['folder'], folder)
            state = production.StateStore(folder).load(previous)
            inputs = pickle.loads((runtime / 'inputs' / f'{day}.pkl').read_bytes())
            target = runs[(runs['variant'] == variant) & (runs['day'] == day)].iloc[0]
            if target['refit'] != state.models.refit:
                path = runtime / 'refits' / variant / f'{target["refit"]}.pkl'
                prepared = pickle.loads(path.read_bytes())
                production.adopt_refit(state, prepared, day, available_before=(
                    prepared.completed_at + timedelta(seconds=1)))
            out = production.run_day(day, inputs.mapped(variant), inputs.temperature, state,
                                     reuse_models=True)
            stored = issued[(issued['variant'] == variant) & (issued['day'] == day)]
            gap = max([float((out[e] - stored[e].reindex(out.index)).abs().max()) for e in EXPERTS]
                      + [float((out['model'] - stored['agg'].reindex(out.index)).abs().max())])
            print(f'reproduce step {variant} {previous} -> {day}: max |diff| {gap:.6f} MW')


def autumn_history(temperature: pd.Series) -> pd.DataFrame:
    """Weekly bias of the archived 2023 and 2024 experts against consolidated actuals."""
    def read(name: str) -> pd.DataFrame:
        frame = pd.read_csv(Path('data/experiments/EXP-009') / name, index_col=0)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
        return frame
    experts = pd.concat([read('experts_2023.csv'), read('experts_2024.csv')])
    actual = read_actuals('2023-01-01', '2025-01-01', [CONSOLIDATED]).set_index('value_time')
    daily = pd.DataFrame({e: local_daily(experts[e] - actual['consommation']) for e in EXPERTS})
    daily['rte'] = local_daily(actual['prevision_j1'] - actual['consommation'])
    daily['temperature'] = local_daily(temperature)
    daily['load'] = local_daily(actual['consommation'])
    daily.index = pd.to_datetime(daily.index)
    return pd.concat([daily.loc['2023-09-11':'2023-11-26'],
                      daily.loc['2024-09-09':'2024-11-24']]).resample('W-SUN').mean().dropna()


def revision_gap_by_month(actuals: pd.DataFrame) -> None:
    pairs = actuals.pivot_table(index='value_time', columns='source_dataset',
                                values='consommation').dropna()
    gap = pairs[CONSOLIDATED] - pairs[REAL_TIME]
    daily = local_daily(gap)
    daily.index = pd.to_datetime(daily.index)
    rng = np.random.default_rng(0)
    for month, values in daily.groupby(daily.index.strftime('%Y-%m')):
        x = values.to_numpy()
        blocks = [x[i:i + 7] for i in range(0, len(x), 7)]
        means = [np.concatenate([blocks[j] for j in rng.integers(0, len(blocks), len(blocks))]).mean()
                 for _ in range(5000)]
        low, high = np.percentile(means, [2.5, 97.5])
        print(f'gap {month}: {x.mean():.0f} MW, 7-day block 95% [{low:.0f}, {high:.0f}], '
              f'{len(x)} days')


SYNOP_QUERY = """
SELECT station_id, value_time, temperature_k, original_values->>'insert_time',
       original_values->>'reference_time', ingested_at, payload_hash
FROM labels.synop WHERE value_time >= %(first)s AND value_time < %(last)s
"""
GFS_FIRST, SYNOP_END = date(2026, 4, 1), date(2026, 10, 26)
REFERENCE = (date(2026, 9, 1), date(2026, 10, 9))  # before the window, to calibrate station against grid
WINDOW = (date(2026, 10, 10), date(2026, 10, 25))


def observed_temperature(first: date, last: date) -> pd.DataFrame:
    """Observed SYNOP temperature (C) at the synoptic UTC hours, one column per GFS point name."""
    with connect_database() as connection, connection.cursor() as cursor:
        cursor.execute(SYNOP_QUERY, {'first': first, 'last': last + timedelta(days=1)})
        frame = pd.DataFrame(cursor.fetchall(), columns=['station_id', 'value_time', 'temperature_k',
                                                         'insert_time', 'reference_time',
                                                         'ingested_at', 'payload_hash'])
    frame['value_time'] = pd.to_datetime(frame['value_time'], utc=True)
    kept = labels.latest_versions(frame)
    kept = kept[(kept['value_time'].dt.hour % 3 == 0) & (kept['value_time'].dt.minute == 0)]
    kept = kept.assign(temperature_c=pd.to_numeric(kept['temperature_k']) - 273.15)
    wide = kept.pivot(index='value_time', columns='station_id', values='temperature_c')
    return wide.rename(columns=dict(zip(labels.STATION_IDS, POINTS, strict=True)))[list(POINTS)]


def gfs_temperature(runtime: Path) -> pd.DataFrame:
    """Production temperature of 2026 (pinned run, 12 points, hourly steps) at the synoptic UTC hours.

    It is read from the latest frozen input, which stores the series the production runs used.
    Rows before 2026 are dropped on read.
    """
    latest = pickle.loads(sorted((runtime / 'inputs').glob('*.pkl'))[-1].read_bytes()).temperature
    latest = latest[latest.index >= pd.Timestamp(GFS_FIRST, tz='UTC')]
    latest = latest[(latest.index.hour % 3 == 0) & (latest.index.minute == 0)]
    frame = latest[[f'{p}_c' for p in POINTS]].rename(columns=lambda c: c[:-2])
    frame['day'] = latest.index.tz_convert(PARIS).date
    return frame


def gfs_error_tables(gfs: pd.DataFrame, observed: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """GFS minus observed, per point and at the national mean (12 points, none missing)."""
    points = list(POINTS)
    matched = observed[points].reindex(gfs.index)
    error = gfs[points] - matched
    national = pd.DataFrame({'gfs': gfs[points].mean(axis=1, skipna=False),
                             'observed': matched.mean(axis=1, skipna=False)})
    national['error'] = national['gfs'] - national['observed']
    national['day'], error['day'] = gfs['day'], gfs['day']
    return error, national


def gfs_section(runtime: Path, work: Path, runs: pd.DataFrame, issued: pd.DataFrame,
                real_time: pd.DataFrame) -> None:
    print('8. GFS temperature used by production against SYNOP observations')
    observed = observed_temperature(GFS_FIRST - timedelta(days=1), SYNOP_END)
    print(f'SYNOP available to {observed.index.max()} UTC; complete 12-station reports at '
          f'{int(observed.notna().all(axis=1).sum())} of {len(observed)} synoptic hours')
    gfs = gfs_temperature(runtime)
    gfs = gfs[gfs['day'] < SYNOP_END]
    print(f'GFS days {gfs["day"].min()} to {gfs["day"].max()}, {gfs["day"].nunique()} days, '
          f'{int(gfs[list(POINTS)].isna().any(axis=1).sum())} synoptic hours with a missing point')
    error, national = gfs_error_tables(gfs, observed)

    gaps = []
    for path in sorted((runtime / 'inputs').glob('*.pkl')):
        stored = pickle.loads(path.read_bytes()).temperature
        both = stored['temperature_c'].reindex(gfs.index)
        window = gfs[(gfs['day'] >= date(2026, 10, 10)) & (gfs['day'] <= date.fromisoformat(path.stem))]
        gaps.append((window[list(POINTS)].mean(axis=1) - both.reindex(window.index)).abs().max())
    print(f'latest frozen series against the {len(gaps)} frozen inputs, 10-10 to each input day: '
          f'max |diff| {np.nanmax(gaps):.6f} C')

    daily = national.groupby('day').agg(gfs=('gfs', 'mean'), observed=('observed', 'mean'),
                                        error=('error', 'mean'), hours=('error', 'count'))
    print('Daily national temperature (C), mean of the 8 synoptic hours')
    print(daily.loc[WINDOW[0]:WINDOW[1]].round(2).to_string())

    ref = national[(national['day'] >= REFERENCE[0]) & (national['day'] <= REFERENCE[1])]
    hourly_bias = ref['error'].groupby(ref.index.hour).mean()
    print(f'Reference {REFERENCE[0]} to {REFERENCE[1]}: mean GFS minus observed '
          f'{ref["error"].mean():+.2f} C, daily std {daily.loc[REFERENCE[0]:REFERENCE[1], "error"].std():.2f}; '
          f'by UTC hour {hourly_bias.round(2).to_dict()}')
    windows = {'10-10 to 10-17': [d for d in daily.index if date(2026, 10, 10) <= d <= date(2026, 10, 17)],
               '10-13 to 10-17 (before)': BEFORE,
               '10-19 to 10-24': [d for d in daily.index if date(2026, 10, 19) <= d <= date(2026, 10, 24)],
               '10-19, 10-20, 10-23, 10-24 (after)': AFTER}
    for name, window in windows.items():
        print(f'GFS minus observed {name}: {daily.loc[window, "error"].mean():+.2f} C '
              f'(excess over the reference {daily.loc[window, "error"].mean() - ref["error"].mean():+.2f} C)')
    first_week = [date(2026, 10, d) for d in range(12, 17)]
    second_week = [date(2026, 10, d) for d in range(19, 24)]
    print('weekdays 10-12 to 10-16 then 10-19 to 10-23, mean temperature (C): '
          f'GFS {daily.loc[first_week, "gfs"].mean():.2f} then {daily.loc[second_week, "gfs"].mean():.2f} '
          f'({daily.loc[second_week, "gfs"].mean() - daily.loc[first_week, "gfs"].mean():+.2f}); '
          f'observed {daily.loc[first_week, "observed"].mean():.2f} then '
          f'{daily.loc[second_week, "observed"].mean():.2f} '
          f'({daily.loc[second_week, "observed"].mean() - daily.loc[first_week, "observed"].mean():+.2f})')
    series = daily.loc[GFS_FIRST:REFERENCE[1], 'error']
    means = series.rolling(4).mean().dropna()
    after_mean = daily.loc[AFTER, 'error'].mean()
    print(f'overlapping 4-day means of the daily error, {series.index.min()} to {series.index.max()}: '
          f'median {means.median():+.2f}, 5% {means.quantile(.05):+.2f}, 95% {means.quantile(.95):+.2f}; '
          f'the 10-19 window ({after_mean:+.2f}) exceeds {100 * (means < after_mean).mean():.0f}% of them')
    monthly = daily.assign(month=[d.strftime('%Y-%m') for d in daily.index]).groupby('month')['error']
    print('monthly mean GFS minus observed (C):', monthly.mean().round(2).to_dict())
    by_point = error[error['day'].isin(AFTER)].drop(columns='day').mean()
    ref_point = error[(error['day'] >= REFERENCE[0]) & (error['day'] <= REFERENCE[1])].drop(columns='day').mean()
    print('GFS minus observed by point, clean days after (C) and reference:')
    print(pd.DataFrame({'after': by_point, 'reference': ref_point}).round(2).T.to_string())

    print('9. Rerun of the production chain with the observed temperature instead of the GFS one')
    slots = {d: pickle.loads((runtime / 'inputs' / f'{d}.pkl').read_bytes()).temperature
             for d in CHAIN_DAYS}
    synoptic_error = national['error'].dropna()
    baseline = pd.Series(synoptic_error.index.hour, index=synoptic_error.index).map(hourly_bias)
    scenarios = {'factual': (True, None)}
    for name, correction in (('observed', synoptic_error), ('excess_removed', synoptic_error - baseline)):
        corrected = {}
        for day, frame in slots.items():
            grid = frame.index.union(correction.index)
            shift = correction.reindex(grid).interpolate(method='time', limit_area='inside')
            shift = shift.reindex(frame.index)
            assert shift.reindex(pd.DatetimeIndex(delivery_slots(day, '30min'))).notna().all()
            shift = shift.fillna(0.0)  # the frame holds the whole history; only the 2026 days move
            corrected[day] = frame.assign(temperature_c=frame['temperature_c'] - shift)
        scenarios[name] = (True, corrected)
    full = chain(runtime, work, runs, issued, scenarios).join(real_time['consommation'])
    full = full[pd.Index(full['day']).isin(AFTER)]
    summary = full.groupby(['variant', 'scenario']).apply(
        lambda g: (g[[*EXPERTS, 'model']].sub(g['consommation'], axis=0)).mean())
    print('mean error against real-time actuals on the clean days after (MW)')
    print(summary.round(0).to_string())
    applied = {name: {d: (v[1][d]['temperature_c'] - slots[d]['temperature_c']).reindex(
        pd.DatetimeIndex(delivery_slots(d, '30min'))).mean() for d in AFTER}
        for name, v in scenarios.items() if v[1] is not None}
    print('mean temperature change applied per day (C)')
    print(pd.DataFrame(applied).round(2).to_string())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    runtime, work = args.runtime.resolve(), args.work_dir.resolve()
    if work.is_relative_to(runtime):
        raise ValueError('The work directory must lie outside the production runtime folder')
    pd.set_option('display.width', 200)

    runs = stored_runs(runtime)
    issued = issued_forecasts(runs)
    actuals = read_actuals('2026-01-01', '2026-10-28', [REAL_TIME, CONSOLIDATED])
    real_time = actuals[actuals['source_dataset'] == REAL_TIME].set_index('value_time')

    print('1. Daily bias against real-time actuals (MW)')
    bias = bias_table(issued, real_time)
    print(bias.loc[(slice(None), slice(date(2026, 10, 12), None)), :].round(0).to_string())
    for variant in ('A', 'B'):
        b = bias.loc[variant]
        print(f'{variant}: aggregate before {b.loc[BEFORE, "agg"].mean():.0f}, '
              f'after {b.loc[AFTER, "agg"].mean():.0f}; RTE before {b.loc[BEFORE, "rte"].mean():.0f}, '
              f'after {b.loc[AFTER, "rte"].mean():.0f}')

    print('2. Same inputs, each weekly model set')
    cf = counterfactual(runtime, runs, issued).join(real_time['consommation'])
    cf = cf[~cf['backup'] & cf['consommation'].notna()]
    columns = ['calendar', 'EXP-003', 'EXP-004', 'EXP-005', 'EXP-008']
    cf_bias = cf.groupby(['variant', 'day', 'refit']).apply(
        lambda g: (g[columns].sub(g['consommation'], axis=0)).mean())
    print(cf_bias.round(0).to_string())
    levels = cf.groupby(['variant', 'day', 'refit'])[['EXP-004', 'EXP-005', 'EXP-008']].mean()
    print('mean level change between consecutive refits on the same inputs (MW)')
    print(levels.groupby(['variant', 'day']).diff().dropna().groupby('variant').mean().round(0))

    stepwise(runtime, work, runs, issued)

    print('3. Full chain, factual against no refit')
    full = chain(runtime, work, runs, issued).join(real_time['consommation'])
    full = full[pd.Index(full['day']).isin(AFTER)]
    summary = full.groupby(['variant', 'scenario']).apply(
        lambda g: (g[[*EXPERTS, 'model']].sub(g['consommation'], axis=0)).mean())
    print(summary.round(0).to_string())
    weights = full.groupby(['variant', 'scenario'])[[f'weight_{e}' for e in EXPERTS]].mean()
    print(weights.round(2).to_string())

    print('4. Temperature sensitivity of the 10-19 models (MW for 1 C colder)')
    sets = model_sets(runtime, runs)
    for day in (date(2026, 10, 20), date(2026, 10, 23), date(2026, 10, 24)):
        inputs = pickle.loads((runtime / 'inputs' / f'{day}.pkl').read_bytes())
        base = experts_with(sets[('A', REFIT)], inputs, 'A')
        cold = experts_with(sets[('A', REFIT)], inputs, 'A', temperature_shift=-1.0)
        print(day, (cold[['EXP-003', 'EXP-004', 'EXP-005', 'EXP-008']].mean()
                    - base[['EXP-003', 'EXP-004', 'EXP-005', 'EXP-008']].mean()).round(0).to_dict())

    print('5. Daily actuals, RTE and forecast temperature')
    last_inputs = pickle.loads(sorted((runtime / 'inputs').glob('*.pkl'))[-1].read_bytes())
    temperature = last_inputs.temperature['temperature_c']
    temperature = temperature[(temperature.index < '2025-01-01') | (temperature.index >= '2026-01-01')]
    daily = pd.DataFrame({'real_time': local_daily(real_time['consommation'].dropna()),
                          'rte': local_daily(real_time['prevision_j1'].dropna()),
                          'temperature': local_daily(temperature[temperature.index >= '2026-01-01'])})
    print(daily.loc[date(2026, 10, 5):date(2026, 10, 26)].round(1).to_string())

    print('6. Autumns 2023 and 2024, weekly bias against consolidated actuals (MW)')
    print(autumn_history(temperature).round(0).to_string())

    print('7. Real-time series: vintages and revision gap')
    with connect_database() as connection, connection.cursor() as cursor:
        cursor.execute(VINTAGE_QUERY)
        vintages = pd.DataFrame(cursor.fetchall(), columns=['value_time', 'ingested_at', 'value'])
    grouped = vintages.sort_values('ingested_at').groupby('value_time')['value']
    revision = grouped.last() - grouped.first()
    print(f'slots {len(revision)}, with more than one vintage {(grouped.size() > 1).sum()}, '
          f'median revision {revision.median():.0f} MW, mean {revision.mean():.1f} MW')
    revision_gap_by_month(actuals)
    gfs_section(runtime, work, runs, issued, real_time)


if __name__ == '__main__':
    main()

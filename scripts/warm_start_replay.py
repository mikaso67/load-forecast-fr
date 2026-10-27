"""Warm-start replay of the production function to the go-live day (D20 P2 A, D21, D23, D24).

Usage:
  python scripts/warm_start_replay.py run --variant A [--last 2026-10-04] [--resume]
  python scripts/warm_start_replay.py combine

`run` reconstructs ML-Poly and the conformal pool from the archive (docs/production_design.md
section 2.4), replays one variant from Monday 2026-01-05 to the last day whose inputs are
complete, and writes the checkpoint `checkpoints/<variant>.pkl` with its manifest under
LFF_RUNTIME_DIR. `--resume` extends an existing checkpoint to a later day instead. `combine` writes
both variants into `replay/replay_forecasts.parquet` (slots) and `replay/replay_scores.parquet`
(days). Reads the clean table and raw inputs only; writes nothing in the database.
"""

import argparse
import json
import logging
import os
import pickle
import subprocess
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
from psycopg.rows import dict_row

from load_forecast import warm_start as ws
from load_forecast.database import clean_query, clean_table
from load_forecast.eco2mix import connect_database
from load_forecast.periods import PRODUCTION_FIRST_FORECAST
from load_forecast.production import FROZEN
from load_forecast.revision_mapping import estimate_revision_mapping
from load_forecast.weather import candidate_runs
from load_forecast.weather_production import pinned_temperature

ARCHIVE = Path('data/experiments')
LOGGER = logging.getLogger('warm_start')


def commit() -> str:
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          check=True).stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain', '--', 'src', 'scripts'],
                           capture_output=True, text=True, check=True).stdout.strip()
    return head + ('-dirty' if dirty else '')


def read_clean() -> pd.DataFrame:
    with connect_database() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(clean_query('SELECT value_time, consommation, prevision_j1, source_dataset '
                                   'FROM {table} ORDER BY value_time'))
        clean = pd.DataFrame(cursor.fetchall()).set_index('value_time')
    clean.index = pd.DatetimeIndex(pd.to_datetime(clean.index, utc=True))
    clean[['consommation', 'prevision_j1']] = clean[['consommation', 'prevision_j1']].astype(float)
    return clean


def read_weather(last: date) -> tuple[pd.DataFrame, pd.Series]:
    """Each day's temperature from the GFS revision pinned at its own cutoff, as production does."""
    with connect_database() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM raw.gfs_points WHERE variable = 'TMP'")
        groups: dict = {}
        for row in cursor.fetchall():
            groups.setdefault(row['run_init_time'], []).append(row)
    now = datetime.now(UTC)
    frames, status = [], {}
    for day in pd.date_range(FROZEN.training_start, last).date:
        rows = [r for run in candidate_runs(day) for r in groups.get(run.init_time, [])]
        frame, selection, _ = pinned_temperature(day, rows, now=now)
        frames.append(frame)
        status[day] = selection['status']
    return pd.concat(frames), pd.Series(status)


def run(root: Path, variant: str, last: date | None, resume: bool, allow_dirty: bool) -> None:
    code = commit()
    if code.endswith('-dirty') and not allow_dirty:
        raise SystemExit('Commit the code first: the manifest must name the code that ran')
    began = time.perf_counter()
    clean = read_clean()
    history = clean['consommation']
    realtime = clean['source_dataset'].eq('eco2mix-national-tr')
    local_last = history.dropna().index.max().tz_convert('Europe/Paris').date()
    temperature, weather_status = read_weather(local_last + timedelta(days=1))
    complete = ws.last_complete_day(history, weather_status)
    last = last or complete
    if last > complete:
        raise SystemExit(f'Inputs are complete only up to {complete}')
    LOGGER.info('inputs read in %.0f s: table %s, last complete day %s, code %s',
                time.perf_counter() - began, clean_table(), complete, code)

    out_folder = root / 'replay'
    report_path = out_folder / f'report_{variant}.json'
    if resume:
        state = ws.load_checkpoint(root, variant)
        first = state.last_day + timedelta(days=1)
        report = json.loads(report_path.read_text(encoding='utf-8'))
    else:
        if ws.checkpoint_path(root, variant).exists():
            raise SystemExit('A checkpoint exists: use --resume to extend it')
        seeded, reconstruction = ws.reconstruct(ws.read_archive(ARCHIVE), history)
        LOGGER.info('reconstruction accepted: %s', reconstruction)
        state, first = ws.for_variant(seeded, variant), PRODUCTION_FIRST_FORECAST
        report = {'variant': variant, 'reconstruction': reconstruction, 'clean_table': clean_table(),
                  'refits': [], 'mappings': {}, 'seconds_per_day': {}}

    connection = connect_database() if variant == 'B' else None

    def offsets_for(day: date) -> pd.Series:
        mapping = estimate_revision_mapping(day, connection)
        report['mappings'][day.isoformat()] = mapping.months_used
        return mapping.offsets

    clock = {'last': time.perf_counter()}

    def after_day(day: date, current, out: pd.DataFrame) -> None:
        seconds = time.perf_counter() - clock['last']
        clock['last'] = time.perf_counter()
        report['seconds_per_day'][day.isoformat()] = round(seconds, 1)
        if day.weekday() == 0:
            report['refits'].append({
                'refit': day.isoformat(),
                'models_bytes': len(pickle.dumps(current.models, protocol=pickle.HIGHEST_PROTOCOL)),
                'kalman_bytes': len(pickle.dumps(current.kalman, protocol=pickle.HIGHEST_PROTOCOL)),
                'state_without_models_bytes': len(pickle.dumps(
                    replace(current, models=None), protocol=pickle.HIGHEST_PROTOCOL))})
        LOGGER.info('%s %s done in %.0f s, MW mean %.0f, backups %d', variant, day, seconds,
                    out['model'].mean(), int(out.filter(like='_backup').any(axis=1).sum()))

    try:
        out = ws.replay_days(state, first, last, history, realtime, temperature, offsets_for,
                             after_day=after_day)
    finally:
        if connection is not None:
            connection.close()
    rows = ws.forecast_rows(out)
    forecasts_path = out_folder / f'forecasts_{variant}.parquet'
    if resume:
        rows = pd.concat([pd.read_parquet(forecasts_path), rows], ignore_index=True)
    out_folder.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(forecasts_path, index=False)
    manifest = ws.publish_checkpoint(root, state, commit=code, extra={
        'replay_first_day': PRODUCTION_FIRST_FORECAST.isoformat(), 'clean_table': clean_table(),
        'consumption_rule': 'backtest rule (D2 only); ingestion filter not applicable',
        'revision_months_used_last_day': report['mappings'].get(last.isoformat(), [])})
    report['manifest'] = manifest
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    LOGGER.info('checkpoint %s written, last day %s, total %.0f s', variant, state.last_day,
                time.perf_counter() - began)


def combine(root: Path) -> None:
    folder = root / 'replay'
    forecasts = pd.concat([pd.read_parquet(folder / f'forecasts_{v}.parquet') for v in ws.VARIANTS],
                          ignore_index=True)
    forecasts.to_parquet(folder / 'replay_forecasts.parquet', index=False)
    scores = ws.score_rows(forecasts, read_clean())
    scores.to_parquet(folder / 'replay_scores.parquet', index=False)
    summary = {}
    for (variant, vintage), group in scores.groupby(['variant', 'actual_vintage']):
        slots = group['n_common_slots']
        summary[f'{variant}_{vintage}'] = {
            'days': len(group), 'first': str(group['delivery_day'].min()),
            'last': str(group['delivery_day'].max()),
            **{f'{name}_mae': float((group[f'{name}_mae'] * slots).sum() / slots.sum())
               for name in ('model', 'rte_d1', 'rte_star') if f'{name}_mae' in group
               and group[f'{name}_mae'].notna().all()},
            'model_coverage': float((group['model_coverage'] * slots).sum() / slots.sum())}
    (folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('run', 'combine'))
    parser.add_argument('--variant', choices=ws.VARIANTS)
    parser.add_argument('--last', type=date.fromisoformat)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--allow-dirty', action='store_true', help='rehearsal only')
    args = parser.parse_args()
    root = Path(os.environ['LFF_RUNTIME_DIR'])
    if not root.is_absolute():
        raise SystemExit('LFF_RUNTIME_DIR must be an absolute path')
    (root / 'replay').mkdir(parents=True, exist_ok=True)
    name = f'progress_{args.variant}.log' if args.mode == 'run' else 'combine.log'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.FileHandler(root / 'replay' / name, encoding='utf-8'),
                                  logging.StreamHandler()])
    if args.mode == 'run':
        if args.variant is None:
            raise SystemExit('run needs --variant')
        run(root, args.variant, args.last, args.resume, args.allow_dirty)
    else:
        combine(root)


if __name__ == '__main__':
    main()

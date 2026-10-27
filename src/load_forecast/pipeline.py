"""Production orchestration independent of the scheduler; local artifacts are trusted inputs."""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import requests
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy import URL, create_engine, text

from load_forecast import eco2mix, production, weather_production
from load_forecast.backtest import day_features, forecast_day, known_history, local_slots
from load_forecast.clean_snapshot import build_clean_snapshot
from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.database import clean_query
from load_forecast.deadline import deadline_check
from load_forecast.delivery_quality import degraded_input_reasons
from load_forecast.linear_model import calendar_ols_features
from load_forecast.pipeline_artifacts import read_artifact, write_artifact
from load_forecast.production_tracking import finish_tracking, start_tracking
from load_forecast.revision_mapping import apply_revision_mapping, estimate_revision_mapping
from load_forecast.serving import ForecastStore, ServingAuthority
from load_forecast.weather import candidate_runs

LOGGER = logging.getLogger(__name__)


@dataclass
class FrozenInputs:
    day: date
    history: pd.Series
    temperature: pd.DataFrame
    realtime: pd.Series
    offsets: pd.Series
    max_ingested_at: str | None
    selection: dict
    weather: list[dict]
    snapshot_hash: str

    def mapped(self, variant: str) -> pd.Series:
        """Only real-time values move to the consolidated scale (D21); training actuals stay put."""
        if variant not in ('A', 'B'):
            raise ValueError('Expected A/B variant')
        if variant == 'A':
            return self.history.copy()
        shifted = apply_revision_mapping(self.history, self.offsets)
        return self.history.where(~self.realtime.reindex(self.history.index, fill_value=False), shifted)


def fallback_values(day: date, history: pd.Series, models: production.WeeklyModels | None,
                    bounds: tuple[float, float]) -> list[dict]:
    """P10: stored calendar model, UTC seven-day lag, then four prior same-weekday slot means."""
    if not (0 < bounds[0] < bounds[1] < np.inf):
        raise ValueError('Fallback needs frozen, finite consumption bounds')
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))
    point = pd.Series(np.nan, index=slots)
    if models is not None:
        point = forecast_day(models.slots['calendar'], history, day, models.refit,
                             calendar_ols_features)['model']
    local = history.index.tz_convert(PARIS)
    for stamp in slots:
        value = point.loc[stamp]
        if not np.isfinite(value) or not bounds[0] <= value <= bounds[1]:
            value = history.get(stamp - pd.Timedelta(days=7), np.nan)
        if not np.isfinite(value) or not bounds[0] <= value <= bounds[1]:
            target = stamp.tz_convert(PARIS)
            selected = ((local.hour == target.hour) & (local.minute == target.minute)
                        & (local.weekday == target.weekday()) & (local.date < day))
            available = history.loc[selected].dropna()
            dates = available.index.tz_convert(PARIS).date
            value = available.groupby(dates).mean().sort_index().tail(4).mean()
        if not np.isfinite(value) or not bounds[0] <= value <= bounds[1]:
            raise ValueError(f'Fallback incomplete for {day}')
        point.loc[stamp] = value
    return [{'value_time': stamp.isoformat(), 'local_slot': int(slot), 'point': float(value),
             'lower': None, 'upper': None, 'n_pool': None}
            for stamp, slot, value in zip(slots, local_slots(slots), point, strict=True)]


def archive_run(start: dict, output: pd.DataFrame, state: production.ProductionState,
                inputs: FrozenInputs, completed: datetime, state_folder: Path,
                state_hash: str, *, stale: bool) -> dict:
    """Serialize the actual outputs and provenance, never substitute another variant's state."""
    def finite(value):
        return float(value) if pd.notna(value) else None
    weights = state.mlpoly.weights()
    kalman = state.kalman
    features = pd.concat({name: day_features(inputs.mapped(state.variant), inputs.day, builder)
                          for name, builder in production.feature_builders(inputs.temperature).items()},
                         axis=1)
    feature_hash = hashlib.sha256(pd.util.hash_pandas_object(features).values.tobytes()
                                 + repr(features.columns.tolist()).encode()).hexdigest()
    last_slot = known_history(inputs.history, inputs.day, None).dropna().index.max()
    last_slot = last_slot.isoformat() if pd.notna(last_slot) else None
    try:
        degraded = degraded_input_reasons(inputs.day, last_slot,
            bool(output[[f'{name}_backup' for name in production.EXPERTS]].to_numpy().any()))
    except Exception:
        LOGGER.exception('Delivery quality diagnosis failed for %s', inputs.day)
        degraded = ['unknown']
    return {'kind': 'primary', 'variant': state.variant, 'id': start['run_id'],
            'delivery_day': start['delivery_day'], 'start': start,
            'result': {'run_id': start['run_id'], 'completed_at': completed.isoformat(),
                       'status': 'complete', 'refit_day': state.models.refit.isoformat(),
                       'mlflow_run_id': None, 'kalman_quarter': str(kalman.fitted_quarter),
                       'weather_status': inputs.selection['status'],
                       'last_consumption_slot': last_slot,
                       'max_ingested_at': inputs.max_ingested_at,
                       'feature_hash': feature_hash,
                       'flags': {'stale_models': stale, 'provisional': True,
                                 'degraded_inputs': degraded,
                                 'refit_id': state.models.identity,
                                 'interval_not_calibrated': bool(output['lower'].isna().any())}},
            'values': [{'value_time': stamp.isoformat(), 'local_slot': int(slot),
                        'point': float(row['model']), 'lower': finite(row['lower']),
                        'upper': finite(row['upper']), 'n_pool': int(row['n_pool'])}
                       for (stamp, row), slot in zip(output.iterrows(), local_slots(output.index),
                                                     strict=True)],
            'experts': [{'value_time': stamp.isoformat(), 'expert': name,
                         'value': float(row[name]),
                         'backup': 'backup_chain' if row[f'{name}_backup'] else None}
                        for stamp, row in output.iterrows() for name in production.EXPERTS],
            'weather': inputs.weather,
            'states': [{'component': 'runtime', 'state_key': 'artifact',
                        'payload': {'folder': str(state_folder), 'sha256': state_hash,
                                    'day': start['delivery_day'], 'snapshot': inputs.snapshot_hash}},
                       {'component': 'mlpoly', 'state_key': 'shared',
                        'payload': {'weight': weights.tolist(), 'regret': state.mlpoly.regret.tolist(),
                                    'squared_regret': state.mlpoly.squares.tolist(),
                                    'absorbed_through': str(state.absorbed_through)}},
                       {'component': 'kalman', 'state_key': 'slots',
                        'payload': {'delta': kalman.filter.delta.tolist(),
                                    'covariance': kalman.filter.covariance.tolist(),
                                    'params': kalman.params.tolist(),
                                    'last_day': str(kalman.filter.last_day)}}]}


class Runtime:
    """Adapters to the existing collectors, snapshot, forecast and serving implementations."""

    def __init__(self, root: Path, store: ForecastStore, *, clock=None):
        if not root.is_absolute():
            raise ValueError('Runtime directory must be an absolute configured path')
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.authority = ServingAuthority(self.root / 'serving', self.clock)
        self._inputs_lock = threading.RLock()

    def alert(self, event: str, day: date) -> None:
        """Persist operational incidents locally, including when SQL/Prefect are down."""
        LOGGER.error('%s for %s', event, day)
        write_artifact(self.root / 'incidents' / f'{day}_{uuid4().hex}.pkl',
                       {'event': event, 'day': day, 'time': self.clock()})

    def collect(self, day: date) -> dict:
        return eco2mix.collect_realtime_30min(start=day - timedelta(days=2),
                                            end=day + timedelta(days=2))

    def stage(self, day: date) -> dict:
        with eco2mix.connect_database() as connection, requests.Session() as session:
            session.headers['User-Agent'] = 'load-forecast-fr/gfs-collector'
            return weather_production.stage_candidates(connection, session, day)

    def refresh(self, day: date) -> dict:
        result = eco2mix.collect_consolidated_weekly(end=day + timedelta(days=1))
        subprocess.run([sys.executable, '-m', 'dbt.cli.main', 'run', '--project-dir', 'dbt',
                        '--profiles-dir', 'dbt', '--target', 'prod'], check=True,
                       capture_output=True)
        return result

    def load_state(self, day: date, variant: str) -> production.ProductionState:
        with self.store.engine.connect() as connection:
            run_id = connection.execute(text('''SELECT run_id FROM forecast.state_runs
                WHERE delivery_day < :day AND variant = :variant ORDER BY delivery_day DESC LIMIT 1'''),
                {'day': day.isoformat(), 'variant': variant}).scalar_one_or_none()
        if run_id is None:
            state = read_artifact(self.root / 'checkpoints' / f'{variant}.pkl')
        else:
            archive = self.store.replay_run(run_id)
            reference = next(row['payload'] for row in archive['states']
                             if row['component'] == 'runtime' and row['state_key'] == 'artifact')
            folder = Path(reference['folder']).resolve()
            if not folder.is_relative_to(self.root / 'runs'):
                raise ValueError('State artifact escapes the trusted run directory')
            path = production.StateStore(folder).state_path(date.fromisoformat(reference['day']))
            if hashlib.sha256(path.read_bytes()).hexdigest() != reference['sha256']:
                raise ValueError('Selected state artifact hash mismatch')
            from load_forecast.retention import kept_models
            state = production.StateStore(folder).load(
                date.fromisoformat(reference['day']), model_source=lambda refit, identity, digest:
                    kept_models(self.root, variant, refit, identity, digest,
                                reference=folder / f'refit_{identity}.pkl'))
        if state.variant != variant or state.period != 'production':
            raise ValueError('Selected state has the wrong variant or period')
        if state.last_day is not None and state.last_day >= day:
            raise ValueError('Checkpoint must precede delivery day')
        return state

    def inputs(self, day: date) -> FrozenInputs:
        with self._inputs_lock:
            return self._inputs(day)

    def _inputs(self, day: date) -> FrozenInputs:
        path = self.root / 'inputs' / f'{day}.pkl'
        if path.exists():
            return read_artifact(path)
        build_clean_snapshot(day, schema=os.environ['LFF_SNAPSHOT_SCHEMA'],
                             now=self.clock())
        with eco2mix.connect_database() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(clean_query('SELECT * FROM {table} ORDER BY value_time'))
            clean = pd.DataFrame(cursor.fetchall()).set_index('value_time')
            clean.index = pd.DatetimeIndex(pd.to_datetime(clean.index, utc=True))
            # Historic weather is rebuilt from the immutable publication identities at each
            # day's own cutoff, not today's selected run. This also avoids the experiment lock.
            selected_today = weather_production.select_staged(connection, day, now=self.clock())
            cursor.execute("SELECT * FROM raw.gfs_points WHERE variable = 'TMP'")
            raw = cursor.fetchall()
            groups = {}
            for row in raw:
                groups.setdefault(row['run_init_time'], []).append(row)
            temperatures, current, weather = [], None, []
            first = production.FROZEN.training_start
            for past in pd.date_range(first, day).date:
                rows = [r for run in candidate_runs(past) for r in groups.get(run.init_time, [])]
                frame, selection, pinned = weather_production.pinned_temperature(
                    past, rows, now=self.clock(), selection=selected_today if past == day else None)
                temperatures.append(frame)
                if past == day:
                    current = selection
                    weather = [{key: (row[key].isoformat() if isinstance(row[key], datetime)
                                      else row[key]) for key in (
                        'run_init_time', 'end_step', 'variable', 'point', 'message_hash',
                        'file_last_modified', 'index_last_modified')}
                        | {'selection_hash': selection['selection_hash']}
                        for row in pinned.to_dict('records')]
            mapping = estimate_revision_mapping(day, connection)
        freeze = forecast_cutoff(day)
        clean = clean.loc[(pd.to_datetime(clean['ingested_at'], utc=True) < freeze)
                          & (clean.index <= freeze - timedelta(hours=2, minutes=30))]
        maximum = pd.to_datetime(clean['ingested_at'], utc=True).max()
        result = FrozenInputs(day, clean['consommation'].astype(float), pd.concat(temperatures),
                              clean['source_dataset'].eq('eco2mix-national-tr'), mapping.offsets,
                              maximum.isoformat() if pd.notna(maximum) else None, current, weather,
                              hashlib.sha256(pd.util.hash_pandas_object(clean).values.tobytes()
                                             + pd.util.hash_pandas_object(pd.concat(temperatures))
                                             .values.tobytes()
                                             + mapping.offsets.to_numpy().tobytes()).hexdigest())
        write_artifact(path, result)
        return result

    def refit_path(self, monday: date, variant: str) -> Path:
        return self.root / 'refits' / variant / f'{monday}.pkl'

    def prepare_refit(self, day: date, variant: str) -> None:
        monday = day - timedelta(days=day.weekday())
        target = self.refit_path(monday, variant)
        if target.exists():
            return
        inputs = self.inputs(monday)
        state = self.load_state(monday, variant)
        prepared = production.prepare_weekly_refit(
            state, inputs.mapped(variant), inputs.temperature, monday, inputs.snapshot_hash,
            clock=self.clock)
        # Exactly one immutable winner per variant/week, with its own models.identity.
        try:
            write_artifact(target, prepared)
        except ValueError:
            if not target.exists():
                raise

    def start_refit(self, day: date, variant: str) -> subprocess.Popen:
        command = [sys.executable, '-m', 'load_forecast.pipeline', 'refit', '--day', str(day),
                   '--variant', variant]
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def choose_models(self, day: date, variant: str,
                      state: production.ProductionState) -> bool:
        monday = day - timedelta(days=day.weekday())
        if state.models is not None and state.models.refit == monday:
            return False
        path = self.refit_path(monday, variant)
        prepared_here = False
        if not path.exists():
            if day.weekday() == 0:
                self.start_refit(day, variant)
                while self.clock() < forecast_cutoff(day) + timedelta(minutes=20) and not path.exists():
                    time.sleep(1)
            else:
                self.prepare_refit(day, variant)
                prepared_here = True
        if path.exists():
            prepared = read_artifact(path)
            if prepared.variant != variant or prepared.models.refit != monday:
                raise ValueError('Wrong refit artifact')
            # Monday may adopt its synchronous refit until 12:20. Later days only use
            # refits complete before their freeze; a refit made today is used as usual.
            timely_monday = (day.weekday() == 0 and
                             prepared.completed_at < forecast_cutoff(day) + timedelta(minutes=20))
            if timely_monday or prepared.completed_at < forecast_cutoff(day) or prepared_here:
                available_before = (forecast_cutoff(day) + timedelta(minutes=20) if timely_monday
                                    else self.clock() + timedelta(microseconds=1) if prepared_here
                                    else forecast_cutoff(day))
                production.adopt_refit(state, prepared, day, available_before=available_before)
                return False
            if day.weekday() != 0:
                # No completed refit existed at today's freeze: refit as usual (D24).
                inputs = self.inputs(monday)
                fresh = production.prepare_weekly_refit(
                    self.load_state(monday, variant), inputs.mapped(variant), inputs.temperature,
                    monday, inputs.snapshot_hash, clock=self.clock)
                write_artifact(self.root / 'refit_attempts' / f'{fresh.models.identity}.pkl', fresh)
                production.adopt_refit(state, fresh, day,
                                       available_before=self.clock() + timedelta(microseconds=1))
                return False
        if state.models is None:
            raise RuntimeError('Weekly refit unavailable and no previous models exist')
        return True

    def forecast(self, day: date, variant: str) -> dict:
        production.check_period(day, 'production')
        now = self.clock()
        if now < forecast_cutoff(day):
            raise ValueError('Cannot forecast before freeze')
        if now >= forecast_cutoff(day) + timedelta(minutes=20):
            raise TimeoutError('A new daily forecast attempt is refused at or after 12:20')
        run_id = uuid4().hex
        start = {'run_id': run_id, 'delivery_day': day.isoformat(), 'variant': variant,
                 'freeze_time': forecast_cutoff(day).isoformat(), 'kind': 'primary',
                 'started_at': now.isoformat(), 'code_commit': os.environ['LFF_CODE_COMMIT']}
        self.store.start_run(start)
        tracking = None
        try:
            tracking = self.start_tracking(start)
            inputs = self.inputs(day)
            state = self.load_state(day, variant)
            stale = self.choose_models(day, variant, state)
            out = production.forecast_production_day(day, inputs.mapped(variant), inputs.temperature,
                                                     state, reuse_models=True)
            folder = self.root / 'runs' / run_id
            path = production.StateStore(folder).save(state)
            if tracking is not None:
                finish_tracking(tracking, models=state.models, kalman=state.kalman)
            archive = archive_run(start, out, state, inputs, self.clock(), folder,
                                  hashlib.sha256(path.read_bytes()).hexdigest(), stale=stale)
            archive['result']['mlflow_run_id'] = tracking
            self.store.complete_run(archive)
        except Exception:
            self.alert('forecast_failed', day)
            try:
                self.store.fail_run(run_id, self.clock(), {'reason': 'forecast_failed'})
                if tracking is not None:
                    finish_tracking(tracking, status='FAILED')
            except Exception:
                self.alert('failure_recording_unavailable', day)
            raise
        if variant == 'B':
            try:
                choice = self.authority.publish_primary(archive)
                if choice['kind'] != 'primary' or choice['id'] != run_id:
                    LOGGER.warning('Primary unserved for %s variant %s at %s: choice already claimed',
                                   day, variant, self.clock().isoformat())
            except TimeoutError as error:
                LOGGER.warning('Late primary for %s variant %s at %s: %s',
                               day, variant, self.clock().isoformat(), error)
        if archive['result'].get('flags', {}).get('degraded_inputs'):
            try:
                self.alert('degraded_inputs:' + ','.join(
                    archive['result']['flags']['degraded_inputs']), day)
            except Exception:
                LOGGER.error('Could not persist degraded inputs alert for %s', day)
        return {'run_id': run_id, 'variant': variant, 'stale_models': stale}

    def start_tracking(self, start: dict) -> str | None:
        return start_tracking(start)

    def prepare_fallback(self, day: date) -> dict:
        try:
            self.authority.read_served(day)
            return {'status': 'already_delivered'}
        except FileNotFoundError:
            pass
        prepared_path = self.authority.root / 'fallback' / f'{day}.json'
        if prepared_path.exists():
            self.authority._archive(json.loads(prepared_path.read_text(encoding='utf-8')))
            return {'status': 'already_prepared', 'day': str(day)}
        with eco2mix.connect_database() as connection, connection.cursor() as cursor:
            base_table = os.environ.get('LFF_BASE_CLEAN_TABLE', 'analytics.load_30min')
            cursor.execute(sql.SQL('SELECT value_time, consommation FROM {} ORDER BY value_time')
                           .format(sql.Identifier(*base_table.split('.'))))
            rows = cursor.fetchall()
            history = pd.Series([r[1] for r in rows],
                                index=pd.DatetimeIndex([r[0] for r in rows]).tz_convert('UTC'))
            history = history.loc[history.index < self.clock() - timedelta(hours=2, minutes=30)]
            bounds_path = self.root / 'fallback_bounds.pkl'
            if not bounds_path.exists():
                cursor.execute(sql.SQL('''SELECT min(consommation), max(consommation) FROM raw.eco2mix
                    WHERE source_dataset = 'eco2mix-national-cons-def'
                    AND value_time >= %s AND value_time < %s'''),
                    (datetime(2021, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)))
                write_artifact(bounds_path, tuple(float(v) for v in cursor.fetchone()))
        state = self.load_state(day, 'B')
        archive = {'kind': 'fallback', 'id': uuid4().hex, 'delivery_day': day.isoformat(),
                   'prepared_at': self.clock().isoformat(),
                   'models': {'refit_id': state.models.identity if state.models else None},
                   'completeness': {'complete': True, 'bounds': read_artifact(bounds_path)},
                   'values': fallback_values(day, history, state.models, read_artifact(bounds_path))}
        self.store.store_fallback(archive)
        self.authority.prepare_fallback(archive)
        return {'status': 'prepared', 'day': str(day)}

    def reconcile(self) -> dict:
        count = 0
        for path in (self.authority.root / 'choice').glob('*.json'):
            self.store.reconcile(self.authority, date.fromisoformat(path.stem))
            count += 1
        return {'reconciled': count}

    def score(self, day: date) -> dict:
        """Score delivered history through day, including newly consolidated older days."""
        from load_forecast.production_scoring import score_production
        return score_production(self.store, self.authority, day, self.clock(), self.scoring_actuals)

    def scoring_actuals(self, connection, start: date, end: date, as_of: datetime) -> dict:
        from load_forecast.production_scoring import read_actuals
        return read_actuals(connection, start, end, as_of)

    def prune(self, day: date, *, dry_run: bool = True) -> dict:
        """Serialize against run completion while pruning redundant local artifacts."""
        from load_forecast.retention import prune_artifacts
        with self.store.engine.begin() as connection:
            if connection.dialect.name == 'postgresql':
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('load_forecast:retention'))"))
            rows = connection.execute(text('''SELECT s.run_id, s.delivery_day, s.variant, a.payload
                FROM forecast.state_runs s LEFT JOIN forecast.states a ON a.run_id = s.run_id
                AND a.component = 'runtime' AND a.state_key = 'artifact' ''')).mappings().all()
            selected = []
            for row in rows:
                reference = json.loads(row['payload'])
                if Path(reference['folder']).resolve() != self.root / 'runs' / row['run_id']:
                    raise ValueError('Selected state folder differs from its run identity')
                selected.append({'run_id': row['run_id'], 'variant': row['variant'],
                                 'day': date.fromisoformat(str(row['delivery_day'])),
                                 'sha256': reference['sha256']})
            active = set(connection.execute(text('''SELECT s.run_id FROM forecast.run_starts s
                LEFT JOIN forecast.run_results r ON r.run_id = s.run_id
                WHERE r.run_id IS NULL''')).scalars())
            return prune_artifacts(self.root, selected, self.clock(), protected=active, dry_run=dry_run)

    def publish_site(self, day: date, *, dry_run: bool = False) -> dict:
        """Export and publish after forecasting or scoring; failure is a local incident."""
        engine = None
        try:
            from load_forecast.site_export import export_site_data, get_reader_engine
            clone = os.environ.get('LFF_SITE_CLONE')
            if not clone:
                raise ValueError('Missing LFF_SITE_CLONE')
            workspace = Path(__file__).resolve().parents[2]
            engine = get_reader_engine()
            export_site_data(engine=engine, output_path=workspace / 'site/data/latest.json',
                             replay_source=os.environ.get('LFF_REPLAY_DIR'))
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                       'Bypass', '-File', str(workspace / 'scripts/publish_site.ps1'),
                       '-PublicRepoPath', clone]
            if dry_run:
                command.append('-DryRun')
            subprocess.run(command, check=True, capture_output=True, timeout=180)
            return {'status': 'published' if not dry_run else 'local_commit_only'}
        except Exception as error:
            # Exceptions can include connection URLs or subprocess output: do not persist them.
            reason = f'site_publication_failed:{type(error).__name__}'
            try:
                self.alert(reason, day)
            except Exception:
                LOGGER.error('Unable to persist site publication incident for %s', day)
            return {'status': 'failed', 'reason': reason}
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    LOGGER.error('Unable to close site reader for %s', day)


def make_runtime() -> Runtime:
    """No connections or shared writes occur until a scheduled adapter is invoked."""
    root = Path(os.environ['LFF_RUNTIME_DIR'])
    url = URL.create('postgresql+psycopg', username=os.environ['POSTGRES_USER'],
                     password=os.environ['POSTGRES_PASSWORD'],
                     host=os.environ.get('POSTGRES_HOST', '127.0.0.1'),
                     port=int(os.environ.get('POSTGRES_PORT', '5432')),
                     database=os.environ.get('POSTGRES_DB', 'load_forecast'))
    return Runtime(root, ForecastStore(create_engine(url, connect_args={'connect_timeout': 5})))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('deadline', 'refit', 'prune'))
    parser.add_argument('--day', type=date.fromisoformat)
    parser.add_argument('--variant', choices=('A', 'B'), default='B')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    day = args.day or (datetime.now(PARIS).date() + timedelta(days=1))
    if args.mode == 'deadline':
        result = deadline_check(Path(os.environ['LFF_RUNTIME_DIR']), day)
        print(json.dumps(result))
    elif args.mode == 'prune':
        print(json.dumps(make_runtime().prune(day, dry_run=args.dry_run)))
    else:
        runtime = make_runtime()
        try:
            runtime.prepare_refit(day, args.variant)
        except Exception:
            runtime.alert('weekly_refit_failed', day)
            raise


if __name__ == '__main__':
    main()

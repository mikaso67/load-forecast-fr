"""Offline rehearsal on synthetic inputs and a private SQLite copy of the forecast tables."""

import copy
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, event, text

from load_forecast import flows, production
from load_forecast.cutoff import PARIS, forecast_cutoff
from load_forecast.deadline import deadline_check
from load_forecast.gbm_model import GBMConfig
from load_forecast.pipeline import FrozenInputs, Runtime, fallback_values
from load_forecast.pipeline_artifacts import read_artifact
from load_forecast.serving import ForecastStore, delivery_deadline


def local_store(root: Path) -> ForecastStore:
    """Apply only the table section of the actual migration to a private, disposable SQLite DB."""
    root.mkdir(parents=True, exist_ok=True)
    existing = (root / 'forecast.sqlite').exists()
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False})
    @event.listens_for(engine, 'connect')
    def attach(connection, _):
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('ATTACH DATABASE ? AS forecast', (str(root / 'forecast.sqlite'),))
        connection.execute('PRAGMA busy_timeout = 10000')
    migration = Path(__file__).resolve().parents[2] / 'infra/postgres/forecast.sql'
    tables = migration.read_text(encoding='utf-8').split('-- BEGIN TABLES', 1)[1]
    tables = tables.split('\n', 1)[1].split('-- END TABLES')[0]
    if not existing:
        with engine.connect() as connection:
            connection.connection.driver_connection.executescript(tables)
            migration = migration.with_name('forecast_scores.sql')
            tables = migration.read_text(encoding='utf-8').split('-- BEGIN TABLES', 1)[1]
            tables = tables.split('-- END TABLES')[0]
            # SQLite qualifies an attached index name instead of its table name.
            tables = tables.replace('INDEX scores_identity ON forecast.scores',
                                    'INDEX forecast.scores_identity ON scores')
            connection.connection.driver_connection.executescript(tables)
    return ForecastStore(engine)


class OfflineRuntime(Runtime):
    """Real forecast/SQL/filesystem path, with synthetic data and no collection or tracking IO."""

    def __init__(self, root: Path, day: date):
        production.check_period(day, 'production')
        self.day, self.now = day, forecast_cutoff(day)
        super().__init__(root, local_store(root), clock=lambda: self.now)
        first = day - timedelta(days=100)
        index = pd.date_range(pd.Timestamp(first, tz='Europe/Paris').tz_convert('UTC'),
                              pd.Timestamp(day + timedelta(days=1), tz='Europe/Paris')
                              .tz_convert('UTC'), freq='30min', inclusive='left')
        hours = np.arange(len(index)) / 2
        self.history = pd.Series(50_000 + 3_000 * np.sin(hours * 2 * np.pi / 24)
                                 + 100 * np.sin(hours / 71)
                                 + np.random.default_rng(71).normal(0, 300, len(index)), index=index)
        self.temperature = pd.DataFrame({'temperature_c': 10 + 5 * np.sin(hours / 97)}, index=index)
        self.settings = production.Settings(training_start=first, gam_smoothing_weight=1e3,
            gbm=GBMConfig(learning_rate=.1, num_leaves=7, min_data_in_leaf=20, rounds=30))
        self.initial = {}
        # Separate seed histories preserve the same state protocol without historical forecasts.
        for variant in ('A', 'B'):
            stamps = index[(index.tz_convert('Europe/Paris').date >= day - timedelta(days=30))
                           & (index.tz_convert('Europe/Paris').date < day)]
            experts = pd.DataFrame({name: self.history.loc[stamps] + bias
                for name, bias in zip(production.EXPERTS, (100, -200, 150, 50), strict=True)})
            state = production.seed_state(experts, self.history, 'production', self.settings)
            state.variant = variant
            self.initial[variant] = state

    def inputs(self, day: date) -> FrozenInputs:
        return FrozenInputs(day, self.history, self.temperature,
                            pd.Series(True, index=self.history.index),
                            pd.Series(500., index=range(24)),
                            (forecast_cutoff(day) - timedelta(seconds=1)).isoformat(),
                            {'status': 'weather missing'}, [], 'synthetic-inputs')

    def load_state(self, day: date, variant: str) -> production.ProductionState:
        with self.store.engine.connect() as connection:
            count = connection.execute(text('''SELECT count(*) FROM forecast.state_runs
                WHERE delivery_day < :day AND variant = :variant'''),
                {'day': str(day), 'variant': variant}).scalar_one()
        return super().load_state(day, variant) if count else copy.deepcopy(self.initial[variant])

    def choose_models(self, day: date, variant: str, state: production.ProductionState) -> bool:
        # The rehearsal uses the real weekly fit with deliberately reduced synthetic settings.
        self.prepare_refit(day, variant)
        prepared = copy.deepcopy(read_artifact(
            self.refit_path(day - timedelta(days=day.weekday()), variant)))
        state.models, state.kalman = prepared.models, prepared.kalman
        return False

    def collect(self, day: date) -> dict:
        return {'day': str(day), 'mode': 'offline', 'external_calls': 0, 'shared_writes': 0}

    def stage(self, day: date) -> dict:
        return {'day': str(day), 'mode': 'offline', 'external_calls': 0, 'shared_writes': 0}

    def refresh(self, day: date) -> dict:
        return {'day': str(day), 'mode': 'offline', 'external_calls': 0, 'shared_writes': 0}

    def start_tracking(self, start: dict) -> None:
        return None

    def publish_site(self, day: date, *, dry_run: bool = False) -> dict:
        return {'status': 'offline', 'external_calls': 0}

    def scoring_actuals(self, connection, start: date, end: date, as_of: datetime) -> dict:
        frame = pd.DataFrame({'consommation': self.history, 'prevision_j1': self.history + 100.,
                              'ingested_at': pd.Timestamp(as_of) - pd.Timedelta(seconds=1),
                              'payload_hash': 'synthetic-actuals'})
        return {'realtime': frame, 'consolidated': frame.copy()}

    def prepare_fallback(self, day: date) -> dict:
        values = fallback_values(day, self.history.loc[self.history.index < forecast_cutoff(day)],
                                 None, (10_000., 100_000.))
        archive = {'kind': 'fallback', 'id': f'offline-{day}', 'delivery_day': str(day),
                   'prepared_at': self.now.isoformat(), 'models': {'kind': 'offline'},
                   'completeness': {'complete': True}, 'values': values}
        self.store.store_fallback(archive)
        self.authority.prepare_fallback(archive)
        return {'day': str(day), 'status': 'prepared'}


def dry_run(day: date) -> dict:
    """Run every flow body, actual forecasts, SQL transactions and deadline check, entirely offline."""
    if day >= datetime.now(PARIS).date():
        raise ValueError('Rehearsal requires a past delivery day')
    with tempfile.TemporaryDirectory() as directory:
        runtime = OfflineRuntime(Path(directory), day)
        previous_commit = os.environ.get('LFF_CODE_COMMIT')
        os.environ['LFF_CODE_COMMIT'] = 'offline-rehearsal'
        try:
            with flows.use_runtime(runtime):
                results = {
                    'collect_consumption': flows.collect_consumption.fn(day),
                    'stage_weather': flows.stage_weather.fn(day),
                    'prepare_fallback': flows.prepare_fallback.fn(day),
                    'daily_forecast': flows.daily_forecast.fn(day),
                }
                runtime.now = delivery_deadline(day)
                results['deadline_check'] = deadline_check(runtime.root, day, clock=runtime.clock)['kind']
                results['reconcile_deliveries'] = flows.reconcile_deliveries.fn()
                runtime.now = datetime.combine(day + timedelta(days=1),
                                               datetime.min.time(), PARIS) + timedelta(hours=8)
                results['score_and_monitor'] = flows.score_and_monitor.fn(day)
                results['consolidated_refresh'] = flows.consolidated_refresh.fn(day)
            with runtime.store.engine.connect() as connection:
                variants = connection.execute(text('''SELECT variant FROM forecast.state_runs
                    WHERE delivery_day = :day ORDER BY variant'''), {'day': str(day)}).scalars().all()
            assert variants == ['A', 'B']
            assert results['deadline_check'] == 'primary'
            results['state_variants'] = variants
            results['mode'] = 'synthetic offline rehearsal; no shared writes or external calls'
            print(json.dumps(results, indent=2))
            return results
        finally:
            runtime.store.engine.dispose()
            if previous_commit is None:
                os.environ.pop('LFF_CODE_COMMIT', None)
            else:
                os.environ['LFF_CODE_COMMIT'] = previous_commit

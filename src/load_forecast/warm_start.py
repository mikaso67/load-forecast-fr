"""Warm start of the production states, from the cold start to the go-live day (D20 P2 A).

1. Reconstruction (docs/production_design.md section 2.4). ML-Poly and the conformal pool run over
   the archived expert forecasts of 2023 to 2025 with the production arithmetic. No model is
   fitted and no forecast of 2025 is computed. The result is accepted only if the weights equal
   the archived weights on every day and the aggregate equals the archived aggregate of 2024 and
   2025, and if the 2025 actuals read now equal the ones the test read.
2. Replay. From Monday 2026-01-05, each variant runs the production function day by day:
   Monday refits, quarterly Kalman variances (cold start in 2026-Q1), filter restarts, daily
   ML-Poly and conformal updates. Variant B maps real-time consumption to the consolidated scale
   with the offsets estimated at each day's freeze (D21); variant A uses consumption as stored.
   The two variants have independent states (D23).

Consumption follows the backtest rule (D2 only), not the ingestion filter of P1: every stored row
was ingested after the freeze of every replayed day, so the filter would leave no history at all.
This is the documented revision leak (section 2.4). The revision offsets keep the D21 rule: only
rows stored before each freeze enter them. Replayed days are labelled `replay`, never served.
"""

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast import monitoring, production
from load_forecast.backtest import known_history, local_slots
from load_forecast.cutoff import PARIS
from load_forecast.pipeline import FrozenInputs
from load_forecast.pipeline_artifacts import read_artifact, write_artifact
from load_forecast.rte_reference import prior_year_hourly_reference

REPLAY = 'replay'
VARIANTS = ('A', 'B')
WEIGHT_TOLERANCE = 1e-9
AGGREGATE_TOLERANCE_MW = 1e-6
ZERO_OFFSETS = pd.Series(0.0, index=range(24))
OffsetsFor = Callable[[date], pd.Series]


@dataclass(frozen=True)
class Archive:
    """Archived outputs of the 2023 burn-in, EXP-009 (2024) and the single 2025 test run."""
    experts: pd.DataFrame
    weights: pd.DataFrame
    aggregate: pd.Series
    actual_2025: pd.Series


def _csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    return frame


def read_archive(folder: Path) -> Archive:
    burn_in = _csv(folder / 'EXP-009' / 'experts_2023.csv')
    test = _csv(folder / 'TEST-2025' / 'experts_2024_2025.csv')
    experts = pd.concat([burn_in, test])[list(production.EXPERTS)]
    if experts.index.has_duplicates or not experts.index.is_monotonic_increasing:
        raise ValueError('Archived experts overlap or are out of order')
    weights = pd.read_csv(folder / 'TEST-2025' / 'weights.csv', index_col='day')
    weights.index = pd.Index([date.fromisoformat(d) for d in weights.index])
    predictions = _csv(folder / 'TEST-2025' / 'predictions.csv')
    aggregate = pd.concat([_csv(folder / 'EXP-009' / 'backtest_predictions.csv')['model'],
                           predictions['candidate1']])
    return Archive(experts, weights[list(production.EXPERTS)], aggregate, predictions['actual'])


def check_2025_actuals(history: pd.Series, archived: pd.Series) -> float:
    """The 2025 actuals that enter the pool must be the ones the test read (section 2.4)."""
    now = history.reindex(archived.index)
    missing = now.isna() & archived.notna()
    if missing.any():
        raise RuntimeError(f'{int(missing.sum())} archived 2025 actuals are missing now')
    difference = float((now - archived).abs().max())
    if difference > AGGREGATE_TOLERANCE_MW:
        raise RuntimeError(f'2025 actuals differ from the test inputs by {difference} MW')
    return difference


def reconstruct(archive: Archive, history: pd.Series,
                settings: production.Settings = production.FROZEN) -> tuple[production.ProductionState, dict]:
    """ML-Poly and pool over the archived experts, the loop of `production.seed_state`, checked."""
    check_2025_actuals(history, archive.actual_2025)
    state = production.ProductionState(period='production', settings=settings)
    days = ev.local_dates(archive.experts.index)
    weight_gap = aggregate_gap = 0.0
    n_compared = 0
    for day, rows in archive.experts.groupby(days.to_numpy(), sort=True):
        known = known_history(history, day, None)
        weights, point = production.aggregate_day(state, day, rows, known)
        production.issue(state, day, rows, weights, point)
        weight_gap = max(weight_gap, float(np.abs(weights[0] - archive.weights.loc[day]).max()))
        saved = archive.aggregate.reindex(point.index)
        if saved.notna().any():
            aggregate_gap = max(aggregate_gap, float((point - saved).abs().max()))
            n_compared += int(saved.notna().sum())
    n_days = len(np.unique(days))
    report = {'days': n_days, 'archived_weight_days': len(archive.weights),
              'max_weight_difference': weight_gap, 'aggregate_slots_compared': n_compared,
              'max_aggregate_difference_mw': aggregate_gap}
    if (n_days != len(archive.weights) or weight_gap > WEIGHT_TOLERANCE
            or aggregate_gap > AGGREGATE_TOLERANCE_MW or n_compared != archive.aggregate.notna().sum()):
        raise RuntimeError(f'Reconstruction does not match the archive: {report}')
    return state, report


def for_variant(state: production.ProductionState, variant: str) -> production.ProductionState:
    if variant not in VARIANTS:
        raise ValueError('Expected variant A or B')
    copied = copy.deepcopy(state)
    copied.variant = variant
    return copied


def mapped_history(history: pd.Series, realtime: pd.Series, offsets: pd.Series,
                   variant: str) -> pd.Series:
    """The series the production run of `variant` reads, through the production code itself."""
    inputs = FrozenInputs(date.min, history, pd.DataFrame(), realtime, offsets, None, {}, [], '')
    return inputs.mapped(variant)


def replay_days(state: production.ProductionState, first: date, last: date, history: pd.Series,
                realtime: pd.Series, temperature: pd.DataFrame, offsets_for: OffsetsFor, *,
                after_day: Callable[[date, production.ProductionState, pd.DataFrame], None]
                | None = None) -> pd.DataFrame:
    """Run the production function on every day of [first, last], advancing `state` in place.

    Day features are cached while the mapped series stays the same, that is while the offsets of
    variant B do not change; variant A never changes it.
    """
    if first > last:
        raise ValueError('Expected an ordered period')
    caches: dict[str, dict] = {}
    fingerprint = None
    outputs = []
    for day in pd.date_range(first, last).date:
        offsets = offsets_for(day) if state.variant == 'B' else ZERO_OFFSETS
        current = offsets.to_numpy(dtype=float).tobytes()
        if current != fingerprint:
            caches, fingerprint = {}, current
        mapped = mapped_history(history, realtime, offsets, state.variant)
        out = production.forecast_production_day(day, mapped, temperature, state, caches=caches)
        out.insert(0, 'delivery_day', day)
        out.insert(0, 'variant', state.variant)
        out['status'] = REPLAY
        out['offset_mean_mw'] = float(offsets.mean())
        outputs.append(out)
        if after_day is not None:
            after_day(day, state, out)
    return pd.concat(outputs)


def settings_hash(settings: production.Settings) -> str:
    text = json.dumps(asdict(settings), sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def checkpoint_path(root: Path, variant: str) -> Path:
    return root / 'checkpoints' / f'{variant}.pkl'


def publish_checkpoint(root: Path, state: production.ProductionState, *, commit: str,
                       extra: dict | None = None) -> dict:
    """Write `checkpoints/<variant>.pkl` (docs/operations.md section 1) and its manifest.

    An existing checkpoint is kept under `checkpoints/history/` and may only be extended to a
    later day; the file itself is immutable once written.
    """
    if state.period != 'production' or state.last_day is None or state.models is None:
        raise ValueError('Only a production state that has issued a day is a checkpoint')
    path = checkpoint_path(root, state.variant)
    manifest_path = path.with_suffix('.manifest.json')
    if path.exists():
        previous = json.loads(manifest_path.read_text(encoding='utf-8'))
        if date.fromisoformat(previous['last_day']) >= state.last_day:
            raise ValueError('A checkpoint can only move forward')
        history = root / 'checkpoints' / 'history'
        history.mkdir(parents=True, exist_ok=True)
        stem = f'{state.variant}_{previous["last_day"]}'
        path.replace(history / f'{stem}.pkl')
        manifest_path.replace(history / f'{stem}.manifest.json')
    digest = write_artifact(path, state)
    manifest = {'variant': state.variant, 'period': state.period,
                'last_day': state.last_day.isoformat(),
                'first_scheduled_day': (state.last_day + timedelta(days=1)).isoformat(),
                'commit': commit, 'settings_sha256': settings_hash(state.settings),
                'settings': asdict(state.settings), 'checkpoint_sha256': digest,
                'refit_day': state.models.refit.isoformat(), 'refit_id': state.models.identity,
                'kalman_quarter': list(state.kalman.fitted_quarter),
                'kalman_fits': [{'refit': fit['refit'].isoformat(), 'days': fit['days']}
                                for fit in state.kalman.fits],
                'mlpoly_absorbed_through': str(state.absorbed_through),
                'issued_days': [min(state.issued).isoformat(), max(state.issued).isoformat()],
                'written_at': datetime.now(UTC).isoformat(), **(extra or {})}
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding='utf-8')
    return manifest


def load_checkpoint(root: Path, variant: str) -> production.ProductionState:
    """Load and check a checkpoint against its manifest, as the pipeline will read it."""
    path = checkpoint_path(root, variant)
    manifest = json.loads(path.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    state = read_artifact(path, manifest['checkpoint_sha256'])
    if (state.variant != variant or state.period != 'production'
            or state.last_day.isoformat() != manifest['last_day']
            or settings_hash(state.settings) != manifest['settings_sha256']):
        raise ValueError('Checkpoint does not match its manifest')
    return state


def forecast_rows(out: pd.DataFrame) -> pd.DataFrame:
    """Slot rows of the replay parquet: forecast, experts, weights, intervals, all `replay`."""
    frame = out.rename(columns={ev.MODEL: 'point'}).copy()
    frame['delivery_day'] = pd.to_datetime(frame['delivery_day']).dt.date
    frame['refit_date'] = pd.to_datetime(frame['refit_date']).dt.date
    frame['local_slot'] = local_slots(frame.index)
    frame.index.name = 'value_time'
    return frame.reset_index()


def vintage_of(sources: pd.Series) -> str:
    realtime = sources.eq('eco2mix-national-tr')
    if realtime.all():
        return 'realtime'
    if not realtime.any():
        return 'consolidated'
    raise ValueError('A delivery day mixes real-time and consolidated actuals')


def score_rows(forecasts: pd.DataFrame, clean: pd.DataFrame) -> pd.DataFrame:
    """Daily scores of the replayed forecasts with the production scoring (section 4.2).

    `clean` holds `consommation`, `prevision_j1` and `source_dataset` on UTC slots. The replay is
    scored as the model; nothing is scored as served. RTE* (prior calendar year hourly mean error)
    is scored on consolidated days only.
    """
    clean = clean.sort_index()
    actual, rte = clean['consommation'].astype(float), clean['prevision_j1'].astype(float)
    targets = pd.DatetimeIndex(forecasts['value_time'].unique()).sort_values()
    rte_star = prior_year_hourly_reference(actual, rte, rte.reindex(targets))
    rows = []
    for (variant, day), group in forecasts.groupby(['variant', 'delivery_day'], sort=True):
        slots = pd.DatetimeIndex(group['value_time'])
        model = pd.DataFrame(group[['point', 'lower', 'upper']].to_numpy(dtype=float),
                             index=slots, columns=['point', 'lower', 'upper'])
        day_actual = actual.reindex(slots)
        sources = clean['source_dataset'].reindex(slots)[day_actual.notna()]
        row = {'variant': variant, 'delivery_day': day, 'status': REPLAY}
        if sources.empty:
            rows.append(row | {'actual_vintage': None, 'n_common_slots': 0})
            continue
        vintage = vintage_of(sources)
        star = None if vintage == 'realtime' else rte_star.reindex(slots)
        metrics = monitoring.score_day(day_actual, rte.reindex(slots), None, model, star, vintage)
        row |= {'actual_vintage': vintage, 'n_common_slots': metrics['n_common_slots']}
        for name in ('model', 'rte_d1', 'rte_star'):
            for key, value in (metrics[name] or {}).items():
                row[f'{name}_{key}'] = value
        rows.append(row)
    return pd.DataFrame(rows)


def last_complete_day(history: pd.Series, weather_status: pd.Series) -> date:
    """Last delivery day whose D-1 09:30 lag is stored and whose weather run was selected."""
    known = history.dropna()
    local = known.index.tz_convert(PARIS)
    lag_days = {stamp.date() + timedelta(days=1) for stamp in local
                if (stamp.hour, stamp.minute) == (9, 30)}
    weather_days = set(weather_status.index[weather_status.isin(['primary', 'fallback'])])
    return max(lag_days & weather_days)

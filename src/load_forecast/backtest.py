"""Expanding weekly fits with one model per Paris half-hour slot (D2, D6, M5).

Feature builders receive only consumption available at that delivery day's cutoff.
They must be pure: no captured data, database reads, fitted encoders or RTE inputs.
Archived weather can be added through a separately reviewed builder with explicit
publication provenance. Consolidated history still has the protocol's revision leak.
"""

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

import pandas as pd
import requests
from joblib import Parallel, delayed

from load_forecast import evaluation as ev
from load_forecast.backtest_plots import save_backtest_plots
from load_forecast.cutoff import available_at, delivery_slots, forecast_cutoff
from load_forecast.periods import last_allowed_day, locked_message
from load_forecast.rte_reference import RTE_STAR
from load_forecast.tracking import backtest_tags, mlflow_tracking_uri


class Model(Protocol):
    def fit(self, x: pd.DataFrame, y: pd.Series) -> object: ...

    def predict(self, x: pd.DataFrame) -> object: ...


FeatureBuilder = Callable[[pd.DatetimeIndex, pd.Series, date], pd.DataFrame]


def known_history(history: pd.Series, day: date, published_at: pd.Series | None) -> pd.Series:
    """Use both slot-end D2 and strict publication times, if supplied."""
    if (not isinstance(history.index, pd.DatetimeIndex) or str(history.index.tz) != 'UTC'
            or history.index.has_duplicates):
        raise ValueError('Expected unique UTC history')
    usable = history.loc[history.index <= ev.last_usable_slot_start(day)].copy()
    if published_at is not None:
        published = published_at.reindex(usable.index)
        if not isinstance(published.dtype, pd.DatetimeTZDtype):
            raise ValueError('Publication timestamps must be aware')
        usable = available_at(pd.DataFrame({'value': usable, 'published_at': published}),
                              forecast_cutoff(day))['value']
    return usable.sort_index()


def calendar_features(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
    """Deterministic calendar inputs for the initial runner; no fitted statistics."""
    local = slots.tz_convert('Europe/Paris')
    return pd.DataFrame({'weekday': local.dayofweek, 'day_of_year': local.dayofyear}, index=slots)


def naive_features(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
    """UTC seasonal lag from already restricted history, reproducing EXP-001."""
    return pd.DataFrame({'lag_7d': history.reindex(slots - pd.Timedelta(days=7)).to_numpy()},
                        index=slots)


class SeasonalNaive:
    """Fit/predict adapter for the no-fit UTC reference."""

    def fit(self, x: pd.DataFrame, y: pd.Series) -> 'SeasonalNaive':
        return self

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return x['lag_7d']


def day_features(
    history: pd.Series, day: date, builder: FeatureBuilder,
    published_at: pd.Series | None = None,
) -> pd.DataFrame:
    """Shared production/backtest feature path; expose no post-cutoff consumption."""
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))
    features = builder(slots, known_history(history, day, published_at), day)
    if not features.index.equals(slots) or features.columns.has_duplicates:
        raise ValueError('Feature builder must return unique columns on the exact delivery grid')
    if set(features.columns) & {'actual', 'rte', 'consommation', 'prevision_j1', 'prevision_j'}:
        raise ValueError('Raw target and RTE columns are forbidden as features')
    return features


def local_slots(index: pd.DatetimeIndex) -> pd.Index:
    """Both occurrences of an autumn wall-clock slot share the same model."""
    local = index.tz_convert('Europe/Paris')
    return pd.Index(local.hour * 2 + local.minute // 30)


def forecast_day(
    models: dict[int, Model], history: pd.Series, day: date, refit_day: date,
    builder: FeatureBuilder = calendar_features, published_at: pd.Series | None = None,
) -> pd.DataFrame:
    """Production entry point used unchanged by the walk-forward loop."""
    if refit_day > day:
        raise ValueError('Refit cannot follow the target day')
    features = day_features(history, day, builder, published_at)
    result = pd.DataFrame({ev.MODEL: float('nan')}, index=features.index)
    slot_ids = local_slots(features.index)
    for slot in range(48):
        x = features.loc[slot_ids == slot]
        if x.empty:
            continue
        predicted = list(models[slot].predict(x))
        if len(predicted) != len(x):
            raise ValueError('Model must predict every requested slot')
        result.loc[x.index, ev.MODEL] = predicted
    result['refit_date'] = refit_day
    result['refit_cutoff'] = forecast_cutoff(refit_day)
    return result


def training_matrix(
    history: pd.Series, refit_day: date, *, builder: FeatureBuilder,
    published_at: pd.Series | None, training_start: date, cache: dict[date, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.Series]:
    """Complete training rows (features, target) known at the refit cutoff, from `training_start`.

    `cache` holds each training day's features, built at that day's own cutoff, and is
    extended in place so later refits reuse it.
    """
    usable = known_history(history, refit_day, published_at).dropna()
    start = pd.Timestamp(training_start, tz='Europe/Paris').tz_convert('UTC')
    usable = usable.loc[usable.index >= start]
    for training_day in sorted(set(ev.local_dates(usable.index))):
        if training_day not in cache:
            cache[training_day] = day_features(history, training_day, builder, published_at)
    if not cache:
        raise ValueError('No training data available at refit')
    x = pd.concat(list(cache.values())).reindex(usable.index)
    valid = x.notna().all(axis=1)
    return x.loc[valid], usable.loc[valid]


def _fit_slot_model(model_factory: Callable[[], Model], x: pd.DataFrame, y: pd.Series) -> Model:
    model = model_factory()
    model.fit(x, y)
    return model


def fit_slot_models(
    history: pd.Series, refit_day: date, model_factory: Callable[[], Model], *,
    builder: FeatureBuilder, published_at: pd.Series | None, training_start: date,
    cache: dict[date, pd.DataFrame],
) -> dict[int, Model]:
    """Independent slot fits at the cutoff; LFF_N_JOBS selects workers (default 1).

    Threads preserve the serial process's numerical-library settings and avoid
    serializing fitted statsmodels objects. Builders and the cache stay on the caller.
    """
    try:
        n_jobs = int(os.environ.get('LFF_N_JOBS', '1'))
    except ValueError as error:
        raise ValueError('LFF_N_JOBS must be a positive integer') from error
    if n_jobs < 1:
        raise ValueError('LFF_N_JOBS must be a positive integer')
    x, y = training_matrix(history, refit_day, builder=builder, published_at=published_at,
                           training_start=training_start, cache=cache)
    ids = local_slots(x.index)
    training = []
    for slot in range(48):
        rows = ids == slot
        if not rows.any():
            raise ValueError(f'No usable training rows for local slot {slot}')
        training.append((x.loc[rows].copy(), y.loc[rows].copy()))
    if n_jobs == 1:
        fitted = [_fit_slot_model(model_factory, slot_x, slot_y) for slot_x, slot_y in training]
    else:
        fitted = Parallel(n_jobs=n_jobs, backend='threading')(
            delayed(_fit_slot_model)(model_factory, slot_x, slot_y)
            for slot_x, slot_y in training)
    return dict(enumerate(fitted))


def walk_forward(
    history: pd.Series, first: date, last: date, model_factory: Callable[[], Model], *,
    builder: FeatureBuilder = calendar_features,
    published_at: pd.Series | None = None, training_start: date = date(2021, 3, 23),
    final_test: bool = False,
) -> pd.DataFrame:
    """Fit at the Monday delivery cutoff, retaining all 46/48/50 target slots.

    `final_test` unlocks 2025 for the final test only; 2026 stays locked.

    A midweek start initializes from the preceding Monday cutoff. Model factories
    create independent instances (including any fitted preprocessing). Missing
    training features/targets are excluded from fitting, never imputed here.
    The builder constructs every historical feature at its own day-ahead cutoff.
    """
    if first > last or first <= training_start or last > last_allowed_day(final_test):
        raise ValueError(f'Expected a period after training_start and {locked_message(final_test)}')
    known_history(history, first, published_at)
    cache = {}
    outputs = []
    refit = None
    models = {}
    for day in pd.date_range(first, last).date:
        monday = day - timedelta(days=day.weekday())
        if refit != monday:
            refit = monday
            models = fit_slot_models(history, refit, model_factory, builder=builder,
                                     published_at=published_at, training_start=training_start,
                                     cache=cache)
        outputs.append(forecast_day(models, history, day, refit, builder, published_at))
    return pd.concat(outputs)


def score_backtest(
    predictions: pd.DataFrame, actual: pd.Series, rte: pd.Series,
    rte_star: pd.Series | None = None,
) -> dict:
    """Use only evaluation.py for common slots, metrics, day types and inference."""
    frame = predictions[[ev.MODEL]].copy()
    frame[ev.ACTUAL], frame[ev.RTE] = actual, rte
    if rte_star is not None:
        frame.loc[rte_star.reindex(frame.index).isna() | frame[ev.RTE].isna(),
                  ev.MODEL] = float('nan')
    scores = ev.score(frame)
    days = ev.local_dates(frame.index)
    daily = ev.daily_mae(frame)
    primary, check = ev.compare_with_check(daily[ev.MODEL], daily[ev.RTE], min(days), max(days))
    result = {'model': asdict(scores.model), 'rte': asdict(scores.rte),
            'n_excluded': scores.n_excluded,
            'excluded': [t.isoformat() for t in scores.excluded],
            'day_types': ev.mae_by_day_type(frame).to_dict(orient='index'),
            'bootstrap_7': asdict(primary), 'bootstrap_14': asdict(check),
            'cold': 'unavailable until the observed-temperature threshold is fixed'}
    for block, boot in ((7, primary), (14, check)):
        result[f'bootstrap_{block}']['verdict'] = boot.verdict
    if rte_star is not None:
        corrected = frame.assign(**{ev.RTE: rte_star.reindex(frame.index)})
        result[RTE_STAR] = asdict(ev.score(corrected).rte)
        result['day_types_rte_star'] = ev.mae_by_day_type(corrected).to_dict(orient='index')
        daily_star = ev.daily_mae(corrected)
        boots = ev.compare_with_check(daily_star[ev.MODEL], daily_star[ev.RTE],
                                      min(days), max(days))
        for block, boot in zip((7, 14), boots, strict=True):
            result[f'rte_star_bootstrap_{block}'] = asdict(boot) | {'verdict': boot.verdict}
    return result


def log_backtest(
    result: dict, first: date, last: date, *, exp: str, ticket: str | None = None,
    params: dict[str, object] | None = None,
    frame: pd.DataFrame, reference: pd.Series | None = None,
    reference_name: str = 'RTE D-1', cold: Mapping[date, bool] | None = None,
    reference_column: str = ev.RTE,
) -> str:
    """Log metrics and plots to MLFLOW_TRACKING_URI; finish after all uploads succeed.

    frame holds UTC actual/model/RTE slots for this period. reference defaults to
    reference_column; pass a Series to override only the daily comparison. Grouped
    comparisons use reference_column (RTE by default), labelled with reference_name.
    The configured server must proxy artifacts (mlflow-artifacts:/ URI).
    Authorship comes from LFF_AUTHOR (default developer); ticket is optional.
    """
    tracking_uri = mlflow_tracking_uri()
    url = f'{tracking_uri}/api/2.0/mlflow'
    timestamp = int(time.time()*1000)
    with TemporaryDirectory(prefix='lff-plots-') as directory, requests.Session() as session:
        plots = save_backtest_plots(frame, first, last, Path(directory), reference=reference,
                                    reference_name=reference_name, cold=cold,
                                    reference_column=reference_column)
        response = session.post(f'{url}/runs/create', json={
            'experiment_id': '0', 'start_time': timestamp,
            'tags': [{'key': k, 'value': v} for k, v in backtest_tags(first, last, exp, ticket).items()]}, timeout=30)
        response.raise_for_status()
        info = response.json()['run']['info']
        run_id = info['run_id']
        try:
            artifact_uri = info['artifact_uri']
            if not artifact_uri.startswith('mlflow-artifacts:/'):
                raise ValueError('MLflow must serve artifacts through its artifact proxy')
            artifact_path = artifact_uri.removeprefix('mlflow-artifacts:').strip('/')
            for path in plots:
                with path.open('rb') as content:
                    upload = session.put(
                        f'{tracking_uri}/api/2.0/mlflow-artifacts/artifacts/'
                        f'{artifact_path}/plots/{path.name}', data=content,
                        headers={'Content-Type': 'image/png'}, timeout=30)
                upload.raise_for_status()
            metrics = {f'{who}_{key}': value for who in ('model', 'rte')
                       for key, value in result[who].items()}
            metrics['n_excluded'] = result['n_excluded']
            if RTE_STAR in result:
                metrics.update({f'rte_star_{k}': v for k, v in result[RTE_STAR].items()})
                for block in (7, 14):
                    metrics.update({f'rte_star_bootstrap_{block}_{k}': v
                                    for k, v in result[f'rte_star_bootstrap_{block}'].items()
                                    if isinstance(v, (int, float))})
            if 'n_expected' in result:
                metrics['n_expected'] = result['n_expected']
            for block in (7, 14):
                metrics.update({f'bootstrap_{block}_{k}': v
                                for k, v in result[f'bootstrap_{block}'].items()
                                if isinstance(v, (int, float))})
            for kind, row in result['day_types'].items():
                if kind not in ('cold', 'cold_any'):
                    metrics.update({f'{kind}_{k}': v for k, v in row.items()})
            response = session.post(f'{url}/runs/log-batch', json={
                'run_id': run_id, 'metrics': [{'key': k, 'value': float(v), 'timestamp': timestamp,
                                             'step': 0} for k, v in metrics.items()],
                'params': [{'key': k, 'value': str(v)} for k, v in (params or {}).items()]},
                timeout=30)
            response.raise_for_status()
            response = session.post(f'{url}/runs/update', json={
                'run_id': run_id, 'status': 'FINISHED', 'end_time': int(time.time()*1000)}, timeout=30)
            response.raise_for_status()
        except Exception:
            try:
                failed = session.post(f'{url}/runs/update', json={
                    'run_id': run_id, 'status': 'FAILED',
                    'end_time': int(time.time()*1000)}, timeout=30)
                failed.raise_for_status()
            except requests.RequestException:
                pass  # Preserve the original failure if the tracking server is unreachable.
            raise
    return run_id

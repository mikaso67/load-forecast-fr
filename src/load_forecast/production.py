"""Production forecast of one delivery day, with stored states and a replay mode (D18, D20).

The served model is candidate 1 of D18: the ML-Poly aggregate (EXP-009, one shared weight vector)
of EXP-004, EXP-005, EXP-007 and EXP-008, with seasonal split-conformal 80 % intervals (EXP-012).
Every setting is the one frozen on 2023 (D13). One call forecasts one day D from the inputs frozen
at its cutoff and the state left by the previous call, then advances that state:
- at the first day of a new week, the Monday refit (D6) of the three OLS families, the GAM and
  LightGBM on the expanding history known at the Monday cutoff;
- at the first refit of a calendar quarter, the Kalman variances by maximum likelihood, warm started
  from the previous quarter; at every refit the filter restarts from the first window day;
- each day, the Kalman filter, ML-Poly and the conformal pool absorb actuals of full days up to
  D-2 only (checklist item 7), read through `known_history` as every input is.
ML-Poly regrets use the expert forecasts and weights stored when D-2 was issued, never values
recomputed later, and the conformal pool uses the stored issued aggregate.

The functions are the ones the backtest ran (`fit_slot_models`, `forecast_day`, the Kalman
helpers, `MLPoly`, `window_quantiles`); this module only orders them day by day and keeps their
state, so a replay of 2024 reproduces EXP-009 and EXP-012 (docs/production_design.md 2.5).

Two periods share the code. The experiment period keeps the lock of `periods.py` (2025 and later
refused). The production period starts on 2026-01-01 (P3 A) and refuses every earlier day, so no
2025 forecast can be computed by either entry point. Its first forecast day is Monday 2026-01-05
(P2 A): 2026-01-01 to 2026-01-04 are declared without forecast, because the Monday refit that
would serve them has a 2025 cutoff. The check runs before any fit, update or replay step.
"""

import copy
import hashlib
import os
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.aggregation import MLPoly
from load_forecast.backtest import (
    FeatureBuilder,
    Model,
    day_features,
    fit_slot_models,
    forecast_day,
    known_history,
    local_slots,
    training_matrix,
)
from load_forecast.conformal import window_quantiles
from load_forecast.cutoff import forecast_cutoff
from load_forecast.gam_model import TEMPERATURE_COLUMN, SlotGAM, lagged_gam_features
from load_forecast.gbm_model import AtRounds, GBMConfig, GlobalGBM
from load_forecast.kalman import (
    UPDATE_LAG_DAYS,
    KalmanState,
    Window,
    absorb_day,
    build_window,
    corrected_day,
    extend_window,
    fit_variances,
    initial_state,
    quarter,
    residual_matrix,
)
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.periods import (
    PRODUCTION_FIRST,
    PRODUCTION_FIRST_FORECAST,
    last_allowed_day,
    locked_message,
)
from load_forecast.temperature_model import temperature_ols_features, with_calendar_backup

Period = Literal['experiment', 'production']
InputMapping = Callable[[pd.Series, date], pd.Series]

EXPERTS = ('EXP-004', 'EXP-005', 'EXP-007', 'EXP-008')
SLOT_FAMILIES = ('calendar', 'weather', 'lagged', 'gam')
N_SLOTS = 48
LOWER, UPPER, N_POOL = 'lower', 'upper', 'n_pool'


@dataclass(frozen=True)
class Settings:
    """Settings chosen on 2023 and frozen (D13, D18); tests may shrink them, production never."""
    training_start: date = date(2021, 3, 23)
    gam_degrees_of_freedom: int = 8
    gam_smoothing_weight: float = 100.0
    gbm: GBMConfig = GBMConfig(learning_rate=0.1, num_leaves=15, min_data_in_leaf=100, rounds=6400)
    season_window_days: int = 365
    level: float = 0.8


FROZEN = Settings()


def identity_mapping(history: pd.Series, day: date) -> pd.Series:
    """Default input mapping: consumption is used as stored."""
    return history


@dataclass
class WeeklyModels:
    refit: date
    slots: dict[str, dict[int, Model]]
    gbm: GlobalGBM
    identity: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class KalmanPart:
    params: np.ndarray | None = None
    fitted_quarter: tuple[int, int] | None = None
    fits: list[dict] = field(default_factory=list)
    filter: KalmanState | None = None
    window: Window | None = None


@dataclass
class Issued:
    """What was issued for one day: the inputs of the later ML-Poly and conformal updates."""
    experts: pd.DataFrame
    weights: np.ndarray
    point: pd.Series


@dataclass
class ProductionState:
    period: Period
    settings: Settings = FROZEN
    last_day: date | None = None
    models: WeeklyModels | None = None
    kalman: KalmanPart = field(default_factory=KalmanPart)
    mlpoly: MLPoly = field(default_factory=lambda: MLPoly(1, len(EXPERTS)))
    absorbed_through: date | None = None
    issued: dict[date, Issued] = field(default_factory=dict)
    variant: str = 'B'


@dataclass(frozen=True)
class PreparedRefit:
    """A separate refit completion; adopting it never replaces an issued state run."""

    variant: str
    snapshot_hash: str
    completed_at: datetime
    models: WeeklyModels
    kalman: KalmanPart


def prepare_weekly_refit(state: ProductionState, history: pd.Series,
                         temperature: pd.DataFrame, monday: date, snapshot_hash: str, *,
                         mapping: InputMapping = identity_mapping,
                         clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> PreparedRefit:
    """Rebuild the reset filter through Monday D-2 using the Sunday frozen inputs (D24)."""
    check_period(monday, state.period)
    if monday.weekday() != 0 or not snapshot_hash or state.variant not in ('A', 'B'):
        raise ValueError('A refit requires Monday, its frozen snapshot hash and an A/B variant')
    scratch = copy.deepcopy(state)
    mapped = mapping(history, monday)
    builders, caches = feature_builders(temperature), {}
    refit(scratch, mapped, monday, builders, caches)
    gam = forecast_day(scratch.models.slots['gam'], mapped, monday, monday, builders['gam'])
    kalman_forecast(scratch, mapped, monday, gam, builders['gam'], caches['gam'])
    completed = clock()
    if completed.tzinfo is None or completed.utcoffset() is None or completed < forecast_cutoff(monday):
        raise ValueError('Refit completion must be aware and at or after the Sunday freeze')
    return PreparedRefit(state.variant, snapshot_hash, completed.astimezone(UTC),
                         scratch.models, scratch.kalman)


def adopt_refit(state: ProductionState, prepared: PreparedRefit, day: date, *,
                available_before: datetime | None = None) -> None:
    """Adopt before the supplied availability limit, preserving issued ML-Poly/pool history."""
    monday = day - timedelta(days=day.weekday())
    if prepared.variant != state.variant or prepared.models.refit != monday:
        raise ValueError('Refit must belong to this variant and model week')
    if prepared.completed_at >= (available_before or forecast_cutoff(day)):
        raise ValueError('Refit was not complete before the adoption availability limit')
    state.models, state.kalman = copy.deepcopy((prepared.models, prepared.kalman))


def check_period(day: date, period: Period) -> None:
    """The experiment period keeps the periods.py lock; the production period starts in 2026."""
    if period == 'experiment':
        if day > last_allowed_day():
            raise ValueError(f'Experiment days must fall {locked_message()}')
    elif period == 'production':
        if day < PRODUCTION_FIRST:
            raise ValueError(f'Production days start on {PRODUCTION_FIRST}')
        if day < PRODUCTION_FIRST_FORECAST:
            raise ValueError(f'{day} is declared without forecast (D20 P2 A): the first forecast '
                             f'is {PRODUCTION_FIRST_FORECAST}, a refit at a 2025 cutoff is forbidden')
    else:
        raise ValueError(f'Unknown period: {period}')


def feature_builders(temperature: pd.DataFrame) -> dict[str, FeatureBuilder]:
    return {'calendar': calendar_ols_features, 'weather': temperature_ols_features(temperature),
            'lagged': lagged_ols_features(temperature), 'gam': lagged_gam_features(temperature)}


def gam_factory(settings: Settings) -> Callable[[], SlotGAM]:
    def make() -> SlotGAM:
        return SlotGAM([TEMPERATURE_COLUMN], df=settings.gam_degrees_of_freedom,
                       alpha_grid=(float(settings.gam_smoothing_weight),), missing_as_nan=True)
    return make


def cached_features(cache: dict[date, pd.DataFrame], history: pd.Series, day: date,
                    builder: FeatureBuilder) -> pd.DataFrame:
    if day not in cache:
        cache[day] = day_features(history, day, builder)
    return cache[day]


def refit(state: ProductionState, history: pd.Series, monday: date,
          builders: dict[str, FeatureBuilder], caches: dict[str, dict]) -> None:
    """Weekly refit at the Monday cutoff, Kalman variances at a new quarter, filter restart."""
    settings = state.settings
    factories = {'calendar': SlotOLS, 'weather': SlotOLS, 'lagged': SlotOLS,
                 'gam': gam_factory(settings)}
    slots = {name: fit_slot_models(history, monday, factories[name], builder=builders[name],
                                   published_at=None, training_start=settings.training_start,
                                   cache=caches.setdefault(name, {}))
             for name in SLOT_FAMILIES}
    cache = caches['gam']
    x, y = training_matrix(history, monday, builder=builders['gam'], published_at=None,
                           training_start=settings.training_start, cache=cache)
    state.models = WeeklyModels(monday, slots, GlobalGBM(settings.gbm).fit(x, y))

    # Through the refit day only: the later days of the week enter at their own cutoff.
    days = [d.date() for d in pd.date_range(min(cache), monday)]
    for d in days:
        cached_features(cache, history, d, builders['gam'])
    fit_through = monday - timedelta(days=UPDATE_LAG_DAYS)
    window = build_window(slots['gam'], cache, days, [d for d in days if d <= fit_through])
    kalman = state.kalman
    if quarter(monday) != kalman.fitted_quarter:
        design, residual = residual_matrix(window, known_history(history, monday, None),
                                           window.day_index(fit_through))
        kalman.params, info = fit_variances(design, residual, kalman.params)
        kalman.fitted_quarter = quarter(monday)
        kalman.fits.append({'refit': monday, 'days': design.shape[0], **info,
                            'params': kalman.params.copy()})
    kalman.window, kalman.filter = window, initial_state()


def kalman_forecast(state: ProductionState, history: pd.Series, day: date, gam: pd.DataFrame,
                    builder: FeatureBuilder, cache: dict) -> pd.DataFrame:
    """Append the new days to the window, absorb the days up to D-2, correct the GAM of D."""
    kalman, models = state.kalman, state.models
    window = kalman.window
    while window.days[-1] < day:
        nxt = window.days[-1] + timedelta(days=1)
        extend_window(window, models.slots['gam'], nxt,
                      cached_features(cache, history, nxt, builder))
    known = known_history(history, day, None)
    t = window.day_index(day)
    target = window.days[t - UPDATE_LAG_DAYS] if t >= UPDATE_LAG_DAYS else None
    start = 0 if kalman.filter.last_day is None else window.day_index(kalman.filter.last_day) + 1
    end = window.day_index(target) if target is not None else -1
    for step in range(start, end + 1):
        absorb_day(kalman.filter, window, step, known, kalman.params)
    if not window.index[window.rows[t]].equals(gam.index):
        raise ValueError('Window rows must match the forecast slots')
    corrected = gam.copy()
    corrected[ev.MODEL] = corrected_day(window, t, kalman.filter)
    return corrected


def expert_frames(state: ProductionState, history: pd.Series, day: date,
                  builders: dict[str, FeatureBuilder], caches: dict[str, dict]) -> dict:
    """The four experts of D with the backup chain of each (M11), as in the EXP-009 runs."""
    models = state.models
    plain = {name: forecast_day(models.slots[name], history, day, models.refit, builders[name])
             for name in SLOT_FAMILIES}
    view = AtRounds(models.gbm, state.settings.gbm.rounds)
    boosted = forecast_day({s: view for s in range(N_SLOTS)}, history, day, models.refit,
                           builders['gam'])
    corrected = kalman_forecast(state, history, day, plain['gam'], builders['gam'],
                                caches.setdefault('gam', {}))
    exp003 = with_calendar_backup(plain['weather'], plain['calendar'])
    exp004 = with_calendar_backup(plain['lagged'], exp003, flag=BACKUP_FLAG)
    exp005 = with_calendar_backup(plain['gam'], exp004, flag='backup_exp004')
    exp007 = with_calendar_backup(corrected, exp005, flag='backup_exp005')
    exp008 = with_calendar_backup(boosted, exp005, flag='backup_exp004')
    return {'EXP-004': (exp004, BACKUP_FLAG), 'EXP-005': (exp005, 'backup_exp004'),
            'EXP-007': (exp007, 'backup_exp005'), 'EXP-008': (exp008, 'backup_exp004')}


def aggregate_day(state: ProductionState, day: date, experts: pd.DataFrame,
                  known: pd.Series) -> tuple[np.ndarray, pd.Series]:
    """ML-Poly: absorb the issued days up to D-2 in order, then weigh the experts of D."""
    for past in sorted(state.issued):
        if past > day - timedelta(days=UPDATE_LAG_DAYS):
            break
        if state.absorbed_through is not None and past <= state.absorbed_through:
            continue
        issued = state.issued[past]
        state.mlpoly.update(np.zeros(len(issued.experts), dtype=int),
                            issued.experts.to_numpy(dtype=float),
                            known.reindex(issued.experts.index).to_numpy(dtype=float),
                            issued.weights)
        state.absorbed_through = past
    weights = state.mlpoly.weights()
    values = experts.to_numpy(dtype=float)
    groups = np.zeros(len(values), dtype=int)
    point = pd.Series((weights[groups] * values).sum(axis=1), index=experts.index, name=ev.MODEL)
    return weights, point


def interval_day(state: ProductionState, day: date, point: pd.Series,
                 known: pd.Series) -> pd.DataFrame:
    """EXP-012 pool: same local slot, same season, issued days D-2-364 to D-2."""
    settings = state.settings
    last = day - timedelta(days=UPDATE_LAG_DAYS)
    first = last - timedelta(days=settings.season_window_days - 1)
    season = ev.get_season(day.month)
    pool_days = [d for d in sorted(state.issued)
                 if first <= d <= last and ev.get_season(d.month) == season]
    if pool_days:
        issued = pd.concat([state.issued[d].point for d in pool_days])
        residual = (known.reindex(issued.index).to_numpy(dtype=float)
                    - issued.to_numpy(dtype=float))
        pool_slots = np.asarray(local_slots(issued.index))
    else:
        residual, pool_slots = np.array([]), np.array([], dtype=int)
    slots = np.asarray(local_slots(point.index))
    values = point.to_numpy(dtype=float)
    out = pd.DataFrame({LOWER: np.nan, UPPER: np.nan, N_POOL: 0}, index=point.index)
    for slot in np.unique(slots):
        target = slots == slot
        pool = residual[pool_slots == slot]
        low, high = window_quantiles(pool, settings.level)
        out.loc[target, LOWER] = values[target] + low
        out.loc[target, UPPER] = values[target] + high
        out.loc[target, N_POOL] = int(np.isfinite(pool).sum())
    return out


def issue(state: ProductionState, day: date, experts: pd.DataFrame, weights: np.ndarray,
          point: pd.Series) -> None:
    """Keep what later updates need; drop days older than the pool that ML-Poly has absorbed."""
    state.issued[day] = Issued(experts.copy(), weights.copy(), point.copy())
    state.last_day = day
    oldest = day - timedelta(days=state.settings.season_window_days + UPDATE_LAG_DAYS + 1)
    for past in [d for d in state.issued if d < oldest]:
        if state.absorbed_through is not None and past <= state.absorbed_through:
            del state.issued[past]


def run_day(day: date, history: pd.Series, temperature: pd.DataFrame, state: ProductionState, *,
            mapping: InputMapping = identity_mapping,
            caches: dict[str, dict] | None = None,
            reuse_models: bool = False) -> pd.DataFrame:
    """Forecast one delivery day and advance `state` in place.

    `history` is the consumption snapshot frozen at the cutoff of D (UTC slots) and `temperature`
    the selected-run weather; `known_history` cuts both at D2 inside every builder and update.
    `mapping` (D21) turns the snapshot into the series every input and online update reads; it
    receives the delivery day so a mapping estimated month by month can stay causal. `caches`
    holds day features already built from the same mapped history; a caller passes them only when
    that holds (one snapshot for the whole replay), otherwise each call rebuilds them.
    """
    check_period(day, state.period)
    if state.last_day is not None and day <= state.last_day:
        raise ValueError('Delivery days must move forward')
    caches = {} if caches is None else caches
    mapped = mapping(history, day)
    builders = feature_builders(temperature)
    monday = day - timedelta(days=day.weekday())
    if reuse_models and state.models is None:
        raise ValueError('No previous models are available for stale-model delivery')
    if not reuse_models and (state.models is None or state.models.refit != monday):
        refit(state, mapped, monday, builders, caches)
    frames = expert_frames(state, mapped, day, builders, caches)
    experts = pd.DataFrame({name: frame[ev.MODEL] for name, (frame, _) in frames.items()})
    if experts.isna().any().any():
        raise RuntimeError('Expert slots without any model')
    known = known_history(mapped, day, None)
    weights, point = aggregate_day(state, day, experts, known)
    intervals = interval_day(state, day, point, known)
    issue(state, day, experts, weights, point)
    output = experts.copy()
    for name, (frame, flag) in frames.items():
        output[f'{name}_backup'] = frame[flag].astype(bool)
    for k, name in enumerate(EXPERTS):
        output[f'weight_{name}'] = weights[0, k]
    output[ev.MODEL] = point
    output = output.join(intervals)
    output['refit_date'] = state.models.refit
    return output


def forecast_experiment_day(day: date, history: pd.Series, temperature: pd.DataFrame,
                            state: ProductionState, **kwargs) -> pd.DataFrame:
    """Entry point for days of the experiment period (acceptance replay); 2025 stays locked."""
    if state.period != 'experiment':
        raise ValueError('This state belongs to the production period')
    return run_day(day, history, temperature, state, **kwargs)


def forecast_production_day(day: date, history: pd.Series, temperature: pd.DataFrame,
                            state: ProductionState, **kwargs) -> pd.DataFrame:
    """Entry point for the production period (P3 A, P2 A): delivery days from 2026-01-05 only."""
    if state.period != 'production':
        raise ValueError('This state belongs to the experiment period')
    return run_day(day, history, temperature, state, **kwargs)


def seed_state(experts: pd.DataFrame, history: pd.Series, period: Period,
               settings: Settings = FROZEN) -> ProductionState:
    """Start ML-Poly and the conformal pool from archived out-of-sample expert forecasts.

    Used for the 2023 burn-in of EXP-009 and for the warm start of section 2.4: ML-Poly runs over
    the archived experts day by day exactly as in production and each aggregate joins the pool.
    No model is fitted and no expert forecast is computed. Models and Kalman start at the first
    refit of the replay that follows.
    """
    if experts.isna().any().any() or list(experts.columns) != list(EXPERTS):
        raise ValueError(f'Archived experts must be complete, with columns {EXPERTS}')
    state = ProductionState(period=period, settings=settings)
    days = pd.Series(ev.local_dates(experts.index), index=experts.index)
    for day, rows in experts.groupby(days.to_numpy(), sort=True):
        known = known_history(history, day, None)
        weights, point = aggregate_day(state, day, rows, known)
        issue(state, day, rows, weights, point)
    return state


class StateStore:
    """One pickle per delivery day, the weekly models in a separate file per refit.

    Files are written to a temporary name and renamed, and each state records the SHA-256 of the
    models file it needs, so a state is never loaded with other models. Pickle runs code on load:
    only load folders this pipeline wrote.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _write(self, path: Path, payload: bytes) -> None:
        temporary = path.with_suffix('.tmp')
        with temporary.open('wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def models_path(self, refit_day: date, identity: str | None = None) -> Path:
        suffix = '' if identity is None else f'_{identity}'
        return self.folder / f'models_{refit_day.isoformat()}{suffix}.pkl'

    def state_path(self, day: date) -> Path:
        return self.folder / f'state_{day.isoformat()}.pkl'

    def save(self, state: ProductionState) -> Path:
        if state.last_day is None or state.models is None:
            raise ValueError('Only a state that has forecast a day can be stored')
        models = self.models_path(state.models.refit, state.models.identity)
        if not models.exists():
            self._write(models, pickle.dumps(state.models, protocol=pickle.HIGHEST_PROTOCOL))
        digest = hashlib.sha256(models.read_bytes()).hexdigest()
        payload = {'state': replace(state, models=None), 'refit': state.models.refit,
                   'models_identity': state.models.identity,
                   'models_sha256': digest}
        path = self.state_path(state.last_day)
        self._write(path, pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        return path

    def load(self, day: date, *,
             model_source: Callable[[date, str, str], WeeklyModels] | None = None) -> ProductionState:
        payload = pickle.loads(self.state_path(day).read_bytes())
        model_path = self.models_path(payload['refit'], payload.get('models_identity'))
        try:
            raw = model_path.read_bytes()
        except FileNotFoundError:
            if model_source is None:
                raise
            # The resolver verifies the immutable retention proof and kept refit's hash.
            models = model_source(payload['refit'], payload['models_identity'], payload['models_sha256'])
            return replace(payload['state'], models=models)
        if hashlib.sha256(raw).hexdigest() != payload['models_sha256']:
            raise ValueError('The stored models do not match the state')
        return replace(payload['state'], models=pickle.loads(raw))


def replay(first: date, last: date, history: pd.Series, temperature: pd.DataFrame,
           state: ProductionState, *, mapping: InputMapping = identity_mapping,
           store: StateStore | None = None) -> tuple[pd.DataFrame, ProductionState]:
    """Run the production function on every day of [first, last] with one snapshot.

    With a store, each day's state is saved and the next day starts from the reloaded file, so the
    replay proves that the stored state is all a day needs. Feature caches are shared across days
    only with the identity mapping, where the mapped history cannot change from day to day.
    """
    if first > last:
        raise ValueError('Expected an ordered period')
    caches: dict[str, dict] = {}
    outputs = []
    for day in pd.date_range(first, last).date:
        if mapping is not identity_mapping:
            caches = {}
        outputs.append(run_day(day, history, temperature, state, mapping=mapping, caches=caches))
        if store is not None:
            store.save(state)
            state = store.load(day)
    return pd.concat(outputs), state

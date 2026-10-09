"""Rung 4 (M7 option B): an online Kalman correction on top of the weekly GAM.

Per local half-hour slot s the filter regresses the GAM residual on the standardized partial effects:
    y_t - gam_t = delta_t' x_t + e_t,   delta_t = delta_(t-1) + w_t,   delta_0 = 0,
with x_t = (1, z_base, z_lags, z_smooth), z = (partial effect - training mean) / training sd, so
every state is a correction in MW and the corrected forecast is gam_t + delta' x_t. One time step is
one delivery day. Diagonal Q and the noise variance e are fitted per slot by maximum likelihood on the
training window of a refit, never later than that refit's cutoff.

Timing (checklist item 7): the forecast of day D uses the filter updated with the full day D-2 only.
`UPDATE_LAG_DAYS` is read at run time so a test can prove that a D-1 update would be detected.

Two ways of handling the weekly GAM refit, both run on the same fitted GAMs:
- `restart`: the filter is rerun from the start of the window on the new GAM effects;
- `keep`: the state and covariance carry over, the new effects are used from the refit on.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from load_forecast import evaluation as ev
from load_forecast.backtest import (
    FeatureBuilder,
    Model,
    day_features,
    fit_slot_models,
    forecast_day,
    known_history,
    local_slots,
)
from load_forecast.lag_model import LAG_COLUMNS, SPECIAL_COLUMNS
from load_forecast.periods import last_allowed_day, locked_message

UPDATE_LAG_DAYS = 2
EFFECT_GROUPS = {'lags': LAG_COLUMNS + SPECIAL_COLUMNS}
EFFECT_COLUMNS = ['base', 'lags', 'smooth']
MODES = ('restart', 'keep')
N_SLOTS = 48
N_STATE = 1 + len(EFFECT_COLUMNS)
PRIOR_VARIANCE = 1e4
NOISE_BOUNDS = (0.0, 25.0)
DRIFT_BOUNDS = (math.log(1e-6), math.log(1e7))
FIT_STEP = 1e-4
FIT_MAX_ITERATIONS = 100


@dataclass
class KalmanState:
    delta: np.ndarray
    covariance: np.ndarray
    last_day: date | None = None


@dataclass
class Window:
    """Standardized partial effects of the weekly GAM for each day of the window.

    `pos[t, k, s]` is the row of `index` holding the k-th occurrence (0, or 1 on an autumn change
    day) of local slot s on day t, or -1 when absent.
    """
    days: list[date]
    index: pd.DatetimeIndex
    gam: np.ndarray
    design: np.ndarray
    pos: np.ndarray
    rows: list[np.ndarray] = field(default_factory=list)

    def day_index(self, day: date) -> int:
        return self.days.index(day)


@dataclass
class KalmanResult:
    gam: pd.DataFrame
    corrected: dict[str, pd.DataFrame]
    fits: list[dict]


def initial_state(n_slots: int = N_SLOTS) -> KalmanState:
    return KalmanState(np.zeros((n_slots, N_STATE)),
                       np.tile(PRIOR_VARIANCE * np.eye(N_STATE), (n_slots, 1, 1)))


def update_step(delta: np.ndarray, covariance: np.ndarray, x: np.ndarray, r: np.ndarray,
                noise: np.ndarray) -> np.ndarray:
    """One batched filter update in place; returns the log-likelihood of each batch member.

    Shapes: delta (..., S, d), covariance (..., S, d, d), x (S, d), r (S,), noise (..., S).
    Members with a non-finite x or r are left unchanged and contribute 0.
    """
    valid = np.isfinite(r) & np.isfinite(x).all(axis=-1)
    xz = np.where(valid[:, None], x, 0.0)
    rz = np.where(valid, r, 0.0)
    px = np.einsum('...sij,sj->...si', covariance, xz)
    variance = np.einsum('si,...si->...s', xz, px) + noise
    error = rz - np.einsum('...si,si->...s', delta, xz)
    gain = px / variance[..., None]
    delta += gain * error[..., None]
    covariance -= gain[..., :, None] * px[..., None, :]
    return np.where(valid, -0.5 * (np.log(2 * np.pi * variance) + error ** 2 / variance), 0.0)


def add_drift(covariance: np.ndarray, drift: np.ndarray) -> None:
    index = np.arange(N_STATE)
    covariance[..., index, index] += drift


def log_likelihood(design: np.ndarray, residual: np.ndarray, params: np.ndarray) -> np.ndarray:
    """Log-likelihood per replica and slot. params (R, S, 1 + d) hold log noise and log drifts."""
    noise, drift = np.exp(params[..., 0]), np.exp(params[..., 1:])
    delta = np.zeros((*params.shape[:2], N_STATE))
    covariance = np.tile(PRIOR_VARIANCE * np.eye(N_STATE), (*params.shape[:2], 1, 1))
    total = np.zeros(params.shape[:2])
    for t in range(design.shape[0]):
        add_drift(covariance, drift)
        for k in range(design.shape[1]):
            if np.isfinite(residual[t, k]).any():
                total += update_step(delta, covariance, design[t, k], residual[t, k], noise)
    return total


def fit_variances(
    design: np.ndarray, residual: np.ndarray, start: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """Per-slot maximum likelihood of the noise and diagonal drift variances (log scale).

    The slots are independent, so one L-BFGS run maximizes the sum; each gradient costs one batched
    pass of N_STATE + 1 replicas (forward differences on the log parameters).
    """
    n_slots = design.shape[2]
    counts = np.maximum(np.isfinite(residual).sum(axis=(0, 1)), 1)
    if start is None:
        noise = np.nanvar(residual, axis=(0, 1))
        start = np.column_stack([np.log(np.maximum(noise, 1.0)), np.zeros((n_slots, N_STATE))])
    bounds = [NOISE_BOUNDS, *([DRIFT_BOUNDS] * N_STATE)] * n_slots

    def objective(flat: np.ndarray) -> tuple[float, np.ndarray]:
        base = flat.reshape(n_slots, 1 + N_STATE)
        batch = np.stack([base] * (2 + N_STATE))
        for j in range(1 + N_STATE):
            batch[1 + j, :, j] += FIT_STEP
        ll = log_likelihood(design, residual, batch)
        grad = -((ll[1:] - ll[0]) / FIT_STEP / counts).T
        return float(-(ll[0] / counts).sum()), grad.ravel()

    result = minimize(objective, start.ravel(), jac=True, method='L-BFGS-B', bounds=bounds,
                      options={'maxiter': FIT_MAX_ITERATIONS})
    params = result.x.reshape(n_slots, 1 + N_STATE)
    return params, {'nit': int(result.nit), 'converged': bool(result.success),
                    'mean_nll_per_obs': float(result.fun / n_slots)}


def build_window(
    models: dict[int, Model], features: dict[date, pd.DataFrame], days: list[date],
    training_days: list[date],
) -> Window:
    """Partial effects of every slot model on every window day, standardized on training days."""
    frame = pd.concat([features[d] for d in days])
    slot = np.asarray(local_slots(frame.index))
    effects = pd.DataFrame(np.nan, index=frame.index, columns=EFFECT_COLUMNS)
    for s in range(N_SLOTS):
        rows = slot == s
        if rows.any():
            part = models[s].partial_effects(frame.loc[rows], EFFECT_GROUPS)
            effects.loc[rows] = part[EFFECT_COLUMNS].to_numpy()
    day_of_row = np.concatenate([[i] * len(features[d]) for i, d in enumerate(days)])
    in_training = np.isin(np.asarray(ev.local_dates(frame.index)), training_days)
    values = effects.to_numpy()
    standard = np.full_like(values, np.nan)
    for s in range(N_SLOTS):
        rows = slot == s
        fit_rows = rows & in_training & np.isfinite(values).all(axis=1)
        if not fit_rows.any():
            continue
        centre = values[fit_rows].mean(axis=0)
        scale = values[fit_rows].std(axis=0)
        flat = scale < 1e-9
        scale[flat] = 1.0
        standard[rows] = (values[rows] - centre) / scale
        standard[np.ix_(rows, np.flatnonzero(flat))] = 0.0
    window = Window(days=days, index=frame.index, gam=values.sum(axis=1), design=standard,
                    pos=np.full((len(days), 2, N_SLOTS), -1))
    seen: dict[tuple[int, int], int] = {}
    for p, (t, s) in enumerate(zip(day_of_row, slot, strict=True)):
        k = seen.get((t, s), 0)
        if k > 1:
            raise ValueError('A local slot occurs more than twice in one delivery day')
        seen[(t, s)] = k + 1
        window.pos[t, k, s] = p
    window.rows = [np.flatnonzero(day_of_row == t) for t in range(len(days))]
    return window


def day_arrays(window: Window, t: int) -> tuple[np.ndarray, np.ndarray]:
    """Design (2, S, d) and GAM prediction (2, S) of day t; NaN where the row is absent."""
    pos = window.pos[t]
    present = pos >= 0
    safe = np.where(present, pos, 0)
    x = np.concatenate([np.ones((2, N_SLOTS, 1)), window.design[safe]], axis=2)
    gam = window.gam[safe]
    x[~present] = np.nan
    gam[~present] = np.nan
    return x, gam


def residual_matrix(window: Window, known: pd.Series, upto: int) -> tuple[np.ndarray, np.ndarray]:
    """Design and residual arrays for days 0..upto, built only from the `known` consumption."""
    y = known.reindex(window.index).to_numpy(dtype=float)
    designs, residuals = [], []
    for t in range(upto + 1):
        x, gam = day_arrays(window, t)
        pos = window.pos[t]
        observed = np.where(pos >= 0, y[np.where(pos >= 0, pos, 0)], np.nan)
        designs.append(x)
        residuals.append(observed - gam)
    return np.stack(designs), np.stack(residuals)


def absorb_day(state: KalmanState, window: Window, t: int, known: pd.Series,
               params: np.ndarray) -> None:
    """Advance the filter by the full delivery day t (both occurrences of a repeated slot)."""
    x, gam = day_arrays(window, t)
    pos = window.pos[t]
    y = known.reindex(window.index[pos[pos >= 0]]).to_numpy(dtype=float)
    observed = np.full(pos.shape, np.nan)
    observed[pos >= 0] = y
    noise, drift = np.exp(params[:, 0]), np.exp(params[:, 1:])
    add_drift(state.covariance, drift)
    for k in range(2):
        if np.isfinite(observed[k] - gam[k]).any():
            update_step(state.delta, state.covariance, x[k], observed[k] - gam[k], noise)
    state.last_day = window.days[t]


def corrected_day(window: Window, t: int, state: KalmanState) -> np.ndarray:
    """gam + delta' x for each row of day t, in the row order of the window."""
    x, gam = day_arrays(window, t)
    correction = np.einsum('ksd,sd->ks', np.nan_to_num(x), state.delta)
    full = gam + correction
    out = np.full(len(window.index), np.nan)
    pos = window.pos[t]
    out[pos[pos >= 0]] = full[pos >= 0]
    return out[window.rows[t]]


def quarter(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3


def walk_forward_kalman(
    history: pd.Series, first: date, last: date, model_factory: Callable[[], Model], *,
    builder: FeatureBuilder, published_at: pd.Series | None = None,
    training_start: date = date(2021, 3, 23), modes: tuple[str, ...] = MODES,
    final_test: bool = False,
) -> KalmanResult:
    """Weekly Monday GAM refit, daily filter update with day D-2, one forecast per day.

    Every mode shares the same GAMs. The noise and drift variances are re-estimated by maximum
    likelihood at the first weekly refit of each calendar quarter (warm start), on the days whose
    consumption is known at that refit cutoff, and held fixed until the next quarter.
    """
    if first > last or first <= training_start or last > last_allowed_day(final_test):
        raise ValueError(f'Expected a period after training_start and {locked_message(final_test)}')
    if set(modes) - set(MODES):
        raise ValueError(f'Unknown modes: {sorted(set(modes) - set(MODES))}')
    known_history(history, first, published_at)
    cache: dict[date, pd.DataFrame] = {}
    gam_out, corrected = [], {mode: [] for mode in modes}
    fits: list[dict] = []
    states = {mode: initial_state() for mode in modes}
    params, fitted_quarter, refit, models = None, None, None, {}
    window = None
    for day in pd.date_range(first, last).date:
        monday = day - timedelta(days=day.weekday())
        if refit != monday:
            refit = monday
            models = fit_slot_models(history, refit, model_factory, builder=builder,
                                     published_at=published_at, training_start=training_start,
                                     cache=cache)
            week_end = min(last, monday + timedelta(days=6))
            window_days = [d.date() for d in pd.date_range(min(cache), week_end)]
            for d in window_days:
                if d not in cache:
                    cache[d] = day_features(history, d, builder, published_at)
            known = known_history(history, refit, published_at)
            fit_through = refit - timedelta(days=UPDATE_LAG_DAYS)
            training_days = [d for d in window_days if d <= fit_through]
            window = build_window(models, cache, window_days, training_days)
            if 'restart' in states:
                states['restart'] = initial_state()
            if quarter(refit) != fitted_quarter:
                design, residual = residual_matrix(window, known, window.day_index(fit_through))
                params, info = fit_variances(design, residual, params)
                fitted_quarter = quarter(refit)
                fits.append({'refit': refit, 'days': design.shape[0], **info,
                             'noise_sd_median': float(np.median(np.exp(params[:, 0] / 2))),
                             'drift_sd_median': np.median(np.exp(params[:, 1:] / 2), axis=0).tolist()})
        plain = forecast_day(models, history, day, refit, builder, published_at)
        gam_out.append(plain)
        known = known_history(history, day, published_at)
        t = window.day_index(day)
        for mode, state in states.items():
            target = window.days[t - UPDATE_LAG_DAYS] if t >= UPDATE_LAG_DAYS else None
            start = 0 if state.last_day is None else window.day_index(state.last_day) + 1
            end = window.day_index(target) if target is not None else -1
            for step in range(start, end + 1):
                absorb_day(state, window, step, known, params)
            values = corrected_day(window, t, state)
            frame = plain.copy()
            if not window.index[window.rows[t]].equals(frame.index):
                raise ValueError('Window rows must match the forecast slots')
            frame[ev.MODEL] = values
            corrected[mode].append(frame)
    return KalmanResult(gam=pd.concat(gam_out),
                        corrected={m: pd.concat(frames) for m, frames in corrected.items()},
                        fits=fits)

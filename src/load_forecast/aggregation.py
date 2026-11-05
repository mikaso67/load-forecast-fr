"""ML-Poly aggregation of expert forecasts (M8 A): gradient trick, absolute loss, D-2 updates.

Gaillard, Stoltz and van Erven (2014). The aggregate for day D is a convex combination of the
experts' forecasts for D. The weights are a function of the experts' past forecasts and of the
actuals of full delivery days up to D-2 only, the same rule as the Kalman filter (checklist item 7).
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import local_slots

UPDATE_LAG_DAYS = 2
N_SLOTS = 48


@dataclass
class Aggregation:
    forecast: pd.Series
    weights: pd.DataFrame  # index (day, group), one column per expert


class MLPoly:
    """Regret and sum of squared regrets per (group, expert); weights follow the ML-Poly rule.

    Weight of expert k: eta_k * max(R_k, 0), normalized, with eta_k = 1 / (1 + sum of r_k squared).
    Uniform weights while no expert has a positive regret. Losses are in MW, so the constant 1 only
    guards against a division by zero and the rule is scale free in practice.
    """

    def __init__(self, n_groups: int, n_experts: int) -> None:
        self.regret = np.zeros((n_groups, n_experts))
        self.squares = np.zeros((n_groups, n_experts))

    def weights(self) -> np.ndarray:
        raw = np.maximum(self.regret, 0.0) / (1.0 + self.squares)
        total = raw.sum(axis=1, keepdims=True)
        uniform = np.full_like(raw, 1.0 / raw.shape[1])
        return np.where(total > 0, raw / np.where(total > 0, total, 1.0), uniform)

    def update(self, groups: np.ndarray, forecasts: np.ndarray, actual: np.ndarray,
               weights: np.ndarray) -> None:
        """Absorb one day. `weights` are the ones used to issue that day's forecast."""
        keep = np.isfinite(actual) & np.isfinite(forecasts).all(axis=1)
        groups, forecasts, actual = groups[keep], forecasts[keep], actual[keep]
        aggregate = (weights[groups] * forecasts).sum(axis=1)
        gradient = np.sign(aggregate - actual)
        regret = gradient[:, None] * (aggregate[:, None] - forecasts)
        daily = np.zeros_like(self.regret)
        np.add.at(daily, groups, regret)
        self.regret += daily
        self.squares += daily ** 2


def known_morning(index: pd.DatetimeIndex, day: date) -> np.ndarray:
    """Mask of the rows of local day D-1 already known at the cutoff of D (D2: start <= 09:30)."""
    previous = np.asarray(ev.local_dates(index)) == day - timedelta(days=1)
    return previous & np.asarray(index <= ev.last_usable_slot_start(day))


def aggregate(
    forecasts: pd.DataFrame, actual: pd.Series, *, per_slot: bool,
    update_lag_days: int = UPDATE_LAG_DAYS, morning: bool = False,
) -> Aggregation:
    """Aggregate the columns of `forecasts` (UTC slots, one column per expert) day by day.

    Day D uses the actuals of the local days up to D - update_lag_days, read row by row from
    `actual`; nothing later is touched. A round is one local day: with `per_slot`, one round per
    local half-hour slot (both occurrences of a repeated autumn slot add up), otherwise one round
    for the whole day with the regrets of all its slots added up.

    With `morning` (research variant, off in production), day D also absorbs the slots of D-1 known
    at its cutoff (D2), as their own round; the rest of D-1 is absorbed at D+1.
    """
    if forecasts.isna().any().any():
        raise ValueError('Expert forecasts must be complete: apply the backup chain first')
    if morning and update_lag_days != UPDATE_LAG_DAYS:
        raise ValueError('The morning update assumes full days up to D-2')
    days = pd.Series(ev.local_dates(forecasts.index), index=forecasts.index)
    groups = np.asarray(local_slots(forecasts.index)) if per_slot else np.zeros(len(days), int)
    values = forecasts.to_numpy(dtype=float)
    observed = actual.reindex(forecasts.index).to_numpy(dtype=float)
    rows = {d: np.flatnonzero(days.to_numpy() == d) for d in sorted(set(days))}
    state = MLPoly(N_SLOTS if per_slot else 1, values.shape[1])
    used: dict[date, np.ndarray] = {}
    done = np.zeros(len(days), dtype=bool)
    output = np.full(len(days), np.nan)
    for day, position in rows.items():
        for past, chosen in rows.items():
            if past <= day - timedelta(days=update_lag_days):
                pick = chosen[~done[chosen]]
            elif morning and past == day - timedelta(days=1):
                pick = chosen[~done[chosen] & known_morning(forecasts.index[chosen], day)]
            else:
                continue
            if pick.size:
                state.update(groups[pick], values[pick], observed[pick], used[past])
                done[pick] = True
        used[day] = state.weights()
        output[position] = (used[day][groups[position]] * values[position]).sum(axis=1)
    index = pd.MultiIndex.from_tuples(
        [(d, g) for d in rows for g in range(len(used[d]))], names=['day', 'group'])
    table = pd.DataFrame(np.concatenate([used[d] for d in rows]), index=index,
                         columns=forecasts.columns)
    return Aggregation(forecast=pd.Series(output, index=forecasts.index, name=ev.MODEL),
                       weights=table)


def morning_level_shift(forecast: pd.Series, actual: pd.Series, shrink: float) -> pd.Series:
    """Research variant (off in production): shift day D by -shrink times the mean error.

    The error is forecast minus actual over the slots of D-1 known at the cutoff of D (D2), with
    the forecast issued for D-1 by the same chain. A day without such a slot is not shifted.
    """
    if not 0.0 <= shrink <= 1.0:
        raise ValueError('The shrinkage must lie in [0, 1]')
    index = forecast.index
    error = (forecast - actual.reindex(index)).to_numpy(dtype=float)
    days = np.asarray(ev.local_dates(index))
    shifted = forecast.to_numpy(dtype=float).copy()
    for day in sorted(set(days)):
        known = ((days == day - timedelta(days=1)) & np.asarray(index <= ev.last_usable_slot_start(day))
                 & np.isfinite(error))
        if known.any():
            shifted[days == day] -= shrink * error[known].mean()
    return pd.Series(shifted, index=index, name=forecast.name)

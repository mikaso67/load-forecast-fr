"""Rung 1 of the modeling plan: ordinary least squares per local slot on calendar features.

The feature builder is pure (M1, M2, M5): it reads only the delivery date, never the
consumption history, so it cannot leak anything published after the cutoff.
"""

from datetime import date

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev

FOURIER_ORDER = 3
DAY_COLUMNS = [f'dow_{d}' for d in range(6)]


def local_delivery_date(slots: pd.DatetimeIndex) -> date:
    days = set(ev.local_dates(slots))
    if len(days) != 1:
        raise ValueError('Slots must belong to one Europe/Paris delivery day')
    return days.pop()


def calendar_ols_features(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
    """Day of week with a holiday coded as Sunday (M2 A), Fourier terms, bridge, Christmas week.

    Monday is dow_0 to Saturday dow_5, Sunday is the reference level. `history` is ignored on
    purpose: with calendar features only, the lag rules of D2 have nothing to guard.
    """
    local_day = local_delivery_date(slots)
    weekday = 6 if ev.is_holiday(local_day) else local_day.weekday()
    phase = 2 * np.pi * local_day.timetuple().tm_yday / 365.25
    row = {f'dow_{d}': float(weekday == d) for d in range(6)}
    for k in range(1, FOURIER_ORDER + 1):
        row[f'sin_{k}'] = float(np.sin(k * phase))
        row[f'cos_{k}'] = float(np.cos(k * phase))
    row['bridge'] = float(ev.is_bridge(local_day))
    row['christmas_week'] = float(local_day.month == 12 and local_day.day >= 24)
    return pd.DataFrame([row] * len(slots), index=slots)


class SlotOLS:
    """Least squares with an intercept; the minimum-norm solution handles constant columns."""

    def fit(self, x: pd.DataFrame, y: pd.Series) -> 'SlotOLS':
        design = np.column_stack([np.ones(len(x)), x.to_numpy(dtype=float)])
        self.columns_ = list(x.columns)
        self.coef_, *_ = np.linalg.lstsq(design, y.to_numpy(dtype=float), rcond=None)
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        design = np.column_stack([np.ones(len(x)), x[self.columns_].to_numpy(dtype=float)])
        return design @ self.coef_

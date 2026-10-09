"""EXP-006: a smoothed temperature T_s(t) = a * T_s(t-1) + (1 - a) * T(t) as a second GAM effect (M4).

The recursion runs over the temperature frame of `weather_features.read_temperature`, where each
delivery day carries the temperature of the run selected for that very day (D11), published before
that day's cutoff. T_s for day D therefore chains the runs of the days up to D, every one of them
published before cutoff(D). It never reads an observation, never a run downloaded later for an
earlier day, and the recursion only looks backwards. A slot without temperature gets NaN and leaves
the state unchanged: the state is carried forward, without decay, to the next slot that has one.
The state starts at the first temperature of the frame, so the first weeks depend on that choice.
"""

from datetime import date

import numpy as np
import pandas as pd

from load_forecast.backtest import FeatureBuilder
from load_forecast.gam_model import lagged_gam_features

SMOOTH_COLUMN = 'temperature_smooth_c'
SLOTS_PER_HOUR = 2


def smoothing_constant(half_life_hours: float) -> float:
    """Per half-hour constant a such that a step response reaches 50 percent after the half life."""
    if half_life_hours <= 0:
        raise ValueError('Half life must be positive')
    return float(0.5 ** (1.0 / (SLOTS_PER_HOUR * half_life_hours)))


def smooth_temperature(celsius: pd.Series, a: float) -> pd.Series:
    """Exponential smoothing in time order over the whole frame; NaN in, NaN out, state carried."""
    if not 0.0 <= a < 1.0:
        raise ValueError('Smoothing constant must be in [0, 1)')
    index = celsius.index
    if (not isinstance(index, pd.DatetimeIndex) or str(index.tz) != 'UTC'
            or not index.is_monotonic_increasing or index.has_duplicates):
        raise ValueError('Expected a sorted unique UTC temperature index')
    values = celsius.to_numpy(dtype=float)
    smoothed = np.full(len(values), np.nan)
    state = np.nan
    for i, value in enumerate(values):
        if np.isnan(value):
            continue
        state = value if np.isnan(state) else a * state + (1.0 - a) * value
        smoothed[i] = state
    return pd.Series(smoothed, index=index, name=SMOOTH_COLUMN)


def smoothed_gam_features(temperature: pd.DataFrame, a: float) -> FeatureBuilder:
    """EXP-005 features plus T_s, read at the slots of the day asked for only.

    The chain is computed once over the frame and is causal, so a row after day D never reaches the
    value of day D. Coverage rules (days before the archive, gaps, days after the last) are those of
    the temperature builder underneath.
    """
    base = lagged_gam_features(temperature)
    smooth = smooth_temperature(temperature['temperature_c'], a)

    def builder(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
        frame = base(slots, history, day)
        frame[SMOOTH_COLUMN] = smooth.reindex(slots)
        return frame

    return builder

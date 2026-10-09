"""Rung 2 of the modeling plan (M5, M6): a penalized B-spline GAM per local half-hour slot.

Same fit/predict interface as `SlotOLS`, so it plugs into the walk-forward runner. The
smoothing weight is chosen by generalized cross validation over a fixed log grid on the training rows handed to
`fit`, so no later observation can influence it (D13).
"""

from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np
import pandas as pd
from statsmodels.gam.api import BSplines, GLMGam

from load_forecast.backtest import FeatureBuilder
from load_forecast.lag_model import lagged_ols_features

TAIL_STEP = 1e-3
TEMPERATURE_COLUMN = 'temperature_c'


class SlotGAM:
    """Gaussian GLMGam: linear terms for `linear_cols`, penalized cubic B-splines for `smooth_cols`."""

    def __init__(self, smooth_cols: list[str], df: int = 8, degree: int = 3,
                 alpha_grid: tuple[float, ...] = tuple(np.logspace(-2, 6, 17)),
                 missing_as_nan: bool = False) -> None:
        if not smooth_cols:
            raise ValueError('At least one smooth column is required')
        self.smooth_cols, self.df, self.degree = list(smooth_cols), df, degree
        self.alpha_grid = alpha_grid
        # Production days with a missing lag or missing weather reach `predict` with NaN features.
        # The runner never imputes: such rows get NaN and the caller applies its backup model (M11).
        self.missing_as_nan = missing_as_nan

    @staticmethod
    def _check(x: pd.DataFrame) -> None:
        if x.isna().any().any():
            raise ValueError('GAM features must not contain NaN')

    def fit(self, x: pd.DataFrame, y: pd.Series) -> 'SlotGAM':
        self._check(x)
        if y.isna().any():
            raise ValueError('GAM target must not contain NaN')
        missing = set(self.smooth_cols) - set(x.columns)
        if missing:
            raise ValueError(f'Missing smooth columns: {sorted(missing)}')
        linear = x.drop(columns=self.smooth_cols)
        self.linear_cols_ = [c for c in linear.columns if linear[c].nunique() > 1]
        self.lower_ = x[self.smooth_cols].min()
        self.upper_ = x[self.smooth_cols].max()
        smooth = x[self.smooth_cols].to_numpy(dtype=float)
        self.splines_ = BSplines(smooth, df=[self.df] * len(self.smooth_cols),
                                 degree=[self.degree] * len(self.smooth_cols),
                                 include_intercept=False)
        exog = self._exog(x)
        endog = y.to_numpy(dtype=float)
        best = None
        for alpha in self.alpha_grid:
            result = GLMGam(endog, exog=exog, smoother=self.splines_,
                            alpha=np.full(len(self.smooth_cols), alpha)).fit()
            if best is None or result.gcv < best[0]:
                best = (result.gcv, alpha, result)
        _, alpha, self.result_ = best
        self.alpha_ = alpha
        self._fit_tail_slopes(x)
        return self

    def _smooth_effect(self, x: pd.DataFrame, values: pd.DataFrame) -> np.ndarray:
        # Linear terms are held at the first row of `x`, so differences isolate the smooths.
        exog = np.repeat(self._exog(x.iloc[:1]), len(values), axis=0)
        return np.asarray(self.result_.predict(exog=exog, exog_smooth=values.to_numpy(dtype=float)))

    def _fit_tail_slopes(self, x: pd.DataFrame) -> None:
        # Slope of each fitted smooth at its training boundary, from training data only.
        # Evaluated one step inside the range, since B-splines are not defined outside it.
        base = pd.DataFrame([self.lower_.to_dict()])
        self.slope_lower_, self.slope_upper_ = {}, {}
        for bound, sign, store in ((self.lower_, 1.0, self.slope_lower_),
                                   (self.upper_, -1.0, self.slope_upper_)):
            for col in self.smooth_cols:
                step = TAIL_STEP * (self.upper_[col] - self.lower_[col])
                edge, inner = base.copy(), base.copy()
                edge[col], inner[col] = bound[col], bound[col] + sign * step
                effect = self._smooth_effect(x, pd.concat([edge, inner]))
                store[col] = float((effect[0] - effect[1]) / (-sign * step))

    def _exog(self, x: pd.DataFrame) -> np.ndarray:
        columns = x[self.linear_cols_].to_numpy(dtype=float)
        return np.column_stack([np.ones(len(x)), columns])

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        if self.missing_as_nan:
            complete = x.notna().all(axis=1).to_numpy()
            out = np.full(len(x), np.nan)
            if complete.any():
                out[complete] = self._predict_complete(x.loc[complete])
            return out
        self._check(x)
        return self._predict_complete(x)

    def _predict_complete(self, x: pd.DataFrame) -> np.ndarray:
        # B-splines are not defined outside their knots: evaluate at the boundary and extend
        # linearly with the boundary slope, so a record cold day still raises the forecast.
        raw = x[self.smooth_cols]
        smooth = raw.clip(self.lower_, self.upper_, axis=1)
        out = np.asarray(self.result_.predict(
            exog=self._exog(x), exog_smooth=smooth.to_numpy(dtype=float)))
        for col in self.smooth_cols:
            out = out + self.slope_lower_[col] * np.minimum(raw[col] - self.lower_[col], 0.0)
            out = out + self.slope_upper_[col] * np.maximum(raw[col] - self.upper_[col], 0.0)
        return out

    def partial_effects(self, x: pd.DataFrame, groups: Mapping[str, Sequence[str]]) -> pd.DataFrame:
        """Additive split of `predict` (tails included): one column per group of linear terms.

        `base` holds the constant and every linear term outside the groups, `smooth` the penalized
        terms. The columns always sum to `predict(x)`. Rows with NaN features give NaN rows
        when the model was built with `missing_as_nan`.
        """
        named = [c for cols in groups.values() for c in cols]
        unknown = set(named) - set(x.columns)
        if unknown or len(named) != len(set(named)):
            raise ValueError(f'Group columns must exist in x and appear once: {sorted(unknown)}')
        if not self.missing_as_nan:
            self._check(x)
        complete = x.notna().all(axis=1).to_numpy()
        out = pd.DataFrame(np.nan, index=x.index, columns=['base', *groups, 'smooth'])
        if not complete.any():
            return out
        rows = x.loc[complete]
        coefficients = np.asarray(self.result_.params)[:1 + len(self.linear_cols_)]
        terms = pd.DataFrame(self._exog(rows) * coefficients, index=rows.index,
                             columns=['const', *self.linear_cols_])
        effects = pd.DataFrame(index=rows.index)
        for name, cols in groups.items():
            effects[name] = terms[[c for c in cols if c in terms.columns]].sum(axis=1)
        effects['base'] = terms.drop(columns=[c for c in named if c in terms.columns]).sum(axis=1)
        effects['smooth'] = self._predict_complete(rows) - terms.sum(axis=1).to_numpy()
        out.loc[complete] = effects[out.columns].to_numpy()
        return out


def lagged_gam_features(temperature: pd.DataFrame) -> FeatureBuilder:
    """EXP-004 features with the degree-day pair replaced by the temperature itself (M4 option C)."""
    base = lagged_ols_features(temperature)

    def builder(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
        frame = base(slots, history, day).drop(columns=['hdd', 'cdd'])
        frame[TEMPERATURE_COLUMN] = temperature['temperature_c'].reindex(slots)
        return frame

    return builder

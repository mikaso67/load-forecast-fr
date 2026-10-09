"""Rung 3 of the modeling plan (M5 form B): one global LightGBM model, the local slot being a feature.

Same fit/predict interface as the slot models, so `forecast_day` is reused unchanged: the 48 slot
entries of the model dictionary all point to the same fitted booster, which reads the slot from the
timestamps of the rows it receives. The objective is L1 because MAE is scored. Training rows are the
complete rows known at the refit cutoff (`training_matrix`, shared with the slot models), so nothing
after the cutoff reaches a fit. Rows with a missing feature are not imputed: LightGBM would route a
NaN down a learned branch, so such rows are given NaN here and the caller applies its backup (M11).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import lightgbm as lgb
import numpy as np
import pandas as pd

from load_forecast.backtest import FeatureBuilder, forecast_day, local_slots, training_matrix
from load_forecast.periods import last_allowed_day, locked_message

SLOT_COLUMN = 'slot'
SEED = 20240
N_SLOTS = 48


@dataclass(frozen=True)
class GBMConfig:
    learning_rate: float
    num_leaves: int
    min_data_in_leaf: int
    rounds: int

    def label(self) -> str:
        return (f'lr={self.learning_rate}, leaves={self.num_leaves}, '
                f'min_data={self.min_data_in_leaf}, rounds={self.rounds}')


def booster_params(config: GBMConfig) -> dict:
    """Everything that is not tuned is fixed here, so a run is deterministic."""
    return {'objective': 'l1', 'metric': 'None', 'learning_rate': config.learning_rate,
            'num_leaves': config.num_leaves, 'min_data_in_leaf': config.min_data_in_leaf,
            'bagging_fraction': 1.0, 'feature_fraction': 1.0, 'lambda_l1': 0.0, 'lambda_l2': 0.0,
            'seed': SEED, 'deterministic': True, 'force_row_wise': True, 'verbosity': -1}


class GlobalGBM:
    """LightGBM booster over all slots; `x` is the day feature frame, slot taken from its index."""

    def __init__(self, config: GBMConfig) -> None:
        self.config = config

    @staticmethod
    def _with_slot(x: pd.DataFrame) -> pd.DataFrame:
        frame = x.copy()
        frame[SLOT_COLUMN] = np.asarray(local_slots(x.index), dtype=float)
        return frame

    def fit(self, x: pd.DataFrame, y: pd.Series) -> 'GlobalGBM':
        if x.isna().any().any() or y.isna().any():
            raise ValueError('Training rows must be complete')
        design = self._with_slot(x)
        self.columns_ = list(design.columns)
        data = lgb.Dataset(design.to_numpy(dtype=float), label=y.to_numpy(dtype=float),
                           feature_name=self.columns_, free_raw_data=False)
        self.booster_ = lgb.train(booster_params(self.config), data,
                                  num_boost_round=self.config.rounds)
        return self

    def predict(self, x: pd.DataFrame, rounds: int | None = None) -> np.ndarray:
        """Forecast of the first `rounds` trees (all of them by default); NaN rows stay NaN."""
        complete = x.notna().all(axis=1).to_numpy()
        out = np.full(len(x), np.nan)
        if complete.any():
            design = self._with_slot(x.loc[complete])[self.columns_]
            out[complete] = self.booster_.predict(design.to_numpy(dtype=float),
                                                  num_iteration=rounds)
        return out


class AtRounds:
    """View of a fitted model that predicts with a fixed number of trees."""

    def __init__(self, model: GlobalGBM, rounds: int) -> None:
        self.model, self.rounds = model, rounds

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return self.model.predict(x, rounds=self.rounds)


def walk_forward_global(
    history: pd.Series, first: date, last: date, config: GBMConfig, *,
    builder: FeatureBuilder, training_start: date = date(2021, 3, 23),
    views: Sequence[int] | None = None,
    on_refit: Callable[[date, GlobalGBM], None] | None = None, final_test: bool = False,
) -> dict[int, pd.DataFrame]:
    """Weekly Monday refit, expanding window, same schedule as `walk_forward`.

    Returns one forecast frame per number of trees in `views` (default: `config.rounds` only). A
    prefix of the trees of a boosted model is the model trained for that many rounds, so several
    candidate round counts are scored from a single fit per week.
    """
    rounds = tuple(views) if views is not None else (config.rounds,)
    if not rounds or max(rounds) > config.rounds or min(rounds) < 1:
        raise ValueError('Every view needs between 1 and config.rounds trees')
    if first > last or first <= training_start or last > last_allowed_day(final_test):
        raise ValueError(f'Expected a period after training_start and {locked_message(final_test)}')
    cache: dict[date, pd.DataFrame] = {}
    outputs: dict[int, list[pd.DataFrame]] = {k: [] for k in rounds}
    refit, model = None, None
    for day in pd.date_range(first, last).date:
        monday = day - timedelta(days=day.weekday())
        if refit != monday:
            refit = monday
            x, y = training_matrix(history, refit, builder=builder, published_at=None,
                                   training_start=training_start, cache=cache)
            model = GlobalGBM(config).fit(x, y)
            if on_refit is not None:
                on_refit(refit, model)
        for k in rounds:
            view = AtRounds(model, k)
            outputs[k].append(forecast_day({s: view for s in range(N_SLOTS)}, history, day,
                                           refit, builder))
    return {k: pd.concat(frames) for k, frames in outputs.items()}

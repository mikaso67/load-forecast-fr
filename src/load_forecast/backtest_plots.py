"""Common-slot diagnostics, using the protocol's evaluation functions."""

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from load_forecast import evaluation as ev

BLUE = '#1f4e9c'
GREY = '#777777'


def plot_data(
    frame: pd.DataFrame, first: date, last: date, *, reference: pd.Series | None = None,
    cold: Mapping[date, bool] | None = None,
    reference_column: str = ev.RTE,
) -> dict:
    """Keep missing days; pair reference errors on the same four-way common slots.

    Cold flags are optional evaluation labels, never inferred from model inputs.
    Without labels the ordinary row includes unlabelled cold days, as in EXP-001.
    Grouped errors use reference_column on the intersection with RTE; an explicit
    reference Series overrides only the daily comparison.
    """
    kept = ev.common_slots(frame).sort_index()
    days = ev.local_dates(frame.index)
    if first > last or frame.empty or min(days) < first or max(days) > last:
        raise ValueError('Plot frame must lie within the backtest calendar')
    if not np.isfinite(kept.to_numpy(dtype=float)).all():
        raise ValueError('Plot values must be finite or missing')
    if reference_column in (ev.ACTUAL, ev.MODEL):
        raise ValueError('Reference column must be a separate forecast')
    grouped = kept.copy()
    grouped[ev.RTE] = frame[reference_column].reindex(kept.index)
    grouped = ev.common_slots(grouped)
    if not np.isfinite(grouped.to_numpy(dtype=float)).all():
        raise ValueError('Reference values must be finite or missing')
    errors = grouped[[ev.MODEL, ev.RTE]].sub(grouped[ev.ACTUAL], axis=0).abs()
    errors = errors.rename(columns={ev.RTE: reference_column})
    local = errors.index.tz_convert('Europe/Paris')
    monthly = errors.groupby(local.strftime('%Y-%m')).mean()
    hourly = errors.groupby(local.hour).mean().reindex(range(24))
    types = ev.mae_by_day_type(grouped, cold).rename(columns={ev.RTE: reference_column})
    if cold is None:
        types = types.drop(index=['cold', 'cold_any'])
    paired = frame[[ev.ACTUAL, ev.MODEL, ev.RTE]].copy()
    paired['reference'] = (frame[reference_column] if reference is None
                           else reference.reindex(frame.index))
    paired = paired.dropna()
    if not np.isfinite(paired.to_numpy(dtype=float)).all():
        raise ValueError('Reference values must be finite or missing')
    model_daily = ev.daily_mae(paired)[ev.MODEL]
    ref_daily = ev.daily_mae(paired.assign(**{ev.MODEL: paired['reference']}))[ev.MODEL]
    calendar = pd.date_range(first, last, freq='D')
    model_daily.index = pd.DatetimeIndex(model_daily.index)
    ref_daily.index = pd.DatetimeIndex(ref_daily.index)
    difference = model_daily.reindex(calendar) - ref_daily.reindex(calendar)
    bootstrap = ev.compare_daily(model_daily, ref_daily, first=first, last=last)
    # Reindex the entire grid so an absent row or value creates a visible line gap.
    start = pd.Timestamp(first, tz='Europe/Paris').tz_convert('UTC')
    end = (pd.Timestamp(last) + pd.Timedelta(days=1)).tz_localize('Europe/Paris').tz_convert('UTC')
    grid = pd.date_range(start, end, freq='30min', inclusive='left')
    return {'series': kept.reindex(grid), 'monthly': monthly, 'hourly': hourly,
            'types': types, 'difference': difference, 'bootstrap': bootstrap}


def _figure(title: str, xlabel: str, ylabel: str) -> tuple[Figure, object]:
    figure = Figure(figsize=(11, 4.5), facecolor='white', layout='constrained')
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    axes.set_facecolor('white')
    axes.set(title=title, xlabel=xlabel, ylabel=ylabel)
    axes.grid(axis='y', color='#dddddd', linewidth=0.5)
    return figure, axes


def _comparison_title(
    table: pd.DataFrame, context: str, reference_column: str, reference_name: str,
) -> str:
    usable = table[[ev.MODEL, reference_column]].dropna()
    lower = int((usable[ev.MODEL] < usable[reference_column]).sum())
    return f'Model MAE is lower than {reference_name} in {lower} of {len(usable)} {context}'


def save_backtest_plots(
    frame: pd.DataFrame, first: date, last: date, output: Path, *,
    reference: pd.Series | None = None, reference_name: str = 'RTE D-1',
    cold: Mapping[date, bool] | None = None,
    reference_column: str = ev.RTE,
) -> list[Path]:
    """Render five PNGs without pyplot/global style changes or fitted statistics."""
    data = plot_data(frame, first, last, reference=reference, cold=cold,
                     reference_column=reference_column)
    grouped_name = 'RTE D-1' if reference_column == ev.RTE else reference_name
    output.mkdir(parents=True, exist_ok=True)
    paths = []

    def save(figure: Figure, name: str) -> None:
        path = output / f'{name}.png'
        figure.savefig(path, dpi=150, facecolor='white')
        figure.clear()
        paths.append(path)

    scores = ev.score(frame)
    figure, axes = _figure(
        f'Model MAE {scores.model.mae:.1f} MW; RTE MAE {scores.rte.mae:.1f} MW',
        'Slot start (UTC)', 'Consumption / forecast (MW)')
    series = data['series']
    for column, label, color in [(ev.ACTUAL, 'Actual', '#222222'),
                                 (ev.RTE, 'RTE D-1', GREY), (ev.MODEL, 'Model', BLUE)]:
        axes.plot(series.index, series[column], label=label, color=color, linewidth=0.6)
    axes.legend()
    save(figure, 'actual_vs_forecast')

    for key, xlabel, context in [('monthly', 'Month (Europe/Paris)', 'months'),
                                  ('hourly', 'Hour (Europe/Paris)', 'local hours'),
                                  ('types', 'Day type (Europe/Paris)', 'day types')]:
        table = data[key]
        title = _comparison_title(table, context, reference_column, grouped_name)
        if key == 'types' and cold is None:
            title += '\nCold labels unavailable; ordinary includes unlabelled cold days'
        figure, axes = _figure(title, xlabel, 'MAE (MW)')
        positions = np.arange(len(table))
        for offset, column, label, color in [(-0.2, ev.MODEL, 'Model', BLUE),
                                              (0.2, reference_column, grouped_name, GREY)]:
            axes.bar(positions + offset, table[column], width=0.4, color=color, label=label)
        axes.set_xticks(positions, table.index.astype(str))
        axes.legend()
        save(figure, f'mae_by_{key}')

    bootstrap = data['bootstrap']
    figure, axes = _figure(
        f'Model minus {reference_name}: {bootstrap.mean:+.1f} MW, {bootstrap.verdict}',
        'Delivery date (Europe/Paris)', f'Daily MAE difference (MW), model minus {reference_name}')
    difference = data['difference']
    axes.plot(difference.index, difference, color=BLUE, linewidth=0.8, label='Daily difference')
    axes.axhline(0, color=GREY, linewidth=0.8)
    axes.axhline(bootstrap.mean, color=BLUE, linestyle='--', label='Mean daily difference')
    axes.axhspan(bootstrap.low, bootstrap.high, color=GREY, alpha=0.2,
                 label=f'95% interval of mean ({bootstrap.block_days}-day block bootstrap)')
    axes.legend()
    save(figure, 'daily_difference')
    return paths

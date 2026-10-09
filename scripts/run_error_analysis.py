"""error_analysis: error analysis of candidate 1 (EXP-009 point forecast, EXP-012 intervals) on 2024 and 2025.

No model is fitted. Forecasts and intervals come from the saved outputs of EXP-012 (2024) and
TEST-2025 (2025). Consumption and RTE D-1 are read from analytics.load_30min for 2023-01-01 to
2025-12-31 Paris, observed national temperature and the cold labels from the SYNOP labels
(evaluation ground truth). RTE* is rebuilt with the D15 rule and must reproduce the saved column.
Writes data/experiments/error_analysis/ and docs/figures/error_analysis_*.png, prints the tables.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from load_forecast import error_analysis as ea  # noqa: E402
from load_forecast import evaluation as ev  # noqa: E402
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database  # noqa: E402
from load_forecast.labels import COLD_THRESHOLD_C, read_labels
from load_forecast.lag_model import lag_features  # noqa: E402
from load_forecast.rte_reference import prior_year_hourly_reference  # noqa: E402

BLUE, GREY, LIGHT = '#1f4e9c', '#555555', '#b5b5b5'
EXP012 = Path('data/experiments/EXP-012/intervals.csv')
TEST = Path('data/experiments/TEST-2025')
OUTPUT = Path('data/experiments/error_analysis')
FIGURES = Path('docs/figures')
YEARS = (2024, 2025)
EDGES = (-np.inf, 2.0, COLD_THRESHOLD_C, 8.0, 12.0, 16.0, 20.0, np.inf)
# Accepted figures the rebuilt frames must reproduce (model, raw RTE, RTE*; EXP-012 on 2024, TEST-2025).
EXPECTED_MAE = {2024: (828.52, 1364.58, 896.65), 2025: (808.81, 1324.98, 942.57)}
QUERY = """SELECT value_time, consommation, prevision_j1 FROM {table}
WHERE value_time >= %s AND value_time < %s ORDER BY value_time"""


def paris_utc(day: str) -> pd.Timestamp:
    return pd.Timestamp(day, tz='Europe/Paris').tz_convert('UTC')


def read_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    with connect_database() as connection:
        rows = connection.execute(clean_query(QUERY), (paris_utc('2023-01-01'), paris_utc('2026-01-01'))).fetchall()
        labels = read_labels(connection, date(2024, 1, 1), date(2026, 1, 1))
    load = pd.DataFrame(rows, columns=['time', 'actual', 'rte_d1'])
    load.index = pd.DatetimeIndex(pd.to_datetime(load.pop('time'), utc=True))
    return load, labels


def build_frames(load: pd.DataFrame) -> dict[int, pd.DataFrame]:
    targets = load.rte_d1[load.index >= paris_utc('2024-01-01')]
    load = load.assign(rte_star=prior_year_hourly_reference(load.actual, load.rte_d1, targets))
    saved = {2024: pd.read_csv(EXP012, index_col=0, parse_dates=True),
             2025: pd.read_csv(TEST / 'intervals.csv', index_col=0, parse_dates=True)}
    frames = {}
    for year, intervals in saved.items():
        frame = intervals.rename(columns={'point': 'model'}).drop(columns='actual')
        frame = frame.join(load[['actual', 'rte_d1', 'rte_star']])
        scored = ea.common(frame)
        got = tuple(round(float((scored[c] - scored.actual).abs().mean()), 2) for c in ea.FORECASTS)
        if not np.allclose(got, EXPECTED_MAE[year], atol=0.01):
            raise RuntimeError(f'{year}: rebuilt MAE {got} differs from accepted {EXPECTED_MAE[year]}')
        frames[year] = frame
    check = pd.read_csv(TEST / 'predictions.csv', index_col=0, parse_dates=True)
    if (check.rte_star - frames[2025].rte_star.reindex(check.index)).abs().max() > 1e-6:
        raise RuntimeError('Rebuilt RTE* differs from the saved TEST-2025 column')
    return frames


def style(ax: plt.Axes, xlabel: str, ylabel: str, title: str) -> None:
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10, loc='left')
    ax.grid(alpha=0.25)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)


def save(fig: plt.Figure, name: str) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(FIGURES / name, dpi=130, facecolor='white')
    plt.close(fig)


def plot_worst_days(dailies: dict[int, pd.DataFrame], tops: dict[int, pd.DataFrame]) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), facecolor='white')
    for ax, year in zip(axes, YEARS, strict=True):
        daily, top = dailies[year], tops[year]
        days = pd.to_datetime(daily.index)
        ax.bar(days, daily.bias, color=LIGHT, width=1.0)
        marked = pd.to_datetime(top.index)
        ax.bar(marked, top.bias, color=BLUE, width=1.6)
        share = top.share_of_abs_error.sum() * 100
        style(ax, 'Day (Europe/Paris)', 'Mean error (MW)',
              f'{year}: the 10 worst days (blue) hold {share:.0f}% of the absolute error of the year')
        ax.axhline(0, color='black', lw=0.6)
    save(fig, 'error_analysis_worst_days.png')


def plot_profiles(profiles: dict[tuple[int, str], pd.DataFrame]) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), facecolor='white')
    for col, year in enumerate(YEARS):
        for row, by in enumerate(('hour', 'month')):
            ax, table = axes[row, col], profiles[(year, by)]
            ax.plot(table.index, table.rte_d1, color=LIGHT, label='raw RTE D-1')
            ax.plot(table.index, table.rte_star, color=GREY, ls='--', label='RTE*')
            ax.plot(table.index, table.model, color=BLUE, lw=2, label='model')
            worse = (table.model > table.rte_star).sum()
            unit = 'Hour of day (Europe/Paris)' if by == 'hour' else 'Month'
            style(ax, unit, 'MAE (MW)', f'{year}: model above RTE* in {worse} of {len(table)} {by}s')
            if row == 0 and col == 0:
                ax.legend(frameon=False, fontsize=8)
    save(fig, 'error_analysis_hour_month.png')


def plot_holidays(holiday_rows: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(11, 4.5), facecolor='white')
    x = np.arange(len(holiday_rows))
    low, high = holiday_rows.band_low, holiday_rows.band_high
    ax.bar(x, high - low, bottom=low, color='#dbe5f3', width=0.8, label='mean 80% interval bounds (error scale)')
    ax.scatter(x, holiday_rows.bias, color=BLUE, zorder=3, label='model mean error')
    ax.scatter(x, holiday_rows.rte_star_bias, color=GREY, marker='s', zorder=3, label='RTE* mean error')
    ax.axhline(0, color='black', lw=0.6)
    ax.set_xticks(x, [f"{d:%d %b %y}\n{n}" for d, n in zip(holiday_rows.index, holiday_rows.holiday, strict=True)],
                  fontsize=6.5, rotation=90)
    outside = ((holiday_rows.bias < low) | (holiday_rows.bias > high)).sum()
    style(ax, 'Holiday', 'Mean error, forecast minus actual (MW)',
          f'Mean error is outside the mean interval bounds on {outside} of {len(holiday_rows)} holidays '
          '(daily means, not slot coverage)')
    ax.legend(frameon=False, fontsize=8)
    save(fig, 'error_analysis_holidays.png')


def plot_temperature(bins: pd.DataFrame, trimmed: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), facecolor='white')
    labels = [f'{b.left:g} to {b.right:g}'.replace('-inf to', '< ').replace(' to inf', '+') for b in bins.index]
    labels[0] = f'< {bins.index[0].right:g}'
    labels[-1] = f'>= {bins.index[-1].left:g}'
    x = np.arange(len(bins))
    ax = axes[0]
    ax.plot(x, bins.rte_d1, color=LIGHT, marker='o', label='raw RTE D-1')
    ax.plot(x, bins.rte_star, color=GREY, ls='--', marker='o', label='RTE*')
    ax.plot(x, bins.model, color=BLUE, lw=2, marker='o', label='model')
    ax.plot(x, trimmed.model, color=BLUE, lw=1, ls=':', marker='o', ms=3,
            label='model, 23-29 Dec removed')
    ax.set_xticks(x, labels, fontsize=8)
    style(ax, 'Observed national daily temperature (deg C)', 'MAE (MW)',
          'Model against RTE* by temperature: edge lost between 2 and 4.84 deg C')
    ax.legend(frameon=False, fontsize=8)
    ax = axes[1]
    ax.bar(x, bins.coverage * 100, color=BLUE, width=0.6)
    ax.axhline(80, color=GREY, ls='--', lw=1)
    ax.set_xticks(x, labels, fontsize=8)
    ax.set_ylim(0, 100)
    style(ax, 'Observed national daily temperature (deg C)', 'Share of slots inside the 80% interval (%)',
          'Coverage by temperature, grey line = 80% target')
    save(fig, 'error_analysis_temperature.png')


def table_text(frame: pd.DataFrame, digits: int = 0) -> str:
    return frame.round(digits).to_string()


def backup_days(load: pd.DataFrame, year: int) -> list[date]:
    """Days whose lag block has a missing value, so the point experts fall back (lag_model rule)."""
    days = []
    for day in pd.date_range(f'{year}-01-01', f'{year}-12-31').date:
        start = pd.Timestamp(day, tz='Europe/Paris').tz_convert('UTC')
        end = pd.Timestamp(day + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        slots = pd.date_range(start, end, freq='30min', inclusive='left')
        if lag_features(slots, load.actual, day).isna().any().any():
            days.append(day)
    return days


def diagnostics(frames: dict[int, pd.DataFrame], dailies: dict[int, pd.DataFrame],
                holiday_rows: pd.DataFrame, plain: pd.DataFrame, load: pd.DataFrame) -> None:
    """Descriptive checks behind the readings of docs/error_analysis.md (no test, nothing selected)."""
    print('\n=== Year MAE with and without 23 to 29 December (slot-weighted over days, MW)')
    for year in YEARS:
        daily = dailies[year]
        keep = ~pd.Index([ea.special_week(d) for d in daily.index])
        for name in ('model', 'rte_star', 'rte_d1'):
            full = (daily[name] * daily.slots).sum() / daily.slots.sum()
            part = (daily.loc[keep, name] * daily.loc[keep, 'slots']).sum() / daily.loc[keep, 'slots'].sum()
            print(f'{year} {name}: all days {full:.1f}, without 23-29 Dec {part:.1f}')
        wins = (daily.loc[keep, 'model'] < daily.loc[keep, 'rte_star']).mean()
        print(f'{year}: days where model beats RTE* without that week {wins:.3f}')
    trimmed = plain[[not ea.special_week(d) for d in plain.index]]
    print('\n=== Ordinary and cold days without 23 to 29 December, by temperature bin (2024 and 2025 pooled)')
    print(ea.temperature_bins(trimmed, EDGES).round(2).to_string())
    cold = trimmed[trimmed.type == 'cold']
    print(f'cold days without that week: {len(cold)}, model MAE {cold.model.mean():.0f}, RTE* {cold.rte_star.mean():.0f}, '
          f'raw RTE {cold.rte_d1.mean():.0f}, bias {cold.bias.mean():.0f}, coverage {cold.coverage.mean():.3f}, '
          f'negative bias on {int((cold.bias < 0).sum())} days')
    print('\n=== Days after a spring change (D-2 or D-7 is the 23-hour day): daily MAE, model / RTE*')
    for year in YEARS:
        daily = dailies[year]
        picks = [d for d in daily.index if ev.is_dst_day(d - timedelta(days=2)) or ev.is_dst_day(d - timedelta(days=7))]
        print(year, daily.loc[picks, ['model', 'bias', 'rte_star', 'type_d2']].round(0).to_string())
    for year in YEARS:
        listed = backup_days(load, year)
        print(f'{year}: days with a missing lag (backup forecast): {listed}')
        print(dailies[year].loc[listed, ['model', 'bias', 'rte_star']].round(0).to_string())
    print('\n=== Holidays by weekday: signed error of the model (forecast minus actual, MW)')
    rows = holiday_rows.assign(group=np.where(holiday_rows.weekday == 'Mon', 'Monday', 'Tuesday to Saturday'))
    rows = rows[rows.weekday != 'Sun']
    print(rows.groupby('group').bias.agg(['count', 'mean', 'min', 'max',
                                          lambda x: int((x > 0).sum())]).round(0).to_string())
    print('Monday holidays:', rows[rows.group == 'Monday'][['holiday', 'bias']].round(0).to_dict('records'))
    print('\n=== Holiday-only pool, leave one holiday out (22 holidays of 2024 and 2025, per slot, post-hoc)')
    days = list(holiday_rows.index)
    frame = pd.concat([frames[2024], frames[2025]])
    loo = ea.leave_one_out_interval(ea.holiday_residuals(frame, days))
    print(f'mean coverage {loo.coverage.mean():.3f}, mean width {loo.width.mean():.0f} MW '
          f'(the seasonal interval on the same days: {holiday_rows.width.mean():.0f} MW, '
          f'coverage {holiday_rows.coverage.mean():.3f}); days with coverage 1: {int((loo.coverage == 1).sum())}')
    print('\n=== Month MAE and coverage, with the season of the month')
    for year in YEARS:
        scored = ea.common(frames[year]).dropna(subset=['covered'])
        month = scored.index.tz_convert('Europe/Paris').month
        table = pd.DataFrame({'mae': (scored.model - scored.actual).abs().groupby(month).mean(),
                              'coverage': scored.covered.astype(float).groupby(month).mean() * 100})
        table['season'] = [ev.get_season(m) for m in table.index]
        hits = 0
        for _season, part in table.groupby('season'):
            hits += part.mae.idxmax() == part.coverage.idxmin()
        print(f'{year}: season in which the month of highest MAE is also the month of lowest coverage: {hits} of 4')
        print(table.round(1).T.to_string())


def main() -> None:
    load, labels = read_inputs()
    frames = build_frames(load)
    cold = {d: bool(v) for d, v in labels.cold.items() if not pd.isna(v)}
    temperature = labels.national_temperature_c.dropna().to_dict()
    dailies = {y: ea.daily_table(f, cold, temperature) for y, f in frames.items()}
    tops = {y: ea.worst_days(d) for y, d in dailies.items()}
    out: dict = {}
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 40)
    for year in YEARS:
        daily, top = dailies[year], tops[year]
        print(f'\n=== {year}: 10 worst days (MAE of the model, MW; bias = forecast minus actual)')
        columns = ['model', 'bias', 'rte_d1', 'rte_star', 'coverage', 'width', 'actual', 'actual_d7', 'type',
                   'holiday', 'type_d2', 'weekday', 'temperature', 'share_of_abs_error']
        print(top[columns].round(2).to_string())
        out[f'worst_{year}'] = json.loads(top[columns].reset_index().astype({'date': str}).to_json(orient='records'))
        print(f'share of the absolute error of the year in the 10 worst days: '
              f'{top.share_of_abs_error.sum():.3f}; days: {len(daily)}; unlabelled temperature days: '
              f'{daily.temperature.isna().sum()}')
        print(f'worst 10 days where the model is worse than RTE*: {int((top.model > top.rte_star).sum())}, '
              f'than raw RTE: {int((top.model > top.rte_d1).sum())}')
        change = (top.actual - top.actual_d7).abs() / 1000
        everyone = (daily.actual - daily.actual_d7).abs() / 1000
        print(f'worst 10 days with |load minus D-7 load| above 4 GW: {int((change > 4).sum())} of '
              f'{int(change.notna().sum())}; all days: {(everyone > 4).sum() / everyone.notna().sum():.3f}')
        worst20 = ea.worst_days(daily, 20).share_of_abs_error.sum()
        print(f'share in the 20 worst days: {worst20:.3f}')
    # Christmas to New Year weeks
    for year in YEARS:
        daily = dailies[year]
        window = [d for d in daily.index if d.month == 12 and 23 <= d.day <= 29]
        rest = daily.drop(index=window)
        print(f'\n=== {year}: 23 to 29 December against the other days (mean daily MAE, MW)')
        print(daily.loc[window, ['model', 'bias', 'rte_d1', 'rte_star', 'type', 'type_d2', 'weekday',
                                 'temperature']].round(1).to_string())
        print('mean daily MAE, 23-29 Dec: model {:.0f}, RTE* {:.0f}, raw RTE {:.0f}; other days: model {:.0f}, '
              'RTE* {:.0f}, raw RTE {:.0f}'.format(
                  daily.loc[window, 'model'].mean(), daily.loc[window, 'rte_star'].mean(),
                  daily.loc[window, 'rte_d1'].mean(), rest.model.mean(), rest.rte_star.mean(), rest.rte_d1.mean()))
    # Profiles by hour and by month
    profiles = {(y, by): ea.profile(frames[y], by) for y in YEARS for by in ('hour', 'month')}
    for (year, by), table in profiles.items():
        print(f'\n=== {year}: MAE by {by}')
        print(table.round(0).T.to_string())
    plot_worst_days(dailies, tops)
    plot_profiles(profiles)
    # Holidays
    holiday_rows = pd.concat([dailies[y][dailies[y].holiday != ''] for y in YEARS])
    star_bias = {}
    for year in YEARS:
        scored = ea.common(frames[year])
        days = pd.Index(ev.local_dates(scored.index))
        star_bias.update((scored.rte_star - scored.actual).set_axis(days).groupby(level=0).mean().to_dict())
    holiday_rows['rte_star_bias'] = [star_bias[d] for d in holiday_rows.index]
    holiday_rows['holidays_in_pool'] = [ea.holidays_in_pool(d) for d in holiday_rows.index]
    pool = frames[2025].n_pool
    holiday_rows['tail_rank'] = ea.tail_rank(int(pool.median()))
    holiday_rows['bias_over_halfwidth'] = holiday_rows.bias / (holiday_rows.width / 2)
    inside = (holiday_rows.bias >= holiday_rows.band_low) & (holiday_rows.bias <= holiday_rows.band_high)
    centre = (holiday_rows.band_low + holiday_rows.band_high) / 2
    print(f'holidays whose mean error is outside the mean interval bounds: {int((~inside).sum())} of {len(inside)}; '
          f'largest offset of the band centre from zero: {centre.abs().max():.1f} MW')
    print('\n=== Holidays (model bias = forecast minus actual, MW; coverage = share of slots inside)')
    print(holiday_rows[['holiday', 'weekday', 'model', 'bias', 'rte_star', 'rte_star_bias', 'rte_d1', 'coverage',
                        'width', 'bias_over_halfwidth', 'type_d2', 'holidays_in_pool']].round(2).to_string())
    for year in YEARS:
        rows = holiday_rows[[d.year == year for d in holiday_rows.index]]
        print(f'{year}: {len(rows)} holidays, model MAE {rows.model.mean():.0f}, RTE* {rows.rte_star.mean():.0f}, '
              f'raw RTE {rows.rte_d1.mean():.0f}, mean coverage {rows.coverage.mean():.3f}, '
              f'mean signed error {rows.bias.mean():.0f}, days with positive bias {int((rows.bias > 0).sum())}, '
              f'tail rank {ea.tail_rank(90)} of ~90')
    out['holidays'] = json.loads(holiday_rows.reset_index().astype({'date': str}).to_json(orient='records'))
    plot_holidays(holiday_rows)
    # Temperature
    both = pd.concat([dailies[y] for y in YEARS])
    plain = both[both.type.isin(['ordinary', 'cold'])]
    bins = ea.temperature_bins(plain, EDGES)
    print('\n=== Ordinary and cold days (2024 and 2025 pooled) by observed temperature bin')
    print(bins.round(2).to_string())
    for year in YEARS:
        part = ea.temperature_bins(dailies[year][dailies[year].type.isin(['ordinary', 'cold'])], EDGES)
        print(f'\n--- {year}')
        print(part.round(2).to_string())
    cold_days = both[both.type == 'cold']
    print(f'\ncold days (precedence, DST/holiday/bridge removed): {len(cold_days)}; model bias mean '
          f'{cold_days.bias.mean():.0f} MW, MAE {cold_days.model.mean():.0f}, RTE* {cold_days.rte_star.mean():.0f}, '
          f'raw RTE {cold_days.rte_d1.mean():.0f}, coverage {cold_days.coverage.mean():.3f}, '
          f'days with negative bias {int((cold_days.bias < 0).sum())}')
    print('correlation of daily signed error with temperature, ordinary+cold days: '
          f'{plain.bias.corr(plain.temperature):.2f}; of daily MAE: {plain.model.corr(plain.temperature):.2f}')
    plot_temperature(bins, ea.temperature_bins(plain[[not ea.special_week(d) for d in plain.index]], EDGES))
    out['temperature_bins'] = json.loads(bins.reset_index().astype({'temperature': str}).to_json(orient='records'))
    # Season boundaries
    for year in YEARS:
        print(f'\n=== {year}: coverage near a season change (14 days) against elsewhere')
        print(ea.boundary_coverage(frames[year]).round(3).to_string())
        monthly = ea.common(frames[year]).dropna(subset=['covered'])
        local = monthly.index.tz_convert('Europe/Paris')
        print((monthly.covered.astype(float).groupby(local.month).mean() * 100).round(1).to_string())
    diagnostics(frames, dailies, holiday_rows, plain, load)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'results.json').write_text(json.dumps(out, indent=1), encoding='utf-8')
    for year, daily in dailies.items():
        daily.to_csv(OUTPUT / f'daily_{year}.csv')


if __name__ == '__main__':
    main()

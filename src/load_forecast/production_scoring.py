"""Score immutable serving choices and rebuild versioned monitoring from stored actuals."""

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text

from load_forecast import monitoring
from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.pipeline_artifacts import write_artifact
from load_forecast.rte_reference import prior_year_hourly_reference
from load_forecast.serving import ForecastStore, ServingAuthority

LOGGER = logging.getLogger(__name__)

ACTUALS_QUERY = """
WITH ranked AS (
    SELECT value_time, consommation, prevision_j1, source_dataset, ingested_at, payload_hash,
           row_number() OVER (PARTITION BY source_dataset, value_time
                             ORDER BY ingested_at DESC, payload_hash DESC) AS rank
    FROM raw.eco2mix
    WHERE value_time >= :start AND value_time < :end AND ingested_at < :as_of
      AND source_dataset IN ('eco2mix-national-tr', 'eco2mix-national-cons-def')
      AND extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
      AND original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
)
SELECT * FROM ranked WHERE rank = 1 ORDER BY value_time
"""


def _json(value: dict | list) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value: dict | list) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def read_actuals(connection, start: date, end: date, as_of: datetime) -> dict[str, pd.DataFrame]:
    """Read latest stored source versions as of scoring time, preserving D12 and both scales."""
    begin = pd.Timestamp(date(start.year - 1, 1, 1), tz=PARIS).tz_convert('UTC')
    # Add a local calendar day before converting so the end boundary follows DST.
    stop = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(PARIS).tz_convert('UTC')
    rows = connection.execute(text(ACTUALS_QUERY),
                              {'start': begin.to_pydatetime(), 'end': stop.to_pydatetime(),
                               'as_of': as_of}).mappings().all()
    frame = pd.DataFrame(rows, columns=['value_time', 'consommation', 'prevision_j1',
                                      'source_dataset', 'ingested_at', 'payload_hash'])
    frame['value_time'] = pd.to_datetime(frame['value_time'], utc=True)
    frame['ingested_at'] = pd.to_datetime(frame['ingested_at'], utc=True)
    frame = frame.set_index('value_time')
    return {vintage: frame.loc[frame.source_dataset.eq(source)].copy()
            for vintage, source in [('realtime', 'eco2mix-national-tr'),
                                    ('consolidated', 'eco2mix-national-cons-def')]}


def forecast_frame(archive: dict | None) -> pd.DataFrame | None:
    """Convert an immutable archive to the UTC scoring contract."""
    if archive is None:
        return None
    frame = pd.DataFrame(archive['values'])
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop('value_time'), utc=True))
    return frame[['point', 'lower', 'upper']].astype(float)


def day_metrics(day: date, actuals: pd.DataFrame, vintage: str,
                served: pd.DataFrame | None, models: dict[str, pd.DataFrame | None],
                reference: pd.DataFrame | None = None) -> dict:
    """Score the headline and both shadow models on one common UTC grid."""
    target = actuals.reindex(pd.DatetimeIndex(delivery_slots(day, '30min')))
    rte = target.prevision_j1.astype(float)
    star, reason = None, None
    if vintage != 'realtime':
        reference = actuals if reference is None else reference
        try:
            star = prior_year_hourly_reference(reference.consommation.astype(float),
                                               reference.prevision_j1.astype(float), rte)
        except ValueError as error:
            reason = str(error)
    columns = [target.consommation.astype(float), rte]
    columns += [f.point for f in [served, *models.values()] if f is not None]
    if star is not None:
        columns.append(star)
    common = pd.concat(columns, axis=1).replace([np.inf, -np.inf], np.nan).dropna().index
    actual, rte = target.consommation.loc[common].astype(float), rte.loc[common]
    star = star.loc[common] if star is not None else None
    frames = {variant: f.reindex(common) if f is not None else None
              for variant, f in models.items()}
    delivered = served.reindex(common) if served is not None else None
    result = monitoring.score_day(actual, rte, delivered, frames['B'], star, vintage)
    result['model_a'] = monitoring.score_day(actual, rte, None, frames['A'], star, vintage)['model']
    result['rte_star_unavailable'] = reason
    return result


def monitor_history(rows: list[dict], end: date) -> list[dict]:
    """Rebuild each vintage separately so revisions also correct all downstream CUSUM states."""
    output = []
    for vintage in monitoring.VINTAGES:
        selected = [r for r in rows if r['actual_vintage'] == vintage]
        if not selected:
            continue
        calendar = pd.date_range(min(r['delivery_day'] for r in selected), end)
        by_day = {pd.Timestamp(r['delivery_day']): r for r in selected}
        dependencies = [by_day[d]['score_id'] if d in by_day else None for d in calendar]
        for variant, series, key in [('B', 'served', 'served'), ('B', 'model', 'model'),
                                     ('A', 'model', 'model_a')]:
            metrics = [by_day[d]['metrics'] if d in by_day else {} for d in calendar]
            errors = pd.Series([monitoring.daily_error({'served': m.get(key)}) for m in metrics],
                               index=calendar)
            z = monitoring.standardize(errors)
            states = monitoring.cusum(z, h=monitoring.H, k=monitoring.K)
            mae = pd.Series([m[key]['mae'] if m.get(key) else np.nan for m in metrics],
                            index=calendar)
            rte = pd.Series([m['rte_d1']['mae'] if m.get('rte_d1') else np.nan for m in metrics],
                            index=calendar)
            skill = monitoring.relative_skill(mae, rte)
            for i, stamp in enumerate(calendar):
                state = states.loc[stamp].to_dict()
                state |= {'z': float(z.loc[stamp]) if pd.notna(z.loc[stamp]) else None,
                          'h': monitoring.H, 'k': monitoring.K,
                          'cusum_alert': bool(state['alarm'] and vintage != 'realtime'),
                          'skill_mean_difference': (float(skill.loc[stamp, 'mean_difference'])
                              if pd.notna(skill.loc[stamp, 'mean_difference']) else None),
                          'skill_n_days': int(skill.loc[stamp, 'n_days']),
                          'skill_alert': bool(skill.loc[stamp, 'alert'])}
                # Prefix identities preserve unchanged earlier states on a later day's rerun.
                identity = {'scores': dependencies[:i + 1], 'variant': variant, 'series': series,
                            'vintage': vintage, 'day': str(stamp.date()), 'state': state}
                output.append({'monitor_id': _digest(identity), 'delivery_day': str(stamp.date()),
                               'variant': variant, 'series': series, 'actual_vintage': vintage,
                               'state': _json(state)})
    return output


def score_production(store: ForecastStore, authority: ServingAuthority, end: date,
                     now: datetime, actuals_reader=read_actuals) -> dict:
    """Append only new score/monitor versions; never execute a migration or mutate a choice."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Scoring time must be timezone aware')
    now = now.astimezone(UTC)
    end = min(end, now.astimezone(PARIS).date() - timedelta(days=1))
    days = sorted(date.fromisoformat(p.stem) for p in (authority.root / 'choice').glob('*.json')
                  if date.fromisoformat(p.stem) <= end)
    if not days:
        return {'day': str(end), 'status': 'no_deliveries', 'scores_inserted': 0,
                'monitoring_inserted': 0}
    inserted = monitors = 0
    with store.engine.begin() as connection:
        # Serializes overlapping daily and weekly scoring; actuals are read after the lock.
        if connection.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(708512)'))
        actuals = actuals_reader(connection, days[0], end, now)
        current = []
        for day in days:
            choice = authority.read_choice(day)
            served = forecast_frame(store.served_for_scoring(authority, day))
            runs = connection.execute(text('''SELECT variant, run_id FROM forecast.state_runs
                WHERE delivery_day = :day'''), {'day': str(day)}).mappings().all()
            ids = {r['variant']: r['run_id'] for r in runs}
            models = {v: forecast_frame(store.replay_run(ids[v])) if v in ids else None
                      for v in ('A', 'B')}
            day_complete = False
            for vintage, frame in actuals.items():
                target = frame.reindex(pd.DatetimeIndex(delivery_slots(day, '30min')))
                known = target.ingested_at.dropna()
                if (known.size != len(target)
                        or not np.isfinite(target.consommation.astype(float)).all()):
                    LOGGER.info('Skipping incomplete %s actuals for %s', vintage, day)
                    continue
                day_complete = True
                reference = None
                if vintage != 'realtime':
                    # A revised final score must not give RTE* a correction unavailable at issue.
                    reference = actuals_reader(connection, day, day, forecast_cutoff(day)).get(
                        'consolidated', frame.iloc[:0])
                metrics = day_metrics(day, frame, vintage, served, models, reference)
                metrics |= {'choice_kind': choice['kind'], 'model_run_ids': ids,
                            'status': 'provisional' if vintage == 'realtime' else 'final'}
                inputs = [{'time': stamp.isoformat(), 'hash': str(row.payload_hash),
                           'ingested_at': row.ingested_at.isoformat()}
                          for stamp, row in target.loc[target.ingested_at.notna()].iterrows()]
                row = monitoring.score_row(day, choice['choice_hash'], metrics,
                                           known.max().isoformat(), now.isoformat())
                row['score_id'] = _digest({'choice': choice['choice_hash'], 'actuals': inputs,
                                           'metrics': metrics})
                saved = {**row, 'metrics': _json(metrics)}
                result = connection.execute(text('''INSERT INTO forecast.scores
                    (delivery_day, choice_hash, actual_vintage, actual_max_ingested_at,
                     scored_at, metrics, score_id) VALUES (:delivery_day, :choice_hash,
                     :actual_vintage, :actual_max_ingested_at, :scored_at, :metrics, :score_id)
                    ON CONFLICT (score_id) DO NOTHING'''), saved)
                inserted += result.rowcount
                current.append(row)
            # Consolidated actuals arrive much later; a complete real-time day is sufficient.
            if not day_complete and now.astimezone(PARIS).date() > day + timedelta(days=3):
                incident = authority.root.parent / 'incidents' / f'scoring_incomplete_{day}.pkl'
                if not incident.exists():
                    LOGGER.warning('Scoring actuals remain incomplete more than 3 days after %s', day)
                    try:
                        write_artifact(incident, {'day': day, 'event': 'scoring_incomplete'})
                    except OSError:
                        LOGGER.exception('Could not persist incomplete scoring incident for %s', day)
        for row in monitor_history(current, end):
            result = connection.execute(text('''INSERT INTO forecast.monitoring
                (monitor_id, delivery_day, variant, series, actual_vintage, computed_at, state)
                VALUES (:monitor_id, :delivery_day, :variant, :series, :actual_vintage,
                        :computed_at, :state) ON CONFLICT (monitor_id) DO NOTHING'''),
                {**row, 'computed_at': now.isoformat()})
            monitors += result.rowcount
            state = json.loads(row['state'])
            if result.rowcount and (state['cusum_alert'] or state['skill_alert']):
                LOGGER.warning('Monitoring review for %s variant %s %s on %s: %s',
                               row['delivery_day'], row['variant'], row['series'],
                               row['actual_vintage'], row['state'])
    LOGGER.info('Scoring through %s: %s new scores, %s monitoring states', end, inserted, monitors)
    return {'day': str(end), 'status': 'scored', 'scores_inserted': inserted,
            'monitoring_inserted': monitors}

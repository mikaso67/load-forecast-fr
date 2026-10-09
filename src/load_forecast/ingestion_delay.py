"""Read-only first-ingestion delay of the 09:30 Paris real-time consumption slot."""

import argparse
import json
from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg

from load_forecast.eco2mix import connect_database, monthly_windows

DELAY_QUERY = '''SELECT value_time, min(ingested_at) AS first_ingested_at
                 FROM raw.eco2mix
                 WHERE source_dataset = 'eco2mix-national-tr' AND consommation IS NOT NULL
                   AND value_time >= %s AND value_time < %s
                   AND (value_time AT TIME ZONE 'Europe/Paris')::time = TIME '09:30'
                 GROUP BY value_time ORDER BY value_time'''


def summarize_delays(rows: list[tuple], start: date, end: date) -> dict:
    """Report the observed bound, retaining historical catch-up and negative delays."""
    if start >= end:
        raise ValueError('Expected increasing Paris dates; end is exclusive')
    daily = []
    for value_time, ingested_at in rows:
        if any(stamp.tzinfo is None or stamp.utcoffset() is None
               for stamp in (value_time, ingested_at)):
            raise ValueError('Expected aware timestamps')
        value_time = value_time.astimezone(UTC)
        ingested_at = ingested_at.astimezone(UTC)
        local = value_time.astimezone(ZoneInfo('Europe/Paris'))
        if not start <= local.date() < end or (local.hour, local.minute) != (9, 30):
            raise ValueError('Expected the requested 09:30 Paris slots')
        daily.append({'date': local.date().isoformat(), 'value_time': value_time.isoformat(),
                      'first_ingested_at': ingested_at.isoformat(),
                      'delay_minutes': (ingested_at - value_time - timedelta(minutes=30))
                      .total_seconds() / 60})
    if len({row['date'] for row in daily}) != len(daily):
        raise ValueError('Expected one first ingestion per day')
    delays = pd.Series([row['delay_minutes'] for row in daily], dtype=float)
    distribution = {name: float(delays.quantile(q)) if len(delays) else None
                    for name, q in [('min', 0), ('median', .5), ('p90', .9), ('p95', .95), ('max', 1)]}
    return {'start': start.isoformat(), 'end_exclusive': end.isoformat(),
            'expected_days': (end - start).days, 'observed_days': len(daily),
            'missing_days': (end - start).days - len(daily),
            'negative_delay_days': int((delays < 0).sum()),
            'within_120_minutes_days': int(((delays >= 0) & (delays <= 120)).sum()),
            'delay_minutes': distribution, 'daily': daily}


def read_delay_report(connection: psycopg.Connection, start: date, end: date) -> dict:
    """Use a consistent read-only transaction; no network source is queried."""
    windows = list(monthly_windows(start, end))
    with connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        rows = connection.execute(DELAY_QUERY, (windows[0][0], windows[-1][1])).fetchall()
    return summarize_delays(rows, start, end)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--daily', action='store_true')
    args = parser.parse_args()
    try:
        with connect_database() as connection:
            report = read_delay_report(connection, args.start, args.end)
        if not args.daily:
            report.pop('daily')
        print(json.dumps(report, sort_keys=True))
    except (psycopg.Error, KeyError, ValueError):
        parser.exit(1, 'Ingestion delay report failed; check environment and stored data.\n')


if __name__ == '__main__':
    main()

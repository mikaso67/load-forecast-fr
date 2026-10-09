"""Read-only Q1 to Q6 audit; load environment variables before running."""

import json
from datetime import UTC, datetime
from pathlib import Path

from load_forecast.eco2mix import connect_database

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data/audit/load_audit_003/results.json'


def main() -> None:
    queries = {
        'row_counts': 'SELECT source_dataset, count(*) FROM raw.eco2mix GROUP BY 1 ORDER BY 1',
        'q1': (ROOT / 'scripts/eco2mix_completeness.sql').read_text(),
        'q2': (ROOT / 'scripts/eco2mix_year_check.sql').read_text(),
        'q3': (ROOT / 'scripts/eco2mix_gaps.sql').read_text(),
        'dst_sample': """SELECT value_time, original_values FROM raw.eco2mix
            WHERE source_dataset = 'eco2mix-national-cons-def'
            AND value_time >= '2024-03-31 01:00 UTC'::timestamptz
            AND value_time < '2024-03-31 02:00 UTC'::timestamptz
            ORDER BY value_time, original_values->>'heure' """,
        'source_keys': 'SELECT DISTINCT jsonb_object_keys(original_values) AS field '
                       'FROM raw.eco2mix ORDER BY field',
    }
    observations = (ROOT / 'scripts/eco2mix_observations.sql').read_text()
    queries.update({f'q{n}': query for n, query in enumerate(observations.split(';'), 4)
                    if query.strip()})
    results = {}
    with connect_database() as connection, connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        for name, query in queries.items():
            params = {'start': datetime(2021, 3, 22, 23, tzinfo=UTC),
                      'end': datetime(2026, 10, 3, 22, tzinfo=UTC)} if name == 'q3' else None
            cursor = connection.execute(query, params)
            columns = [item.name for item in cursor.description]
            results[name] = [dict(zip(columns, row, strict=True)) for row in cursor]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(results, default=str, indent=2), encoding='utf-8')
    print(json.dumps({'output': 'data/audit/load_audit_003/results.json',
                      'rows_per_query': {name: len(rows) for name, rows in results.items()}},
                     sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Read-only eCO2mix audit failed; diagnostics suppressed') from None

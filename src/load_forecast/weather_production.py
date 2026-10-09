"""Stage candidate GFS steps, freeze stored revisions and replay pinned inputs."""

import argparse
import hashlib
import json
from datetime import UTC, date, datetime

import pandas as pd
import psycopg
import requests
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from load_forecast.cutoff import forecast_cutoff
from load_forecast.eco2mix import connect_database
from load_forecast.weather import (
    POINTS,
    S3_URL,
    VARIABLES,
    candidate_runs,
    extract_run,
    list_objects,
)
from load_forecast.weather_collector import DDL, INSERT_DAY, INSERT_POINT, point_row
from load_forecast.weather_features import temperature_day

IDENTITY = ('run_init_time', 'end_step', 'variable', 'point', 'message_hash')


def validate_variables(variables: tuple[str, ...]) -> None:
    """Require a nonempty set of supported fields for staging and completeness."""
    if not variables or len(set(variables)) != len(variables) or not set(variables) <= VARIABLES.keys():
        raise ValueError('Expected distinct known GFS variables')


def stage_candidates(
    connection: psycopg.Connection, session: requests.Session, day: date,
    variables: tuple[str, ...] = ('TMP',),
) -> dict:
    """Download available file/index pairs for both runs without selecting a day.

    Each step commits independently so an interrupted staging pass can be retried.
    HTTP metadata and byte-range validation use the historical collector's rules.
    """
    validate_variables(variables)
    result = {'steps': 0, 'points_inserted': 0}
    for run in candidate_runs(day):
        objects = list_objects(session, run)
        for step in range(run.first_step, run.first_step + 27):
            key = run.prefix + f'{step:03d}'
            if any(name not in objects or objects[name].size <= 0 for name in (key, key + '.idx')):
                continue
            points = extract_run(session, run, objects, variables, steps=(step,))
            ingested_at = datetime.now(UTC)
            rows = []
            for point in points:
                row = list(point_row(run, point, ingested_at))
                metadata = row[-2].obj | {'objects': {
                    name: {'last_modified': objects[name].modified.isoformat(),
                           'size': objects[name].size, 'etag': objects[name].etag}
                    for name in (key, key + '.idx')
                }}
                row[-2] = Jsonb(metadata)
                rows.append(tuple(row))
            with connection.transaction(), connection.cursor() as cursor:
                cursor.executemany(INSERT_POINT, rows)
                result['points_inserted'] += cursor.rowcount
            result['steps'] += 1
    return result


def _identity(row: dict) -> list:
    return [row['run_init_time'].isoformat(), *(row[key] for key in IDENTITY[1:])]


def _digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def pin_selection(
    day: date, rows: list[dict], *, now: datetime,
    variables: tuple[str, ...] = ('TMP',),
) -> dict:
    """Choose the latest eligible revision per required point; ties fail the run."""
    validate_variables(variables)
    cutoff = forecast_cutoff(day)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Selection time must be aware')
    if now < cutoff:
        raise ValueError("Delivery day's cutoff has not arrived")
    failures = {}
    for run in candidate_runs(day):
        groups = {}
        for row in rows:
            if (row['run_init_time'] == run.init_time
                    and row['file_last_modified'] < cutoff
                    and row['index_last_modified'] < cutoff):
                key = (row['end_step'], row['variable'], row['point'])
                groups.setdefault(key, []).append(row)
        pinned, missing = [], []
        for step in range(run.first_step, run.first_step + 27):
            for variable in sorted(variables):
                for point in sorted(POINTS):
                    key = (step, variable, point)
                    versions = groups.get(key, [])
                    latest = max((r['file_last_modified'] for r in versions), default=None)
                    winners = [r for r in versions if r['file_last_modified'] == latest]
                    if len(winners) != 1:
                        missing.append(list(key))
                    else:
                        pinned.append(winners[0])
        if missing:
            failures[run.init_time.isoformat()] = missing
            continue
        manifest = {'cutoff': cutoff.isoformat(), 'variables': sorted(variables),
                    'failures': failures, 'points': [_identity(row) for row in pinned],
                    'objects': {}}
        # Metadata for all 54 objects travels with the pinned manifest when staged.
        for row in pinned:
            manifest['objects'].update(row['original_metadata'].get('objects', {}))
        return {'delivery_day': day, 'cutoff': cutoff,
                'status': 'fallback' if run.fallback else 'primary',
                'run_init_time': run.init_time, 'fallback': run.fallback,
                'source_url': S3_URL + '/' + run.prefix,
                'selection_hash': _digest(manifest), 'manifest': manifest}
    manifest = {'cutoff': cutoff.isoformat(), 'variables': sorted(variables),
                'failures': failures, 'points': [], 'objects': {}}
    return {'delivery_day': day, 'cutoff': cutoff, 'status': 'weather missing',
            'run_init_time': None, 'fallback': False, 'source_url': None,
            'selection_hash': _digest(manifest), 'manifest': manifest}


def select_staged(
    connection: psycopg.Connection, day: date, *, now: datetime | None = None,
    variables: tuple[str, ...] = ('TMP',),
) -> dict:
    """Select at F from stored points only, then append the immutable manifest."""
    now = datetime.now(UTC) if now is None else now
    # Refuse before querying storage, including an empty staging table.
    pin_selection(day, [], now=now, variables=variables)
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT * FROM raw.gfs_points
                          WHERE run_init_time = ANY(%s) AND variable = ANY(%s)''',
                       ([run.init_time for run in candidate_runs(day)], list(variables)))
        selection = pin_selection(day, cursor.fetchall(), now=now, variables=variables)
        cursor.execute(INSERT_DAY, (
            day, selection['selection_hash'], selection['cutoff'], selection['status'],
            selection['run_init_time'], selection['fallback'], selection['source_url'],
            Jsonb(selection['manifest']), datetime.now(UTC),
        ))
    return selection


def replay_points(selection: dict, rows: list[dict]) -> pd.DataFrame:
    """Resolve exactly the pinned identities, ignoring every other stored revision."""
    manifest = selection['manifest']
    if _digest(manifest) != selection['selection_hash']:
        raise ValueError('Pinned weather manifest hash mismatch')
    indexed = {}
    for row in rows:
        indexed.setdefault(tuple(_identity(row)), []).append(row)
    pinned = []
    for identity in manifest['points']:
        versions = indexed.get(tuple(identity), [])
        if len(versions) != 1:
            raise ValueError('Pinned point missing or ambiguous')
        pinned.append(versions[0])
    return pd.DataFrame(pinned, columns=list(rows[0]) if rows else [*IDENTITY, 'valid_time'])


def read_pinned_temperature(
    connection: psycopg.Connection, day: date, selection_hash: str,
) -> pd.DataFrame:
    """Replay TMP using a caller's exact day/manifest identity, including DST slots."""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT * FROM raw.gfs_days
                          WHERE delivery_day = %s AND selection_hash = %s''', (day, selection_hash))
        selection = cursor.fetchone()
        if selection is None:
            raise ValueError('Pinned weather selection missing')
        selection['manifest'] = selection['original_metadata']
        if 'TMP' not in selection['manifest'].get('variables', []):
            raise ValueError('Pinned manifest does not include TMP')
        cursor.execute('SELECT * FROM raw.gfs_points WHERE run_init_time = %s',
                       (selection['run_init_time'],))
        points = replay_points(selection, cursor.fetchall())
    return temperature_day(day, pd.Series(selection), points)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('stage', 'select'))
    parser.add_argument('--day', type=date.fromisoformat, required=True)
    parser.add_argument('--variables', nargs='+', choices=tuple(VARIABLES), default=['TMP'])
    args = parser.parse_args()
    try:
        # The early-selection guard also precedes connection/DDL.
        if args.mode == 'select':
            pin_selection(args.day, [], now=datetime.now(UTC), variables=tuple(args.variables))
        with connect_database() as connection:
            if args.mode == 'stage':
                with connection.transaction():
                    connection.execute(DDL)
                with requests.Session() as session:
                    session.headers['User-Agent'] = 'load-forecast-fr/gfs-collector'
                    result = stage_candidates(connection, session, args.day, tuple(args.variables))
            else:
                result = select_staged(connection, args.day, variables=tuple(args.variables))
        print(json.dumps(result, default=str, sort_keys=True))
    except (psycopg.Error, requests.RequestException, KeyError, ValueError, RuntimeError):
        parser.exit(1, 'Weather production command failed; check cutoff, storage and source.\n')


if __name__ == '__main__':
    main()

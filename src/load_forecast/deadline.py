"""Bounded serving check, launched independently of Prefect and PostgreSQL."""

import argparse
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from load_forecast.cutoff import PARIS
from load_forecast.pipeline_artifacts import write_artifact
from load_forecast.serving import ServingAuthority


def deadline_check(root: Path, day: date, *, clock=None) -> dict:
    """Read prepared files and claim the choice; no forecast or database connection is made."""
    if not root.is_absolute():
        raise ValueError('Runtime directory must be an absolute configured path')
    authority = ServingAuthority(root / 'serving', clock or (lambda: datetime.now(UTC)))
    choice = authority.deadline_check(day)
    if choice['kind'] == 'none':
        logging.getLogger(__name__).error('no_delivery for %s', day)
        write_artifact(root / 'incidents' / f'{day}_no_delivery.pkl',
                       {'event': 'no_delivery', 'day': day, 'choice_hash': choice['choice_hash'],
                        'reason': choice.get('reason', 'Prepared fallback unavailable')})
    return choice


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', type=date.fromisoformat)
    args = parser.parse_args()
    day = args.day or (datetime.now(PARIS).date() + timedelta(days=1))
    try:
        choice = deadline_check(Path(os.environ['LFF_RUNTIME_DIR']), day)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'Deadline check failed: {error}\n')
    print(json.dumps(choice))
    if choice['kind'] == 'none':
        raise SystemExit(2)


if __name__ == '__main__':
    main()

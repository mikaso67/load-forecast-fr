"""Shared MLflow server configuration for logging, reads and health checks."""

import os
from datetime import date

DEFAULT_TRACKING_URI = 'http://127.0.0.1:5000'


def mlflow_tracking_uri() -> str:
    """Read configuration at call time, retaining URL prefixes without trailing slashes."""
    return (os.environ.get('MLFLOW_TRACKING_URI') or DEFAULT_TRACKING_URI).rstrip('/')


def backtest_tags(first: date, last: date, exp: str, ticket: str | None = None) -> dict[str, str]:
    """Build run tags at call time, with an optional tracking ticket."""
    tags = {'author': os.environ.get('LFF_AUTHOR') or 'developer', 'exp': exp,
            'backtest_start': str(first), 'backtest_end': str(last)}
    if ticket is not None:
        tags['ticket'] = ticket
    return tags

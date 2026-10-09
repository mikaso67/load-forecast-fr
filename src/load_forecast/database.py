"""Shared clean-table selection and safe SQL identifier quoting."""

import os

from psycopg import sql


def clean_table() -> str:
    """Return the quoted schema.table configured by LFF_CLEAN_TABLE."""
    parts = os.environ.get('LFF_CLEAN_TABLE', 'analytics.load_30min').split('.')
    if len(parts) != 2 or not all(parts):
        raise ValueError('LFF_CLEAN_TABLE must be schema.table')
    return sql.Identifier(*parts).as_string()


def clean_query(template: str) -> sql.Composed:
    """Insert the configured table into a template; values remain bound parameters."""
    return sql.SQL(template).format(table=sql.SQL(clean_table()))

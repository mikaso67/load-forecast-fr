"""Clean load data loading utilities for EDA and modeling."""

from datetime import UTC, datetime

import pandas as pd
import psycopg
from psycopg import sql

from load_forecast.database import clean_table


def load_clean_load(
    connection: psycopg.Connection,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> pd.DataFrame:
    """Load half-hourly load and forecast series from PostgreSQL into a DataFrame."""
    params: list[datetime] = []
    clauses: list[sql.Composable] = []

    if start_time is not None:
        if start_time.tzinfo is None or start_time.utcoffset() is None:
            raise ValueError("start_time must be timezone aware")
        clauses.append(sql.SQL("AND value_time >= %s"))
        params.append(start_time.astimezone(UTC))

    if end_time is not None:
        if end_time.tzinfo is None or end_time.utcoffset() is None:
            raise ValueError("end_time must be timezone aware")
        clauses.append(sql.SQL("AND value_time < %s"))
        params.append(end_time.astimezone(UTC))

    query_parts = [
        sql.SQL(
            "SELECT value_time, consommation, prevision_j1, prevision_j, source_dataset "
            "FROM {} WHERE 1=1"
        ).format(sql.SQL(clean_table()))
    ]
    query_parts.extend(clauses)
    query_parts.append(sql.SQL("ORDER BY value_time"))
    query = sql.SQL(" ").join(query_parts)

    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]

    df = pd.DataFrame(rows, columns=columns)
    if not df.empty:
        df["value_time"] = pd.to_datetime(df["value_time"], utc=True)
    return df

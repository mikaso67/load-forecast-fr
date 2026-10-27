"""Estimate and apply hourly consumption revision offsets (option B, D21).

I estimate the revision mapping from raw.eco2mix for a given freeze time F.
It computes the mean gap (consolidated minus real-time) by Europe/Paris clock hour,
using only half-hours where both vintages were ingested strictly before F.
A month counts only once all its expected consolidated rows are stored before F.
If no consolidated month is available before F, I return zero offsets and log it.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import psycopg

from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.eco2mix import connect_database
from load_forecast.revision_gap import summarize_by

logger = logging.getLogger(__name__)

PAIRS_QUERY = """
WITH filtered AS (
    SELECT source_dataset, value_time, consommation, ingested_at,
           row_number() OVER (PARTITION BY source_dataset, value_time
                              ORDER BY ingested_at DESC) AS latest
    FROM raw.eco2mix
    WHERE source_dataset IN ('eco2mix-national-tr', 'eco2mix-national-cons-def')
      AND ingested_at < %(freeze)s
      AND extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
      AND original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
), latest AS (
    SELECT * FROM filtered WHERE latest = 1 AND consommation IS NOT NULL
)
SELECT r.value_time,
       r.consommation AS real_time,
       c.consommation AS consolidated
FROM latest r
JOIN latest c USING (value_time)
WHERE r.source_dataset = 'eco2mix-national-tr'
  AND c.source_dataset = 'eco2mix-national-cons-def'
ORDER BY r.value_time;
"""

CONS_COUNTS_QUERY = """
WITH filtered_cons AS (
    SELECT value_time,
           row_number() OVER (PARTITION BY value_time ORDER BY ingested_at DESC) AS latest
    FROM raw.eco2mix
    WHERE source_dataset = 'eco2mix-national-cons-def'
      AND ingested_at < %(freeze)s
      AND extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
      AND original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
      AND consommation IS NOT NULL
)
SELECT to_char(value_time AT TIME ZONE 'Europe/Paris', 'YYYY-MM') AS month_str,
       count(*) AS n_slots
FROM filtered_cons
WHERE latest = 1
GROUP BY 1
ORDER BY 1;
"""


def parse_freeze_timestamp(freeze: datetime | pd.Timestamp | date | str) -> datetime:
    """Normalize a freeze parameter into a timezone-aware UTC datetime."""
    if isinstance(freeze, str):
        freeze = pd.Timestamp(freeze)
    if isinstance(freeze, pd.Timestamp):
        if freeze.tzinfo is None:
            raise ValueError("Freeze timestamp must be timezone-aware")
        return freeze.tz_convert(UTC).to_pydatetime()
    if isinstance(freeze, datetime):
        if freeze.tzinfo is None or freeze.utcoffset() is None:
            raise ValueError("Freeze timestamp must be timezone-aware")
        return freeze.astimezone(UTC)
    if isinstance(freeze, date):
        return forecast_cutoff(freeze)
    raise TypeError(f"Unsupported freeze type: {type(freeze)}")


def expected_month_slots(year: int, month: int) -> int:
    """Return the expected count of 30-minute slots published by the source under D12 for the given month.

    In Europe/Paris, ordinary days have 48 half-hour slots and spring DST days have 46 slots.
    On the autumn DST transition day, the theoretical wall-clock day has 50 slots, but the
    source is missing 00:00 and 00:30 UTC per Protocol D12, so the source publishes 48 slots.
    """
    if not isinstance(year, int) or not isinstance(month, int):
        raise TypeError(f"Year and month must be integers, got {type(year).__name__} and {type(month).__name__}")
    if not (1 <= month <= 12):
        raise ValueError(f"Month must be between 1 and 12, got {month}")
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    current = start
    total = 0
    while current < end:
        day_slots = len(delivery_slots(current, "30min"))
        if day_slots == 50:
            total += day_slots - 2
        else:
            total += day_slots
        current += timedelta(days=1)
    return total


def _parse_month_key(month_str: str) -> tuple[int, int]:
    """Validate and parse a 'YYYY-MM' month string into (year, month)."""
    if not isinstance(month_str, str) or len(month_str) != 7 or month_str[4] != "-":
        raise ValueError(f"Invalid month key format: {month_str!r}, expected 'YYYY-MM'")
    try:
        year = int(month_str[:4])
        month = int(month_str[5:7])
    except ValueError as e:
        raise ValueError(f"Non-integer year or month in key: {month_str!r}") from e
    if not (1 <= month <= 12):
        raise ValueError(f"Month out of range [1, 12] in key: {month_str!r}")
    return year, month


class RevisionMapping(pd.DataFrame):
    """Table of 24 hourly revision offsets with estimation metadata."""

    @property
    def _constructor(self):
        return RevisionMapping

    @property
    def months_used(self) -> list[str]:
        return self.attrs.get("months_used", [])

    @property
    def n_slots(self) -> int:
        return self.attrs.get("n_slots", 0)

    @property
    def slots_per_month(self) -> dict[str, int]:
        return self.attrs.get("slots_per_month", {})

    @property
    def zero_offsets(self) -> bool:
        return self.attrs.get("zero_offsets", False)

    @property
    def message(self) -> str:
        return self.attrs.get("message", "")

    @property
    def offsets(self) -> pd.Series:
        return self["offset"]

    def apply(
        self,
        values: pd.Series | pd.DataFrame,
    ) -> pd.Series | pd.DataFrame:
        """Apply this revision mapping to real-time lag values or consumption history."""
        return apply_revision_mapping(values, self)


def build_zero_revision_mapping(
    message: str = "No consolidated month available before freeze; returning zero offsets.",
) -> RevisionMapping:
    """Return a 24-hour zero-offset table when no eligible consolidated month exists."""
    logger.info(message)
    df = pd.DataFrame(
        {
            "offset": np.zeros(24, dtype=float),
            "n_slots": np.zeros(24, dtype=int),
            "mean": np.zeros(24, dtype=float),
        },
        index=pd.Index(range(24), name="hour"),
    )
    mapping = RevisionMapping(df)
    mapping.attrs["months_used"] = []
    mapping.attrs["n_slots"] = 0
    mapping.attrs["slots_per_month"] = {}
    mapping.attrs["zero_offsets"] = True
    mapping.attrs["message"] = message
    return mapping


def calculate_revision_mapping(
    pairs_df: pd.DataFrame,
    consolidated_counts: dict[str, int],
) -> RevisionMapping:
    """Calculate 24 hourly offsets from paired half-hours and consolidated month counts.

    A month counts only once its consolidated rows are stored before F, meaning its stored
    consolidated half-hours cover every slot published by the source for that month under D12.
    """
    if pairs_df.empty or not consolidated_counts:
        return build_zero_revision_mapping()

    eligible_months: set[str] = set()
    for month_str, count in consolidated_counts.items():
        year, month = _parse_month_key(month_str)
        if not isinstance(count, (int, np.integer)) or count < 0:
            raise ValueError(f"Invalid slot count for month {month_str!r}: {count!r}")
        expected = expected_month_slots(year, month)
        if count >= expected:
            eligible_months.add(month_str)

    if not eligible_months:
        return build_zero_revision_mapping(
            "No consolidated month with full coverage available before freeze; returning zero offsets."
        )

    if not isinstance(pairs_df["value_time"].dtype, pd.DatetimeTZDtype):
        value_time = pd.to_datetime(pairs_df["value_time"], utc=True)
    else:
        value_time = pairs_df["value_time"].dt.tz_convert(UTC)

    paris_time = value_time.dt.tz_convert(PARIS)
    paris_months = paris_time.dt.strftime("%Y-%m")

    mask = paris_months.isin(eligible_months)
    filtered_pairs = pairs_df.loc[mask].copy()

    if filtered_pairs.empty:
        return build_zero_revision_mapping(
            "No paired half-hours in eligible consolidated months; returning zero offsets."
        )

    filtered_time = value_time.loc[mask]
    filtered_gap = (
        filtered_pairs["consolidated"].astype(float)
        - filtered_pairs["real_time"].astype(float)
    )
    filtered_gap.index = pd.DatetimeIndex(filtered_time)

    filtered_paris_months = paris_months.loc[mask]
    slots_per_month = filtered_paris_months.value_counts().to_dict()
    months_used = sorted(slots_per_month.keys())
    total_slots = int(len(filtered_gap))

    by_hour = summarize_by(filtered_gap, "hour")

    rows = []
    for h in range(24):
        if h in by_hour.index:
            offset = float(by_hour.loc[h, "mean"])
            hour_slots = int(by_hour.loc[h, "n_slots"])
        else:
            offset = 0.0
            hour_slots = 0
        rows.append({"hour": h, "offset": offset, "n_slots": hour_slots, "mean": offset})

    df = pd.DataFrame(rows).set_index("hour")
    mapping = RevisionMapping(df)
    mapping.attrs["months_used"] = months_used
    mapping.attrs["n_slots"] = total_slots
    mapping.attrs["slots_per_month"] = slots_per_month
    mapping.attrs["zero_offsets"] = False
    mapping.attrs["message"] = (
        f"Estimated revision mapping from {total_slots} slots across {len(months_used)} months: {', '.join(months_used)}"
    )
    logger.info(mapping.attrs["message"])
    return mapping


def estimate_revision_mapping(
    freeze: datetime | pd.Timestamp | date | str,
    connection: psycopg.Connection | None = None,
) -> RevisionMapping:
    """From raw.eco2mix, estimate 24 hourly revision offsets as-of freeze time F.

    Only half-hours where both vintages were ingested strictly before F are used.
    A month counts only once all its consolidated rows are stored before F.
    If no consolidated month is available before F, I return zero offsets.
    """
    freeze_dt = parse_freeze_timestamp(freeze)

    if connection is not None:
        return _fetch_and_calculate(connection, freeze_dt)

    with connect_database() as conn:
        return _fetch_and_calculate(conn, freeze_dt)


def _fetch_and_calculate(connection: psycopg.Connection, freeze_dt: datetime) -> RevisionMapping:
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor = connection.execute(PAIRS_QUERY, {"freeze": freeze_dt})
        pairs_rows = cursor.fetchall()
        cols = [desc.name for desc in cursor.description]
        pairs_df = pd.DataFrame(pairs_rows, columns=cols)

        cursor = connection.execute(CONS_COUNTS_QUERY, {"freeze": freeze_dt})
        count_rows = cursor.fetchall()
        counts_dict = {row[0]: int(row[1]) for row in count_rows}

    return calculate_revision_mapping(pairs_df, counts_dict)


def _extract_offsets_series(
    mapping: RevisionMapping | pd.DataFrame | pd.Series | dict[int, float],
) -> pd.Series:
    """Normalize input mapping into a pd.Series indexed by hour 0..23."""
    if isinstance(mapping, (RevisionMapping, pd.DataFrame)):
        if "offset" in mapping.columns:
            s = mapping["offset"]
        elif "mean" in mapping.columns:
            s = mapping["mean"]
        else:
            s = mapping.iloc[:, 0]
        return s.reindex(range(24), fill_value=0.0).astype(float)
    if isinstance(mapping, pd.Series):
        return mapping.reindex(range(24), fill_value=0.0).astype(float)
    if isinstance(mapping, dict):
        return pd.Series([float(mapping.get(h, 0.0)) for h in range(24)], index=range(24))
    raise TypeError(f"Unsupported mapping type: {type(mapping)}")


def apply_revision_mapping(
    values: pd.Series | pd.DataFrame,
    mapping: RevisionMapping | pd.DataFrame | pd.Series | dict[int, float],
) -> pd.Series | pd.DataFrame:
    """Apply hourly revision offsets to real-time consumption or lag features.

    If values is a pd.Series with a UTC DatetimeIndex:
        Each timestamp is shifted by the offset of its Europe/Paris clock hour.
    If values is a pd.DataFrame with lag feature columns ('lag_d2', 'lag_d7', 'lag_last'):
        'lag_d2' and 'lag_d7' are shifted by the delivery slot's Europe/Paris clock hour offset,
        and 'lag_last' (the D-1 09:30 Paris slot) is shifted by the hour 9 offset.
        Non-lag columns (such as special day indicators) are preserved unchanged.
    If values is a pd.DataFrame with other columns and a UTC DatetimeIndex:
        Every numeric column is shifted by the Europe/Paris clock hour offset of its index.
    """
    offsets = _extract_offsets_series(mapping)

    if isinstance(values, pd.Series):
        if not isinstance(values.index, pd.DatetimeIndex):
            raise ValueError("Expected a pd.Series with a DatetimeIndex")
        if values.index.tz is None or str(values.index.tz) != "UTC":
            raise ValueError("Expected a UTC-indexed Series")
        local_hours = values.index.tz_convert(PARIS).hour
        hourly_deltas = pd.Series(offsets.loc[local_hours].to_numpy(), index=values.index)
        return values + hourly_deltas

    if isinstance(values, pd.DataFrame):
        mapped = values.copy()
        lag_cols = [c for c in ("lag_d2", "lag_d7", "lag_last") if c in mapped.columns]
        if lag_cols:
            if not isinstance(mapped.index, pd.DatetimeIndex):
                raise ValueError("Expected a DatetimeIndex on the lag features DataFrame")
            local_hours = mapped.index.tz_convert(PARIS).hour
            hourly_deltas = offsets.loc[local_hours].to_numpy()
            if "lag_d2" in mapped.columns:
                mapped["lag_d2"] = mapped["lag_d2"] + hourly_deltas
            if "lag_d7" in mapped.columns:
                mapped["lag_d7"] = mapped["lag_d7"] + hourly_deltas
            if "lag_last" in mapped.columns:
                mapped["lag_last"] = mapped["lag_last"] + float(offsets.loc[9])
            return mapped

        if not isinstance(mapped.index, pd.DatetimeIndex):
            raise ValueError("Expected a DatetimeIndex on the DataFrame")
        local_hours = mapped.index.tz_convert(PARIS).hour
        hourly_deltas = offsets.loc[local_hours].to_numpy()
        for col in mapped.select_dtypes(include=[np.number]).columns:
            mapped[col] = mapped[col] + hourly_deltas
        return mapped

    raise TypeError(f"Unsupported values type: {type(values)}")


def make_lag_mapping_fn(
    freeze: datetime | pd.Timestamp | date | str,
    connection: psycopg.Connection | None = None,
) -> Callable[[pd.Series | pd.DataFrame], pd.Series | pd.DataFrame]:
    """Return an injectable mapping function for a given freeze time."""
    mapping = estimate_revision_mapping(freeze, connection=connection)
    return lambda values: apply_revision_mapping(values, mapping)

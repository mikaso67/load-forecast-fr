"""Forecast boundaries and publication filtering from the approved protocol."""

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PARIS = ZoneInfo("Europe/Paris")


def forecast_cutoff(target_day: date) -> datetime:
    """Return noon on local D-1 as an aware UTC instant."""
    previous_day = target_day - timedelta(days=1)
    return datetime.combine(previous_day, time(12), PARIS).astimezone(UTC)


def delivery_slots(target_day: date, freq: str) -> Sequence[datetime]:
    """Return slot starts between the two local midnights, including DST changes."""
    start = datetime.combine(target_day, time(), PARIS).astimezone(UTC)
    end = datetime.combine(target_day + timedelta(days=1), time(), PARIS).astimezone(UTC)
    return pd.date_range(start, end, freq=freq, inclusive="left").to_pydatetime().tolist()


def available_at(
    df: pd.DataFrame, cutoff: datetime, published_col: str = "published_at"
) -> pd.DataFrame:
    """Keep only rows published strictly before the aware cutoff."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("The cutoff must be timezone-aware")
    published = pd.DatetimeIndex(df[published_col])
    if published.tz is None:
        raise ValueError("Publication timestamps must be timezone-aware")
    return df.loc[published < cutoff.astimezone(UTC)].copy()


def local_to_utc(local_times: Iterable[datetime]) -> Sequence[datetime]:
    """Convert ordered naive Paris labels; infer a repeated hour from its order."""
    labels = pd.DatetimeIndex(list(local_times))
    if labels.tz is not None:
        raise ValueError("Expected naive Europe/Paris labels")
    converted = labels.tz_localize(PARIS, ambiguous="infer", nonexistent="raise")
    return converted.tz_convert(UTC).to_pydatetime().tolist()

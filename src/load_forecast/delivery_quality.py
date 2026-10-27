"""Operational input provenance checks, independent of forecast selection."""

from datetime import date

import pandas as pd

from load_forecast.evaluation import last_usable_slot_start


def degraded_input_reasons(day: date, last_consumption_slot: str | None,
                           backup_used: bool) -> list[str]:
    """Explain backup experts and consumption older than the existing D2 boundary."""
    reasons = ['expert_backup_chain'] if backup_used else []
    last = pd.Timestamp(last_consumption_slot) if last_consumption_slot else pd.NaT
    if pd.isna(last):
        reasons.append('missing_consumption')
    elif last.tzinfo is None:
        raise ValueError('Last consumption slot must be timezone aware')
    elif last < last_usable_slot_start(day):
        reasons.append('stale_consumption')
    return reasons

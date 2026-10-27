"""Evaluation periods: the 2025 test year stays locked unless a caller says it is the final test."""

from datetime import date

VALIDATION_LAST = date(2024, 12, 31)
TEST_FIRST = date(2025, 1, 1)
TEST_LAST = date(2025, 12, 31)


def last_allowed_day(final_test: bool = False) -> date:
    """Latest delivery day a walk-forward or a weather read may reach."""
    return TEST_LAST if final_test else VALIDATION_LAST


def locked_message(final_test: bool = False) -> str:
    return 'before 2026' if final_test else 'before 2025'


# Production period (D20 P3 A): forecasts and monitoring only, never used to choose or tune a model.
# It has its own entry point in production.py, so the experiment lock above stays unchanged.
PRODUCTION_FIRST = date(2026, 1, 1)
# D20 P2 A: 2026-01-01 to 2026-01-04 are declared without forecast (no fit at a 2025 cutoff); the
# first forecast is the Monday 2026-01-05, whose refit is the cold start.
PRODUCTION_FIRST_FORECAST = date(2026, 1, 5)

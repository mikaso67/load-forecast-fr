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

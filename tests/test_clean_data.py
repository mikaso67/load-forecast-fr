"""Unit tests for clean_data utilities."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from load_forecast.clean_data import load_clean_load


def test_load_clean_load_mocked():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    cursor.description = [
        ("value_time",),
        ("consommation",),
        ("prevision_j1",),
        ("prevision_j",),
        ("source_dataset",),
    ]
    sample_dt = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    cursor.fetchall.return_value = [
        (sample_dt, 55000, 54000, 54500, "eco2mix-national-cons-def")
    ]

    df = load_clean_load(
        conn,
        start_time=datetime(2024, 1, 1, tzinfo=UTC),
        end_time=datetime(2024, 1, 2, tzinfo=UTC),
    )

    assert len(df) == 1
    assert df["consommation"].iloc[0] == 55000
    assert df["value_time"].dt.tz is not None
    cursor.execute.assert_called_once()
    query_obj, params = cursor.execute.call_args[0]
    assert len(params) == 2


def test_load_clean_load_rejects_naive_datetimes():
    conn = MagicMock()
    with pytest.raises(ValueError, match="start_time must be timezone aware"):
        load_clean_load(conn, start_time=datetime(2024, 1, 1, 0, 0))

    with pytest.raises(ValueError, match="end_time must be timezone aware"):
        load_clean_load(
            conn,
            start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, 0, 0),
        )


def test_load_clean_load_aware_conversion():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.description = [("value_time",)]
    cursor.fetchall.return_value = []

    paris = ZoneInfo("Europe/Paris")
    start = datetime(2024, 1, 1, 1, 0, tzinfo=paris)  # 00:00 UTC
    end = datetime(2024, 1, 2, 1, 0, tzinfo=paris)    # 00:00 UTC

    load_clean_load(conn, start_time=start, end_time=end)
    cursor.execute.assert_called_once()
    _, params = cursor.execute.call_args[0]
    assert params[0] == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    assert params[1] == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)

"""First usable consumption ingestion, slot ends and Paris date boundaries."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from load_forecast.ingestion_delay import read_delay_report, summarize_delays


def test_distribution_uses_slot_end_and_keeps_catchup_and_negative_delays():
    start, end = date(2024, 3, 30), date(2024, 4, 2)
    rows = []
    for offset, delay in enumerate([-5, 120, 1440]):
        value = datetime.combine(start + timedelta(days=offset), datetime.min.time(),
                                 ZoneInfo('Europe/Paris')).replace(hour=9, minute=30)
        value = value.astimezone(UTC)
        rows.append((value, value + timedelta(minutes=30 + delay)))
    report = summarize_delays(rows, start, end)
    assert report['delay_minutes'] == {'min': -5., 'median': 120., 'p90': 1176.,
                                       'p95': 1308., 'max': 1440.}
    assert report['negative_delay_days'] == report['within_120_minutes_days'] == 1
    assert report['missing_days'] == 0
    assert rows[0][0].hour == 8 and rows[1][0].hour == 7


def test_read_only_query_uses_first_nonnull_real_time_version_and_autumn_bounds():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    report = read_delay_report(db, date(2024, 10, 27), date(2024, 10, 28))
    commands = db.execute.call_args_list
    assert 'READ ONLY' in commands[0].args[0]
    query, bounds = commands[1].args
    assert 'min(ingested_at)' in query
    assert "source_dataset = 'eco2mix-national-tr'" in query
    assert 'consommation IS NOT NULL' in query and '09:30' in query
    assert (bounds[1] - bounds[0]).total_seconds() == 25 * 3600
    assert report['observed_days'] == 0 and report['missing_days'] == 1
    assert all(value is None for value in report['delay_minutes'].values())


def test_invalid_or_duplicate_slots_refused():
    start, end = date(2024, 7, 15), date(2024, 7, 16)
    value = datetime(2024, 7, 15, 7, 30, tzinfo=UTC)
    row = (value, value + timedelta(hours=1))
    with pytest.raises(ValueError, match='one first ingestion'):
        summarize_delays([row, row], start, end)
    with pytest.raises(ValueError, match='aware'):
        summarize_delays([(value.replace(tzinfo=None), value)], start, end)
    with pytest.raises(ValueError, match='09:30'):
        summarize_delays([(value - timedelta(hours=1), value)], start, end)
    with pytest.raises(ValueError, match='increasing'):
        summarize_delays([], end, start)


def test_elapsed_delay_crossing_dst_uses_utc_not_wall_clock():
    paris = ZoneInfo('Europe/Paris')
    value = datetime(2024, 3, 30, 9, 30, tzinfo=paris)
    ingestion = datetime(2024, 3, 31, 10, 0, tzinfo=paris)
    report = summarize_delays([(value, ingestion)], date(2024, 3, 30), date(2024, 4, 1))
    assert report['delay_minutes']['min'] == 23 * 60
    assert report['daily'][0]['value_time'].endswith('+00:00')

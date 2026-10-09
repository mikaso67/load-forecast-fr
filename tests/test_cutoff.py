"""Forecast-time leakage tests. Specification in docs/leakage_tests.md.

The tests target `load_forecast.cutoff`, which does not exist yet. While it is missing, every test is
an expected failure. Once the module exists the marker turns off and the tests run for real.
"""

import importlib
import importlib.util
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

MODULE = "load_forecast.cutoff"


def _module_missing() -> bool:
    try:
        return importlib.util.find_spec(MODULE) is None
    except ModuleNotFoundError:
        return True


pytestmark = pytest.mark.xfail(
    _module_missing(),
    reason=f"{MODULE} is not implemented yet",
    raises=ModuleNotFoundError,
    strict=True,
)


def _cutoff_module():
    return importlib.import_module(MODULE)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# 1. Cutoff at 12:00 Europe/Paris on D-1


@pytest.mark.parametrize(
    ("target_day", "expected"),
    [
        (date(2025, 1, 15), _utc(2025, 1, 14, 11)),
        (date(2025, 7, 15), _utc(2025, 7, 14, 10)),
        (date(2025, 3, 30), _utc(2025, 3, 29, 11)),
        (date(2025, 3, 31), _utc(2025, 3, 30, 10)),
        (date(2025, 10, 26), _utc(2025, 10, 25, 10)),
        (date(2025, 10, 27), _utc(2025, 10, 26, 11)),
    ],
)
def test_cutoff_is_noon_paris_on_previous_local_day(target_day, expected):
    cutoff = _cutoff_module().forecast_cutoff(target_day)
    assert cutoff == expected


def test_cutoff_is_timezone_aware_utc():
    cutoff = _cutoff_module().forecast_cutoff(date(2025, 1, 15))
    assert cutoff.utcoffset() == timedelta(0)


# 2. Delivery day slots on 23-hour and 25-hour days


@pytest.mark.parametrize(
    ("target_day", "hours"),
    [(date(2025, 1, 15), 24), (date(2025, 3, 30), 23), (date(2025, 10, 26), 25)],
)
@pytest.mark.parametrize(("freq", "slots_per_hour"), [("15min", 4), ("30min", 2), ("1h", 1)])
def test_delivery_slots_count(target_day, hours, freq, slots_per_hour):
    slots = list(_cutoff_module().delivery_slots(target_day, freq))
    assert len(slots) == hours * slots_per_hour


@pytest.mark.parametrize(
    ("target_day", "first", "end"),
    [
        (date(2025, 1, 15), _utc(2025, 1, 14, 23), _utc(2025, 1, 15, 23)),
        (date(2025, 3, 30), _utc(2025, 3, 29, 23), _utc(2025, 3, 30, 22)),
        (date(2025, 10, 26), _utc(2025, 10, 25, 22), _utc(2025, 10, 26, 23)),
    ],
)
def test_delivery_slots_span_local_midnight_to_midnight(target_day, first, end):
    slots = list(_cutoff_module().delivery_slots(target_day, "30min"))
    steps = {b - a for a, b in zip(slots, slots[1:])}
    assert slots[0] == first
    assert slots[-1] + timedelta(minutes=30) == end
    assert steps == {timedelta(minutes=30)}
    assert all(s.utcoffset() == timedelta(0) for s in slots)


# 3. Values published after the cutoff are excluded


def _published_rows(cutoff: datetime) -> pd.DataFrame:
    second = timedelta(seconds=1)
    return pd.DataFrame(
        {
            "ts": [cutoff - timedelta(hours=3), cutoff - timedelta(hours=2), cutoff - timedelta(hours=1)],
            "published_at": [cutoff - second, cutoff, cutoff + second],
            "value": [1.0, 2.0, 3.0],
        }
    )


def test_available_at_keeps_only_rows_published_strictly_before_cutoff():
    cutoff = _utc(2025, 1, 14, 11)
    kept = _cutoff_module().available_at(_published_rows(cutoff), cutoff)
    assert kept["value"].tolist() == [1.0]


def test_available_at_filters_on_publication_time_not_value_time():
    cutoff = _utc(2025, 1, 14, 11)
    rows = pd.DataFrame(
        {
            "ts": [cutoff - timedelta(days=2)],
            "published_at": [cutoff + timedelta(hours=1)],
            "value": [1.0],
        }
    )
    kept = _cutoff_module().available_at(rows, cutoff)
    assert kept.empty


def test_available_at_rejects_naive_cutoff():
    rows = _published_rows(_utc(2025, 1, 14, 11))
    with pytest.raises(ValueError):
        _cutoff_module().available_at(rows, datetime(2025, 1, 14, 12))


# 4. UTC storage: local labels converted once, without loss or duplicates


def _local_labels(day: date, skip_hour: int | None = None, repeat_hour: int | None = None) -> list[datetime]:
    labels = []
    for hour in range(24):
        if hour == skip_hour:
            continue
        times = 2 if hour == repeat_hour else 1
        for _ in range(times):
            labels += [datetime(day.year, day.month, day.day, hour, m) for m in (0, 30)]
    return labels


def test_local_to_utc_fall_back_day_keeps_repeated_hour():
    labels = _local_labels(date(2025, 10, 26), repeat_hour=2)
    utc = list(_cutoff_module().local_to_utc(labels))
    assert len(utc) == 50
    assert len(set(utc)) == 50
    assert utc == sorted(utc)
    assert utc[0] == _utc(2025, 10, 25, 22)
    assert utc[4] == _utc(2025, 10, 26, 0)
    assert utc[6] == _utc(2025, 10, 26, 1)


def test_local_to_utc_spring_forward_day_has_46_slots():
    labels = _local_labels(date(2025, 3, 30), skip_hour=2)
    utc = list(_cutoff_module().local_to_utc(labels))
    steps = {b - a for a, b in zip(utc, utc[1:])}
    assert len(utc) == 46
    assert steps == {timedelta(minutes=30)}
    assert utc[0] == _utc(2025, 3, 29, 23)


def test_local_to_utc_rejects_nonexistent_local_time():
    with pytest.raises(ValueError):
        _cutoff_module().local_to_utc([datetime(2025, 3, 30, 2, 30)])


def test_local_to_utc_returns_utc_aware_timestamps():
    utc = list(_cutoff_module().local_to_utc([datetime(2025, 7, 1, 12, 0)]))
    assert utc == [_utc(2025, 7, 1, 10)]
    assert utc[0].utcoffset() == timedelta(0)

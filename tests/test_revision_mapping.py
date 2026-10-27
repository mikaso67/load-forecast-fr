"""Tests for hourly revision mapping estimation and application (option B, D21)."""

import os
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from load_forecast.cutoff import PARIS, delivery_slots
from load_forecast.eco2mix import connect_database
from load_forecast.lag_model import LAG_COLUMNS, lag_features
from load_forecast.revision_mapping import (
    RevisionMapping,
    apply_revision_mapping,
    build_zero_revision_mapping,
    calculate_revision_mapping,
    estimate_revision_mapping,
    expected_month_slots,
    make_lag_mapping_fn,
    parse_freeze_timestamp,
)

EXPECTED_SECTION_4_PROFILE = {
    0: 1485.6,
    1: 1032.5,
    2: 1220.0,
    3: 855.3,
    4: 546.0,
    5: 211.5,
    6: -90.7,
    7: 11.8,
    8: 275.5,
    9: 873.7,
    10: 1244.3,
    11: 1438.1,
    12: 1612.5,
    13: 2444.3,
    14: 1873.7,
    15: 2131.9,
    16: 1796.4,
    17: 1289.0,
    18: 617.2,
    19: 461.8,
    20: 895.9,
    21: 593.5,
    22: 495.1,
    23: 1212.8,
}


def database_available() -> bool:
    if "POSTGRES_USER" not in os.environ:
        return False
    try:
        with connect_database() as conn:
            conn.execute("SELECT 1")
            return True
    except Exception:
        return False


def test_parse_freeze_timestamp():
    dt = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert parse_freeze_timestamp(dt) == dt
    ts = pd.Timestamp("2026-10-09 14:00:00+02:00")
    assert parse_freeze_timestamp(ts) == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert parse_freeze_timestamp("2026-10-09 12:00:00+00:00") == dt
    assert parse_freeze_timestamp(date(2026, 10, 10)) == datetime(2026, 10, 9, 10, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        parse_freeze_timestamp(datetime(2026, 10, 9, 12, 0))
    with pytest.raises(ValueError):
        parse_freeze_timestamp(pd.Timestamp("2026-10-09 12:00:00"))


def test_expected_month_slots_handles_dst_and_regular_months():
    # July (31 days, regular): 31 * 48 = 1488
    assert expected_month_slots(2026, 7) == 1488
    # August (31 days, regular): 31 * 48 = 1488
    assert expected_month_slots(2026, 8) == 1488
    # September (30 days, regular): 30 * 48 = 1440
    assert expected_month_slots(2026, 9) == 1440
    # October 2026 (autumn DST change with 48 source slots under D12): 30 * 48 + 48 = 1488
    assert expected_month_slots(2026, 10) == 1488
    # October 2024 (autumn DST change with 48 source slots under D12): 30 * 48 + 48 = 1488
    assert expected_month_slots(2024, 10) == 1488
    # March 2026 (spring DST change on 2026-03-29 with 46 slots): 30 * 48 + 46 = 1486
    assert expected_month_slots(2026, 3) == 1486
    # February 2026 (non-leap, 28 days): 28 * 48 = 1344
    assert expected_month_slots(2026, 2) == 1344
    # February 2024 (leap year, 29 days): 29 * 48 = 1392
    assert expected_month_slots(2024, 2) == 1392

    with pytest.raises(ValueError):
        expected_month_slots(2026, 0)
    with pytest.raises(ValueError):
        expected_month_slots(2026, 13)
    with pytest.raises(TypeError):
        expected_month_slots("2026", 10)  # type: ignore


def test_calculate_revision_mapping_invalid_inputs_propagate_or_raise():
    pairs_df = pd.DataFrame({
        "value_time": [datetime(2026, 7, 1, 0, 0, tzinfo=UTC)],
        "real_time": [40000.0],
        "consolidated": [41000.0],
    })
    # Malformed month format
    with pytest.raises(ValueError, match="Invalid month key format"):
        calculate_revision_mapping(pairs_df, {"invalid-format": 1488})

    # Month out of range
    with pytest.raises(ValueError, match="Month out of range"):
        calculate_revision_mapping(pairs_df, {"2026-13": 1488})

    # Negative slot count
    with pytest.raises(ValueError, match="Invalid slot count"):
        calculate_revision_mapping(pairs_df, {"2026-07": -1})

    # Non-integer slot count
    with pytest.raises(ValueError, match="Invalid slot count"):
        calculate_revision_mapping(pairs_df, {"2026-07": "1488"})  # type: ignore


def test_autumn_month_eligibility_under_d12():
    # October 2024 has 31 days with autumn clock change on 2024-10-27.
    # Under D12, 00:00 and 00:30 UTC are absent from the source, so the source
    # publishes 48 slots instead of 50 on the change day, yielding 1488 total slots.
    assert expected_month_slots(2024, 10) == 1488

    # Generate 1488 half-hours for October 2024 matching source slots under D12
    all_slots = []
    for d in (date(2024, 10, 1) + timedelta(days=i) for i in range(31)):
        day_slots = delivery_slots(d, "30min")
        if len(day_slots) == 50:
            # 00:00 and 00:30 UTC absent from source under D12
            day_slots = [s for s in day_slots if not (s.hour == 0 and s.minute in (0, 30))]
        all_slots.extend(day_slots)

    assert len(all_slots) == 1488
    hours = [s.astimezone(PARIS).hour for s in all_slots]

    pairs_df = pd.DataFrame({
        "value_time": all_slots,
        "real_time": [45000.0] * 1488,
        "consolidated": [45000.0 + h * 5.0 for h in hours],
    })

    # When consolidated count matches the 1488 slots published by the source,
    # the autumn month must be eligible and yield non-zero offsets
    mapping = calculate_revision_mapping(pairs_df, {"2024-10": 1488})
    assert mapping.zero_offsets is False
    assert mapping.n_slots == 1488
    assert mapping.months_used == ["2024-10"]
    assert mapping.slots_per_month == {"2024-10": 1488}
    for h in range(24):
        assert mapping.loc[h, "offset"] == pytest.approx(h * 5.0)
        assert mapping.loc[h, "n_slots"] == 62


def test_calculate_revision_mapping_synthetic_complete_and_incomplete():
    # Generate 1488 half-hours for July 2026
    start_utc = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
    slots = [start_utc + timedelta(minutes=30 * i) for i in range(1488)]
    hours = [t.astimezone(PARIS).hour for t in slots]
    # Gaps equal to hour * 10
    rt = [40000.0] * 1488
    cons = [40000.0 + h * 10.0 for h in hours]
    pairs_df = pd.DataFrame({
        "value_time": slots,
        "real_time": rt,
        "consolidated": cons,
    })

    # Case A: July count is incomplete (only 1000 stored)
    incomplete_counts = {"2026-07": 1000}
    mapping_incomplete = calculate_revision_mapping(pairs_df, incomplete_counts)
    assert mapping_incomplete.zero_offsets is True
    assert mapping_incomplete.n_slots == 0
    assert mapping_incomplete.months_used == []
    assert (mapping_incomplete["offset"] == 0.0).all()

    # Case B: July count is complete (1488 stored)
    complete_counts = {"2026-07": 1488}
    mapping_complete = calculate_revision_mapping(pairs_df, complete_counts)
    assert mapping_complete.zero_offsets is False
    assert mapping_complete.n_slots == 1488
    assert mapping_complete.months_used == ["2026-07"]
    assert mapping_complete.slots_per_month == {"2026-07": 1488}
    for h in range(24):
        assert mapping_complete.loc[h, "offset"] == pytest.approx(h * 10.0)
        assert mapping_complete.loc[h, "n_slots"] == 62


def test_dst_autumn_change_day_mapping():
    # Autumn change day: 2026-10-25 (50 slots)
    day = date(2026, 10, 25)
    slots = pd.DatetimeIndex(delivery_slots(day, "30min"))
    assert len(slots) == 50

    # Constant offset table: hour 2 has offset 1200.0, other hours 500.0
    offsets_df = pd.DataFrame({
        "offset": [1200.0 if h == 2 else 500.0 for h in range(24)],
        "n_slots": [10] * 24,
    }, index=pd.Index(range(24), name="hour"))
    mapping = RevisionMapping(offsets_df)

    values = pd.Series(40000.0, index=slots)
    mapped = apply_revision_mapping(values, mapping)

    # 4 slots in hour 2: two in CEST (02:00, 02:30), two in CET (02:00, 02:30)
    hour_2_slots = slots[slots.tz_convert(PARIS).hour == 2]
    assert len(hour_2_slots) == 4
    for s in hour_2_slots:
        assert mapped.loc[s] == 41200.0

    other_slots = slots[slots.tz_convert(PARIS).hour != 2]
    for s in other_slots:
        assert mapped.loc[s] == 40500.0


def test_dst_spring_change_day_mapping():
    # Spring change day: 2026-03-29 (46 slots)
    day = date(2026, 3, 29)
    slots = pd.DatetimeIndex(delivery_slots(day, "30min"))
    assert len(slots) == 46

    # Verify hour 2 does not appear on spring change day
    assert 2 not in slots.tz_convert(PARIS).hour

    offsets_df = pd.DataFrame({
        "offset": [100.0 * h for h in range(24)],
        "n_slots": [10] * 24,
    }, index=pd.Index(range(24), name="hour"))
    mapping = RevisionMapping(offsets_df)

    values = pd.Series(50000.0, index=slots)
    mapped = apply_revision_mapping(values, mapping)
    assert len(mapped) == 46
    for s in slots:
        local_hour = s.astimezone(PARIS).hour
        assert mapped.loc[s] == pytest.approx(50000.0 + 100.0 * local_hour)


def test_apply_revision_mapping_to_lag_dataframe():
    delivery_day = date(2026, 7, 15)
    slots = pd.DatetimeIndex(delivery_slots(delivery_day, "30min"))
    assert len(slots) == 48

    # Create synthetic history covering D-8 to D-1
    history_slots = pd.date_range("2026-07-06 00:00", "2026-07-15 00:00", freq="30min", tz="UTC")
    history = pd.Series(35000.0, index=history_slots)

    # Build lag features DataFrame
    features = lag_features(slots, history, delivery_day)
    assert list(features.columns) == ["lag_d2", "lag_d7", "lag_last", "special_d2", "special_d7", "special_last"]

    offsets_dict = {h: 100.0 * h for h in range(24)}
    mapping = RevisionMapping(pd.DataFrame({"offset": offsets_dict}))

    # Map the lag DataFrame directly
    mapped_df = apply_revision_mapping(features, mapping)

    # Compare against mapping history first
    mapped_history = apply_revision_mapping(history, mapping)
    expected_features = lag_features(slots, mapped_history, delivery_day)

    for col in LAG_COLUMNS:
        pd.testing.assert_series_equal(mapped_df[col], expected_features[col], check_exact=False, rtol=1e-5)

    # Special indicators must be unchanged
    for col in ["special_d2", "special_d7", "special_last"]:
        pd.testing.assert_series_equal(mapped_df[col], features[col])

    # Method and callable interfaces
    mapped_via_method = mapping.apply(features)
    pd.testing.assert_frame_equal(mapped_df, mapped_via_method)


def test_zero_mapping_is_identity():
    zero_mapping = build_zero_revision_mapping()
    assert zero_mapping.zero_offsets is True

    slots = pd.date_range("2026-07-15 00:00", "2026-07-15 23:30", freq="30min", tz="UTC")
    series = pd.Series(42000.0, index=slots)
    mapped_series = apply_revision_mapping(series, zero_mapping)
    pd.testing.assert_series_equal(series, mapped_series)

    df = pd.DataFrame({"lag_d2": 42000.0, "lag_d7": 41000.0, "lag_last": 40000.0, "special_d2": 0.0}, index=slots)
    mapped_df = apply_revision_mapping(df, zero_mapping)
    pd.testing.assert_frame_equal(df, mapped_df)


@pytest.mark.skipif(not database_available(), reason="Requires database connection to raw.eco2mix")
def test_july_august_2026_offsets_match_docs_after_2026_10_09():
    freeze = pd.Timestamp("2026-10-10 12:00:00", tz="UTC")
    mapping = estimate_revision_mapping(freeze)

    assert mapping.zero_offsets is False
    assert mapping.months_used == ["2026-07", "2026-08"]
    assert mapping.n_slots == 2976
    assert mapping.slots_per_month == {"2026-07": 1488, "2026-08": 1488}
    assert len(mapping) == 24

    for hour, expected_offset in EXPECTED_SECTION_4_PROFILE.items():
        actual_offset = mapping.loc[hour, "offset"]
        assert actual_offset == pytest.approx(expected_offset, abs=0.1), (
            f"Hour {hour} offset {actual_offset:.2f} differs from doc {expected_offset:.2f}"
        )


@pytest.mark.skipif(not database_available(), reason="Requires database connection to raw.eco2mix")
def test_zero_offsets_before_2026_10_09():
    freeze = pd.Timestamp("2026-10-09 12:00:00", tz="UTC")
    mapping = estimate_revision_mapping(freeze)

    assert mapping.zero_offsets is True
    assert mapping.months_used == []
    assert mapping.n_slots == 0
    assert (mapping["offset"] == 0.0).all()
    assert "zero offsets" in mapping.message.lower()


@pytest.mark.skipif(not database_available(), reason="Requires database connection to raw.eco2mix")
def test_row_ingested_at_or_after_freeze_is_ignored_database():
    # Consolidated July 2026 was ingested at 2026-10-09 13:34:01.278332 UTC.
    # Consolidated August 2026 was ingested at 2026-10-09 13:34:02.426555 UTC.
    # Setting freeze between the two ingests should include July and ignore August.
    freeze_between = datetime(2026, 10, 9, 13, 34, 1, 500000, tzinfo=UTC)
    mapping = estimate_revision_mapping(freeze_between)

    assert mapping.zero_offsets is False
    assert mapping.months_used == ["2026-07"]
    assert mapping.n_slots == 1488
    assert mapping.slots_per_month == {"2026-07": 1488}
    assert "2026-08" not in mapping.months_used


@pytest.mark.skipif(not database_available(), reason="Requires database connection to raw.eco2mix")
def test_make_lag_mapping_fn_matches_direct_mapping():
    freeze = pd.Timestamp("2026-10-10 12:00:00", tz="UTC")
    fn = make_lag_mapping_fn(freeze)
    mapping = estimate_revision_mapping(freeze)

    slots = pd.date_range("2026-07-15 00:00", "2026-07-15 23:30", freq="30min", tz="UTC")
    series = pd.Series(42000.0, index=slots)

    res_fn = fn(series)
    res_direct = apply_revision_mapping(series, mapping)
    pd.testing.assert_series_equal(res_fn, res_direct)

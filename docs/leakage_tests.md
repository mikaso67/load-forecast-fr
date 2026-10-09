# Leakage tests

Specification of the forecast-time tests in `tests/test_cutoff.py`. Rules come from
`docs/protocol.md` sections 1, 2 and 7.

## Expected interface

The tests import `load_forecast.cutoff` (package in `src/load_forecast/`). While it does not exist,
every test is reported as xfail. When it exists, the xfail marker turns off and the tests must pass.

| Function | Contract |
|----------|----------|
| `forecast_cutoff(target_day: date) -> datetime` | 12:00 Europe/Paris on the local day before `target_day`, returned as a timezone-aware UTC datetime |
| `delivery_slots(target_day: date, freq: str) -> Sequence[datetime]` | UTC start times of every slot of the local day, from local midnight to the next local midnight |
| `available_at(df, cutoff, published_col="published_at") -> DataFrame` | Rows whose publication time is strictly before `cutoff`; raises `ValueError` if `cutoff` is naive |
| `local_to_utc(local_times) -> Sequence[datetime]` | Converts naive Europe/Paris labels in time order to UTC; a repeated October hour is resolved by order; a March time that does not exist raises `ValueError` |

A reference implementation of about 20 lines with pandas passes all 26 tests. Two deliberate bugs
(`<=` instead of `<`, UTC offset taken from D instead of D-1) each make tests fail.

## Tests

### 1. Cutoff at 12:00 Europe/Paris on D-1

`test_cutoff_is_noon_paris_on_previous_local_day`
Checks the UTC cutoff for a winter day, a summer day, and the days around both DST changes.
On D = 2025-03-30 the cutoff is 11:00 UTC, on D = 2025-03-31 it is 10:00 UTC: the offset of D-1 counts, not that of D.

`test_cutoff_is_timezone_aware_utc`
Checks the cutoff carries an explicit UTC offset of zero.
A naive datetime would be compared with UTC data as if it were local time, shifting the cutoff by one or two hours.

### 2. Daylight saving days (23 and 25 hours)

`test_delivery_slots_count`
Checks that a delivery day has 24, 23 or 25 hours of slots at 15 min, 30 min and 1 hour.
A day built as "24 hours from midnight UTC" silently drops or duplicates an hour twice a year.

`test_delivery_slots_span_local_midnight_to_midnight`
Checks the first slot starts at local midnight, the last ends at the next local midnight, with a constant step.
This is what makes the model and RTE D-1 scored on the same slots on DST days.

### 3. Values published after the cutoff are excluded

`test_available_at_keeps_only_rows_published_strictly_before_cutoff`
Rows published one second before, exactly at, and one second after the cutoff: only the first is kept.
The protocol says "published before" the cutoff, so the boundary is strict.

`test_available_at_filters_on_publication_time_not_value_time`
A value about two days ago but published after the cutoff is excluded.
Filtering on the value timestamp instead of the publication time is the most common leak with revised or late data.

`test_available_at_rejects_naive_cutoff`
A cutoff without a timezone raises `ValueError` instead of being guessed.
Guessing the timezone would hide the bug instead of failing loudly.

### 4. UTC storage

`test_local_to_utc_fall_back_day_keeps_repeated_hour`
On 2025-10-26 the local hour 02:00 appears twice; the 50 half-hours become 50 distinct, ordered UTC times.
Dropping duplicates on local time would lose an hour of real consumption every October.

`test_local_to_utc_spring_forward_day_has_46_slots`
On 2025-03-30 the local hour 02:00 does not exist; the 46 half-hours map to UTC with a constant 30 min step.
A gap or a doubled slot here would corrupt lags that are computed by position.

`test_local_to_utc_rejects_nonexistent_local_time`
A label 02:30 on 2025-03-30 cannot exist in Paris and raises `ValueError`.
Such a label means the source or a parser is broken, and shifting it silently would hide that.

`test_local_to_utc_returns_utc_aware_timestamps`
12:00 Paris on 2025-07-01 is stored as 10:00 UTC, with an explicit offset of zero.
Everything stored and joined is UTC, so one conversion at ingestion is the only place local time can go wrong.

## Not covered yet

- The last usable consumption slot at the cutoff (protocol D2): added once D2 is decided.
- The truncation test (features from the full database equal features from a copy truncated at the
  cutoff): added once the feature pipeline exists.
- The database column type (`timestamptz`) of the raw tables: belongs to the collector tests.

# Clean Layer: Half-Hourly Load and RTE Forecasts (`analytics.load_30min`)

This document describes the clean data pipeline built with dbt from the raw eCO2mix table `raw.eco2mix`.
The pipeline consists of an intermediate view `int_load_30min_filtered` and a clean table `load_30min`.
I build the production relation `analytics.load_30min` with `--target prod`.
Development targets write into their own schemas; I select their relation for Python
with `LFF_CLEAN_TABLE`. Its default is `analytics.load_30min`.

For a production delivery day, I call `build_clean_snapshot` from
`load_forecast.clean_snapshot` after loading the environment:

```python
from datetime import date
from load_forecast.clean_snapshot import build_clean_snapshot

table = build_clean_snapshot(date(2026, 10, 9), schema="dev")
```

I supply my writable development schema explicitly. The helper refuses before
the day's freeze, computes that freeze in UTC, and runs dbt with `ingested_before`.
This strict `ingested_at < F` filter applies before D12 and source selection, so a
consolidated revision arriving at or after F cannot replace a known real-time row.
Without the variable, the clean model keeps its existing output.

The helper materializes `load_30min_snapshot_YYYYMMDD` in that schema. It inlines
the intermediate model for this run and preserves the ordinary clean relations.
After dbt succeeds, it sets `LFF_CLEAN_TABLE` in the calling Python process and
returns the relation name. A failed run leaves that setting unchanged. Repeating
the same day rebuilds the same dated table from the same freeze. I run models
with `dbt run`, because the existing full-history dbt tests assume complete days
and inspect raw revisions without an ingestion boundary. Feature builders still
apply D2 separately. This helper does not reconstruct pre-archive availability.

---

## 1. Model Specification and Business Rules

The pipeline transforms raw half-hourly and quarter-hourly eCO2mix observations into a validated,
consistent time series suitable for model training, evaluation, and production inference.

Key rules applied:

1. **30-Minute Sampling Grid (Protocol D1):**
   Only rows where the UTC minute is `00` or `30` are retained (`extract(minute FROM value_time) IN (0, 30)`).
   Quarter-hour timestamps (minutes `15` and `45`) are excluded without interpolation.

2. **Delivery Day Coverage from 2021-03-23:**
   eCO2mix ingestion covers French delivery days from `2021-03-23` onwards. Because French delivery days
   run from 00:00 to 24:00 Europe/Paris time, the delivery day `2021-03-23` begins at `2021-03-22 23:00:00 UTC`
   (winter time UTC+1). All records satisfying `(value_time AT TIME ZONE 'Europe/Paris')::date >= '2021-03-23'`
   are included.

3. **Spring DST Artefact Removal (Protocol D12):**
   On the 23-hour spring transition Sunday (last Sunday of March), the ODRE source contains duplicated
   records at 01:00 and 01:30 UTC labeled with phantom local hour `02:xx` (a wall time that does not exist in
   Europe/Paris) alongside valid records labeled `03:xx`.
   The intermediate model `int_load_30min_filtered` drops these `02:xx` phantom rows before deduplication
   (`original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')`).
   This intermediate guard ensures deterministic filtering independent of deduplication ordering.
   Each spring transition day contains exactly 46 half-hour slots.

4. **Source Precedence and Deterministic Tie-Break:**
   When multiple datasets provide records for the same timestamp, the consolidated/definitive dataset
   (`eco2mix-national-cons-def`) takes priority over the real-time dataset (`eco2mix-national-tr`).
   In `load_30min`, `ROW_NUMBER()` orders by source precedence, with `ingested_at DESC` as a deterministic
   tie-break for identical priority. The selected source is stored in `source_dataset`.

5. **Autumn DST Retention (Protocol D12):**
   On the 25-hour autumn transition Sunday (last Sunday of October), the 4 missing quarter-hours (00:00 to 00:45 UTC,
   or 2 half-hours 00:00 and 00:30 UTC) are not imputed. The existing records are preserved as given at their source
   timestamps. Each autumn transition day contains 48 evaluated slots (missing slots are skipped during scoring).

---

## 2. Row Counts per Year

### 2.1 Europe/Paris Calendar Years

Query used: [Query 1](#query-1-row-counts-by-paris-calendar-year).

| Paris year | Source dataset | Rows | Consumption filled | Cons. (%) | RTE D-1 filled | D-1 (%) | RTE D filled | D (%) |
|---|---|---|---|---|---|---|---|---|
| 2021 | eco2mix-national-cons-def | 13,630 | 13,630 | 100.000 | 13,630 | 100.000 | 13,630 | 100.000 |
| 2022 | eco2mix-national-cons-def | 17,518 | 17,518 | 100.000 | 17,518 | 100.000 | 17,518 | 100.000 |
| 2023 | eco2mix-national-cons-def | 17,518 | 17,518 | 100.000 | 17,518 | 100.000 | 17,518 | 100.000 |
| 2024 | eco2mix-national-cons-def | 17,566 | 17,566 | 100.000 | 17,566 | 100.000 | 17,566 | 100.000 |
| 2025 | eco2mix-national-cons-def | 17,518 | 17,518 | 100.000 | 17,518 | 100.000 | 17,518 | 100.000 |
| 2026 | eco2mix-national-cons-def | 8,686 | 8,686 | 100.000 | 8,686 | 100.000 | 8,686 | 100.000 |
| 2026 | eco2mix-national-tr | 4,560 | 4,558 | 99.956 | 4,560 | 100.000 | 4,560 | 100.000 |
| **Total** | | **96,996** | **96,994** | **99.998** | **96,996** | **100.000** | **96,996** | **100.000** |

Yearly total observations:
- 2021 (partial, 2021-03-23 to 2021-12-31): 13,630 rows (matches expected 284 days minus 2 spring DST slots).
- 2022 (ordinary year, 365 days): 17,518 rows (expected 17,520 minus 2 spring DST slots).
- 2023 (ordinary year, 365 days): 17,518 rows (expected 17,520 minus 2 spring DST slots).
- 2024 (leap year, 366 days): 17,566 rows (expected 17,568 minus 2 spring DST slots).
- 2025 (ordinary year, 365 days): 17,518 rows (expected 17,520 minus 2 spring DST slots).
- 2026 (partial, through 2026-10-03): 13,246 rows (8,686 cons-def + 4,560 tr).

Note on missing consumption: Exactly 2 records in 2026 real-time data have null `consommation`:
`2026-10-03 21:00:00+00` and `2026-10-03 21:30:00+00`. These represent the most recently published real-time
slots at the end of the collection window, where RTE forecast values were available before observed consumption
was consolidated.

### 2.2 UTC Calendar Years

Query used: [Query 2](#query-2-row-counts-by-utc-calendar-year).

| UTC year | Rows | Consumption filled | RTE D-1 filled | RTE D filled | First UTC timestamp | Last UTC timestamp |
|---|---|---|---|---|---|---|
| 2021 | 13,632 | 13,632 | 13,632 | 13,632 | 2021-03-22 23:00:00+00 | 2021-12-31 23:30:00+00 |
| 2022 | 17,518 | 17,518 | 17,518 | 17,518 | 2022-01-01 00:00:00+00 | 2022-12-31 23:30:00+00 |
| 2023 | 17,518 | 17,518 | 17,518 | 17,518 | 2023-01-01 00:00:00+00 | 2023-12-31 23:30:00+00 |
| 2024 | 17,566 | 17,566 | 17,566 | 17,566 | 2024-01-01 00:00:00+00 | 2024-12-31 23:30:00+00 |
| 2025 | 17,518 | 17,518 | 17,518 | 17,518 | 2025-01-01 00:00:00+00 | 2025-12-31 23:30:00+00 |
| 2026 | 13,244 | 13,242 | 13,244 | 13,244 | 2026-01-01 00:00:00+00 | 2026-10-03 21:30:00+00 |

Note: The first two timestamps of 2021 (`2021-03-22 23:00` and `23:30` UTC) belong to UTC date 2021-03-22,
which corresponds to local midnight of French delivery day `2021-03-23`.

---

## 3. Excluded Timestamps

### 3.1 Spring DST 02:xx Phantom Artefacts (12 Half-Hour Records)

Query used: [Query 3](#query-3-excluded-spring-dst-artefacts).

The following 12 half-hour records (2 per year across 2021-2026) were dropped because they represent
impossible local wall times (`02:00` and `02:30`) emitted at UTC times corresponding to `03:00` and `03:30` CEST:

| Excluded UTC timestamp | Source dataset | Local date | Phantom local heure | Consommation (MW) | Prevision J-1 (MW) | Prevision J (MW) |
|---|---|---|---|---|---|---|
| 2021-03-28 01:00:00+00 | eco2mix-national-cons-def | 2021-03-28 | 02:00 | 51,860 | 51,500 | 51,900 |
| 2021-03-28 01:30:00+00 | eco2mix-national-cons-def | 2021-03-28 | 02:30 | 52,090 | 51,500 | 51,900 |
| 2022-03-27 01:00:00+00 | eco2mix-national-cons-def | 2022-03-27 | 02:00 | 46,112 | 44,700 | 44,800 |
| 2022-03-27 01:30:00+00 | eco2mix-national-cons-def | 2022-03-27 | 02:30 | 46,073 | 44,700 | 44,800 |
| 2023-03-26 01:00:00+00 | eco2mix-national-cons-def | 2023-03-26 | 02:00 | 44,716 | 42,700 | 43,700 |
| 2023-03-26 01:30:00+00 | eco2mix-national-cons-def | 2023-03-26 | 02:30 | 45,124 | 42,700 | 43,700 |
| 2024-03-31 01:00:00+00 | eco2mix-national-cons-def | 2024-03-31 | 02:00 | 46,662 | 46,423 | 45,700 |
| 2024-03-31 01:30:00+00 | eco2mix-national-cons-def | 2024-03-31 | 02:30 | 47,346 | 46,423 | 45,700 |
| 2025-03-30 01:00:00+00 | eco2mix-national-cons-def | 2025-03-30 | 02:00 | 47,832 | 47,000 | 47,600 |
| 2025-03-30 01:30:00+00 | eco2mix-national-cons-def | 2025-03-30 | 02:30 | 48,397 | 47,000 | 47,600 |
| 2026-03-29 01:00:00+00 | eco2mix-national-cons-def | 2026-03-29 | 02:00 | 54,149 | 52,400 | 52,400 |
| 2026-03-29 01:30:00+00 | eco2mix-national-cons-def | 2026-03-29 | 02:30 | 53,867 | 52,400 | 52,400 |

### 3.2 Excluded Quarter-Hour Timestamps (Minutes 15 and 45)

Query used: [Query 4](#query-4-excluded-quarter-hour-records).

All records with UTC minutes `15` or `45` (97,008 quarter-hour records across 2021 to 2026) were filtered
out by the 30-minute sampling rule.

### 3.3 Autumn Missing Timestamps (Preserved as Missing)

On each autumn daylight saving Sunday (last Sunday of October), the ODRE source omits timestamps for
`00:00` and `00:30` UTC. Per Protocol D12 (where the first-or-second hour question remains unproven until
further real-time observation), these slots are not imputed. The affected slots are:
- `2021-10-31 00:00:00+00` and `2021-10-31 00:30:00+00`
- `2022-10-30 00:00:00+00` and `2022-10-30 00:30:00+00`
- `2023-10-29 00:00:00+00` and `2023-10-29 00:30:00+00`
- `2024-10-27 00:00:00+00` and `2024-10-27 00:30:00+00`
- `2025-10-26 00:00:00+00` and `2025-10-26 00:30:00+00`

---

## 4. Verification Queries

### Query 1: Row Counts by Paris Calendar Year

The clean-table SQL below uses `clean_query` templates: `{table}` is the relation selected by `LFF_CLEAN_TABLE`. The recorded outputs came from a development build of the clean model `load_30min`.

```sql
SELECT 
    extract(year FROM value_time AT TIME ZONE 'Europe/Paris')::int AS paris_year,
    source_dataset,
    count(*) AS total_rows,
    count(consommation) AS cons_filled,
    round(100.0 * count(consommation) / count(*), 3) AS cons_pct,
    count(prevision_j1) AS d1_filled,
    round(100.0 * count(prevision_j1) / count(*), 3) AS d1_pct,
    count(prevision_j) AS d_filled,
    round(100.0 * count(prevision_j) / count(*), 3) AS d_pct,
    min(value_time) AS min_utc,
    max(value_time) AS max_utc
FROM {table}
GROUP BY paris_year, source_dataset
ORDER BY paris_year, source_dataset;
```

### Query 2: Row Counts by UTC Calendar Year

```sql
SELECT 
    extract(year FROM value_time AT TIME ZONE 'UTC')::int AS utc_year,
    count(*) AS total_rows,
    count(consommation) AS cons_filled,
    count(prevision_j1) AS d1_filled,
    count(prevision_j) AS d_filled,
    min(value_time) AS min_utc,
    max(value_time) AS max_utc
FROM {table}
GROUP BY utc_year
ORDER BY utc_year;
```

### Query 3: Excluded Spring DST Artefacts

```sql
SELECT 
    value_time,
    source_dataset,
    original_values->>'date' AS local_date,
    original_values->>'heure' AS phantom_heure,
    consommation,
    prevision_j1,
    prevision_j
FROM raw.eco2mix
WHERE extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
  AND (value_time AT TIME ZONE 'Europe/Paris')::date >= '2021-03-23'
  AND original_values->>'heure' != to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
ORDER BY value_time;
```

### Query 4: Excluded Quarter-Hour Records

```sql
SELECT count(*) AS excluded_quarter_hour_records
FROM raw.eco2mix
WHERE extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (15, 45)
  AND (value_time AT TIME ZONE 'Europe/Paris')::date >= '2021-03-23';
```

Output: `97008`

---

## 5. dbt Tests Suite

The pipeline is validated with automated dbt tests across the intermediate and clean layers:

1. **Intermediate Layer (`int_load_30min_filtered`):**
   - `not_null` on `value_time`, `source_dataset`, `prevision_j1`, `prevision_j`, `ingested_at`.
   - `accepted_values` on `source_dataset` (`eco2mix-national-cons-def`, `eco2mix-national-tr`).
   - `assert_no_spring_02xx_in_intermediate`: singular test verifying zero spring 02:xx phantom rows exist
     in the intermediate model before deduplication.

2. **Clean Layer (`load_30min`):**
   - `unique` on `value_time`: confirms zero duplicated timestamps in the 30-minute series.
   - `not_null` on `value_time`: confirms every slot has a valid UTC timestamp.
   - `not_null` on `source_dataset`: confirms source provenance is recorded for every observation.
   - `accepted_values` on `source_dataset`: confirms only valid ODRE datasets are recorded.
   - `not_null` on `prevision_j1`: confirms zero missing values in the RTE D-1 benchmark series.
   - `not_null` on `prevision_j`: confirms zero missing values in the RTE D intraday series.
   - `not_null` on `consommation` for `eco2mix-national-cons-def`: confirms 100% fill rate for definitive data.
   - `not_null` (severity warn) on `consommation` for `eco2mix-national-tr`: flags the 2 recent unfinalized slots.
   - `assert_no_spring_02xx_artefacts`: singular test verifying that every spring DST day has exactly 46 slots
     and that no forecast matches phantom `02:xx` filler values.

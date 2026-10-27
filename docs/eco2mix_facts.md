# eCO2mix collector and measured facts

Measured on 2026-10-03 in `load_forecast.raw.eco2mix`. This is an ingestion
audit, including the requested 2025 history, not a model evaluation or test-year run.
SQL files below are the exact queries executed through Psycopg. They use UTC
storage and Europe/Paris calendar years. These figures describe this snapshot;
the real-time source and stored distinct revisions can change on subsequent loads.

## Run the collector

Set `POSTGRES_USER` and `POSTGRES_PASSWORD` in the process environment using the
developer's local credentials. `POSTGRES_DB` must be `load_forecast` (the default).
The local `.env` lives in the repository root, beside `.env.example`; Compose's
`--env-file .env` loads it for Compose, not into your PowerShell process.
Optional `POSTGRES_HOST` and `POSTGRES_PORT` default to `127.0.0.1` and `5432`.
The module reads environment variables; it does not automatically load `.env`.
Optional `ODRE_API_KEY` is sent in an Authorization header, never in the URL.
Install the dependencies in `requirements.txt` in the shared environment before
running the commands below. The original collector verification used the existing
ignored `.venv` environment.

From the repository root, after setting the environment:

```powershell
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.eco2mix
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.eco2mix --start 2024-01-01 --end 2025-01-01 --dataset eco2mix-national-cons-def

# Weekly consolidated refresh (runs once a week to ingest consolidated data from 2026-07-01 onward and record first appearances):
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.eco2mix --mode weekly-consolidated

# Real-time consumption collection (runs every 30 minutes to append recent quarter-hours):
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.eco2mix --mode realtime-30min
```

Dates are half-open Paris calendar dates. The default starts at 2021-03-23 and
ends at tomorrow's midnight, including today's available records. Both the
consolidated/definitive and real-time datasets are requested independently in
monthly JSON exports using `where`, `order_by=date_heure`, and `timezone=UTC`.
No interpolation, dataset precedence, DST repair or forecast input selection is
performed. A batch validates fully before an atomic insert.

The collector provides two operational modes:
- `--mode weekly-consolidated`: requests the consolidated dataset (`eco2mix-national-cons-def`)
  from 2026-07-01 onward using monthly windows. When a month returns records for the first
  time, its first ingestion timestamp is permanently recorded in `raw.eco2mix_first_appearance`.
  Unpublished months (0 records) are not recorded. Running this weekly is idempotent:
  already ingested rows and the earliest recorded timestamps are preserved.
- `--mode realtime-30min`: collects recent real-time quarter-hours (`eco2mix-national-tr`,
  by default the last 2 days), appending new and revised records with their ingestion timestamp
  into `raw.eco2mix`, providing an upper bound on the publication delay.

The raw key is `(source_dataset, value_time, payload_hash)`, with a SHA-256 hash
of the canonical full source object. Identical originals are skipped and different
originals at the same timestamp are retained. JSONB preserves all source fields,
including nulls; dedicated consumption and forecast columns retain integer MW
values. `value_time` and `ingested_at` are `timestamptz`, with UTC sessions.
This is an archive of distinct values first seen, not a complete revision timeline:
if a source later reverts to a previously seen identical object it is skipped too.
Ingestion time does not establish the historical publication time.

The table `raw.eco2mix_first_appearance` records `(source_dataset, month, first_ingested_at, record_count)`
with primary key `(source_dataset, month)`. Row storage and the first-appearance record commit in the
same transaction, and the recorded appearance timestamp recovers the earliest `ingested_at` stored
for that month if records were already present.

Every HTTP attempt, including retries and failures, is reserved and committed to
`raw.eco2mix_api_calls` before the request. Dry-run requests go through the same quota reservation
when database access is available; if dry-run cannot reserve without the database, it makes at most
one attempt per request without retries. Writer modes check role privileges before any mutating SQL
and refuse execution under a read-only role. A PostgreSQL advisory transaction lock
serializes the monthly budget check across collector processes using this database.
The collector stops at 45,000 reserved attempts per UTC month. ODRE documents a
50,000-call monthly quota; other clients sharing the provider's quota must be
accounted for separately. Exports avoid records-endpoint pagination limits.
Transient connection errors, timeouts, HTTP 429 and server errors get at most five
attempts with exponential backoff. Redirects and permanent HTTP errors fail closed.
Connection and HTTP exception details are suppressed to keep credentials out of logs.
[ODRE quota and update frequency](https://odre.opendatasoft.com/explore/dataset/eco2mix-national-tr/information/),
[Explore API export documentation](https://help.opendatasoft.com/apis/ods-explore-v2/).

## Filled values by source, year and sampling grid

Query: [Q1](../scripts/eco2mix_completeness.sql).
The denominator is retained original records, including distinct conflicting
records at a timestamp. Zero is filled, null is missing. The 30-minute sample
keeps UTC minutes 00 and 30; it does not average or fill the 15-minute series.
Q2 below separately checks coverage of unique timestamps.

| Source | Paris year | Step (min) | Records | Consumption filled (%) | RTE D-1 filled (%) | RTE D filled (%) |
|--------|------------|------------|---------|------------------------|-------------------|-----------------|
| cons-def | 2021 | 15 | 27,264 | 50.000 | 100.000 | 100.000 |
| cons-def | 2021 | 30 | 13,632 | 100.000 | 100.000 | 100.000 |
| cons-def | 2022 | 15 | 35,040 | 50.000 | 100.000 | 100.000 |
| cons-def | 2022 | 30 | 17,520 | 100.000 | 100.000 | 100.000 |
| cons-def | 2023 | 15 | 35,040 | 50.000 | 100.000 | 100.000 |
| cons-def | 2023 | 30 | 17,520 | 100.000 | 100.000 | 100.000 |
| cons-def | 2024 | 15 | 35,136 | 50.000 | 100.000 | 100.000 |
| cons-def | 2024 | 30 | 17,568 | 100.000 | 100.000 | 100.000 |
| cons-def | 2025 | 15 | 35,040 | 50.000 | 100.000 | 100.000 |
| cons-def | 2025 | 30 | 17,520 | 100.000 | 100.000 | 100.000 |
| cons-def | 2026 | 15 | 17,376 | 50.000 | 100.000 | 100.000 |
| cons-def | 2026 | 30 | 8,688 | 100.000 | 100.000 | 100.000 |
| tr | 2026 | 15 | 9,120 | 99.956 | 100.000 | 100.000 |
| tr | 2026 | 30 | 4,560 | 99.956 | 100.000 | 100.000 |

`cons-def` abbreviates `eco2mix-national-cons-def`; `tr` abbreviates
`eco2mix-national-tr`. The first year starts at the requested date, and the final
year is partial. No real-time records were returned for older years in this
snapshot; those empty groups have no defined filled percentage.

## Coverage and source timestamp anomalies

Query: [Q2](../scripts/eco2mix_year_check.sql), full Paris calendar year 2024,
both datasets combined, counting each UTC timestamp once.

| Step (min) | Expected timestamps | Present | Missing timestamps | Missing consumption | Missing D-1 | Missing D |
|------------|---------------------|---------|--------------------|---------------------|-------------|-----------|
| 15 | 35,136 | 35,132 | 4 | 17,570 | 4 | 4 |
| 30 | 17,568 | 17,566 | 2 | 2 | 2 | 2 |

Query: [Q3](../scripts/eco2mix_gaps.sql), bound parameters
`start=2021-03-22T23:00:00+00:00`, `end=2026-10-03T22:00:00+00:00`.
These are all missing timestamp blocks in the requested period, regardless of
whether particular values at a present timestamp are null.

| First missing UTC | Last missing UTC | Missing quarter-hours |
|-------------------|------------------|-----------------------|
| 2021-10-31 00:00 | 2021-10-31 00:45 | 4 |
| 2022-10-30 00:00 | 2022-10-30 00:45 | 4 |
| 2023-10-29 00:00 | 2023-10-29 00:45 | 4 |
| 2024-10-27 00:00 | 2024-10-27 00:45 | 4 |
| 2025-10-26 00:00 | 2025-10-26 00:45 | 4 |

Query: Q4 in [observations](../scripts/eco2mix_observations.sql).
The consolidated/definitive source also has conflicting originals at spring
transition times. Every row below contains four conflicting timestamps and eight
original records. Real-time has no conflicting timestamps in this snapshot.

| Paris year | First conflicting UTC | Last conflicting UTC |
|------------|-----------------------|----------------------|
| 2021 | 2021-03-28 01:00 | 2021-03-28 01:45 |
| 2022 | 2022-03-27 01:00 | 2022-03-27 01:45 |
| 2023 | 2023-03-26 01:00 | 2023-03-26 01:45 |
| 2024 | 2024-03-31 01:00 | 2024-03-31 01:45 |
| 2025 | 2025-03-30 01:00 | 2025-03-30 01:45 |
| 2026 | 2026-03-29 01:00 | 2026-03-29 01:45 |

Inspect the preserved objects rather than selecting an arbitrary hash as truth:

```sql
SELECT value_time, original_values
FROM raw.eco2mix
WHERE source_dataset = 'eco2mix-national-cons-def'
  AND value_time >= '2024-03-31 01:00 UTC'::timestamptz
  AND value_time < '2024-03-31 02:00 UTC'::timestamptz
ORDER BY value_time, original_values->>'heure';
```

This query was run during the audit: local `heure` labels 02:00 and 03:00
map to the same UTC timestamp, with equal consumption and different D-1 forecasts.
The collector uses the offset-bearing `date_heure` as supplied and preserves both
objects. Downstream transformations need a reviewed conflict and DST policy.
Filled-record percentages alone therefore do not establish a clean forecast grid.

## Stored bounds, freshness and publication times

Query: Q5 in [observations](../scripts/eco2mix_observations.sql).

| Source | Raw records | Unique UTC times | First UTC | Last UTC |
|--------|-------------|------------------|-----------|----------|
| cons-def | 184,896 | 184,872 | 2021-03-22 23:00 | 2026-06-30 21:45 |
| tr | 9,120 | 9,120 | 2026-06-30 22:00 | 2026-10-03 21:45 |

The latest non-null real-time consumption was for `2026-10-03T20:45:00+00:00`.
It was first ingested at `2026-10-03T21:07:23.876688+00:00`, an age of
`00:22:23.876688` (Q5). This is one snapshot's age at ingestion, not a measurement
of publication delay. Per-value publication timestamps are absent from the returned
source records. Measuring publication delay requires repeated first-seen polling,
which was not performed. Historical consolidation and ingestion times cannot
reconstruct when a real-time value originally became available.

RTE identifies D-1 as a forecast established the previous day and D as updated
on the target day. The inspected source information does not establish the exact
publication hour or demonstrate that the archived D-1 value was available at our
noon cutoff. The collector does not use RTE forecasts as inputs and makes no
historical availability guarantee.
[RTE consumption and forecast description](https://www.rte-france.com/donnees-publications/eco2mix-donnees-temps-reel/consommation-france),
[ODRE consolidated dataset](https://odre.opendatasoft.com/explore/dataset/eco2mix-national-cons-def/information/).

Query: Q6 in [observations](../scripts/eco2mix_observations.sql) returned
148 reserved calls for October 2026 after the validation-year load and full
history load. No uncounted HTTP calls were made by these collector commands.

## Verification run

The original collector run used ignored local launchers. I preserve its measured
results below and replace the commands with tracked equivalents. From the root,
load the process environment with the maintained PowerShell loader:

```powershell
. ./scripts/load_env.ps1
$env:PYTHONPATH = 'src'
```

Collector commands (for a collector task or developer-managed ingestion only):

```powershell
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.eco2mix --start 2024-01-01 --end 2025-01-01 --dataset eco2mix-national-cons-def
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.eco2mix --start 2021-03-23 --end 2026-10-04
```

Read-only reproduction of Q1 to Q6 on the current stored snapshot:

```powershell
& ../load-forecast-fr-venv/Scripts/python.exe scripts/audit_eco2mix.py
```

The tracked audit uses the SQL files cited above in one read-only transaction.
It writes `data/audit/load_audit_003/results.json`. Q3 retains the historical bounds
2021-03-22 23:00 UTC to 2026-10-03 22:00 UTC; other queries describe the current
stored rows, so a later ingestion can change their output.

The original test wrapper set `TEST_ECO2MIX_DATABASE=1` and called pytest.
Its maintained equivalent is for a developer-authorized live idempotence check
only; it attempts duplicate inserts of an existing original into the raw table:

```powershell
$env:TEST_ECO2MIX_DATABASE = '1'
& ../load-forecast-fr-venv/Scripts/python.exe -m pytest
Remove-Item Env:TEST_ECO2MIX_DATABASE
& ../load-forecast-fr-venv/Scripts/python.exe -m ruff check .
```

For an ordinary read-only work session, run pytest without that environment flag.
The source code for the tests is tracked under `tests/`.

Both collector runs exited zero. The second run's validation-year requests each
reported `inserted=0`; the retained raw-year count stayed 35,136 (Q1). The audit
executed Q1 to Q6 and wrote an ignored `.venv/t003_audit.json`.
The full test run, with `TEST_ECO2MIX_DATABASE=1`, returned 34 passed and
26 xfailed. The database test reinserted an existing real original twice, checked
unchanged yearly row count and ingestion timestamp, and verified actual PostgreSQL
timestamp types and UTC sessions. No synthetic rows were inserted or data deleted.
Without that environment flag the database test is skipped. Ruff and
`git diff --check` passed; the authored cutoff tests were unchanged.
The run results above are historical collector evidence. The displayed commands are
maintained equivalents; the original ignored launchers are not distributed.
The leading `&` explicitly invokes each executable in PowerShell.
Use the public module commands above for a new collector run.

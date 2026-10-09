# Consumption revision gap

## 1. Stored overlap

I checked `raw.eco2mix` on 2026-10-07 as `lff_agent`, in one read-only,
repeatable-read transaction. There are **zero shared UTC timestamps** between
`eco2mix-national-tr` and `eco2mix-national-cons-def`, including timestamps with
null consumption. The filled-value and half-hour overlaps are also zero.
I read counts and storage metadata only, with no model outputs.

| Source | Raw rows | Distinct UTC timestamps | Timestamps with consumption |
|---|---:|---:|---:|
| Consolidated/definitive | 184896 | 184872 | 92436 |
| Real-time | 9120 | 9120 | 9116 |

I collapse append-only revisions by UTC timestamp before counting. A timestamp
counts as filled when at least one stored version has consumption. Counts cover
the raw quarter-hour grid, including source DST artefacts; they are not counts
of usable clean half-hours.

| Paris period | Real-time timestamps | Consolidated timestamps | Shared timestamps |
|---|---:|---:|---:|
| 2021 (from 23 March) | 0 | 27260 | 0 |
| 2022 | 0 | 35036 | 0 |
| 2023 | 0 | 35036 | 0 |
| 2024 | 0 | 35132 | 0 |
| 2025 | 0 | 35036 | 0 |
| 2026-01 | 0 | 2976 | 0 |
| 2026-02 | 0 | 2688 | 0 |
| 2026-03 | 0 | 2972 | 0 |
| 2026-04 | 0 | 2880 | 0 |
| 2026-05 | 0 | 2976 | 0 |
| 2026-06 | 0 | 2880 | 0 |
| 2026-07 | 2976 | 0 | 0 |
| 2026-08 | 2976 | 0 | 0 |
| 2026-09 | 2880 | 0 | 0 |
| 2026-10 (through 3 October) | 288 | 0 | 0 |
| All | 9120 | 184872 | 0 |

Consolidated timestamps span `2021-03-22 23:00` to `2026-06-30 21:45` UTC.
Real-time timestamps span `2026-06-30 22:00` to `2026-10-03 21:45` UTC.
Their latest ingestion times are respectively `2026-10-03 21:07:01.684218`
and `2026-10-03 21:07:23.876688` UTC. These bounds describe stored timestamps,
including nulls, rather than continuous filled consumption coverage. I cannot
infer today's source availability from a snapshot last ingested on 3 October.

## 2. Publication expectation and limits

The existing [benchmark note](rte_benchmark.md), section 3.2,
records consolidation around mid-month M+1 and definitive data in the second
semester of A+1, both **unverified**. Applying that provisional timetable to
2026 gives the following expectations, not confirmed ODRE release dates:

| Delivery month | Consolidated publication expectation (unverified) |
|---|---|
| January to June 2026 | Around mid-February to mid-July 2026, respectively |
| July 2026 | Around mid-August 2026 |
| August 2026 | Around mid-September 2026 |
| September 2026 | Around mid-October 2026 |
| October 2026 | Around mid-November 2026 |
| November 2026 | Around mid-December 2026 |
| December 2026 | Around mid-January 2027 |

Definitive 2026 data would be expected in July to December 2027 under the same
unverified A+1 rule. I did not query an external API in this research task.
The absence of July and August consolidated rows locally does not establish a
publication delay: the stored consolidated series may simply need a refresh.

With no paired vintages, the mean and MAE of real-time minus consolidated
consumption by month or local hour are **not measurable**, rather than zero.
The implied EXP-004 lag forecast shift and the change in conformal residuals
are also not measurable. Comparing different periods cannot isolate revisions.
Once overlap is stored, the lag effect must use the fitted slot coefficients
and the gaps at each actual lag timestamp; the approximate 0.57 multiplier
alone would not measure that effect. No coefficient or model was changed.

## 3. Reproduce

The exact queries are in [revision_gap_counts.sql](../scripts/revision_gap_counts.sql).
They count the complete stored history, with annual periods before 2026 and
monthly periods in 2026. The runner uses environment credentials without
printing them and imports only the existing database connection helper.

From the repository root in PowerShell:

```powershell
. .\scripts\load_env.ps1
& ..\load-forecast-fr-venv\Scripts\python.exe scripts/check_revision_gap.py
& ..\load-forecast-fr-venv\Scripts\python.exe -m pytest
& ..\load-forecast-fr-venv\Scripts\python.exe -m ruff check .
```

The counts can change after ingestion. Re-run the counts before measuring the
revision gap; no model run or new backtest is needed for this check.

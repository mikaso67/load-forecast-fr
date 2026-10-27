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

## 4. Measured gap, July and August 2026

I re-read `raw.eco2mix` on 2026-10-09 as `lff_agent`, in one read-only,
repeatable-read transaction, after the weekly collector stored the consolidated
months July and August 2026. Sections 1 to 3 describe the earlier snapshot of
2026-10-07, when the overlap was still empty. I apply the clean-layer row rules
(UTC half-hours, the D12 wall-clock check, latest stored version per source and
timestamp). The sign is **gap = consolidated minus real-time**: a positive gap
means the real-time value is below the consolidated one.

Every half-hour of the 62 Paris days has both vintages: 2976 shared slots out
of 2976 expected, one stored version of each value. Intervals are moving block
bootstrap 95% intervals of the mean over Paris daily means (seed 20240,
10000 draws), 7-day blocks as the primary rule and 14-day blocks as the check
(D10).

### 4.1 When each vintage was stored

| Item | Value (UTC) |
|---|---|
| First appearance, consolidated July 2026 | 2026-10-09 13:34:01, 2976 rows |
| First appearance, consolidated August 2026 | 2026-10-09 13:34:02, 2976 rows |
| Earlier consolidated requests for the same ranges | 2026-10-03 21:07, no row stored |
| Real-time July and August rows | all ingested 2026-10-03 21:07, 33 to 95 days after their timestamp |

The first appearance is an upper bound on the publication date: July and
August consolidated data were public by 2026-10-09 at the latest. If the
requests of 2026-10-03 received a valid empty answer, both months were
published between the two dates, roughly 9 weeks after the end of July and
5 weeks after the end of August, much later than the unverified mid M+1 rule of
section 2. The request ledger stores requests, not answers, so this lower bound
is not proven. Either way, a consolidated value at D-2 or D-7 is never known at
the 12:00 D-1 freeze: in production every lag is a real-time value.

The real-time values were stored by a backfill, not at 12:00 D-1. If RTE revises
real-time values within days of publication, the gap a production forecast
actually meets can differ from what I measure here. The stored data cannot
show this; values stored by the 30-minute collector as they are first published
will.

### 4.2 Gap by month

| Paris month | Slots | Mean (MW) | Median | SD | Mean absolute | 7-day 95% interval | 14-day check |
|---|---:|---:|---:|---:|---:|---|---|
| July 2026 | 1488 | 943.8 | 873.5 | 759.5 | 984.8 | [838.5, 1153.5] | [848.0, 1068.7] |
| August 2026 | 1488 | 1100.1 | 961.0 | 896.6 | 1129.5 | [965.6, 1204.1] | [976.5, 1163.9] |
| Both | 2976 | 1022.0 | 921.5 | 834.4 | 1057.2 | [920.7, 1125.0] | [934.5, 1097.5] |

Real-time consumption is about 1.0 GW, or 2.2% (SD 1.8 points), below the
consolidated value. The gap is positive in 92% of slots (237 of 2976 are
negative), and the mean absolute gap is close to the mean. The July and August intervals overlap: I do not claim a
difference between the two months.

### 4.3 Gap by Paris hour

Both months pooled, 124 slots per hour (two half-hours on each of 62 days).
These are 24 separate intervals, not corrected for multiple comparisons.

| Paris hour | Mean (MW) | Median | SD | 7-day 95% interval | 14-day check |
|---|---:|---:|---:|---|---|
| 00:00 | 1486 | 1460 | 324 | [1409, 1599] | [1398, 1612] |
| 01:00 | 1032 | 1035 | 247 | [968, 1118] | [964, 1118] |
| 02:00 | 1220 | 1170 | 249 | [1144, 1324] | [1143, 1330] |
| 03:00 | 855 | 822 | 289 | [775, 956] | [770, 961] |
| 04:00 | 546 | 530 | 271 | [463, 656] | [456, 663] |
| 05:00 | 212 | 168 | 275 | [163, 288] | [164, 291] |
| 06:00 | -91 | -123 | 329 | [-148, 13] | [-133, 33] |
| 07:00 | 12 | 19 | 300 | [-54, 102] | [-37, 111] |
| 08:00 | 276 | 190 | 361 | [198, 375] | [201, 381] |
| 09:00 | 874 | 900 | 464 | [735, 1020] | [760, 981] |
| 10:00 | 1244 | 1230 | 626 | [1024, 1468] | [1055, 1398] |
| 11:00 | 1438 | 1356 | 793 | [1185, 1714] | [1200, 1655] |
| 12:00 | 1612 | 1550 | 796 | [1356, 1864] | [1356, 1831] |
| 13:00 | 2444 | 2364 | 894 | [2151, 2731] | [2116, 2734] |
| 14:00 | 1874 | 1738 | 925 | [1551, 2146] | [1497, 2150] |
| 15:00 | 2132 | 2107 | 823 | [1854, 2350] | [1842, 2332] |
| 16:00 | 1796 | 1790 | 726 | [1548, 1998] | [1564, 1945] |
| 17:00 | 1289 | 1196 | 602 | [1068, 1453] | [1102, 1392] |
| 18:00 | 617 | 594 | 435 | [478, 734] | [496, 706] |
| 19:00 | 462 | 472 | 356 | [384, 545] | [401, 532] |
| 20:00 | 896 | 882 | 316 | [823, 994] | [828, 995] |
| 21:00 | 594 | 596 | 308 | [477, 703] | [452, 724] |
| 22:00 | 495 | 482 | 316 | [384, 616] | [373, 625] |
| 23:00 | 1213 | 1204 | 226 | [1165, 1290] | [1174, 1287] |

The gap is largest in the early afternoon (1.8 to 2.4 GW from 13:00 to 16:00),
not distinguishable from zero at 06:00 and 07:00, and high again around
midnight. The day-time shape follows solar production, which suggests that the
real-time series misses part of the consumption met by distributed solar and
that consolidation adds it back. This is a hypothesis: I did not test it, and it
does not explain the jump at 13:00 or the alternation between night hours. The
month-by-hour means are in `data/revision_gap/month_hour_mean.csv` after a run.

### 4.4 Implied shift on the lag features of the served model

The served model aggregates four experts that share the same three lags
(EXP-004): the same local slot of D-2, of D-7, and the D-1 09:30 slot. The
backtest reads them on the consolidated scale, production reads them on the
real-time scale, so each lag moves by minus the gap at its own timestamp.
56 delivery days, 2026-07-08 to 2026-09-01, have all three lags inside the
overlap (2688 slots).

| Lag feature | Mean shift (MW) | 7-day 95% interval | 14-day check |
|---|---:|---|---|
| D-2, same slot | -1020.1 | [-1145.7, -924.2] | [-1098.3, -958.0] |
| D-7, same slot | -1013.2 | [-1115.6, -900.4] | [-1058.1, -926.3] |
| D-1 09:30 | -968.3 | [-1147.2, -812.1] | [-1085.3, -859.8] |

To turn this into a forecast shift I need fitted coefficients. I refit the
EXP-004 linear model at the Monday refit of 2024-12-30, the last refit of the
validation year, so no 2025 or 2026 value enters the fit, and I change no
model. Its three lag coefficients sum to 0.569 on average over the 48 slots
(0.486 to 0.664), which reproduces the 0.57 quoted earlier; the mean
coefficients are 0.156 (D-2), 0.063 (D-7) and 0.351 (D-1 09:30). With each
slot's own coefficients and the gap at each actual lag timestamp:

| Quantity | Mean (MW) | 7-day 95% interval | 14-day check |
|---|---:|---|---|
| EXP-004 forecast shift, production minus backtest inputs | -584.5 | [-661.7, -518.4] | [-636.7, -539.9] |

This is the linear expert only. The GAM experts carry the same lags as linear
terms with their own coefficients, LightGBM uses them non-linearly, and the
aggregation mixes the four, so the shift of the served forecast is not
computed here. I expect, without having measured it, that the online parts
are affected more than the lags: the Kalman correction, the aggregation losses
and the conformal residuals would be updated on real-time actuals, about 1 GW
below the consolidated target the experts were trained on. This is a hypothesis
to test once the production function exists. For scale, -584 MW is 70% of the 2024 MAE of the aggregation
(828.5 MW) and several times its margin over RTE* in 2024 (68 MW) or in 2025
(134 MW).

### 4.5 RTE D-1 on the same half-hours

The RTE D-1 value is identical in both datasets on all 2976 slots. Its mean
error is +167.2 MW against real-time consumption and -854.7 MW against
consolidated consumption (MAE 713.7 and 1057.6 MW). On these two months the
vintage alone moves the mean error of RTE by the mean gap. This is consistent
with the hypothesis of `docs/rte_benchmark.md` that the negative bias of RTE
against consolidated data comes mostly from consolidation, for summer 2026
only: it says nothing about 2024.

### 4.6 What two months can and cannot show

They show that in July and August 2026 the gap is large (about 1 GW), positive
in 92% of slots, statistically clear by month and in 22 of 24 hours, and
that it would move every lag by about -1 GW and the linear expert by about
-0.58 GW.

They cannot show:
- the gap in winter or over a year. If the day-time gap comes from solar
  production, it should shrink in winter, while heating could add a pattern of
  its own. Nothing here can be applied to December or January;
- whether the gap is stable from year to year;
- the gap a production forecast actually meets, because the real-time values
  were stored months after publication (section 4.1);
- the shift of the served aggregation, of the Kalman correction and of the
  interval width;
- much about dependence beyond two weeks: 62 days are about nine 7-day blocks,
  and each month's 14-day check rests on two or three blocks.

### 4.7 Proposal for P5

This is a proposal; the decision belongs to D20 P5.

1. The production design asked for a materiality threshold fixed before the
   measurement. None was written down, so any threshold set now is set after
   seeing the numbers, and I record that. The conclusion does not depend on it:
   the 7-day interval of the linear-expert shift, [-662, -518] MW, lies beyond
   50, 100 or 134 MW, the margin by which the first test candidate beat RTE*
   in 2025.
2. I propose to declare the gap material and not to report option A alone as
   the production forecast. Run option B (real-time lags and online-update
   actuals mapped to the consolidated scale) in parallel with A for at least
   eight weeks, as the design foresees, and keep both labelled provisional.
3. B needs three amendments that these numbers reveal. The mapping must be by
   Paris hour, because the gap ranges from about 0 to 2.4 GW across the day.
   It can only use months whose consolidated data were stored before the
   freeze, which on the timing of section 4.1 means months at least five
   weeks old and probably more, so a summer mapping would be applied to
   autumn days; it should be re-estimated as each
   consolidated month appears. And it must not be trusted in winter before a
   winter month has been measured.
4. Before go-live, re-measure the gap on real-time values as first stored by
   the 30-minute collector, for September and October 2026, once their
   consolidated months appear, to check the caveat of section 4.1.
5. Option C (real-time target) deserves a second look: RTE D-1 is nearly
   unbiased on the real-time scale, and the online updates would then use the
   scale of the data they actually receive. Its cost, a partly mapped training
   history, is unchanged.

### 4.8 Reproduce

The query is in [revision_gap_measure.sql](../scripts/revision_gap_measure.sql)
and the statistics in `src/load_forecast/revision_gap.py`. From the repository
root in PowerShell:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = "src"
& ..\load-forecast-fr-venv\Scripts\python.exe scripts/measure_revision_gap.py
```

The run takes about two minutes, mostly for the EXP-004 refit. It prints every
number of this section and writes `summary.json` and the hour, month-by-hour
and coefficient tables to `data/revision_gap/`. New ingestion can change the
result; the window is fixed to July and August 2026.

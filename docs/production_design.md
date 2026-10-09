# Production design

Status: proposal. Every open choice below (P1 to P10) lists options with trade-offs and a recommendation.
I decide each one before the corresponding part is built.

This document designs the daily production run that forecasts delivery day D at 12:00 Europe/Paris on D-1
and delivers before 12:30 (D16). It builds on `docs/protocol.md` (D2, D6, D11, D15, D16),
`docs/orchestration.md` (Prefect, D17), `docs/revision_gap.md`, `docs/api.md`, and the EXP-009, EXP-012
and TEST-2025 reports. The guiding rule is the one of protocol section 3: production and backtest call the
same functions with the same information, so the backtest numbers describe what production does.

## 0. Open choices

| # | Question | Recommendation |
|---|----------|----------------|
| P1 | How the consumption snapshot is built at the freeze | dbt variable `ingested_before` on the clean layer |
| P2 | How the online states start on the first production day | aggregation and conformal states rebuilt from the archived outputs; models and Kalman start at a Monday refit in 2026 |
| P3 | Status of 2026 and later days | a production period, never used to choose a model |
| P4 | When the weekly refit runs | on the critical path after the Sunday freeze, as in the backtest |
| P5 | Real-time versus consolidated consumption | measure first; keep the backtest rule meanwhile; map inputs only if the gap is material |
| P6 | Which forecast is the official production score | the forecast actually delivered at 12:30 |
| P7 | Drift statistic | two-sided CUSUM on the daily mean error, threshold calibrated by a seasonal block bootstrap, plus relative skill against raw RTE D-1 |
| P8 | Missing last consumption value at the freeze | keep the current backup chain |
| P9 | Weather-missing day | keep the current backup chain, correct the M11 wording |
| P10 | Deadline enforcement | a fallback prepared the day before, served at 12:30 by a bounded check outside the main flow |

## 1. Frozen input snapshot

### 1.1 Freeze time

The freeze time of delivery day D is `F = forecast_cutoff(D)`, 12:00 Paris on D-1 in UTC
(10:00 UTC in summer time, 11:00 UTC in winter time, computed from the local date of D-1).
F is a parameter of the run computed from D, never the wall clock when the job starts. A run that starts
at 12:04 because the machine was busy still freezes at 12:00, so its inputs equal those of an on-time run.
A run that would start before F refuses to start.

Every run records a start event and, if it completes, one completion row (section 4.1) with: delivery day,
F, the code commit, the models in use, the pinned weather rows, the last consumption slot used, the maximum
`ingested_at` read, a hash of the input feature matrix and a status. These rows are what make a forecast
replayable. Which run was delivered is a separate record (section 4.1).

### 1.2 Consumption (D2)

Two filters apply together:
- D2, as in the backtest: only slots that end at or before F - 2 h, so the last usable slot is
  09:30 to 10:00 Paris on D-1 (`evaluation.last_usable_slot_start`).
- Ingestion: only raw rows with `ingested_at < F`. `raw.eco2mix` is append-only and stamps every row with
  its ingestion time, which is always later than its publication time. The filter can therefore only remove
  information, never add some: it is conservative.

In the backtest the second filter is not applied, because historical consolidated rows were all ingested
in 2026, long after their cutoffs. This is the documented revision leak (protocol 2.2, checklist item 12),
and section 3 deals with it. If the collector ingests on time, the two filters select the same slots, and the
production features equal the backtest features built from the same values.

The production lags are the EXP-004 lags: the same local slot on D-2 and D-7 and the 09:30 slot of D-1
(`lag_model.lag_features`). The real-time dataset must therefore hold the 09:30 slot before 12:00.
The collector runs every 30 minutes with an extra run at 11:55 Paris, so a 09:30 slot published up to
1 h 55 after it ends still enters the snapshot. The first ingestion time minus the slot end is an upper
bound on the publication delay, not its measurement: a value can be published up to one collection
interval (30 minutes) before the collector sees it. This bound is what D2 asks for before the 2-hour rule
can ever be relaxed; it can only make the rule look stricter than needed, never looser.

P1. How the snapshot reaches the code.
- A. A dbt variable `ingested_before` on `int_load_30min_filtered`, like the existing `delivery_end_exclusive`
  variable, built at the freeze into a run snapshot relation that the code reads through `LFF_CLEAN_TABLE`.
  One implementation of D12 and of the consolidated-first priority. Replay = the same build with the same F.
  Cost: one dbt build per day on the critical path (the clean model is about 190 000 raw rows, seconds).
- B. A Python query on `raw.eco2mix` that redoes the D12 filter and the source priority. No dbt on the critical
  path, but two copies of the cleaning rules that can drift apart.
- C. Read `clean.load_30min` as it is at 12:00. Simplest, but the table changes with every collection and
  every consolidated refresh, so a past forecast cannot be replayed.
Recommendation: A. It keeps one definition of the clean data and makes replay a single command.

### 1.3 Weather (D11)

Candidate files are staged in the morning: the 00 UTC run of D-1 (steps f021 to f047) and the 18 UTC run of
D-2 (steps f027 to f053) are downloaded into `raw.gfs_points` as soon as they appear. Staging selects nothing.
At F the D11 completeness test runs on the S3 `LastModified` of every needed step file and its `.idx`,
strictly before F, first on the 00 UTC run and then on the fallback run. The result is written to
`raw.gfs_days` with `cutoff = F`, `status`, `run_init_time`, `fallback`, `source_url` and `selection_hash`,
the table the backtest already uses.

Append-only storage keeps every revision; it does not by itself freeze what a run used. `raw.gfs_points`
is keyed by (run, step, variable, point, message hash), so a file republished later adds a second row for the
same run, step, variable and point, and `raw.gfs_days` accepts a second selection for the same delivery day.
The backtest reader (`weather_features.read_temperature`) joins points by run only and raises on either
ambiguity, so one later revision would make a past forecast unreplayable. Production therefore pins the
inputs of each run:
- Selection rule at F: for each needed (step, variable, point) of the selected run, the point row whose file
  and `.idx` `LastModified` are both strictly before F; if several revisions qualify, the one with the latest
  file `LastModified`; a tie, or a needed row with none, makes that GFS run fail the test, and the D11
  order continues (fallback run, then weather missing, P9). Rows with a `LastModified` at or after F never
  enter, whenever they were ingested.
- Pinning: the run stores the `(delivery_day, selection_hash)` of its `raw.gfs_days` row and the full list of
  point identities `(run_init_time, end_step, variable, point, message_hash)` it read
  (`forecast.run_weather`, section 4.1).
- Replay reads exactly the pinned identities, never "the selection of day D", so later selections and
  revisions cannot change it.
- Test: insert, after a run, a second revision of one of its point rows with a `LastModified` after F and a
  second `raw.gfs_days` row for the same day; the replay gives the same feature hash, and a new selection at
  the same F ignores the late revision. A second test inserts two revisions before F and checks that the
  latest one is selected.
The archive holds no such conflict today (zero duplicate selections and zero duplicate point revisions,
read-only queries of 2026-10-08), so this is a rule for a case the storage allows, not a fix of a past leak.

The current collector refuses to run before the cutoff (`weather.py` and `weather_collector.py`, see
`docs/orchestration.md` 1.1). Production needs that guard split in two: staging allowed at any time, selection
allowed only at or after F.

Frequency seen in the archive (counts of `raw.gfs_days` read on 2026-10-08, 2021-03-23 to 2026-10-04):
4 fallback days and 2 weather-missing days out of 2 022, none since 2024. The 00 UTC run is complete about
3.7 hours after its start (`docs/weather_sources.md`), more than 6 hours before F.

### 1.4 Calendar

Holidays, bridges, day of week and local slots are deterministic functions of D (checklist item 10).
The code commit in the run row fixes their definition.

## 2. Served model and daily updates

### 2.1 What is served

Candidate 1 of D18: the EXP-009 ML-Poly aggregate (one shared weight vector) of four experts, EXP-004
(linear with lags), EXP-005 (GAM), EXP-007 (GAM plus restart Kalman) and EXP-008 (LightGBM), with EXP-012
seasonal split-conformal 80 % intervals (pool: same local slot, same season, 365 days ending at D-2).
Every setting stays as chosen on 2023 (D13, D18). Each expert keeps its backup chain
(`scripts/expert_forecasts.py`): EXP-007 and EXP-008 fall back to EXP-005, EXP-005 to EXP-004, EXP-004 to
EXP-003, EXP-003 to the EXP-002 calendar model. After the chain every expert has a value for every slot.
EXP-002, then the seasonal naive forecast, is the late fallback of D16, prepared ahead (P10).

Evidence for this choice: 2024 MAE 828.5 MW (RTE* 896.7, raw RTE D-1 1364.6); 2025 single test MAE 808.8 MW,
significantly better than raw RTE D-1 and RTE*, interval coverage 81.1 % overall. Known weak spots: cold days,
holidays and the Christmas week (`docs/error_analysis.md`), and interval calibration by month.

### 2.2 Weekly refit (D6)

The models that forecast Monday to Sunday are fitted at the cutoff of the Monday, that is at the Sunday 12:00
freeze, on the expanding history from 2021-03-23 (`backtest.walk_forward`). The refit fits the 48 slot models
of EXP-002, EXP-003, EXP-004 and EXP-005 and the global LightGBM (fixed 6 400 rounds, fixed seed).

The Kalman variances of EXP-007 follow their own schedule, as in `kalman.walk_forward_kalman`. The noise
variance and the drift variances of each slot are fitted by maximum likelihood only at the first weekly
refit of each calendar quarter, on the days up to that refit minus two days whose consumption is known at
the refit cutoff, starting the optimizer from the previous quarter's parameters (warm start). They are then
held fixed until the next quarter. The optimizer stops after 100 iterations, and 3 of the 4 fits of 2024
stopped there (`data/experiments/EXP-007/results.json`, `variance_fits`), so the parameters depend on the
warm-start path: a fresh fit on the same data does not give them back. Production therefore stores the
full parameter array (48 slots by 1 noise and 4 drift log-variances) of every quarterly fit, with its
quarter, the refit day and the snapshot it used, and never refits them outside that schedule.

The fitted models and the variance array are logged in MLflow and each completed run points to them.

P4. When the refit runs.
- A. After the Sunday freeze, on the critical path of the Monday forecast. Exactly the backtest schedule.
  Risk: the refit must finish with the forecast before 12:30. Its duration is not measured yet; the EXP-009
  burn-in (four experts, 52 weekly refits plus a year of daily forecasts in about 17 minutes) suggests tens of
  seconds to a few minutes per refit, an indirect estimate only.
- B. Refit at the Saturday freeze and use it from Monday. Off the critical path, but every model sees one day
  less than in the backtest, so production differs from what was validated.
- C. A then B on failure: if the Sunday refit is not done by 12:20, serve the Monday forecast with last week's
  models and flag it.
Recommendation: A, with the C behaviour as the failure rule (section 6), after measuring the refit time.

### 2.3 Daily online updates (checklist item 7)

At the freeze for day D, three states move forward by one day, each with actuals of full local days up to
D-2 only. D-2 ends at 00:00 Paris on D-1, 12 hours before F, so it is inside the D2 window.
- Kalman (EXP-007, restart mode, `kalman.walk_forward_kalman`): at each weekly refit the filter is reset
  (zero correction, prior covariance) and rerun on the new GAM effects from the first window day
  (2021-03-23) to D-2, with the actuals known at that Monday's freeze. On each other day of the week it
  absorbs only the one new day D-2, with the actuals known at that day's freeze; days absorbed earlier in
  the week are not revised until the next restart. The state of day D is therefore a function of the
  Monday models, the quarterly variances, the Monday snapshot and the daily snapshots of the week, not of
  today's actuals alone. Each completed run stores the state after its update (correction and covariance
  per slot) and the snapshot identity (F and maximum `ingested_at`) of every day it absorbed, and a replay
  must reproduce that state from the same snapshots.
- ML-Poly (EXP-009): absorb day D-2. The regret of an expert uses its forecast for D-2 and the weights used to
  issue D-2. In production these are the stored values of the state run of D-2 (section 4.1), not values
  recomputed later: once the consolidated data arrive, a recomputed EXP-004 forecast for D-2 would differ
  from the one actually computed at F. Past updates are never revised. Each completed run stores the
  regret and the sum of squared regrets after its update: the normalized weights alone do not identify
  them (two states with the same weights can give different next weights).
- Conformal (EXP-012): the pool of slot s for D holds the residuals actual minus issued aggregate of slot s on
  the same-season days between D-366 and D-2, each with the latest actual ingested before F.

All three read actuals through the same as-of snapshot as the inputs, so a replay reproduces them.

### 2.4 Start of the online states

The ML-Poly regret starts on 2023-01-01 and the conformal pool of day D holds the residuals of the 365 days
ending at D-2, so production cannot start from nothing. Two constraints hold for every option: no forecast of a
2025 day is computed again (D18 allows one single test run), and the states carried over from 2025 are rebuilt
only from values the test read.

What the archive holds (folder `data/experiments/`, local and outside version control, listed on 2026-10-08):
- `EXP-009/experts_2023.csv`: the four expert forecasts of the 2023 burn-in, local days 2023-01-01 to 2023-12-31.
- `TEST-2025/experts_2024_2025.csv`: the four experts, 2024-01-01 to 2025-12-31. Its 2024 part equals the
  EXP-009 experts to 7.3e-12 MW (`results.json`, `reproduction_2024_max_abs_difference_mw`).
- `EXP-009/backtest_predictions.csv`: the 2024 aggregate.
- `TEST-2025/predictions.csv`: for 2025, the aggregate (candidate 1), EXP-007, the actual, raw RTE D-1 and RTE*.
- `TEST-2025/intervals.csv`: the 2025 intervals with point, actual and pool size.
- `TEST-2025/weights.csv`: the ML-Poly weights used on each day from 2023-01-01 to 2025-12-31.
- `TEST-2025/results.json` and the MLflow run of the test: metrics, protocol commit, SHA-256 of the two expert
  input files.

What is missing: the fitted models of every refit (OLS, GAM, LightGBM); the Kalman variance arrays
(`EXP-007/results.json` keeps only their medians for the four 2024 fits, nothing for 2025); the Kalman
correction and covariance; the ML-Poly regret and squared regret; the actuals of 2023 and 2024, which exist
only in the database.

Reconstruction allowed in every option (no model is run, only the arithmetic the test already did):
- ML-Poly: run `aggregation.aggregate` over the archived expert table from 2023-01-01 to 2025-12-31, with the
  2023 and 2024 actuals from the clean layer and the 2025 actuals from `TEST-2025/predictions.csv`. It is
  accepted only if the recomputed weights equal `TEST-2025/weights.csv` on every day and the aggregate equals
  `EXP-009/backtest_predictions.csv` (2024) and candidate 1 of `TEST-2025/predictions.csv` (2025) slot by slot
  to 1e-6 MW. A mismatch means the database actuals differ from those the runs read, and the start stops there.
  The regrets at the end of 2025 are then known exactly, because they are a deterministic function of inputs
  that were checked. I ran this check on 2026-10-08, reading the database only up to 2024-12-31: the
  recomputed weights equal the archived ones on all 1 096 days (largest difference 7.7e-15) and the aggregate
  equals the archived one to 4.4e-11 MW in 2024 and in 2025.
- Conformal: the pool of a 2026 day takes the 2025 residuals actual minus candidate 1 from
  `TEST-2025/predictions.csv`, then the residuals of the 2026 days forecast by production or replay.
The archive must be copied into the tested backups with these checksums before any of this is built: it is the
only copy of the 2025 state.

The models and the Kalman state of the first 2026 week are the open part. 2026-01-01 is a Thursday, so it
belongs to the week of Monday 2025-12-29, whose models the test fitted and did not save, and the Kalman
variances in use that week are the ones fitted at 2025-10-06, the first refit of the fourth quarter, also not
saved.

P2. Warm start.
- A. Fresh start at a Monday. The production period starts with the refit of Monday 2026-01-05 (freeze
  2026-01-04 12:00 Paris), fitted on the expanding history as every production refit is. 2026-01-05 is the first
  refit of the first quarter, so the Kalman variances are fitted at that refit, with a cold optimizer start
  (no previous parameters), which is how every backtest walk began (the EXP-007 walk and the test walk both
  started cold on 2024-01-01). 2026-01-01 to 2026-01-04 get no forecast: ML-Poly adds no regret and the pool no
  residual for these four days, the rule already used for days without actuals (section 6). Nothing is
  fitted at a 2025 cutoff and no 2025 forecast is computed. From 2026-01-05 on, every refit and every Kalman
  restart trains on the history including 2025, as any production refit must: 2025 is then training data for
  2026 forecasts, never an evaluation period (P3). Costs: the first-quarter variances differ from an
  uninterrupted warm-start chain, and a declared four-day gap.
- B. Exact chain on the test data. Besides the reconstruction above, refit without forecasting the models of
  Monday 2025-12-29 and the eight quarterly variance fits of the test walk (first refits of each quarter from
  2024-01-01 to 2025-10-06) on a clean layer built with `ingested_before` set to the start of the test run
  (P1), accepted only if its 2024 and 2025 actuals equal the archived ones. The replay can then start on
  2026-01-01 with the Monday model of that week and the true fourth-quarter parameters as warm start. No 2025
  forecast is computed, but models are fitted at 2025 cutoffs, and only the 2024 part of the chain can be
  checked (against the medians of `EXP-007/results.json`); the 2025 part has no archived value to compare
  with. Costs about eight extra refits.
- C. Cold start: uniform weights and no interval until each season has 9 production residuals. No
  reconstruction, but no interval for most of the first year and weights that relearn what 2023 to 2025
  already taught.
Rejected: replaying everything from 2023-01-01 with the production code. It computes every 2025 forecast
again, a second look at the locked test year that D4 and D18 forbid.
Recommendation: A. It needs no state that the archive does not hold, every reconstructed state is checked
against an archived output, and its only deviations (cold variance start, four-day gap) are the ones each
backtest walk had at its own start.

With A, the replay runs from 2026-01-05 to the day before go-live with the production code in replay mode.
The replayed days are stored with status `replay` and never reported as production results. Before the
collector ran on time, the ingestion filter of P1 cannot apply (the rows were ingested later), so replayed
days use the backtest rule (D2 only), the documented revision leak. The conformal pool becomes fully made of
production residuals one year after go-live.

P3. Status of 2026. The code locks every day after 2025-12-31 (`periods.last_allowed_day`). Production and
the replay of P2 must forecast 2026 days.
- A. Define the days from 2026-01-01 as a production period: forecasts and monitoring only, never used to
  choose or tune a model, with a separate entry point that keeps the experiment lock unchanged.
- B. Lift the lock for every code path. Simpler, but nothing then stops an experiment from reading 2026.
Recommendation: A.

### 2.5 Acceptance test before go-live

The production function, run in replay mode over 2024 with the 2023 burn-in, must reproduce the EXP-012
outputs: the four experts and the aggregate to 0.01 MW (1015.70, 911.85, 885.32, 1113.94 and 828.52) and the
intervals slot by slot to 1e-6 MW (`data/experiments/EXP-012/intervals.csv`). This proves that production runs
the validated model and not a re-implementation.

## 3. Real-time versus consolidated consumption

### 3.1 The problem

The models are trained and scored on consolidated consumption (protocol 2.2). In production, every recent
value is a real-time value: the three lags of D, the actuals of D-2 used by the three online states, and the
last weeks of every weekly refit. Consolidated values are said to arrive around the middle of the next
month (unverified, `docs/rte_benchmark.md` 3.2). The stored consolidated rows end on 2026-06-30
(`docs/revision_gap.md`). The API-call ledger `raw.eco2mix_api_calls` records requests, not what each
response contained, so it does not by itself prove that later months were unpublished; the consolidation
delay may be months, and it is not measured. Once weekly requests run (section 7.2, package 1), the first
ingestion of each month gives an upper bound on its publication date, not the date itself.
Today the stored overlap between the two vintages is zero timestamps (`docs/revision_gap.md`), so the gap
is not measured.

Why it matters: the EXP-004 lag coefficients sum to about 0.57, so a real-time minus consolidated gap of g MW
moves the lag-based experts by roughly 0.57 g. One reading of the RTE figures shows the stakes. RTE D-1 has a
mean error of about -1146 MW against consolidated consumption in 2024 and about +130 MW against real-time
consumption in July to early October 2026. If both figures reflected revisions only, the real-time values
would be about 1.3 GW below the consolidated ones, the forecast would move by about 700 MW, and the Kalman and
aggregation updates would then learn a mixture of the two scales. This is a hypothesis: the two periods do
not overlap and the difference may also come from the season or from changes at RTE.

The same issue decides what RTE* means in production: its correction is the prior-year mean error of RTE
against consolidated data (D15), so it is only meaningful when scored against consolidated actuals.

### 3.2 Options

P5.
- A. Keep the backtest rule: consolidated target, inputs and online updates on whatever vintage is known at F.
  No change, but an unknown systematic shift in production, which the backtest cannot show.
- B. Consolidated target, real-time inputs mapped to the consolidated scale: lag and online-update values are
  shifted by an estimated gap by month and local hour, estimated only on overlap stored before F.
  Inputs then match the training data. Needs the overlap, adds an estimate whose own error enters the
  forecast, and cannot be validated on 2024, which has no real-time vintage: it can only be judged in
  production, against A run in parallel.
- C. Real-time target: train on consolidated history mapped to the real-time scale and score against
  real-time actuals the next morning. Online updates become consistent with the target, but the history is
  then partly synthetic, and the comparison with RTE moves to a scale where RTE is nearly unbiased.
  A training history in real-time vintage becomes possible only after one or two years of collection.

Recommendation: measure before choosing. Request the consolidated dataset for July 2026 onward every week,
record the first date each month appears (an upper bound on the consolidation delay), and as soon as the
overlap exists, measure the mean and MAE of real-time minus consolidated by month and local hour, and the
implied forecast shift with the fitted lag coefficients at each lag timestamp (`docs/revision_gap.md`
section 2). Run production under A meanwhile, labelled "pre-revision-check".
If the implied mean forecast shift is significant by the block bootstrap and larger than a threshold I fix
before seeing the measurement, run B in parallel with A for at least eight weeks and keep the better one
on consolidated actuals. C stays a long-term option.

## 4. Storage and later scoring

### 4.1 Tables

A new schema `forecast`, insert-only like the raw tables: no row is ever updated or deleted, and the
production role has no UPDATE or DELETE privilege on it. A run's life is a sequence of immutable events, not
a row whose status changes.
- `forecast.run_starts`: written when a run begins, before any input is read. `run_id`, `delivery_day`,
  `freeze_time` (F, UTC), `kind` (`primary`, `fallback`, `replay`), `started_at`, `code_commit`.
- `forecast.run_results`: one row, written once, when a run completes, in the same transaction as all its
  output rows below. `run_id` (unique, references `run_starts`), `completed_at`, `status` (`complete`,
  `failed`), `refit_day`, `mlflow_run_id`, `kalman_quarter`, `weather_status`, `last_consumption_slot`,
  `max_ingested_at`, `feature_hash`, flags (backup slots per expert, stale models, interval not calibrated).
  A run that crashes has a start and no result; a run that completes after 12:30 has a result and no
  delivery. Outputs are visible only through a result row, so a half-written run is never read.
- `forecast.run_weather`: the pinned weather inputs of the run (section 1.3): the `selection_hash` and every
  `(run_init_time, end_step, variable, point, message_hash)` read.
- `forecast.values`: `run_id`, `value_time` (UTC), `local_slot`, `point`, `lower`, `upper`, `n_pool`.
- `forecast.experts`: `run_id`, `value_time`, `expert`, `value`, `backup` (the model that produced it).
- `forecast.states`: the online states after the run's update: ML-Poly `weight`, `regret` and
  `squared_regret` per expert; Kalman correction and covariance per slot; the list of absorbed days with the
  snapshot identity of each. A replay recomputes them and must find the same values.
- `forecast.fallbacks`: the fallback prepared ahead for a delivery day (P10), with `prepared_at`, the models
  it used and its completeness check.
- `forecast.choices`: insert-only, primary key `choice_hash`. One row per choice file the reconciler has read:
  `delivery_day`, `kind` (`primary`, `fallback`, `none`), `run_id` or `fallback_id`, `chosen_at`.
- `forecast.deliveries`: a mirror of the first choice the reconciler recorded for each day, never a decision
  maker. Primary key `delivery_day`, then `choice_hash`, inserted with `ON CONFLICT DO NOTHING`. It stays
  insert-only and is never updated.
- `forecast.delivery_incidents`: insert-only. When a mirror row and the choice file of the same day carry
  different `choice_hash` values, the reconciler inserts a row with the day, both hashes and the time. It
  changes nothing else.
- The serving authority. The decision "which forecast is served for day D" is not taken in PostgreSQL,
  because it must be taken when PostgreSQL is down. It is one file per delivery day in a local directory
  `serving/choice/`, `D.json`. The file either does not exist or is complete, because it is published in
  three steps:
  1. The writer writes the full JSON to a temporary file in the same directory (`D.<writer id>.tmp`), after
     the values file it names has itself been written, flushed and fsynced. The JSON holds `kind`, the id and
     content hash of the forecast, the path of the values file, `chosen_at` and its own `choice_hash`. The
     writer flushes and fsyncs the temporary file.
  2. It publishes with a no-replace rename: on Windows `os.rename(tmp, D.json)`, which fails if the target
     exists; on POSIX `os.link(tmp, D.json)`, which fails if the target exists, then unlink of the temporary
     file. The operating system lets exactly one writer succeed, and the winner's file is already complete.
  3. A writer that gets "already exists" deletes its own temporary file, reads `D.json` and obeys it.
  The choice is first-writer-wins and final: nothing deletes or rewrites `D.json`. Readers (API, offline
  reader, scoring, reconciler) ignore every `*.tmp` file, parse `D.json` and check its `choice_hash` against
  the content; a file that fails either check is an alert, never a served forecast. A cleanup run by the
  reconciler removes temporary files older than one hour.
  - The primary path. When a run has committed its result row and outputs to the database, it first writes
    its values file (write, fsync, rename), then, only if the local clock is before the 12:30 deadline of D,
    publishes the choice file naming itself. It does not wait for the database after the commit, so a
    database failure at that point does not matter.
  - The fallback path. The deadline check at 12:30 publishes a choice file naming the prepared fallback
    file (P10), without opening the database. If `D.json` already exists it does nothing but read it. If no
    fallback file exists, it publishes a choice of kind `none`: the unmet guarantee is recorded and alerted,
    and a run that completes later is stored and used for the online states, never served.
  - A paused or crashed writer. The primary writes its temporary file at 12:29:59 and then pauses or dies
    before the rename. No `D.json` exists, so nobody can read a partial choice. At 12:30 the fallback
    publishes its own complete file and wins; the fallback is served. If the primary wakes up later, its
    rename fails because the target exists, it deletes its temporary file and obeys `D.json`. If it never
    wakes up, its temporary file is ignored by every reader and removed by the cleanup. If instead the
    primary paused after the rename, `D.json` is complete and the fallback finds it. In every ordering one
    complete choice exists and one forecast is served.
  - Who obeys it. The API reads the choice file of D and serves the values file it names, so it serves the
    same forecast with the database up or down. The offline reader (a script that prints or copies the served
    forecast) does the same.
  - Scoring and online updates. The scoring reads the choice file of D, takes its `choice_hash`, and selects
    the served forecast from `forecast.choices` by that hash, never by the existence of a `forecast.deliveries`
    row. It refuses to score a day whose hash has no `forecast.choices` row yet. The online updates take their
    runs from `state_runs` below, which does not depend on what was served.
  - The reconciler. A step started by the scheduler, and at every database reconnection, lists the choice
    files, inserts into `forecast.choices` every choice not yet there (with the `forecast.fallbacks` or run
    row it names if absent), and inserts the `forecast.deliveries` row if the day has none. If the day's
    mirror row has a different `choice_hash` from the file, it inserts a `forecast.delivery_incidents` row.
    Conflict case: the mirror holds a primary row (for instance from an older reconciler pass) while the
    file says fallback. The mirror row stays as history, the incident is logged, the choices table holds
    both hashes, and scoring, the API and the online updates all select by the file's `choice_hash`, so
    the fallback is served and scored and exactly one forecast is counted for the day.
  - Durability. The directory sits on the same disk as the fallback files, is part of the backup, and is not
    in the repository. It is the single point of failure of the serving guarantee, which is why a whole-PC
    outage stays an unmet guarantee (section 6). Atomic no-replace publication and fsync must be checked on
    the deployment filesystem before go-live.
- `forecast.state_runs`: primary key `delivery_day` alone, so that two runs can never both feed the same day.
  The run whose experts and weights feed the online states for that day: the first completed primary run of D
  at the same F, delivered or not, because its values were computed with the information of F exactly as in
  the backtest. It is chosen atomically inside the run's own result transaction: `INSERT ... ON CONFLICT
  (delivery_day) DO NOTHING`. If two runs complete at once, the second transaction waits for the first, finds
  the key taken and inserts nothing, so the choice is the first transaction to commit and no check for
  absence is run before the insert. A day without any completed primary run has no state run, and the states
  skip it.
- `forecast.scores`: `delivery_day`, `actual_vintage` (`realtime`, `consolidated`, `definitive`),
  `actual_max_ingested_at`, `scored_at`, then for the served forecast, the primary model, raw RTE D-1 and RTE*:
  n slots, MAE, RMSE, MAPE, daily mean error, coverage and width. A day is scored again, in a new row, each time
  a newer vintage arrives.

The API serves the run or fallback named in the choice file of the day (`docs/api.md`) instead of refitting on
each request. The headline score (P6) follows the choice file's `choice_hash` through `forecast.choices`; the model score and every online update follow `forecast.state_runs`. The choice file is the only
place where "which forecast was served" is decided and `state_runs` the only place where "which run feeds the
states" is decided, so the API, the scores and the states cannot disagree on either.

### 4.2 Scoring

Scoring reuses the evaluation module (`evaluation.score`, `daily_mae`, `compare_daily`, `mae_by_day_type`)
on the common slots of the model, the actual and RTE D-1, as in the backtest.
- Provisional score, the morning of D+1, on real-time actuals: served forecast, primary model and raw RTE D-1.
  RTE* is not computed here, because its correction was estimated against consolidated data (section 3.1).
- Final score, after each consolidated refresh, on consolidated actuals: served forecast, primary model, raw
  RTE D-1 and RTE* with the prior calendar year's mean error by local hour (2025 for 2026 deliveries,
  `rte_reference.prior_year_hourly_reference`). In early January the previous December may not be consolidated
  yet; RTE* is then reported as missing, the limitation already written in that module.
- Day types use the D9 labels (latest stored SYNOP version), once the labels are collected.
- Comparisons with RTE use the paired daily difference and the 7-day block bootstrap (D10), on periods long
  enough to mean something (a quarter or more).

P6. Official production score.
- A. The forecast actually served at 12:30, fallbacks included. This is what a user received.
- B. The primary model only. Measures the model, hides operational failures.
Recommendation: A as the headline, B next to it, with the number of fallback and late days.

## 5. Drift monitoring

The weekly refit and the three online states already adapt to slow change. Monitoring answers a different
question: is the system still behaving like the backtest said it would? An alert opens a review; it never
changes the model automatically. A model change is a new experiment.

What to watch, from the most to the least important:
1. Level shift of the error. The drifts that hurt load forecasts are persistent level changes: the 2022 to
   2023 consumption drop, a real-time versus consolidated offset, a change of perimeter at RTE.
2. Relative skill against RTE D-1, which faces the same weather and calendar, so a hard month for both does
   not raise an alert.
3. Interval coverage.
4. Inputs: GFS temperature forecast error against the SYNOP national mean, consumption ingestion delay of the
   09:30 slot, number of backup slots per expert, change in the aggregation weights.

P7. Statistic for item 1.
- A. Two-sided CUSUM (Page, 1954) on the standardized daily mean error. For day d, e_d is the mean of actual
  minus served forecast over the slots of d, and z_d = (e_d - mu_s) / sigma_s, where mu_s and sigma_s are the
  mean and standard deviation of e_d in the reference period for the season s of d (winter errors are larger,
  so one sigma for the whole year would make winter alarms more likely than summer ones). The two
  recurrences are
      S+_d = max(0, S+_(d-1) + z_d - k),    S-_d = max(0, S-_(d-1) - z_d - k),
  with S+ = S- = 0 on the first scored day, k = 0.5 (aimed at a shift of one sigma), and an alarm when
  S+_d > h or S-_d > h. After an alarm the alert is logged, a review opens, and both sums restart at 0 the
  next day. Missing days: a day with fewer than 40 scored slots, or with no delivered forecast, does not
  update the sums (they carry over unchanged) and is counted; more than 7 consecutive missing days restart
  both sums at 0 with a log line, because evidence that old no longer describes the current state.
  Target: the in-control yearly false-alarm probability, P(at least one alarm in 365 scored days when nothing
  has changed). I fix its value before calibration; my proposal is 0.2, about one false review every five
  years. The threshold h is the smallest value that meets it.
  Calibration of h: textbook CUSUM tables assume independent days, but daily errors are autocorrelated
  (weather episodes, holidays), so those tables give too many false alarms. I simulate in-control years
  instead, by a seasonal block bootstrap of the reference standardized series: each simulated year is
  built from consecutive blocks of b days, each block drawn from the reference years at the same calendar
  position (a start day within 15 days of the target date), so the seasonal mean and variance and the
  dependence inside a block are kept. h is set with b = 7 days (D10), and the same calibration is repeated
  with b = 14 and b = 28. The reported h is the largest of the three. Because blocks keep dependence only
  up to their length, I also report the autocorrelation of z_d up to lag 60 and the ratio of its long-run
  variance (Bartlett estimate) to the variance retained by each block length; if the 28-day blocks still
  retain much less, the false-alarm probability is reported as uncertain and probably underestimated. These
  variance diagnostics are a caution, not a certified bound: they do not prove that the true alarm
  probability is above or below the simulated one.
  Until this evidence is computed and written, the design makes no claim about the false-alarm rate.
  Reference period: the daily errors of the aggregate in 2023 (burn-in, recomputed from the archived experts
  by the reconstruction of section 2.4, no model run; 2023 is also the tuning year of every setting, D13, so
  its errors are slightly optimistic), 2024 (EXP-009, out of sample) and 2025 (test run, archived). Using
  2025 here is a monitoring setting, not a model choice: it changes no reported 2025 number, but the 2025
  errors are no longer out of sample for the threshold, and I write that next to h. Three reference years
  per calendar position is thin, which is one more reason to report h for all three block lengths.
- B. Rolling 28-day mean error against a fixed band. Easier to explain, but it forgets evidence older than the
  window and reacts to a shift only when a large part of the window has moved.
- C. Rolling 28-day MAE against a fixed threshold. Seasonal: MAE is higher every winter, so a single threshold
  either alerts every winter or misses a summer drift.
Recommendation: A for item 1, the only statistically calibrated alert of this section once the
calibration above is done. Items 2 to 4 are operational bands: fixed rules chosen to be readable, with no
claimed false-alarm rate.
- Item 2: the 28-day mean of the paired daily MAE difference served forecast minus raw RTE D-1 on the same
  actual vintage, alert when it is above zero: the model losing to RTE for four weeks is the failure that
  matters.
- Item 3: the 8-week coverage of the 80 % interval, alert outside the 2.5 % to 97.5 % range of the same
  rolling statistic over 2024 and 2025. This range is a marginal band of overlapping windows, not a
  sequential false-alarm rate. Monthly coverage already ranged from 58.5 to 91.7 % in 2024 (EXP-012), so a
  narrow band around 80 % would alert constantly.
- Item 4: ingestion delay of the 09:30 slot above 90 minutes on 3 days of a week, any weather-missing day,
  any expert on backup for a whole day, except the days whose D-2 or D-7 lag falls on a spring change day
  (section 6).

Thresholds are fixed and written before go-live and never tuned on production data. Until the real-time
versus consolidated gap is measured (section 3), the CUSUM runs on consolidated actuals only, because the
reference mu_s is on that scale; a CUSUM on real-time actuals would alert on the revision gap itself.
The cost is delay: if consolidation takes months (section 3.1), a drift is detected months late. Meanwhile
the real-time CUSUM is computed and shown, but it does not alert.

## 6. Failure cases

| Case | Detection | Behaviour | Flag |
|------|-----------|-----------|------|
| 00 UTC GFS run incomplete at F | D11 test at F | 18 UTC run of D-2, same test | `weather_status = fallback` |
| Both runs incomplete | D11 test at F | backup chain (P9) | `weather missing`, interval not calibrated |
| 09:30 slot or another lag missing at F | lag features NaN | backup chain (P8) | backup slots per expert |
| Actuals of D-2 missing | as-of snapshot | ML-Poly adds no regret for those slots, Kalman and conformal use what exists | count of missing slots |
| Sunday refit not finished by 12:20 | run timer | last week's models, Monday refit retried after delivery | `stale_models` |
| Forecast not ready at 12:30 | deadline check (P10) | publish the choice file naming the fallback prepared the day before; the late run is stored, never served | delivery `kind = fallback` |
| Main flow or Prefect down at 12:30, PostgreSQL up | deadline check (P10), started by Windows Task Scheduler | same: the choice file names the prepared fallback; the reconciler inserts the delivery row | delivery `kind = fallback` |
| PostgreSQL down at 12:30, PC up | the deadline check does not need the database: it creates the choice file, or finds one | if the choice file already names a primary (committed and claimed before 12:30), that primary is served from its values file and nothing else; otherwise the fallback file is served. Either way one forecast is served, and the reconciler loads the choice into `forecast.deliveries` when the database is back; a conflicting mirror row is an incident and the file's `choice_hash` decides | incident logged |
| Prepared fallback absent or incomplete at 12:30 | preparation alert the day before, then the deadline check | choice file of kind `none`: no delivery, an unmet guarantee, never silently replaced by a late run | `no delivery` |
| Whole PC down at 12:30 | nothing runs | no delivery: an unmet delivery guarantee, logged when the PC is back; a run with the same F may then be computed for the online states (inputs are replayable), never served | `late`, no delivery |
| Spring change day | 46 slots | forecast every slot | none |
| Days whose D-2 or D-7 is the spring change day (2024-04-02 and 2024-04-07 for 2024-03-31) | the lag of a 02:xx slot does not exist, so `lag_model.lag_features` makes the whole day NaN | backup chain (EXP-004 to EXP-003, the others as in section 2.1), as in the backtest | backup slots, not a drift alert |
| Autumn change day | 50 slots | forecast every slot (D12) | none |

P8. Missing last consumption value. Today a single missing lag sends the whole day of every expert down to
EXP-003 (calendar plus temperature, 2024 MAE 1719 MW against 829 for the aggregate).
- A. Keep this rule: it is identical in backtest and production and already tested.
- B. Use the latest slot ingested before F and before 10:00, plus its age as a feature. Uses more of what is
  known, but it is a model change: a one-change experiment chosen on 2023 first.
- C. Seasonal naive for that day. Simpler and worse.
Recommendation: A, and measure how often it happens through the ingestion delays before deciding on B.

P9. Weather-missing day. In the code, every expert ends on EXP-002 on such a day, because the EXP-004 backup
(EXP-003) also needs weather. The modeling plan M11 option A describes a weather-free model with calendar and
lags, which does not exist.
- A. Keep the code (calendar only, 2024 MAE 2871 MW) and correct the M11 wording. Two days in five and a half
  years, so the expected cost is a few MW a year.
- B. Build the calendar plus lags model that M11 describes and put it before EXP-002 in every chain. Closer to
  M11, but a new model for about one day in a thousand.
Recommendation: A. On these days the interval is flagged as not calibrated, because the conformal pool
contains residuals of the aggregate, not of the backup model.

P10. Deadline enforcement. D16 serves a fallback (EXP-002 calendar model, then seasonal naive) when the
forecast is not ready at 12:30. A fallback computed at 12:30, or after weather selection and the dbt
snapshot of the main flow, can miss the deadline itself or contain NaN (the seasonal naive value is missing
whenever the slot seven days earlier is). The fallback must therefore be prepared ahead, complete, and
served by a step that computes nothing.
- A. Prepared fallback, bounded serving. The deployment `prepare_fallback` runs at 13:00 Paris on D-2 for day
  D, with a second attempt at 06:00 on D-1 if no fallback is stored yet. It reads only the stored EXP-002
  models of the latest completed refit and the clean layer as it is then; it does not depend on weather
  selection, on the snapshot of D or on the main flow of D. Per slot of D (46, 48 or 50 slots): the EXP-002
  forecast; else the seasonal naive value; else the mean of the same local slot over the last four same
  weekdays available. A value is accepted only if finite and inside a plausibility range fixed before
  go-live from the 2021 to 2025 minimum and maximum of consumption. It is complete only when every slot of
  D has an accepted value; it is then stored in `forecast.fallbacks` and written atomically (write then
  rename) to a local file. The completeness check runs at preparation, so a failure is known about 23 hours
  before it matters and raises an alert. Preparation can itself fail (no stored model, a slot without an
  accepted value, a full disk), so a working preparation on the dry week is a go-live check (7.2), not an
  assumption. At 12:30 the deadline check, started by Windows Task Scheduler and not by Prefect so that a
  Prefect outage does not stop it, only reads local files and creates the choice file of section 4.1
  (exclusive create, first writer wins). It does not open the database, so its duration does not depend on
  it. The reconciler inserts the delivery row afterwards. The race of 12:29:59: the primary has committed,
  written its values file and created `D.json` at 12:29:59; the database then becomes unreachable. At 12:30
  the deadline check tries to create `D.json`, gets "already exists", reads that the primary was chosen and
  serves the primary values file. One forecast is served. When the database is back, the reconciler inserts
  the primary into `forecast.deliveries`; the prepared fallback stays stored as unused. Had the deadline
  check created the file first, the primary's claim would have failed, the primary would stay stored for the
  online states and never be served, and the fallback would be the single served forecast. Interval: none on
  fallback days, flagged, because the conformal pool holds residuals of the aggregate.
- B. A timeout on the main flow only, falling back inside the same process. Simpler, but a crash of the
  process or of Prefect also kills the fallback, and the fallback still depends on the steps that failed.
- C. A serving path off this PC (for example the prepared fallback copied every day to a hosted service).
  The only option that survives a whole-PC outage, but it adds an external service, credentials and costs
  outside the scope of this project.
Recommendation: A. It meets D16 for every failure of the main flow, of Prefect or of PostgreSQL, provided
the fallback preparation of the day before succeeded (the alert and the go-live check cover this; if it
failed, the day ends with a `none` choice). It does not meet it when the PC itself is down at 12:30: in this single-machine setup that is an unmet delivery
guarantee, recorded as such, and only C would remove it.

## 7. Prefect flow and build order

### 7.1 Deployments (Prefect, run natively on Windows, D17)

All schedules are cron expressions in the Europe/Paris time zone, so they follow the clock changes.

| Deployment | Schedule (Paris) | Steps |
|------------|------------------|-------|
| `collect_consumption` | every 30 min, plus 11:55 | append real-time eCO2mix rows with `ingested_at` |
| `stage_weather` | every 30 min, 05:30 to 11:30 | download candidate GFS steps, no selection |
| `prepare_fallback` | 13:00 for D+2, retry 06:00 for D+1 | complete fallback stored in `forecast.fallbacks` and in a local file (P10), alert if incomplete |
| `daily_forecast` | 12:00 | start event (F) -> weather selection with pinning (D11, section 1.3) -> clean snapshot (P1) -> Sunday only: refit and log to MLflow; first refit of a quarter: Kalman variances -> Kalman, ML-Poly and conformal updates to D-2 from the state runs -> four experts with backups -> aggregate -> intervals -> one transaction: result row, outputs, states, `state_runs` row if none -> values file and claim of the choice file if before 12:30 |
| `deadline_check` (Windows Task Scheduler) | 12:30 | create the choice file with the prepared fallback if none exists, without the database (P10), alert |
| `reconcile_deliveries` | every 10 min and at each database reconnection | insert the missing `forecast.deliveries` rows from the choice files, report any disagreement |
| `score_and_monitor` | 08:00 | provisional scores on real-time actuals, final scores where consolidated data arrived, drift statistics, alerts |
| `consolidated_refresh` | weekly until the delay is known, then monthly | collect consolidated data, rebuild the clean layer, final scores |

Retries: collectors retry with delay; `daily_forecast` does not start a new attempt past 12:20, because the
deadline check takes over; a run that completes later is stored and used for the online states, never served. Every production run is logged in a separate MLflow experiment, with the delivery day and status
as tags.

### 7.2 Build order

Each package ends with a test or a measurement, and the leakage-sensitive ones (2, 3, 5) are reviewed by
someone other than their author.

| # | Package | Role | Done when |
|---|---------|------|-----------|
| 1 | Weekly consolidated requests from July 2026, then measure the revision gap (P5) | engineering, then science | first publication date per month; gap by month and local hour with bootstrap intervals, implied forecast shift |
| 2 | Split weather staging from selection; consumption collector every 30 min; ingestion delay report | engineering | selection refused before F; delay distribution of the 09:30 slot |
| 3 | As-of clean snapshot (P1) | engineering | a truncation test: snapshot at F equals the backtest features when every row was ingested before F |
| 4 | `forecast` schema, insert-only, with tests | engineering | insert-only roles; replay of one run from the tables; the weather pinning tests of section 1.3; a race test where the main flow and the deadline check claim the choice file at the same moment and exactly one forecast is served, with the database up and with it stopped between the primary commit and 12:30, a writer killed between its temporary file and the rename, and a mirror row that disagrees with the choice file; a test that two runs completing together leave one `state_runs` row |
| 5 | Production forecast function with stored states and the replay mode (P2, P3) | science | reproduces EXP-012 on 2024 (section 2.5) |
| 6 | Warm start to the go-live day (P2) | science | the reconstruction of section 2.4 matches the archived weights and aggregates; the states on go-live equal a second full replay from the same reconstructed checkpoint with the same snapshots |
| 7 | Prefect deployments, fallback preparation and the deadline check (P10) | engineering | a dry week in shadow mode with every flag exercised, including a killed main flow, a stopped Prefect server, an unreachable database at 12:30, and a failed fallback preparation (the alert fires and the day ends with a `none` choice); the preparation succeeding on every day of the week is a go-live check |
| 8 | Scoring and drift monitoring, thresholds fixed (P7) | science | h written before go-live with its simulated false-alarm probability for 7, 14 and 28-day blocks, the autocorrelation and long-run variance evidence, and the role of 2025 stated |
| 9 | API reads stored forecasts; dashboard of runs, scores and drift | engineering, then exploration | API tests on stored runs; dashboard pages for the last 90 days |

Packages 1 and 2 can start now. Package 5 needs 3 and 4. Go-live needs 1 to 8; until P5 is decided, the
production numbers are reported as provisional.

## 8. Limits of this design

- Production inherits every limit of `docs/error_analysis.md`: cold days, holidays, the Christmas week, monthly
  interval calibration, settings tuned on a favourable 2023.
- 2024 and 2025 have both been looked at. A future model change needs a validation period that nobody has
  seen, which can only be production data scored after a pre-registered change. This document does not
  define that process.
- Delivery rests on one PC: a whole-PC outage at 12:30 is an unmet delivery guarantee (P10).
- The ingestion time is the collector's clock on one PC. The Windows clock must stay synchronised; a clock
  error of minutes near 12:00 could move a slot in or out of the snapshot.
- The refit duration, the real publication delay and the revision gap are not measured yet. Three of the
  recommendations (P4, P5, P8) depend on them.

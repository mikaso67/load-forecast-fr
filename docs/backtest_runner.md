# Backtest runner

`src/load_forecast/backtest.py` exposes `walk_forward` for batch point forecasts.
Pass a unique UTC consumption Series, inclusive delivery dates, and a factory
that creates a fresh fit/predict model. The runner creates 48 independent models,
one per Europe/Paris wall-clock half-hour. Both autumn occurrences use the same
slot model. Every delivery grid contains all 46, 48 or 50 UTC slots.

Fits use an expanding window from 2021-03-23. A Monday delivery forecast is fitted
at its Sunday noon cutoff; subsequent days reuse that fit. A midweek start fits
using the preceding Monday delivery cutoff. Predictions record both the refit
delivery date and its UTC cutoff. Training targets obey D2 and optional strict
publication filtering. Missing training features or targets are excluded from
fit, with no imputation.

`LFF_N_JOBS` controls the number of workers for the 48 independent slot fits.
It defaults to 1; set `$env:LFF_N_JOBS = '6'` in PowerShell to use six joblib
threads. Only fitting runs concurrently: feature construction, cutoff filtering
and the cache stay on the calling thread. Each factory must create an independent
model without shared mutable state. Invalid or nonpositive worker counts fail
clearly. Threads retain the serial process's numerical-library settings; speed
depends on the model, hardware and native library thread count.

The feature builder receives `(slots, known_consumption, delivery_day)` and must
return a DataFrame on exactly that delivery grid. Historical feature rows are
built at their own day-ahead cutoffs, then cached during the run. Target features
use that target day's cutoff. `forecast_day` is the same entry point for production
and backtesting. Builders must be pure and use only the supplied history and
deterministic calendar information. They must not capture unrestricted inputs,
read databases, or fit encoders. Preprocessing belongs inside each model's fit.
The runner cannot prevent arbitrary callback code from accessing external data.
Weather integration requires a reviewed builder that preserves D11 publication
provenance; the initial builders cover calendar features and the UTC seven-day lag.

`SeasonalNaive` with `naive_features` adapts the EXP-001 reference to fit/predict.
`score_backtest` uses only evaluation.py for common-slot metrics, exclusions,
day types and both bootstrap block lengths. It evaluates against raw RTE D-1 and,
when supplied, the D15 bias-corrected reference RTE*. `score_backtest` does not take
cold labels and reports cold MAE as unavailable; SYNOP cold-day MAE is computed in the
experiment scripts, which pass the cold map to `evaluation.mae_by_day_type`
using D9 observed-temperature labels. `log_backtest` logs required tags, metrics,
day types and bootstrap settings to the existing local MLflow service.

The runner refuses dates in 2025 or later by default. Setting `final_test=True`
unlocks delivery dates through 2025-12-31 for the pre-registered test run;
dates in 2026 or later remain locked. Consolidated history retains the accepted
historical revision limitation. No hyperparameters are tuned here.

For the real EXP-001 reproduction, with the existing infrastructure running:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ..\load-forecast-fr-venv\Scripts\python.exe scripts/check_backtest.py
```

The reproduction succeeded on 2026-10-05. It reads the configured clean relation from
2021-03-22 23:00 UTC (March 23 local midnight) to 2024-12-31 23:00 UTC exclusive.
It reproduces EXP-001: model MAE 3249.409986 MW, RTE MAE 1364.642450 MW on
17564 common slots. The four exclusions and both bootstrap intervals match
the original experiment. No test-year observations are read.

The script confirms the local MLflow run is FINISHED and saves ignored
`data/experiments/EXP-001/backtest_results.json` and `backtest_predictions.csv`.
The CSV retains refit delivery dates and UTC cutoffs for all 17568 target slots.
Verified MLflow run: `02e85d3049ec43ad9913833fbb2f9827`, tagged exp=EXP-001, backtest_start=2024-01-01, backtest_end=2024-12-31.
Synthetic tests verify daily prediction equality with evaluation.seasonal_naive
over all of 2024, fitted-model truncation invariance, refit boundaries, and DST.

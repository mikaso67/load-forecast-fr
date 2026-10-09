# load-forecast-fr

I forecast French national electricity consumption one day ahead at 30-minute resolution.
I evaluate every model against RTE's official D-1 forecast on identical timestamps.

## 1. The Question

I forecast French national electricity consumption one day ahead at 30-minute resolution.
I evaluate whether statistical and machine learning models beat RTE's official D-1 forecast on identical timestamps.

## 2. Results

I evaluate all experiments on the 2024 validation year. Only the two preselected milestone candidates (D18) are evaluated on the locked 2025 test year.
All metrics are computed on common timestamps where the model, raw RTE D-1, and actual consumption are present.
RTE* a2 is the leak-free corrected benchmark that removes the prior calendar year's mean bias by local hour.
Significance is evaluated using paired daily MAE differences with a 7-day moving block bootstrap.

### 2024 Validation Year

Common slots: 17,564 for EXP-001 (excluding two autumn clock-change slots on 2024-10-27 at 00:00 and 00:30 UTC, plus two missing-lag slots on 2024-11-03 at 00:00 and 00:30 UTC); 17,566 for EXP-002 through EXP-012 (two autumn clock-change slots excluded on 2024-10-27 at 00:00 and 00:30 UTC).
Benchmarks on 2024: raw RTE D-1 achieves MAE 1364.6 MW (RMSE 1701.6 MW, MAPE 2.73%). RTE* a2 achieves MAE 896.7 MW (RMSE 1171.3 MW, MAPE 1.79%).

| Experiment | Model description | Model MAE (MW) | Model RMSE (MW) | Model MAPE (%) | Difference vs reference (MW) | 7-day 95% Bootstrap CI (MW) | Significance verdict |
|---|---|---|---|---|---|---|---|
| [EXP-001](docs/experiments/EXP-001.md) | Seasonal naive (same UTC half-hour 7 days earlier, D2 lag) | 3249.4 | 4860.3 | 6.1% | +1884.7 (vs raw RTE) | [+1219.5, +2640.0] | significant (worse) |
| [EXP-002](docs/experiments/EXP-002.md) | Linear regression per local slot (calendar features only) | 2871.2 | 4061.6 | 5.5% | -378.3 (vs EXP-001) | [-1099.8, +81.3] | not significant |
| [EXP-003](docs/experiments/EXP-003.md) | Linear regression per local slot (calendar plus HDD/CDD) | 1719.1 | 2277.8 | 3.4% | -1152.0 (vs EXP-002) | [-1562.7, -610.8] | significant (better) |
| [EXP-004](docs/experiments/EXP-004.md) | Linear regression per local slot (calendar, HDD/CDD, lags D-2, D-7, last) | 1015.7 | 1388.4 | 2.0% | -703.3 (vs EXP-003) | [-843.2, -519.8] | significant (better) |
| [EXP-005](docs/experiments/EXP-005.md) | GAM per local slot (smooth GFS temperature plus lags) | 911.9 | 1290.5 | 1.8% | -103.8 (vs EXP-004) | [-138.7, -75.3] | significant (better) |
| [EXP-006](docs/experiments/EXP-006.md) | GAM per local slot (smooth temperature, 96h smoothed temperature, lags) | 907.7 | 1305.6 | 1.8% | -4.1 (vs EXP-005) | [-39.9, +24.1] | not significant |
| [EXP-007](docs/experiments/EXP-007.md) | GAM per local slot plus online Kalman filter (restart mode) | 885.3 | 1293.7 | 1.7% | -26.5 (vs EXP-005) | [-86.0, +25.4] | not significant |
| [EXP-008](docs/experiments/EXP-008.md) | Global LightGBM (L1 objective, slot feature) | 1113.9 | 1547.4 | 2.2% | +202.2 (vs EXP-005) | [+120.4, +292.4] | significant (worse) |
| [EXP-009](docs/experiments/EXP-009.md) | ML-Poly online aggregation of 4 experts (shared weights) | 828.5 | 1207.6 | 1.6% | -56.8 (vs EXP-007) | [-83.4, -18.7] | significant (better) |
| [EXP-010](docs/experiments/EXP-010.md) | Rolling split-conformal 80% intervals (N = 182 days) | 828.5 | 1207.6 | 1.6% | coverage 78.5% [73.9, 83.9] | mean width 2551 MW | winter coverage 58.7% |
| [EXP-011](docs/experiments/EXP-011.md) | Adaptive conformal inference (gamma = 0.001) | 828.5 | 1207.6 | 1.6% | coverage diff -0.44 pts (vs EXP-010) | [-0.69, -0.22] pts | statistically detectable (decrease) |
| [EXP-012](docs/experiments/EXP-012.md) | Seasonal group (Mondrian) split-conformal (365-day pool) | 828.5 | 1207.6 | 1.6% | coverage diff +1.57 pts (vs EXP-010) | [-1.96, +4.71] pts | not significant |

Benchmark comparisons on 2024:
- EXP-004 vs raw RTE: -348.8 MW, 7-day 95% CI [-470.5, -233.2] MW, significant (better); vs RTE*: +119.1 MW, [+28.1, +204.5] MW, significant (worse).
- EXP-005 vs raw RTE: -452.7 MW, 7-day 95% CI [-579.0, -336.8] MW, significant (better); vs RTE*: +15.2 MW, [-73.4, +95.8] MW, not significant.
- EXP-007 vs raw RTE: -479.2 MW, 7-day 95% CI [-602.8, -365.0] MW, significant (better); vs RTE*: -11.3 MW, [-108.8, +79.1] MW, not significant.
- EXP-008 vs raw RTE: -250.5 MW, 7-day 95% CI [-365.5, -139.3] MW, significant (better); vs RTE*: +217.4 MW, [+109.2, +318.8] MW, significant (worse).
- EXP-009 vs raw RTE: -536.0 MW, 7-day 95% CI [-648.8, -427.1] MW, significant (better); vs RTE*: -68.1 MW, [-152.3, +14.6] MW, not significant on primary 7-day block (14-day check is significant: [-160.3, -12.6] MW).
- EXP-012 interval calibration: overall coverage is 80.1% [76.1, 84.5] with mean width 2641 MW. Seasonal coverages are winter 79.0%, spring 83.6%, summer 81.0%, and autumn 76.9%. Mean absolute distance of the four seasons to 80% is 2.2 points, compared to 9.5 points in EXP-010.

### 2025 Test Year

The test year 2025 was evaluated in a single locked run (TEST-2025) on 17,518 common slots (two autumn clock-change slots excluded on 2025-10-26 at 00:00 and 00:30 UTC).
Candidate 1 is the EXP-009 ML-Poly aggregation with EXP-012 seasonal conformal intervals.
Candidate 2 is the EXP-007 GAM plus Kalman model.
RTE* a2 uses the 2024 hourly bias table.

| Model / Benchmark | MAE (MW) | RMSE (MW) | MAPE (%) | Mean error (MW) |
|---|---|---|---|---|
| Candidate 1 (EXP-009 aggregate + EXP-012 intervals) | 808.8 | 1127.6 | 1.55% | +9.1 |
| Candidate 2 (EXP-007 GAM + Kalman) | 869.3 | 1239.2 | 1.67% | +30.1 |
| RTE* a2 (2024 hourly bias) | 942.6 | 1213.9 | 1.87% | +172.8 |
| Raw RTE D-1 | 1325.0 | 1658.5 | 2.67% | -973.5 |

Pairwise daily MAE differences on 2025 (7-day moving block bootstrap, 10,000 draws):
- Candidate 1 vs raw RTE: -516.1 MW, 95% CI [-645.9, -409.8] MW, significant (better).
- Candidate 1 vs RTE*: -133.8 MW, 95% CI [-210.5, -64.0] MW, significant (better).
- Candidate 2 vs raw RTE: -455.6 MW, 95% CI [-594.1, -346.5] MW, significant (better).
- Candidate 2 vs RTE*: -73.2 MW, 95% CI [-160.2, +1.2] MW, not significant (14-day sensitivity check [-177.6, -4.4] MW is significant: borderline).
- Candidate 1 vs Candidate 2: -60.5 MW, 95% CI [-85.8, -27.8] MW, significant (better).

Candidate 1 interval coverage on 2025:
- Overall 80% coverage: 81.1% (7-day 95% CI [77.5, 84.9]%), mean width 2670 MW.
- By season: winter 82.7%, spring 81.3%, summer 81.6%, autumn 78.9%.
- By day type: ordinary 83.7% (316 days), cold 71.2% (31 days), holiday 41.9% (11 days).

## 3. What I Take From It

I beat raw RTE D-1 significantly with every model that includes consumption lags.
I beat the leak-free RTE* benchmark on the 2025 test year with the online aggregate (-133.8 MW, significant).
I find that combining specialized experts (GAM, Kalman, LightGBM) outperforms any single model.
I achieve near-target 80% interval coverage with seasonal Mondrian conformal prediction, but holidays and cold spells remain under-covered.
I observe that raw RTE error is dominated by a systematic offset against consolidated data that stale annual corrections cannot track.

## 4. Data and Sources

I use three data sources:

- eCO2mix (RTE / ODRE): national electricity consumption and RTE forecasts at 30-minute resolution from 2021-03-23 onward. I ingest records into `raw.eco2mix` using `src/load_forecast/eco2mix.py`. I clean and deduplicate data with dbt models in `dbt/models/`. The production dbt target writes to `analytics`; isolated development targets remain available in `dbt/profiles.yml`.
- NOAA GFS: I ingest numerical weather predictions on a 0.25-degree grid. I download the 00 UTC run of D-1 (forecast steps f021 to f047) through S3 byte-range queries with `src/load_forecast/weather_collector.py`. If that run is late or incomplete, I fall back to the 18 UTC run of D-2. In `src/load_forecast/weather_features.py`, I linearly interpolate hourly forecasts to 30-minute intervals across 12 French grid points. I compute the national mean temperature as an unweighted average of these points.
- Meteo-France SYNOP: I load daily station observations across the 12 weather points into `labels.synop` with `src/load_forecast/labels.py`. I label a day cold under rule D9 when the national daily mean temperature is strictly below 4.84 degrees Celsius. I require at least 10 valid stations with all 8 synoptic reports. These labels serve only as evaluation ground truth. I never use them as model inputs.

## 5. Method

I build an evaluation pipeline following strict protocol rules:

- Production schedule design (Rule D16, docs/orchestration.md, step 7 planned): I design the daily pipeline around a 12:00 Europe/Paris information freeze on D-1. I execute model inference immediately after 12:00. I deliver forecasts before 12:30 Europe/Paris. Prefect will orchestrate this daily schedule.
- Rule D2 availability: Historical consumption records lack per-slot publication timestamps. I restrict consumption inputs to slots ending at or before 10:00 Europe/Paris on D-1. This enforces a two-hour availability delay.
- Rule D11 weather cutoff: I require GFS weather runs to have an S3 LastModified timestamp before the 12:00 D-1 cutoff. If 00 UTC files are late or incomplete, I fall back to the 18 UTC run of D-2.
- Rule D15 benchmark: I evaluate models against raw RTE D-1 and against RTE* a2. RTE* subtracts the previous calendar year mean bias by local hour. It provides a leak-free benchmark against consolidated data.
- Walk-forward backtest: I train models on an expanding historical window starting on 2021-03-23. I refit models every Monday.
- Pre-registration: I choose hyperparameters and model designs on 2023 inner walk-forwards only. I commit design choices to Git before running backtests on 2024 or 2025. Two documented exceptions apply: EXP-006 did not commit its 96-hour half-life before the 2024 run, and EXP-010 performed a dry run on 2024 after fixing the window N but before the final run.
- Moving block bootstrap: I evaluate statistical significance using paired daily MAE differences. I draw 10,000 samples with a 7-day moving block bootstrap. When the 95% interval contains zero, I report the difference as not significant.

## 6. Limits

I identify eight operational and modeling limits documented in `docs/error_analysis.md` and `docs/revision_gap.md`:

1. Revision gap: In backtests, I use consolidated consumption for lags, aggregation losses, and conformal residuals. A production pipeline will receive real-time telemetry instead. In `docs/revision_gap.md`, I confirmed that `raw.eco2mix` holds zero overlapping timestamps between real-time and consolidated vintages. I have not measured the operational impact of this revision shift.
2. RTE* reference drift: RTE* subtracts the hourly bias of the prior year. In 2025, the bias of RTE against consolidated consumption shifted from -1146 MW to -973.5 MW. This caused RTE* to over-correct by +172.8 MW.
3. Holidays and Christmas week: Holidays exhibit weekday-dependent errors. Monday holidays are under-forecast with a mean bias of -1605 MW. Tuesday to Saturday holidays are over-forecast with a mean bias of +1479 MW. Christmas week (23 to 29 December) contains 8 of the 20 worst days across 2024 and 2025.
4. Conformal calibration by month: Seasonal Mondrian conformal prediction brings seasonal averages close to 80% (78.9% to 82.7% in 2025). Monthly coverage still varies from 59.6% in November to 92.9% in February. Cold days (71.2%) and holidays (41.9%) remain under-covered. I hypothesize that this occurs because seasonal pools are dominated by ordinary days.
5. 2023 tuning environment: I tuned model settings on 2023. I update aggregation weights online, but I selected the choice between shared and per-slot weights on 2023. In `docs/analysis_2023_vs_2024.md`, January and February 2023 accounted for 54% of adaptive model gains. I hypothesize that post-crisis recovery created an unusually favorable baseline.
6. Single test year: The 2025 test year is a single realization of 365 autocorrelated days. It contains 31 cold days and 11 holidays. Candidate 1 and Candidate 2 share underlying experts. Their results therefore do not provide independent evidence.
7. Observed versus forecast temperature: I evaluate cold-day labels with observed SYNOP station records. In contrast, models receive GFS weather forecasts at the 12:00 D-1 cutoff.
8. Spring clock change fallback: The 23-hour spring daylight saving day breaks subsequent 2-day and 7-day lags. I force models to use fallback forecasts without consumption lags for two days each year.

## 7. Repository Structure

Project structure:

```text
load-forecast-fr/
|-- dbt/                 # dbt project, intermediate and clean models, schema tests
|-- docs/                # Protocol, research, error analysis, benchmark and orchestration docs
|   \-- experiments/    # Experiment reports, pre-registration evidence and final test
|-- infra/               # Docker Compose (PostgreSQL 16.4, MLflow 3.16.1), SQL init, role scripts
|-- notebooks/           # Exploratory data analysis (01_eda.ipynb)
|-- scripts/             # Collector runners, experiment execution, selection and verification scripts
|   \-- source_research/ # Historical external-source verification scripts
|-- src/load_forecast/   # Core forecasting library:
|   |-- api/             # FastAPI service endpoints and schemas
|   |-- aggregation.py   # ML-Poly online expert aggregation
|   |-- backtest.py      # Walk-forward backtest runner with MLflow tracking
|   |-- backtest_plots.py# Standard diagnostic artifact plotting
|   |-- baseline.py      # Seasonal naive baseline logic
|   |-- clean_data.py    # Clean data loading helpers
|   |-- conformal.py     # Rolling, adaptive, and seasonal Mondrian prediction intervals
|   |-- cutoff.py        # 12:00 Europe/Paris cutoff calculations and leakage guards
|   |-- database.py      # Shared clean-table configuration and SQL quoting
|   |-- eco2mix.py       # eCO2mix ODRE ingestion collector
|   |-- error_analysis.py# Residual and error breakdown utilities
|   |-- evaluation.py    # Metric calculation, day-type labeling, block bootstrap
|   |-- experiment_reports.py # Pinned pre-registration report lookup
|   |-- gam_model.py     # Generalized Additive Model implementation
|   |-- gbm_model.py     # Global LightGBM regression model
|   |-- kalman.py        # Online Kalman filter residual tracking
|   |-- labels.py        # SYNOP station cold-day observation labels
|   |-- lag_model.py     # Consumption lag feature builders and fallbacks
|   |-- linear_model.py  # Local-slot linear model implementation
|   |-- monthly_analysis.py # Monthly error and bias diagnostics
|   |-- periods.py       # Evaluation period guards
|   |-- rte_reference.py # RTE* operational bias correction
|   |-- smoothed_temperature.py # Exponentially smoothed temperature features
|   |-- temperature_model.py # Degree-day temperature feature builder
|   |-- tracking.py      # MLflow logging helpers
|   |-- weather.py       # NOAA GFS coordinates and download parameters
|   |-- weather_collector.py # GFS S3 byte-range download collector
|   \-- weather_features.py  # GFS 30-minute interpolation and national temperature
|-- tests/               # Automated test suite (pytest)
|-- README.md            # Project overview and reproduction guide
|-- pyproject.toml       # Ruff and pytest configurations
\-- requirements.txt     # Pinned Python dependencies
```

## 8. How to Run

Prerequisites (developer-managed):
- The shared Python 3.13 virtual environment exists at `../load-forecast-fr-venv`.
- The PostgreSQL 16.4 container is running with databases `load_forecast` and `mlflow` (see `infra/README.md`).
- The developer provides the root `.env` file with database credentials (`POSTGRES_USER=lff_agent`), loaded with `scripts/load_env.ps1`.
- Raw table `raw.eco2mix` must already be populated by collectors before dbt models build. Weather tables (`raw.gfs_days`, `raw.gfs_points`) and labels (`labels.synop`) are needed for weather experiments and evaluations, not for dbt itself.

1. Dependencies and environment:
   I install pinned dependencies in the shared Python virtual environment:
   ```powershell
   ..\load-forecast-fr-venv\Scripts\pip.exe install -r requirements.txt
   ```
   I load the developer-provided `.env` and configure the Python path:
   ```powershell
   . .\scripts\load_env.ps1
   $env:PYTHONPATH = "src"
   ```

2. Run automated tests and linting:
   ```powershell
   ..\load-forecast-fr-venv\Scripts\pytest.exe
   ..\load-forecast-fr-venv\Scripts\ruff.exe check .
   ```

3. Build clean data models with dbt:
   I build the `analytics` schema with the production target:
   ```powershell
   ..\load-forecast-fr-venv\Scripts\dbt.exe build --project-dir dbt --profiles-dir dbt --target prod
   ```

4. Run an experiment:
   I run validation experiments on the 2024 validation period. For a self-contained validation run without cached expert dependencies, I execute the linear calendar benchmark EXP-002:
   ```powershell
   ..\load-forecast-fr-venv\Scripts\python.exe scripts/run_exp002.py
   ```
   All Python readers, including the API and notebook, use `LFF_CLEAN_TABLE` (default `analytics.load_30min`). I can set it to another `schema.table` before running Python to use an isolated development build. This script logs the run to MLflow.

   To reproduce the ML-Poly aggregation EXP-009, I must fulfill two prerequisites:
   - The runner reads clean data from schema `analytics.load_30min`.
   - The 2023 burn-in expert cache must exist at `data/experiments/EXP-009/experts_2023.csv`. I generate this cache by running `scripts/select_aggregation.py` (which evaluates the four experts across 2023):
   ```powershell
   ..\load-forecast-fr-venv\Scripts\python.exe scripts/select_aggregation.py
   ..\load-forecast-fr-venv\Scripts\python.exe scripts/run_exp009.py
   ```

   The 2025 test evaluation (`scripts/run_test2025.py`) was executed once under protocol D18 and is locked. The script enforces this no-rerun rule and exits if `docs/experiments/TEST-2025.md` contains results. I report its outcomes as archived historical milestones only.

5. Start the reference forecast API:
   The FastAPI service serves the reference model (EXP-002 calendar OLS). By default, the API reads from `analytics.load_30min`. I configure the table using the `LFF_CLEAN_TABLE` environment variable:
   ```powershell
   $env:LFF_CLEAN_TABLE = "analytics.load_30min"
   ..\load-forecast-fr-venv\Scripts\uvicorn.exe load_forecast.api.app:app --host 127.0.0.1 --port 8000
   ```
   I query health and forecast endpoints:
   - `GET http://127.0.0.1:8000/health`
   - `GET http://127.0.0.1:8000/forecast/2024-06-15`

## 9. Stack

- Language: Python 3.13
- Database: PostgreSQL 16.4
- Tracking: MLflow 3.16.1
- Transformations: dbt (dbt-core, dbt-postgres)
- Modeling: NumPy least squares, statsmodels (GLMGam), LightGBM, joblib
- Weather decoding: eccodes
- API: FastAPI, Uvicorn, httpx
- Testing and Linting: pytest, ruff
- Infrastructure: Docker, Docker Compose

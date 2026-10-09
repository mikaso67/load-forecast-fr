# Backtest plot artifacts

`log_backtest` requires a `frame` indexed by unique UTC half-hour slot starts,
with `actual`, `model` and `rte_d1` in MW. Each successful run saves five PNGs in
MLflow's `plots/` artifact folder before marking the run FINISHED:

- Actual and both forecasts over the full period, with UTC slot starts.
- Slot-weighted MAE by Europe/Paris month.
- Slot-weighted MAE by Europe/Paris hour, combining both autumn occurrences.
- Slot-weighted MAE by day type, using the evaluation harness's precedence.
- Daily model-minus-reference MAE difference and the 95% interval of its mean
  from the seven-day moving block bootstrap (10000 draws, seed 20240).

The shaded band is uncertainty about the mean daily difference, not a forecast
prediction interval or a band for individual daily differences. Negative differences
favour the model. Titles state a measured finding; group comparisons are descriptive.
Every figure has a white background, the model in `#1f4e9c`, RTE in grey and axes
with units. Actual consumption is dark grey.

All diagnostics use slots common to actual, model and RTE. The time-series plot
retains gaps rather than connecting through missing slots. Monthly, hourly and
day-type MAEs pool slot errors; the daily difference uses daily MAEs. The full
local-date calendar retains missing days before bootstrapping.

For the first experiment, the reference defaults to RTE D-1. Later experiments pass
`reference=<saved UTC prediction Series>` and `reference_name='EXP-001'` (or the
chosen reference). Daily comparisons additionally require the reference to be present
on the same slots. They never average model and reference errors on different slots.
For monthly, hourly and day-type comparisons against that experiment, add its
predictions as a column in `frame`, then pass `reference_column='exp001'` and
`reference_name='EXP-001'`. The default column is `rte_d1`. These grouped plots
use the intersection of actual, model, RTE and the selected reference, with grey
bars and titles naming the selected reference. The time-series plot keeps RTE.
When `reference` is omitted, the daily comparison also uses `reference_column`;
an explicit `reference` Series still overrides only the daily comparison.
Pass `cold=<date-to-bool mapping>` for the fixed D9 evaluation labels. With no labels,
the day-type figure explicitly says that ordinary includes unlabelled cold days.
The EXP-001 rerun reproduces its original evaluation without adding cold labels.

The logger reads `MLFLOW_TRACKING_URI` at each call, defaulting to
`http://127.0.0.1:5000`. Both tracking REST requests and artifact uploads use that
server (including any URL path prefix). The run's `mlflow-artifacts:/` URI supplies
the artifact path. It requires artifact proxying as configured in shared infrastructure. Temporary
PNG files are removed when logging exits. An upload failure raises and the run is
marked FAILED when the tracking server remains reachable. Periods must contain
at least seven calendar days and two
scored days for the protocol's bootstrap.

From the repository root, with the existing database and MLflow server running:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ..\load-forecast-fr-venv\Scripts\python.exe scripts/run_baseline.py
```

This reads only the seven-day warmup and the 2024 Paris validation year. It prints
the new MLflow run id; open experiment 0, that run, and its `plots/` artifact folder.
The baseline and generic runner scripts both call this logger. `run_exp002.py`
passes the saved-in-memory EXP-001 forecast as its reference; no model settings change.

`pyproject.toml` restricts default pytest discovery to `tests/`. Scratch copies are
not collected by a plain `python -m pytest` invocation.

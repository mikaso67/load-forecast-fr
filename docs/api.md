# Reference forecast API

I serve the reference model, EXP-002 calendar OLS: 48 independent local-slot
models with day-of-week (holidays as Sunday), Fourier order 3, bridge and Christmas-week
features. The later model ladder and the final EXP-009 aggregation are experimental
milestones and are not deployed to this endpoint. Responses contain point forecasts in MW,
UTC and Europe/Paris slot starts, the model name, delivery cutoff, Monday refit date and
refit cutoff.
An ordinary day has 48 rows, spring DST 46 and autumn DST 50. The two occurrences of
an autumn wall-clock half-hour share a model and have distinct UTC timestamps and offsets.

The service refits on each request using the preceding Monday delivery date's cutoff
(Sunday noon Paris), matching EXP-002's weekly expanding training schedule.
Training starts on 2021-03-23. The SQL query selects only consumption from
the table configured by `LFF_CLEAN_TABLE` (default `analytics.load_30min`),
ending at the last slot whose end is at or before refit cutoff
minus two hours (D2). The fitting path applies the same guard again. No RTE or observed
temperature label is a feature. The existing `SlotOLS`, `calendar_ols_features` and
`forecast_day` functions are reused. API tests compare with `walk_forward` on 2024.

Historical consolidated revisions remain the accepted limitation from protocol section
2.2. These responses reconstruct a forecast under D2; they do not reproduce the exact
data vintage originally published. No metrics, tuning or MLflow writes occur. The 2025
test year has occurred under D18, but this API endpoint remains restricted to the EXP-002
calendar reference and does not score test-year dates.

## Local run (PowerShell, repository root)

The developer manages PostgreSQL, MLflow and the clean relation. They must already exist.
The folder's ignored `.env` holds the read-only `lff_agent` credentials and optionally
`MLFLOW_TRACKING_URI` (default `http://127.0.0.1:5000`). Never put credentials in a URL.
Set `LFF_CLEAN_TABLE` to `schema.table` to read another clean relation, for example
`$env:LFF_CLEAN_TABLE = 'analytics.load_30min'`. Both identifiers are quoted by psycopg;
SQL fragments are not accepted. The relation must expose `value_time` and `consommation`
with the same UTC half-hour semantics. The database role must have SELECT permission.

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = (Resolve-Path .\src).Path
& ..\load-forecast-fr-venv\Scripts\python.exe -m uvicorn load_forecast.api.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

In another PowerShell terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
$forecast = Invoke-RestMethod http://127.0.0.1:8000/forecast/2024-01-17
$forecast | Select-Object delivery_date, model_name, refit_date
$forecast.forecasts.Count
$forecast.forecasts | Select-Object -First 1
```

Stop this local API with Ctrl+C. `GET /docs` provides the request/response schema.

## Responses and failures

`GET /health` runs `SELECT 1` and checks MLflow's `/health` endpoint with a five-second
HTTP timeout. It returns 200 when both respond, otherwise 503. Its only fields are
`status`, `database` and `mlflow`, each `ok` or `unavailable`. Neither connection details
nor exception messages appear. It checks connectivity, not model readiness.

`GET /forecast/{delivery_date}` accepts ISO dates. It returns 400 before usable training
history or while the delivery cutoff is in the future, 422 for an invalid date, and 503
for missing training slots, database errors or invalid predictions. Forecasts need
PostgreSQL; an unavailable MLflow service does not prevent them.

Fitting every request keeps this first service simple but costs CPU and is unsuitable
for heavy concurrent traffic. There is no authentication or rate limiting. Run it on
loopback locally. Deployment, model caching, deploying the aggregation and a shared compose entry remain developer
decisions.

## Container preparation

`src/load_forecast/api/Dockerfile` uses Python 3.13.1, pinned repository dependencies
and a non-root user. Its build context is the repository root:

```powershell
docker build -f src/load_forecast/api/Dockerfile -t load-forecast-api .
```

This session does not build or start a container and does not change shared compose.
At deployment, supply credentials as environment variables and set `POSTGRES_HOST`
and `MLFLOW_TRACKING_URI` to addresses reachable from the container.

## Verification

```powershell
& ..\load-forecast-fr-venv\Scripts\python.exe -m pytest tests/test_api.py -q
& ..\load-forecast-fr-venv\Scripts\python.exe -m ruff check .
```

Tests mock database connections and health HTTP requests, check D2 query bounds and
post-cutoff poisoning, compare predictions with the EXP-002 runner, cover both DST days,
and check validation and sanitized error responses.

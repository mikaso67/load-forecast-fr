# Forecast API and operational dashboard

I serve the production forecast and operational run monitoring through a FastAPI service and a Streamlit dashboard.

The API serves pre-stored forecasts produced by the daily pipeline. The served endpoint returns the delivery choice recorded for the day (primary model forecast or prepared fallback), including point forecasts, 80% prediction intervals, local half-hour slots, and model metadata, without recomputing predictions live. Live EXP-002 computation has been retired from the served path and replaced by stored choice retrieval; historical calculation routines remain available in internal modules for reference.

Responses contain point forecasts in MW, UTC slot timestamps, local Paris slot indices, 80% lower and upper bounds, pool sizes, choice hashes, and delivery metadata.
An ordinary day has 48 rows, spring DST 46 and autumn DST 50.

Run execution events are exposed via `GET /runs/{delivery_date}`, returning run identifiers, completion status, freeze times, weather status, refit days, and MLflow run links.

The Streamlit dashboard (`dashboard/app.py`) provides an interactive view of production performance over the last 90 days. It tracks served forecasts against actual consumption and RTE D-1, headline and primary model MAE scores, operational delivery reliability (fallback and late days), two-sided CUSUM drift detection, and production run history.

## Local run (PowerShell, repository root)

The developer manages PostgreSQL, MLflow and the clean relation. They must already exist.
The folder's ignored `.env` holds database credentials and optionally `MLFLOW_TRACKING_URI` (default `http://127.0.0.1:5000`). Never put credentials in a URL.
Set `LFF_DATABASE_URL` or database environment variables (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`).
Set `LFF_CLEAN_TABLE` to `schema.table` to read another clean relation (default `analytics.load_30min`).

### Starting the FastAPI service

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = (Resolve-Path .\src).Path
& ..\load-forecast-fr-venv\Scripts\python.exe -m uvicorn load_forecast.api.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

In another PowerShell terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
$forecast = Invoke-RestMethod http://127.0.0.1:8000/forecast/2024-04-15
$forecast | Select-Object delivery_day, kind, model_or_fallback, interval
$forecast.values.Count
$forecast.values | Select-Object -First 1

$runs = Invoke-RestMethod http://127.0.0.1:8000/runs/2024-04-15
$runs | Format-Table run_id, kind, status, started_at, completed_at
```

Stop the local API with Ctrl+C. `GET /docs` provides the request/response OpenAPI schema.

### Starting the Streamlit dashboard

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = (Resolve-Path .\src).Path
& ..\load-forecast-fr-venv\Scripts\streamlit.exe run dashboard/app.py
```

The dashboard opens in your browser at `http://localhost:8501`. If the database contains no recorded deliveries or actual consumption yet, the dashboard displays a clear informational status notice without error.

## Responses and failures

`GET /health` runs `SELECT 1` on the database and checks MLflow's `/health` endpoint with a five-second HTTP timeout. It returns 200 when both respond, otherwise 503. Its only fields are `status`, `database` and `mlflow`, each `ok` or `unavailable`. Neither connection details nor exception messages appear. It checks connectivity, not model readiness.

`GET /forecast/{delivery_date}` accepts ISO dates (YYYY-MM-DD). It returns:
- 200: Served forecast payload with choice hash, delivery kind, model name or fallback description, 80% interval specification, and slot-by-slot predictions with bounds.
- 404: No forecast choice recorded for the requested delivery date.
- 422: Invalid date format.
- 500: Corrupted choice payload or unreadable stored record.
- 503: Database unavailable.

`GET /runs/{delivery_date}` accepts ISO dates (YYYY-MM-DD). It returns:
- 200: List of run execution events for the requested delivery date, ordered by start time. Returns an empty list if no runs occurred on that date.
- 422: Invalid date format.
- 503: Database unavailable.

## Container preparation

`src/load_forecast/api/Dockerfile` uses Python 3.13.1, pinned repository dependencies and a non-root user. Its build context is the repository root:

```powershell
docker build -f src/load_forecast/api/Dockerfile -t load-forecast-api .
```

This session does not build or start a container and does not change shared compose.
At deployment, supply credentials as environment variables and set `POSTGRES_HOST` and `MLFLOW_TRACKING_URI` to addresses reachable from the container.

## Verification

```powershell
& ..\load-forecast-fr-venv\Scripts\python.exe -m pytest tests/test_api.py tests/test_dashboard.py -q
& ..\load-forecast-fr-venv\Scripts\python.exe -m ruff check .
```

Tests mock database connections and health HTTP requests, check primary and fallback stored choice serving across ordinary and DST days, verify run event retrieval, validate sanitized error responses, and test dashboard rendering against both empty and populated database states.

"""FastAPI routes. Dependency failures return fixed messages, never exception text."""

from datetime import UTC, date, datetime
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from load_forecast.api.service import read_run_events, read_served_forecast
from load_forecast.eco2mix import connect_database
from load_forecast.tracking import mlflow_tracking_uri

app = FastAPI(title='French load forecast', debug=False)


class ForecastSlot(BaseModel):
    timestamp_utc: datetime
    timestamp_paris: datetime
    local_slot: int
    forecast_mw: float
    lower_mw: float | None = None
    upper_mw: float | None = None
    n_pool: int | None = None


class ForecastResponse(BaseModel):
    delivery_date: date
    kind: str
    model_or_fallback: str | None = None
    choice_hash: str
    chosen_at: datetime
    interval: dict[str, Any] | None = None
    values: list[ForecastSlot] = []
    forecasts: list[ForecastSlot] = []


class RunEvent(BaseModel):
    run_id: str
    delivery_day: date
    freeze_time: datetime
    kind: str
    started_at: datetime
    code_commit: str
    completed_at: datetime | None = None
    status: str | None = None
    refit_day: date | None = None
    mlflow_run_id: str | None = None
    kalman_quarter: str | None = None
    weather_status: str | None = None
    last_consumption_slot: datetime | None = None
    max_ingested_at: datetime | None = None
    feature_hash: str | None = None
    flags: dict[str, Any] | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)


def database_reachable() -> bool:
    try:
        with connect_database() as connection:
            return connection.execute('SELECT 1').fetchone()[0] == 1
    except Exception:
        return False


def mlflow_reachable() -> bool:
    try:
        url = mlflow_tracking_uri()
        return requests.get(f'{url}/health', timeout=5).status_code == 200
    except Exception:
        return False


@app.get('/health')
def health() -> JSONResponse:
    database, mlflow = database_reachable(), mlflow_reachable()
    return JSONResponse(status_code=200 if database and mlflow else 503, content={
        'status': 'ok' if database and mlflow else 'unavailable',
        'database': 'ok' if database else 'unavailable',
        'mlflow': 'ok' if mlflow else 'unavailable',
    })


@app.get('/forecast/{delivery_date}', response_model=ForecastResponse)
def forecast(delivery_date: date) -> ForecastResponse:
    try:
        data = read_served_forecast(delivery_date)
    except FileNotFoundError:
        raise HTTPException(404, 'No forecast found for delivery date') from None
    except ValueError:
        raise HTTPException(500, 'Serving choice corrupted or invalid') from None
    except Exception:
        raise HTTPException(503, 'Forecast unavailable') from None

    slots = [
        ForecastSlot(
            timestamp_utc=row['timestamp_utc'],
            timestamp_paris=row['timestamp_paris'],
            local_slot=row['local_slot'],
            forecast_mw=row['forecast_mw'],
            lower_mw=row.get('lower_mw'),
            upper_mw=row.get('upper_mw'),
            n_pool=row.get('n_pool'),
        )
        for row in data.get('values', [])
    ]
    return ForecastResponse(
        delivery_date=data['delivery_date'],
        kind=data['kind'],
        model_or_fallback=data.get('model_or_fallback'),
        choice_hash=data['choice_hash'],
        chosen_at=data['chosen_at'],
        interval=data.get('interval'),
        values=slots,
        forecasts=slots,
    )


@app.get('/runs/{delivery_date}', response_model=list[RunEvent])
def runs(delivery_date: date) -> list[RunEvent]:
    try:
        events = read_run_events(delivery_date)
    except Exception:
        raise HTTPException(503, 'Database unavailable') from None
    return [RunEvent(**event) for event in events]


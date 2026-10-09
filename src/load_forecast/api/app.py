"""FastAPI routes. Dependency failures return fixed messages, never exception text."""

from datetime import UTC, date, datetime

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from load_forecast import evaluation as ev
from load_forecast.api.service import MODEL_NAME, TRAINING_START, predict, read_history, refit_date
from load_forecast.cutoff import forecast_cutoff
from load_forecast.eco2mix import connect_database
from load_forecast.tracking import mlflow_tracking_uri

app = FastAPI(title='French load forecast', debug=False)


class ForecastSlot(BaseModel):
    timestamp_utc: datetime
    timestamp_paris: datetime
    forecast_mw: float


class ForecastResponse(BaseModel):
    delivery_date: date
    cutoff_utc: datetime
    model_name: str
    refit_date: date
    refit_cutoff_utc: datetime
    forecasts: list[ForecastSlot]


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
    if delivery_date <= TRAINING_START:
        raise HTTPException(400, 'Delivery date precedes usable training history')
    cutoff = forecast_cutoff(delivery_date)
    if cutoff > now_utc():
        raise HTTPException(400, 'Delivery cutoff is in the future')
    if refit_date(delivery_date) <= TRAINING_START:
        raise HTTPException(400, 'Delivery date precedes usable training history')
    try:
        predictions = predict(delivery_date, read_history(delivery_date))
    except Exception:
        raise HTTPException(503, 'Forecast unavailable') from None
    refit = refit_date(delivery_date)
    return ForecastResponse(
        delivery_date=delivery_date, cutoff_utc=cutoff, model_name=MODEL_NAME,
        refit_date=refit, refit_cutoff_utc=forecast_cutoff(refit), forecasts=[
            ForecastSlot(timestamp_utc=stamp.to_pydatetime(),
                         timestamp_paris=stamp.tz_convert('Europe/Paris').to_pydatetime(),
                         forecast_mw=float(value))
            for stamp, value in predictions[ev.MODEL].items()
        ],
    )

FROM python:3.13.1-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "mlflow==3.16.1" "psycopg[binary]==3.3.6"

WORKDIR /app

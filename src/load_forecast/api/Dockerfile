FROM python:3.13.1-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "load_forecast.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

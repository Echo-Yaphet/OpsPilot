FROM python:3.11-slim
WORKDIR /app
COPY apps/container-metrics-exporter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/container-metrics-exporter/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9101"]

FROM python:3.11-slim
WORKDIR /app
COPY apps/control-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/control-api/opspilot ./opspilot
COPY apps/shared-identity/workload_identity.py .
COPY apps/executor-gateway ./executor-gateway
COPY tests ./tests
CMD ["uvicorn", "opspilot.main:app", "--host", "0.0.0.0", "--port", "8080"]

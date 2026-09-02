FROM python:3.11-slim
WORKDIR /app
COPY apps/docker-proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/docker-proxy/app.py .
COPY apps/shared-identity/workload_identity.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "2375"]

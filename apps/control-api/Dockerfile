FROM python:3.11-slim
WORKDIR /app
COPY apps/control-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/control-api/opspilot ./opspilot
COPY apps/shared-identity/workload_identity.py .
COPY apps/shared-identity/issuer_client.py .
COPY apps/executor-gateway ./executor-gateway
COPY apps/runtime-executor ./runtime-executor
COPY apps/runtime-executor/runtime_store.py .
COPY apps/runtime-actuator ./runtime-actuator
COPY apps/container-metrics-exporter ./container-metrics-exporter
COPY apps/policy-distributor ./policy-distributor
COPY apps/promtail ./promtail
COPY apps/workload-identity-issuer ./workload-identity-issuer
COPY docker-compose.yml .gitignore ./
COPY infra/loki/promtail-config.yml ./infra/loki/promtail-config.yml
COPY infra/kubernetes ./infra/kubernetes
COPY infra/vault-agent ./infra/vault-agent
COPY tests ./tests
CMD ["uvicorn", "opspilot.main:app", "--host", "0.0.0.0", "--port", "8080"]

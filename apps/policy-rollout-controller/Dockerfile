FROM python:3.11-slim
WORKDIR /app
COPY apps/control-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/control-api/opspilot ./opspilot
COPY apps/shared-identity/workload_identity.py .
COPY apps/policy-rollout-controller/controller.py .
COPY apps/policy-rollout-controller/evaluation.py .
ENTRYPOINT ["python", "/app/controller.py"]

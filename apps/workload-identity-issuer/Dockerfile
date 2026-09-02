FROM python:3.11-slim
WORKDIR /app
COPY apps/workload-identity-issuer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/workload-identity-issuer/app.py apps/workload-identity-issuer/bootstrap.py ./
COPY apps/shared-identity/workload_identity.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8085"]

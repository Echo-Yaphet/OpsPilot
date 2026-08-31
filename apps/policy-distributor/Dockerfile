FROM python:3.11-slim
WORKDIR /app
COPY apps/policy-distributor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/policy-distributor/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8070"]

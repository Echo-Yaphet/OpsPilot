FROM python:3.11-slim
WORKDIR /app
COPY apps/executor-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/executor-gateway/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]

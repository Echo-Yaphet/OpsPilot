FROM python:3.11-slim
WORKDIR /app
COPY apps/shared-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/shared-service/app.py .
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]

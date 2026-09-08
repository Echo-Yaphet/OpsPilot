FROM python:3.11-slim
WORKDIR /app
COPY apps/repair-validator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/repair-validator/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8096"]

FROM python:3.11-slim
WORKDIR /app
COPY apps/repair-replica/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/repair-replica/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8097"]

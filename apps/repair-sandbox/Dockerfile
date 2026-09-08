FROM python:3.11-slim
WORKDIR /app
RUN useradd --system --uid 10001 --create-home repair
RUN mkdir -p /lab /data && chown -R repair:repair /lab /data
COPY apps/repair-sandbox/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/repair-sandbox/app.py .
USER 10001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8095"]

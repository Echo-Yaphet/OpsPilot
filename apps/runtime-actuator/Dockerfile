FROM python:3.11-slim
WORKDIR /app
COPY apps/runtime-actuator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/runtime-actuator/app.py .
CMD ["uvicorn", "app:app", "--uds", "/run/opspilot/actuator.sock"]

FROM python:3.11-slim
WORKDIR /app
COPY apps/runtime-actuator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/runtime-actuator/app.py .
CMD ["sh", "-c", "exec uvicorn app:app --uds ${RUNTIME_ACTUATOR_STATE_DIR:-/run/opspilot}/actuator.sock"]

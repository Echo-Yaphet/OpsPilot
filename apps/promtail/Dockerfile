FROM grafana/promtail:3.5.3 AS promtail

FROM python:3.11-slim

COPY --from=promtail /usr/bin/promtail /usr/bin/promtail
COPY apps/promtail/tls_syslog_gateway.py /opt/opspilot/tls_syslog_gateway.py

ENTRYPOINT ["python", "/opt/opspilot/tls_syslog_gateway.py"]

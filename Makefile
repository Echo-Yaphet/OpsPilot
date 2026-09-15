.PHONY: up down ps logs test smoke evaluate-stage5 repair-lab-validate repair-agent-live runtime-identity-validate runtime-orchestrator-validate runtime-log-pki runtime-log-rotate runtime-log-vault-publish runtime-log-vault-apply dashboard-dev dashboard-build fault-redis fault-cpu fault-mysql fault-combined recover

up: runtime-log-pki
	docker compose up -d --build

runtime-log-pki:
	./scripts/prepare-runtime-log-secrets.sh

runtime-log-rotate:
	./scripts/rotate-runtime-log-certificates.sh

runtime-log-vault-publish:
	./scripts/publish-runtime-log-bundle-to-vault.sh

runtime-log-vault-apply:
	./scripts/apply-runtime-log-vault-agent-secret.sh

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

test:
	docker compose run --rm -v ./scripts:/app/scripts:ro -v ./apps/shared-service:/app/shared-service:ro control-api python -m pytest -q

smoke:
	./scripts/smoke-test.sh

evaluate-stage5:
	mkdir -p work/stage5-evaluations
	docker compose run --rm --no-deps \
		-v ./apps/control-api/opspilot:/app/opspilot:ro \
		-v ./scripts:/app/scripts:ro \
		-v ./work/stage5-evaluations:/app/evaluation-output \
		control-api env PYTHONPATH=/app python /app/scripts/run-stage5-evaluation.py \
		$(if $(STAGE5_REPETITIONS),--repetitions $(STAGE5_REPETITIONS),) \
		$(if $(STAGE5_EVALUATION_ID),--evaluation-id $(STAGE5_EVALUATION_ID),)

repair-lab-validate:
	docker compose --profile repair-lab up -d --build repair-lab-redis repair-validator repair-sandbox repair-lab-payment
	docker compose --profile repair-lab run --rm --no-deps -v ./scripts:/app/scripts:ro control-api python /app/scripts/validate-repair-lab.py
	python3 scripts/validate-repair-lab-security.py

repair-agent-live:
	python3 scripts/validate-repair-agent-live.py

runtime-identity-validate:
	docker compose exec executor-gateway python /app/validate-runtime-identity.py

runtime-orchestrator-validate:
	docker compose --profile orchestrator-runtime build executor-gateway workload-identity-issuer-orchestrated runtime-executor-redis-a runtime-executor-redis-b
	docker compose up -d --no-deps executor-gateway
	docker compose --profile orchestrator-runtime up -d runtime-audit-db workload-identity-issuer-orchestrated runtime-executor-redis-a runtime-executor-redis-b
	docker compose up -d --force-recreate --wait redis
	docker compose exec executor-gateway python /app/validate-orchestrator-runtime.py
	docker compose exec runtime-audit-db psql -U opspilot_runtime -d opspilot_runtime -c "SELECT operation,target,outcome,placement,executor_id FROM runtime_audit ORDER BY id DESC LIMIT 1"

dashboard-dev:
	cd apps/dashboard && npm run dev

dashboard-build:
	cd apps/dashboard && npm run build

fault-redis:
	./scripts/faults/redis-down.sh

fault-cpu:
	./scripts/faults/cpu-spike.sh

fault-mysql:
	./scripts/faults/mysql-down.sh

fault-combined:
	./scripts/faults/redis-mysql-down.sh

recover:
	./scripts/recover-runtime-dependencies.sh

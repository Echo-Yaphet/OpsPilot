.PHONY: up down ps logs test smoke runtime-log-pki runtime-log-rotate runtime-log-vault-publish runtime-log-vault-apply dashboard-dev dashboard-build fault-redis fault-cpu fault-mysql recover

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
	docker compose run --rm -v ./scripts:/app/scripts:ro control-api python -m pytest -q

smoke:
	./scripts/smoke-test.sh

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

recover:
	./scripts/recover-runtime-dependencies.sh

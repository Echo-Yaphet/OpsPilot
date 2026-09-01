.PHONY: up down ps logs test smoke runtime-log-pki dashboard-dev dashboard-build fault-redis fault-cpu fault-mysql recover

up: runtime-log-pki
	docker compose up -d --build

runtime-log-pki:
	./scripts/generate-runtime-log-pki.sh

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

test:
	docker compose run --rm control-api python -m pytest -q

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
	docker compose start redis mysql
	docker compose restart payment-service

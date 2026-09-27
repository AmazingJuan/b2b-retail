SHELL := /bin/sh
COMPOSE := docker compose
DB := $(COMPOSE) exec -T postgres psql -U retail -d retail
PYTHON := python3

.PHONY: up db-reset datos verificar medir ruta clean

up:
	$(COMPOSE) up -d
	$(COMPOSE) exec -T postgres sh -c 'until pg_isready -U retail -d retail; do sleep 1; done'

db-reset: up
	$(DB) -f /workspace/modelo-fisico.sql

datos: db-reset
	$(PYTHON) datos/generar.py --output datos/generated --seed $${SEED:-42} --orders $${ORDERS:-510000} --as-of-date $${AS_OF_DATE:-2026-09-20}
	$(COMPOSE) exec -T postgres sh -c 'rm -rf /tmp/retail-data && mkdir -p /tmp/retail-data'
	$(COMPOSE) cp datos/generated/. postgres:/tmp/retail-data/
	$(DB) -f /workspace/sql/load.sql
	$(DB) -c 'ANALYZE;'

verificar: up
	$(DB) -f /workspace/sql/verify.sql | tee evidence/verification.txt

medir: up
	$(PYTHON) scripts/application_route.py --dsn 'host=localhost port=5433 dbname=retail user=retail password=retail' --output evidence/application-route.json
	$(PYTHON) scripts/measure.py --dsn 'host=localhost port=5433 dbname=retail user=retail password=retail' --output evidence/metrics.json
	$(PYTHON) scripts/compare_inserts.py --dsn 'host=localhost port=5433 dbname=retail user=retail password=retail' --output evidence/load-strategies.json

ruta: up
	$(PYTHON) scripts/application_route.py --dsn 'host=localhost port=5433 dbname=retail user=retail password=retail' --output evidence/application-route.json

clean:
	$(COMPOSE) down -v
	rm -rf datos/generated evidence/*.txt evidence/*.json evidence/plans

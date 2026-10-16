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

# ============================================================================
# ENTREGABLE 3 · Resiliencia y observabilidad (ver entregable3/README.md)
# ============================================================================
E3_COMPOSE := docker compose -f compose.yaml -f entregable3/compose.e3.yaml
REPS ?= 3
PHASE_S ?= 30
EXP ?= all

.PHONY: e3-test e3-test-ruta e3-medicion-corregida e3-up e3-down e3-momento1 e3-objetivos e3-verificar-fallas e3-experimentos e3-mejoras e3-revertir-mejoras e3-medicion-final e3-figuras e3-kill-switch

e3-up:
	$(E3_COMPOSE) up -d --build
	until curl -sf localhost:8000/health >/dev/null; do sleep 1; done

e3-down:
	$(E3_COMPOSE) down

e3-objetivos:
	$(PYTHON) entregable3/scripts/objetivos.py

e3-momento1: e3-objetivos
	$(PYTHON) entregable3/scripts/momento1.py --repetitions $(REPS)

e3-verificar-fallas:
	$(PYTHON) entregable3/experiments/verificar_fallas.py

e3-experimentos:
	$(PYTHON) entregable3/experiments/run.py $(EXP) --reps $(REPS) --phase-s $(PHASE_S)

e3-kill-switch:
	curl -s -XPOST localhost:8000/admin/kill-switch

e3-mejoras:
	$(E3_COMPOSE) exec -T postgres psql -U retail -d retail -f /workspace/entregable3/sql/mejoras.sql

e3-revertir-mejoras:
	$(E3_COMPOSE) exec -T postgres psql -U retail -d retail -f /workspace/entregable3/sql/mejoras_revert.sql

e3-medicion-final:
	$(PYTHON) entregable3/scripts/medicion_final.py --repetitions $(REPS)

e3-figuras:
	$(PYTHON) entregable3/scripts/figuras.py

e3-test:
	$(PYTHON) entregable3/tests/test_medicion.py
	docker cp entregable3/tests/test_resiliencia.py b2b-e3-app:/srv/test_resiliencia.py
	docker exec -w /srv b2b-e3-app python test_resiliencia.py
	$(PYTHON) entregable3/tests/test_integracion.py

# Equivalencia ruta E2 vs ruta corregida (copia la base: requiere la app y el exportador detenidos)
e3-test-ruta:
	docker stop b2b-e3-app b2b-e3-postgres-exporter
	$(PYTHON) entregable3/tests/test_ruta_equivalente.py; status=$$?; docker start b2b-e3-postgres-exporter b2b-e3-app; exit $$status

e3-medicion-corregida:
	$(PYTHON) entregable3/scripts/medicion_final.py --solo corregido --repetitions 5 --output-dir entregable3/evidence/final_corregido

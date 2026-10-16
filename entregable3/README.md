# Entregable 3 · Resiliencia y observabilidad (B2B Retail)

El E3 parte del sistema y de los resultados medidos en el **Entregable 2**: el mismo modelo físico
(`modelo-fisico.sql`), los mismos datos (`datos/generar.py --seed 42`, 510 000 órdenes, 3 066 892
líneas), las mismas consultas críticas Q1–Q5 y la misma ruta transaccional de 5 000 órdenes. Sobre
eso se agregan un servicio con **fallas configurables en caliente**, **mecanismos de corrección
conmutables**, **observabilidad** (métricas, logs y trazas) y un **orquestador de experimentos**
que ejecuta baseline → falla → recuperación → corrección → falla con corrección → recuperación.

```
                        ┌────────────── plano de control /admin (fallas, kill switch, resiliencia)
 loadgen (lazo abierto) │
 100 rps ──HTTP──► app FastAPI ──pool(10)──► Toxiproxy ──► PostgreSQL 16 (datos del E2)
                   │  └─pool crítico(3)─┘   (fallas de red)   ▲
                   │                                          │ conexión directa (fallas de datos)
   /metrics ──► Prometheus ◄── postgres-exporter, docker-exporter
   OTLP ──► Jaeger            Grafana (dashboard "E3 · Resiliencia y observabilidad")
   logs JSON con trace_id ──► docker logs
```

## Estructura

| Ruta | Contenido |
|---|---|
| `compose.e3.yaml` | Extensión del `compose.yaml` del E2: app, Toxiproxy, Prometheus, Grafana, Jaeger, exportadores |
| `app/e3app/` | Servicio: `queries.py` (SQL del E2), `faults.py` (8 fallas), `resilience.py` (correcciones), `core.py`, `main.py` |
| `experiments/experiments.json` | Los 8 experimentos: hipótesis, falla, corrección y **predicción previa verificable** |
| `experiments/run.py` | Orquestador (fases, carga, recolección de evidencia, verificación de predicciones) |
| `experiments/loadgen.py` | Carga de lazo abierto que corrige la *coordinated omission* |
| `experiments/verificar_fallas.py` | Verificación automática de las 5 condiciones de cada falla |
| `scripts/momento1.py` | Reconstrucción del punto de partida del E2 y línea base |
| `scripts/objetivos.py` | Tabla de objetivos (0,90 × valor E2) y comparación |
| `scripts/medicion_final.py` | Medición del estado corregido contra la meta del −10% (`--solo corregido`) |
| `scripts/ruta_corregida.py` | Ruta de órdenes con asignación de franjas por rango (equivalente a la del E2) |
| `tests/` | Pruebas automáticas (`make e3-test`, `make e3-test-ruta`) |
| `sql/mejoras.sql` · `sql/mejoras_revert.sql` | Corrección de diseño físico (índices de cobertura) y su reversión |
| `evidence/` | Toda la evidencia generada (ver abajo) |
| `informe/` | Informe técnico (≤ 10 páginas) |

## Requisitos

Docker con Compose v2, Python ≥ 3.10 (solo librería estándar; `matplotlib` para las figuras), cliente
`psql`. **Medir conectado a la corriente**: en batería la latencia se triplica y `momento1.py` se niega a
medir en ese estado.

## Procedimiento reproducible

```bash
# 1. Momento 1: punto de partida del E2 y línea base (stack del E3 abajo, condiciones del E2)
make e3-down
make e3-momento1

# 2. Sistema del E3, pruebas y verificación de las fallas configurables
make e3-up                       # app :8000 · Grafana :3000 · Prometheus :9090 · Jaeger :16686 · Toxiproxy :8474
make e3-test                     # medición, resiliencia e integración
make e3-test-ruta                # la ruta corregida deja el mismo estado que la del E2
make e3-verificar-fallas         # 8 fallas × 5 condiciones

# 3. Experimentos (8 × 3 repeticiones × 6 fases de 30 s)
make e3-experimentos

# 4. Estado corregido contra la meta del −10% y figuras
make e3-medicion-corregida
make e3-figuras
make e3-revertir-mejoras         # volver al diseño físico exacto del E2
```

Informe: `cd entregable3/informe && pdflatex informe.tex` (tablas: `python3 entregable3/scripts/tablas.py`).

### Plano de control (activación sin redespliegue)

```bash
curl -XPOST localhost:8000/admin/faults/net_latency -d '{"params":{"latency_ms":30,"jitter_ms":10},"duration_s":60}' -H 'content-type: application/json'
curl -XDELETE localhost:8000/admin/faults/net_latency        # desactivar una falla
curl -XPOST localhost:8000/admin/kill-switch                 # KILL SWITCH: apaga todo y bloquea nuevas activaciones (409)
curl -XPOST localhost:8000/admin/arm                         # rearmar
curl -XPUT  localhost:8000/admin/resilience -d '{"retry":{"enabled":true}}' -H 'content-type: application/json'
curl -XPOST localhost:8000/admin/resilience/reset            # volver a la configuración del baseline
curl localhost:8000/admin/state                              # fallas, resiliencia, circuit breaker y pool
```

También se puede activar el kill switch al arrancar con `FAULT_INJECTION_ENABLED=false`. Cada falla
acepta `duration_s` (se desactiva sola, como protección).

### Fallas por familia

| Familia | Falla | Mecanismo | Parámetros |
|---|---|---|---|
| Red | `net_latency` | toxic `latency` de Toxiproxy entre app y BD | `latency_ms`, `jitter_ms` |
| Red | `net_partition` | proxy deshabilitado (conexiones cerradas/rechazadas) | — |
| Aplicación | `app_error` | excepción sintética en el procesamiento (HTTP 500) | `probability`, `queries` |
| Aplicación | `app_latency` | pausa larga en una fracción de solicitudes | `probability`, `delay_ms`, `queries` |
| Recursos | `pool_shrink` | fuga de conexiones: solo `max_size` utilizables | `max_size` |
| Recursos | `noisy_neighbor` | N sesiones ejecutan el reporte analítico pesado | `sessions` |
| Datos | `index_drop` | `DROP INDEX CONCURRENTLY` del índice crítico (se recrea con su definición) | `schema`, `index` |
| Datos | `table_lock` | transacción que retiene `LOCK TABLE … ACCESS EXCLUSIVE` | `table`, `mode` |

### Correcciones (conmutables en caliente, apagadas en el baseline)

`retry` (backoff exponencial con jitter) · `circuit_breaker` · `timeouts` (pool, `statement_timeout`,
`lock_timeout`) · `pool_check` · `cache` (TTL) · `bulkhead` (concurrencia por tipo de carga) ·
`pool_bulkhead` (pool aislado para Q1/Q2) · `hedging` · `report_limits` · y, en el diseño físico,
`sql/mejoras.sql`.

## Evidencia

| Ruta | Qué contiene |
|---|---|
| `evidence/e2_referencia/` | Métricas canónicas del E2 congeladas (commit `d9d4910`) |
| `evidence/objetivos/` | Tabla de objetivos (0,90 × E2) y comparación del estado corregido |
| `evidence/baseline/` | Línea base del Momento 1: conteos, checksum, entorno, energía, 3 repeticiones |
| `evidence/ciclo_cpu.json` | Regímenes de energía del equipo (turbo / sostenido / limitado) |
| `evidence/verificacion_fallas.json` | 5 condiciones × 8 fallas y tiempo del kill switch |
| `evidence/experimentos/E1…E8/` | `manifest.json`, `result.json` (agregado y CV) y, por repetición, `requests.csv`, `summary.json`, `timeline.json`, `prometheus.json`, `traces_*.json`, `app_logs.jsonl`, `docker_stats.json` |
| `evidence/senales.json` | Promedio por fase de las señales clave (detectar/localizar) |
| `evidence/final_corregido/` | Estado corregido contra la meta del −10% (servicio sin/con índices y ruta A/B) |
| `evidence/figuras/` | Figuras del informe |

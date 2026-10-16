# Portal B2B de proveedores de retail

Proyecto integrador del curso **Aplicaciones y Sistemas Escalables** (Universidad EAFIT), desarrollado por
Diego Andrés Gonzalez y Juan Pablo Avendaño.

El portal conecta a una cadena de retail con sus proveedores: contratos de suministro, catálogo negociado,
órdenes de compra y franjas de descargue en los centros de distribución (CEDI). El modelo está dividido en
esquemas por contexto (`proveedores`, `catalogo`, `ordenes`, `logistica` y `auditoria`), sin joins entre ellos.

## Entregables

| Entregable | Tema | Dónde está |
|---|---|---|
| **E2 · Escalabilidad de los datos** | Generar y cargar ~5,1 millones de filas con forma realista (sesgo Zipf, estacionalidad, casos borde) y medir cómo se comportan las consultas críticas | Raíz del repositorio |
| **E3 · Resiliencia y observabilidad** | Fallas configurables sobre el sistema del E2, experimentos con predicción previa, observabilidad y correcciones | [`entregable3/`](entregable3/README.md) |

## Requisitos

- Docker con Compose v2
- Python ≥ 3.10 (solo librería estándar; `matplotlib` para las figuras del E2)
- Cliente `psql`

## E2 · Escalabilidad de los datos

```bash
make datos       # base limpia, genera los CSV con semilla 42 y los carga con COPY + ANALYZE
make verificar   # conteos, sesgo, estacionalidad, casos borde y checksum de determinismo
make medir       # ruta transaccional de 5.000 órdenes, Q1–Q5 frío/caliente y estrategias de carga
make clean       # apaga y borra la base y los archivos generados
```

La generación es determinista: con `--seed 42` dos corridas producen exactamente los mismos datos
(`datos/generated/manifest.json` guarda los checksums). PostgreSQL 16 queda expuesto en el puerto `5433`.

| Entidad | Filas |
|---|---:|
| Proveedores | 105.000 |
| Contratos | 16.500 |
| SKU del catálogo | 420.000 |
| Órdenes de compra | 510.000 |
| Líneas de orden | 3.066.892 |
| Franjas de descargue | 160.000 |
| Eventos de auditoría | 850.000 |

Las consultas críticas que se miden son:

- **Q1:** contrato vigente de un proveedor.
- **Q2:** precios vigentes del proveedor.
- **Q3:** franjas disponibles de un CEDI en la fecha de corte.
- **Q4:** OTIF y fill rate mensual del proveedor.
- **Q5:** últimas 50 órdenes del proveedor.

Cada una se mide en frío (proveedor 100000) y en caliente (proveedor 1).

## E3 · Resiliencia y observabilidad

Parte de la corrida canónica del E2 y le agrega un servicio con **8 fallas configurables en 4 familias**
(red, aplicación, recursos y datos), activables en caliente y con kill switch, junto con un stack de
observabilidad (Prometheus, Grafana, Jaeger y logs con `trace_id`) y un orquestador de experimentos.

```bash
make e3-down && make e3-momento1     # punto de partida del E2 y línea base
make e3-up                           # servicio :8000 · Grafana :3000 · Prometheus :9090 · Jaeger :16686
make e3-verificar-fallas             # comprueba las 5 condiciones de cada falla
make e3-experimentos                 # 8 experimentos × 3 repeticiones
make e3-medicion-corregida           # estado corregido frente a la meta de mejora del 10%
make e3-kill-switch                  # apaga todas las fallas activas
```

El detalle (fallas, correcciones, experimentos y evidencia) está en [`entregable3/README.md`](entregable3/README.md).

## Estructura

| Ruta | Contenido |
|---|---|
| `compose.yaml` | PostgreSQL 16 del proyecto |
| `modelo-fisico.sql` | Modelo físico: esquemas, tablas, llaves e índices |
| `datos/generar.py` | Generador determinista de los datos (streaming a CSV) |
| `sql/load.sql` · `sql/verify.sql` | Carga con `\copy` y verificación automática del conjunto |
| `scripts/` | Medición de consultas, ruta transaccional, comparación de estrategias de carga y figuras del E2 |
| `evidence/` | Evidencia del E2: métricas, planes de ejecución y verificación |
| `entregable3/` | Todo el E3: servicio, fallas, observabilidad, experimentos y su evidencia |
| `Makefile` | Comandos del E2 (`datos`, `verificar`, `medir`) y del E3 (`e3-*`) |

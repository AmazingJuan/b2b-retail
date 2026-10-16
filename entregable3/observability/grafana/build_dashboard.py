#!/usr/bin/env python3
"""Genera dashboards/e3.json (dashboard de Grafana del E3). Ejecutar tras modificar paneles."""
import json
from pathlib import Path

PANELS = [
    ('Fallas y kill switch', [
        ('Fallas activas', 'max by (fault, family) (e3_fault_active)', '{{family}}/{{fault}}', 'short'),
        ('Inyecciones de falla /s', 'sum by (fault) (rate(e3_fault_injections_total[5s]))', '{{fault}}', 'ops'),
        ('Mecanismos de resiliencia habilitados', 'max by (mechanism) (e3_resilience_enabled)', '{{mechanism}}', 'short'),
    ]),
    ('Servicio (RED: rate, errors, duration)', [
        ('Throughput por código HTTP', 'sum by (status) (rate(e3_http_requests_total[5s]))', '{{status}}', 'reqps'),
        ('Tasa de error (5xx+429)', 'sum(rate(e3_http_requests_total{status!="200"}[5s])) / clamp_min(sum(rate(e3_http_requests_total[5s])), 1e-9)', 'error ratio', 'percentunit'),
        ('Latencia p50/p95/p99 (servicio)', [
            ('histogram_quantile(0.50, sum by (le) (rate(e3_request_duration_seconds_bucket[10s])))', 'p50'),
            ('histogram_quantile(0.95, sum by (le) (rate(e3_request_duration_seconds_bucket[10s])))', 'p95'),
            ('histogram_quantile(0.99, sum by (le) (rate(e3_request_duration_seconds_bucket[10s])))', 'p99'),
        ], None, 's'),
        ('p95 por consulta', 'histogram_quantile(0.95, sum by (le, query) (rate(e3_request_duration_seconds_bucket[10s])))', '{{query}}', 's'),
        ('Latencia media (señal engañosa)', 'sum(rate(e3_request_duration_seconds_sum[10s])) / clamp_min(sum(rate(e3_request_duration_seconds_count[10s])), 1e-9)', 'media', 's'),
    ]),
    ('Dónde se va el tiempo: pool vs SQL', [
        ('Pool: tamaño, libres y en espera', [
            ('e3_pool_size', 'abiertas'), ('e3_pool_available', 'libres'),
            ('e3_pool_requests_waiting', 'en espera'), ('e3_pool_max_size', 'utilizables'),
            ('e3_critical_pool_available', 'crítico libres'), ('e3_critical_pool_requests_waiting', 'crítico en espera')], None, 'short'),
        ('Espera de pool p95', 'histogram_quantile(0.95, sum by (le) (rate(e3_pool_wait_seconds_bucket[10s])))', 'pool wait p95', 's'),
        ('SQL p95 por consulta', 'histogram_quantile(0.95, sum by (le, query) (rate(e3_db_query_duration_seconds_bucket[10s])))', '{{query}}', 's'),
    ]),
    ('Patrones de resiliencia', [
        ('Circuit breaker (0 cerrado, 1 semi, 2 abierto)', 'e3_circuit_breaker_state', 'estado', 'short'),
        ('Reintentos /s', 'sum by (outcome) (rate(e3_retries_total[5s]))', '{{outcome}}', 'ops'),
        ('Rechazos bulkhead /s', 'sum by (compartment) (rate(e3_bulkhead_rejections_total[5s]))', '{{compartment}}', 'ops'),
        ('Caché hit ratio', 'sum(rate(e3_cache_total{result="hit"}[10s])) / clamp_min(sum(rate(e3_cache_total[10s])), 1e-9)', 'hit ratio', 'percentunit'),
        ('Hedging /s', 'sum by (winner) (rate(e3_hedged_requests_total[5s]))', '{{winner}}', 'ops'),
        ('Reportes pesados /s', 'sum by (outcome) (rate(e3_reports_total[5s]))', '{{outcome}}', 'ops'),
    ]),
    ('PostgreSQL', [
        ('Sesiones por estado', 'sum by (state) (pg_stat_activity_count{datname="retail"})', '{{state}}', 'short'),
        ('Locks por modo', 'sum by (mode) (pg_locks_count{datname="retail"})', '{{mode}}', 'short'),
        ('Seq scans /s por tabla', 'sum by (relname) (rate(pg_stat_user_tables_seq_scan{datname="retail"}[10s]))', '{{relname}}', 'ops'),
        ('Tuplas leídas por seq scan /s', 'sum by (relname) (rate(pg_stat_user_tables_seq_tup_read{datname="retail"}[10s]))', '{{relname}}', 'short'),
    ]),
    ('Contenedores (API de Docker)', [
        ('CPU por contenedor (cores)', 'sum by (name) (docker_container_cpu_cores{name=~"b2b.*"})', '{{name}}', 'short'),
        ('Memoria working set', 'sum by (name) (docker_container_memory_working_set_bytes{name=~"b2b.*"})', '{{name}}', 'bytes'),
        ('Red RX/TX app', [
            ('sum(rate(docker_container_network_receive_bytes_total{name="b2b-e3-app"}[10s]))', 'rx'),
            ('sum(rate(docker_container_network_transmit_bytes_total{name="b2b-e3-app"}[10s]))', 'tx')], None, 'Bps'),
        ('Proceso app: CPU y GC', [
            ('rate(process_cpu_seconds_total{job="app"}[10s])', 'cpu'),
            ('sum(rate(python_gc_collections_total{job="app"}[10s]))', 'gc/s')], None, 'short'),
    ]),
]


def panel(panel_id, title, exprs, legend, unit, x, y):
    if isinstance(exprs, str):
        exprs = [(exprs, legend)]
    return {
        'id': panel_id, 'type': 'timeseries', 'title': title,
        'datasource': {'type': 'prometheus', 'uid': 'prometheus'},
        'gridPos': {'h': 8, 'w': 8, 'x': x, 'y': y},
        'fieldConfig': {'defaults': {'unit': unit, 'custom': {'lineWidth': 2, 'fillOpacity': 8}}, 'overrides': []},
        'options': {'legend': {'displayMode': 'list', 'placement': 'bottom'}, 'tooltip': {'mode': 'multi'}},
        'targets': [{'refId': chr(65 + i), 'expr': expr, 'legendFormat': leg} for i, (expr, leg) in enumerate(exprs)],
    }


def build():
    panels, y, panel_id = [], 0, 1
    for row_title, row_panels in PANELS:
        panels.append({'id': panel_id, 'type': 'row', 'title': row_title, 'collapsed': False, 'gridPos': {'h': 1, 'w': 24, 'x': 0, 'y': y}})
        panel_id += 1
        y += 1
        for index, (title, exprs, legend, unit) in enumerate(row_panels):
            panels.append(panel(panel_id, title, exprs, legend, unit, (index % 3) * 8, y + (index // 3) * 8))
            panel_id += 1
        y += ((len(row_panels) + 2) // 3) * 8
    return {
        'uid': 'e3-resiliencia', 'title': 'E3 · Resiliencia y observabilidad (B2B Retail)', 'schemaVersion': 39,
        'time': {'from': 'now-15m', 'to': 'now'}, 'refresh': '5s', 'tags': ['e3'], 'panels': panels,
        'annotations': {'list': [{
            'name': 'Fallas', 'enable': True, 'iconColor': 'red', 'datasource': {'type': 'prometheus', 'uid': 'prometheus'},
            'expr': 'max by (fault) (e3_fault_active) > 0', 'step': '1s', 'titleFormat': 'Falla {{fault}}', 'useValueForTime': False}]},
    }


if __name__ == '__main__':
    out = Path(__file__).parent / 'dashboards/e3.json'
    out.write_text(json.dumps(build(), indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'dashboard escrito en {out}')

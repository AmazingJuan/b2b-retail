#!/usr/bin/env python3
import argparse
import json
import statistics
import subprocess
import time
from datetime import date
from pathlib import Path

QUERIES = {
    'Q1': "SELECT id FROM proveedores.contrato WHERE proveedor_id = {provider} AND inicia_en <= DATE '2026-09-20' AND vence_en >= DATE '2026-09-20' LIMIT 1",
    'Q2': "SELECT id, precio FROM catalogo.sku WHERE proveedor_id = {provider} AND vigente_desde <= DATE '2026-09-20' AND vigente_hasta >= DATE '2026-09-20' LIMIT 20",
    'Q3': "SELECT id FROM logistica.franja_descargue WHERE cedi_id = {cedi} AND inicia_en::date = DATE '2026-09-20' AND reservadas < capacidad ORDER BY inicia_en",
    'Q4': "SELECT proveedor_id, date_trunc('month', creada_en) AS mes, round(100.0 * avg((entregada_en <= prometida_en)::int), 2) AS otif_pct, round(100.0 * sum(cantidad_recibida) / NULLIF(sum(cantidad_solicitada), 0), 2) AS fill_rate_pct FROM ordenes.orden_compra WHERE proveedor_id = {provider} GROUP BY proveedor_id, date_trunc('month', creada_en)",
    'Q5': "SELECT id FROM ordenes.orden_compra WHERE proveedor_id = {provider} ORDER BY creada_en DESC, id DESC LIMIT 50",
}


def psql(dsn, sql):
    command = ['psql', dsn, '-X', '-q', '-t', '-A', '-c', sql]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def measure(dsn, sql, executions):
    samples = []
    for _ in range(executions):
        started = time.perf_counter_ns()
        psql(dsn, sql)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        'executions': executions,
        'min_ms': round(min(samples), 3),
        'max_ms': round(max(samples), 3),
        'p50_ms': round(percentile(samples, .50), 3),
        'p95_ms': round(percentile(samples, .95), 3),
        'p99_ms': round(percentile(samples, .99), 3),
        'mean_ms': round(statistics.mean(samples), 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dsn', required=True)
    parser.add_argument('--output', type=Path, default=Path('evidence/metrics.json'))
    parser.add_argument('--executions', type=int, default=200)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {'measured_at': date.today().isoformat(), 'executions_per_scenario': args.executions, 'queries': {}}
    for query_id, template in QUERIES.items():
        cold = template.format(provider=100_000, cedi=5)
        hot = template.format(provider=1, cedi=1)
        evidence['queries'][query_id] = {
            'cold': measure(args.dsn, cold, args.executions),
            'hot': measure(args.dsn, hot, args.executions),
            'sql': template,
        }
    plans = Path('evidence/plans')
    plans.mkdir(parents=True, exist_ok=True)
    for label, (index, sql) in {
        'q5': ('orden_proveedor_fecha_idx', QUERIES['Q5'].format(provider=1)),
        'q4': ('orden_proveedor_fecha_idx', QUERIES['Q4'].format(provider=1)),
    }.items():
        drop_indexes = 'DROP INDEX IF EXISTS ordenes.orden_proveedor_fecha_idx; DROP INDEX IF EXISTS ordenes.ordenes_orden_proveedor_fecha_idx;'
        before = psql(args.dsn, drop_indexes + ' EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) ' + sql)
        psql(args.dsn, 'CREATE INDEX IF NOT EXISTS orden_proveedor_fecha_idx ON ordenes.orden_compra (proveedor_id, creada_en DESC, id DESC); ANALYZE ordenes.orden_compra;')
        after = psql(args.dsn, 'EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) ' + sql)
        (plans / f'{label}_before.txt').write_text(before, encoding='utf-8')
        (plans / f'{label}_after.txt').write_text(after, encoding='utf-8')
    args.output.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()

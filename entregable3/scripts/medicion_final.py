#!/usr/bin/env python3
"""Medición final del E3 contra la meta de mejora (objetivo = 0,90 × E2).

A) Método estricto del E2: misma base reconstruida (seed 42 + ruta de 5000 órdenes), mismas consultas,
   mismos escenarios frío/caliente, mismo scripts/measure.py (psql por ejecución, 200 ejecuciones
   secuenciales) y 3 repeticiones; se aplica la corrección de diseño físico (sql/mejoras.sql).
   Se ejecuta con el stack del E3 abajo, igual que la línea base del Momento 1.
B) Ruta de aplicación: las mismas consultas y escenarios, 200 ejecuciones secuenciales por consulta y
   escenario, el mismo estadístico, pero a través de la API del E3 (pool de conexiones persistente).
   Se mide sin y con las mejoras para separar el efecto del pool del de los índices.
"""
import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import momento1 as m1  # noqa: E402

E3 = m1.E3
E3_COMPOSE = ['docker', 'compose', '-f', str(m1.REPO / 'compose.yaml'), '-f', str(E3 / 'compose.e3.yaml')]
QUERY_IDS = ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']


def apply_sql(name):
    m1.run(m1.PSQL + ['-q', '-f', f'/workspace/entregable3/sql/{name}'], cwd=m1.REPO, capture_output=True)
    m1.log(f'aplicado sql/{name}')


def strict_method(out_dir, repetitions, executions):
    """A) método del E2 con la corrección de índices aplicada."""
    data_info = m1.verify_or_generate()
    timings = m1.reset_and_load()
    counts, checksum = m1.verification(out_dir)
    # Estado corregido: los índices de cobertura forman parte del sistema, así que la ruta de 5000
    # órdenes del E2 se ejecuta ya con ellos (incluye el costo de mantenerlos al insertar).
    apply_sql('mejoras.sql')
    route_data = m1.route(out_dir)
    metrics = m1.measure(out_dir, repetitions, executions)
    metrics['route'] = route_data
    metrics['secuencia'] = 'db-reset -> COPY -> ANALYZE -> verify.sql -> mejoras.sql -> ruta 5000 órdenes -> measure.py'
    (out_dir / 'metrics_final.json').write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding='utf-8')
    manifest = {'counts': counts, 'checksum': checksum, 'load_timings': timings, 'data_ok': data_info['csv_checksums_ok'],
                'postgres_settings': m1.pg_settings(), 'environment': m1.environment(), 'correccion': 'sql/mejoras.sql'}
    (out_dir / 'run-manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    return metrics


def psql_db(db, *args, stdin=None):
    return subprocess.run(['docker', 'exec', '-i', 'b2b-retail-postgres', 'psql', '-U', 'retail', '-d', db, '-q', '-v', 'ON_ERROR_STOP=1', *args],
                          check=True, capture_output=True, stdin=stdin)


def route_ab(out_dir, runs=6):
    """Ruta de 5000 órdenes sobre copias de la base recién cargada. A = ruta del E2 (scripts/application_route.py,
    sin cambios) con el diseño físico del E2; B = ruta corregida + sql/mejoras.sql. Orden ABBA… para repartir
    la deriva del host."""
    results = {'A': [], 'B': []}
    order = [('A', 'B') if i % 2 == 0 else ('B', 'A') for i in range(runs)]
    for pair in order:
        for name in pair:
            psql_db('postgres', '-c', 'DROP DATABASE IF EXISTS retail_ruta')
            psql_db('postgres', '-c', 'CREATE DATABASE retail_ruta TEMPLATE retail_base STRATEGY FILE_COPY')
            with open(E3 / 'sql' / ('mejoras.sql' if name == 'B' else 'mejoras_revert.sql')) as stream:
                psql_db('retail_ruta', stdin=stream)
            out = out_dir / 'ruta_ab.json'
            # A: ruta del E2 sin cambios; B: ruta corregida (asignación por rango, equivalente: tests/test_ruta_equivalente.py)
            script = E3 / 'scripts/ruta_corregida.py' if name == 'B' else m1.REPO / 'scripts/application_route.py'
            m1.run([sys.executable, str(script), '--dsn', m1.DSN.replace('dbname=retail', 'dbname=retail_ruta'),
                    '--orders-csv', str(m1.DATA / 'ordenes.csv'), '--output', str(out)], cwd=m1.REPO, capture_output=True)
            results[name].append(json.loads(out.read_text())['seconds'])
    psql_db('postgres', '-c', 'DROP DATABASE IF EXISTS retail_ruta')
    summary = {k: {'corridas': v, 'mediana': round(statistics.median(v), 3)} for k, v in results.items()}
    (out_dir / 'ruta_ab.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    m1.log(f'ruta A/B: {summary}')
    return summary


def api_measure(executions):
    """200 ejecuciones secuenciales por consulta/escenario a través de la API, con conexión HTTP persistente."""
    import http.client
    conn = http.client.HTTPConnection('localhost', 8000, timeout=30)
    result = {'method': 'API del E3 (pool persistente), 200 ejecuciones secuenciales, cliente HTTP keep-alive', 'queries': {}}
    for query in QUERY_IDS:
        result['queries'][query] = {}
        for scenario in ('cold', 'hot'):
            samples = []
            for _ in range(executions):
                started = time.perf_counter_ns()
                conn.request('GET', f'/consultas/{query}?escenario={scenario}')
                response = conn.getresponse()
                response.read()
                if response.status != 200:
                    raise RuntimeError(f'{query}/{scenario}: HTTP {response.status}')
                samples.append((time.perf_counter_ns() - started) / 1e6)
            result['queries'][query][scenario] = {
                'executions': executions, 'p50_ms': round(m1_percentile(samples, .5), 3),
                'p95_ms': round(m1_percentile(samples, .95), 3), 'p99_ms': round(m1_percentile(samples, .99), 3),
                'mean_ms': round(statistics.mean(samples), 3)}
    return result


def m1_percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def median_of(runs):
    merged = {'queries': {}, 'repetitions': len(runs), 'method': runs[0]['method'], 'aggregation': 'mediana entre repeticiones'}
    for query in QUERY_IDS:
        merged['queries'][query] = {}
        for scenario in ('cold', 'hot'):
            merged['queries'][query][scenario] = {
                metric: round(statistics.median(r['queries'][query][scenario][metric] for r in runs), 3)
                for metric in ('p50_ms', 'p95_ms', 'p99_ms', 'mean_ms')}
    return merged


def app_route(out_dir, repetitions, executions):
    """B) ruta de aplicación sin y con las mejoras."""
    import os
    env = {**os.environ, 'E3_TRACE_RATIO': '0.1', 'E3_BSP_DELAY_MS': '1000', 'E3_BSP_BATCH': '128'}
    subprocess.run(E3_COMPOSE + ['up', '-d'], cwd=m1.REPO, check=True, capture_output=True, env=env)
    for _ in range(90):
        try:
            urllib.request.urlopen('http://localhost:8000/health', timeout=2)
            break
        except Exception:  # noqa: BLE001 - arrancando
            time.sleep(1)
    urllib.request.urlopen(urllib.request.Request('http://localhost:8000/admin/kill-switch', method='POST'))
    urllib.request.urlopen(urllib.request.Request('http://localhost:8000/admin/resilience/reset', method='POST'))
    results = {}
    for label, sql in (('sin_mejoras', 'mejoras_revert.sql'), ('con_mejoras', 'mejoras.sql')):
        subprocess.run(E3_COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'retail', '-d', 'retail', '-q', '-f',
                                     f'/workspace/entregable3/sql/{sql}'], cwd=m1.REPO, check=True, capture_output=True)
        api_measure(20)  # calentamiento
        runs = [api_measure(executions) for _ in range(repetitions)]
        results[label] = median_of(runs)
        (out_dir / f'metrics_api_{label}.json').write_text(json.dumps(results[label], indent=2, ensure_ascii=False), encoding='utf-8')
        m1.log(f'ruta de aplicación {label} medida')
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=E3 / 'evidence/final')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--executions', type=int, default=200)
    parser.add_argument('--solo', choices=['estricto', 'aplicacion', 'corregido'])
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()  # measure.py corre con otro cwd
    args.output_dir.mkdir(parents=True, exist_ok=True)
    power = m1.power_state()
    m1.log(f'energía: {power}')
    if 'Discharging' in power['power_supply_status'].values():
        raise SystemExit('El equipo está en batería: la medición no es comparable con el E2.')
    if args.solo not in ('aplicacion', 'corregido'):
        subprocess.run(E3_COMPOSE + ['stop', 'app', 'toxiproxy', 'postgres-exporter', 'docker-exporter', 'prometheus', 'grafana', 'jaeger'],
                       cwd=m1.REPO, capture_output=True)
        subprocess.run(['docker', 'compose', '-f', str(m1.REPO / 'compose.yaml'), 'up', '-d', 'postgres'], cwd=m1.REPO, check=True, capture_output=True)
        strict_method(args.output_dir, args.repetitions, args.executions)
    if args.solo == 'corregido':
        subprocess.run(E3_COMPOSE + ['stop', 'app', 'postgres-exporter'], cwd=m1.REPO, capture_output=True)
        m1.verify_or_generate()
        m1.reset_and_load()
        psql_db('postgres', '-c', 'DROP DATABASE IF EXISTS retail_base')
        psql_db('postgres', '-c', 'CREATE DATABASE retail_base TEMPLATE retail STRATEGY FILE_COPY')   # recién cargada, como en el E2
        routes = route_ab(args.output_dir)
        psql_db('postgres', '-c', 'DROP DATABASE IF EXISTS retail_base')
        m1.route(args.output_dir / 'ruta_canonica')                                                     # estado canónico para las consultas
        results = app_route(args.output_dir, args.repetitions, args.executions)
        for label in results:
            data = json.loads((args.output_dir / f'metrics_api_{label}.json').read_text())
            data['route'] = {'seconds': routes['B' if label == 'con_mejoras' else 'A']['mediana'], 'detalle': 'mediana de ruta_ab.json'}
            (args.output_dir / f'metrics_api_{label}.json').write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    elif args.solo != 'estricto':
        app_route(args.output_dir, args.repetitions, args.executions)
    for name, label in (('metrics_final', 'Final E3 · método E2 + índices'), ('metrics_api_sin_mejoras', 'API con pool, sin índices'),
                        ('metrics_api_con_mejoras', 'API con pool + índices')):
        path = args.output_dir / f'{name}.json'
        if path.exists():
            subprocess.run([sys.executable, str(E3 / 'scripts/objetivos.py'), '--compare', str(path), '--label', label],
                           check=True, capture_output=True)
    m1.log(f'medición final escrita en {args.output_dir}')


if __name__ == '__main__':
    main()

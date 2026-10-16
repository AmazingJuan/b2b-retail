#!/usr/bin/env python3
"""Momento 1 del E3: reconstruye la corrida canónica del E2 y mide la línea base.

Pasos (todos reproducibles, sin fallas activas):
  1. Verifica que los CSV del seed 42 coincidan con los checksums del manifest del E2
     (si no existen o no coinciden, los regenera con datos/generar.py y los mismos parámetros).
  2. Deja la base limpia (modelo-fisico.sql), carga con \\copy y ejecuta ANALYZE.
  3. Corre sql/verify.sql (conteos, sesgo, casos borde, checksum de determinismo).
  4. Ejecuta la ruta transaccional del E2 (5 000 órdenes), igual que `make medir`.
  5. Mide Q1–Q5 frío/caliente con scripts/measure.py del E2 (sin modificarlo), N repeticiones.
  6. Registra entorno, configuración y condiciones de ejecución.
"""
import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
E3 = REPO / 'entregable3'
DATA = REPO / 'datos/generated'
MANIFEST = E3 / 'evidence/e2_referencia/manifest.json'
DSN = 'host=localhost port=5433 dbname=retail user=retail password=retail'
COMPOSE = ['docker', 'compose', '-f', str(REPO / 'compose.yaml')]
PSQL = COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'retail', '-d', 'retail', '-v', 'ON_ERROR_STOP=1']
E2_PARAMS = {'seed': 42, 'orders': 510000, 'as_of_date': '2026-09-20'}


def log(message):
    print(f'[momento1 {datetime.now().strftime("%H:%M:%S")}] {message}', flush=True)


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_or_generate():
    manifest = json.loads(MANIFEST.read_text())
    expected = manifest['checksums_csv']
    status = {}
    for name, checksum in expected.items():
        path = DATA / name
        status[name] = path.exists() and sha256(path) == checksum
    if not all(status.values()):
        log(f'CSV ausentes o distintos ({status}); regenerando con seed {E2_PARAMS["seed"]}')
        run([sys.executable, str(REPO / 'datos/generar.py'), '--output', str(DATA), '--seed', str(E2_PARAMS['seed']),
             '--orders', str(E2_PARAMS['orders']), '--as-of-date', E2_PARAMS['as_of_date']], cwd=REPO)
        status = {name: sha256(DATA / name) == checksum for name, checksum in expected.items()}
    if not all(status.values()):
        raise SystemExit(f'Los CSV regenerados no coinciden con el manifest del E2: {status}')
    log('checksums SHA-256 de los 7 CSV coinciden con el manifest del E2')
    return {'manifest': manifest, 'csv_checksums_ok': status}


def reset_and_load():
    timings = {}
    run(COMPOSE + ['up', '-d', 'postgres'], cwd=REPO, capture_output=True)
    run(COMPOSE + ['exec', '-T', 'postgres', 'sh', '-c', 'until pg_isready -U retail -d retail; do sleep 1; done'], cwd=REPO, capture_output=True)
    started = time.perf_counter()
    run(PSQL + ['-q', '-f', '/workspace/modelo-fisico.sql'], cwd=REPO, capture_output=True)
    timings['db_reset_s'] = round(time.perf_counter() - started, 3)
    run(COMPOSE + ['exec', '-T', 'postgres', 'sh', '-c', 'rm -rf /tmp/retail-data && mkdir -p /tmp/retail-data'], cwd=REPO)
    run(COMPOSE + ['cp', f'{DATA}/.', 'postgres:/tmp/retail-data/'], cwd=REPO, capture_output=True)
    started = time.perf_counter()
    run(PSQL + ['-q', '-f', '/workspace/sql/load.sql'], cwd=REPO, capture_output=True)
    timings['copy_s'] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    run(PSQL + ['-q', '-c', 'ANALYZE;'], cwd=REPO, capture_output=True)
    timings['analyze_s'] = round(time.perf_counter() - started, 3)
    log(f'base limpia y cargada: {timings}')
    return timings


def verification(out_dir):
    result = run(PSQL + ['-f', '/workspace/sql/verify.sql'], cwd=REPO, capture_output=True)
    (out_dir / 'verification.txt').write_text(result.stdout, encoding='utf-8')
    counts = run(PSQL + ['-t', '-A', '-F', ',', '-c', """
        SELECT 'proveedores', count(*) FROM proveedores.proveedor UNION ALL
        SELECT 'contratos', count(*) FROM proveedores.contrato UNION ALL
        SELECT 'skus', count(*) FROM catalogo.sku UNION ALL
        SELECT 'ordenes', count(*) FROM ordenes.orden_compra UNION ALL
        SELECT 'lineas', count(*) FROM ordenes.linea_orden UNION ALL
        SELECT 'franjas', count(*) FROM logistica.franja_descargue UNION ALL
        SELECT 'eventos', count(*) FROM auditoria.evento"""], cwd=REPO, capture_output=True)
    rows = dict(line.split(',') for line in counts.stdout.strip().splitlines())
    checksum = next((line for line in result.stdout.splitlines() if 'determinismo_checksum' in line), '').split('|')[-1].strip()
    log(f'conteos: {rows}; checksum determinismo: {checksum}')
    return {k: int(v) for k, v in rows.items()}, checksum


def route(out_dir):
    output = out_dir / 'application-route.json'
    run([sys.executable, str(REPO / 'scripts/application_route.py'), '--dsn', DSN,
         '--orders-csv', str(DATA / 'ordenes.csv'), '--output', str(output)], cwd=REPO, capture_output=True)
    data = json.loads(output.read_text())
    log(f'ruta transaccional: {data["accepted_rows"]}/{data["attempted_rows"]} filas en {data["seconds"]} s')
    return data


def measure(out_dir, repetitions, executions):
    runs = []
    for rep in range(1, repetitions + 1):
        rep_dir = out_dir / f'rep{rep}'
        rep_dir.mkdir(parents=True, exist_ok=True)
        # measure.py escribe evidence/plans relativo al cwd: se ejecuta dentro de rep_dir para no tocar la evidencia del E2.
        run([sys.executable, str(REPO / 'scripts/measure.py'), '--dsn', DSN, '--output', str(rep_dir / 'metrics.json'),
             '--executions', str(executions)], cwd=rep_dir, capture_output=True)
        runs.append(json.loads((rep_dir / 'metrics.json').read_text()))
        log(f'repetición {rep}/{repetitions} medida')
    aggregate = {'method': 'scripts/measure.py del E2 (psql por ejecución, 200 ejecuciones secuenciales)',
                 'repetitions': repetitions, 'aggregation': 'mediana entre repeticiones', 'queries': {}, 'dispersion': {}}
    for query_id in runs[0]['queries']:
        aggregate['queries'][query_id] = {}
        aggregate['dispersion'][query_id] = {}
        for scenario in ('cold', 'hot'):
            aggregate['queries'][query_id][scenario] = {}
            aggregate['dispersion'][query_id][scenario] = {}
            for metric in ('p50_ms', 'p95_ms', 'p99_ms', 'mean_ms'):
                values = [r['queries'][query_id][scenario][metric] for r in runs]
                aggregate['queries'][query_id][scenario][metric] = round(statistics.median(values), 3)
                aggregate['dispersion'][query_id][scenario][metric] = {
                    'min': min(values), 'max': max(values),
                    'cv_pct': round(100 * statistics.pstdev(values) / statistics.mean(values), 2)}
    return aggregate


def pg_settings():
    result = run(PSQL + ['-t', '-A', '-F', '=', '-c',
                         "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN "
                         "('server_version','shared_buffers','work_mem','effective_cache_size','max_connections',"
                         "'random_page_cost','max_parallel_workers_per_gather','jit','TimeZone')"], cwd=REPO, capture_output=True)
    return dict(line.split('=', 1) for line in result.stdout.strip().splitlines())


def environment():
    def out(command):
        try:
            return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        except Exception as error:  # noqa: BLE001 - solo es registro del entorno
            return f'n/d ({error})'
    meminfo = Path('/proc/meminfo').read_text().splitlines()[0] if Path('/proc/meminfo').exists() else 'n/d'
    return {
        'host': platform.node(), 'kernel': platform.release(), 'python': platform.python_version(),
        'cpu_count': os.cpu_count(), 'memoria': meminfo,
        'cpu_model': next((l.split(':', 1)[1].strip() for l in Path('/proc/cpuinfo').read_text().splitlines() if l.startswith('model name')), 'n/d'),
        'docker': out(['docker', '--version']), 'psql_cliente': out(['psql', '--version']),
        'git_commit': out(['git', '-C', str(REPO), 'rev-parse', 'HEAD']),
        'git_branch': out(['git', '-C', str(REPO), 'rev-parse', '--abbrev-ref', 'HEAD']),
        'imagen_postgres': 'postgres:16-alpine (compose.yaml)',
        'energia': power_state(),
        'load_average': out(['cat', '/proc/loadavg']),
    }


def power_state():
    """Estado de energía/CPU: en batería con governor powersave la latencia se triplica."""
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    supplies = {p.name: read(p / 'status') for p in Path('/sys/class/power_supply').glob('*') if (p / 'status').exists()}
    online = {p.name: read(p / 'online') for p in Path('/sys/class/power_supply').glob('*') if (p / 'online').exists()}
    cpu = '/sys/devices/system/cpu/cpu0/cpufreq/'
    return {'power_supply_status': supplies, 'ac_online': online,
            'governor': read(cpu + 'scaling_governor'), 'energy_performance_preference': read(cpu + 'energy_performance_preference'),
            'cur_freq_khz': read(cpu + 'scaling_cur_freq'), 'max_freq_khz': read(cpu + 'cpuinfo_max_freq')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=E3 / 'evidence/baseline')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--executions', type=int, default=200)
    parser.add_argument('--skip-load', action='store_true', help='mide sin recargar (solo para depurar)')
    parser.add_argument('--allow-battery', action='store_true', help='permite medir en batería (resultado no comparable)')
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()  # measure.py corre con otro cwd
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()

    if subprocess.run(['docker', 'ps', '-q', '-f', 'name=b2b-e3-app'], capture_output=True, text=True).stdout.strip():
        raise SystemExit('El stack del E3 está arriba. Ejecute `make e3-down` primero: la línea base se mide en las condiciones del E2.')
    power = power_state()
    log(f'energía: {power}')
    if 'Discharging' in power['power_supply_status'].values() and not args.allow_battery:
        raise SystemExit('El equipo está en batería: la medición no es comparable con el E2. Conecte el cargador o use --allow-battery.')
    data_info = verify_or_generate()
    timings = {} if args.skip_load else reset_and_load()
    counts, checksum = verification(args.output_dir)
    route_data = {} if args.skip_load else route(args.output_dir)
    metrics = measure(args.output_dir, args.repetitions, args.executions)
    metrics['route'] = route_data
    (args.output_dir / 'metrics_baseline.json').write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding='utf-8')

    run_manifest = {
        'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
        'faults_active': False,
        'e2_reference': {'commit': 'd9d4910', 'measured_at': '2026-09-27', 'params': E2_PARAMS},
        'data': {'manifest_e2': data_info['manifest'], 'csv_checksums_ok': data_info['csv_checksums_ok']},
        'counts_after_load_and_route': counts, 'determinism_checksum_first_10000_orders': checksum,
        'load_timings': timings, 'postgres_settings': pg_settings(), 'environment': environment(),
        'conditions': {'measure_executions_per_scenario': args.executions, 'repetitions': args.repetitions,
                       'cold': 'proveedor_id=100000, cedi_id=5', 'hot': 'proveedor_id=1, cedi_id=1',
                       'sequence': 'db-reset -> COPY -> ANALYZE -> verify.sql -> ruta 5000 órdenes -> measure.py (igual que make datos + make medir del E2)'},
    }
    (args.output_dir / 'run-manifest.json').write_text(json.dumps(run_manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    run([sys.executable, str(E3 / 'scripts/objetivos.py'), '--compare', str(args.output_dir / 'metrics_baseline.json'),
         '--label', 'Baseline E3'], capture_output=True)
    log(f'línea base escrita en {args.output_dir}')


if __name__ == '__main__':
    main()

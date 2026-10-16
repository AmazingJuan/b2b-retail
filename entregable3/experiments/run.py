#!/usr/bin/env python3
"""Orquestador de experimentos del E3.

Por cada experimento y repetición ejecuta, con carga constante de lazo abierto:
  baseline → falla → recuperacion (kill switch) → correccion → falla_correccion → recuperacion_correccion
y guarda: solicitudes crudas (CSV), resumen por fase, verificación de la predicción previa,
series de Prometheus, trazas más lentas de Jaeger, logs JSON del servicio y muestras de docker stats.

Uso:  python3 entregable3/experiments/run.py E1 E3 --reps 3      (o "all")
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analysis import aggregate, check_prediction, phase_stats, recovery_time, timeline  # noqa: E402
from loadgen import LoadGenerator  # noqa: E402

REPO = HERE.parents[1]
EVIDENCE = REPO / 'entregable3/evidence/experimentos'
API = 'http://localhost:8000'
PROM = 'http://localhost:9090'
JAEGER = 'http://localhost:16686'
COMPOSE = ['docker', 'compose', '-f', str(REPO / 'compose.yaml'), '-f', str(REPO / 'entregable3/compose.e3.yaml')]
PHASES = ['baseline', 'falla', 'recuperacion', 'correccion', 'falla_correccion', 'recuperacion_correccion']

PROM_SIGNALS = {
    'rps_by_status': 'sum by (status) (rate(e3_http_requests_total[3s]))',
    'svc_p50': 'histogram_quantile(0.50, sum by (le) (rate(e3_request_duration_seconds_bucket[5s])))',
    'svc_p95': 'histogram_quantile(0.95, sum by (le) (rate(e3_request_duration_seconds_bucket[5s])))',
    'svc_p99': 'histogram_quantile(0.99, sum by (le) (rate(e3_request_duration_seconds_bucket[5s])))',
    'svc_mean': 'sum(rate(e3_request_duration_seconds_sum[5s])) / clamp_min(sum(rate(e3_request_duration_seconds_count[5s])), 1e-9)',
    'db_p95_by_query': 'histogram_quantile(0.95, sum by (le, query) (rate(e3_db_query_duration_seconds_bucket[5s])))',
    'pool_wait_p95': 'histogram_quantile(0.95, sum by (le) (rate(e3_pool_wait_seconds_bucket[5s])))',
    'pool_available': 'e3_pool_available', 'pool_waiting': 'e3_pool_requests_waiting', 'pool_size': 'e3_pool_size',
    'fault_active': 'max by (fault) (e3_fault_active)',
    'cb_state': 'e3_circuit_breaker_state',
    'retries': 'sum by (outcome) (rate(e3_retries_total[3s]))',
    'bulkhead_rejections': 'sum by (compartment) (rate(e3_bulkhead_rejections_total[3s]))',
    'cache_hits': 'sum by (result) (rate(e3_cache_total[3s]))',
    'hedges': 'sum by (winner) (rate(e3_hedged_requests_total[3s]))',
    'reports': 'sum by (outcome) (rate(e3_reports_total[3s]))',
    'pg_sessions_by_state': 'sum by (state) (pg_stat_activity_count{datname="retail"})',
    'pg_locks_by_mode': 'sum by (mode) (pg_locks_count{datname="retail"})',
    'pg_seq_scan_orden_compra': 'sum(rate(pg_stat_user_tables_seq_scan{datname="retail",relname="orden_compra"}[5s]))',
    'pg_seq_tup_read': 'sum by (relname) (rate(pg_stat_user_tables_seq_tup_read{datname="retail"}[5s]))',
    'pg_idx_scan_orden_compra': 'sum(rate(pg_stat_user_tables_idx_scan{datname="retail",relname="orden_compra"}[5s]))',
    'cpu_by_container': 'sum by (name) (docker_container_cpu_cores{name=~"b2b.*"})',
    'mem_by_container': 'sum by (name) (docker_container_memory_working_set_bytes{name=~"b2b.*"})',
    'app_net_tx': 'sum(rate(docker_container_network_transmit_bytes_total{name="b2b-e3-app"}[5s]))',
    'app_process_cpu': 'rate(process_cpu_seconds_total{job="app"}[5s])',
    'app_gc': 'sum(rate(python_gc_collections_total{job="app"}[5s]))',
}


def log(message):
    print(f'[run {datetime.now().strftime("%H:%M:%S")}] {message}', flush=True)


def http(method, path, body=None, base=API, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def wait_healthy(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            health = http('GET', '/health')
            if health['pool']['pool_size'] >= 1:
                return health
        except Exception:  # noqa: BLE001 - aún arrancando
            pass
        time.sleep(1)
    raise RuntimeError('la API no quedó saludable')


def psql_file(path):
    rel = Path(path)
    run = subprocess.run(COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'retail', '-d', 'retail', '-v', 'ON_ERROR_STOP=1',
                                    '-f', f'/workspace/entregable3/{rel}'], capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(run.stderr)
    return run.stdout


class DockerStats(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            out = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}'], capture_output=True, text=True)
            ts = time.time()
            for line in out.stdout.splitlines():
                item = json.loads(line)
                if item.get('Name', '').startswith('b2b'):
                    self.samples.append({'t': ts, 'name': item['Name'], 'cpu': item['CPUPerc'], 'mem': item['MemUsage']})


def prometheus_range(start, end):
    result = {}
    for name, expr in PROM_SIGNALS.items():
        query = urllib.parse.urlencode({'query': expr, 'start': start, 'end': end, 'step': '1'})
        try:
            data = http('GET', f'/api/v1/query_range?{query}', base=PROM)
            result[name] = [{'labels': s['metric'], 'values': [[float(t), float(v)] for t, v in s['values']]}
                            for s in data['data']['result']]
        except Exception as error:  # noqa: BLE001 - una señal ausente no invalida el experimento
            result[name] = {'error': repr(error)}
    return result


def slowest_traces(start, end, limit=5):
    query = urllib.parse.urlencode({'service': 'b2b-e3-api', 'start': int(start * 1e6), 'end': int(end * 1e6),
                                    'limit': 300, 'minDuration': '50ms'})
    try:
        data = http('GET', f'/api/traces?{query}', base=JAEGER)
    except Exception as error:  # noqa: BLE001
        return {'error': repr(error)}
    traces = []
    for item in data.get('data', []):
        spans = item['spans']
        root = min(spans, key=lambda s: s['startTime'])
        traces.append({
            'trace_id': item['traceID'], 'duration_ms': round(root['duration'] / 1000, 2), 'root': root['operationName'],
            'spans': sorted([{'op': s['operationName'], 'ms': round(s['duration'] / 1000, 2),
                              'offset_ms': round((s['startTime'] - root['startTime']) / 1000, 2),
                              'tags': {t['key']: t['value'] for t in s.get('tags', []) if t['key'].startswith(('e3.', 'pool.', 'db.statement', 'error'))},
                              'events': [l.get('fields', [{}])[0].get('value') for l in s.get('logs', [])]}
                             for s in spans], key=lambda s: s['offset_ms']),
        })
    return sorted(traces, key=lambda t: -t['duration_ms'])[:limit]


def app_logs(start, end):
    since = datetime.fromtimestamp(start, timezone.utc).isoformat()
    until = datetime.fromtimestamp(end, timezone.utc).isoformat()
    out = subprocess.run(['docker', 'logs', '--since', since, '--until', until, 'b2b-e3-app'], capture_output=True, text=True)
    return [line for line in (out.stdout + out.stderr).splitlines() if line.startswith('{')]


def reset_system(experiment=None):
    http('POST', '/admin/kill-switch')
    http('POST', '/admin/resilience/reset')
    if experiment and experiment.get('correction', {}).get('sql_revert'):
        psql_file(experiment['correction']['sql_revert'])
    http('POST', '/admin/arm')


def run_once(experiment, rep, out_dir, phase_s, warmup_s):
    out_dir.mkdir(parents=True, exist_ok=True)
    reset_system(experiment)
    wait_healthy()
    load = experiment['load']
    warm = LoadGenerator(API, load['rate'], load.get('workers', 64), load.get('mix'), load.get('scenarios'), seed=7)
    warm.start()
    time.sleep(warmup_s)
    warm.stop()

    generator = LoadGenerator(API, load['rate'], load.get('workers', 64), load.get('mix'), load.get('scenarios'), seed=42 + rep)
    stats = DockerStats()
    stats.start()
    marks, actions = {}, []

    def act(label, fn):
        started = time.time()
        result = fn()
        actions.append({'t': started, 'action': label, 'elapsed_ms': round((time.time() - started) * 1000, 1), 'result': result})

    fault = experiment['fault']
    correction = experiment.get('correction', {})
    generator.start()
    t_begin = generator.wall0
    for phase in PHASES:
        marks[phase] = time.time()
        if phase == 'falla' or phase == 'falla_correccion':
            act(f'enable {fault["name"]}', lambda: http('POST', f'/admin/faults/{fault["name"]}', {'params': fault.get('params', {})}))
        elif phase.startswith('recuperacion'):
            act('kill-switch', lambda: http('POST', '/admin/kill-switch'))
        elif phase == 'correccion':
            act('arm', lambda: http('POST', '/admin/arm'))
            if correction.get('sql_apply'):
                act(f'sql {correction["sql_apply"]}', lambda: psql_file(correction['sql_apply'])[-500:])
            if correction.get('resilience'):
                act('resilience', lambda: http('PUT', '/admin/resilience', correction['resilience']))
        time.sleep(max(0.0, marks[phase] + phase_s - time.time()))
    t_end = time.time()
    generator.stop()
    stats.stop.set()
    reset_system(experiment)

    rows = generator.dump(out_dir / 'requests.csv')
    bounds = {p: (marks[p], marks[PHASES[i + 1]] if i + 1 < len(PHASES) else t_end) for i, p in enumerate(PHASES)}
    summary = {'experiment': experiment['id'], 'rep': rep, 'phase_s': phase_s,
               'phases': {p: phase_stats(rows, *bounds[p]) for p in PHASES},
               'bounds': {p: [round(a - t_begin, 2), round(b - t_begin, 2)] for p, (a, b) in bounds.items()},
               'actions': [{**a, 't_rel': round(a['t'] - t_begin, 2)} for a in actions]}
    series = timeline(rows, t_begin, t_end)
    base_p95 = summary['phases']['baseline']['p95_ms']
    summary['recovery'] = {p: recovery_time(series, summary['bounds'][p][0], base_p95) for p in ('recuperacion', 'recuperacion_correccion')}
    summary['predictions'] = [check_prediction(summary, c) for c in experiment['prediccion']['checks']]
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    (out_dir / 'timeline.json').write_text(json.dumps(series), encoding='utf-8')
    (out_dir / 'prometheus.json').write_text(json.dumps(prometheus_range(t_begin, t_end)), encoding='utf-8')
    fault_window = bounds['falla']
    (out_dir / 'traces_falla.json').write_text(json.dumps(slowest_traces(*fault_window), indent=1), encoding='utf-8')
    (out_dir / 'traces_falla_correccion.json').write_text(json.dumps(slowest_traces(*bounds['falla_correccion']), indent=1), encoding='utf-8')
    (out_dir / 'app_logs.jsonl').write_text('\n'.join(app_logs(t_begin, t_end)) + '\n', encoding='utf-8')
    (out_dir / 'docker_stats.json').write_text(json.dumps(stats.samples), encoding='utf-8')
    ok = sum(p['ok'] for p in summary['predictions'])
    log(f"{experiment['id']} rep {rep}: predicciones {ok}/{len(summary['predictions'])} · "
        + ' | '.join(f"{p}: p95={summary['phases'][p].get('p95_ms')} err={summary['phases'][p].get('error_rate')}" for p in PHASES))
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('experiments', nargs='+', help='IDs (E1..E8) o "all"')
    parser.add_argument('--reps', type=int, default=3)
    parser.add_argument('--phase-s', type=float, default=30)
    parser.add_argument('--warmup-s', type=float, default=10)
    parser.add_argument('--definitions', type=Path, default=HERE / 'experiments.json')
    args = parser.parse_args()
    definitions = {e['id']: e for e in json.loads(args.definitions.read_text())['experimentos']}
    selected = list(definitions) if args.experiments == ['all'] else args.experiments
    wait_healthy()
    for experiment_id in selected:
        experiment = definitions[experiment_id]
        exp_dir = EVIDENCE / experiment_id
        manifest = {'experiment': experiment, 'started_at': datetime.now(timezone.utc).isoformat(), 'reps': args.reps,
                    'phase_s': args.phase_s, 'warmup_s': args.warmup_s, 'phases': PHASES,
                    'git_commit': subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip(),
                    'state_before': http('GET', '/admin/state')}
        summaries = []
        for rep in range(1, args.reps + 1):
            log(f'{experiment_id} repetición {rep}/{args.reps}: {experiment["titulo"]}')
            summaries.append(run_once(experiment, rep, exp_dir / f'rep{rep}', args.phase_s, args.warmup_s))
        manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
        result = {'experiment': experiment_id, 'aggregate': aggregate(summaries),
                  'recovery': {p: [s['recovery'][p] for s in summaries] for p in ('recuperacion', 'recuperacion_correccion')},
                  'predictions_per_rep': [[{'metric': c['metric'], 'phase': c['phase'], 'observed': c['observed'], 'ok': c['ok']}
                                           for c in s['predictions']] for s in summaries]}
        (exp_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        (exp_dir / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    log('listo')


if __name__ == '__main__':
    main()

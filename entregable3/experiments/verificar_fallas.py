#!/usr/bin/env python3
"""Verificación automática de las cinco condiciones de cada falla configurable.

Para cada una de las 8 fallas:
  1. Sin redespliegue: se activa por API y el contenedor de la app conserva su StartedAt.
  2. Parametrizable: los parámetros enviados quedan aplicados (estado de /admin/faults).
  3. Efecto observable: una sonda específica muestra la falla y e3_fault_active=1 en /metrics.
  4. Kill switch: POST /admin/kill-switch la desactiva y bloquea nuevas activaciones (409).
  5. Reversible: tras el kill switch la sonda vuelve al comportamiento del baseline.
Además prueba la expiración automática (duration_s) con una falla.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = 'http://localhost:8000'
TOXIPROXY = 'http://localhost:8474'
OUT = Path(__file__).resolve().parents[1] / 'evidence/verificacion_fallas.json'


def call(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


def started_at():
    return subprocess.run(['docker', 'inspect', '-f', '{{.State.StartedAt}}', 'b2b-e3-app'], capture_output=True, text=True).stdout.strip()


def metric(name, labels):
    with urllib.request.urlopen(f'{API}/metrics') as response:
        for line in response.read().decode().splitlines():
            if line.startswith(name + '{') and all(f'{k}="{v}"' in line for k, v in labels.items()):
                return float(line.rsplit(' ', 1)[1])
    return None


def timed_queries(query, n=20, scenario='hot'):
    statuses, latencies = [], []
    for _ in range(n):
        started = time.perf_counter()
        status, _ = call('GET', f'{API}/consultas/{query}?escenario={scenario}', timeout=40)
        latencies.append((time.perf_counter() - started) * 1000)
        statuses.append(status)
    latencies.sort()
    return {'ok_ratio': statuses.count(200) / n, 'p50_ms': round(latencies[n // 2], 2), 'max_ms': round(latencies[-1], 2)}


def concurrent_probe(query, n=40, workers=20):
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda _: timed_queries(query, 1), range(n)))
    return {'ok_ratio': sum(r['ok_ratio'] for r in results) / n,
            'p50_ms': sorted(r['p50_ms'] for r in results)[n // 2], 'max_ms': max(r['max_ms'] for r in results)}


def pool_usable():
    """Conexiones que la app puede usar = abiertas - retenidas por la falla (métrica e3_pool_max_size)."""
    return _gauge('e3_pool_max_size')


def _gauge(name):
    with urllib.request.urlopen(f'{API}/metrics') as response:
        for line in response.read().decode().splitlines():
            if line.startswith(name + ' '):
                return float(line.rsplit(' ', 1)[1])
    return None


def pool_stats():
    return call('GET', f'{API}/health')[1]['pool']


def pg_probe(sql):
    out = subprocess.run(['docker', 'exec', 'b2b-retail-postgres', 'psql', '-U', 'retail', '-d', 'retail', '-t', '-A', '-c', sql],
                         capture_output=True, text=True)
    return out.stdout.strip()


# falla -> (parámetros no default, sonda, predicado de "efecto visible" comparando sonda con baseline)
CASES = {
    'net_latency': ({'latency_ms': 60, 'jitter_ms': 0}, lambda: timed_queries('Q1'),
                    lambda base, now: now['p50_ms'] >= base['p50_ms'] + 50),
    'net_partition': ({}, lambda: {**timed_queries('Q1', 5), 'proxy_enabled': call('GET', f'{TOXIPROXY}/proxies/postgres')[1]['enabled']},
                      lambda base, now: now['ok_ratio'] == 0 and now['proxy_enabled'] is False),
    'app_error': ({'probability': 0.5, 'queries': ['Q2']}, lambda: timed_queries('Q2', 40),
                  lambda base, now: 0.25 <= now['ok_ratio'] <= 0.75),
    'app_latency': ({'probability': 1.0, 'delay_ms': 300, 'queries': ['Q1']}, lambda: timed_queries('Q1', 5),
                    lambda base, now: now['p50_ms'] >= 300),
    'pool_shrink': ({'max_size': 1}, lambda: {**concurrent_probe('Q4'), 'pool_utilizable': pool_usable()},
                    lambda base, now: now['pool_utilizable'] == 1 and now['p50_ms'] > base['p50_ms']),
    'noisy_neighbor': ({'sessions': 2}, lambda: {**timed_queries('Q3'), 'reportes_activos': int(pg_probe(
        "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'SELECT sku_id, sum(cantidad%' AND state = 'active'") or 0)},
                       lambda base, now: now['reportes_activos'] >= 1),
    'index_drop': ({}, lambda: {**timed_queries('Q5'), 'indice_existe': pg_probe(
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'orden_proveedor_fecha_idx'") == '1'},
                   lambda base, now: now['indice_existe'] is False),
    'table_lock': ({}, lambda: {**timed_queries('Q1', 5), 'locks_exclusivos': int(pg_probe(
        "SELECT count(*) FROM pg_locks WHERE mode = 'AccessExclusiveLock' AND relation = 'logistica.franja_descargue'::regclass AND granted") or 0)},
                   lambda base, now: now['locks_exclusivos'] == 1),
}


def main():
    call('POST', f'{API}/admin/kill-switch')
    call('POST', f'{API}/admin/resilience/reset')
    call('POST', f'{API}/admin/arm')
    # Calentamiento: con el servicio recién levantado la primera sonda sale en frío (~33 ms vs ~6 ms)
    # y ensucia el baseline de la primera falla.
    for query in ('Q1', 'Q2', 'Q3', 'Q4', 'Q5'):
        timed_queries(query, 30)
    boot = started_at()
    report = {'container_started_at': boot, 'faults': {}}
    for name, (params, probe, effect) in CASES.items():
        result = {}
        base = probe()
        status, state = call('POST', f'{API}/admin/faults/{name}', {'params': params})
        time.sleep(1)
        now = probe()
        result['1_sin_redespliegue'] = status == 200 and started_at() == boot
        result['2_parametrizable'] = all(state['params'].get(k) == v for k, v in params.items())
        result['3_observable'] = effect(base, now) and metric('e3_fault_active', {'fault': name}) == 1.0
        status_ks, ks = call('POST', f'{API}/admin/kill-switch')
        blocked, _ = call('POST', f'{API}/admin/faults/{name}', {'params': params})
        result['4_kill_switch'] = status_ks == 200 and not ks['active'] and blocked == 409 and metric('e3_fault_active', {'fault': name}) == 0.0
        call('POST', f'{API}/admin/arm')
        time.sleep(3 if name == 'net_partition' else 1)
        after = probe()
        if name == 'net_partition':
            time.sleep(8)  # el pool reconecta con backoff propio; se mide la reversión, no la velocidad
            after = probe()
        pool = pool_stats()
        result['pool_despues'] = {k: pool.get(k) for k in ('pool_size', 'pool_available', 'pool_max')}
        result['5_reversible'] = (after.get('ok_ratio', 1) >= base.get('ok_ratio', 1) and not effect(base, after)
                                  and pool['pool_size'] <= pool['pool_max'])
        result['kill_switch_ms'] = ks['elapsed_ms']
        result['sondas'] = {'baseline': base, 'falla': now, 'despues_kill_switch': after}
        result['cumple_5'] = all(result[k] for k in result if k[0].isdigit())
        report['faults'][name] = result
        print(f"{name:15s} {'OK' if result['cumple_5'] else 'FALLA'} "
              + ' '.join(f"{k}={'✔' if result[k] else '✘'}" for k in result if k[0].isdigit()) + f" (kill switch {ks['elapsed_ms']} ms)", flush=True)

    call('POST', f'{API}/admin/faults/app_latency', {'params': {'probability': 1.0, 'delay_ms': 200, 'queries': ['Q1']}, 'duration_s': 2})
    during = timed_queries('Q1', 3)
    time.sleep(2.5)
    report['expiracion_automatica'] = {'durante': during, 'despues': timed_queries('Q1', 3),
                                       'activa_despues': call('GET', f'{API}/admin/faults')[1]['faults']['app_latency']['active']}
    report['container_started_at_final'] = started_at()
    report['todas_cumplen'] = all(f['cumple_5'] for f in report['faults'].values()) and report['container_started_at_final'] == boot
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('expiración automática:', report['expiracion_automatica'])
    print('TODAS CUMPLEN' if report['todas_cumplen'] else 'HAY FALLAS QUE NO CUMPLEN', '→', OUT)
    sys.exit(0 if report['todas_cumplen'] else 1)


if __name__ == '__main__':
    main()

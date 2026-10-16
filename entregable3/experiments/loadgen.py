#!/usr/bin/env python3
"""Generador de carga de lazo abierto (tasa constante) solo con la librería estándar.

- La solicitud k se programa en t0 + k/rate y la atiende el worker k mod W (estilo wrk2).
- La latencia se mide desde el instante *programado*, no desde el envío: si el sistema se
  bloquea, la espera acumulada aparece en la latencia (corrige la "coordinated omission").
- Mezcla de consultas y escenarios reproducible (semilla fija por worker).
"""
import bisect
import csv
import http.client
import json
import random
import threading
import time
from urllib.parse import urlparse

ZIPF_S = 1.08          # mismo parámetro de sesgo del generador del E2
PROVIDERS = 105_000


def zipf_cdf(n=PROVIDERS, s=ZIPF_S):
    total, cdf = 0.0, []
    for rank in range(1, n + 1):
        total += 1.0 / rank ** s
        cdf.append(total)
    return [value / total for value in cdf]


class LoadGenerator:
    def __init__(self, base_url, rate, workers=64, mix=None, scenarios=None, seed=42, timeout_s=40):
        parsed = urlparse(base_url)
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.rate = float(rate)
        self.workers = int(workers)
        self.mix = mix or {'Q1': .25, 'Q2': .25, 'Q3': .2, 'Q4': .15, 'Q5': .15}
        self.scenarios = scenarios or {'hot': .5, 'cold': .5}
        self.seed = seed
        self.timeout_s = timeout_s
        self.records = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []
        self._cdf = zipf_cdf() if 'zipf' in self.scenarios else None

    def _choose(self, rng, weights):
        keys = list(weights)
        return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]

    def _path(self, rng):
        query = self._choose(rng, self.mix)
        scenario = self._choose(rng, self.scenarios)
        if scenario == 'zipf':
            provider = bisect.bisect_left(self._cdf, rng.random()) + 1
            return query, scenario, f'/consultas/{query}?proveedor={provider}&cedi={rng.randint(1, 5)}'
        return query, scenario, f'/consultas/{query}?escenario={scenario}'

    def _worker(self, index):
        rng = random.Random(self.seed * 1000 + index)
        conn = None
        k = index
        while not self._stop.is_set():
            scheduled = self.t0 + k / self.rate
            k += self.workers
            delay = scheduled - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            query, scenario, path = self._path(rng)
            sent = time.monotonic()
            status, kind = 0, ''
            try:
                for attempt in range(2):
                    reused = conn is not None
                    if conn is None:
                        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
                    try:
                        conn.request('GET', path)
                        response = conn.getresponse()
                        break
                    except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                        # Conexión keep-alive que el servidor cerró por inactividad (uvicorn: 5 s). Con
                        # muchos workers cada uno queda inactivo más tiempo; se reconecta una sola vez.
                        conn.close()
                        conn = None
                        if not reused or attempt:
                            raise
                body = response.read()
                status = response.status
                if status != 200:
                    try:
                        kind = json.loads(body).get('error', '')
                    except ValueError:
                        kind = 'non_json'
            except Exception as error:  # noqa: BLE001 - toda falla del cliente se registra
                kind = f'client_{type(error).__name__}'
                if conn is not None:
                    conn.close()
                conn = None
            done = time.monotonic()
            with self._lock:
                self.records.append((self.wall0 + (scheduled - self.t0), query, scenario, status, kind,
                                     round((done - scheduled) * 1000, 3), round((done - sent) * 1000, 3)))

    def start(self):
        self.t0 = time.monotonic() + 0.2
        self.wall0 = time.time() + 0.2
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, args=(index,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=self.timeout_s + 5)

    def dump(self, path):
        with self._lock:
            rows = sorted(self.records)
        with open(path, 'w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['t_sched', 'query', 'scenario', 'status', 'error', 'latency_ms', 'service_ms'])
            writer.writerows(rows)
        return rows


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Carga de lazo abierto contra la API del E3')
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--rate', type=float, default=100)
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--workers', type=int, default=64)
    parser.add_argument('--output', default='loadgen.csv')
    args = parser.parse_args()
    generator = LoadGenerator(args.url, args.rate, args.workers)
    generator.start()
    time.sleep(args.duration)
    generator.stop()
    rows = generator.dump(args.output)
    print(f'{len(rows)} solicitudes registradas en {args.output}')

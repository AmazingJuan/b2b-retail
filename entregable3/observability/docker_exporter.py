#!/usr/bin/env python3
"""Exportador Prometheus mínimo de CPU/memoria/red por contenedor usando la API de Docker.

Reemplaza a cAdvisor, que en este host (Docker 29 con image store de containerd)
solo reporta el cgroup raíz y no puede asociar métricas a contenedores.
"""
import http.client
import http.server
import json
import socket
import threading
import time

PREFIX = 'b2b'
INTERVAL_S = 1.0


class DockerSocket(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost')

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def docker_get(path):
    conn = DockerSocket()
    conn.request('GET', path)
    data = json.loads(conn.getresponse().read())
    conn.close()
    return data


METRICS = {}
LOCK = threading.Lock()


def sample(container):
    name = container['Names'][0].lstrip('/')
    stats = docker_get(f"/containers/{container['Id']}/stats?stream=false&one-shot=false")
    cpu, pre = stats['cpu_stats'], stats['precpu_stats']
    cpu_delta = cpu['cpu_usage']['total_usage'] - pre.get('cpu_usage', {}).get('total_usage', 0)
    sys_delta = cpu.get('system_cpu_usage', 0) - pre.get('system_cpu_usage', 0)
    cores = cpu.get('online_cpus') or len(cpu['cpu_usage'].get('percpu_usage') or [1])
    networks = stats.get('networks', {}).values()
    memory = stats.get('memory_stats', {})
    inactive = memory.get('stats', {}).get('inactive_file', 0)
    return name, {
        'docker_container_cpu_cores': (cpu_delta / sys_delta) * cores if sys_delta > 0 else 0.0,
        'docker_container_cpu_seconds_total': cpu['cpu_usage']['total_usage'] / 1e9,
        'docker_container_memory_working_set_bytes': max(memory.get('usage', 0) - inactive, 0),
        'docker_container_network_receive_bytes_total': sum(n['rx_bytes'] for n in networks),
        'docker_container_network_transmit_bytes_total': sum(n['tx_bytes'] for n in networks),
    }


def collect_loop():
    while True:
        started = time.time()
        try:
            containers = [c for c in docker_get('/containers/json') if c['Names'][0].lstrip('/').startswith(PREFIX)]
            threads, results = [], {}

            def work(item):
                try:
                    name, values = sample(item)
                    results[name] = values
                except Exception:  # noqa: BLE001 - contenedor que desaparece entre llamadas
                    pass
            for item in containers:
                thread = threading.Thread(target=work, args=(item,))
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            with LOCK:
                METRICS.clear()
                METRICS.update(results)
        except Exception as error:  # noqa: BLE001
            print('error recolectando', error, flush=True)
        time.sleep(max(0.0, INTERVAL_S - (time.time() - started)))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        lines = []
        with LOCK:
            names = sorted({metric for values in METRICS.values() for metric in values})
            for metric in names:
                kind = 'counter' if metric.endswith('_total') else 'gauge'
                lines.append(f'# TYPE {metric} {kind}')
                for container, values in sorted(METRICS.items()):
                    lines.append(f'{metric}{{name="{container}"}} {values[metric]}')
        body = ('\n'.join(lines) + '\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    threading.Thread(target=collect_loop, daemon=True).start()
    http.server.ThreadingHTTPServer(('0.0.0.0', 9417), Handler).serve_forever()

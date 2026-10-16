"""Fallas configurables por familia, activables en caliente y con kill switch.

Propiedades que cumple cada falla:
  1. Activación sin redespliegue (API /admin/faults).
  2. Parametrizable (probabilidad, intensidad, alcance y duración).
  3. Kill switch global (/admin/kill-switch) que desactiva todo y bloquea nuevas activaciones.
  4. Reversible: disable() restaura el estado previo (índice, pool, proxy, bloqueos, hilos).
  5. Observable y auditable: métrica e3_fault_active, contador de inyecciones, log JSON y traza.
"""
import json
import logging
import os
import random
import threading
import time
import urllib.request

import psycopg

from . import telemetry as t

log = logging.getLogger('e3.faults')
TOXIPROXY = os.getenv('TOXIPROXY_URL', 'http://toxiproxy:8474')
TOXIPROXY_PROXY = os.getenv('TOXIPROXY_PROXY', 'postgres')
ADMIN_DSN = os.getenv('ADMIN_DSN', 'host=postgres port=5432 dbname=retail user=retail password=retail')


class InjectedError(Exception):
    """Excepción sintética de la familia aplicación."""


class FaultsDisarmed(Exception):
    pass


def toxiproxy(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f'{TOXIPROXY}{path}', data=data, method=method, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


class Fault:
    name = ''
    family = ''
    description = ''
    defaults = {}

    def __init__(self, manager):
        self.manager = manager
        self.active = False
        self.params = dict(self.defaults)
        self.enabled_at = None
        self.expires_at = None
        t.FAULT_ACTIVE.labels(self.name, self.family).set(0)

    def enable(self, params):
        self.params = {**self.defaults, **(params or {})}
        self._enable()
        self.active = True
        self.enabled_at = time.time()
        t.FAULT_ACTIVE.labels(self.name, self.family).set(1)

    def disable(self):
        if self.active:
            self._disable()
        self.active = False
        self.expires_at = None
        t.FAULT_ACTIVE.labels(self.name, self.family).set(0)

    def _enable(self):
        pass

    def _disable(self):
        pass

    def describe(self):
        return {'name': self.name, 'family': self.family, 'description': self.description, 'active': self.active,
                'params': self.params, 'defaults': self.defaults, 'enabled_at': self.enabled_at, 'expires_at': self.expires_at}


# ---------------------------------------------------------------- Familia: red (Toxiproxy entre app y BD)

class NetworkLatency(Fault):
    name, family = 'net_latency', 'red'
    description = 'Latencia y jitter en la red app→PostgreSQL (toxic latency de Toxiproxy, sentido downstream)'
    defaults = {'latency_ms': 30, 'jitter_ms': 10}

    def _enable(self):
        try:
            toxiproxy('DELETE', f'/proxies/{TOXIPROXY_PROXY}/toxics/e3_latency')
        except Exception:  # noqa: BLE001 - no existía
            pass
        toxiproxy('POST', f'/proxies/{TOXIPROXY_PROXY}/toxics', {
            'name': 'e3_latency', 'type': 'latency', 'stream': 'downstream', 'toxicity': 1.0,
            'attributes': {'latency': int(self.params['latency_ms']), 'jitter': int(self.params['jitter_ms'])}})

    def _disable(self):
        toxiproxy('DELETE', f'/proxies/{TOXIPROXY_PROXY}/toxics/e3_latency')


class NetworkPartition(Fault):
    name, family = 'net_partition', 'red'
    description = 'Partición de red app→PostgreSQL: el proxy se deshabilita y cierra/rechaza conexiones'
    defaults = {}

    def _enable(self):
        toxiproxy('POST', f'/proxies/{TOXIPROXY_PROXY}', {'enabled': False})

    def _disable(self):
        toxiproxy('POST', f'/proxies/{TOXIPROXY_PROXY}', {'enabled': True})


# ---------------------------------------------------------------- Familia: aplicación

class AppError(Fault):
    name, family = 'app_error', 'aplicacion'
    description = 'Excepciones intermitentes en el procesamiento de la consulta (HTTP 500)'
    defaults = {'probability': 0.2, 'queries': ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']}

    def maybe_inject(self, query):
        if self.active and query in self.params['queries'] and random.random() < self.params['probability']:
            t.FAULT_INJECTIONS.labels(self.name).inc()
            raise InjectedError(f'falla inyectada en {query}')


class AppLatency(Fault):
    name, family = 'app_latency', 'aplicacion'
    description = 'Latencia de cola: una fracción de solicitudes sufre una pausa larga (dependencia lenta / pausa de GC)'
    defaults = {'probability': 0.1, 'delay_ms': 800, 'queries': ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']}

    def maybe_inject(self, query):
        if self.active and query in self.params['queries'] and random.random() < self.params['probability']:
            t.FAULT_INJECTIONS.labels(self.name).inc()
            time.sleep(self.params['delay_ms'] / 1000)
            return True
        return False


# ---------------------------------------------------------------- Familia: recursos

class PoolShrink(Fault):
    """Fuga de conexiones: un componente defectuoso retiene conexiones y deja solo `max_size` utilizables.

    Se implementa reteniendo conexiones reales (no con pool.resize): en psycopg_pool la reducción es
    perezosa y resize(1)→resize(10) dejó 19 conexiones abiertas con máximo 10, es decir, no era reversible.
    """
    name, family = 'pool_shrink', 'recursos'
    description = 'Agotamiento de recursos: fuga de conexiones; solo max_size conexiones del pool quedan utilizables'
    defaults = {'max_size': 2}

    def _enable(self):
        pool = self.manager.pool
        keep = int(self.params['max_size'])
        self.held = []
        for _ in range(max(0, pool.max_size - keep)):
            self.held.append(pool.getconn(timeout=10))
        t.POOL_MAX.set(keep)

    def _disable(self):
        pool = self.manager.pool
        for conn in self.held:
            pool.putconn(conn)
        self.held = []
        t.POOL_MAX.set(pool.max_size)


class NoisyNeighbor(Fault):
    name, family = 'noisy_neighbor', 'recursos'
    description = 'Vecino ruidoso: N sesiones ejecutan en bucle el reporte analítico pesado sobre ~3M líneas'
    defaults = {'sessions': 4}

    def _enable(self):
        self.stop = threading.Event()
        self.threads = []
        for index in range(int(self.params['sessions'])):
            thread = threading.Thread(target=self._loop, name=f'noisy-{index}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def _loop(self):
        while not self.stop.is_set():
            outcome = self.manager.run_report()
            if outcome != 'ok':
                self.stop.wait(0.2)  # rechazado por el bulkhead o con error: espera antes de insistir
            t.FAULT_INJECTIONS.labels(self.name).inc()

    def _disable(self):
        self.stop.set()
        # Cancela los reportes en curso para que el kill switch no espere a que terminen (~1-3 s cada uno).
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                         "WHERE query LIKE 'SELECT sku_id, sum(cantidad%%' AND pid <> pg_backend_pid()")
        for thread in self.threads:
            thread.join(timeout=30)


# ---------------------------------------------------------------- Familia: datos / base de datos

class IndexDrop(Fault):
    name, family = 'index_drop', 'datos'
    description = 'Regresión de esquema: se elimina un índice crítico (se recrea con su definición original al desactivar)'
    defaults = {'schema': 'ordenes', 'index': 'orden_proveedor_fecha_idx'}

    def _enable(self):
        qualified = f"{self.params['schema']}.{self.params['index']}"
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            row = conn.execute('SELECT pg_get_indexdef(%s::regclass)', (qualified,)).fetchone()
            self.definition = row[0]
            conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {qualified}')

    def _disable(self):
        definition = self.definition.replace('CREATE INDEX', 'CREATE INDEX CONCURRENTLY IF NOT EXISTS', 1)
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(definition)
            conn.execute(f"ANALYZE {self.params['schema']}.{definition.split(' ON ')[1].split(' ')[0].split('.')[-1]}")


class TableLock(Fault):
    name, family = 'table_lock', 'datos'
    description = 'Contención de bloqueos: una transacción retiene un lock exclusivo sobre la tabla de franjas'
    defaults = {'table': 'logistica.franja_descargue', 'mode': 'ACCESS EXCLUSIVE'}

    def _enable(self):
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._hold, name='table-lock', daemon=True)
        self.thread.start()
        self.ready.wait(timeout=10)
        if self.error:
            raise self.error

    def _hold(self):
        try:
            with psycopg.connect(ADMIN_DSN) as conn:
                conn.execute("SET application_name = 'e3-fault-table-lock'")
                conn.execute(f"LOCK TABLE {self.params['table']} IN {self.params['mode']} MODE")
                self.ready.set()
                self.stop.wait()
                conn.rollback()
        except Exception as error:  # noqa: BLE001 - se reporta al activador
            self.error = error
            self.ready.set()

    def _disable(self):
        self.stop.set()
        self.thread.join(timeout=10)


FAULT_TYPES = [NetworkLatency, NetworkPartition, AppError, AppLatency, PoolShrink, NoisyNeighbor, IndexDrop, TableLock]


class FaultManager:
    def __init__(self, pool, run_report):
        self.pool = pool
        self.run_report = run_report
        self._lock = threading.Lock()
        self.faults = {cls.name: cls(self) for cls in FAULT_TYPES}
        self.kill_switch = os.getenv('FAULT_INJECTION_ENABLED', 'true').lower() != 'true'
        t.KILL_SWITCH.set(1 if self.kill_switch else 0)

    def get(self, name):
        return self.faults[name]

    def enable(self, name, params=None, duration_s=None):
        with self._lock:
            if self.kill_switch:
                raise FaultsDisarmed('kill switch accionado: rearme con POST /admin/arm')
            fault = self.faults[name]
            if fault.active:
                fault.disable()
            fault.enable(params)
            if duration_s:
                fault.expires_at = time.time() + float(duration_s)
                threading.Timer(float(duration_s), self._expire, args=(name, fault.enabled_at)).start()
        t.log_event(log, 'fault_enabled', fault=name, family=fault.family, params=fault.params, duration_s=duration_s)
        return fault.describe()

    def _expire(self, name, enabled_at):
        fault = self.faults[name]
        if fault.active and fault.enabled_at == enabled_at:
            self.disable(name, reason='duration_elapsed')

    def disable(self, name, reason='api'):
        with self._lock:
            fault = self.faults[name]
            was_active = fault.active
            fault.disable()
        if was_active:
            t.log_event(log, 'fault_disabled', fault=name, family=fault.family, reason=reason)
        return fault.describe()

    def engage_kill_switch(self):
        started = time.perf_counter()
        errors = {}
        with self._lock:
            self.kill_switch = True
            t.KILL_SWITCH.set(1)
            for name, fault in self.faults.items():
                try:
                    fault.disable()
                except Exception as error:  # noqa: BLE001 - el kill switch sigue con las demás
                    errors[name] = repr(error)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        t.log_event(log, 'kill_switch_engaged', level=logging.WARNING, elapsed_ms=elapsed_ms, errors=errors)
        return {'kill_switch': True, 'elapsed_ms': elapsed_ms, 'errors': errors, 'active': self.active()}

    def arm(self):
        with self._lock:
            self.kill_switch = False
            t.KILL_SWITCH.set(0)
        t.log_event(log, 'faults_armed')
        return {'kill_switch': False}

    def active(self):
        return [name for name, fault in self.faults.items() if fault.active]

    def describe(self):
        return {'kill_switch': self.kill_switch, 'faults': {name: f.describe() for name, f in self.faults.items()}}

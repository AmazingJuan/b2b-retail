"""Mecanismos de resiliencia configurables en caliente (todos apagados en el baseline).

Cada mecanismo es la "corrección" de uno o más experimentos y se activa por la API de
administración sin redesplegar, igual que las fallas. Así el mismo binario se mide en
baseline, con la falla activa y con la corrección.
"""
import copy
import random
import threading
import time

from . import telemetry as t

DEFAULTS = {
    # Reintento con backoff exponencial y jitter completo; solo para lecturas idempotentes.
    'retry': {'enabled': False, 'max_attempts': 3, 'base_ms': 10, 'max_ms': 200},
    # Circuit breaker global hacia la base de datos (cuenta solo errores de conexión/pool).
    'circuit_breaker': {'enabled': False, 'failure_threshold': 5, 'open_seconds': 2.0},
    # Timeouts: espera máxima de pool, statement_timeout y lock_timeout por sesión.
    'timeouts': {'enabled': False, 'pool_timeout_s': 1.0, 'statement_ms': 2000, 'lock_ms': 250},
    # Verificación de la conexión al sacarla del pool (descarta conexiones rotas tras un corte).
    'pool_check': {'enabled': False},
    # Caché TTL para datos maestros (contratos y precios cambian poco).
    'cache': {'enabled': False, 'ttl_s': 30, 'queries': ['Q1', 'Q2']},
    # Compartimentos (bulkheads): concurrencia máxima por tipo de carga; el excedente se rechaza (429).
    'bulkhead': {'enabled': False, 'limits': {'Q1': 8, 'Q2': 8, 'Q3': 3, 'Q4': 4, 'Q5': 8, 'report': 1}},
    # Solicitudes de cobertura (hedging): si el primer intento no responde en after_ms se lanza otro.
    'hedging': {'enabled': False, 'after_ms': 40},
    # Límite de paralelismo para el reporte pesado (protege CPU de la BD).
    'report_limits': {'enabled': False, 'max_parallel_workers_per_gather': 0},
}

POOL_TIMEOUT_DEFAULT_S = 30.0  # valor por defecto de psycopg_pool (configuración "ingenua" del baseline)


class Config:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        self._publish()

    def get(self, name):
        with self._lock:
            return self._data[name]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch):
        with self._lock:
            for name, values in patch.items():
                if name not in self._data:
                    raise KeyError(f'mecanismo desconocido: {name}')
                for key, value in values.items():
                    if key == 'limits':
                        self._data[name]['limits'].update(value)
                    else:
                        self._data[name][key] = value
            self._publish()
            return copy.deepcopy(self._data)

    def reset(self):
        with self._lock:
            self._data = copy.deepcopy(DEFAULTS)
            self._publish()
            return copy.deepcopy(self._data)

    def _publish(self):
        for name, values in self._data.items():
            t.RESILIENCE_ENABLED.labels(name).set(1 if values['enabled'] else 0)


class CircuitOpen(Exception):
    pass


class BulkheadFull(Exception):
    pass


class CircuitBreaker:
    CLOSED, HALF_OPEN, OPEN = 0, 1, 2
    NAMES = {0: 'closed', 1: 'half_open', 2: 'open'}

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self.probe_in_flight = False
        t.CB_STATE.set(0)

    def _transition(self, state):
        if state != self.state:
            self.state = state
            t.CB_STATE.set(state)
            t.CB_TRANSITIONS.labels(self.NAMES[state]).inc()

    def before(self, query):
        cfg = self.config.get('circuit_breaker')
        if not cfg['enabled']:
            return
        with self._lock:
            if self.state == self.OPEN:
                if time.monotonic() - self.opened_at >= cfg['open_seconds'] and not self.probe_in_flight:
                    self._transition(self.HALF_OPEN)
                    self.probe_in_flight = True
                    return
                t.CB_REJECTIONS.labels(query).inc()
                raise CircuitOpen('circuito abierto hacia la base de datos')
            if self.state == self.HALF_OPEN:
                if self.probe_in_flight:
                    t.CB_REJECTIONS.labels(query).inc()
                    raise CircuitOpen('circuito semiabierto: sonda en curso')
                self.probe_in_flight = True

    def success(self):
        with self._lock:
            self.failures = 0
            self.probe_in_flight = False
            self._transition(self.CLOSED)

    def failure(self):
        cfg = self.config.get('circuit_breaker')
        with self._lock:
            self.probe_in_flight = False
            self.failures += 1
            if self.state == self.HALF_OPEN or (cfg['enabled'] and self.failures >= cfg['failure_threshold']):
                self.opened_at = time.monotonic()
                self._transition(self.OPEN)

    def reset(self):
        with self._lock:
            self.failures = 0
            self.probe_in_flight = False
            self._transition(self.CLOSED)


class Bulkheads:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._inflight = {}

    def acquire(self, compartment):
        cfg = self.config.get('bulkhead')
        if not cfg['enabled']:
            return False
        limit = cfg['limits'].get(compartment)
        if limit is None:
            return False
        with self._lock:
            current = self._inflight.get(compartment, 0)
            if current >= limit:
                t.BULKHEAD_REJECTIONS.labels(compartment).inc()
                raise BulkheadFull(f'compartimento {compartment} lleno ({limit})')
            self._inflight[compartment] = current + 1
            t.BULKHEAD_INFLIGHT.labels(compartment).set(current + 1)
        return True

    def release(self, compartment):
        with self._lock:
            current = max(0, self._inflight.get(compartment, 1) - 1)
            self._inflight[compartment] = current
            t.BULKHEAD_INFLIGHT.labels(compartment).set(current)


class TtlCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def get(self, key):
        with self._lock:
            item = self._items.get(key)
            if item and item[0] > time.monotonic():
                return item[1]
            return None

    def put(self, key, value, ttl):
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)

    def clear(self):
        with self._lock:
            self._items.clear()


def backoff_seconds(attempt, base_ms, max_ms):
    """Backoff exponencial con jitter completo (AWS Architecture Blog, 'Exponential Backoff And Jitter')."""
    return random.uniform(0, min(max_ms, base_ms * (2 ** attempt))) / 1000

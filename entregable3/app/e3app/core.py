"""Ruta de ejecución de las consultas: caché → circuit breaker → bulkhead → reintentos → (hedging) → intento.

Un intento = fallas de aplicación → espera de pool → verificación de conexión → timeouts de sesión → SQL.
Cada paso queda instrumentado con métricas, spans y logs para poder localizar dónde se pierde el tiempo.
"""
import concurrent.futures as cf
import logging
import os
import threading
import time

import psycopg
from opentelemetry import context as context_api
from opentelemetry import trace
from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool, PoolTimeout

from . import telemetry as t
from .faults import FaultManager, InjectedError
from .queries import REPORT, render
from .resilience import (POOL_TIMEOUT_DEFAULT_S, BulkheadFull, CircuitBreaker, CircuitOpen, Bulkheads, Config,
                         TtlCache, backoff_seconds)

log = logging.getLogger('e3.core')
tracer = trace.get_tracer('e3app.core')
APP_DSN = os.getenv('APP_DSN', 'host=toxiproxy port=6432 dbname=retail user=retail password=retail')
POOL_SIZE = int(os.getenv('POOL_SIZE', '10'))
CRITICAL_POOL_SIZE = int(os.getenv('CRITICAL_POOL_SIZE', '3'))


def is_connection_error(error):
    """Errores de infraestructura (conexión/pool). Los timeouts de consulta y de lock no abren el circuito."""
    if isinstance(error, PoolTimeout):
        return True
    if isinstance(error, (pg_errors.QueryCanceled, pg_errors.LockNotAvailable)):
        return False
    return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))


class Engine:
    def __init__(self):
        self.config = Config()
        self.pool = ConnectionPool(APP_DSN, min_size=POOL_SIZE, max_size=POOL_SIZE, name='app', open=False,
                                   timeout=POOL_TIMEOUT_DEFAULT_S,
                                   kwargs={'autocommit': True, 'prepare_threshold': None, 'connect_timeout': 3})
        # Pool crítico: solo se usa si el mecanismo pool_bulkhead está habilitado.
        self.critical_pool = ConnectionPool(APP_DSN, min_size=CRITICAL_POOL_SIZE, max_size=CRITICAL_POOL_SIZE, name='critical',
                                            open=False, timeout=POOL_TIMEOUT_DEFAULT_S,
                                            kwargs={'autocommit': True, 'prepare_threshold': None, 'connect_timeout': 3})
        self.breaker = CircuitBreaker(self.config)
        self.bulkheads = Bulkheads(self.config)
        self.cache = TtlCache()
        self.hedge_pool = cf.ThreadPoolExecutor(max_workers=64, thread_name_prefix='hedge')
        self.faults = FaultManager(self.pool, self.run_report)
        self._stop = threading.Event()
        t.POOL_MAX.set(POOL_SIZE)

    def start(self):
        self.pool.open(wait=False)
        self.critical_pool.open(wait=False)
        threading.Thread(target=self._pool_stats_loop, daemon=True, name='pool-stats').start()

    def stop(self):
        self._stop.set()
        self.faults.engage_kill_switch()
        self.pool.close()
        self.critical_pool.close()

    def _pool_stats_loop(self):
        while not self._stop.wait(0.5):
            stats = self.pool.get_stats()
            t.POOL_SIZE.set(stats.get('pool_size', 0))
            t.POOL_AVAILABLE.set(stats.get('pool_available', 0))
            t.POOL_WAITING.set(stats.get('requests_waiting', 0))
            critical = self.critical_pool.get_stats()
            t.CRITICAL_POOL_AVAILABLE.set(critical.get('pool_available', 0))
            t.CRITICAL_POOL_WAITING.set(critical.get('requests_waiting', 0))

    # ------------------------------------------------------------------ conexiones

    def _pool_for(self, query):
        bulkhead = self.config.get('pool_bulkhead')
        return self.critical_pool if bulkhead['enabled'] and query in bulkhead['queries'] else self.pool

    def _getconn(self, query, pool):
        timeouts = self.config.get('timeouts')
        wait = timeouts['pool_timeout_s'] if timeouts['enabled'] else POOL_TIMEOUT_DEFAULT_S
        started = time.perf_counter()
        with tracer.start_as_current_span('pool.getconn') as span:
            try:
                conn = pool.getconn(timeout=wait)
            finally:
                waited = time.perf_counter() - started
                t.POOL_WAIT.labels(query).observe(waited)
                span.set_attribute('pool.wait_ms', round(waited * 1000, 3))
            if self.config.get('pool_check')['enabled']:
                try:
                    ConnectionPool.check_connection(conn)
                except Exception:
                    span.add_event('pool.check_failed')
                    pool.putconn(conn)
                    conn = pool.getconn(timeout=wait)
            self._apply_session(conn, timeouts)
            return conn

    def _apply_session(self, conn, timeouts):
        desired = (timeouts['statement_ms'], timeouts['lock_ms']) if timeouts['enabled'] else (0, 0)
        if getattr(conn, '_e3_settings', None) != desired:
            conn.execute(f'SET statement_timeout = {int(desired[0])}; SET lock_timeout = {int(desired[1])}')
            conn._e3_settings = desired

    # ------------------------------------------------------------------ un intento

    def _attempt(self, query, sql):
        with tracer.start_as_current_span('consulta.intento') as span:
            self.faults.get('app_error').maybe_inject(query)
            if self.faults.get('app_latency').maybe_inject(query):
                span.add_event('fault.app_latency', {'delay_ms': self.faults.get('app_latency').params['delay_ms']})
            pool = self._pool_for(query)
            span.set_attribute('pool.name', pool.name)
            conn = self._getconn(query, pool)
            try:
                started = time.perf_counter()
                with tracer.start_as_current_span('db.execute') as db_span:
                    db_span.set_attribute('db.statement', sql)
                    rows = conn.execute(sql).fetchall()
                t.DB_LATENCY.labels(query).observe(time.perf_counter() - started)
                return rows
            except Exception:
                conn._e3_settings = None
                raise
            finally:
                pool.putconn(conn)

    def _hedged(self, query, sql):
        cfg = self.config.get('hedging')
        context = trace.set_span_in_context(trace.get_current_span())

        def run():
            token = context_api.attach(context)
            try:
                return self._attempt(query, sql)
            finally:
                context_api.detach(token)

        primary = self.hedge_pool.submit(run)
        done, _ = cf.wait([primary], timeout=cfg['after_ms'] / 1000)
        if done:
            t.HEDGES.labels(query, 'primary_fast').inc()
            return primary.result()
        backup = self.hedge_pool.submit(run)
        trace.get_current_span().add_event('hedge.launched')
        pending = {primary, backup}
        last_error = None
        while pending:
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for future in done:
                if future.exception() is None:
                    t.HEDGES.labels(query, 'primary' if future is primary else 'backup').inc()
                    return future.result()
                last_error = future.exception()
        raise last_error

    # ------------------------------------------------------------------ consulta completa

    def execute(self, query, provider, cedi):
        sql = render(query, provider, cedi)
        span = trace.get_current_span()
        span.set_attribute('e3.query', query)
        cache_cfg = self.config.get('cache')
        cache_key = (query, int(provider), int(cedi))
        if cache_cfg['enabled'] and query in cache_cfg['queries']:
            cached = self.cache.get(cache_key)
            if cached is not None:
                t.CACHE.labels(query, 'hit').inc()
                span.set_attribute('e3.cache', 'hit')
                return cached
            t.CACHE.labels(query, 'miss').inc()

        self.breaker.before(query)
        compartment = self.bulkheads.acquire(query) and query
        try:
            retry = self.config.get('retry')
            attempts = retry['max_attempts'] if retry['enabled'] else 1
            for attempt in range(attempts):
                try:
                    if self.config.get('hedging')['enabled']:
                        rows = self._hedged(query, sql)
                    else:
                        rows = self._attempt(query, sql)
                    self.breaker.success()
                    if attempt:
                        t.RETRIES.labels(query, 'recovered').inc()
                    break
                except (InjectedError, psycopg.Error, PoolTimeout) as error:
                    connection_error = is_connection_error(error)
                    if connection_error:
                        self.breaker.failure()
                    else:
                        self.breaker.success()  # la BD respondió: el error no es de conectividad
                    retriable = isinstance(error, InjectedError) or connection_error
                    if not retriable or attempt == attempts - 1:
                        if attempt:
                            t.RETRIES.labels(query, 'exhausted').inc()
                        raise
                    pause = backoff_seconds(attempt, retry['base_ms'], retry['max_ms'])
                    span.add_event('retry', {'attempt': attempt + 1, 'error': type(error).__name__, 'backoff_ms': round(pause * 1000, 1)})
                    t.RETRIES.labels(query, 'attempt').inc()
                    time.sleep(pause)
                    if connection_error:
                        self.breaker.before(query)
        finally:
            if compartment:
                self.bulkheads.release(compartment)
        if cache_cfg['enabled'] and query in cache_cfg['queries']:
            self.cache.put(cache_key, rows, cache_cfg['ttl_s'])
        return rows

    # ------------------------------------------------------------------ reporte pesado (vecino ruidoso)

    def run_report(self):
        try:
            compartment = self.bulkheads.acquire('report') and 'report'
        except BulkheadFull:
            t.REPORTS.labels('rejected').inc()
            return 'rejected'
        try:
            limits = self.config.get('report_limits')
            with self.pool.connection(timeout=POOL_TIMEOUT_DEFAULT_S) as conn:
                with conn.transaction():
                    conn.execute('SET LOCAL statement_timeout = 0')
                    if limits['enabled']:
                        conn.execute(f"SET LOCAL max_parallel_workers_per_gather = {int(limits['max_parallel_workers_per_gather'])}")
                    conn.execute(REPORT).fetchall()
            t.REPORTS.labels('ok').inc()
            return 'ok'
        except Exception as error:  # noqa: BLE001 - el reporte no debe tumbar el hilo
            t.REPORTS.labels('error').inc()
            t.log_event(log, 'report_failed', level=logging.WARNING, error=repr(error))
            return 'error'
        finally:
            if compartment:
                self.bulkheads.release(compartment)

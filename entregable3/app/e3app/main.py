"""API del E3: consultas críticas del E2 + plano de control de fallas y resiliencia + observabilidad."""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

import psycopg
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg_pool import PoolTimeout

from . import telemetry as t
from .core import Engine
from .faults import FaultsDisarmed, InjectedError
from .queries import QUERIES, SCENARIOS
from .resilience import BulkheadFull, CircuitOpen

log = t.setup_logging()
t.setup_tracing()
PsycopgInstrumentor().instrument(enable_commenter=False)
engine = Engine()


@asynccontextmanager
async def lifespan(_app):
    engine.start()
    t.log_event(log, 'service_started', pool_size=engine.pool.max_size)
    yield
    engine.stop()


app = FastAPI(title='B2B Retail · E3 resiliencia', lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls='metrics,health')


def classify(error):
    """Traduce cada tipo de error a un código HTTP distinto para que las señales sean distinguibles."""
    if isinstance(error, InjectedError):
        return 500, 'injected_error'
    if isinstance(error, CircuitOpen):
        return 503, 'circuit_open'
    if isinstance(error, BulkheadFull):
        return 429, 'bulkhead_full'
    if isinstance(error, PoolTimeout):
        return 503, 'pool_timeout'
    if isinstance(error, psycopg.errors.LockNotAvailable):
        return 504, 'lock_timeout'
    if isinstance(error, psycopg.errors.QueryCanceled):
        return 504, 'statement_timeout'
    if isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError)):
        return 503, 'db_unavailable'
    return 500, 'internal_error'


@app.get('/consultas/{query_id}')
def consulta(query_id: str, escenario: str = Query('hot', pattern='^(hot|cold)$'),
             proveedor: int | None = None, cedi: int | None = None):
    if query_id not in QUERIES:
        raise HTTPException(404, f'consulta desconocida: {query_id}')
    params = SCENARIOS[escenario]
    provider = proveedor if proveedor is not None else params['provider']
    cedi_id = cedi if cedi is not None else params['cedi']
    span = trace.get_current_span()
    span.set_attribute('e3.scenario', escenario)
    started = time.perf_counter()
    try:
        rows = engine.execute(query_id, provider, cedi_id)
        status, body = 200, {'query': query_id, 'escenario': escenario, 'filas': len(rows)}
    except Exception as error:  # noqa: BLE001 - toda falla se clasifica y se registra
        status, kind = classify(error)
        body = {'query': query_id, 'error': kind, 'detalle': str(error)[:200]}
        span.set_attribute('e3.error', kind)
        t.log_event(log, 'request_failed', level=logging.WARNING, query=query_id, scenario=escenario,
                    status=status, error=kind, detail=str(error)[:200])
    elapsed = time.perf_counter() - started
    t.REQUEST_LATENCY.labels(query_id).observe(elapsed)
    t.REQUESTS.labels(query_id, str(status)).inc()
    if elapsed > 0.5:
        t.log_event(log, 'slow_request', level=logging.WARNING, query=query_id, scenario=escenario,
                    status=status, elapsed_ms=round(elapsed * 1000, 1))
    return JSONResponse(body, status_code=status)


@app.get('/reportes/ventas-sku')
def reporte():
    outcome = engine.run_report()
    return JSONResponse({'resultado': outcome}, status_code={'ok': 200, 'rejected': 429}.get(outcome, 500))


@app.get('/health')
async def health():
    return {'status': 'ok', 'pool': engine.pool.get_stats(), 'active_faults': engine.faults.active()}


@app.get('/metrics')
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ------------------------------------------------------------------ plano de control (async: responde aun con el threadpool saturado)

@app.get('/admin/state')
async def state():
    return {'faults': engine.faults.describe(), 'resilience': engine.config.snapshot(),
            'circuit_breaker': engine.breaker.NAMES[engine.breaker.state], 'pool': engine.pool.get_stats(),
            'pool_max_size': engine.pool.max_size}


@app.get('/admin/faults')
async def list_faults():
    return engine.faults.describe()


@app.post('/admin/faults/{name}')
async def enable_fault(name: str, payload: dict = Body(default={})):
    if name not in engine.faults.faults:
        raise HTTPException(404, f'falla desconocida: {name}')
    try:
        return await asyncio.to_thread(engine.faults.enable, name, payload.get('params'), payload.get('duration_s'))
    except FaultsDisarmed as error:
        raise HTTPException(409, str(error)) from error


@app.delete('/admin/faults/{name}')
async def disable_fault(name: str):
    if name not in engine.faults.faults:
        raise HTTPException(404, f'falla desconocida: {name}')
    return await asyncio.to_thread(engine.faults.disable, name)


@app.post('/admin/kill-switch')
async def kill_switch():
    return await asyncio.to_thread(engine.faults.engage_kill_switch)


@app.post('/admin/arm')
async def arm():
    return engine.faults.arm()


@app.get('/admin/resilience')
async def get_resilience():
    return engine.config.snapshot()


@app.put('/admin/resilience')
async def put_resilience(patch: dict = Body(...)):
    try:
        snapshot = engine.config.update(patch)
    except KeyError as error:
        raise HTTPException(400, str(error)) from error
    engine.breaker.reset()
    engine.cache.clear()
    t.log_event(log, 'resilience_updated', patch=patch)
    return snapshot


@app.post('/admin/resilience/reset')
async def reset_resilience():
    snapshot = engine.config.reset()
    engine.breaker.reset()
    engine.cache.clear()
    t.log_event(log, 'resilience_reset')
    return snapshot


@app.middleware('http')
async def admin_audit(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/admin') and request.method != 'GET':
        t.log_event(log, 'admin_call', method=request.method, path=request.url.path, status=response.status_code)
    return response

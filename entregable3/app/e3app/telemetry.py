"""Métricas Prometheus, logs JSON correlacionados con trazas y trazas OpenTelemetry."""
import json
import logging
import os
import sys
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram

SERVICE = os.getenv('OTEL_SERVICE_NAME', 'b2b-e3-api')

# Buckets finos alrededor del baseline (1-20 ms) y largos para las colas de las fallas.
LATENCY_BUCKETS = (.001, .0025, .005, .0075, .01, .015, .02, .03, .05, .075, .1, .15, .2, .3, .5, .75, 1, 1.5, 2, 3, 5, 10)

REQUESTS = Counter('e3_http_requests_total', 'Solicitudes HTTP atendidas', ['query', 'status'])
REQUEST_LATENCY = Histogram('e3_request_duration_seconds', 'Latencia de extremo a extremo en el servicio', ['query'], buckets=LATENCY_BUCKETS)
DB_LATENCY = Histogram('e3_db_query_duration_seconds', 'Tiempo de ejecución SQL (sin espera de pool)', ['query'], buckets=LATENCY_BUCKETS)
POOL_WAIT = Histogram('e3_pool_wait_seconds', 'Tiempo esperando una conexión del pool', ['query'], buckets=LATENCY_BUCKETS)
POOL_SIZE = Gauge('e3_pool_size', 'Conexiones abiertas en el pool')
POOL_AVAILABLE = Gauge('e3_pool_available', 'Conexiones libres en el pool')
POOL_WAITING = Gauge('e3_pool_requests_waiting', 'Solicitudes en cola esperando conexión')
POOL_MAX = Gauge('e3_pool_max_size', 'Tamaño máximo configurado del pool')
CRITICAL_POOL_AVAILABLE = Gauge('e3_critical_pool_available', 'Conexiones libres en el pool crítico (bulkhead de conexiones)')
CRITICAL_POOL_WAITING = Gauge('e3_critical_pool_requests_waiting', 'Solicitudes esperando en el pool crítico')
FAULT_ACTIVE = Gauge('e3_fault_active', 'Falla activa (1) o inactiva (0)', ['fault', 'family'])
FAULT_INJECTIONS = Counter('e3_fault_injections_total', 'Veces que una falla afectó una solicitud', ['fault'])
KILL_SWITCH = Gauge('e3_kill_switch_engaged', 'Kill switch accionado (1): ninguna falla puede activarse')
RESILIENCE_ENABLED = Gauge('e3_resilience_enabled', 'Mecanismo de resiliencia habilitado', ['mechanism'])
RETRIES = Counter('e3_retries_total', 'Reintentos ejecutados', ['query', 'outcome'])
CB_STATE = Gauge('e3_circuit_breaker_state', 'Estado del circuit breaker (0 cerrado, 1 semiabierto, 2 abierto)')
CB_TRANSITIONS = Counter('e3_circuit_breaker_transitions_total', 'Transiciones del circuit breaker', ['to'])
CB_REJECTIONS = Counter('e3_circuit_breaker_rejections_total', 'Solicitudes rechazadas con el circuito abierto', ['query'])
BULKHEAD_INFLIGHT = Gauge('e3_bulkhead_inflight', 'Solicitudes dentro del compartimento', ['compartment'])
BULKHEAD_REJECTIONS = Counter('e3_bulkhead_rejections_total', 'Rechazos por compartimento lleno', ['compartment'])
CACHE = Counter('e3_cache_total', 'Resultados de caché', ['query', 'result'])
HEDGES = Counter('e3_hedged_requests_total', 'Solicitudes de cobertura (hedging) lanzadas', ['query', 'winner'])
TIMEOUTS = Counter('e3_db_timeouts_total', 'Cancelaciones por statement/lock timeout', ['query', 'kind'])
REPORTS = Counter('e3_reports_total', 'Ejecuciones del reporte pesado', ['outcome'])


class JsonFormatter(logging.Formatter):
    def format(self, record):
        span = trace.get_current_span().get_span_context()
        payload = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created)) + f'.{int(record.msecs):03d}Z',
            'level': record.levelname, 'logger': record.name, 'msg': record.getMessage(),
        }
        if span.is_valid:
            payload['trace_id'] = format(span.trace_id, '032x')
            payload['span_id'] = format(span.span_id, '016x')
        payload.update(getattr(record, 'fields', {}))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv('LOG_LEVEL', 'INFO'))
    return logging.getLogger('e3')


def log_event(logger, msg, level=logging.INFO, **fields):
    logger.log(level, msg, extra={'fields': fields})


def setup_tracing():
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
    provider = TracerProvider(resource=Resource.create({'service.name': SERVICE}))
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f'{endpoint}/v1/traces')))
    trace.set_tracer_provider(provider)
    return trace.get_tracer('e3app')

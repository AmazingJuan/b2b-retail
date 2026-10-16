#!/bin/sh
# Guion de la demo en vivo de la sustentación (E3).
# Uso, desde la raíz del repo:   sh entregable3/demo_vivo.sh <paso>
#
# Terminal 1 (carga, déjala corriendo toda la demo):
#   python3 entregable3/experiments/loadgen.py --rate 100 --duration 1200 --output /tmp/demo_carga.csv
# Navegador: Grafana http://localhost:3000 (dashboard "E3 · Resiliencia y observabilidad")
#            Jaeger  http://localhost:16686 (servicio b2b-e3-api)
#
# Pasos, en orden:
#   estado     estado actual: fallas activas, mecanismos de resiliencia, pool
#   inicial    deja el sistema en su estado inicial (sin fallas, sin correcciones)
#   combinar   activa la COMBINACIÓN: latencia de red 30 ms + fuga de conexiones (2 de 10)
#   logs       muestra los últimos errores/lentitudes de la app (con trace_id para Jaeger)
#   corregir   activa las correcciones (pool aislado Q1/Q2 + bulkhead + caché + timeouts), con la falla aún activa
#
# Alternativa principal para la demo: editar entregable3/control/fallas.yml (la app lo aplica en ~1 s).
#   cascada    alternativa: bloqueo ACCESS EXCLUSIVE sobre franjas (E8, falla en cascada)
#   apagar     KILL SWITCH: apaga todas las fallas de una vez
#   rearmar    vuelve a permitir activar fallas tras el kill switch
set -eu
API=${API:-http://localhost:8000}
J='content-type: application/json'

case "${1:-}" in
  estado)
    curl -s "$API/admin/state" | python3 -m json.tool ;;
  inicial)
    curl -s -XPOST "$API/admin/kill-switch" >/dev/null || true
    curl -s -XPOST "$API/admin/arm" >/dev/null
    curl -s -XPOST "$API/admin/resilience/reset" >/dev/null
    echo "Estado inicial: sin fallas y sin correcciones (configuración del baseline)." ;;
  combinar)
    # Duración de protección: si algo se cuelga, las fallas se apagan solas a los 180 s.
    curl -s -XPOST "$API/admin/faults/net_latency" -H "$J" -d '{"params":{"latency_ms":30,"jitter_ms":10},"duration_s":180}'; echo
    curl -s -XPOST "$API/admin/faults/pool_shrink" -H "$J" -d '{"params":{"max_size":2},"duration_s":180}'; echo
    echo "Combinación activa: net_latency (red) + pool_shrink (recursos)." ;;
  logs)
    docker logs --since 20s b2b-e3-app 2>&1 | grep -E 'request_failed|slow_request|fault_enabled|fault_disabled' | tail -n 8 ;;
  corregir)
    curl -s -XPUT "$API/admin/resilience" -H "$J" \
      -d '{"pool_bulkhead":{"enabled":true},"bulkhead":{"enabled":true},"cache":{"enabled":true,"ttl_s":30},"timeouts":{"enabled":true,"pool_timeout_s":1.0}}'; echo
    echo "Correcciones activas: pool aislado Q1/Q2, límite de concurrencia, caché y timeout de pool 1 s." ;;
  cascada)
    curl -s -XPOST "$API/admin/faults/table_lock" -H "$J" -d '{"duration_s":30}'; echo ;;
  apagar)
    curl -s -XPOST "$API/admin/kill-switch"; echo ;;
  rearmar)
    curl -s -XPOST "$API/admin/arm"; echo ;;
  *)
    sed -n '2,20p' "$0"; exit 1 ;;
esac

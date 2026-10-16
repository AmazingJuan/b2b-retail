"""Consultas críticas heredadas del E2 (scripts/measure.py), con el mismo texto SQL.

Se formatean con enteros validados (igual que el E2, que usaba literales), de modo que el
planificador vea exactamente la misma consulta que se midió en el Entregable 2.
"""

QUERIES = {
    'Q1': "SELECT id FROM proveedores.contrato WHERE proveedor_id = {provider} AND inicia_en <= DATE '2026-09-20' AND vence_en >= DATE '2026-09-20' LIMIT 1",
    'Q2': "SELECT id, precio FROM catalogo.sku WHERE proveedor_id = {provider} AND vigente_desde <= DATE '2026-09-20' AND vigente_hasta >= DATE '2026-09-20' LIMIT 20",
    'Q3': "SELECT id FROM logistica.franja_descargue WHERE cedi_id = {cedi} AND inicia_en::date = DATE '2026-09-20' AND reservadas < capacidad ORDER BY inicia_en",
    'Q4': "SELECT proveedor_id, date_trunc('month', creada_en) AS mes, round(100.0 * avg((entregada_en <= prometida_en)::int), 2) AS otif_pct, round(100.0 * sum(cantidad_recibida) / NULLIF(sum(cantidad_solicitada), 0), 2) AS fill_rate_pct FROM ordenes.orden_compra WHERE proveedor_id = {provider} GROUP BY proveedor_id, date_trunc('month', creada_en)",
    'Q5': "SELECT id FROM ordenes.orden_compra WHERE proveedor_id = {provider} ORDER BY creada_en DESC, id DESC LIMIT 50",
}

# Escenarios del E2: frío = proveedor de la cola larga y último CEDI; caliente = proveedor "hot" y CEDI 1.
SCENARIOS = {'cold': {'provider': 100_000, 'cedi': 5}, 'hot': {'provider': 1, 'cedi': 1}}

# Reporte analítico pesado (ventas por SKU sobre ~3M líneas). Es la carga "vecino ruidoso" del experimento E6.
REPORT = (
    'SELECT sku_id, sum(cantidad * precio_unitario) AS ventas '
    'FROM ordenes.linea_orden GROUP BY sku_id ORDER BY ventas DESC LIMIT 20'
)


def render(query_id, provider, cedi):
    return QUERIES[query_id].format(provider=int(provider), cedi=int(cedi))

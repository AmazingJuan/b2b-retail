SELECT 'proveedores' AS tabla, count(*) AS filas, CASE WHEN count(*) >= 100000 THEN 'PASA' ELSE 'FALLA' END AS veredicto FROM proveedores.proveedor
UNION ALL SELECT 'contratos', count(*), CASE WHEN count(*) >= 15000 THEN 'PASA' ELSE 'FALLA' END FROM proveedores.contrato
UNION ALL SELECT 'skus', count(*), CASE WHEN count(*) >= 400000 THEN 'PASA' ELSE 'FALLA' END FROM catalogo.sku
UNION ALL SELECT 'ordenes', count(*), CASE WHEN count(*) >= 500000 THEN 'PASA' ELSE 'FALLA' END FROM ordenes.orden_compra
UNION ALL SELECT 'lineas', count(*), CASE WHEN count(*) >= 3000000 THEN 'PASA' ELSE 'FALLA' END FROM ordenes.linea_orden
UNION ALL SELECT 'franjas', count(*), CASE WHEN count(*) >= 150000 THEN 'PASA' ELSE 'FALLA' END FROM logistica.franja_descargue
UNION ALL SELECT 'eventos', count(*), CASE WHEN count(*) >= 800000 THEN 'PASA' ELSE 'FALLA' END FROM auditoria.evento;

SELECT 'top_1_pct_proveedores' AS metrica,
       round(100.0 * sum(total)::numeric / (SELECT count(*) FROM ordenes.orden_compra), 2) AS valor
FROM (SELECT proveedor_id, count(*) AS total FROM ordenes.orden_compra GROUP BY proveedor_id ORDER BY total DESC LIMIT 1000) hot;
SELECT 'top_5_pct_proveedores' AS metrica,
       round(100.0 * sum(total)::numeric / (SELECT count(*) FROM ordenes.orden_compra), 2) AS valor
FROM (SELECT proveedor_id, count(*) AS total FROM ordenes.orden_compra GROUP BY proveedor_id ORDER BY total DESC LIMIT 5000) hot;

SELECT 'casos_borde' AS metrica,
  (SELECT count(*) FROM ordenes.linea_orden GROUP BY orden_id HAVING count(*) >= 300 LIMIT 1) IS NOT NULL AS orden_300_lineas,
  EXISTS (SELECT 1 FROM proveedores.contrato WHERE vence_en = DATE '2026-09-20' - 1) AS contrato_vencido,
  EXISTS (SELECT 1 FROM proveedores.contrato WHERE vence_en = DATE '2026-09-20') AS contrato_vence_hoy,
  EXISTS (SELECT 1 FROM logistica.franja_descargue WHERE id = (SELECT max(id) FROM logistica.franja_descargue) AND reservadas < capacidad) AS ultima_franja_disponible,
  EXISTS (SELECT 1 FROM proveedores.proveedor p WHERE NOT EXISTS (SELECT 1 FROM proveedores.contrato c WHERE c.proveedor_id = p.id AND c.vence_en >= DATE '2026-09-20')) AS proveedor_sin_contrato,
  EXISTS (SELECT 1 FROM catalogo.sku s WHERE s.proveedor_id <> (SELECT proveedor_id FROM proveedores.contrato c WHERE c.id = s.contrato_id)) AS sku_fuera_catalogo,
  (SELECT max(total) >= 5000 FROM (SELECT count(*) total FROM ordenes.orden_compra GROUP BY proveedor_id) x) AS proveedor_hot,
  EXISTS (SELECT 1 FROM proveedores.proveedor p WHERE p.activo AND NOT EXISTS (SELECT 1 FROM ordenes.orden_compra o WHERE o.proveedor_id = p.id)) AS proveedor_activo_sin_ordenes,
  EXISTS (SELECT 1 FROM proveedores.proveedor p WHERE p.activo AND NOT EXISTS (SELECT 1 FROM ordenes.orden_compra o WHERE o.proveedor_id = p.id AND date_trunc('month', o.creada_en) = DATE '2026-09-01')) AS proveedor_mes_sin_ordenes;

SELECT 'determinismo_checksum' AS metrica, md5(string_agg(id::text || ':' || proveedor_id::text || ':' || creada_en::text, ',' ORDER BY id)) AS valor
FROM (SELECT id, proveedor_id, creada_en FROM ordenes.orden_compra ORDER BY id LIMIT 10000) muestra;

SELECT 'otif_fill_rate' AS metrica,
  round(100.0 * avg((entregada_en <= prometida_en)::int), 2) AS otif_pct,
  round(100.0 * sum(cantidad_recibida) / NULLIF(sum(cantidad_solicitada), 0), 2) AS fill_rate_pct
FROM ordenes.orden_compra;

SELECT 'franjas_fecha_referencia' AS metrica, count(*) AS valor
FROM logistica.franja_descargue
WHERE inicia_en::date = DATE '2026-09-20';

DO $$
BEGIN
  IF (SELECT count(*) FROM proveedores.proveedor) < 100000
     OR (SELECT count(*) FROM proveedores.contrato) < 15000
     OR (SELECT count(*) FROM catalogo.sku) < 400000
     OR (SELECT count(*) FROM ordenes.orden_compra) < 500000
     OR (SELECT count(*) FROM ordenes.linea_orden) < 3000000
     OR (SELECT count(*) FROM logistica.franja_descargue) < 150000
     OR (SELECT count(*) FROM auditoria.evento) < 800000 THEN
    RAISE EXCEPTION 'No se alcanzo el volumen objetivo exigido';
  END IF;
END $$;

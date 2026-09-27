-- ============================================================================
-- VERIFICACIÓN AUTOMÁTICA DEL CONJUNTO DE DATOS
-- ============================================================================

-- 1. Conteo de entidades contra el volumen objetivo mínimo exigido
SELECT 'proveedores' AS tabla, count(*) AS filas, 
       CASE WHEN count(*) > 100000 THEN 'PASA' ELSE 'FALLA' END AS veredicto 
FROM proveedores.proveedor
UNION ALL 
SELECT 'contratos', count(*), 
       CASE WHEN count(*) > 15000 THEN 'PASA' ELSE 'FALLA' END 
FROM proveedores.contrato
UNION ALL 
SELECT 'skus', count(*), 
       CASE WHEN count(*) > 400000 THEN 'PASA' ELSE 'FALLA' END 
FROM catalogo.sku
UNION ALL 
SELECT 'ordenes', count(*), 
       CASE WHEN count(*) > 500000 THEN 'PASA' ELSE 'FALLA' END 
FROM ordenes.orden_compra
UNION ALL 
SELECT 'lineas', count(*), 
       CASE WHEN count(*) > 3000000 THEN 'PASA' ELSE 'FALLA' END 
FROM ordenes.linea_orden
UNION ALL 
SELECT 'franjas', count(*), 
       CASE WHEN count(*) > 150000 THEN 'PASA' ELSE 'FALLA' END 
FROM logistica.franja_descargue
UNION ALL 
SELECT 'eventos', count(*), 
       CASE WHEN count(*) > 800000 THEN 'PASA' ELSE 'FALLA' END 
FROM auditoria.evento;

-- 2. Concentración Zipf de proveedores (Top 1%, Top 5%, Top 20%)
SELECT 'top_1_pct_proveedores' AS metrica,
       round(100.0 * sum(total)::numeric / (SELECT count(*) FROM ordenes.orden_compra), 2) AS valor
FROM (SELECT proveedor_id, count(*) AS total FROM ordenes.orden_compra GROUP BY proveedor_id ORDER BY total DESC LIMIT 1050) hot;

SELECT 'top_5_pct_proveedores' AS metrica,
       round(100.0 * sum(total)::numeric / (SELECT count(*) FROM ordenes.orden_compra), 2) AS valor
FROM (SELECT proveedor_id, count(*) AS total FROM ordenes.orden_compra GROUP BY proveedor_id ORDER BY total DESC LIMIT 5250) hot;

SELECT 'top_20_pct_proveedores' AS metrica,
       round(100.0 * sum(total)::numeric / (SELECT count(*) FROM ordenes.orden_compra), 2) AS valor
FROM (SELECT proveedor_id, count(*) AS total FROM ordenes.orden_compra GROUP BY proveedor_id ORDER BY total DESC LIMIT 21000) hot;

-- 3. Concentración de SKUs del catálogo (Top 10% concentra ~60% de líneas de orden)
SELECT 'top_10_pct_skus_en_lineas' AS metrica,
       round(100.0 * count(*)::numeric / (SELECT count(*) FROM ordenes.linea_orden), 2) AS valor,
       CASE WHEN (100.0 * count(*)::numeric / (SELECT count(*) FROM ordenes.linea_orden)) >= 58.0 THEN 'PASA' ELSE 'FALLA' END AS veredicto
FROM ordenes.linea_orden
WHERE sku_id <= 42000;

-- 4. Estacionalidad verificable: Mensual (últimos 3 días hábiles >= 22%)
WITH ordenes_dias AS (
  SELECT 
    creada_en,
    EXTRACT(ISODOW FROM creada_en) AS dia_semana, -- 1=Lunes .. 5=Viernes
    creada_en::date AS fecha,
    (date_trunc('month', creada_en) + interval '1 month - 1 day')::date AS fin_mes
  FROM ordenes.orden_compra
),
dias_habiles_mes AS (
  SELECT DISTINCT fecha, fin_mes
  FROM ordenes_dias
  WHERE dia_semana BETWEEN 1 AND 5
),
clasificacion AS (
  SELECT fecha,
         row_number() OVER (PARTITION BY date_trunc('month', fecha) ORDER BY fecha DESC) AS ranking_habil_desc
  FROM dias_habiles_mes
)
SELECT 'estacionalidad_fin_mes_ultimos_3_dias_habiles' AS metrica,
       round(100.0 * count(CASE WHEN c.ranking_habil_desc <= 3 THEN 1 END)::numeric / count(*), 2) AS pct_ordenes,
       CASE WHEN (100.0 * count(CASE WHEN c.ranking_habil_desc <= 3 THEN 1 END)::numeric / count(*)) >= 22.0 THEN 'PASA' ELSE 'FALLA' END AS veredicto
FROM ordenes_dias od
LEFT JOIN clasificacion c ON od.fecha = c.fecha;

-- 5. Estacionalidad verificable: Horaria (Pico 8-11 am vs Valle 00-05 am)
SELECT 'estacionalidad_horaria_pico_8_a_11' AS metrica,
       round(100.0 * count(CASE WHEN EXTRACT(HOUR FROM creada_en) BETWEEN 8 AND 11 THEN 1 END)::numeric / count(*), 2) AS pct_pico_matutino,
       round(100.0 * count(CASE WHEN EXTRACT(HOUR FROM creada_en) BETWEEN 0 AND 5 THEN 1 END)::numeric / count(*), 2) AS pct_valle_nocturno,
       CASE WHEN count(CASE WHEN EXTRACT(HOUR FROM creada_en) BETWEEN 8 AND 11 THEN 1 END) > 5 * count(CASE WHEN EXTRACT(HOUR FROM creada_en) BETWEEN 0 AND 5 THEN 1 END) THEN 'PASA' ELSE 'FALLA' END AS veredicto
FROM ordenes.orden_compra;

-- 6. Órdenes por día de los últimos 60 días (para evidenciar el pico de fin de mes)
SELECT creada_en::date AS dia, count(*) AS total_ordenes
FROM ordenes.orden_compra
WHERE creada_en::date >= (SELECT max(creada_en::date) - 60 FROM ordenes.orden_compra)
GROUP BY creada_en::date
ORDER BY dia DESC
LIMIT 10;

-- 7. Existencia de los 8 Casos Borde sembrados con su respectivo identificador
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

-- 8. Identificadores explícitos de los casos borde para verificación del jurado
SELECT * FROM (SELECT 'ID_ORDEN_300_LINEAS' AS caso, orden_id::text AS identificador FROM ordenes.linea_orden GROUP BY orden_id HAVING count(*) >= 300 LIMIT 1) t1
UNION ALL
SELECT * FROM (SELECT 'ID_CONTRATO_VENCIDO_AYER', id::text FROM proveedores.contrato WHERE vence_en = DATE '2026-09-20' - 1 LIMIT 1) t2
UNION ALL
SELECT * FROM (SELECT 'ID_CONTRATO_VENCE_HOY', id::text FROM proveedores.contrato WHERE vence_en = DATE '2026-09-20' LIMIT 1) t3
UNION ALL
SELECT * FROM (SELECT 'ID_ULTIMA_FRANJA_DISPONIBLE', id::text FROM logistica.franja_descargue WHERE id = (SELECT max(id) FROM logistica.franja_descargue) AND reservadas < capacidad LIMIT 1) t4
UNION ALL
SELECT * FROM (SELECT 'ID_PROVEEDOR_SIN_CONTRATO', id::text FROM proveedores.proveedor p WHERE p.id > 16500 AND NOT EXISTS (SELECT 1 FROM proveedores.contrato c WHERE c.proveedor_id = p.id) LIMIT 1) t5
UNION ALL
SELECT * FROM (SELECT 'ID_SKU_FUERA_CATALOGO', id::text FROM catalogo.sku s WHERE s.proveedor_id <> (SELECT proveedor_id FROM proveedores.contrato c WHERE c.id = s.contrato_id) LIMIT 1) t6
UNION ALL
SELECT * FROM (SELECT 'ID_PROVEEDOR_HOT_5000_ORDENES', proveedor_id::text FROM ordenes.orden_compra GROUP BY proveedor_id HAVING count(*) >= 5000 LIMIT 1) t7
UNION ALL
SELECT * FROM (SELECT 'ID_PROVEEDOR_ACTIVO_SIN_ORDEN_EN_MES', id::text FROM proveedores.proveedor p WHERE p.id <= 16500 AND p.activo AND EXISTS (SELECT 1 FROM ordenes.orden_compra o WHERE o.proveedor_id = p.id) AND NOT EXISTS (SELECT 1 FROM ordenes.orden_compra o WHERE o.proveedor_id = p.id AND date_trunc('month', o.creada_en) = DATE '2025-12-01') LIMIT 1) t8;

-- 9. Checksum de determinismo
SELECT 'determinismo_checksum' AS metrica, md5(string_agg(id::text || ':' || proveedor_id::text || ':' || creada_en::text, ',' ORDER BY id)) AS valor
FROM (SELECT id, proveedor_id, creada_en FROM ordenes.orden_compra ORDER BY id LIMIT 10000) muestra;

-- 10. Métricas OTIF y Fill Rate
SELECT 'otif_fill_rate' AS metrica,
  round(100.0 * avg((entregada_en <= prometida_en)::int), 2) AS otif_pct,
  round(100.0 * sum(cantidad_recibida) / NULLIF(sum(cantidad_solicitada), 0), 2) AS fill_rate_pct
FROM ordenes.orden_compra;

-- 11. Franjas disponibles fecha referencia
SELECT 'franjas_fecha_referencia' AS metrica, count(*) AS valor
FROM logistica.franja_descargue
WHERE inicia_en::date = DATE '2026-09-20';

-- 12. Regla dura: Fallar si no supera los objetivos
DO $$
BEGIN
  IF (SELECT count(*) FROM proveedores.proveedor) <= 100000
     OR (SELECT count(*) FROM proveedores.contrato) <= 15000
     OR (SELECT count(*) FROM catalogo.sku) <= 400000
     OR (SELECT count(*) FROM ordenes.orden_compra) <= 500000
     OR (SELECT count(*) FROM ordenes.linea_orden) <= 3000000
     OR (SELECT count(*) FROM logistica.franja_descargue) <= 150000
     OR (SELECT count(*) FROM auditoria.evento) <= 800000 THEN
    RAISE EXCEPTION 'No se supero el volumen objetivo exigido en todas las entidades';
  END IF;
END $$;

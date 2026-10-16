-- Corrección de diseño físico del E3 (reversible con mejoras_revert.sql).
-- 1) Índice de cobertura para Q4/Q5: mismo orden que orden_proveedor_fecha_idx más las columnas
--    que Q4 agrega. Permite Index Only Scan (sin visitar el heap) y además es redundante con el
--    índice original: si este desaparece (falla index_drop, E7), Q5 y Q4 siguen con un buen plan.
CREATE INDEX IF NOT EXISTS orden_proveedor_fecha_cov_idx
  ON ordenes.orden_compra (proveedor_id, creada_en DESC, id DESC)
  INCLUDE (prometida_en, entregada_en, cantidad_solicitada, cantidad_recibida);
-- 2) Índice de cobertura para Q3: evita el Bitmap Heap Scan de ~1 050 páginas por CEDI.
CREATE INDEX IF NOT EXISTS franja_cedi_fecha_cov_idx
  ON logistica.franja_descargue (cedi_id, inicia_en) INCLUDE (reservadas, capacidad, id);
-- El mapa de visibilidad debe estar al día para que el Index Only Scan no consulte el heap.
VACUUM (ANALYZE) ordenes.orden_compra;
VACUUM (ANALYZE) logistica.franja_descargue;

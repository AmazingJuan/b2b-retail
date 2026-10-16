-- Revierte mejoras.sql para volver al diseño físico del E2.
DROP INDEX IF EXISTS ordenes.orden_proveedor_fecha_cov_idx;
DROP INDEX IF EXISTS logistica.franja_cedi_fecha_cov_idx;
ANALYZE ordenes.orden_compra;
ANALYZE logistica.franja_descargue;

-- ============================================================================
-- MODELO FÍSICO DDL - PORTAL B2B RETAIL
-- ============================================================================
-- Cumple aislamiento por esquemas funcionales (Bounded Contexts):
-- 1. proveedores: Registro de entidades proveedoras y contratos marco de suministro.
-- 2. catalogo: SKUs negociados bajo contratos vigentes con precios y vigencias.
-- 3. ordenes: Cabeceras de orden de compra y detalle de líneas de producto.
-- 4. logistica: Franjas horarias de descargue en Centros de Distribución (CEDI).
-- 5. auditoria: Registro append-only transversal de trazabilidad operacional.
-- ============================================================================

DROP SCHEMA IF EXISTS auditoria CASCADE;
DROP SCHEMA IF EXISTS logistica CASCADE;
DROP SCHEMA IF EXISTS ordenes CASCADE;
DROP SCHEMA IF EXISTS catalogo CASCADE;
DROP SCHEMA IF EXISTS proveedores CASCADE;

CREATE SCHEMA proveedores;
CREATE SCHEMA catalogo;
CREATE SCHEMA ordenes;
CREATE SCHEMA logistica;
CREATE SCHEMA auditoria;

-- ============================================================================
-- ESQUEMA: proveedores
-- ============================================================================

-- Proveedores comerciales registrados en la plataforma.
-- id integer (4 bytes): Suficiente para escala de cientos de miles de proveedores.
-- creado_en date (4 bytes): Granularidad diaria suficiente para alta de cuenta.
CREATE TABLE proveedores.proveedor (
  id integer PRIMARY KEY,
  nombre text NOT NULL,
  activo boolean NOT NULL,
  creado_en date NOT NULL
);

-- Contratos marco de suministro suscritos con proveedores.
-- Desfase intencional: solo ~14% de proveedores cuenta con contrato activo (camino de rechazo).
CREATE TABLE proveedores.contrato (
  id integer PRIMARY KEY,
  proveedor_id integer NOT NULL,
  inicia_en date NOT NULL,
  vence_en date NOT NULL,
  estado text NOT NULL
);
ALTER TABLE proveedores.contrato ADD CONSTRAINT contrato_proveedor_fk FOREIGN KEY (proveedor_id) REFERENCES proveedores.proveedor (id);

-- Soporte directo a Q1 (validación de contrato vigente por proveedor_id y fecha).
CREATE INDEX contrato_proveedor_vigencia_idx ON proveedores.contrato (proveedor_id, inicia_en, vence_en);

-- ============================================================================
-- ESQUEMA: catalogo
-- ============================================================================

-- SKUs con precios y periodos de vigencia pactados contractualmente.
-- precio numeric(12,2): Precisión monetaria exacta sin errores de coma flotante.
CREATE TABLE catalogo.sku (
  id integer PRIMARY KEY,
  contrato_id integer NOT NULL,
  proveedor_id integer NOT NULL,
  precio numeric(12,2) NOT NULL,
  vigente_desde date NOT NULL,
  vigente_hasta date NOT NULL
);
ALTER TABLE catalogo.sku ADD CONSTRAINT sku_contrato_fk FOREIGN KEY (contrato_id) REFERENCES proveedores.contrato (id);
ALTER TABLE catalogo.sku ADD CONSTRAINT sku_proveedor_fk FOREIGN KEY (proveedor_id) REFERENCES proveedores.proveedor (id);

-- Soporte a Q2: verificación de precio y vigencia por proveedor_id y rango de fechas.
CREATE INDEX sku_proveedor_vigencia_idx ON catalogo.sku (proveedor_id, vigente_desde, vigente_hasta);
CREATE INDEX sku_contrato_idx ON catalogo.sku (contrato_id);

-- ============================================================================
-- ESQUEMA: ordenes
-- ============================================================================

-- Cabecera de órdenes de compra emitidas a proveedores hacia un CEDI.
-- cedi_id smallint (2 bytes): Soporta la red física de CEDIs (1 a 5) con mínimo footprint.
CREATE TABLE ordenes.orden_compra (
  id integer PRIMARY KEY,
  proveedor_id integer NOT NULL,
  creada_en timestamptz NOT NULL,
  estado text NOT NULL,
  cedi_id smallint NOT NULL,
  prometida_en timestamptz NOT NULL,
  entregada_en timestamptz NOT NULL,
  cantidad_solicitada integer NOT NULL,
  cantidad_recibida integer NOT NULL
);
ALTER TABLE ordenes.orden_compra ADD CONSTRAINT orden_proveedor_fk FOREIGN KEY (proveedor_id) REFERENCES proveedores.proveedor (id);

-- Índice compuesto de alta cobertura para Q5 (paginación ordenada) y filtro de Q4 (OTIF por proveedor).
CREATE INDEX orden_proveedor_fecha_idx ON ordenes.orden_compra (proveedor_id, creada_en DESC, id DESC);
CREATE INDEX orden_fecha_idx ON ordenes.orden_compra (creada_en);

-- Líneas de detalle asociadas a cada orden de compra.
-- id bigint (8 bytes): Proyectado para exceder holgadamente el límite de integer (3M+ filas).
-- cantidad smallint (2 bytes): Optimizado para tamaños típicos de lote por SKU.
CREATE TABLE ordenes.linea_orden (
  id bigint PRIMARY KEY,
  orden_id integer NOT NULL,
  sku_id integer NOT NULL,
  cantidad smallint NOT NULL,
  precio_unitario numeric(12,2) NOT NULL
);
ALTER TABLE ordenes.linea_orden ADD CONSTRAINT linea_orden_fk FOREIGN KEY (orden_id) REFERENCES ordenes.orden_compra (id);
ALTER TABLE ordenes.linea_orden ADD CONSTRAINT linea_sku_fk FOREIGN KEY (sku_id) REFERENCES catalogo.sku (id);

CREATE INDEX linea_orden_id_idx ON ordenes.linea_orden (orden_id);
CREATE INDEX linea_sku_idx ON ordenes.linea_orden (sku_id);

-- ============================================================================
-- ESQUEMA: logistica
-- ============================================================================

-- Franjas horarias de descargue en muelles de CEDIs (recurso escaso y concurrente).
-- capacidad y reservadas smallint (2 bytes): Contadores de muelle de bajo overhead.
CREATE TABLE logistica.franja_descargue (
  id integer PRIMARY KEY,
  cedi_id smallint NOT NULL,
  inicia_en timestamptz NOT NULL,
  capacidad smallint NOT NULL,
  reservadas smallint NOT NULL
);

-- Soporte directo a Q3: disponibilidad de franjas por CEDI y fecha.
CREATE INDEX franja_cedi_fecha_idx ON logistica.franja_descargue (cedi_id, inicia_en);

-- ============================================================================
-- ESQUEMA: auditoria
-- ============================================================================

-- Registro transversal inmutable (append-only) de eventos del dominio.
-- id bigint (8 bytes): Secuencia extendida de eventos operacionales.
-- detalle jsonb: Atributos contextuales flexibles con indexación binaria.
CREATE TABLE auditoria.evento (
  id bigint PRIMARY KEY,
  entidad text NOT NULL,
  entidad_id bigint NOT NULL,
  tipo text NOT NULL,
  creado_en timestamptz NOT NULL,
  detalle jsonb NOT NULL
);

CREATE INDEX evento_fecha_idx ON auditoria.evento (creado_en);
CREATE INDEX evento_entidad_idx ON auditoria.evento (entidad, entidad_id);

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

CREATE TABLE proveedores.proveedor (
  id integer PRIMARY KEY,
  nombre text NOT NULL,
  activo boolean NOT NULL,
  creado_en date NOT NULL
);
CREATE TABLE proveedores.contrato (
  id integer PRIMARY KEY,
  proveedor_id integer NOT NULL,
  inicia_en date NOT NULL,
  vence_en date NOT NULL,
  estado text NOT NULL
);
ALTER TABLE proveedores.contrato ADD CONSTRAINT contrato_proveedor_fk FOREIGN KEY (proveedor_id) REFERENCES proveedores.proveedor (id);
CREATE INDEX contrato_proveedor_vigencia_idx ON proveedores.contrato (proveedor_id, inicia_en, vence_en);

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
CREATE INDEX sku_proveedor_vigencia_idx ON catalogo.sku (proveedor_id, vigente_desde, vigente_hasta);
CREATE INDEX sku_contrato_idx ON catalogo.sku (contrato_id);

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
CREATE INDEX orden_proveedor_fecha_idx ON ordenes.orden_compra (proveedor_id, creada_en DESC, id DESC);
CREATE INDEX orden_fecha_idx ON ordenes.orden_compra (creada_en);

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

CREATE TABLE logistica.franja_descargue (
  id integer PRIMARY KEY,
  cedi_id smallint NOT NULL,
  inicia_en timestamptz NOT NULL,
  capacidad smallint NOT NULL,
  reservadas smallint NOT NULL
);
CREATE INDEX franja_cedi_fecha_idx ON logistica.franja_descargue (cedi_id, inicia_en);

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

#!/usr/bin/env python3
"""Ruta transaccional del E2 con la corrección del E3 (asignación de franjas por rango).

Idéntica a scripts/application_route.py salvo la tabla `assigned`. scripts/application_route.py queda intacto
como línea base. tests/test_ruta_equivalente.py verifica que ambas asignan exactamente las mismas franjas.
"""
import argparse
import csv
import json
import subprocess
import time
from pathlib import Path

FIELDS = [
    'id', 'proveedor_id', 'creada_en', 'estado', 'cedi_id',
    'prometida_en', 'entregada_en', 'cantidad_solicitada', 'cantidad_recibida',
]


def run_psql(dsn, sql, capture=True):
    result = subprocess.run(
        ['psql', dsn, '-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1'],
        input=sql,
        text=True,
        check=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ''


def main():
    parser = argparse.ArgumentParser(description='Ruta transaccional de ordenes (E3: asignacion de franjas por rango en lugar de LATERAL OFFSET)')
    parser.add_argument('--dsn', required=True)
    parser.add_argument('--orders-csv', default='datos/generated/ordenes.csv')
    parser.add_argument('--rows', type=int, default=5000)
    parser.add_argument('--output', type=Path, default=Path('evidence/application-route.json'))
    args = parser.parse_args()

    with open(args.orders_csv, newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))[:args.rows]
    if not rows:
        raise ValueError('No hay ordenes para procesar')

    offset = int(run_psql(args.dsn, 'SELECT COALESCE(max(id), 0) + 1 FROM ordenes.orden_compra;').splitlines()[-1])
    route_path = Path('/tmp/application-route.csv')
    with route_path.open('w', newline='', encoding='utf-8') as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        for index, row in enumerate(rows):
            row = dict(row)
            row['id'] = str(offset + index)
            writer.writerow(row)

    columns = ', '.join(FIELDS)
    started = time.perf_counter()
    sql = f"""
CREATE TEMP TABLE incoming (LIKE ordenes.orden_compra);
\\copy incoming ({columns}) FROM '{route_path}' WITH (FORMAT csv, HEADER true)
BEGIN;
CREATE TEMP TABLE accepted AS
SELECT i.*, row_number() OVER (PARTITION BY i.cedi_id ORDER BY i.id) AS slot_rank
FROM incoming i
WHERE EXISTS (SELECT 1 FROM proveedores.proveedor p WHERE p.id = i.proveedor_id)
  AND EXISTS (SELECT 1 FROM catalogo.sku s WHERE s.proveedor_id = i.proveedor_id);
CREATE TEMP TABLE assigned AS
SELECT a.*, f.id AS slot_id
FROM accepted a
JOIN (
  -- Corrección E3: la franja asignada a la orden k de un CEDI es la k-ésima franja disponible en orden
  -- de inicio. Se numeran una vez (un recorrido por CEDI) en lugar de un LATERAL con OFFSET por orden (O(n·k)).
  SELECT f.id, f.cedi_id, row_number() OVER (PARTITION BY f.cedi_id ORDER BY f.inicia_en, f.id) AS slot_rank
  FROM logistica.franja_descargue f
  WHERE f.reservadas < f.capacidad
    AND f.cedi_id IN (SELECT DISTINCT cedi_id FROM accepted)
) f ON f.cedi_id = a.cedi_id AND f.slot_rank = a.slot_rank;
INSERT INTO ordenes.orden_compra ({columns})
SELECT id, proveedor_id, creada_en, estado, cedi_id, prometida_en, entregada_en, cantidad_solicitada, cantidad_recibida
FROM assigned;
UPDATE logistica.franja_descargue f
SET reservadas = f.reservadas + 1
FROM assigned
WHERE f.id = assigned.slot_id;
COMMIT;
SELECT (SELECT count(*) FROM incoming), (SELECT count(*) FROM assigned);
"""
    result = run_psql(args.dsn, sql)
    attempted, inserted = [int(value) for value in result.splitlines()[-1].split('|')]
    elapsed = time.perf_counter() - started
    evidence = {
        'attempted_rows': attempted,
        'accepted_rows': inserted,
        'rejected_rows': attempted - inserted,
        'seconds': round(elapsed, 3),
        'rows_per_second': round(attempted / elapsed, 2),
        'validations': ['proveedor existente', 'SKU del proveedor', 'capacidad de franja'],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()

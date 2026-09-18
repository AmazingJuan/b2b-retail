#!/usr/bin/env python3
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


def run_psql(dsn, sql, capture=False):
    result = subprocess.run(
        ['psql', dsn, '-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1'],
        input=sql,
        text=True,
        check=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ''


def row_values(row):
    return (
        f"({row['id']},{row['proveedor_id']},'{row['creada_en']}','{row['estado']}',"
        f"{row['cedi_id']},'{row['prometida_en']}','{row['entregada_en']}',"
        f"{row['cantidad_solicitada']},{row['cantidad_recibida']})"
    )


def create_temp_table():
    return 'CREATE TEMP TABLE carga_tmp (LIKE ordenes.orden_compra);\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dsn', required=True)
    parser.add_argument('--orders-csv', default='datos/generated/ordenes.csv')
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--output', type=Path, default=Path('evidence/load-strategies.json'))
    args = parser.parse_args()

    with open(args.orders_csv, newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))[:args.rows]
    columns = ', '.join(FIELDS)
    results = {}

    for name in ['copy', 'insert_batch_1000', 'insert_row_autocommit', 'application_transactional_route']:
        started = time.perf_counter()
        if name == 'copy':
            copy_path = Path('/tmp/retail-copy.csv')
            with copy_path.open('w', newline='', encoding='utf-8') as output:
                writer = csv.DictWriter(output, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            run_psql(args.dsn, create_temp_table() + f"\\copy carga_tmp ({columns}) FROM '{copy_path}' WITH (FORMAT csv, HEADER true)\n")
            inserted_rows = len(rows)
        elif name == 'insert_batch_1000':
            statements = []
            for offset in range(0, len(rows), 1000):
                batch = ','.join(row_values(row) for row in rows[offset:offset + 1000])
                statements.append(f'INSERT INTO carga_tmp ({columns}) VALUES {batch};')
            run_psql(args.dsn, create_temp_table() + 'BEGIN;\n' + '\n'.join(statements) + '\nCOMMIT;\n')
            inserted_rows = len(rows)
        elif name == 'insert_row_autocommit':
            statements = [f'INSERT INTO carga_tmp ({columns}) VALUES {row_values(row)};' for row in rows]
            run_psql(args.dsn, create_temp_table() + '\n'.join(statements) + '\n')
            inserted_rows = len(rows)
        else:
            statements = []
            for row in rows:
                value = row_values(row)
                statements.append(
                    f"INSERT INTO carga_tmp ({columns}) SELECT incoming.id::integer, incoming.proveedor_id::integer, incoming.creada_en::timestamptz, incoming.estado::text, incoming.cedi_id::smallint, incoming.prometida_en::timestamptz, incoming.entregada_en::timestamptz, incoming.cantidad_solicitada::integer, incoming.cantidad_recibida::integer FROM (VALUES {value}) AS incoming({columns}) "
                    "WHERE EXISTS (SELECT 1 FROM proveedores.proveedor p WHERE p.id = incoming.proveedor_id) "
                    "AND EXISTS (SELECT 1 FROM catalogo.sku s WHERE s.proveedor_id = incoming.proveedor_id) "
                    "AND EXISTS (SELECT 1 FROM logistica.franja_descargue f WHERE f.cedi_id = incoming.cedi_id);"
                )
            output = run_psql(
                args.dsn,
                create_temp_table() + 'BEGIN;\n' + '\n'.join(statements) + '\nCOMMIT;\n'
                + 'SELECT count(*) FROM carga_tmp;\n',
                capture=True,
            )
            inserted_rows = int(output.splitlines()[-1])
        elapsed = time.perf_counter() - started
        results[name] = {
            'attempted_rows': len(rows),
            'inserted_rows': inserted_rows,
            'seconds': round(elapsed, 3),
            'rows_per_second': round(len(rows) / elapsed, 2),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'rows': len(rows), 'results': results}, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
import argparse
import bisect
import calendar
import csv
import hashlib
import json
import math
import random
from functools import lru_cache
from datetime import date
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)
REFERENCE_DATE = date(2026, 9, 20)
PROVIDERS = 100_000
CONTRACTS = 15_000
SKUS = 400_000
SLOTS = 150_000
EVENTS = 800_000
ZIPF_S = 1.08


@lru_cache(maxsize=None)
def zipf_bucket(start, end):
    ranks = list(range(start, end + 1))
    weights = [1.0 / (rank ** ZIPF_S) for rank in ranks]
    cumulative = []
    total = 0.0
    for weight in weights:
        total += weight
        cumulative.append(total)
    return ranks, cumulative, total


def zipf_provider(rng, count):
    bucket = rng.random()
    if bucket < 0.15:
        start, end = 1, min(1000, count)
    elif bucket < 0.40:
        start, end = min(1001, count), min(5000, count)
    else:
        start, end = min(5001, count), count
    ranks, cumulative, total = zipf_bucket(start, end)
    return ranks[bisect.bisect_left(cumulative, rng.random() * total)]


def order_date(rng, index, total):
    day = rng.randint(0, 730)
    date = BASE_DATE + timedelta(days=day)
    if date.day >= 28 and rng.random() < 0.65:
        last_day = calendar.monthrange(date.year, date.month)[1]
        date = date.replace(day=min(28 + rng.randint(0, 3), last_day))
    hour = rng.choices(range(24), weights=[1, 1, 1, 1, 2, 4, 10, 18, 26, 28, 24, 16, 10, 8, 7, 6, 5, 4, 3, 2, 2, 1, 1, 1], k=1)[0]
    return date.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60))


def write_csv(path, header, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('datos/generated'))
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--orders', type=int, default=500_000)
    parser.add_argument('--as-of-date', type=date.fromisoformat, default=REFERENCE_DATE)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    args.output.mkdir(parents=True, exist_ok=True)

    write_csv(args.output / 'proveedores.csv', ['id', 'nombre', 'activo', 'creado_en'],
              ((i, f'Proveedor {i:06d}', True, (BASE_DATE - timedelta(days=rng.randrange(3650))).date().isoformat()) for i in range(1, PROVIDERS + 1)))

    contracts = []
    for i in range(1, CONTRACTS + 1):
        provider = i
        start = args.as_of_date - timedelta(days=30)
        end = args.as_of_date + timedelta(days=365)
        if i == 1:
            end = args.as_of_date - timedelta(days=1)
        elif i == 2:
            end = args.as_of_date
        elif i == 3:
            end = args.as_of_date + timedelta(days=1)
        contracts.append((i, provider, start.isoformat(), end.isoformat(), 'vigente' if end >= args.as_of_date else 'vencido'))
    write_csv(args.output / 'contratos.csv', ['id', 'proveedor_id', 'inicia_en', 'vence_en', 'estado'], contracts)

    def sku_rows():
        for i in range(1, SKUS + 1):
            contract_id = ((i - 1) % CONTRACTS) + 1
            provider_id = 99_999 if i == SKUS else contract_id
            yield (i, contract_id, provider_id, f'{rng.uniform(5, 5000):.2f}', '2024-01-01', '2030-12-31')
    write_csv(args.output / 'skus.csv', ['id', 'contrato_id', 'proveedor_id', 'precio', 'vigente_desde', 'vigente_hasta'], sku_rows())

    write_csv(args.output / 'franjas.csv', ['id', 'cedi_id', 'inicia_en', 'capacidad', 'reservadas'],
              ((i, ((i - 1) % 5) + 1, (BASE_DATE + timedelta(minutes=(i - 1) * 15)).isoformat(), 20, 19 if i == SLOTS else rng.randrange(0, 20)) for i in range(1, SLOTS + 1)))

    order_rows = []
    line_rows = []
    audit_rows = []
    line_id = 1
    for order_id in range(1, args.orders + 1):
        provider = 1 if order_id <= max(5000, args.orders // 20) else zipf_provider(rng, PROVIDERS)
        created = order_date(rng, order_id, args.orders)
        promised = created + timedelta(days=1 + (order_id % 5))
        delivered = promised + timedelta(days=-1 if order_id % 11 == 0 else (1 if order_id % 7 == 0 else 0))
        requested = 10 + (order_id % 40)
        received = requested - (order_id % 5 if order_id % 13 == 0 else 0)
        order_rows.append((order_id, provider, created.isoformat(), 'confirmada', (order_id % 5) + 1, promised.isoformat(), delivered.isoformat(), requested, received))
        line_count = 300 if order_id == 1 else max(1, min(300, int(rng.expovariate(1 / 5.5)) + 1))
        for _ in range(line_count):
            sku = ((provider - 1) % SKUS) + 1 if order_id == 2 else rng.randint(1, SKUS)
            line_rows.append((line_id, order_id, sku, rng.randint(1, 50), f'{rng.uniform(5, 5000):.2f}'))
            line_id += 1
        if order_id <= EVENTS:
            audit_rows.append((order_id, 'orden', order_id, 'creada', created.isoformat(), json.dumps({'seed': args.seed, 'orden': order_id})))

    while len(audit_rows) < EVENTS:
        event_id = len(audit_rows) + 1
        audit_rows.append((event_id, 'proveedor', ((event_id - 1) % PROVIDERS) + 1, 'actualizado', (BASE_DATE + timedelta(days=event_id % 730)).isoformat(), json.dumps({'batch': event_id % 100})))

    write_csv(args.output / 'ordenes.csv', ['id', 'proveedor_id', 'creada_en', 'estado', 'cedi_id', 'prometida_en', 'entregada_en', 'cantidad_solicitada', 'cantidad_recibida'], order_rows)
    write_csv(args.output / 'lineas.csv', ['id', 'orden_id', 'sku_id', 'cantidad', 'precio_unitario'], line_rows)
    write_csv(args.output / 'eventos.csv', ['id', 'entidad', 'entidad_id', 'tipo', 'creado_en', 'detalle'], audit_rows)

    checksums = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.output.glob('*.csv'))
    }
    manifest = {
        'seed': args.seed,
        'as_of_date': args.as_of_date.isoformat(),
        'orders': args.orders,
        'lines': len(line_rows),
        'zipf_s': ZIPF_S,
        'checksum_ordenes': checksums['ordenes.csv'],
        'checksums_csv': checksums,
    }
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()

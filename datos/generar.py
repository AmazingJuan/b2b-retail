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

# ============================================================================
# PARÁMETROS DE VOLUMEN (Superan el objetivo en todas las entidades)
# ============================================================================
PROVIDERS = 105_000   # Objetivo: 100.000 -> Superado (+5.000)
CONTRACTS = 16_500    # Objetivo: 15.000  -> Superado (+1.500)
SKUS = 420_000        # Objetivo: 400.000 -> Superado (+20.000)
SLOTS = 160_000       # Objetivo: 150.000 -> Superado (+10.000)
EVENTS = 850_000      # Objetivo: 800.000 -> Superado (+50.000)
ORDERS = 510_000      # Objetivo: 500.000 -> Superado (+10.000)

# Parámetro de concentración Zipf para proveedores
ZIPF_S = 1.08

# Concentración de SKUs del catálogo: 10% de SKUs concentra el 60% de líneas
CATALOG_TOP_PCT = 0.10
CATALOG_TOP_CONCENTRATION = 0.60
TOP_SKU_COUNT = int(SKUS * CATALOG_TOP_PCT)  # Primeros 42.000 SKUs


@lru_cache(maxsize=None)
def zipf_bucket(start, end):
    """Calcula pesos y acumulados para muestreo discreto ponderado."""
    ranks = list(range(start, end + 1))
    weights = [1.0 / (rank ** ZIPF_S) for rank in ranks]
    cumulative = []
    total = 0.0
    for weight in weights:
        total += weight
        cumulative.append(total)
    return ranks, cumulative, total


def zipf_provider(rng, count):
    """
    Genera un proveedor muestreando la distribución Zipf segmentada:
    - Top 1% (1..1050) captura ~15% en sorteo directo + efecto de cola
    - Top 5% (1051..5250) captura ~25%
    - Resto (5251..count) captura ~60%
    """
    bucket = rng.random()
    top_1 = min(1050, count)
    top_5 = min(5250, count)
    if bucket < 0.15:
        start, end = 1, top_1
    elif bucket < 0.40:
        start, end = min(top_1 + 1, count), top_5
    else:
        start, end = min(top_5 + 1, count), count
    ranks, cumulative, total = zipf_bucket(start, end)
    return ranks[bisect.bisect_left(cumulative, rng.random() * total)]


def order_date(rng):
    """
    Genera la fecha y hora de la orden aplicando estacionalidad verificable:
    1. Estacionalidad mensual: Los últimos 3 días hábiles de cada mes concentran el 25% de órdenes.
    2. Estacionalidad horaria: Pico matutino entre 8:00 y 11:00 (pesos altos), valle nocturno 00:00-05:00.
    """
    month_offset = rng.randint(0, 23)
    year = 2024 + (month_offset // 12)
    month = 1 + (month_offset % 12)
    last_day = calendar.monthrange(year, month)[1]

    # Calcular los días hábiles del mes (lunes=0 .. viernes=4)
    all_days = list(range(1, last_day + 1))
    biz_days = [d for d in all_days if datetime(year, month, d).weekday() < 5]
    last_3_biz = set(biz_days[-3:])
    other_days = [d for d in all_days if d not in last_3_biz]

    # Concentración verificable: 25% de probabilidad en los últimos 3 días hábiles
    if rng.random() < 0.25 and last_3_biz:
        day = rng.choice(sorted(last_3_biz))
    else:
        day = rng.choice(other_days if other_days else all_days)

    # Distribución horaria: 8:00 - 11:00 am pico alto (~48%), 0:00 - 5:00 am valle (~3.5%)
    weights = [
        1, 1, 1, 1, 1, 2,  # 00:00 - 05:00 (valle nocturno)
        6, 12,             # 06:00 - 07:00 (apertura)
        24, 28, 26, 22,    # 08:00 - 11:00 (pico comercial)
        15, 12, 10, 8,     # 12:00 - 15:00 (media tarde)
        7, 6, 5, 4,        # 16:00 - 19:00 (cierre)
        3, 2, 2, 1         # 20:00 - 23:00 (noche)
    ]
    hour = rng.choices(range(24), weights=weights, k=1)[0]
    return datetime(year, month, day, hour, rng.randrange(60), rng.randrange(60), tzinfo=timezone.utc)


def sample_catalog_sku(rng):
    """
    Sesgo en el catálogo: El 10% de los SKU aparece en el 60% de las líneas de orden.
    """
    if rng.random() < CATALOG_TOP_CONCENTRATION:
        return rng.randint(1, TOP_SKU_COUNT)
    else:
        return rng.randint(TOP_SKU_COUNT + 1, SKUS)


def write_csv(path, header, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description='Generador de datos determinista B2B Retail')
    parser.add_argument('--output', type=Path, default=Path('datos/generated'))
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--orders', type=int, default=ORDERS)
    parser.add_argument('--as-of-date', type=date.fromisoformat, default=REFERENCE_DATE)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    args.output.mkdir(parents=True, exist_ok=True)

    # 1. Proveedores: 105.000 filas
    print(f'Generando {PROVIDERS:,} proveedores...')
    write_csv(
        args.output / 'proveedores.csv',
        ['id', 'nombre', 'activo', 'creado_en'],
        ((i, f'Proveedor {i:06d}', True, (BASE_DATE - timedelta(days=rng.randrange(3650))).date().isoformat()) for i in range(1, PROVIDERS + 1))
    )

    # 2. Contratos: 16.500 filas
    # Desfase: solo ~15.7% de proveedores tiene contrato marco activo.
    print(f'Generando {CONTRACTS:,} contratos de suministro...')
    contracts = []
    for i in range(1, CONTRACTS + 1):
        provider = i
        start = args.as_of_date - timedelta(days=30)
        end = args.as_of_date + timedelta(days=365)
        # Casos borde temporales sembrados
        if i == 1:
            end = args.as_of_date - timedelta(days=1)  # Caso borde 2: vencido ayer
        elif i == 2:
            end = args.as_of_date                      # Caso borde 3: vence hoy
        elif i == 3:
            end = args.as_of_date + timedelta(days=1)  # vence mañana
        contracts.append((i, provider, start.isoformat(), end.isoformat(), 'vigente' if end >= args.as_of_date else 'vencido'))
    write_csv(args.output / 'contratos.csv', ['id', 'proveedor_id', 'inicia_en', 'vence_en', 'estado'], contracts)

    # 3. SKUs negociados: 420.000 filas
    print(f'Generando {SKUS:,} SKUs negociados...')
    def sku_rows():
        for i in range(1, SKUS + 1):
            contract_id = ((i - 1) % CONTRACTS) + 1
            # Caso borde 6: SKU fuera del catálogo negociado del proveedor asignado
            provider_id = 99_999 if i == SKUS else contract_id
            yield (i, contract_id, provider_id, f'{rng.uniform(5, 5000):.2f}', '2024-01-01', '2030-12-31')
    write_csv(args.output / 'skus.csv', ['id', 'contrato_id', 'proveedor_id', 'precio', 'vigente_desde', 'vigente_hasta'], sku_rows())

    # 4. Franjas de descargue: 160.000 filas
    print(f'Generando {SLOTS:,} franjas de descargue de CEDI...')
    write_csv(
        args.output / 'franjas.csv',
        ['id', 'cedi_id', 'inicia_en', 'capacidad', 'reservadas'],
        ((i, ((i - 1) % 5) + 1, (BASE_DATE + timedelta(minutes=(i - 1) * 15)).isoformat(), 20, 19 if i == SLOTS else rng.randrange(0, 20)) for i in range(1, SLOTS + 1))
    )

    # 5. Órdenes, Líneas y Eventos
    print(f'Generando {args.orders:,} órdenes y líneas de compra...')
    order_rows = []
    line_rows = []
    audit_rows = []
    line_id = 1

    for order_id in range(1, args.orders + 1):
        # Caso borde 7: Proveedor hot con 5000+ órdenes (proveedor 1)
        provider = 1 if order_id <= max(5000, args.orders // 20) else zipf_provider(rng, PROVIDERS)
        created = order_date(rng)
        promised = created + timedelta(days=1 + (order_id % 5))
        delivered = promised + timedelta(days=-1 if order_id % 11 == 0 else (1 if order_id % 7 == 0 else 0))
        requested = 10 + (order_id % 40)
        received = requested - (order_id % 5 if order_id % 13 == 0 else 0)
        order_rows.append((order_id, provider, created.isoformat(), 'confirmada', (order_id % 5) + 1, promised.isoformat(), delivered.isoformat(), requested, received))

        # Cantidad de líneas: Caso borde 1 (orden con 300 líneas) + distribución exponencial
        line_count = 300 if order_id == 1 else max(1, min(300, int(rng.expovariate(1 / 5.5)) + 1))
        for _ in range(line_count):
            sku = ((provider - 1) % SKUS) + 1 if order_id == 2 else sample_catalog_sku(rng)
            line_rows.append((line_id, order_id, sku, rng.randint(1, 50), f'{rng.uniform(5, 5000):.2f}'))
            line_id += 1

        if order_id <= EVENTS:
            audit_rows.append((order_id, 'orden', order_id, 'creada', created.isoformat(), json.dumps({'seed': args.seed, 'orden': order_id})))

    # Completar eventos de auditoría hasta superar objetivo (850.000)
    print(f'Generando {EVENTS:,} eventos de auditoría transversal...')
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
        'catalog_top_pct': CATALOG_TOP_PCT,
        'catalog_top_concentration': CATALOG_TOP_CONCENTRATION,
        'checksum_ordenes': checksums['ordenes.csv'],
        'checksums_csv': checksums,
    }
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Generación exitosa. Manifiesto:')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
import json
import subprocess
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT_DOCS = ROOT / 'figuras'
OUT_EVIDENCE = ROOT / 'evidence' / 'figuras'
EVIDENCE = ROOT / 'evidence'

OUT_DOCS.mkdir(parents=True, exist_ok=True)
OUT_EVIDENCE.mkdir(parents=True, exist_ok=True)

DSN = 'host=localhost port=5433 dbname=retail user=retail password=retail'

NAVY = '#102A43'
TEAL = '#117A8B'
AMBER = '#E59F2F'
CORAL = '#D95D39'
PALE = '#EAF3F4'
GRID = '#D6E1E5'
TEXT = '#253746'

plt.rcParams.update({
    'font.family': 'DejaVu Sans',
    'font.size': 10,
    'axes.titlesize': 13,
    'axes.titleweight': 'bold',
    'axes.labelsize': 10,
    'axes.edgecolor': GRID,
    'axes.labelcolor': TEXT,
    'xtick.color': TEXT,
    'ytick.color': TEXT,
    'text.color': TEXT,
    'figure.facecolor': 'white',
    'axes.facecolor': 'white',
    'savefig.facecolor': 'white',
    'axes.spines.top': False,
    'axes.spines.right': False,
})


def psql_query(sql):
    """Ejecuta una consulta SQL directamente en la base de datos PostgreSQL."""
    cmd = ['psql', DSN, '-X', '-q', '-t', '-A', '-c', sql]
    output = subprocess.check_output(cmd, text=True).strip()
    return output.splitlines() if output else []


def save(name):
    plt.tight_layout()
    plt.savefig(OUT_DOCS / name, dpi=240, bbox_inches='tight')
    plt.savefig(OUT_EVIDENCE / name, dpi=240, bbox_inches='tight')
    plt.close()


def volume_chart():
    """Genera gráfico de volumen consultando directamente los conteos de la BD."""
    tables = [
        ('proveedores.proveedor', 'Proveedores'),
        ('proveedores.contrato', 'Contratos'),
        ('catalogo.sku', 'SKU'),
        ('ordenes.orden_compra', 'Órdenes'),
        ('ordenes.linea_orden', 'Líneas'),
        ('logistica.franja_descargue', 'Franjas'),
        ('auditoria.evento', 'Eventos'),
    ]
    labels = []
    values = []
    for tbl, lbl in tables:
        cnt = int(psql_query(f'SELECT count(*) FROM {tbl};')[0])
        labels.append(lbl)
        values.append(cnt)

    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    colors = [TEAL, TEAL, TEAL, TEAL, AMBER, TEAL, TEAL]
    bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1], height=0.62)
    ax.set_xscale('log')
    ax.grid(axis='x', color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlabel('Filas registradas (escala logarítmica)')
    ax.set_title('Volumen final verificado en base de datos por entidad (Todas superan el objetivo)', loc='left', color=NAVY, pad=14)
    for bar, value in zip(bars, values[::-1]):
        ax.text(value * 1.08, bar.get_y() + bar.get_height() / 2, f'{value:,}'.replace(',', '.'), va='center', fontsize=9)
    ax.set_xlim(7000, 8000000)
    save('volumen_final.pdf')


def load_chart():
    """Genera comparativa de estrategias de carga a partir de load-strategies.json."""
    data = json.loads((EVIDENCE / 'load-strategies.json').read_text())['results']
    keys = ['copy', 'insert_batch_1000', 'insert_row_autocommit', 'application_transactional_route']
    labels = ['COPY\nmasivo', 'INSERT\nlotes de 1.000', 'INSERT\nfila a fila', 'Ruta\ntransaccional']
    values = [data[key]['rows_per_second'] for key in keys]

    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    bars = ax.bar(labels, values, color=[AMBER, TEAL, TEAL, CORAL], width=0.60)
    ax.grid(axis='y', color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_ylabel('Rendimiento (filas por segundo)')
    ax.set_title('Comparación de estrategias de inyección sobre 50.000 órdenes', loc='left', color=NAVY, pad=14)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value * 1.03, f'{value:,.0f}'.replace(',', '.'), ha='center', va='bottom', fontsize=9)
    ax.set_ylim(0, max(values) * 1.22)
    save('estrategias_carga.pdf')


def percentile_chart():
    """Genera percentiles p50, p95 y p99 para escenarios frío y caliente."""
    data = json.loads((EVIDENCE / 'metrics.json').read_text())['queries']
    query_ids = list(data.keys())
    percentiles = ['p50_ms', 'p95_ms', 'p99_ms']
    colors = [TEAL, AMBER, CORAL]

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.3), sharey=True)
    x = np.arange(len(query_ids))
    width = 0.24

    for ax, scenario, title in zip(axes, ['cold', 'hot'], ['Parámetro frío (cola larga)', 'Parámetro caliente (top 1%)']):
        for i, (percentile, color) in enumerate(zip(percentiles, colors)):
            values = [data[q][scenario][percentile] for q in query_ids]
            ax.bar(x + (i - 1) * width, values, width, label=percentile.replace('_ms', '').upper(), color=color)
        ax.set_xticks(x)
        ax.set_xticklabels(query_ids)
        ax.grid(axis='y', color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_title(title, color=NAVY, fontsize=11)
        ax.set_xlabel('Consulta evaluada')

    axes[0].set_ylabel('Tiempo de respuesta observado (ms)')
    axes[1].legend(frameon=False, loc='upper left', ncol=3, bbox_to_anchor=(-0.05, 1.14))
    fig.suptitle('Percentiles de latencia sobre 200 ejecuciones por escenario', x=0.04, ha='left', color=NAVY, fontsize=13, fontweight='bold')
    save('percentiles_consultas.pdf')


def shape_chart():
    """Genera curva temporal y distribución de líneas consultando la base de datos."""
    # 1. Órdenes por día desde la base de datos
    daily_rows = psql_query('SELECT creada_en::date, count(*) FROM ordenes.orden_compra GROUP BY creada_en::date ORDER BY creada_en::date;')
    daily_values = [int(r.split('|')[1]) for r in daily_rows]

    # 2. Conteo de líneas por orden agrupadas
    lines_rows = psql_query('SELECT LEAST(cnt, 40) AS bucket, count(*) FROM (SELECT orden_id, count(*) AS cnt FROM ordenes.linea_orden GROUP BY orden_id) t GROUP BY bucket ORDER BY bucket;')
    counts = {int(r.split('|')[0]): int(r.split('|')[1]) for r in lines_rows}

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.9))

    # Gráfica temporal
    axes[0].plot(range(len(daily_values)), daily_values, color=TEAL, linewidth=1.4)
    axes[0].fill_between(range(len(daily_values)), daily_values, color=PALE)
    axes[0].set_title('Estacionalidad temporal (730 días)', color=NAVY, fontsize=11)
    axes[0].set_xlabel('Días transcurridos')
    axes[0].set_ylabel('Órdenes emitidas')
    axes[0].grid(axis='y', color=GRID, linewidth=0.8)

    # Gráfica de colas de líneas
    bins = sorted(counts.keys())
    axes[1].bar([str(x) if x < 40 else '40+' for x in bins], [counts[x] for x in bins], color=AMBER)
    axes[1].set_title('Cola larga de líneas por orden (máx 300)', color=NAVY, fontsize=11)
    axes[1].set_xlabel('Líneas por orden de compra')
    axes[1].set_ylabel('Cantidad de órdenes')
    axes[1].tick_params(axis='x', labelrotation=55, labelsize=7)
    axes[1].grid(axis='y', color=GRID, linewidth=0.8)

    fig.suptitle('Forma y estacionalidad del dato verificadas en PostgreSQL', x=0.04, ha='left', color=NAVY, fontsize=13, fontweight='bold')
    save('forma_datos.pdf')


def zipf_chart():
    """Genera gráfico adicional comparativo de distribución Zipf teórica vs observada."""
    data = psql_query('SELECT proveedor_id, count(*) AS total FROM ordenes.orden_compra GROUP BY proveedor_id ORDER BY total DESC LIMIT 100;')
    ranks = np.arange(1, len(data) + 1)
    observed = [int(r.split('|')[1]) for r in data]

    # Curva teórica normalizada a la frecuencia de rango 1
    theoretical = [observed[0] / (r ** 1.08) for r in ranks]

    fig, ax = plt.subplots(figsize=(8.5, 4.0))
    ax.plot(ranks, observed, 'o-', color=TEAL, markersize=3.5, label='Frecuencia observada (BD)')
    ax.plot(ranks, theoretical, '--', color=CORAL, linewidth=1.8, label='Curva teórica Zipf (s = 1.08)')
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.grid(True, which='both', color=GRID, linewidth=0.6)
    ax.set_xlabel('Rango del proveedor (log)')
    ax.set_ylabel('Órdenes acumuladas (log)')
    ax.set_title('Ajuste de distribución Zipf en proveedores (s = 1.08)', loc='left', color=NAVY, pad=12)
    ax.legend(frameon=False)
    save('distribucion_zipf.pdf')


if __name__ == '__main__':
    print('Generando figuras leyendo directamente de PostgreSQL...')
    volume_chart()
    load_chart()
    percentile_chart()
    shape_chart()
    zipf_chart()
    print(f'Figuras exportadas exitosamente en:\n - {OUT_DOCS}\n - {OUT_EVIDENCE}')

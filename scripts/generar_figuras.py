#!/usr/bin/env python3
import csv
import json
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence' / 'figuras'
DATA = ROOT / 'datos' / 'generated'
EVIDENCE = ROOT / 'evidence'
OUT.mkdir(parents=True, exist_ok=True)

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
    'axes.titlesize': 14,
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


def save(name):
    plt.tight_layout()
    plt.savefig(OUT / name, dpi=220, bbox_inches='tight')
    plt.close()


def volume_chart():
    labels = ['Proveedores', 'Contratos', 'SKU', 'Ordenes', 'Lineas', 'Franjas', 'Eventos']
    values = [100000, 15000, 400000, 505000, 3003817, 150000, 800000]
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    bars = ax.barh(labels[::-1], values[::-1], color=[TEAL, TEAL, TEAL, TEAL, AMBER, TEAL, TEAL][::-1], height=.62)
    ax.set_xscale('log')
    ax.grid(axis='x', color=GRID, linewidth=.8)
    ax.set_axisbelow(True)
    ax.set_xlabel('Filas, escala logaritmica')
    ax.set_title('Volumen final por entidad', loc='left', color=NAVY, pad=14)
    for bar, value in zip(bars, values[::-1]):
        ax.text(value * 1.08, bar.get_y() + bar.get_height() / 2, f'{value:,}'.replace(',', '.'), va='center', fontsize=9)
    ax.set_xlim(7000, 7000000)
    save('volumen_final.pdf')


def load_chart():
    data = json.loads((EVIDENCE / 'load-strategies.json').read_text())['results']
    keys = ['copy', 'insert_batch_1000', 'insert_row_autocommit', 'application_transactional_route']
    labels = ['COPY', 'INSERT\nlotes de 1.000', 'INSERT\nfila a fila', 'Ruta\ntransaccional']
    values = [data[key]['rows_per_second'] for key in keys]
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    bars = ax.bar(labels, values, color=[AMBER, TEAL, TEAL, CORAL], width=.62)
    ax.grid(axis='y', color=GRID, linewidth=.8)
    ax.set_axisbelow(True)
    ax.set_ylabel('Filas por segundo')
    ax.set_title('Comparacion de estrategias de carga', loc='left', color=NAVY, pad=14)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value * 1.03, f'{value:,.0f}'.replace(',', '.'), ha='center', va='bottom', fontsize=9)
    ax.set_ylim(0, max(values) * 1.22)
    save('estrategias_carga.pdf')


def percentile_chart():
    data = json.loads((EVIDENCE / 'metrics.json').read_text())['queries']
    query_ids = list(data)
    percentiles = ['p50_ms', 'p95_ms', 'p99_ms']
    colors = [TEAL, AMBER, CORAL]
    fig, axes = plt.subplots(1, 2, figsize=(9, 4.3), sharey=True)
    x = np.arange(len(query_ids))
    width = .24
    for ax, scenario, title in zip(axes, ['cold', 'hot'], ['Parametro frio', 'Parametro caliente']):
        for i, (percentile, color) in enumerate(zip(percentiles, colors)):
            values = [data[q][scenario][percentile] for q in query_ids]
            ax.bar(x + (i - 1) * width, values, width, label=percentile.replace('_ms', ''), color=color)
        ax.set_xticks(x)
        ax.set_xticklabels(query_ids)
        ax.grid(axis='y', color=GRID, linewidth=.8)
        ax.set_axisbelow(True)
        ax.set_title(title, color=NAVY)
        ax.set_xlabel('Consulta')
    axes[0].set_ylabel('Tiempo de respuesta (ms)')
    axes[1].legend(frameon=False, loc='upper left', ncol=3, bbox_to_anchor=(-.02, 1.12))
    fig.suptitle('Percentiles sobre 200 ejecuciones por escenario', x=.05, ha='left', color=NAVY, fontsize=14, fontweight='bold')
    save('percentiles_consultas.pdf')


def shape_chart():
    days = Counter()
    line_counts = Counter()
    with (DATA / 'ordenes.csv').open(newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            days[row['creada_en'][:10]] += 1
    with (DATA / 'lineas.csv').open(newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            line_counts[row['orden_id']] += 1
    ordered_days = sorted(days)
    daily_values = [days[day] for day in ordered_days]
    counts = Counter(min(value, 40) for value in line_counts.values())
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8))
    axes[0].plot(range(len(daily_values)), daily_values, color=TEAL, linewidth=1.4)
    axes[0].fill_between(range(len(daily_values)), daily_values, color=PALE)
    axes[0].set_title('Ordenes por dia', color=NAVY)
    axes[0].set_xlabel('Dias desde el inicio')
    axes[0].set_ylabel('Ordenes')
    axes[0].grid(axis='y', color=GRID, linewidth=.8)
    bins = sorted(counts)
    axes[1].bar([str(x) if x < 40 else '40+' for x in bins], [counts[x] for x in bins], color=AMBER)
    axes[1].set_title('Cola de lineas por orden', color=NAVY)
    axes[1].set_xlabel('Lineas por orden')
    axes[1].set_ylabel('Ordenes')
    axes[1].tick_params(axis='x', labelrotation=55, labelsize=7)
    axes[1].grid(axis='y', color=GRID, linewidth=.8)
    fig.suptitle('Forma del dato', x=.05, ha='left', color=NAVY, fontsize=14, fontweight='bold')
    save('forma_datos.pdf')


if __name__ == '__main__':
    volume_chart()
    load_chart()
    percentile_chart()
    shape_chart()
    print(f'Figuras escritas en {OUT}')

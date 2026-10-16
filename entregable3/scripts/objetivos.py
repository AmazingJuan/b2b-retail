#!/usr/bin/env python3
"""Tabla de objetivos de mejora del E3: objetivo = 0,90 x valor reportado en el E2.

Solo usa métricas que el E2 reportó como referencia (p50/p95/p99 por consulta y
escenario frío/caliente, y el tiempo de la ruta transaccional). Con --compare
contrasta un archivo de medición del E3 contra los objetivos conservando la
misma consulta, escenario y métrica.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E2_METRICS = ROOT / 'evidence/e2_referencia/metrics.json'
E2_ROUTE = ROOT / 'evidence/e2_referencia/application-route.json'
FACTOR = 0.90
METRICS = ('p50_ms', 'p95_ms', 'p99_ms')
SCENARIO_NAMES = {'cold': 'frío (proveedor 100000 / CEDI 5)', 'hot': 'caliente (proveedor 1 / CEDI 1)'}


def build_targets():
    e2 = json.loads(E2_METRICS.read_text())
    route = json.loads(E2_ROUTE.read_text())
    rows = []
    for query_id, data in e2['queries'].items():
        for scenario in ('cold', 'hot'):
            for metric in METRICS:
                value = data[scenario][metric]
                rows.append({
                    'carga': query_id, 'escenario': scenario, 'metrica': metric,
                    'valor_e2': value, 'objetivo_e3': round(value * FACTOR, 3),
                })
    rows.append({
        'carga': 'RUTA', 'escenario': '5000 órdenes por ruta transaccional', 'metrica': 'seconds',
        'valor_e2': route['seconds'], 'objetivo_e3': round(route['seconds'] * FACTOR, 3),
    })
    return {'factor': FACTOR, 'fuente': 'evidence/e2_referencia (commit d9d4910, medido 2026-09-27)', 'objetivos': rows}


def value_from(measure, row):
    if row['carga'] == 'RUTA':
        return measure.get('route', {}).get('seconds')
    return measure.get('queries', {}).get(row['carga'], {}).get(row['escenario'], {}).get(row['metrica'])


def to_markdown(targets, measure=None, label='E3'):
    header = '| Consulta/carga | Escenario | Métrica | Valor E2 | Objetivo E3 (≤ 90%) |'
    sep = '|---|---|---|---:|---:|'
    if measure:
        header += f' {label} | Δ vs E2 | ¿Cumple? |'
        sep += '---:|---:|:---:|'
    lines = [header, sep]
    met = total = 0
    for row in targets['objetivos']:
        scenario = SCENARIO_NAMES.get(row['escenario'], row['escenario'])
        line = f"| {row['carga']} | {scenario} | {row['metrica'].replace('_ms', '')} | {row['valor_e2']:.3f} | {row['objetivo_e3']:.3f} |"
        if measure:
            value = value_from(measure, row)
            if value is None:
                line += ' — | — | — |'
            else:
                ok = value <= row['objetivo_e3']
                met += ok
                total += 1
                delta = 100 * (value - row['valor_e2']) / row['valor_e2']
                line += f" {value:.3f} | {delta:+.1f}% | {'✅' if ok else '❌'} |"
        lines.append(line)
    if measure:
        lines.append('')
        lines.append(f'**Cumplen la meta: {met}/{total}**')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'evidence/objetivos')
    parser.add_argument('--compare', type=Path, help='JSON de medición E3 (formato de measure.py, opcional clave route)')
    parser.add_argument('--label', default='E3')
    args = parser.parse_args()
    targets = build_targets()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.compare:
        measure = json.loads(args.compare.read_text())
        name = args.compare.stem
        (args.output_dir / f'comparacion_{name}.md').write_text(to_markdown(targets, measure, args.label), encoding='utf-8')
        print(to_markdown(targets, measure, args.label))
    else:
        (args.output_dir / 'objetivos.json').write_text(json.dumps(targets, indent=2, ensure_ascii=False), encoding='utf-8')
        (args.output_dir / 'objetivos.md').write_text(to_markdown(targets), encoding='utf-8')
        print(to_markdown(targets))


if __name__ == '__main__':
    main()

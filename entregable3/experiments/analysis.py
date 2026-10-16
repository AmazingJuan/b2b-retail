"""Estadísticas por fase, verificación de predicciones y agregación entre repeticiones."""
import statistics
from collections import Counter, defaultdict


def percentile(values, fraction):
    """Mismo método de interpolación lineal que scripts/measure.py del E2."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def phase_stats(rows, start, end, settle_s=0.0):
    """rows: (t_sched, query, scenario, status, error, latency_ms, service_ms). settle_s descarta el transitorio inicial."""
    window = [r for r in rows if start + settle_s <= r[0] < end]
    duration = max(end - start - settle_s, 1e-9)
    if not window:
        return {'requests': 0}
    latencies = [r[5] for r in window]
    ok = [r for r in window if r[3] == 200]
    statuses = Counter(str(r[3]) for r in window)
    kinds = Counter(r[4] for r in window if r[3] != 200)
    per_query = defaultdict(list)
    for r in window:
        per_query[r[1]].append(r[5])
    return {
        'requests': len(window),
        'offered_rps': round(len(window) / duration, 2),
        'throughput_rps': round(len(ok) / duration, 2),
        'error_rate': round(1 - len(ok) / len(window), 4),
        'p50_ms': round(percentile(latencies, .50), 3),
        'p95_ms': round(percentile(latencies, .95), 3),
        'p99_ms': round(percentile(latencies, .99), 3),
        'max_ms': round(max(latencies), 3),
        'mean_ms': round(statistics.mean(latencies), 3),
        'ok_p95_ms': round(percentile([r[5] for r in ok], .95), 3) if ok else None,
        'status': dict(statuses),
        'errors': dict(kinds),
        'per_query_p95_ms': {q: round(percentile(v, .95), 3) for q, v in sorted(per_query.items())},
        'per_query_p50_ms': {q: round(percentile(v, .50), 3) for q, v in sorted(per_query.items())},
    }


def timeline(rows, start, end, bucket_s=1.0):
    buckets = defaultdict(list)
    for r in rows:
        if start <= r[0] < end:
            buckets[int((r[0] - start) // bucket_s)].append(r)
    series = []
    for index in range(int((end - start) // bucket_s) + 1):
        items = buckets.get(index, [])
        lat = [r[5] for r in items]
        series.append({
            't': round(index * bucket_s, 1), 'requests': len(items),
            'ok': sum(1 for r in items if r[3] == 200),
            'errors': sum(1 for r in items if r[3] != 200),
            'p50_ms': round(percentile(lat, .5), 2) if lat else None,
            'p95_ms': round(percentile(lat, .95), 2) if lat else None,
            'p99_ms': round(percentile(lat, .99), 2) if lat else None,
        })
    return series


def recovery_time(series, phase_start_rel, baseline_p95, error_tolerance=0.01, factor=1.5, stable_s=3):
    """Segundos desde phase_start_rel hasta que p95 <= factor*baseline y errores <= tolerancia durante stable_s seguidos."""
    streak = 0
    for point in series:
        if point['t'] < phase_start_rel:
            continue
        healthy = (point['requests'] > 0 and point['p95_ms'] is not None and point['p95_ms'] <= factor * baseline_p95
                   and point['errors'] <= error_tolerance * max(point['requests'], 1))
        streak = streak + 1 if healthy else 0
        if streak >= stable_s:
            return round(point['t'] - phase_start_rel - (stable_s - 1), 1)
    return None


def metric_value(summary, phase, metric):
    data = summary['phases'].get(phase, {})
    if metric.startswith('query_p95:'):
        return data.get('per_query_p95_ms', {}).get(metric.split(':', 1)[1])
    if metric.startswith('query_p50:'):
        return data.get('per_query_p50_ms', {}).get(metric.split(':', 1)[1])
    if metric.startswith('status_share:'):
        code = metric.split(':', 1)[1]
        total = data.get('requests') or 1
        return round(data.get('status', {}).get(code, 0) / total, 4)
    if metric == 'recovery_s':
        return summary.get('recovery', {}).get(phase)
    return data.get(metric)


def check_prediction(summary, check):
    value = metric_value(summary, check['phase'], check['metric'])
    if value is not None and check.get('relative_to'):
        base = metric_value(summary, check['relative_to'], check['metric'])
        if base is None:
            value = None
        elif check.get('mode') == 'ratio':
            value = round(value / base, 3) if base else None
        else:
            value = round(value - base, 3)
    if value is None:
        return {**check, 'observed': None, 'ok': False}
    expect = check['expect']
    if expect == 'between':
        ok = check['low'] <= value <= check['high']
    elif expect == '>=':
        ok = value >= check['value']
    elif expect == '<=':
        ok = value <= check['value']
    else:
        raise ValueError(expect)
    return {**check, 'observed': value, 'ok': ok}


def aggregate(summaries, metrics=('p50_ms', 'p95_ms', 'p99_ms', 'error_rate', 'throughput_rps', 'mean_ms')):
    """Media, desviación y coeficiente de variación entre repeticiones (reproducibilidad)."""
    result = {}
    phases = summaries[0]['phases'].keys()
    for phase in phases:
        result[phase] = {}
        for metric in metrics:
            values = [s['phases'][phase].get(metric) for s in summaries if s['phases'].get(phase, {}).get(metric) is not None]
            if not values:
                continue
            mean = statistics.mean(values)
            sd = statistics.pstdev(values) if len(values) > 1 else 0.0
            result[phase][metric] = {'mean': round(mean, 4), 'sd': round(sd, 4),
                                     'cv_pct': round(100 * sd / mean, 1) if mean else 0.0, 'values': values}
    return result

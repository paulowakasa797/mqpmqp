"""Offline coverage/provenance audit of the bounded Binance comparison dataset."""
from __future__ import annotations

import argparse
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from importlib.util import find_spec
from pathlib import Path

from auto_trading.contracts import digest, load_profile, strategy_hash
from research_acceleration.binance_comparison_data import (
    ArchiveSpec, OUTPUT, ROOT, STEPS, SYMBOLS, immutable_write, ms, sha, validate_archive, verified_csv,
)


def source_file(name: str) -> Path:
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('SOURCE_OUTSIDE_REPOSITORY')
    return path


def comparison_gate(coverage: list[dict], *, historical_quotes: bool, historical_availability: bool,
                    source_bindings_match: bool, timesfm_installed: bool) -> dict:
    reasons = []
    expected = {(s, 'klines', tf) for s in SYMBOLS for tf in STEPS}
    present = {(r['symbol'], r['kind'], r['interval']) for r in coverage}
    if not expected <= present or any(r['missing_rows'] or r['duplicate_timestamps'] for r in coverage):
        reasons.append('ALIGNED_DATA_COVERAGE_INCOMPLETE')
    if not historical_quotes:
        reasons.append('HISTORICAL_EXECUTABLE_BID_ASK_AND_PRICE_LEVEL_DEPTH_MISSING')
    if not historical_availability:
        reasons.append('HISTORICAL_FIRST_AVAILABILITY_NOT_ESTABLISHED')
    if not source_bindings_match:
        reasons.append('CURRENT_STRATEGY_OR_PROFILE_BINDING_MISMATCH')
    groups = []
    for name in ('original', 'simple_trend_filter', 'timesfm_filter'):
        why = list(reasons)
        if name == 'timesfm_filter' and not timesfm_installed:
            why.append('TIMESFM3_RUNTIME_NOT_INSTALLED')
        groups.append({'group': name, 'status': 'BLOCKED' if why else 'ELIGIBILITY_ONLY_NOT_BACKTESTED',
                       'reasons': why, 'backtest_executed': False, 'trades': None,
                       'win_rate': None, 'profit_factor': None, 'net_r': None, 'max_drawdown': None})
    return {'groups': groups, 'profitability_validated': False, 'production_allowed': False}


def audit(manifest_path: Path) -> Path:
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_relative_to(OUTPUT.resolve()):
        raise ValueError('MANIFEST_OUTSIDE_COMPARISON_OUTPUT')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = OUTPUT / 'audits' / run
    target.mkdir(parents=True, exist_ok=False)
    window = ms(manifest['protocol']['capture_start']), ms(manifest['protocol']['end_exclusive'])
    analysis_start = ms(manifest['protocol']['analysis_start'])
    buckets, catalog, legacy_comparisons = defaultdict(list), [], []
    checks = []
    for index, item in enumerate(manifest['objects']):
        if item['status'] != 'VERIFIED':
            continue
        path = source_file(item['path'])
        payload = path.read_bytes()
        source_id = f's{index:04d}'
        if item['kind'] == 'funding':
            envelope = json.loads(payload)
            if sha(envelope['payload'].encode()) != item['sha256'] or envelope['url'] != item['url']:
                raise ValueError('FUNDING_SOURCE_CHANGED')
            catalog.append({'source_id': source_id, 'path': item['path'], 'sha256': sha(payload), 'url': item['url']})
            checks.append({'source_id': source_id, 'ok': True, 'kind': 'funding'})
            continue
        spec = ArchiveSpec(item['kind'], item['symbol'], item['period'], item['interval'])
        if item['url'] != 'https://data.binance.vision/data/futures/um/' + spec.relative:
            raise ValueError('SOURCE_URL_IDENTITY_CONFLICT')
        if item.get('reused_original_cache'):
            checksum = (OUTPUT / 'reverified_checksums' / spec.symbol / (spec.filename + '.CHECKSUM')).read_bytes()
        else:
            checksum = Path(str(path) + '.CHECKSUM').read_bytes()
        quality = validate_archive(spec, payload, checksum, window)
        if quality != item['quality']:
            raise ValueError('MANIFEST_QUALITY_OR_SOURCE_DRIFT:' + spec.relative)
        catalog.append({'source_id': source_id, 'path': item['path'], 'sha256': sha(payload), 'url': item['url']})
        checks.append({'source_id': source_id, 'ok': True, 'kind': spec.kind, 'quality': quality})
        if spec.kind == 'bookDepth':
            # This percentage aggregate is not converted into executable price levels.
            continue
        content = verified_csv(spec, payload, checksum)
        table = list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
        header, rows = table[0], table[1:]
        selected = []
        for source_row, row in enumerate(rows, start=2):
            timestamp = ms(row[0]) if spec.kind == 'metrics' else int(row[0])
            if window[0] <= timestamp < window[1]:
                selected.append((timestamp, row, source_id, source_row))
        buckets[(spec.symbol, spec.kind, spec.interval)].append((header, selected))
        if spec.kind == 'metrics':
            old = ROOT / 'data_lake_v2/raw/position_data_recovery/metrics' / spec.symbol / spec.filename
            if old.exists():
                import zipfile
                with zipfile.ZipFile(old) as z:
                    old_rows = list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode('utf-8-sig'))))[1:]
                a, b = {r[0]: r for r in rows}, {r[0]: r for r in old_rows}
                common = a.keys() & b.keys()
                def same_values(left, right):
                    return left[1] == right[1] and all(Decimal(x) == Decimal(y) for x, y in zip(left[2:], right[2:]))
                changed = sum(not same_values(a[t], b[t]) for t in common)
                legacy_comparisons.append({'symbol': spec.symbol, 'day': spec.period, 'official_sha256': sha(payload),
                    'legacy_sha256': sha(old.read_bytes()), 'byte_identical': sha(payload) == sha(old.read_bytes()),
                    'same_timestamp_rows': len(common), 'different_values_at_same_timestamp': changed,
                    'official_only_times': sorted(a.keys() - b.keys()), 'legacy_only_times': sorted(b.keys() - a.keys()),
                    'legacy_input_preserved': True})
    catalog_hash = digest(catalog)
    immutable_write(target / 'source_catalog.json', json.dumps(catalog, indent=2).encode())
    normalized, coverage = [], []
    for (symbol, kind, interval), parts in sorted(buckets.items()):
        header = parts[0][0]
        if any(p[0] != header for p in parts):
            raise ValueError('CROSS_ARCHIVE_SCHEMA_CONFLICT')
        rows = sorted((r for _, selected in parts for r in selected), key=lambda r: r[0])
        timestamps = [r[0] for r in rows]
        duplicate_count = len(timestamps) - len(set(timestamps))
        if duplicate_count:
            raise ValueError('CROSS_ARCHIVE_DUPLICATE_TIMESTAMPS')
        step = 300000 if kind == 'metrics' else STEPS[interval]
        expected = (window[1] - window[0]) // step
        analysis_count = sum(t >= analysis_start for t in timestamps)
        p = target / 'normalized' / f'{symbol}_{kind}_{interval or "5m"}.csv'
        p.parent.mkdir(exist_ok=True)
        with p.open('x', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow([*header, 'source_id', 'source_csv_row', 'historical_available_at'])
            for _, values, source_id, source_row in rows:
                writer.writerow([*values, source_id, source_row, ''])
        normalized.append({'path': str(p.relative_to(ROOT)), 'sha256': sha(p.read_bytes()), 'source_catalog_hash': catalog_hash,
            'ordering': 'derived_copy_sorted_by_original_timestamp; original ZIP bytes unchanged',
            'historical_available_at': None})
        coverage.append({'symbol': symbol, 'kind': kind, 'interval': interval or '5m', 'rows_with_warmup': len(rows),
            'analysis_rows': analysis_count, 'expected_rows_with_warmup': expected,
            'missing_rows': expected - len(rows), 'duplicate_timestamps': duplicate_count,
            'off_grid_rows': sum(t % step != 0 for t in timestamps),
            'first_timestamp': min(timestamps), 'last_timestamp': max(timestamps)})
        print(json.dumps({'normalized': p.name, 'rows': len(rows), 'analysis_rows': analysis_count}), flush=True)
    # A real connector sample and independently downloaded Vision archive must agree.
    probe = json.loads((OUTPUT / 'public_plugin_probe.json').read_text())
    plugin_klines = probe['receipts'][0]['value']['structuredContent']['result']
    kline_path = target / 'normalized/BTCUSDT_klines_5m.csv'
    wanted = {int(row[0]): row for row in plugin_klines}
    matches = []
    with kline_path.open(newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle)
        next(reader)
        for row in reader:
            t = int(row[0])
            if t in wanted:
                matches.append(all(Decimal(str(a)) == Decimal(str(b)) for a, b in zip(row[:12], wanted[t])))
    if len(matches) != len(wanted) or not all(matches):
        raise ValueError('REAL_PLUGIN_AND_ARCHIVE_KLINES_MISMATCH')
    funding_probe = probe['receipts'][1]['value']['structuredContent']['result']
    source_funding = json.loads(json.loads((OUTPUT / 'funding/BTCUSDT.json').read_text())['payload'])
    funding_map = {row['fundingTime']: row for row in source_funding}
    funding_matches = [all(Decimal(str(row[k])) == Decimal(str(funding_map[row['fundingTime']][k]))
                           for k in ('fundingTime', 'fundingRate', 'markPrice')) for row in funding_probe]
    if not all(funding_matches):
        raise ValueError('PLUGIN_AND_DIRECT_FUNDING_MISMATCH')
    bound = manifest['protocol']['strategy_hash'] == strategy_hash() and manifest['protocol']['profile_hash'] == digest(load_profile())
    gate = comparison_gate(coverage, historical_quotes=False, historical_availability=False,
                          source_bindings_match=bound, timesfm_installed=find_spec('timesfm3') is not None)
    result = {'manifest': str(manifest_path.relative_to(ROOT)), 'manifest_sha256': sha(manifest_path.read_bytes()),
              'status': 'PARTIAL_DATA_ACQUIRED_COMPARISON_BLOCKED', 'coverage': coverage,
              'object_statuses': dict(Counter(r['status'] for r in manifest['objects'])), 'revalidated_objects': len(checks),
              'checks': checks, 'normalized_files': normalized, 'legacy_cache_comparison': legacy_comparisons,
              'plugin_klines_vs_archive': {'rows': len(matches), 'all_match': all(matches)},
              'plugin_funding_vs_direct_rest': {'rows': len(funding_matches), 'all_match': all(funding_matches)},
              'source_bindings_match': bound, 'comparison': gate, 'production_allowed': False}
    immutable_write(target / 'audit.json', json.dumps(result, indent=2).encode())
    print(json.dumps({'audit': str(target / 'audit.json'), 'objects_rechecked': len(checks),
                      'comparison': gate, 'production_allowed': False}), flush=True)
    return target / 'audit.json'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    audit(args.manifest)
    return 2  # Collection/audit completed; the complete execution dataset is absent.


if __name__ == '__main__':
    raise SystemExit(main())

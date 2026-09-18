"""Bounded public Binance archive acquisition; never creates trading snapshots.

Raw archive times remain source times. Historical first availability is unknown.
The acquired evidence cannot qualify the execution or Research Gate by itself.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import math
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'reports/auto_trading_comparison_v1'
ARCHIVE = 'https://data.binance.vision/data/futures/um/'
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
STEPS = {'1m': 60000, '5m': 300000, '15m': 900000, '1h': 3600000}
KINDS = ('klines', 'markPriceKlines', 'indexPriceKlines', 'premiumIndexKlines', 'metrics', 'bookDepth', 'bookTicker')
KLINE_HEADER = ('open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'count', 'taker_buy_volume', 'taker_buy_quote_volume', 'ignore')
METRICS_HEADER = (
    'create_time',
    'symbol',
    'sum_open_interest',
    'sum_open_interest_value',
    'count_toptrader_long_short_ratio',
    'sum_toptrader_long_short_ratio',
    'count_long_short_ratio',
    'sum_taker_long_short_vol_ratio',
)


def ms(value: str) -> int:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return int(instant.timestamp() * 1000)


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError('EXISTING_ARTIFACT_CONFLICT:' + str(path))
        return
    # The run is single-process and each source path belongs to one job.
    # Interrupted partial files are retained and refused on resume, never replaced.
    with path.open('xb') as handle:
        handle.write(payload)


@dataclass(frozen=True)
class ArchiveSpec:
    kind: str
    symbol: str
    period: str
    interval: str = ''

    def __post_init__(self) -> None:
        if self.kind not in KINDS or self.symbol not in SYMBOLS:
            raise ValueError('INVALID_ARCHIVE_IDENTITY')
        if not re.fullmatch(r'2026-\d{2}(?:-\d{2})?', self.period):
            raise ValueError('INVALID_PERIOD')
        date.fromisoformat(self.period + '-01' if len(self.period) == 7 else self.period)
        if self.kind.endswith('Klines') or self.kind == 'klines':
            if self.interval not in STEPS:
                raise ValueError('INVALID_INTERVAL')
        elif self.interval:
            raise ValueError('UNEXPECTED_INTERVAL')

    @property
    def filename(self) -> str:
        return f'{self.symbol}-{self.interval or self.kind}-{self.period}.zip'

    @property
    def relative(self) -> str:
        frequency = 'monthly' if len(self.period) == 7 else 'daily'
        middle = f'{self.interval}/' if self.interval else ''
        return f'{frequency}/{self.kind}/{self.symbol}/{middle}{self.filename}'

    @property
    def bounds(self) -> tuple[int, int]:
        begin = date.fromisoformat(self.period + '-01' if len(self.period) == 7 else self.period)
        days = calendar.monthrange(begin.year, begin.month)[1] if len(self.period) == 7 else 1
        return ms(begin.isoformat()), ms((begin + timedelta(days=days)).isoformat())


class BudgetExceeded(RuntimeError):
    pass


class PublicReader:
    def __init__(self, *, max_requests: int, max_bytes: int, seconds: float, clock=time.monotonic):
        if not 1 <= max_requests <= 1200 or not 1 <= max_bytes <= 512 * 2**20 or not 0 < seconds <= 1800:
            raise ValueError('INVALID_BUDGET')
        self.clock, self.deadline = clock, clock() + seconds
        self.max_requests, self.max_bytes = max_requests, max_bytes
        self.requests = self.bytes = 0
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.audit: list[dict] = []

    def get(self, url: str, maximum: int) -> tuple[bytes, dict]:
        parsed = urllib.parse.urlsplit(url)
        valid_archive = url.startswith(ARCHIVE) and parsed.hostname == 'data.binance.vision' and not parsed.query
        valid_rest = parsed.hostname == 'fapi.binance.com' and parsed.path in {'/fapi/v1/fundingRate', '/fapi/v1/time'}
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port or parsed.fragment or not (valid_archive or valid_rest):
            raise ValueError('PUBLIC_ENDPOINT_NOT_ALLOWED')
        with self.lock:
            if self.cancelled.is_set() or self.requests >= self.max_requests or self.bytes >= self.max_bytes or self.clock() >= self.deadline:
                raise BudgetExceeded('PUBLIC_ACQUISITION_BUDGET_EXHAUSTED')
            self.requests += 1
        receipt = {'url': url, 'method': 'GET', 'sent_at': datetime.now(timezone.utc).isoformat()}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('PUBLIC_REDIRECT_REFUSED')
        try:
            opener = urllib.request.build_opener(NoRedirect())
            req = urllib.request.Request(url, headers={'User-Agent': 'BinancePublicResearch/1.0', 'Accept-Encoding': 'identity'})
            timeout = max(0.01, min(8, self.deadline - self.clock()))
            with opener.open(req, timeout=timeout) as response:
                receipt.update(http_status=response.status, server_date=response.headers.get('Date'))
                chunks, size = [], 0
                while True:
                    if response.fp is None:
                        break
                    if self.clock() >= self.deadline or self.cancelled.is_set():
                        raise BudgetExceeded('PUBLIC_ACQUISITION_DEADLINE')
                    # Reserve read capacity so simultaneous streams cannot overshoot.
                    with self.lock:
                        capacity = min(65536, maximum - size + 1, self.max_bytes - self.bytes)
                        if capacity <= 0:
                            raise BudgetExceeded('PUBLIC_BYTE_BUDGET_EXHAUSTED')
                        self.bytes += capacity
                    try:
                        # read1 performs at most one buffered/raw read. A drip feed
                        # must return control to the absolute-deadline check.
                        sock = response.fp.raw._sock
                        sock.settimeout(max(0.01, min(8, self.deadline - self.clock())))
                        part = response.read1(capacity)
                    except BaseException:
                        # Capacity stays conservatively charged when partial reads are unknown.
                        raise
                    with self.lock:
                        self.bytes -= capacity - len(part)
                    size += len(part)
                    if size > maximum:
                        raise ValueError('PUBLIC_OBJECT_TOO_LARGE')
                    if not part:
                        break
                    chunks.append(part)
            payload = b''.join(chunks)
            receipt.update(bytes=len(payload), sha256=sha(payload))
            return payload, receipt
        except urllib.error.HTTPError as exc:
            receipt.update(http_status=exc.code, error='HTTP_ERROR')
            if exc.code in {418, 429}:
                self.cancelled.set()
            raise
        except Exception as exc:
            receipt.update(error=type(exc).__name__, detail=str(exc)[:240])
            raise
        finally:
            receipt['received_at'] = datetime.now(timezone.utc).isoformat()
            with self.lock:
                self.audit.append(receipt)


def verified_csv(spec: ArchiveSpec, payload: bytes, checksum: bytes) -> bytes:
    pattern = re.fullmatch(r'([0-9a-f]{64})\s+\*?([^\s]+)', checksum.decode('ascii').strip())
    if not pattern or pattern[2] != spec.filename or pattern[1] != sha(payload):
        raise ValueError('ARCHIVE_CHECKSUM_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        expected = spec.filename[:-4] + '.csv'
        if len(members) != 1 or members[0].filename != expected:
            raise ValueError('ARCHIVE_MEMBER_MISMATCH')
        member = members[0]
        if member.is_dir() or member.flag_bits & 1 or not 0 < member.file_size <= 96 * 2**20:
            raise ValueError('ARCHIVE_MEMBER_UNSAFE')
        return archive.read(member)  # ZipFile also checks CRC.


def validate_archive(spec: ArchiveSpec, payload: bytes, checksum: bytes, window: tuple[int, int]) -> dict:
    content = verified_csv(spec, payload, checksum)
    lower, upper = spec.bounds
    common = {'sha256': sha(payload), 'csv_sha256': sha(content), 'available_at': None,
              'availability_status': 'HISTORICAL_FIRST_AVAILABILITY_UNKNOWN', 'production_allowed': False}
    if spec.kind == 'metrics':
        # Official ZIPs can contain unsorted source rows. Preserve their bytes and
        # audit order explicitly; the strict legacy parser and its gates stay intact.
        table = csv.reader(io.StringIO(content.decode('utf-8-sig')))
        if tuple(next(table)) != METRICS_HEADER:
            raise ValueError('METRICS_SCHEMA_MISMATCH')
        timestamps, missing_values = [], 0
        for row in table:
            if len(row) != 8 or row[1] != spec.symbol:
                raise ValueError('METRICS_IDENTITY_SCHEMA_MISMATCH')
            timestamp = ms(row[0])
            if not lower <= timestamp < upper:
                raise ValueError('METRICS_OUT_OF_SOURCE_DAY')
            for value in row[2:]:
                if not value:
                    missing_values += 1
                elif not math.isfinite(float(value)) or float(value) < 0:
                    raise ValueError('METRICS_VALUE_INVALID')
            timestamps.append(timestamp)
        if not timestamps:
            raise ValueError('EMPTY_ARCHIVE')
        duplicates = len(timestamps) - len(set(timestamps))
        if duplicates:
            raise ValueError('METRICS_DUPLICATE_SOURCE_TIMESTAMP')
        return {**common, 'rows': len(timestamps), 'selected_rows': sum(window[0] <= t < window[1] for t in timestamps),
                'first_timestamp': min(timestamps), 'last_timestamp': max(timestamps),
                'off_grid_rows': sum(t % 300000 != 0 for t in timestamps), 'schema': 'metrics_8_fields',
                'source_order_nonmonotonic': sum(a >= b for a, b in zip(timestamps, timestamps[1:])),
                'missing_values': missing_values, 'duplicate_timestamps': duplicates,
                'expected_source_rows': (upper - lower) // 300000,
                'raw_source_order_preserved': True, 'trading_snapshot_eligible': False}
    table = csv.reader(io.StringIO(content.decode('utf-8-sig')))
    header = next(table)
    if spec.kind == 'bookDepth':
        if header != ['timestamp', 'percentage', 'depth', 'notional']:
            raise ValueError('DEPTH_SCHEMA_MISMATCH')
        previous, count, selected = None, 0, 0
        seen_levels: set[float] = set()
        first, last = None, None
        for row in table:
            if len(row) != 4:
                raise ValueError('DEPTH_ROW_SCHEMA')
            timestamp = ms(row[0])
            values = [float(v) for v in row[1:]]
            if not all(math.isfinite(v) for v in values) or values[1] < 0 or values[2] < 0 or not -100 <= values[0] <= 100:
                raise ValueError('DEPTH_VALUE_INVALID')
            if not lower <= timestamp < upper or (previous is not None and timestamp < previous):
                raise ValueError('DEPTH_TIMESTAMP_INVALID')
            if timestamp != previous:
                seen_levels = set()
            if values[0] in seen_levels:
                raise ValueError('DUPLICATE_DEPTH_LEVEL')
            seen_levels.add(values[0])
            first = timestamp if first is None else first
            last = previous = timestamp
            count += 1
            selected += window[0] <= timestamp < window[1]
        if count == 0:
            raise ValueError('EMPTY_ARCHIVE')
        return {**common, 'rows': count, 'selected_rows': selected, 'first_timestamp': first, 'last_timestamp': last,
                'schema': 'percentage_depth_statistics', 'executable_bid_ask': False, 'price_level_order_book': False}
    if spec.kind == 'bookTicker':
        # New coverage requires a separately verified schema; never infer it from depth.
        raise ValueError('BOOK_TICKER_SCHEMA_REQUIRES_REVIEW')
    if tuple(header) != KLINE_HEADER:
        raise ValueError('KLINE_SCHEMA_MISMATCH')
    step = STEPS[spec.interval]
    previous, first, count, selected, gaps = None, None, 0, 0, 0
    for row in table:
        if len(row) != 12:
            raise ValueError('KLINE_REQUIRED_FIELD_MISSING')
        t, closed, trades = int(row[0]), int(row[6]), int(row[8])
        o, h, low, c, v, q, buy, buy_q = [float(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
        if not all(math.isfinite(x) for x in (o, h, low, c, v, q, buy, buy_q)):
            raise ValueError('KLINE_NONFINITE')
        if low > min(o, c) or h < max(o, c) or h < low or min(v, q, buy, buy_q, trades) < 0:
            raise ValueError('KLINE_OHLC_OR_VOLUME_INVALID')
        if spec.kind != 'premiumIndexKlines' and min(o, h, low, c) <= 0:
            raise ValueError('KLINE_PRICE_INVALID')
        if buy > v + 1e-8 or buy_q > q + 1e-6:
            raise ValueError('KLINE_TAKER_VOLUME_INVALID')
        if t % step or closed != t + step - 1 or not lower <= t < upper:
            raise ValueError('KLINE_TIMESTAMP_INVALID')
        if previous is not None:
            if t <= previous:
                raise ValueError('KLINE_DUPLICATE_OR_UNSORTED')
            gaps += (t - previous) // step - 1
        first = t if first is None else first
        previous = t
        count += 1
        selected += window[0] <= t < window[1]
    if not count:
        raise ValueError('EMPTY_ARCHIVE')
    expected_selected = max(0, (min(upper, window[1]) - max(lower, window[0])) // step)
    return {**common, 'rows': count, 'selected_rows': selected, 'expected_selected_rows': expected_selected,
            'selected_missing_rows': expected_selected - selected, 'internal_missing_rows': gaps,
            'first_timestamp': first, 'last_timestamp': previous, 'schema': 'binance_futures_12_fields'}


def plan() -> list[ArchiveSpec]:
    specs = []
    for symbol in SYMBOLS:
        for period in ('2026-05', '2026-06', '2026-07-01'):
            for interval in STEPS:
                specs.append(ArchiveSpec('klines', symbol, period, interval))
            for kind, interval in [('markPriceKlines', '1m'), ('indexPriceKlines', '1m'), ('premiumIndexKlines', '5m')]:
                specs.append(ArchiveSpec(kind, symbol, period, interval))
            specs.append(ArchiveSpec('bookTicker', symbol, period))
        for offset in range(49):
            day = (date(2026, 5, 14) + timedelta(days=offset)).isoformat()
            specs.extend([ArchiveSpec('metrics', symbol, day), ArchiveSpec('bookDepth', symbol, day)])
    return specs


def acquire(spec: ArchiveSpec, reader: PublicReader, output: Path, window: tuple[int, int]) -> dict:
    item = {'kind': spec.kind, 'symbol': spec.symbol, 'period': spec.period, 'interval': spec.interval,
            'url': ARCHIVE + spec.relative, 'status': 'NOT_RUN'}
    path = output / 'raw' / spec.relative
    old = ROOT / 'data_lake_v2/raw/position_data_recovery/metrics' / spec.symbol / spec.filename
    local = path if path.is_file() else old if spec.kind == 'metrics' and old.is_file() else path
    cached_receipt = path.with_suffix('.receipt.json')
    try:
        if path.is_file() and cached_receipt.is_file():
            check_path = Path(str(path) + '.CHECKSUM')
            checksum = check_path.read_bytes()
            receipt = json.loads(cached_receipt.read_text(encoding='utf-8'))
            if receipt.get('url') != item['url'] or receipt.get('checksum_sha256') != sha(checksum):
                raise ValueError('CACHED_SOURCE_BINDING_MISMATCH')
            item['transport'] = 'previous_verified_public_download'
        else:
            checksum, _ = reader.get(item['url'] + '.CHECKSUM', 4096)
            item['transport'] = 'official_archive_https'
        payload = local.read_bytes() if local.is_file() else None
        if payload is not None and local == old:
            item['legacy_cache_sha256'] = sha(payload)
            # A differently packaged/modified legacy cache is never represented as
            # the current official original. Keep it untouched and acquire anew.
            expected = checksum.decode('ascii').strip().split()[0]
            if sha(payload) != expected:
                item['legacy_cache_status'] = 'DOES_NOT_MATCH_CURRENT_OFFICIAL_CHECKSUM'
                local, payload = path, None
            else:
                item['legacy_cache_status'] = 'CURRENT_OFFICIAL_CHECKSUM_MATCH'
        if payload is None:
            payload, _ = reader.get(item['url'], 16 * 2**20)
        verified_csv(spec, payload, checksum)
        if local == path:
            immutable_write(path, payload)
            immutable_write(Path(str(path) + '.CHECKSUM'), checksum)
            if not cached_receipt.exists():
                immutable_write(cached_receipt, json.dumps({'url': item['url'], 'sha256': sha(payload),
                    'checksum_sha256': sha(checksum), 'downloaded_at': datetime.now(timezone.utc).isoformat()}, indent=2).encode())
        else:
            # Reuse original metrics bytes, but authenticate against a current public checksum.
            immutable_write(output / 'reverified_checksums' / spec.symbol / (spec.filename + '.CHECKSUM'), checksum)
        quality = validate_archive(spec, payload, checksum, window)
        item.update(status='VERIFIED', path=str(local.relative_to(ROOT)), reused_original_cache=local == old, quality=quality)
    except urllib.error.HTTPError as exc:
        item.update(status='SOURCE_NOT_FOUND' if exc.code == 404 else 'HTTP_ERROR', http_status=exc.code)
    except Exception as exc:
        item.update(status='BLOCKED', error=type(exc).__name__, reason=str(exc)[:300])
    return item


def funding(reader: PublicReader, output: Path, symbol: str, window: tuple[int, int]) -> dict:
    params = {'symbol': symbol, 'startTime': window[0], 'endTime': window[1] - 1, 'limit': 1000}
    url = 'https://fapi.binance.com/fapi/v1/fundingRate?' + urllib.parse.urlencode(params)
    path = output / 'funding' / (symbol + '.json')
    item = {'kind': 'funding', 'symbol': symbol, 'url': url}
    try:
        if path.exists():
            envelope = json.loads(path.read_text(encoding='utf-8'))
            payload = envelope['payload'].encode()
            if envelope['url'] != url or sha(payload) != envelope['sha256']:
                raise ValueError('FUNDING_BINDING_MISMATCH')
        else:
            payload, receipt = reader.get(url, 2 * 2**20)
            envelope = {'payload': payload.decode(), 'url': url, 'sha256': sha(payload), 'receipt': receipt,
                        'available_at': None, 'production_allowed': False}
        rows = json.loads(payload)
        previous = 0
        if not isinstance(rows, list) or not rows or len(rows) >= 1000:
            raise ValueError('FUNDING_EMPTY_OR_PAGINATION_REQUIRED')
        for row in rows:
            t = row['fundingTime']
            if row['symbol'] != symbol or not isinstance(t, int) or not window[0] <= t < window[1] or t <= previous:
                raise ValueError('FUNDING_IDENTITY_TIME_INVALID')
            if not math.isfinite(float(row['fundingRate'])) or not math.isfinite(float(row['markPrice'])) or float(row['markPrice']) <= 0:
                raise ValueError('FUNDING_VALUES_INVALID')
            previous = t
        immutable_write(path, json.dumps(envelope, indent=2).encode())
        item.update(status='VERIFIED', rows=len(rows), sha256=sha(payload), path=str(path.relative_to(ROOT)),
                    first_timestamp=rows[0]['fundingTime'], last_timestamp=rows[-1]['fundingTime'], available_at=None)
    except Exception as exc:
        item.update(status='BLOCKED', error=type(exc).__name__, reason=str(exc)[:240])
    return item


def collect(*, max_requests=900, max_mib=256, seconds=900, workers=3) -> Path:
    if not 1 <= workers <= 4:
        raise ValueError('WORKER_LIMIT')
    reader = PublicReader(max_requests=max_requests, max_bytes=max_mib * 2**20, seconds=seconds)
    window = ms('2026-05-14'), ms('2026-07-02')
    run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = OUTPUT / 'runs' / run
    folder.mkdir(parents=True, exist_ok=False)
    from auto_trading.contracts import digest, load_profile, strategy_hash
    frozen = {'strategy_hash': strategy_hash(), 'profile_hash': digest(load_profile()),
              'collector_sha256': sha(Path(__file__).read_bytes()), 'symbols': list(SYMBOLS),
              'capture_start': '2026-05-14', 'analysis_start': '2026-05-18', 'end_exclusive': '2026-07-02',
              'production_allowed': False, 'group_names': ['original', 'simple_trend_filter', 'timesfm_filter'],
              'historical_first_availability': 'UNKNOWN', 'max_requests': max_requests, 'max_mib': max_mib, 'seconds': seconds}
    immutable_write(folder / 'protocol.json', json.dumps(frozen, indent=2).encode())
    specs, results = plan(), []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        try:
            jobs = [pool.submit(acquire, spec, reader, OUTPUT, window) for spec in specs]
            for future in as_completed(jobs):
                result = future.result()
                results.append(result)
                with (folder / 'progress.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(result) + '\n')
                if len(results) % 25 == 0 or result['status'] not in {'VERIFIED', 'SOURCE_NOT_FOUND'}:
                    print(json.dumps({'completed': len(results), 'total': len(specs), 'requests': reader.requests,
                                      'mib': round(reader.bytes / 2**20, 2), 'last_status': result['status'],
                                      'last_source': result['url'], 'reason': result.get('reason')}), flush=True)
        except BaseException:
            # Signal cancellation before shutdown waits for active workers.
            reader.cancelled.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        results.extend(funding(reader, OUTPUT, s, window) for s in SYMBOLS)
    finally:
        immutable_write(folder / 'http_audit.json', json.dumps(reader.audit, indent=2).encode())
        manifest = {'protocol': frozen, 'objects': sorted(results, key=lambda x: x['url']),
                    'http_requests': reader.requests, 'network_bytes': reader.bytes, 'production_allowed': False}
        immutable_write(folder / 'manifest.json', json.dumps(manifest, indent=2).encode())
    print(json.dumps({'manifest': str(folder / 'manifest.json'), 'verified': sum(r['status'] == 'VERIFIED' for r in results),
                      'source_not_found': sum(r['status'] == 'SOURCE_NOT_FOUND' for r in results),
                      'blocked': sum(r['status'] == 'BLOCKED' for r in results), 'requests': reader.requests}), flush=True)
    return folder / 'manifest.json'


def supervise(command: list[str], seconds: float) -> dict:
    """Hard wall-clock guard, including DNS/headers/chunked stream stalls.

    Only this newly created child is terminated. All network threads belong to
    that child; no external process, account, service, or user session is touched.
    """
    if not 0 < seconds <= 1800:
        raise ValueError('INVALID_SUPERVISOR_DEADLINE')
    started = time.monotonic()
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    child = subprocess.Popen(command, creationflags=flags, stdout=sys.stdout, stderr=sys.stderr)
    result = {'pid': child.pid, 'deadline_seconds': seconds, 'production_allowed': False}
    try:
        result.update(status='CHILD_EXITED', exit_code=child.wait(timeout=seconds))
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        result.update(status='HARD_DEADLINE_TERMINATED' if isinstance(exc, subprocess.TimeoutExpired) else 'USER_INTERRUPTED',
                      exit_code=124 if isinstance(exc, subprocess.TimeoutExpired) else 130)
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        result.update(child_reaped=child.poll() is not None, elapsed_seconds=time.monotonic() - started)
    return result


def repair_reference_day(parent_manifest: Path, day: str, *, max_requests: int,
                         max_mib: int, seconds: float, workers: int) -> Path:
    parent_manifest = parent_manifest.resolve()
    if not parent_manifest.is_relative_to(OUTPUT.resolve()) or not 1 <= workers <= 4:
        raise ValueError('INVALID_REPAIR_SCOPE')
    parent_bytes = parent_manifest.read_bytes()
    parent = json.loads(parent_bytes)
    window = ms(parent['protocol']['capture_start']), ms(parent['protocol']['end_exclusive'])
    if not window[0] <= ms(day) < window[1] or len(day) != 10:
        raise ValueError('REPAIR_OUTSIDE_FROZEN_WINDOW')
    specs = [ArchiveSpec(kind, symbol, day, interval) for symbol in SYMBOLS
             for kind, interval in [('markPriceKlines', '1m'), ('indexPriceKlines', '1m'), ('premiumIndexKlines', '5m')]]
    reader = PublicReader(max_requests=max_requests, max_bytes=max_mib * 2**20, seconds=seconds)
    target = OUTPUT / 'runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-repair')
    target.mkdir(parents=True, exist_ok=False)
    results = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(acquire, spec, reader, OUTPUT, window) for spec in specs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({'source': result['url'], 'status': result['status'], 'reason': result.get('reason')}), flush=True)
    except BaseException:
        reader.cancelled.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        immutable_write(target / 'http_audit.json', json.dumps(reader.audit, indent=2).encode())
    result = {**parent, 'objects': sorted(parent['objects'] + results, key=lambda r: r['url']),
              'parent_manifest': str(parent_manifest.relative_to(ROOT)), 'parent_manifest_sha256': sha(parent_bytes),
              'repair_day': day, 'repair_collector_sha256': sha(Path(__file__).read_bytes()),
              'repair_http_requests': reader.requests, 'repair_network_bytes': reader.bytes}
    path = target / 'manifest.json'
    immutable_write(path, json.dumps(result, indent=2).encode())
    print(json.dumps({'manifest': str(path), 'repair_objects': len(results)}), flush=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-requests', type=int, default=900)
    parser.add_argument('--max-mib', type=int, default=256)
    parser.add_argument('--seconds', type=float, default=900)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--acquire-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--repair-reference-day')
    parser.add_argument('--parent-manifest', type=Path)
    args = parser.parse_args()
    options = vars(args)
    child_mode = options.pop('acquire_child')
    repair_day, parent_manifest = options.pop('repair_reference_day'), options.pop('parent_manifest')
    if bool(repair_day) != bool(parent_manifest):
        parser.error('repair day and parent manifest must be supplied together')
    if not child_mode:
        # The public CLI always supervises the complete child lifetime. Even a
        # resolver or HTTP metadata read that never yields can be terminated.
        command = [sys.executable, '-u', '-m', 'research_acceleration.binance_comparison_data',
                   *sys.argv[1:], '--acquire-child']
        result = supervise(command, args.seconds)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        immutable_write(OUTPUT / 'supervision' / (stamp + '.json'), json.dumps(result, indent=2).encode())
        print(json.dumps(result), flush=True)
        return result['exit_code']
    path = repair_reference_day(parent_manifest, repair_day, **options) if repair_day else collect(**options)
    report = json.loads(path.read_text())
    return 0 if all(r['status'] == 'VERIFIED' for r in report['objects']) else 2


if __name__ == '__main__':
    raise SystemExit(main())

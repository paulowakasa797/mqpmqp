from __future__ import annotations

import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from research_acceleration.binance_comparison_data import (
    ArchiveSpec, BudgetExceeded, KLINE_HEADER, PublicReader, acquire, immutable_write,
    ms, plan, sha, validate_archive, verified_csv,
)


class BinanceComparisonDataTests(unittest.TestCase):
    def setUp(self):
        self.spec = ArchiveSpec('klines', 'BTCUSDT', '2026-05-18', '5m')
        self.start = ms('2026-05-18')
        self.row = [self.start, 100, 102, 99, 101, 10, self.start + 299999, 1000, 4, 6, 600, 0]

    def archive(self, rows=None, header=KLINE_HEADER, member=None):
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows([self.row] if rows is None else rows)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(member or self.spec.filename[:-4] + '.csv', stream.getvalue())
        payload = buffer.getvalue()
        checksum = f'{sha(payload)}  {self.spec.filename}\n'.encode()
        return payload, checksum

    def validate(self, rows=None):
        payload, checksum = self.archive(rows)
        return validate_archive(self.spec, payload, checksum, (self.start, self.start + 86400000))

    def test_real_source_times_are_not_promoted_to_historical_availability(self):
        result = self.validate()
        self.assertIsNone(result['available_at'])
        self.assertEqual(result['availability_status'], 'HISTORICAL_FIRST_AVAILABILITY_UNKNOWN')
        self.assertFalse(result['production_allowed'])

    def test_checksum_mismatch_rejects_before_parsing(self):
        payload, checksum = self.archive()
        with self.assertRaisesRegex(ValueError, 'CHECKSUM_MISMATCH'):
            verified_csv(self.spec, payload + b'bad', checksum)

    def test_checksum_must_bind_exact_filename(self):
        payload, checksum = self.archive()
        with self.assertRaisesRegex(ValueError, 'CHECKSUM_MISMATCH'):
            verified_csv(self.spec, payload, checksum.replace(b'BTCUSDT', b'ETHUSDT'))

    def test_zip_traversal_member_is_never_extracted(self):
        payload, checksum = self.archive(member='../outside.csv')
        with self.assertRaisesRegex(ValueError, 'MEMBER_MISMATCH'):
            verified_csv(self.spec, payload, checksum)

    def test_missing_row_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'REQUIRED_FIELD_MISSING'):
            self.validate([self.row[:-1]])

    def test_invalid_ohlc_is_rejected(self):
        row = self.row.copy()
        row[2] = 98
        with self.assertRaisesRegex(ValueError, 'OHLC_OR_VOLUME_INVALID'):
            self.validate([row])

    def test_partial_candle_and_microsecond_time_are_rejected(self):
        for close in (self.start + 299998, (self.start + 299999) * 1000):
            with self.subTest(close=close):
                row = self.row.copy()
                row[6] = close
                with self.assertRaisesRegex(ValueError, 'TIMESTAMP_INVALID'):
                    self.validate([row])

    def test_duplicate_rows_cannot_inflate_coverage(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_OR_UNSORTED'):
            self.validate([self.row, self.row])

    def test_gaps_are_reported_without_fabrication(self):
        row = self.row.copy()
        row[0] += 600000
        row[6] += 600000
        result = self.validate([self.row, row])
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['internal_missing_rows'], 1)
        self.assertEqual(result['selected_missing_rows'], 286)

    def test_negative_premium_is_valid_but_negative_trade_price_is_not(self):
        row = self.row.copy()
        row[1:5] = [-0.0002, 0.0001, -0.0003, -0.0001]
        payload, checksum = self.archive([row])
        spec = ArchiveSpec('premiumIndexKlines', 'BTCUSDT', '2026-05-18', '5m')
        result = validate_archive(spec, payload, checksum, (self.start, self.start + 86400000))
        self.assertEqual(result['rows'], 1)
        with self.assertRaisesRegex(ValueError, 'PRICE_INVALID'):
            self.validate([row])

    def test_finite_values_and_taker_volume_bounds(self):
        for field, value, reason in [(4, float('nan'), 'NONFINITE'), (9, 11, 'TAKER_VOLUME_INVALID')]:
            with self.subTest(field=field):
                row = self.row.copy()
                row[field] = value
                with self.assertRaisesRegex(ValueError, reason):
                    self.validate([row])

    def test_local_artifact_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive.zip'
            immutable_write(path, b'original')
            immutable_write(path, b'original')
            with self.assertRaisesRegex(ValueError, 'ARTIFACT_CONFLICT'):
                immutable_write(path, b'different')
            self.assertEqual(path.read_bytes(), b'original')

    def test_budget_and_private_endpoint_refused_without_network(self):
        reader = PublicReader(max_requests=1, max_bytes=100, seconds=1, clock=lambda: 1)
        with patch('urllib.request.build_opener', side_effect=AssertionError('NETWORK_CALLED')):
            with self.assertRaisesRegex(ValueError, 'ENDPOINT_NOT_ALLOWED'):
                reader.get('https://fapi.binance.com/fapi/v2/account', 100)
            reader.requests = 1
            with self.assertRaises(BudgetExceeded):
                reader.get('https://fapi.binance.com/fapi/v1/time', 100)

    def test_fixed_plan_is_unique_and_contains_existing_metrics_window(self):
        specs = plan()
        self.assertEqual(len({s.relative for s in specs}), len(specs))
        self.assertEqual(sum(s.kind == 'metrics' for s in specs), 147)
        self.assertEqual(sum(s.kind == 'bookDepth' for s in specs), 147)
        self.assertIn(ArchiveSpec('metrics', 'BTCUSDT', '2026-07-01'), specs)

    def metrics_archive(self):
        from position_data_recovery_gate import METRICS_HEADER
        self.spec = ArchiveSpec('metrics', 'BTCUSDT', '2026-05-18')
        rows = [['2026-05-18 00:05:00', 'BTCUSDT', 100, 10000, 1, 1, 1, 1],
                ['2026-05-18 00:00:00', 'BTCUSDT', 101, 10100, 1, 1, 1, 1]]
        return self.archive(rows, header=METRICS_HEADER)

    def test_official_metrics_raw_order_is_audited_without_rewriting(self):
        payload, checksum = self.metrics_archive()
        original_sha = sha(payload)
        result = validate_archive(self.spec, payload, checksum, (self.start, self.start + 86400000))
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['source_order_nonmonotonic'], 1)
        self.assertEqual(result['first_timestamp'], self.start)
        self.assertIsNone(result['available_at'])
        self.assertEqual(sha(payload), original_sha)

    def test_nonmatching_legacy_cache_downloads_new_public_copy_and_preserves_old(self):
        payload, checksum = self.metrics_archive()
        class Reader:
            def get(self, url, maximum):
                return (checksum if url.endswith('CHECKSUM') else payload), {'url': url}
        with tempfile.TemporaryDirectory() as tmp, patch('research_acceleration.binance_comparison_data.ROOT', Path(tmp)):
            root = Path(tmp)
            old = root / 'data_lake_v2/raw/position_data_recovery/metrics/BTCUSDT' / self.spec.filename
            old.parent.mkdir(parents=True)
            old.write_bytes(b'legacy-user-bytes')
            result = acquire(self.spec, Reader(), root / 'out', (self.start, self.start + 86400000))
            self.assertEqual(result['status'], 'VERIFIED')
            self.assertEqual(old.read_bytes(), b'legacy-user-bytes')
            self.assertEqual(result['legacy_cache_status'], 'DOES_NOT_MATCH_CURRENT_OFFICIAL_CHECKSUM')
            self.assertFalse(result['reused_original_cache'])
            self.assertEqual((root / result['path']).read_bytes(), payload)

    def test_timezone_offset_preserves_the_actual_instant(self):
        self.assertEqual(ms('2026-05-18T00:00:00+08:00'), ms('2026-05-17T16:00:00+00:00'))

    def test_slow_stream_rechecks_absolute_deadline_between_single_reads(self):
        from types import SimpleNamespace
        now = [0.0]
        timeouts = []
        class Response:
            status = 200
            headers = {}
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append)))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count):
                raise AssertionError('MULTI_RECEIVE_READ_BYPASSES_DEADLINE')
            def read1(self, count):
                now[0] += 0.4
                return b'x'
        opener = SimpleNamespace(open=lambda *a, **kw: Response())
        reader = PublicReader(max_requests=1, max_bytes=1000, seconds=1, clock=lambda: now[0])
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(BudgetExceeded):
                reader.get('https://fapi.binance.com/fapi/v1/time', 100)
        self.assertEqual(reader.bytes, 3)
        self.assertTrue(timeouts and timeouts[-1] <= 0.21)

    def test_interrupt_cancels_reader_before_pool_shutdown(self):
        import research_acceleration.binance_comparison_data as module
        seen = []
        class Reader:
            def __init__(self, **kwargs):
                import threading
                self.cancelled = threading.Event()
                self.audit, self.requests, self.bytes = [], 0, 0
                seen.append(self)
        class Pool:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): self.shutdown()
            def submit(self, *args): return object()
            def shutdown(self, *args, **kwargs):
                self_cancelled = seen[0].cancelled.is_set()
                if not self_cancelled:
                    raise AssertionError('POOL_WAITED_BEFORE_CANCELLATION')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(module, 'OUTPUT', Path(tmp)), \
             patch.object(module, 'PublicReader', Reader), \
             patch.object(module, 'ThreadPoolExecutor', Pool), \
             patch.object(module, 'as_completed', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                module.collect()

    def test_hard_supervisor_terminates_and_reaps_a_stalled_child(self):
        import sys
        from research_acceleration.binance_comparison_data import supervise
        result = supervise([sys.executable, '-c', 'import time; time.sleep(30)'], 0.2)
        self.assertEqual(result['status'], 'HARD_DEADLINE_TERMINATED')
        self.assertEqual(result['exit_code'], 124)
        self.assertTrue(result['child_reaped'])
        self.assertLess(result['elapsed_seconds'], 5)

    def test_missing_execution_data_blocks_all_three_groups_without_zero_pnl(self):
        from research_acceleration.binance_comparison_audit import comparison_gate
        coverage = [{'symbol': symbol, 'kind': 'klines', 'interval': interval,
                     'missing_rows': 0, 'duplicate_timestamps': 0}
                    for symbol in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT') for interval in ('1m', '5m', '15m', '1h')]
        result = comparison_gate(coverage, historical_quotes=False, historical_availability=False,
                                 source_bindings_match=True, timesfm_installed=False)
        self.assertEqual(len(result['groups']), 3)
        for group in result['groups']:
            self.assertEqual(group['status'], 'BLOCKED')
            self.assertFalse(group['backtest_executed'])
            self.assertIsNone(group['net_r'])
        self.assertFalse(result['production_allowed'])


if __name__ == '__main__':
    unittest.main()

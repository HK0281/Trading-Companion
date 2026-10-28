"""Offline fixed-delay Tencent batches: structural overlap, never live quote performance."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
from statistics import median
import sys
from threading import Event, Lock
from time import perf_counter
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.collection_execution import CollectionExecutor
from trading_companion.screening import ScreeningCondition, ScreeningRequest, ScreeningService, market_stratum
from trading_companion.screening_source import MARKET_FILTERS, TENCENT_ENDPOINT, TencentQuoteSnapshotSource
from trading_companion.security_directory import Security, SecurityDirectory


NOW = datetime(2026, 10, 27, 4, tzinfo=timezone.utc)


def artificial_directory():
    groups = (("600", "SH"), ("000", "SZ"), ("688", "SH"), ("300", "SZ"), ("920", "BJ"))
    return SecurityDirectory(tuple(Security(f"{prefix}{index:03d}.{exchange}", f"人工{prefix}{index}")
        for prefix, exchange in groups for index in range(400)), "offline_artificial_benchmark", NOW)


class ArtificialResponse:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def iter_content(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]

    def close(self):
        pass


class ArtificialTransport:
    def __init__(self, trusted, delay):
        self.identities = {item.instrument_id[-2:].lower() + item.instrument_id[:6]: item
                           for item in trusted.securities}
        self.delay, self.lock = delay, Lock()
        self.calls, self.response_bytes, self.active, self.maximum = [], 0, 0, 0

    def factory(self):
        owner = self
        class Client:
            def request(self, method, url, **kwargs):
                symbols = tuple(url.removeprefix(TENCENT_ENDPOINT).split(","))
                lines = []
                for symbol in symbols:
                    security = owner.identities[symbol]
                    fields = [""] * 88
                    fields[0], fields[1], fields[2] = "1", security.name, security.instrument_id[:6]
                    for index, value in {3: "10", 30: "20261027120000", 32: "1", 38: "2", 44: "3",
                                         45: "4", 46: "1", 52: "10", 57: "100", 82: "CNY"}.items():
                        fields[index] = value
                    lines.append('v_' + symbol + '="' + "~".join(fields) + '";')
                body = "\n".join(lines).encode("gb18030")
                with owner.lock:
                    owner.calls.append(symbols)
                    owner.response_bytes += len(body)
                    owner.active += 1
                    owner.maximum = max(owner.maximum, owner.active)
                try:
                    Event().wait(owner.delay)
                    return ArtificialResponse(body)
                finally:
                    with owner.lock:
                        owner.active -= 1

            def close(self):
                pass
        return Client()


def benchmark(coverage, batch_size, repeat, delay_ms):
    trusted = artificial_directory()
    selected_count = 50 if coverage == "standard" else 200
    tasks = 5 * ceil(selected_count / batch_size)
    series, expected, expected_calls, expected_bytes = {}, None, None, None
    pool = CollectionExecutor()
    try:
        for label, width in (("serial", 1), ("parallel_3c", 3)):
            times, maxima, requests, sizes = [], [], [], []
            for _ in range(repeat):
                http = ArtificialTransport(trusted, delay_ms / 1000)
                source = TencentQuoteSnapshotSource(session_factory=http.factory, executor=pool, max_workers=width,
                    clock=lambda: NOW, monotonic_clock=lambda: 0.0, coverage=coverage, batch_size=batch_size)
                start = perf_counter()
                snapshot = source.read_snapshot(trusted)
                times.append(round((perf_counter() - start) * 1000, 3))
                result = ScreeningService(clock=lambda: NOW).screen(snapshot,
                    ScreeningRequest((ScreeningCondition("price", "gt", 0),))).to_dict()
                actual = (snapshot, result)
                calls = sorted(http.calls)
                if expected is None:
                    expected, expected_calls, expected_bytes = actual, calls, http.response_bytes
                if (actual != expected or calls != expected_calls or http.response_bytes != expected_bytes
                        or len(http.calls) != tasks or http.maximum > width):
                    raise ValueError("Artificial snapshots, coverage, identities, requests or bytes differ")
                maxima.append(http.maximum)
                requests.append(len(http.calls))
                sizes.append(http.response_bytes)
            series[label] = {"workers": width, "theoretical_wait_batches": ceil(tasks / width),
                "elapsed_ms": times, "median_ms": median(times), "maximum_inflight": maxima,
                "requests": requests, "response_bytes": sizes}
    finally:
        pool.close(wait=True)
    summaries = {}
    for stratum in MARKET_FILTERS:
        ids = [item.instrument_id for item in expected[0].quotes if market_stratum(item.instrument_id) == stratum]
        summaries[stratum] = {"count": len(ids), "first": ids[0], "last": ids[-1],
                             "sha256": sha256("\n".join(ids).encode("ascii")).hexdigest()}
    return {"benchmark": "Tencent_screening_fixed_delay", "data_mode": "offline_artificial_http_gb18030",
        "interpretation": "structural_overlap_not_live_source_performance", "coverage": coverage,
        "batch_size": batch_size, "repeat": repeat, "delay_ms_per_request": delay_ms,
        "task_descriptions": tasks, "pool_unfinished_limit": 3, "selected_ids": summaries,
        "series": series, "snapshots_results_coverage_requests_bytes_equal": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", choices=("standard", "expanded"), default="standard")
    parser.add_argument("--batch-size", type=int, choices=range(50, 81), default=80)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--delay-ms", type=int, default=100)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 20 or not 1 <= args.delay_ms <= 1000:
        parser.error("Choose repeat 1..20 and delay-ms 1..1000")
    def denied(*args, **kwargs):
        raise AssertionError("Benchmark forbids network and local environment reads")
    with patch("socket.socket.connect", side_effect=denied), patch("socket.socket.connect_ex", side_effect=denied), patch(
        "socket.getaddrinfo", side_effect=denied), patch("requests.Session.request", side_effect=denied), patch(
        "trading_companion.local_env._read_local_values", side_effect=denied):
        report = benchmark(args.coverage, args.batch_size, args.repeat, args.delay_ms)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

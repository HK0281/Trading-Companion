"""Fixed-delay artificial HTTP responses; structural overlap, never live source performance."""

import argparse
from datetime import datetime
import json
from math import ceil
from pathlib import Path
from statistics import median
import sys
import re
from threading import Event, Lock
from time import perf_counter
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.collection_execution import CollectionExecutor
from trading_companion.company_research import ResearchDataService, _ENDPOINTS
from trading_companion.company_research_contracts import RESEARCH_KINDS, ResearchDataRequest, SHANGHAI


NOW = datetime(2026, 10, 25, 12, tzinfo=SHANGHAI)
COMPANIES = (("300308.SZ", "中际旭创"), ("000001.SZ", "平安银行"), ("600000.SH", "浦发银行"))
PAYLOADS = {
    "profile": {"success": True, "result": {"data": [{"SECUCODE": "300308.SZ", "ORG_NAME": "人工基准公司",
                 "MAIN_BUSINESS": "离线人工业务"}]}},
    "financial": {"success": True, "result": {"data": [{"SECUCODE": "300308.SZ", "REPORT_DATE": "2026-06-30",
        "NOTICE_DATE": "2026-08-22", "CURRENCY": "CNY", "TOTALOPERATEREVE": 123000000,
        "PARENTNETPROFIT": 4500000, "TOTALOPERATEREVETZ": 10, "PARENTNETPROFITTZ": 5, "ROEJQ": 8}]}},
    "news": {"code": 0, "result": {"cmsArticleWebOld": []}},
    "announcements": {"success": True, "data": {"list": []}},
}


class ArtificialResponse:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def iter_content(self, size):
        yield self.body

    def close(self):
        pass


class ArtificialTransport:
    def __init__(self, delay):
        self.delay, self.lock = delay, Lock()
        self.requests = self.response_bytes = self.active = self.maximum = 0

    def client(self):
        owner = self
        class Client:
            def request(self, method, url, **kwargs):
                kind = next(key for key, (_, endpoint) in _ENDPOINTS.items() if endpoint == url)
                symbol = re.search(r'SECUCODE="([0-9]{6}\.(?:SH|SZ))"', kwargs["params"].get("filter", ""))
                # Only profile/financial contain issuer-bound rows; empty document lists carry no company data.
                payload = json.dumps(PAYLOADS[kind], ensure_ascii=False)
                if symbol is not None:
                    payload = payload.replace("300308.SZ", symbol[1])
                body = payload.encode("utf-8")
                with owner.lock:
                    owner.requests += 1
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


def benchmark(repeat, delay_ms, companies=1, kinds="all"):
    requests = tuple(ResearchDataRequest(symbol, name, NOW, allow_current_background=True)
                     for symbol, name in COMPANIES[:companies])
    selected = ("financial",) if kinds == "financial" else RESEARCH_KINDS
    descriptions = len(requests) * len(selected)
    pool, expected, series = CollectionExecutor(), None, {}
    try:
        for label, workers, batches in (("serial", 1, descriptions),
                                        ("company_parallel_3a", 3, companies * ceil(len(selected) / 3)),
                                        ("batch_parallel_3b", 3, ceil(descriptions / 3))):
            times, inflight, counts, sizes = [], [], [], []
            for _ in range(repeat):
                transport = ArtificialTransport(delay_ms / 1000)
                service = ResearchDataService(session_factory=transport.client, max_workers=workers,
                    executor=pool, clock=lambda: NOW, news_provider="eastmoney")
                started = perf_counter()
                result = tuple(batch.to_dict() for batch in (
                    tuple(service.collect(request, selected) for request in requests)
                    if label == "company_parallel_3a" else service.collect_many(requests, selected)))
                times.append(round((perf_counter() - started) * 1000, 3))
                if expected is None:
                    expected = result
                if result != expected or not all(batch["items"] for batch in result):
                    raise ValueError("Artificial collection results differ or contain no evidence")
                inflight.append(transport.maximum)
                counts.append(transport.requests)
                sizes.append(transport.response_bytes)
            series[label] = {"workers": workers, "theoretical_wait_batches": batches,
                             "elapsed_ms": times, "median_ms": median(times),
                             "maximum_inflight": inflight, "requests": counts, "response_bytes": sizes}
    finally:
        pool.close(wait=True)
    return {"benchmark": "company_candidates_fixed_delay", "data_mode": "offline_artificial_http",
            "interpretation": "structural_overlap_not_live_source_performance", "repeat": repeat,
            "delay_ms_per_request": delay_ms, "companies": companies, "kinds": list(selected),
            "task_descriptions": descriptions, "pool_unfinished_limit": 3, "series": series,
            "serial_over_3b": round(series["serial"]["median_ms"] / series["batch_parallel_3b"]["median_ms"], 4),
            "3a_over_3b": round(series["company_parallel_3a"]["median_ms"] / series["batch_parallel_3b"]["median_ms"], 4),
            "results_equal": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--delay-ms", type=int, default=100)
    parser.add_argument("--companies", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--kinds", choices=("financial", "all"), default="all")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 20 or not 1 <= args.delay_ms <= 1000:
        parser.error("Choose repeat 1..20 and delay-ms 1..1000")
    def denied(*args, **kwargs):
        raise AssertionError("Benchmark forbids network and local environment reads")
    with patch("socket.socket.connect", side_effect=denied), patch("socket.socket.connect_ex", side_effect=denied), patch(
        "socket.getaddrinfo", side_effect=denied), patch("requests.Session.request", side_effect=denied), patch(
        "trading_companion.local_env._read_local_values", side_effect=denied):
        report = benchmark(args.repeat, args.delay_ms, args.companies, args.kinds)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

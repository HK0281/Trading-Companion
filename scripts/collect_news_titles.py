"""Preview a fixed public-title collection; --live requires an explicit request budget."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
from pathlib import Path
import sys
from threading import Lock
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import SHANGHAI, aware
from trading_companion.news_triage_shadow import COLLECTION_ROOT, NewsTitleCapture
from trading_companion.product_plan import ProductPlan
from trading_companion.product_research import collect_product_research
from trading_companion.security_directory import Security
from trading_companion.turn_control import TurnStopped


# Frozen pilot universe, not a whole-market sample. Names remain subject to source-page verification.
# Third field denotes the exchange board; fourth documents deliberate sector/name coverage.
COMPANIES = (
    ("600000.SH", "浦发银行", "sh_main", "bank"),
    ("600036.SH", "招商银行", "sh_main", "bank_name_collision"),
    ("600016.SH", "民生银行", "sh_main", "bank_name_collision"),
    ("600030.SH", "中信证券", "sh_main", "broker_name_collision"),
    ("600999.SH", "招商证券", "sh_main", "broker_name_collision"),
    ("601688.SH", "华泰证券", "sh_main", "broker"),
    ("601398.SH", "工商银行", "sh_main", "bank_name_collision"),
    ("601988.SH", "中国银行", "sh_main", "bank_name_collision"),
    ("600009.SH", "上海机场", "sh_main", "transport_name_collision"),
    ("600519.SH", "贵州茅台", "sh_main", "consumer"),
    ("600887.SH", "伊利股份", "sh_main", "consumer"),
    ("600104.SH", "上汽集团", "sh_main", "automotive"),
    ("600585.SH", "海螺水泥", "sh_main", "materials_name_collision"),
    ("600276.SH", "恒瑞医药", "sh_main", "healthcare"),
    ("600050.SH", "中国联通", "sh_main", "telecom_name_collision"),
    ("000001.SZ", "平安银行", "sz_main", "bank_name_collision"),
    ("000039.SZ", "中集集团", "sz_main", "industrial"),
    ("000063.SZ", "中兴通讯", "sz_main", "telecom_name_collision"),
    ("000157.SZ", "中联重科", "sz_main", "industrial"),
    ("000100.SZ", "TCL科技", "sz_main", "electronics"),
    ("000333.SZ", "美的集团", "sz_main", "consumer_name_collision"),
    ("000651.SZ", "格力电器", "sz_main", "consumer"),
    ("000538.SZ", "云南白药", "sz_main", "healthcare_name_collision"),
    ("000568.SZ", "泸州老窖", "sz_main", "consumer"),
    ("002027.SZ", "分众传媒", "sz_main", "media_name_collision"),
    ("002142.SZ", "宁波银行", "sz_main", "bank_name_collision"),
    ("002415.SZ", "海康威视", "sz_main", "electronics"),
    ("002475.SZ", "立讯精密", "sz_main", "electronics"),
    ("002594.SZ", "比亚迪", "sz_main", "automotive"),
    ("000776.SZ", "广发证券", "sz_main", "broker_name_collision"),
    ("300014.SZ", "亿纬锂能", "chinext", "battery"),
    ("300015.SZ", "爱尔眼科", "chinext", "healthcare"),
    ("300033.SZ", "同花顺", "chinext", "finance_name_collision"),
    ("300059.SZ", "东方财富", "chinext", "finance_name_collision"),
    ("300122.SZ", "智飞生物", "chinext", "healthcare"),
    ("300124.SZ", "汇川技术", "chinext", "industrial"),
    ("300274.SZ", "阳光电源", "chinext", "energy_name_collision"),
    ("300308.SZ", "中际旭创", "chinext", "electronics"),
    ("300750.SZ", "宁德时代", "chinext", "battery"),
    ("300760.SZ", "迈瑞医疗", "chinext", "healthcare"),
    ("688008.SH", "澜起科技", "star", "semiconductor"),
    ("688012.SH", "中微公司", "star", "semiconductor_name_collision"),
    ("688036.SH", "传音控股", "star", "electronics"),
    ("688041.SH", "海光信息", "star", "semiconductor_name_collision"),
    ("688082.SH", "盛美上海", "star", "semiconductor"),
    ("688111.SH", "金山办公", "star", "software_name_collision"),
    ("688126.SH", "沪硅产业", "star", "semiconductor"),
    ("688169.SH", "石头科技", "star", "consumer_name_collision"),
    ("688256.SH", "寒武纪", "star", "semiconductor_name_collision"),
    ("688981.SH", "中芯国际", "star", "semiconductor"),
)
_ENDPOINTS = frozenset(("https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php",
                        "https://np-anotice-stock.eastmoney.com/api/security/ann"))
KINDS = ("news", "announcements")


def build_plan(*, selected=None, max_requests=None):
    if max_requests is not None and (type(max_requests) is not int or not 1 <= max_requests <= 100):
        raise ValueError("Use a public-request limit between 1 and 100")
    chosen = set(selected) if selected is not None else {row[0] for row in COMPANIES}
    if not chosen or chosen - {row[0] for row in COMPANIES}:
        raise ValueError("Select only companies from the fixed collection list")
    companies = [{"instrument_id": code, "name": name, "board": board, "coverage": coverage}
                 for code, name, board, coverage in COMPANIES if code in chosen]
    return {"version": "news-title-collection-v1", "status": "planned", "companies": companies,
            "sources": ["sina_company_news", "eastmoney_announcements"],
            "kinds": list(KINDS), "lookback_days": 30, "titles_per_source": 5,
            "planned_network_requests": 2 * len(companies), "network_request_limit": max_requests,
            "max_title_records": 10 * len(companies), "min_request_interval_seconds": 1,
            "max_concurrency": 1, "model_calls": 0,
            "natural_sample_order": "sha256_normalized_title_then_instrument_then_date",
            "annotation_target": {"development": 200, "sealed": 200,
                "natural_per_split": 130, "hard_per_split": 70},
            "output_root": ".agentbus/tmp/news-title-collection",
            "scope": "fixed_pilot_universe_not_market_distribution_or_full_coverage"}


class _LimitedTransport:
    """Charge every outgoing attempt and pace request starts across both fixed sources."""

    def __init__(self, client, limit, *, timer=None, sleeper=None):
        self.client, self.limit, self.used = client, limit, 0
        self._timer, self._sleep = timer or monotonic, sleeper or sleep
        self._last_start = None
        self._lock = Lock()

    def request(self, method, url, **kwargs):
        import requests
        with self._lock:
            if method != "GET" or url not in _ENDPOINTS or kwargs.get("allow_redirects") is not False:
                raise ValueError("Collection allows only the two fixed read-only endpoints")
            if self.used >= self.limit:
                raise requests.RequestException("collection_request_budget_exhausted")
            now = self._timer()
            if self._last_start is not None:
                delay = 1.0 - (now - self._last_start)
                while delay > 0:
                    self._sleep(delay)
                    now = self._timer()
                    delay = 1.0 - (now - self._last_start)
            if not isfinite(now):
                raise ValueError("Use a finite monotonic collection clock")
            self.used += 1
            self._last_start = now
            return self.client.request(method, url, **kwargs)


def collect_titles(plan, *, output_root=COLLECTION_ROOT, clock=None, timer=None, sleeper=None, session_factory=None):
    """Explicit live execution entry; tests inject HTTP fixtures, time and local output."""
    # Revalidate rather than treating a caller-supplied plan as permission to expand collection.
    expected = build_plan(selected=[c["instrument_id"] for c in plan["companies"]],
                          max_requests=plan["network_request_limit"])
    if plan != expected or plan["network_request_limit"] is None:
        raise ValueError("Review a bounded collection plan before live execution")
    import requests
    clock = clock or (lambda: datetime.now(timezone.utc))
    cutoff = clock()
    if not aware(cutoff):
        raise ValueError("Collection cutoff must be timezone-aware")
    capture = NewsTitleCapture(output_root)
    client = (session_factory or requests.Session)()
    transport = _LimitedTransport(client, plan["network_request_limit"], timer=timer, sleeper=sleeper)
    completed = unavailable = 0
    unique = set()
    code = "collection_completed"
    try:
        service = ResearchDataService(session=transport, clock=clock, max_workers=1,
                                      news_provider="sina", announcement_provider="eastmoney")
        for company in plan["companies"]:
            for kind in KINDS:
                if transport.used >= transport.limit:
                    code = "collection_request_budget_exhausted"
                    break
                selected_plan = ProductPlan(status="ready", securities=(Security(company["instrument_id"], company["name"]),),
                    kinds=(kind,), start_date=cutoff.astimezone(SHANGHAI).date() - timedelta(days=30),
                    end_date=cutoff.astimezone(SHANGHAI).date(), as_of=cutoff.astimezone(SHANGHAI).date(),
                    source="tencent", user_question="")
                bundle = collect_product_research(selected_plan, data_service=service, now=cutoff)
                completed += 1
                unavailable += sum(c["status"] == "unavailable" for stock in bundle.companies for c in stock.coverage)
                if not capture.capture(bundle, captured_at=cutoff, origin="collection"):
                    code = capture.status()["stop_code"] or "collection_capture_unavailable"
                    break
                for item in bundle.evidence:
                    if item.content_kind == "title":
                        unique.add((item.instrument_id, item.raw_value, item.date))
            if code != "collection_completed":
                break
    except TurnStopped:
        raise
    except Exception:
        code = "collection_unavailable"
    finally:
        try:
            client.close()
        except Exception:
            code = "collection_transport_close_failed"
    stats = capture.status()
    not_run = plan["planned_network_requests"] - completed
    partial = code != "collection_completed" or unavailable > 0 or not_run > 0
    if code == "collection_completed" and unavailable:
        code = "collection_sources_unavailable"
    return {**plan, "status": "partial" if partial else "completed", "code": code,
            "as_of": cutoff.isoformat(), "network_requests_attempted": transport.used,
            "source_sections_completed": completed, "source_sections_unavailable": unavailable,
            "source_sections_not_run": not_run, "capture": stats,
            "output_directory": str(capture.directory) if capture.directory is not None else None,
            "unique_company_titles": len(unique), "raw_title_shortfall_for_400": max(0, 400 - len(unique)),
            "annotation_status": "not_annotated_or_group_deduplicated",
            "quality_status": "not_evaluated"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--live", action="store_true", help="Fetch public title lists, never models or full articles")
    parser.add_argument("--max-requests", type=int, help="Required for --live; all failed attempts count (1..100)")
    parser.add_argument("--company", action="append", help="Optional qualified identifier from the fixed 50-company list")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(selected=args.company, max_requests=args.max_requests)
        if args.live and args.max_requests is None:
            raise ValueError("--live requires --max-requests")
    except ValueError:
        print(json.dumps({"status": "invalid_request", "code": "invalid_collection_plan", "model_calls": 0}))
        return 2
    # Preview never constructs a transport, reads credentials, or creates an output directory.
    if args.live:
        print(json.dumps(plan, ensure_ascii=False), file=sys.stderr, flush=True)
        result = collect_titles(plan)
    else:
        result = plan
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in ("planned", "completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
# Second approved pilot: distinct issuers, same board quotas, locally verified names.
# Common words, geographic/group names are intentional candidates, not known mismatches.
SECOND_COMPANIES = (
    ("600008.SH", "首创环保", "sh_main", "environment_common_name"),
    ("600010.SH", "包钢股份", "sh_main", "steel_geographic_name"),
    ("600085.SH", "同仁堂", "sh_main", "healthcare_common_name"),
    ("600108.SH", "亚盛集团", "sh_main", "agriculture_group_name"),
    ("600120.SH", "浙江东方", "sh_main", "finance_geographic_name"),
    ("600153.SH", "建发股份", "sh_main", "trade_group_name"),
    ("600170.SH", "上海建工", "sh_main", "construction_geographic_name"),
    ("600177.SH", "雅戈尔", "sh_main", "consumer_group_name"),
    ("600246.SH", "万通发展", "sh_main", "property_common_name"),
    ("600611.SH", "大众交通", "sh_main", "transport_common_name"),
    ("600637.SH", "东方明珠", "sh_main", "media_geographic_name"),
    ("600675.SH", "中华企业", "sh_main", "property_common_name"),
    ("600834.SH", "申通地铁", "sh_main", "transport_common_name"),
    ("601919.SH", "中远海控", "sh_main", "shipping_group_name"),
    ("603288.SH", "海天味业", "sh_main", "consumer_common_name"),
    ("000009.SZ", "中国宝安", "sz_main", "industrial_geographic_name"),
    ("000021.SZ", "深科技", "sz_main", "electronics_common_name"),
    ("000028.SZ", "国药一致", "sz_main", "healthcare_group_name"),
    ("000031.SZ", "大悦城", "sz_main", "property_common_name"),
    ("000090.SZ", "天健集团", "sz_main", "construction_group_name"),
    ("000400.SZ", "许继电气", "sz_main", "industrial_group_name"),
    ("000423.SZ", "东阿阿胶", "sz_main", "healthcare_geographic_name"),
    ("000681.SZ", "视觉中国", "sz_main", "media_common_name"),
    ("000963.SZ", "华东医药", "sz_main", "healthcare_geographic_name"),
    ("002007.SZ", "华兰生物", "sz_main", "healthcare_common_name"),
    ("002008.SZ", "大族激光", "sz_main", "industrial_common_name"),
    ("002129.SZ", "TCL中环", "sz_main", "energy_group_name"),
    ("002230.SZ", "科大讯飞", "sz_main", "software_common_name"),
    ("002271.SZ", "东方雨虹", "sz_main", "materials_common_name"),
    ("002304.SZ", "洋河股份", "sz_main", "consumer_geographic_name"),
    ("300024.SZ", "机器人", "chinext", "industrial_common_word"),
    ("300075.SZ", "数字政通", "chinext", "software_common_name"),
    ("300085.SZ", "银之杰", "chinext", "finance_common_name"),
    ("300118.SZ", "东方日升", "chinext", "energy_common_name"),
    ("300251.SZ", "光线传媒", "chinext", "media_common_name"),
    ("300315.SZ", "掌趣科技", "chinext", "software_common_name"),
    ("300364.SZ", "中文在线", "chinext", "media_common_words"),
    ("300433.SZ", "蓝思科技", "chinext", "electronics_common_name"),
    ("300454.SZ", "深信服", "chinext", "software_common_name"),
    ("300785.SZ", "值得买", "chinext", "consumer_common_words"),
    ("688001.SH", "华兴源创", "star", "industrial_common_name"),
    ("688003.SH", "天准科技", "star", "industrial_common_name"),
    ("688006.SH", "杭可科技", "star", "battery_geographic_name"),
    ("688009.SH", "中国通号", "star", "transport_group_name"),
    ("688016.SH", "心脉医疗", "star", "healthcare_common_name"),
    ("688023.SH", "安恒信息", "star", "software_common_name"),
    ("688088.SH", "虹软科技", "star", "software_common_name"),
    ("688099.SH", "晶晨股份", "star", "semiconductor_common_name"),
    ("688208.SH", "道通科技", "star", "industrial_common_name"),
    ("688343.SH", "云天励飞", "star", "software_common_name"),
)
# Frozen from the trusted local directory, without inspecting captured titles or errors.
# ST is a directory name feature, not a claim of a particular penalty or investigation.
THIRD_COMPANIES = (
    ("600079.SH", "ST人福", "sh_main", "本地ST名称；医药"),
    ("600080.SH", "ST金花", "sh_main", "本地ST名称；医药"),
    ("600082.SH", "ST海泰", "sh_main", "本地ST名称；园区地产"),
    ("600187.SH", "*ST国中", "sh_main", "本地ST名称；环保"),
    ("600568.SH", "ST中珠", "sh_main", "本地ST名称；医药地产"),
    ("600011.SH", "华能国际", "sh_main", "电力；燃料成本周期"),
    ("600029.SH", "南方航空", "sh_main", "航空；运价与油价周期"),
    ("600096.SH", "云天化", "sh_main", "化工；产品价格周期"),
    ("600115.SH", "中国东航", "sh_main", "航空；运价与油价周期"),
    ("600157.SH", "永泰能源", "sh_main", "煤炭电力；能源周期"),
    ("600219.SH", "南山铝业", "sh_main", "有色金属；铝价周期"),
    ("600221.SH", "海航控股", "sh_main", "航空；运价与油价周期"),
    ("600346.SH", "恒力石化", "sh_main", "石化；原料与产品价格周期"),
    ("600348.SH", "华阳股份", "sh_main", "煤炭；能源价格周期"),
    ("600938.SH", "中国海油", "sh_main", "油气；能源价格周期"),
    ("000010.SZ", "*ST美丽", "sz_main", "本地ST名称；园林工程"),
    ("000056.SZ", "*ST皇庭", "sz_main", "本地ST名称；商业地产"),
    ("000078.SZ", "ST海王", "sz_main", "本地ST名称；医药流通"),
    ("000488.SZ", "ST晨鸣", "sz_main", "本地ST名称；造纸周期"),
    ("000609.SZ", "*ST中迪", "sz_main", "本地ST名称；地产"),
    ("002024.SZ", "ST易购", "sz_main", "本地ST名称；零售"),
    ("002122.SZ", "ST汇洲", "sz_main", "本地ST名称；机械"),
    ("001979.SZ", "招商蛇口", "sz_main", "地产；项目结算周期"),
    ("000723.SZ", "美锦能源", "sz_main", "焦化；能源价格周期"),
    ("000792.SZ", "盐湖股份", "sz_main", "钾肥锂盐；产品价格周期"),
    ("000933.SZ", "神火股份", "sz_main", "煤铝；能源金属周期"),
    ("002460.SZ", "赣锋锂业", "sz_main", "锂盐；产品价格周期"),
    ("002466.SZ", "天齐锂业", "sz_main", "锂盐；产品价格周期"),
    ("002470.SZ", "金正大", "sz_main", "化肥；原料产品价格周期"),
    ("002496.SZ", "辉丰股份", "sz_main", "农化；产品价格周期"),
    ("300020.SZ", "ST银江", "chinext", "本地ST名称；软件工程"),
    ("300027.SZ", "ST华谊", "chinext", "本地ST名称；影视项目周期"),
    ("300044.SZ", "*ST赛为", "chinext", "本地ST名称；软件工程"),
    ("300037.SZ", "新宙邦", "chinext", "电解液；电池材料周期"),
    ("300072.SZ", "海新能科", "chinext", "化工能源；产品价格周期"),
    ("300073.SZ", "当升科技", "chinext", "正极材料；电池材料周期"),
    ("300343.SZ", "联创股份", "chinext", "氟化工；产品价格周期"),
    ("300438.SZ", "鹏辉能源", "chinext", "电池储能；产能周期"),
    ("300763.SZ", "锦浪科技", "chinext", "光伏；装机与库存周期"),
    ("300919.SZ", "中伟新材", "chinext", "电池材料；金属价格周期"),
    ("688022.SH", "ST瀚川", "star", "本地ST名称；自动化设备"),
    ("688033.SH", "*ST天宜", "star", "本地ST名称；交通装备"),
    ("688005.SH", "容百科技", "star", "正极材料；电池材料周期"),
    ("688063.SH", "派能科技", "star", "储能；库存与需求周期"),
    ("688180.SH", "君实生物", "star", "创新药；研发投入与商业化周期"),
    ("688223.SH", "晶科能源", "star", "光伏；产能与产品价格周期"),
    ("688303.SH", "大全能源", "star", "硅料；产品价格周期"),
    ("688390.SH", "固德威", "star", "光伏；装机与库存周期"),
    ("688599.SH", "天合光能", "star", "光伏；产能与产品价格周期"),
    ("688707.SH", "振华新材", "star", "电池材料；金属价格周期"),
)
BATCHES = {"first": COMPANIES, "second": SECOND_COMPANIES, "third": THIRD_COMPANIES}
_ENDPOINTS = frozenset(("https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php",
                        "https://np-anotice-stock.eastmoney.com/api/security/ann"))
KINDS = ("news", "announcements")


def build_plan(*, selected=None, max_requests=None, batch="first"):
    if type(batch) is not str or batch not in BATCHES:
        raise ValueError("Select one fixed collection batch")
    universe = BATCHES[batch]
    if max_requests is not None and (type(max_requests) is not int or not 1 <= max_requests <= 100):
        raise ValueError("Use a public-request limit between 1 and 100")
    chosen = set(selected) if selected is not None else {row[0] for row in universe}
    if not chosen or chosen - {row[0] for row in universe}:
        raise ValueError("Select only companies from the fixed collection list")
    companies = [{"instrument_id": code, "name": name, "board": board, "coverage": coverage}
                 for code, name, board, coverage in universe if code in chosen]
    return {"version": "news-title-collection-v2", "batch": batch, "status": "planned", "companies": companies,
            "sources": ["sina_company_news", "eastmoney_announcements"],
            "kinds": list(KINDS), "lookback_days": 30, "titles_per_source": 5,
            "planned_network_requests": 2 * len(companies), "network_request_limit": max_requests,
            "max_title_records": 10 * len(companies), "min_request_interval_seconds": 1,
            "max_concurrency": 1, "model_calls": 0,
            "natural_sample_order": "sha256_normalized_title_then_instrument_then_date",
            "annotation_target": {"development": 200, "sealed": 200,
                "natural_per_split": 130, "hard_per_split": 70,
                "append_new_natural_limit_per_split": 150 if batch == "third" else None},
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
                          max_requests=plan["network_request_limit"], batch=plan["batch"])
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
    parser.add_argument("--batch", choices=tuple(BATCHES), default="first", help="One fixed 50-company batch per run")
    parser.add_argument("--company", action="append", help="Optional qualified identifier from the fixed 50-company list")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(selected=args.company, max_requests=args.max_requests, batch=args.batch)
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

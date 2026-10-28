"""Bounded metadata storage, failure isolation and explicit thread-context propagation."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.tool_model import ToolCall, ToolModelResponse
from trading_companion.turn_trace import (
    JsonlTraceSink, TraceLimits, TurnTrace, _CODES, _EVENT_CODES, trace_message, trace_model_attempt, trace_model_response,
    trace_outcome, trace_phase, trace_provider_request, trace_run_id,
)


PRIVATE = "private-trace-unit-sentinel"


# Exact non-turn codes, reviewed individually. Never exclude a whole module/prefix dynamically.
NON_TURN_CODES = {
    "aligned_research_series": "图表附件成功状态，不是整轮结束原因。",
    "archive_replay_unavailable": "独立归档回放CLI结果，未接入消息追踪。",
    "asset_not_found": "静态资源HTTP错误，不经过消息入口。",
    "at_least_two_candidates_required": "候选比较附件的数量校验状态。",
    "candidate_evidence_ids_not_unique": "候选比较附件的证据ID校验状态。",
    "candidate_metric_missing_or_invalid": "候选比较附件的指标可用性状态。",
    "candidate_metrics_compared": "候选比较附件的完成状态，整轮使用公司研究结果码。",
    "candidate_metrics_not_comparable": "候选比较附件的不可比状态。",
    "candidate_metrics_partially_compared": "候选比较附件的部分可比状态。",
    "candidate_report_periods_differ": "候选比较附件的报告期校验状态。",
    "candidate_units_differ": "候选比较附件的单位校验状态。",
    "chart_context_invalid": "图表附件的输入校验状态。",
    "chart_data_unavailable": "图表附件可选数据缺失，不改变整轮回复。",
    "chart_integrity_mismatch": "图表附件完整性校验失败，不改变整轮回复。",
    "chart_payload_limit": "图表附件的容量限制状态。",
    "checked_edit_base": "已核对的指标编辑基准，仅是执行中间状态。",
    "checked_reference_synthesis": "确定性综合报告附件的检查状态。",
    "condition_update_ready": "条件更新已准备好，之后仍须执行研究。",
    "context_authenticity_mismatch": "市场背景附件的真实性校验状态。",
    "context_cache_limit": "可选市场背景附件的缓存容量状态。",
    "context_cache_miss": "可选市场背景附件的缓存缺失状态。",
    "context_future_snapshot": "市场背景附件的未来时间校验状态。",
    "context_integrity_mismatch": "市场背景附件的完整性校验状态。",
    "context_not_configured": "市场背景附件未配置，不是整轮失败。",
    "context_payload_limit": "市场背景附件的容量限制状态。",
    "context_result_unavailable": "市场背景附件没有可关联的研究结果。",
    "context_unusable": "可选市场背景附件构建失败，保留原回复。",
    "contextual_outlook_ready": "继承展望条件的内部准备状态。",
    "conversation_call_limit": "独立终端会话的调用上限，该入口未开启持久化追踪。",
    "current_background_not_covered": "市场背景附件未覆盖该证券。",
    "current_background_stale": "市场背景附件过期，不是整轮结果。",
    "current_background_unavailable": "市场背景附件来源不可用。",
    "current_market_context_refreshed": "独立市场背景刷新CLI的成功结果。",
    "deterministic_facts": "确定性事实报告附件的成功状态。",
    "directory_refreshed": "独立证券目录刷新CLI的成功结果。",
    "directory_source_error": "独立证券目录刷新CLI的来源错误。",
    "directory_validation_error": "独立证券目录刷新CLI的校验错误。",
    "evaluation_call_limit": "独立评估运行器的调用上限，不是产品轮次结果。",
    "evaluation_fixture_unusable": "独立评估运行器的夹具错误。",
    "evaluation_internal_error": "独立评估运行器的内部错误。",
    "exchange_current_context_available": "公司背景证据领域的来源状态。",
    "exchange_current_context_unavailable": "公司背景证据领域的来源缺失状态。",
    "final_tool_calls": "最终回答阶段仍提议工具的内部failure_code；整轮另报模型或解释不可用。",
    "fixture_unavailable": "独立MVP评估运行器的夹具错误。",
    "historical_long_scenario_ready": "ATR风险情景附件的完成状态。",
    "historical_replay_computed": "历史回放附件的计算状态。",
    "invalid_content_length": "消息服务之前的HTTP长度校验错误。",
    "invalid_evaluation_options": "独立评估CLI参数校验错误。",
    "invalid_known_at": "可选归档的观察时间校验问题。",
    "invalid_refresh_configuration": "独立目录刷新CLI配置错误。",
    "invalid_risk_parameters": "ATR风险情景附件的参数校验问题。",
    "invalid_screening_request": "独立筛选CLI的输入校验错误。",
    "invalid_snapshot_bundle": "可选归档的证据包校验问题。",
    "invalid_snapshot_clock": "可选归档的时钟校验问题。",
    "invalid_snapshot_id": "独立归档读取的ID校验问题。",
    "invalid_snapshot_limit": "独立归档查询的数量限制问题。",
    "invalid_snapshot_path": "可选归档的本地路径校验问题。",
    "invalid_snapshot_payload": "可选归档的序列化内容校验问题。",
    "invalid_snapshot_timestamp": "可选归档的时间格式校验问题。",
    "json_required": "消息服务之前的HTTP内容类型校验错误。",
    "live_refresh_not_enabled": "独立目录刷新CLI未显式开启联网。",
    "market_context_limited": "市场背景附件的部分可用状态。",
    "market_context_refresh_failed": "独立市场背景刷新CLI的错误结果。",
    "metric_display_invalid": "比较附件中单个指标的显示校验问题。",
    "metric_evidence_ambiguous": "比较附件中单个指标的证据歧义。",
    "metric_evidence_available": "比较附件中单个指标的证据成功状态。",
    "metric_evidence_identity_mismatch": "比较附件中单个指标的身份校验问题。",
    "metric_evidence_missing": "比较附件中单个指标的证据缺失状态。",
    "metric_evidence_not_fact": "比较附件中单个指标的非事实证据拒绝。",
    "metric_raw_value_invalid": "比较附件中单个指标的数值校验问题。",
    "metric_report_period_invalid": "比较附件中单个指标的报告期校验问题。",
    "metric_unit_not_percent": "比较附件中单个指标的百分比单位校验问题。",
    "not_started": "结果对象及评估对象的初始状态，不是已结束轮次。",
    "origin_not_allowed": "消息服务之前的HTTP来源拒绝。",
    "product_plan_not_started": "公司计划对象的初始状态，不是已结束轮次。",
    "proposal_complete": "研究提议阶段直接完成的内部failure_code；整轮另报模型不可用。",
    "proposal_incomplete": "提议或独立smoke检查的不完整状态；整轮另报模型不可用。",
    "proposal_refused": "提议或独立smoke检查的拒绝状态；整轮另报模型不可用。",
    "refresh_latest_ready": "刷新条件已准备好，之后仍须执行研究。",
    "relative_range_ready": "相对日期条件已准备好，之后仍须执行研究。",
    "replay_insufficient_history": "历史回放附件的数据不足状态。",
    "report_evidence_mismatch": "确定性事实报告附件的证据校验状态。",
    "reported_market_context": "市场背景附件的成功状态。",
    "request_too_large": "消息服务之前的HTTP请求体容量拒绝。",
    "research_input_ready": "技术请求理解已准备好，随后仍须执行研究。",
    "risk_atr_not_requested": "ATR风险情景附件未请求该指标。",
    "risk_atr_selection_ambiguous": "ATR风险情景附件的指标实例选择歧义。",
    "risk_atr_unavailable": "ATR风险情景附件缺少ATR证据。",
    "risk_calculation_unavailable": "ATR风险情景附件的计算不可用状态。",
    "risk_close_unavailable": "ATR风险情景附件缺少收盘价证据。",
    "risk_evidence_mismatch": "ATR风险情景附件的证据校验问题。",
    "risk_market_data_unavailable": "ATR风险情景附件的行情缺失状态。",
    "risk_nonpositive_reference": "ATR风险情景附件的参考数值非正。",
    "risk_price_grid_unavailable": "ATR风险情景附件缺少价格网格。",
    "same_period_metric_ranked": "候选比较附件中单项指标的排序成功状态。",
    "saved_result_ready": "缓存结果内部核验成功，随后仍须形成轮次答复。",
    "scenario_expectations_failed": "独立评估场景的契约失败，不是产品答复。",
    "scenario_expectations_met": "独立评估场景的契约成功，不是产品答复。",
    "screening_data_validation_error": "独立筛选CLI的数据校验错误。",
    "screening_selection_ready": "名单选择的内部准备状态，随后继续研究。",
    "screening_source_error": "独立筛选CLI的来源错误。",
    "smoke_arguments_mismatch": "独立工具往返smoke CLI的参数校验错误。",
    "snapshot_corrupt": "可选归档内容损坏，不覆盖研究回复结果码。",
    "snapshot_not_found": "独立归档查询未找到快照。",
    "snapshot_schema_mismatch": "可选归档的数据库版本问题。",
    "snapshot_source_time_in_future": "可选归档的来源时间校验问题。",
    "snapshot_store_closed": "可选归档已关闭，不覆盖研究回复结果码。",
    "snapshot_store_unavailable": "可选归档不可用，不覆盖研究回复结果码。",
    "snapshot_too_large": "可选归档的内容容量限制问题。",
    "stopped_after_failure": "独立评估首错停止后的未执行场景状态。",
    "synthesis_data_unavailable": "确定性综合报告附件的数据不足状态。",
    "synthesis_evidence_mismatch": "确定性综合报告附件的证据校验问题。",
    "synthesis_payload_limit": "确定性综合报告附件的容量限制状态。",
    "synthetic_result_not_usable": "独立工具往返smoke CLI的人工结果不可用状态。",
    "technical_insights_computed": "技术派生事实附件的完成状态。",
    "technical_insights_evidence_mismatch": "技术派生事实附件的证据校验问题。",
    "technical_insights_no_data": "技术派生事实附件的缺失状态。",
    "tool_execution_error": "独立工具往返smoke CLI的工具异常结果。",
    "tool_execution_not_usable": "独立工具往返smoke CLI的工具结果拒绝。",
    "tool_round_trip_completed": "独立工具往返smoke CLI的成功结果。",
    "unclassified_failure": "独立评估报告的未分类失败，不是产品结果。",
}


def source_code_literals(sources):
    """Inspect code-bearing declarations/calls without importing or evaluating source modules."""
    trees = {path: ast.parse(source) for path, source in sources.items()}
    slots, codes = {}, {}

    def parameters(function):
        args = function.args.posonlyargs + function.args.args
        return args[1:] if args and args[0].arg in ("self", "cls") else args

    def add_slot(name, index):
        if name in slots and slots[name] != index:
            raise AssertionError(f"Code argument moved or has conflicting signatures: {name}")
        slots[name] = index

    def literals(expression):
        if isinstance(expression, ast.Constant) and type(expression.value) is str:
            yield expression.value
        elif isinstance(expression, ast.IfExp):
            yield from literals(expression.body)
            yield from literals(expression.orelse)
        elif isinstance(expression, ast.BoolOp):
            for value in expression.values:
                yield from literals(value)
        elif isinstance(expression, ast.Dict):
            for value in expression.values:
                yield from literals(value)
        elif isinstance(expression, ast.Subscript):
            yield from literals(expression.value)
        elif isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute) and expression.func.attr == "get":
            yield from literals(expression.func.value)
            if len(expression.args) > 1:
                yield from literals(expression.args[1])
        elif isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            left = tuple(literals(expression.left))
            if isinstance(expression.right, ast.Attribute) and expression.right.attr == "status":
                # Existing branch guards constrain these ToolModelResponse status combinations.
                statuses = {"general_knowledge_": ("refused", "incomplete"),
                            "proposal_": ("complete", "refused", "incomplete"),
                            "final_": ("tool_calls", "refused", "incomplete")}
                if len(left) != 1 or left[0] not in statuses:
                    raise AssertionError("Unreviewed status-derived code expression")
                right = statuses[left[0]]
            else:
                right = tuple(literals(expression.right))
            for prefix in left:
                for suffix in right:
                    yield prefix + suffix

    def collect(path, node, expression):
        for code in literals(expression):
            codes.setdefault(code, set()).add(f"{path}:{node.lineno}")

    # Discover code argument positions for dataclasses, exceptions and forwarding helpers.
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                fields = [item.target.id for item in node.body
                          if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)]
                if "code" in fields:
                    add_slot(node.name, fields.index("code"))
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        names = [arg.arg for arg in parameters(item)]
                        if "code" in names:
                            add_slot(node.name, names.index("code"))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "__init__":
                names = [arg.arg for arg in parameters(node)]
                if "code" in names:
                    add_slot(node.name, names.index("code"))

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in ("code", "stop_code", "failure_code"):
                        collect(path, node, keyword.value)
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                if name in slots and len(node.args) > slots[name]:
                    collect(path, node, node.args[slots[name]])
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any((target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else "")
                       .lstrip("_") in ("code", "stop_code", "failure_code") for target in targets):
                    collect(path, node, node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.posonlyargs + node.args.args
                for arg, default in zip(args[len(args) - len(node.args.defaults):], node.args.defaults):
                    if arg.arg in ("code", "stop_code", "failure_code"):
                        collect(path, node, default)
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                    if arg.arg in ("code", "stop_code", "failure_code"):
                        collect(path, node, default)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in ("code", "stop_code", "failure_code"):
                        collect(path, node, value)
            elif (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2
                  and isinstance(node.value.elts[0], ast.Constant) and type(node.value.elts[0].value) is str
                  and isinstance(node.value.elts[1], ast.Constant) and type(node.value.elts[1].value) is str):
                # Existing invalid_reason() helpers return (fixed_code, public_message).
                # Trace metadata helpers explicitly return (fixed_status, fixed_code).
                index = 1 if node.value.elts[0].value in ("ok", "limited", "rejected", "failed", "stopped", "skipped") else 0
                collect(path, node, node.value.elts[index])
    return codes


class TraceCodeVocabularyTests(unittest.TestCase):
    def test_all_source_codes_are_registered_or_explicitly_excluded(self):
        root = Path(__file__).resolve().parents[1]
        sources = {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                   for path in (root / "src/trading_companion").rglob("*.py")}
        codes = source_code_literals(sources)
        self.assertTrue(codes, "No source codes were discovered")
        missing = codes.keys() - _CODES - _EVENT_CODES - NON_TURN_CODES.keys()
        self.assertFalse(missing, "Unreviewed trace result codes:\n" + "\n".join(
            f"{code}: {', '.join(sorted(codes[code]))}" for code in sorted(missing)))
        self.assertFalse(NON_TURN_CODES.keys() - codes.keys(), "Remove obsolete non-turn exclusions")
        self.assertFalse(NON_TURN_CODES.keys() & (_CODES | _EVENT_CODES), "A code cannot be both registered and excluded")
        for code, reason in NON_TURN_CODES.items():
            with self.subTest(code=code):
                self.assertTrue(reason.strip(), "Each exclusion needs its own reviewed reason")

    def test_scanner_catches_new_codes_across_public_construction_forms(self):
        source = '''
class Result:
    status: str
    code: str
class ApiProblem:
    def __init__(self, status, code, message): pass
def finish(result, status, code, message): pass
def issue(code="new_default_issue"): pass
def example(flag):
    Result("failed", "new_positional_issue")
    Result(status="failed", code="new_keyword_issue")
    ApiProblem(503, "new_api_issue", "message_must_not_be_a_code")
    finish(None, "failed", "new_finish_issue", "message_must_not_be_a_code")
    code = "new_left_issue" if flag else "new_right_issue"
    return {"code": "new_projection_issue"}
def invalid_reason():
    return "new_tuple_issue", "message_must_not_be_a_code"
'''
        found = source_code_literals({"fixture.py": source})
        expected = {"new_default_issue", "new_positional_issue", "new_keyword_issue", "new_api_issue",
                    "new_finish_issue", "new_left_issue", "new_right_issue", "new_projection_issue", "new_tuple_issue"}
        self.assertEqual(found.keys(), expected)
        self.assertEqual(found.keys() - _CODES - NON_TURN_CODES.keys(), expected)
        with self.assertRaisesRegex(AssertionError, "Unreviewed status-derived"):
            source_code_literals({"new.py": 'code = "new_prefix_" + response.status'})


def read_records(directory):
    return [json.loads(line) for path in sorted(Path(directory).glob("turns*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


class TraceStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "traces"
        self.sink = JsonlTraceSink(self.directory)

    def scope(self, sink=None):
        return trace_message(self.sink if sink is None else sink, mode="demo", provider="fake", history=False)

    def attempt(self, usage=None):
        with trace_model_attempt():
            trace_model_response(ToolModelResponse("complete", PRIVATE, usage=usage,
                                                  requested_model=PRIVATE, model=PRIVATE))

    def test_configuration_is_bounded_and_disabled_tracing_creates_nothing(self):
        for values in ({"max_events": 65}, {"max_events": True}, {"record_bytes": 65537},
                       {"record_bytes": 2047}, {"file_bytes": 6 * 1024 * 1024}, {"file_count": 5},
                       {"record_bytes": 4096, "file_bytes": 2048}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                TraceLimits(**values)
        self.assertFalse(self.directory.exists())
        with trace_message(None, mode="normal", provider="fake", history=False):
            self.attempt()
            self.assertIsNone(trace_run_id())
        self.assertFalse(self.directory.exists())

    def test_schema_missing_usage_and_untrusted_model_metadata(self):
        with self.scope():
            self.attempt()
        record = read_records(self.directory)[0]
        self.assertEqual(record["schema_version"], "trace-v2")
        self.assertEqual(record["models"]["attempted"], 1)
        self.assertEqual(record["models"]["provider_requests_started"], 0)
        self.assertEqual(record["models"]["reported_usage"], dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens")))
        self.assertFalse(record["models"]["usage_complete_for_attempts"])
        self.assertEqual(record["models"]["events"][0]["requested_model"], "fake-tools-v1")
        self.assertNotIn(PRIVATE, json.dumps(record))

    def test_unknown_exception_codes_and_raw_messages_never_enter_files(self):
        with self.assertRaises(ModelCallError), self.scope():
            with trace_model_attempt():
                raise ModelCallError(PRIVATE, PRIVATE)
        record = read_records(self.directory)[0]
        self.assertEqual(record["outcome"]["code"], "other")
        self.assertEqual(record["models"]["events"][0]["error_code"], "other")
        self.assertNotIn(PRIVATE, json.dumps(record))

    def test_partial_and_inconsistent_usage_are_known_parts_with_incomplete_flag(self):
        with self.scope():
            self.attempt(TokenUsage(10, None, None))
            self.attempt(TokenUsage(2, 3, 99))
            self.attempt(TokenUsage(4, 1, 5))
        models = read_records(self.directory)[0]["models"]
        self.assertEqual(models["reported_usage"], {"prompt_tokens": 16, "completion_tokens": 4, "total_tokens": 104})
        self.assertEqual(models["responses_with_complete_usage"], 1)
        self.assertFalse(models["usage_complete_for_attempts"])

    def test_distinct_private_surfaces_are_excluded_by_field_allowlists(self):
        from trading_companion.agent import AgentTurnResult
        markers = {key: "private-" + key + "-trace-sentinel" for key in
                   ("question", "draft", "answer", "arguments", "response_model", "request", "step")}
        with self.scope():
            with trace_model_attempt():
                trace_model_response(ToolModelResponse("tool_calls", markers["answer"],
                    tool_calls=(ToolCall("untrusted-call-id", "untrusted_tool", {"account": markers["arguments"]}),),
                    provider="deepseek", model=markers["response_model"], is_mock=False))
            trace_outcome(AgentTurnResult(status="completed", code="research_completed",
                user_question=markers["question"], model_draft=markers["draft"],
                workflow_trace={"stop_reason": "answered", "tool_calls_used": 1, "steps": [markers["step"]],
                                "request_id": markers["request"]}))
        encoded = (self.directory / "turns.jsonl").read_text(encoding="utf-8")
        for marker in (*markers.values(), "untrusted-call-id", "untrusted_tool", "account"):
            self.assertNotIn(marker, encoded)
        self.assertEqual(read_records(self.directory)[0]["workflow"], {"stop_reason": "answered", "tool_calls_used": 1})

    def test_event_and_serialized_byte_limits_keep_aggregates_and_terminal_record(self):
        for max_events in (0, 1, 64):
            path = self.directory / str(max_events)
            sink = JsonlTraceSink(path, limits=TraceLimits(max_events=max_events, record_bytes=2048))
            with self.scope(sink):
                for _ in range(100):
                    self.attempt(TokenUsage(10, 5, 15))
            record = read_records(path)[0]
            self.assertEqual(record["models"]["attempted"], 100)
            self.assertEqual(record["models"]["responses_received"], 100)
            self.assertEqual(record["models"]["reported_usage"]["total_tokens"], 1500)
            self.assertTrue(record["truncated"])
            self.assertEqual(record["dropped_events"], 100 - len(record["models"]["events"]))
            self.assertLessEqual(len(record["models"]["events"]), max_events)
            self.assertLessEqual((path / "turns.jsonl").stat().st_size, 2048)
            self.assertIn("outcome", record)

    def test_rotation_enforces_actual_file_count_bytes_and_preserves_unrelated_files(self):
        self.directory.mkdir()
        unrelated = self.directory / "keep.txt"
        unrelated.write_text(PRIVATE, encoding="utf-8")
        sink = JsonlTraceSink(self.directory, limits=TraceLimits(record_bytes=2048, file_bytes=4096, file_count=3))
        for _ in range(40):
            with self.scope(sink):
                self.attempt()
        files = list(self.directory.glob("turns*.jsonl"))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 4096 for path in files))
        self.assertLessEqual(sum(path.stat().st_size for path in files), 3 * 4096)
        records = read_records(self.directory)
        self.assertLess(len(records), 40)
        self.assertEqual(len({item["run_id"] for item in records}), len(records))
        self.assertEqual(unrelated.read_text(encoding="utf-8"), PRIVATE)

    def test_single_retained_file_rotates_without_oversize_or_backups(self):
        sink = JsonlTraceSink(self.directory, limits=TraceLimits(record_bytes=2048, file_bytes=2048, file_count=1))
        for _ in range(6):
            with self.scope(sink):
                self.attempt()
        self.assertEqual(len(list(self.directory.glob("turns*.jsonl"))), 1)
        self.assertLessEqual((self.directory / "turns.jsonl").stat().st_size, 2048)
        self.assertEqual(len(read_records(self.directory)), 1)

    def test_concurrent_writers_produce_complete_separate_lines(self):
        def work(_):
            with self.scope():
                self.attempt(TokenUsage(2, 1, 3))
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(work, range(32)))
        records = read_records(self.directory)
        self.assertEqual(len(records), 32)
        self.assertEqual(len({item["run_id"] for item in records}), 32)
        self.assertTrue(all(item["models"]["attempted"] == 1 for item in records))

    def test_threads_need_individually_copied_contexts(self):
        with self.scope(), ThreadPoolExecutor(max_workers=2) as executor:
            executor.submit(self.attempt).result()  # ContextVar is absent in a fresh worker.
            futures = [executor.submit(copy_context().run, self.attempt) for _ in range(2)]
            for future in futures:
                future.result()
        self.assertEqual(read_records(self.directory)[0]["models"]["attempted"], 2)

    def test_nested_disabled_scope_does_not_inherit_or_erase_parent_trace(self):
        with self.scope():
            original = trace_run_id()
            with trace_message(None, mode="normal", provider="fake", history=False):
                self.attempt()
                self.assertIsNone(trace_run_id())
            self.assertEqual(trace_run_id(), original)
            self.attempt()
        self.assertIsNone(trace_run_id())
        self.assertEqual(read_records(self.directory)[0]["models"]["attempted"], 1)

    def test_collector_finishes_at_most_once_and_io_holds_no_collector_lock(self):
        trace = TurnTrace(self.sink, mode="normal", provider="fake", history=False)
        acquired = []
        def write(record):
            free = trace._lock.acquire(blocking=False)
            acquired.append(free)
            if free:
                trace._lock.release()
        with patch.object(self.sink, "write", side_effect=write) as writer:
            trace.finish()
            trace.finish()
        writer.assert_called_once()
        self.assertEqual(acquired, [True])

    def test_mkdir_open_and_rotation_failures_are_isolated_and_warn_once(self):
        for method in ("mkdir", "open", "replace"):
            with self.subTest(method=method):
                path = self.directory / method
                warning = Mock(side_effect=RuntimeError(PRIVATE))
                sink = JsonlTraceSink(path, limits=TraceLimits(record_bytes=2048, file_bytes=2048), warning=warning)
                if method == "replace":
                    with self.scope(sink):
                        self.attempt()
                with patch.object(Path, method, side_effect=PermissionError(PRIVATE)) as failure:
                    for _ in range(3):
                        with self.scope(sink):
                            self.attempt()
                self.assertTrue(failure.called)
                warning.assert_called_once_with()

    def test_partial_write_rolls_back_to_previous_complete_line(self):
        with self.scope():
            self.attempt()
        before = (self.directory / "turns.jsonl").read_bytes()
        original_open = Path.open
        class ShortWriter:
            def __init__(self, path, *args, **kwargs):
                self.handle = original_open(path, *args, **kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def tell(self):
                return self.handle.tell()
            def truncate(self, position):
                return self.handle.truncate(position)
            def write(self, data):
                self.handle.write(data[:len(data) // 2])
                raise OSError(PRIVATE)
        with patch.object(Path, "open", ShortWriter):
            with self.scope():
                self.attempt()
        self.assertEqual((self.directory / "turns.jsonl").read_bytes(), before)

    def test_bad_diagnostic_clock_and_hook_failures_do_not_escape(self):
        warning = Mock()
        sink = JsonlTraceSink(self.directory, clock=lambda: float("nan"), warning=warning)
        with self.scope(sink):
            self.attempt()
        warning.assert_called_once_with()
        self.assertFalse(self.directory.exists())
        with patch.object(TurnTrace, "phase", side_effect=ValueError(PRIVATE)):
            with self.scope():
                trace_phase("projection")
        self.assertEqual(len(read_records(self.directory)), 1)

    def test_request_hook_without_active_model_does_not_invent_calls(self):
        with self.scope():
            trace_provider_request("deepseek", PRIVATE)
        self.assertEqual(read_records(self.directory)[0]["models"]["attempted"], 0)


if __name__ == "__main__":
    unittest.main()

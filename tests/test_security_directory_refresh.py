from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from openpyxl import Workbook
from requests.exceptions import HTTPError, Timeout

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.exchange_directory import (
    DIRECTORY_SOURCE, SSE_QUERY, SSE_URL, SZSE_URL, ExchangeDirectorySource,
    parse_sse_directory, parse_szse_directory,
)
from trading_companion.research_intent import INTENT_TOOL_NAME, prepare_research_question
from trading_companion.security_directory import (
    Security, SecurityDirectory, load_security_directory, save_security_directory,
)
from trading_companion.security_directory_refresh import main, refresh_security_directory
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


NOW = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
SECRET = "private-transport-or-filesystem-error"


def sse_payload(stock_type="1", rows=None):
    if rows is None:
        rows = [{"A_STOCK_CODE": "600000" if stock_type == "1" else "688981",
                 "SEC_NAME_CN": "浦发银行" if stock_type == "1" else "中芯国际", "STOCK_TYPE": stock_type}]
    return {"sqlId": SSE_QUERY, "actionErrors": [], "fieldErrors": {},
            "pageHelp": {"pageNo": 1, "pageCount": 1, "total": len(rows)}, "result": rows}


def sse_body(payload=None, *, stock_type="1"):
    return json.dumps(sse_payload(stock_type) if payload is None else payload).encode("utf-8")


def szse_body(rows=None, *, header=("A股代码", "A股简称"), wrong_dimensions=False):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows if rows is not None else [("000001", "平安银行"), ("002001", "*ST测试"), ("301999", "N新股")]:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    if not wrong_dimensions:
        return stream.getvalue()
    modified = BytesIO()
    with ZipFile(BytesIO(stream.getvalue())) as original, ZipFile(modified, "w") as target:
        for entry in original.infolist():
            content = original.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(br'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', content)
            target.writestr(entry, content)
    return modified.getvalue()


def response(body, *, status=200):
    result = Mock()
    result.status_code = status
    result.iter_content.return_value = [body]
    return result


def directory(*, age=0, fixture=False):
    return SecurityDirectory((Security("000001.SZ", "平安银行", ("测试别名",)),),
                             "synthetic_refresh_test", NOW - timedelta(days=age), fixture)


class GuardedTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("Unexpected live HTTP")),
            patch("requests.post", side_effect=AssertionError("Unexpected model HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No credential reads")),
        ):
            context.start()
            self.addCleanup(context.stop)


class ExchangeParsingTests(GuardedTests):
    def test_sse_preserves_official_names_and_codes_without_alias_inference(self):
        records = parse_sse_directory(sse_body(), stock_type="1")
        self.assertEqual(records, (Security("600000.SH", "浦发银行"),))
        self.assertEqual(parse_sse_directory(sse_body(stock_type="8"), stock_type="8")[0].instrument_id,
                         "688981.SH")

    def test_sse_keeps_st_and_new_listing_names(self):
        rows = [{"A_STOCK_CODE": code, "SEC_NAME_CN": name, "STOCK_TYPE": "1"}
                for code, name in (("600001", "*ST测试"), ("603999", "N新股"))]
        self.assertEqual([item.name for item in parse_sse_directory(sse_body(sse_payload(rows=rows)), stock_type="1")],
                         ["*ST测试", "N新股"])

    def test_sse_star_list_can_contain_a_cdr_identity(self):
        rows = [{"A_STOCK_CODE": "689009", "SEC_NAME_CN": "九号公司", "STOCK_TYPE": "8"}]
        records = parse_sse_directory(sse_body(sse_payload("8", rows)), stock_type="8")
        self.assertEqual(records, (Security("689009.SH", "九号公司"),))

    def test_sse_rejects_invalid_json_or_wrong_query(self):
        for body in (b"<html>error</html>", b"\xff", b"[]", b"{}", sse_body({"sqlId": "wrong"})):
            with self.subTest(body=body), self.assertRaises(DataValidationError):
                parse_sse_directory(body, stock_type="1")

    def test_sse_rejects_missing_or_nonempty_error_fields(self):
        for key, value in (("actionErrors", ["error"]), ("actionErrors", None),
                           ("fieldErrors", {"code": "error"}), ("fieldErrors", None)):
            payload = sse_payload()
            payload[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(DataValidationError):
                parse_sse_directory(sse_body(payload), stock_type="1")

    def test_sse_rejects_partial_pages_missing_totals_and_boolean_counts(self):
        for changes in ({"total": 2}, {"total": 0}, {"total": True}, {"total": "1"},
                        {"total": 10001}, {"pageNo": 2}, {"pageNo": True},
                        {"pageCount": 2}, {"pageCount": True}):
            payload = sse_payload()
            payload["pageHelp"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DataValidationError):
                parse_sse_directory(sse_body(payload), stock_type="1")
        for field in ("pageHelp", "result"):
            payload = sse_payload()
            del payload[field]
            with self.assertRaises(DataValidationError):
                parse_sse_directory(sse_body(payload), stock_type="1")

    def test_sse_rejects_wrong_board_codes_missing_names_and_numeric_codes(self):
        for changes in ({"A_STOCK_CODE": "688981"}, {"A_STOCK_CODE": "000001"},
                        {"A_STOCK_CODE": 600000}, {"A_STOCK_CODE": "600000.0"},
                        {"SEC_NAME_CN": None}, {"SEC_NAME_CN": ""},
                        {"STOCK_TYPE": "8"}, {"STOCK_TYPE": 1}):
            payload = sse_payload()
            payload["result"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DataValidationError):
                parse_sse_directory(sse_body(payload), stock_type="1")

    def test_sse_rejects_duplicate_rows_even_when_total_matches(self):
        payload = sse_payload()
        payload["result"] *= 2
        payload["pageHelp"]["total"] = 2
        with self.assertRaises(DataValidationError):
            parse_sse_directory(sse_body(payload), stock_type="1")

    def test_sse_disallows_b_shares_as_a_configured_fragment(self):
        with self.assertRaises(InvalidRequestError):
            parse_sse_directory(sse_body(), stock_type="2")

    def test_szse_preserves_zeroes_st_new_names_and_no_inferred_aliases(self):
        records = parse_szse_directory(szse_body())
        self.assertEqual([item.instrument_id for item in records], ["000001.SZ", "002001.SZ", "301999.SZ"])
        self.assertEqual([item.name for item in records], ["平安银行", "*ST测试", "N新股"])
        self.assertTrue(all(not item.aliases for item in records))

    def test_szse_does_not_trust_incorrect_a1_a1_dimensions(self):
        self.assertEqual(len(parse_szse_directory(szse_body(wrong_dimensions=True))), 3)

    def test_szse_directory_is_not_restricted_to_tencent_analysis_prefixes(self):
        records = parse_szse_directory(szse_body([("302132", "中航成飞")]))
        self.assertEqual(records, (Security("302132.SZ", "中航成飞"),))

    def test_szse_column_positions_are_not_hardcoded(self):
        records = parse_szse_directory(szse_body([("ignored", "平安银行", "000001")],
                                                header=("公司全称", "A股简称", "A股代码")))
        self.assertEqual(records[0], Security("000001.SZ", "平安银行"))

    def test_szse_rejects_missing_or_duplicate_required_headers(self):
        for header in (("代码", "A股简称"), ("A股代码", "名称"), ("A股代码", "A股代码", "A股简称")):
            with self.subTest(header=header), self.assertRaises(DataValidationError):
                parse_szse_directory(szse_body([], header=header))

    def test_szse_rejects_empty_or_truncated_workbooks(self):
        for body in (b"", b"<html>error</html>", szse_body()[:100], szse_body([])):
            with self.subTest(length=len(body)), self.assertRaises(DataValidationError):
                parse_szse_directory(body)

    def test_szse_does_not_repair_invalid_codes_or_skip_invalid_rows(self):
        for code in (1, True, "1", "000001.0", "600000", "200001", "920001", None, "０００００１"):
            with self.subTest(code=code), self.assertRaises(DataValidationError):
                parse_szse_directory(szse_body([(code, "测试")]))
        for name in (None, "", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(DataValidationError):
                parse_szse_directory(szse_body([("000001", "平安银行"), ("000002", name)]))

    def test_szse_rejects_duplicate_records(self):
        with self.assertRaises(DataValidationError):
            parse_szse_directory(szse_body([("000001", "平安银行"), ("000001", "不同名称")]))

    def test_szse_expansion_and_record_limits_are_enforced(self):
        with patch("trading_companion.exchange_directory.MAX_XLSX_BYTES", 10), self.assertRaises(DataValidationError):
            parse_szse_directory(szse_body())
        with patch("trading_companion.exchange_directory.MAX_SECURITIES", 2), self.assertRaises(DataValidationError):
            parse_szse_directory(szse_body())


class ExchangeFetchTests(GuardedTests):
    def responses(self):
        return [response(sse_body()), response(sse_body(stock_type="8")), response(szse_body())]

    def test_success_requires_three_fragments_then_assigns_retrieval_time(self):
        responses = self.responses()
        with patch("requests.get", side_effect=responses) as fetch, \
                patch("trading_companion.exchange_directory.datetime") as clock:
            clock.now.return_value = NOW
            result = ExchangeDirectorySource(timeout_seconds=2).read_directory()
        self.assertEqual(result.source, DIRECTORY_SOURCE)
        self.assertEqual(result.retrieved_at, NOW)
        self.assertFalse(result.is_fixture)
        self.assertEqual(len(result.securities), 5)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [SSE_URL, SSE_URL, SZSE_URL])
        self.assertEqual([call.kwargs["params"]["STOCK_TYPE"] for call in fetch.call_args_list[:2]], ["1", "8"])
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs["timeout"], (2, 2))
            self.assertTrue(call.kwargs["stream"])
            self.assertFalse(call.kwargs["allow_redirects"])
        for item in responses:
            item.close.assert_called_once()
        clock.now.assert_called_once()

    def test_timeout_configuration_is_checked_before_http(self):
        for timeout in (0, -1, True, "15", None, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(InvalidRequestError):
                ExchangeDirectorySource(timeout_seconds=timeout)

    def test_http_error_closes_response_and_does_not_retry(self):
        item = response(b"error")
        item.raise_for_status.side_effect = HTTPError(SECRET)
        with patch("requests.get", return_value=item) as fetch, self.assertRaises(DataSourceError) as caught:
            ExchangeDirectorySource().read_directory()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(fetch.call_count, 1)
        item.close.assert_called_once()

    def test_transport_and_stream_timeouts_are_sanitized(self):
        with patch("requests.get", side_effect=Timeout(SECRET)) as fetch, self.assertRaises(DataSourceError) as caught:
            ExchangeDirectorySource().read_directory()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(fetch.call_count, 1)
        item = response(b"")
        item.iter_content.side_effect = Timeout(SECRET)
        with patch("requests.get", return_value=item), self.assertRaises(DataSourceError):
            ExchangeDirectorySource().read_directory()
        item.close.assert_called_once()

    def test_redirect_is_rejected_without_following(self):
        item = response(b"redirect", status=302)
        with patch("requests.get", return_value=item) as fetch, self.assertRaises(DataSourceError):
            ExchangeDirectorySource().read_directory()
        self.assertEqual(fetch.call_count, 1)
        item.close.assert_called_once()

    def test_oversized_or_empty_bodies_fail_and_close(self):
        for body in (b"", b"123456"):
            item = response(body)
            with patch("trading_companion.exchange_directory.MAX_HTTP_BYTES", 5), \
                    patch("requests.get", return_value=item), self.assertRaises(DataValidationError):
                ExchangeDirectorySource().read_directory()
            item.close.assert_called_once()

    def test_later_fragment_failure_never_returns_partial_data_or_a_new_time(self):
        for index in (1, 2):
            responses = self.responses()
            responses[index].iter_content.side_effect = Timeout(SECRET)
            with patch("requests.get", side_effect=responses) as fetch, \
                    patch("trading_companion.exchange_directory.datetime") as clock, \
                    self.assertRaises(DataSourceError):
                ExchangeDirectorySource().read_directory()
            self.assertEqual(fetch.call_count, index + 1)
            clock.now.assert_not_called()

    def test_invalid_fragment_does_not_trigger_later_requests(self):
        with patch("requests.get", return_value=response(b"{}")) as fetch, self.assertRaises(DataValidationError):
            ExchangeDirectorySource().read_directory()
        self.assertEqual(fetch.call_count, 1)

    def test_refreshed_snapshot_prepares_a_natural_request_without_another_directory_download(self):
        with patch("requests.get", side_effect=self.responses()), \
                patch("trading_companion.exchange_directory.datetime") as clock:
            clock.now.return_value = NOW
            snapshot = ExchangeDirectorySource().read_directory()
        model = FakeToolModel([ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent_1", INTENT_TOOL_NAME, {
            "intent": "research", "instrument_mentions": ["平安银行"], "date_expression": "最近",
            "indicator_mentions": [], "clarification_reason": None,
        }),))])
        prepared = prepare_research_question("看看平安银行最近走势", model=model, directory=snapshot,
                                             reference_date=date(2026, 10, 20))
        self.assertEqual(prepared.status, "ready")
        self.assertEqual(prepared.request.instrument_id, "000001.SZ")
        self.assertEqual(model.responses_consumed, 1)

    def test_known_identity_does_not_authorize_unsupported_tencent_prefixes(self):
        for instrument, name in (("689009.SH", "九号公司"), ("302132.SZ", "中航成飞")):
            snapshot = replace(directory(), securities=(Security(instrument, name),))
            model = FakeToolModel([ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent_1", INTENT_TOOL_NAME, {
                "intent": "research", "instrument_mentions": [name], "date_expression": "最近",
                "indicator_mentions": [], "clarification_reason": None,
            }),))])
            prepared = prepare_research_question(f"看看{name}最近走势", model=model, directory=snapshot,
                                                 reference_date=date(2026, 10, 20))
            self.assertEqual(prepared.status, "needs_clarification")
            self.assertEqual(prepared.code, "invalid_research_parameters")
            self.assertIsNone(prepared.request)


class SnapshotSaveTests(GuardedTests):
    def setUp(self):
        super().setUp()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "snapshot.json"

    def test_save_round_trips_all_fields_and_aliases_without_retiming(self):
        expected = directory(age=2)
        save_security_directory(expected, self.path)
        self.assertEqual(load_security_directory(self.path), expected)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_validated_new_snapshot_replaces_old_only_once(self):
        save_security_directory(directory(age=2), self.path)
        old_bytes = self.path.read_bytes()
        actual_replace = os.replace
        def replace_checked(source, destination):
            self.assertEqual(self.path.read_bytes(), old_bytes)
            self.assertEqual(load_security_directory(source), directory())
            self.assertEqual(Path(source).parent, self.path.parent)
            actual_replace(source, destination)
        with patch("trading_companion.security_directory.os.replace", side_effect=replace_checked) as swap:
            save_security_directory(directory(), self.path)
        swap.assert_called_once()
        self.assertEqual(load_security_directory(self.path), directory())

    def test_replace_failure_keeps_old_bytes_and_time_and_removes_temporary(self):
        save_security_directory(directory(age=2), self.path)
        original = self.path.read_bytes()
        with patch("trading_companion.security_directory.os.replace", side_effect=OSError(SECRET)), \
                self.assertRaises(DataSourceError) as caught:
            save_security_directory(directory(), self.path)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_write_failure_keeps_old_snapshot_and_cleans_temporary(self):
        save_security_directory(directory(age=2), self.path)
        original = self.path.read_bytes()
        with patch("trading_companion.security_directory.os.fsync", side_effect=OSError(SECRET)), \
                self.assertRaises(DataSourceError):
            save_security_directory(directory(), self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_temporary_cleanup_failure_is_sanitized_and_does_not_change_target(self):
        save_security_directory(directory(age=2), self.path)
        original = self.path.read_bytes()
        with patch("trading_companion.security_directory.os.replace", side_effect=OSError(SECRET)), \
                patch.object(Path, "unlink", side_effect=OSError(SECRET)), self.assertRaises(DataSourceError) as caught:
            save_security_directory(directory(), self.path)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)

    def test_size_limit_rejects_before_creating_a_file(self):
        with patch("trading_companion.security_directory.MAX_SNAPSHOT_BYTES", 10), \
                self.assertRaises(DataValidationError):
            save_security_directory(directory(), self.path)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_readback_mismatch_keeps_old_snapshot(self):
        save_security_directory(directory(age=2), self.path)
        original = self.path.read_bytes()
        original_loader = load_security_directory
        def mismatched(path):
            loaded = original_loader(path)
            return replace(loaded, source="unexpected") if Path(path) != self.path else loaded
        with patch("trading_companion.security_directory.load_security_directory", side_effect=mismatched), \
                self.assertRaises(DataValidationError):
            save_security_directory(directory(), self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_fixture_and_unrelated_files_are_not_overwritten(self):
        save_security_directory(directory(fixture=True), self.path)
        original = self.path.read_bytes()
        with self.assertRaises(InvalidRequestError):
            save_security_directory(directory(), self.path)
        self.assertEqual(self.path.read_bytes(), original)
        unrelated = self.folder / "other.json"
        unrelated.write_text('{"unrelated":true}', encoding="utf-8")
        with self.assertRaises(DataValidationError):
            save_security_directory(directory(), unrelated)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), '{"unrelated":true}')

    def test_non_json_output_is_not_overwritten(self):
        with self.assertRaises(InvalidRequestError):
            save_security_directory(directory(), self.folder / "README.md")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_symbolic_link_destination_is_rejected_before_network(self):
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(InvalidRequestError):
            refresh_security_directory(self.path)

    def test_destination_inspection_errors_are_sanitized(self):
        with patch.object(Path, "is_symlink", side_effect=OSError(SECRET)), \
                self.assertRaises(DataSourceError) as caught:
            refresh_security_directory(self.path)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_fragment_failure_preserves_existing_snapshot_without_automatic_fallback(self):
        save_security_directory(directory(age=2), self.path)
        original = self.path.read_bytes()
        with patch("trading_companion.exchange_directory.ExchangeDirectorySource.read_directory",
                   side_effect=DataSourceError("one fragment failed")), self.assertRaises(DataSourceError):
            refresh_security_directory(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        load_security_directory(self.path).assert_usable(date(2026, 10, 20), development=False)

    def test_failed_first_refresh_does_not_create_a_snapshot_or_parent(self):
        nested = self.folder / "new" / "snapshot.json"
        with patch("trading_companion.exchange_directory.ExchangeDirectorySource.read_directory",
                   side_effect=DataSourceError("one fragment failed")), self.assertRaises(DataSourceError):
            refresh_security_directory(nested)
        self.assertFalse(nested.parent.exists())

    def test_loading_does_not_renew_an_expired_snapshot(self):
        save_security_directory(directory(age=8), self.path)
        with self.assertRaises(DataValidationError):
            load_security_directory(self.path).assert_usable(date(2026, 10, 20), development=False)
        self.assertEqual(load_security_directory(self.path).retrieved_at, NOW - timedelta(days=8))

    def test_optional_reference_date_validates_before_replacing_existing_snapshot(self):
        save_security_directory(directory(age=2), self.path)
        original = self.path.read_bytes()
        for fetched in (directory(age=8), directory(age=-1), directory(fixture=True)):
            with self.subTest(fetched=fetched.metadata()), patch(
                    "trading_companion.exchange_directory.ExchangeDirectorySource.read_directory", return_value=fetched), \
                    self.assertRaises(DataValidationError):
                refresh_security_directory(self.path, reference_date=date(2026, 10, 20))
            self.assertEqual(self.path.read_bytes(), original)

    def test_optional_reference_date_accepts_a_current_real_snapshot(self):
        with patch("trading_companion.exchange_directory.ExchangeDirectorySource.read_directory", return_value=directory()):
            refreshed = refresh_security_directory(self.path, reference_date=date(2026, 10, 20))
        self.assertEqual(refreshed, directory())
        self.assertEqual(load_security_directory(self.path), directory())


class RefreshCliTests(GuardedTests):
    def invoke(self, arguments):
        output = StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def test_default_cli_has_no_network_or_disk_side_effects(self):
        with patch("trading_companion.security_directory_refresh.refresh_security_directory") as refresh:
            code, report = self.invoke([])
        refresh.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(report["network_requested"])
        self.assertFalse(report["snapshot_replaced"])

    def test_live_flag_saves_validated_data_and_reports_coverage_limits(self):
        with TemporaryDirectory() as folder, \
                patch("trading_companion.exchange_directory.ExchangeDirectorySource.read_directory",
                      return_value=directory()):
            path = Path(folder) / "snapshot.json"
            code, report = self.invoke(["--live", "--output", str(path), "--timeout", "2"])
            self.assertEqual(load_security_directory(path), directory())
        self.assertEqual(code, 0)
        self.assertTrue(report["snapshot_replaced"])
        self.assertFalse(report["completeness"]["independent_full_market_completeness_proven"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["market_bar_requests"], 0)

    def test_cli_reports_source_and_validation_failures_without_success(self):
        for error, expected in ((DataSourceError("source failed"), "directory_source_error"),
                                (DataValidationError("invalid table"), "directory_validation_error")):
            with TemporaryDirectory() as folder, \
                    patch("trading_companion.exchange_directory.ExchangeDirectorySource.read_directory", side_effect=error):
                path = Path(folder) / "snapshot.json"
                code, report = self.invoke(["--live", "--output", str(path)])
                self.assertFalse(path.exists())
            self.assertEqual((code, report["code"]), (1, expected))
            self.assertFalse(report["snapshot_replaced"])

    def test_invalid_timeout_is_rejected_before_network(self):
        for timeout in ("0", "-1", "nan", "inf"):
            code, report = self.invoke(["--live", "--timeout", timeout])
            self.assertEqual(code, 1)
            self.assertEqual(report["code"], "invalid_refresh_configuration")


if __name__ == "__main__":
    unittest.main()

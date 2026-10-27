import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ResponseContent } from "../src/ResponseContent";
import { parseScreeningResult, ScreeningResults } from "../src/ScreeningResults";
import { presentationReply } from "./fixtures";

const fields = { price: { label: "最新价", unit: "CNY/share" }, change_percent: { label: "涨跌幅", unit: "percent" },
  amount_cny: { label: "成交额", unit: "CNY" }, turnover_percent: { label: "换手率", unit: "percent" },
  pe_dynamic: { label: "动态市盈率", unit: "ratio" }, pb: { label: "市净率", unit: "ratio" },
  market_cap_cny: { label: "总市值", unit: "CNY" }, float_market_cap_cny: { label: "流通市值", unit: "CNY" } };

function fixture() {
  const quoted = "2026-10-26T14:59:00+08:00";
  const condition = { field: "price", operator: "gt", value: 10 };
  const strata = [{ stratum: "sse_main", label: "沪市主板", total_basis: "vendor_declared", vendor_total: 100,
    directory_records: null as number | null, scanned_records: 4, identity_verified_records: 3, unverified_records: 1,
    missing_quote_records: 0, pages_read: 1, vendor_listing_exhausted: false as boolean | null, directory_stratum_exhausted: null as boolean | null }];
  const rows = [12, 11].map((price, index) => ({ instrument_id: `60000${index}.SH`, name: index ? "示例公司" : "浦发银行",
    quoted_at: quoted, quote_date: "2026-10-26", stratum: "sse_main", risk_status_verified: false, listing_date: null,
    values: { price, change_percent: index ? -1.2 : 2.5, amount_cny: index ? 500000 : 1000000,
      turnover_percent: 1, pe_dynamic: (index ? 9 : null) as number | null, pb: 0.8,
      market_cap_cny: null as number | null, float_market_cap_cny: null as number | null },
    condition_checks: [{ ...condition, label: "最新价", unit: "CNY/share", actual_value: price, status: "passed", quoted_at: quoted }],
    sort_value: index ? 500000 : 1000000, sample_position: index + 1 }));
  const value = { version: "quote-screening-result-v1", status: "completed", code: "sample_conditions_evaluated", trading_enabled: false,
    scope: "numeric_conditions_within_actual_identity_verified_sample_not_full_market",
    request: { conditions: [condition], sort_by: "amount_cny", sort_direction: "desc", limit: 2, max_quote_age_days: 7 }, rows,
    conditions: [{ ...condition, label: "最新价", unit: "CNY/share", passed: 2, failed: 0, unknown: 1 }], fields,
    coverage: { scope: "actual_bounded_sample", independent_full_market_completeness_proven: false,
      vendor_declared_records: 100, directory_universe_records: null, scanned_records: 4, identity_verified_records: 3,
      unverified_records: 1, missing_quote_records: 0, age_eligible_records: 3, stale_records_not_evaluated: 0,
      condition_evaluated_records: 3, condition_matched_records: 2, matched_missing_sort_records: 0,
      sortable_matched_records: 2, returned_records: 2, result_truncated: false, strata },
    source: { source: "eastmoney_stratified_quote_list_v1", retrieved_at: "2026-10-26T15:00:00+08:00", data_authenticity: "vendor_snapshot",
      directory: { is_fixture: false }, strata, independent_full_market_completeness_proven: false } };
  const turn = { ...presentationReply().result, answer: "已按条件完成样本筛选。", screening: value, report: null, parameters: null };
  return { turn, value };
}

describe("screening sample presentation", () => {
  it("explains the activity shortlist before the table when the backend applied its default", () => {
    const { turn } = fixture(); turn.applied_defaults = ["screening:activity_shortlist"];
    render(<ResponseContent turn={turn} />);
    const notice = screen.getByText("未指定数值条件，本轮按成交额列出活跃研究候选，不代表潜力评分。");
    expect(notice).toBeVisible();
    expect(notice.compareDocumentPosition(screen.getByRole("table", { name: "筛选候选" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("does not infer an activity default from text or unrelated applied defaults", () => {
    const { turn } = fixture(); turn.applied_defaults = ["screening:other"];
    turn.answer = "未指定数值条件，本轮按成交额列出活跃研究候选。";
    render(<ResponseContent turn={turn} />);
    expect(screen.queryByText(/不代表潜力评分/)).not.toBeInTheDocument();
  });

  it("keeps the component notice opt-in for explicitly conditioned results", () => {
    const { value } = fixture(), data = parseScreeningResult(value)!;
    const view = render(<ScreeningResults data={data} />);
    expect(screen.queryByText(/不代表潜力评分/)).not.toBeInTheDocument();
    view.rerender(<ScreeningResults data={data} activityDefault />);
    expect(screen.getByText(/未指定数值条件.*不代表潜力评分/)).toBeVisible();
  });

  it("shows actual quote columns with units and unknown values without another request", () => {
    const { turn } = fixture();
    const fetcher = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("No requests"));
    render(<ResponseContent turn={turn} />);
    const table = within(screen.getByRole("table", { name: "筛选候选" }));
    expect(table.getByText("浦发银行")).toBeVisible();
    expect(table.getByText("600000.SH · 沪市主板")).toBeVisible();
    expect(table.getByRole("columnheader", { name: "最新价（元/股）" })).toBeVisible();
    expect(table.getByRole("columnheader", { name: "成交额（元）" })).toBeVisible();
    expect(table.getByRole("columnheader", { name: "动态市盈率（倍）" })).toBeVisible();
    expect(table.getByText("未知")).toBeVisible();
    expect(table.getByText("1,000,000")).toBeVisible();
    expect(screen.queryByText("关键读数")).not.toBeInTheDocument();
    expect(screen.queryByText(turn.answer!)).not.toBeInTheDocument();
    expect(screen.getByText(/东方财富报价/)).toBeVisible();
    expect(fetcher).not.toHaveBeenCalled(); fetcher.mockRestore();
  });

  it("keeps detailed conditions and partial coverage collapsed", () => {
    const { turn } = fixture(); render(<ResponseContent turn={turn} />);
    expect(screen.getByText("最新价 > 10 元/股")).not.toBeVisible();
    fireEvent.click(screen.getByText("筛选条件与覆盖"));
    expect(screen.getByText("最新价 > 10 元/股")).toBeVisible();
    expect(screen.getByText("供应商声明 100")).toBeVisible();
    const checks = within(screen.getByRole("table", { name: "筛选条件检查" }));
    expect(checks.getByRole("columnheader", { name: "未知" })).toBeVisible();
    expect(checks.getByRole("cell", { name: "1" })).toBeVisible();
    expect(screen.getByText(/未采样和缺失数据不计为条件失败/)).toBeVisible();
    expect(screen.queryByText(/执行记录|model_calls|debug/)).not.toBeInTheDocument();
  });

  it("labels configured-directory coverage without claiming a vendor market total", () => {
    const { turn, value } = fixture();
    value.coverage.strata[0] = { ...value.coverage.strata[0], total_basis: "configured_directory", vendor_total: null as unknown as number,
      directory_records: 100, vendor_listing_exhausted: null, directory_stratum_exhausted: false };
    render(<ResponseContent turn={turn} />); fireEvent.click(screen.getByText("筛选条件与覆盖"));
    expect(screen.getByText("目录 100")).toBeVisible();
    expect(screen.queryByText("供应商声明 100")).not.toBeInTheDocument();
  });

  it("labels synthetic results prominently", () => {
    const { turn, value } = fixture(); value.source.data_authenticity = "synthetic"; value.source.directory.is_fixture = true;
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText("人工样例 · 非真实行情")).toBeVisible();
  });

  it("can show an empty sample without turning it into a market-wide assertion", () => {
    const { turn, value } = fixture(); value.rows = []; value.status = "empty";
    value.coverage.condition_matched_records = value.coverage.sortable_matched_records = value.coverage.returned_records = 0;
    value.conditions[0] = { ...value.conditions[0], passed: 0, failed: 2, unknown: 1 };
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText("本次覆盖样本没有可展示的匹配项。")).toBeVisible();
    expect(screen.queryByRole("table", { name: "筛选候选" })).not.toBeInTheDocument();
  });

  it("renders names and fallback text as plain text", () => {
    const { turn, value } = fixture(); value.rows[0].name = '<img src=x onerror="alert(1)">';
    const { container } = render(<ResponseContent turn={turn} />);
    expect(screen.getByText(value.rows[0].name)).toBeVisible();
    expect(container.querySelector("img, script, iframe")).toBeNull();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([null, {}, [], { version: "wrong" }])("keeps confirmed plain text when optional data is malformed: %j", (screening) => {
    const { turn, value } = fixture(); turn.screening = screening as typeof value;
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText(turn.answer!)).toBeVisible();
    expect(screen.queryByRole("table", { name: "筛选候选" })).not.toBeInTheDocument();
  });

  it.each([
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].sort_value = 4; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].values.price = 4; value.rows[0].condition_checks[0].actual_value = 4; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].condition_checks[0].status = "unknown"; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].quoted_at = "2027-01-01T00:00:00Z"; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].quote_date = "2026-02-30"; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].stratum = "star"; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].values.price = Number.NaN; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[0].values.amount_cny = 1e30; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[1].instrument_id = value.rows[0].instrument_id; },
    (value: ReturnType<typeof fixture>["value"]) => { value.coverage.returned_records = 20; },
    (value: ReturnType<typeof fixture>["value"]) => { value.coverage.independent_full_market_completeness_proven = true; },
    (value: ReturnType<typeof fixture>["value"]) => { value.conditions[0].unknown = 3; },
    (value: ReturnType<typeof fixture>["value"]) => { value.request.sort_by = "__proto__"; },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows.reverse(); },
    (value: ReturnType<typeof fixture>["value"]) => { value.rows[1].sample_position = 1; },
  ])("rejects inconsistent quote, condition, ranking or coverage metadata", (mutate) => {
    const { value } = fixture(); mutate(value); expect(parseScreeningResult(value)).toBeNull();
  });

  it("rejects a unit mismatch without displaying misleading table values", () => {
    const { turn, value } = fixture(); value.fields = structuredClone(fields); value.fields.amount_cny.unit = "USD";
    expect(parseScreeningResult(value)).toBeNull(); render(<ResponseContent turn={turn} />);
    expect(screen.getByText(turn.answer!)).toBeVisible();
    expect(screen.queryByRole("table", { name: "筛选候选" })).not.toBeInTheDocument();
  });
});

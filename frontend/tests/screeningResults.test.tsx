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

function auditedFixture(expanded = false) {
  const base = fixture();
  const total = expanded ? 400 : 5;
  const directory_audit = { version: "screening-directory-audit-v1",
    selection_method: expanded ? "directory_code_uniform_interval" : "vendor_page_order",
    directory_records: total, directory_observed_records: 3, directory_unobserved_records: total - 3,
    request_count: expanded ? 3 : 1,
    strata: ["sse_main", "szse_main", "star", "chinext", "bse"].map((stratum, index) => ({
      stratum, directory_records: index ? 0 : total, directory_observed_records: index ? 0 : 3,
      directory_unobserved_records: index ? 0 : total - 3,
      directory_selected_records: (expanded && !index ? 4 : null) as number | null,
      directory_not_selected_records: (expanded && !index ? total - 4 : null) as number | null,
      vendor_not_scanned_records: (!expanded && !index ? 96 : null) as number | null,
      selection_limit: (expanded && !index ? 4 : null) as number | null,
    })) };
  const coverage = Object.assign(base.value.coverage, { directory_audit });
  const source = Object.assign(base.value.source, {
    directory: { is_fixture: false, records: total },
    sampling_policy: expanded ? "configured_directory_code_uniform_interval" : "fixed_market_strata_code_ascending_bounded_pages",
  });
  if (expanded) {
    Object.assign(coverage, { unverified_records: 0, missing_quote_records: 1 });
    Object.assign(coverage.strata[0], { total_basis: "configured_directory", directory_records: total, vendor_total: null,
      pages_read: 3, unverified_records: 0, missing_quote_records: 1, directory_stratum_exhausted: false, vendor_listing_exhausted: null });
  }
  return { ...base, audit: directory_audit, source };
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

  it("shows each candidate number beside its identity in the confirmed sample order", () => {
    const { turn } = fixture(); render(<ResponseContent turn={turn} />);
    const table = within(screen.getByRole("table", { name: "筛选候选" }));
    expect(table.getByRole("columnheader", { name: "序号" })).toBeVisible();
    const rows = table.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const first = within(rows[0]).getAllByRole("cell"), second = within(rows[1]).getAllByRole("cell");
    expect(first[0]).toHaveTextContent(/^1$/);
    expect(first[1]).toHaveTextContent("浦发银行");
    expect(first[1]).toHaveTextContent("600000.SH");
    expect(second[0]).toHaveTextContent(/^2$/);
    expect(second[1]).toHaveTextContent("示例公司");
    expect(second[1]).toHaveTextContent("600001.SH");
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
    expect(screen.getByText(/本次样本中有 2 只符合条件，展示 2 只候选/)).toBeVisible();
    fireEvent.click(screen.getByText("筛选条件与覆盖"));
    expect(screen.getByText("供应商声明 100")).toBeVisible();
    expect(screen.getByText(/未采样和缺失数据不计为条件失败.*结果仅描述实际样本/)).toBeVisible();
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

  it("shows interval sampling, unselected identities and directory gaps with three batches", () => {
    const { turn, value } = auditedFixture(true);
    expect(parseScreeningResult(value)).not.toBeNull();
    render(<ResponseContent turn={turn} />); fireEvent.click(screen.getByText("筛选条件与覆盖"));
    expect(screen.getByText(/按代码均匀间隔抽取 4\/400/)).toBeVisible();
    expect(screen.getByText("目录中未选中 396 只")).toBeVisible();
    expect(screen.getAllByText("目录未覆盖，不能判断该市场没有股票")).toHaveLength(4);
    expect(screen.getByText(/“尚未观察”包含未采样或未返回可核对报价/)).toBeVisible();
    expect(screen.getByText("配置目录仅扫描部分")).toBeVisible();
  });

  it("labels vendor pagination separately from independent directory reconciliation", () => {
    const { turn, value } = auditedFixture();
    expect(parseScreeningResult(value)).not.toBeNull();
    render(<ResponseContent turn={turn} />); fireEvent.click(screen.getByText("筛选条件与覆盖"));
    expect(screen.getAllByText("按供应商分页顺序")).toHaveLength(5);
    expect(screen.getByText("供应商列表未读取 96 条")).toBeVisible();
    expect(screen.getByText("供应商列表仅读取部分")).toBeVisible();
    expect(screen.queryByText(/按代码均匀间隔/)).not.toBeInTheDocument();
  });

  it.each([
    (value: ReturnType<typeof auditedFixture>) => { value.audit.directory_records++; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata[0].directory_observed_records++; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata[0].directory_not_selected_records = 0; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata[0].selection_limit = 0; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata[0].selection_limit = 5; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata[0].selection_limit = 201; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata.pop(); },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.strata[1].stratum = "sse_main"; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.request_count = 21; },
    (value: ReturnType<typeof auditedFixture>) => { value.audit.selection_method = "vendor_page_order"; },
    (value: ReturnType<typeof auditedFixture>) => { value.source.directory.records = 1; },
  ])("falls back to confirmed text when the optional coverage audit contradicts base counts", mutate => {
    const sample = auditedFixture(true); mutate(sample);
    expect(parseScreeningResult(sample.value)).toBeNull();
    render(<ResponseContent turn={sample.turn} />);
    expect(screen.getByText(sample.turn.answer!)).toBeVisible();
    expect(screen.queryByRole("table", { name: "筛选候选" })).not.toBeInTheDocument();
  });

  it("rejects more than two vendor pages and more than four directory batches", () => {
    const vendor = fixture().value; vendor.coverage.strata[0].pages_read = 3;
    expect(parseScreeningResult(vendor)).toBeNull();
    const directory = auditedFixture(true).value; directory.coverage.strata[0].pages_read = 5;
    expect(parseScreeningResult(directory)).toBeNull();
  });

  it("rejects a unit mismatch without displaying misleading table values", () => {
    const { turn, value } = fixture(); value.fields = structuredClone(fields); value.fields.amount_cny.unit = "USD";
    expect(parseScreeningResult(value)).toBeNull(); render(<ResponseContent turn={turn} />);
    expect(screen.getByText(turn.answer!)).toBeVisible();
    expect(screen.queryByRole("table", { name: "筛选候选" })).not.toBeInTheDocument();
  });
});

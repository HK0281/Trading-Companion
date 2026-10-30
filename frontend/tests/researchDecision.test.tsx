import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import fixture from "./decisionFixture.json";
import legacy from "./decisionLegacyFixture.json";
import { parseCompanyAnswer, CompanyParagraphs } from "../src/ProductResearch";

const fresh = () => structuredClone(fixture);

describe("reference decisions", () => {
  it.each(["profile", "multiple", "timeframe", "horizon"])("rejects inconsistent horizon %s", kind => {
    const payload = fresh();
    const plan = payload.trade_plans[0];
    if (kind === "profile") plan.horizon_profile = "short";
    if (kind === "multiple") plan.values!.atr_multiple = "9";
    if (kind === "timeframe") plan.atr_timeframe = "weekly";
    if (kind === "horizon") plan.horizon = "未来120个交易日";
    expect(parseCompanyAnswer(payload)).toBeNull();
  });
  it("retains an explicit four-month horizon while showing medium parameters", () => {
    const payload = fresh();
    payload.decisions[0].horizon = "未来4个月";
    for (const p of payload.trade_plans) p.horizon = "未来4个月";
    render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    expect(screen.getAllByText(/未来4个月/).length).toBeGreaterThan(0);
    expect(screen.getByText(/使用中线（1-3个月）参数/)).toBeVisible();
  });
  it("shows the weekly volatility date separately from the daily entry date", () => {
    const payload = fresh();
    payload.decisions[0].horizon = "未来120个交易日";
    for (const p of payload.trade_plans) {
      Object.assign(p, { horizon: "未来120个交易日", horizon_profile: "long", atr_timeframe: "weekly",
        atr_source_date: "2026-09-20", atr_multiple: "2", target_multiples: ["2", "3.5"] });
    }
    payload.trade_plans[0].values!.atr_multiple = "2";
    render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    expect(screen.getByText(/完整周线 ATR14.*波动日期 2026-09-20/)).toBeVisible();
    expect(screen.getByRole("table", { name: /ATR 参考计划.*行情 2026-09-25/ })).toBeVisible();
    expect(screen.getByText(/止盈 2（3.5R）/)).toBeVisible();
  });
  it.each(["answer_binding_invalid", "answer_binding_value_mismatch"])("keeps a surviving verdict after %s", code => {
    const payload = fresh();
    payload.decisions[0].reasons = payload.decisions[0].reasons.slice(0, 1);
    Object.assign(payload.decisions[0], { reason_review: { retained: 1, deleted: 1, codes: [code] } });
    expect(parseCompanyAnswer(payload)).not.toBeNull();
  });
  it("renders the offline example with verdict, ATR table and source dates", () => {
    expect(fixture.trade_plans[0].values!.atr).toBe("0.8102");
    expect(fixture.trade_plans[0].values!.reference_entry).toBe("10.40");
    const data = parseCompanyAnswer(fresh())!;
    expect(data).not.toBeNull();
    render(<CompanyParagraphs data={data} />);
    expect(screen.getByText("买入")).toBeVisible();
    expect(screen.getAllByText(/未来40个交易日/)[0]).toBeVisible();
    expect(screen.getByText(/模型自评，未经历史校准/)).toBeVisible();
    expect(screen.getByRole("table", { name: /ATR 参考计划 · 仅供参考.*2026-09-25/ })).toBeVisible();
    expect(screen.getByRole("table", { name: /关键价位区间/ })).toBeVisible();
    expect(screen.getByText(/人工样例，非真实行情/)).toBeVisible();
    expect(screen.getByText(/止盈 2/)).toBeVisible();
    fireEvent.click(screen.getByText("研判依据"));
    expect(screen.getAllByText(/人工演示固定采用偏多研判/)[0]).toBeVisible();
  });

  it("keeps an unavailable verdict honest while showing application levels", () => {
    const payload = fresh();
    Object.assign(payload.decisions[0], { status: "unavailable", rating: null, direction: null, conviction: null, plan_id: null });
    render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    expect(screen.getByText(/本轮研判未形成/)).toBeVisible();
    expect(screen.queryByText("持有")).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: /ATR 参考计划/ })).toBeVisible();
  });

  it("separates a hypothetical holding exit from an empty account", () => {
    const payload = fresh();
    Object.assign(payload.decisions[0], { rating: "卖出", direction: "偏空", plan_id: payload.trade_plans[1].plan_id });
    render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    expect(screen.getByText(/如已持有：收盘跌破/)).toBeVisible();
    expect(screen.getByText(/空仓：观望/)).toBeVisible();
    expect(screen.queryByText("入场参考")).not.toBeInTheDocument();
    expect(screen.queryByText(/实际盈亏比/)).not.toBeInTheDocument();
  });

  it("renders missing plan data without invented zero prices", () => {
    const payload = fresh();
    Object.assign(payload.decisions[0], { plan_id: null });
    for (const p of payload.trade_plans) Object.assign(p, { status: "unavailable", code: "risk_atr_unavailable", values: null });
    render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    expect(screen.getByText(/参考计划不可用/)).toBeVisible();
    expect(screen.queryByRole("table", { name: /ATR 参考计划/ })).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: /关键价位区间/ })).toBeVisible();
  });

  it.each(["unknown-plan", "wrong-company", "wrong-purpose", "probability", "price-order", "missing-company"])("rejects %s", kind => {
    // v3 also serves ordinary research with no verdict; v2 always requires one per company.
    const payload = kind === "missing-company" ? structuredClone(legacy) : fresh();
    if (kind === "unknown-plan") payload.decisions[0].plan_id = "not-supplied";
    if (kind === "wrong-company") payload.decisions[0].instrument_id = "600000.SH";
    if (kind === "wrong-purpose") payload.decisions[0].plan_id = payload.trade_plans[1].plan_id;
    if (kind === "probability") Object.assign(payload.decisions[0], { conviction: 0.9 });
    if (kind === "price-order") payload.trade_plans[0].values!.reference_stop = "999";
    if (kind === "missing-company") payload.decisions = [];
    expect(parseCompanyAnswer(payload)).toBeNull();
  });

  it("keeps source-derived prose as text nodes", () => {
    const payload = fresh();
    payload.decisions[0].reasons[0].text = '<img src=x onerror="alert(1)">';
    const { container } = render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    fireEvent.click(screen.getByText("研判依据"));
    expect(screen.getByText(/img src=x/)).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
  });

  it("keeps historical v2 decisions readable", () => {
    expect(legacy.version).toBe("product-research-answer-v2");
    render(<CompanyParagraphs data={parseCompanyAnswer(legacy)!} />);
    expect(screen.getByText("买入")).toBeVisible();
    expect(screen.getByRole("table", { name: /ATR 参考计划/ })).toBeVisible();
  });

  it("retains a v3 verdict with one reason and shows deletion counts and program condition origin", () => {
    const payload = fresh();
    payload.decisions[0].reasons = payload.decisions[0].reasons.slice(0, 1);
    Object.assign(payload.decisions[0], {
      reason_review: { retained: 1, deleted: 1, codes: ["answer_numeric_unmatched"] },
      invalidation_origin: "application",
    });
    const data = parseCompanyAnswer(payload);
    expect(data).not.toBeNull();
    render(<CompanyParagraphs data={data!} />);
    expect(screen.getByText("买入")).toBeVisible();
    expect(screen.getByText(/已保留 1 条依据，删除 1 条/)).toBeVisible();
    expect(screen.getByText(/研判失效或重评条件 · 程序参考计划/)).toBeVisible();
    expect(screen.queryByText(/999999/)).not.toBeInTheDocument();
  });

  it("keeps the historical v2 minimum and rejects dishonest deletion metadata", () => {
    const old = structuredClone(legacy);
    old.decisions[0].reasons = old.decisions[0].reasons.slice(0, 1);
    expect(parseCompanyAnswer(old)).toBeNull();
    const payload = fresh();
    Object.assign(payload.decisions[0], { reason_review: { retained: 99, deleted: 1, codes: ["answer_numeric_unmatched"] } });
    expect(parseCompanyAnswer(payload)).toBeNull();
    const missing = fresh();
    Object.assign(missing.decisions[0], { invalidation_origin: "application", invalidation: null });
    expect(parseCompanyAnswer(missing)).toBeNull();
  });
});

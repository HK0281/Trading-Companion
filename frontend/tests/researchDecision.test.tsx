import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import fixture from "./decisionFixture.json";
import { parseCompanyAnswer, CompanyParagraphs } from "../src/ProductResearch";

const fresh = () => structuredClone(fixture);

describe("reference decisions", () => {
  it("renders the offline example with verdict, ATR table and source dates", () => {
    expect(fixture.trade_plans[0].values!.atr).toBe("0.8102");
    expect(fixture.trade_plans[0].values!.reference_entry).toBe("10.40");
    const data = parseCompanyAnswer(fresh())!;
    expect(data).not.toBeNull();
    render(<CompanyParagraphs data={data} />);
    expect(screen.getByText("买入")).toBeVisible();
    expect(screen.getByText("未来20个交易日")).toBeVisible();
    expect(screen.getByText(/模型自评，未经历史校准/)).toBeVisible();
    expect(screen.getByRole("table")).toHaveAccessibleName(/ATR 参考计划.*2026-09-25/);
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
    expect(screen.getByRole("table")).toBeVisible();
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
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it.each(["unknown-plan", "wrong-company", "wrong-purpose", "probability", "price-order", "missing-company"])("rejects %s", kind => {
    const payload = fresh();
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
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import fixture from "./decisionFixture.json";
import legacy from "./decisionLegacyFixture.json";
import { CompanyParagraphs, parseCompanyAnswer } from "../src/ProductResearch";
import { ResponseContent } from "../src/ResponseContent";
import { reply } from "./fixtures";

function fresh() {
  const claim = (text: string) => ({ text, evidence_ids: [] as string[] });
  return { ...structuredClone(fixture), version: "product-research-answer-v3",
    quality: { state: "complete", code: "answer_ready", reasons: [] as { code: string; section: string; index: number }[] },
    narratives: [{ instrument_id: fixture.securities[0].instrument_id, summary: claim("未来一个月**偏多**，存在不确定性。"),
      current: claim("2026-09-25收盘10.40元。"), stance: claim("倾向买入，仅供参考。"),
      drivers: [{ ...claim("历史走势需要结合财务判断。"), title: "主要依据" }],
      scenarios: { base: claim("可能延续震荡。"), better: claim("盈利改善时重评。"), worse: claim("跌破观察区间时重评。") },
      levels: [{ ...claim("需要继续观察。"), lower: "9.00", upper: "10.40", lower_label: "区间下沿", upper_label: "收盘" }] }] };
}

describe("natural research narratives", () => {
  it.each(["answer_binding_invalid", "answer_binding_value_mismatch"])("explains local numeric binding failure %s", code => {
    const payload = fresh();
    payload.quality = { state: "partial", code: "answer_partial", reasons: [{ code, section: "current", index: 0 }] };
    render(<CompanyParagraphs data={parseCompanyAnswer(payload)!} />);
    expect(screen.getByRole("status")).toHaveTextContent("相关句子已移除");
  });
  it("renders conclusion, finite bold, lists, price table and existing decision card", () => {
    const data = parseCompanyAnswer(fresh());
    expect(data).not.toBeNull();
    const { container } = render(<CompanyParagraphs data={data!} />);
    expect(container.querySelector(".narrative-conclusion strong strong")).toHaveTextContent("偏多");
    expect(screen.getByRole("table", { name: /关键价位区间/ })).toBeVisible();
    expect(screen.getByRole("table", { name: /ATR 参考计划/ })).toBeVisible();
    expect(container.querySelectorAll("ul")).toHaveLength(2);
    expect(screen.getByText("基准情景")).toBeVisible();
    expect(screen.getByText("我的判断")).toBeVisible();
  });

  it("renders hostile markup as text without creating HTML, links or images", () => {
    const p = fresh();
    p.narratives[0].current.text = '<img src=x onerror="alert(1)"> [链接](https://example.invalid)';
    const { container } = render(<CompanyParagraphs data={parseCompanyAnswer(p)!} />);
    expect(screen.getByText(/img src=x/)).toBeVisible();
    expect(container.querySelector("img,script,a")).toBeNull();
  });

  it("explains partial output and fallback without inventing a rating", () => {
    const p = fresh();
    p.quality = { state: "fallback", code: "answer_facts_only", reasons: [
      { code: "answer_numeric_unmatched", section: "current", index: 0 }] };
    Object.assign(p.decisions[0], { status: "unavailable", rating: null, direction: null, conviction: null, plan_id: null });
    render(<CompanyParagraphs data={parseCompanyAnswer(p)!} />);
    expect(screen.getByRole("status")).toHaveTextContent("answer_numeric_unmatched");
    expect(screen.getByText("程序事实")).toBeVisible();
    expect(screen.queryByText("持有")).not.toBeInTheDocument();
  });

  it("rejects cross-company citations, reversed levels and duplicate identities", () => {
    for (const change of [
      (p: ReturnType<typeof fresh>) => { p.narratives[0].current.evidence_ids = ["unknown"]; },
      (p: ReturnType<typeof fresh>) => { p.narratives[0].levels[0].lower = "100"; },
      (p: ReturnType<typeof fresh>) => { p.narratives.push(p.narratives[0]); },
    ]) {
      const p = fresh(); change(p); expect(parseCompanyAnswer(p)).toBeNull();
    }
    expect(parseCompanyAnswer(structuredClone(legacy))).not.toBeNull();
  });

  it("accepts the bounded larger evidence set needed for multi-period, three-company research", () => {
    const p = fresh();
    while (p.evidence.length < 300) p.evidence.push({ ...p.evidence[0], id: `bounded-${p.evidence.length}` });
    expect(parseCompanyAnswer(p)).not.toBeNull();
    while (p.evidence.length < 481) p.evidence.push({ ...p.evidence[0], id: `bounded-${p.evidence.length}` });
    expect(parseCompanyAnswer(p)).toBeNull();
  });

  it("renders the real demo fixture with every narrative section and no deleted text", () => {
    expect(fixture.version).toBe("product-research-answer-v3");
    expect(fixture.quality.state).toBe("complete");
    expect(fixture.quality.deleted_sentences).toBe(0);
    const { container } = render(<CompanyParagraphs data={parseCompanyAnswer(fixture)!} />);
    expect(container.querySelectorAll(".narrative-drivers li")).toHaveLength(3);
    expect(container.querySelectorAll(".narrative-scenarios li")).toHaveLength(3);
    expect(screen.getByRole("table", { name: /关键价位区间/ }).querySelectorAll("tbody tr")).toHaveLength(3);
    expect(screen.getByText(/已完成日线收盘/)).toBeVisible();
    expect(screen.getByText(/倾向买入，但只作为固定人工示意/)).toBeVisible();
  });

  it("names only missing coverage while a limited turn has a complete answer", () => {
    const turn = reply().result;
    turn.status = "limited"; turn.company_research = structuredClone(fixture);
    render(<ResponseContent turn={turn} />);
    expect(screen.getByRole("note")).toHaveTextContent("回答已通过本轮校验；公司资料、财务、新闻、公告本轮未取得");
    expect(screen.getByRole("note")).not.toHaveTextContent("技术分析本轮未取得");
  });

  it("does not describe unrequested, available or excluded coverage as missing", () => {
    const p = structuredClone(fixture);
    p.coverage[0].sections = [{ ...p.coverage[0].sections[0], kind: "technical", status: "available" },
      { ...p.coverage[0].sections[0], kind: "risk", status: "excluded" }];
    const turn = reply().result;
    turn.status = "limited"; turn.company_research = p;
    render(<ResponseContent turn={turn} />);
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("does not describe incomplete answer validation as complete", () => {
    const p = fresh(); p.quality.state = "partial";
    const turn = reply().result;
    turn.status = "limited"; turn.company_research = p;
    render(<ResponseContent turn={turn} />);
    expect(screen.queryByText(/回答已通过本轮校验/)).not.toBeInTheDocument();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { ResearchWorkflow } from "../src/ResearchWorkflow";
import { parseResearchWorkflow, parseWorkflowSupportingEvidence, workflowStopLabels } from "../src/workflowProjection";
import { parseConversation } from "../src/presentation";
import { chartReply, mockApi } from "./fixtures";

function rawWorkflow() {
  return {
    version: "research-workflow-v1", request_id: "research-fixture", instrument_id: "000001.SZ",
    mode: "bounded_read_only_research",
    steps: [
      { tool: "analyze_daily_indicators", status: "executed", code: "analysis_completed" },
      { tool: "get_indicator_knowledge", status: "executed", code: "knowledge_completed" },
      { tool: "inspect_research_snapshot", status: "limited", code: "snapshot_incomplete" },
      { tool: "analyze_additional_indicators", status: "executed", code: "analysis_completed" },
    ],
    tool_calls_used: 4, model_calls_attempted: 6, analyses_used: 2,
    limits: { tool_calls: 6, model_calls: 6, analyses: 2 }, stop_reason: "answered",
  };
}

function workflowReply(id?: string) {
  const value = chartReply(id);
  value.result.research_workflow = rawWorkflow();
  value.result.answer = "本轮直接回答，工具过程无需占据正文。";
  return value;
}

function supportingEvidence() {
  return { request_id: "supplement-fixture", instrument_id: "000001.SZ", trade_date: "2026-09-25",
    fact_id: "supplement-fixture|000001.SZ|2026-09-25|rsi(window=2)|zone", text: "RSI(2)处于超买区域。" };
}

function supportingReply(id?: string) {
  const value = workflowReply(id);
  value.result.parameters!.source = "tencent";
  const scope = { instrument_id: "000001.SZ", trade_date: "2026-09-25", source: "tencent",
    start_date: value.result.parameters!.start_date, as_of_date: value.result.parameters!.as_of_date };
  value.result.conversation = { version: "conversation-answer-v1", origin: "model", request_id: "research-fixture",
    instrument_id: "000001.SZ", trade_date: "2026-09-25",
    grounding_scope: "request_and_relation_claims_checked_prose_semantics_not_verified",
    paragraphs: ["已结合本轮结果分析。", "需要继续观察趋势能否延续。", "可关注动量与价格配合。"],
    citations: ["answer", "outlook", "watch_for"].map((role) => ({ role, fact_ids: [supportingEvidence().fact_id] })),
    evidence_snapshots: [{ ...scope, role: "primary", request_id: "research-fixture" },
      { ...scope, role: "supplement", request_id: "supplement-fixture" }],
  };
  value.result.research_workflow = { ...rawWorkflow(), supporting_evidence: [supportingEvidence()] };
  return value;
}

describe("bounded research workflow projection", () => {
  it("projects only display fields and never raw tool codes or debug extras", () => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), draft: "private reasoning", key: "private credential" };
    const workflow = parseResearchWorkflow(turn);
    expect(workflow?.steps).toEqual(rawWorkflow().steps.map(({ tool, status }) => ({ tool, status })));
    expect(workflow).not.toHaveProperty("draft");
    expect(workflow).not.toHaveProperty("key");
    expect(workflow!.steps[0]).not.toHaveProperty("code");
  });

  it("renders the four read-only tools and bounded call counts in Chinese", () => {
    render(<ResearchWorkflow turn={workflowReply().result} />);
    for (const label of ["历史行情与指标", "指标知识", "已有研究快照", "补充指标分析"])
      expect(screen.getByText(label)).toBeVisible();
    expect(screen.getByText("部分可用")).toBeVisible();
    expect(screen.getByText("已完成本轮回答")).toBeVisible();
    expect(screen.getByText("工具调用 4 / 6 次 · 模型尝试 6 / 6 次 · 行情分析 2 / 2 次")).toBeVisible();
    expect(screen.queryByText("analysis_completed")).not.toBeInTheDocument();
    expect(screen.queryByText(/费用|收益|实时进度|流式/)).not.toBeInTheDocument();
  });

  it.each(Object.entries(workflowStopLabels))("summarizes %s using a fixed stop label", (reason, label) => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), stop_reason: reason };
    render(<ResearchWorkflow turn={turn} />);
    expect(screen.getByText(label)).toBeVisible();
  });

  it.each(["version", "request_id", "instrument_id", "mode", "steps", "tool_calls_used",
    "model_calls_attempted", "analyses_used", "limits", "stop_reason"])("isolates malformed %s", (field) => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), [field]: { toString: 0 } };
    expect(parseResearchWorkflow(turn)).toBeNull();
    render(<ResearchWorkflow turn={turn} />);
    expect(screen.getByText(/研究过程记录暂不可用/)).toBeVisible();
    expect(screen.queryByText("历史行情与指标")).not.toBeInTheDocument();
  });

  it.each([
    { version: "research-workflow-v2" }, { request_id: "another-request" }, { instrument_id: "600000.SH" },
    { mode: "unbounded_trading" }, { stop_reason: "unknown_reason" }, { stop_reason: "constructor" },
    { limits: { tool_calls: 7, model_calls: 6, analyses: 2 } },
    { limits: { tool_calls: 6, model_calls: 7, analyses: 2 } },
    { limits: { tool_calls: 6, model_calls: 6, analyses: 3 } },
    { limits: { tool_calls: "6", model_calls: 6, analyses: 2 } },
    { steps: Array.from({ length: 7 }, () => rawWorkflow().steps[0]) },
    { steps: [{ tool: "place_order", status: "executed", code: "private-code" }] },
    { steps: [{ tool: "constructor", status: "executed", code: "private-code" }] },
    { steps: [{ tool: "inspect_research_snapshot", status: "running", code: "private-code" }] },
    { steps: [{ tool: "inspect_research_snapshot", status: "__proto__", code: "private-code" }] },
    { steps: [{ tool: "inspect_research_snapshot", status: "executed", code: {} }] },
    { steps: [{ tool: "inspect_research_snapshot", status: "executed", code: "" }] },
  ])("rejects unknown or unbound optional projection %j", (override) => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), ...override };
    expect(parseResearchWorkflow(turn)).toBeNull();
  });

  it.each(["tool_calls_used", "model_calls_attempted", "analyses_used"])("bounds %s without accepting coerced values", (field) => {
    const turn = workflowReply().result;
    for (const value of [-1, 7, 1.5, "1", true, null, NaN, Infinity]) {
      turn.research_workflow = { ...rawWorkflow(), [field]: value };
      expect(parseResearchWorkflow(turn)).toBeNull();
    }
    turn.research_workflow = { ...rawWorkflow(), [field]: 0 };
    expect(parseResearchWorkflow(turn)).not.toBeNull();
    if (field === "analyses_used") {
      turn.research_workflow = { ...rawWorkflow(), [field]: 3 };
      expect(parseResearchWorkflow(turn)).toBeNull();
    }
  });

  it("requires an existing primary report and confirmed parameters for binding", () => {
    const turn = workflowReply().result;
    const report = turn.report;
    turn.report = null;
    expect(parseResearchWorkflow(turn)).toBeNull();
    turn.report = { fields: [], limitations: [] };
    expect(parseResearchWorkflow(turn)).toBeNull();
    turn.report = report; turn.parameters = null;
    expect(parseResearchWorkflow(turn)).toBeNull();
  });

  it("allows an empty ended execution record without inventing a tool step", () => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), steps: [], tool_calls_used: 0, model_calls_attempted: 1,
      analyses_used: 0, stop_reason: "model_error" };
    render(<ResearchWorkflow turn={turn} />);
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByText("模型未能完成本轮回答")).toBeVisible();
  });

  it.each(["rejected", "failed"])("does not label a %s tool as successfully executed", (status) => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), steps: [{ ...rawWorkflow().steps[0], status }] };
    render(<ResearchWorkflow turn={turn} />);
    expect(screen.getByText(status === "rejected" ? "未执行" : "执行失败")).toBeVisible();
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
  });

  it("never renders unknown codes, arbitrary model text, or source HTML", () => {
    const turn = workflowReply().result;
    turn.research_workflow = { ...rawWorkflow(), steps: [{ ...rawWorkflow().steps[0],
      code: "<script>private credential</script>", arguments: "private parameters", content: "private model reasoning" }] };
    const { container } = render(<ResearchWorkflow turn={turn} />);
    expect(screen.getByText("历史行情与指标")).toBeVisible();
    expect(screen.queryByText(/private/)).not.toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("adds nothing for older responses without a workflow", () => {
    const { container } = render(<ResearchWorkflow turn={chartReply().result} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows checked supplemental relations with their bound evidence identifiers", () => {
    const turn = supportingReply().result;
    expect(parseWorkflowSupportingEvidence(turn)).toEqual([supportingEvidence()]);
    render(<ResearchWorkflow turn={turn} />);
    expect(screen.getByRole("region", { name: "已核对的补充指标关系" })).toBeVisible();
    expect(screen.getByText(supportingEvidence().text)).toBeVisible();
    expect(screen.getByText(supportingEvidence().fact_id)).toBeVisible();
  });

  it.each([
    { request_id: "another-supplement" }, { instrument_id: "600000.SH" }, { trade_date: "2026-09-24" },
    { fact_id: "research-fixture|000001.SZ|2026-09-25|rsi(window=2)|zone" },
    { fact_id: "supplement-fixture|600000.SH|2026-09-25|rsi(window=2)|zone" },
    { fact_id: "supplement-fixture|000001.SZ|2026-09-24|rsi(window=2)|zone" },
    { fact_id: "supplement-fixture|000001.SZ|2026-09-25|" }, { fact_id: {} },
    { text: "" }, { text: "x".repeat(2001) }, { text: {} },
  ])("isolates an invalid supplemental relation without discarding answer or tools %j", (override) => {
    const turn = supportingReply().result;
    turn.research_workflow = { ...rawWorkflow(), supporting_evidence: [{ ...supportingEvidence(), ...override }] };
    expect(parseWorkflowSupportingEvidence(turn)).toEqual([]);
    expect(parseResearchWorkflow(turn)).not.toBeNull();
    expect(parseConversation(turn)).not.toBeNull();
    render(<ResearchWorkflow turn={turn} />);
    expect(screen.queryByRole("region", { name: "已核对的补充指标关系" })).not.toBeInTheDocument();
    expect(screen.getByText("历史行情与指标")).toBeVisible();
  });

  it("rejects the entire optional list for oversized, duplicate, or mixed invalid evidence", () => {
    const turn = supportingReply().result;
    for (const supporting_evidence of [
      "private model draft", {}, null, [supportingEvidence(), supportingEvidence()],
      [supportingEvidence(), { ...supportingEvidence(), request_id: "not-bound" }],
      Array.from({ length: 9 }, (_, index) => ({ ...supportingEvidence(), fact_id: `${supportingEvidence().fact_id}${index}` })),
    ]) {
      turn.research_workflow = { ...rawWorkflow(), supporting_evidence };
      expect(parseWorkflowSupportingEvidence(turn)).toEqual([]);
      expect(parseResearchWorkflow(turn)).not.toBeNull();
    }
  });

  it("does not trust supporting text without a validated supplemental snapshot allowlist", () => {
    const turn = supportingReply().result;
    const conversation = parseConversation(turn)!;
    for (const evidence_snapshots of [undefined, [], conversation.evidence_snapshots!.slice(0, 1),
      conversation.evidence_snapshots!.map((snapshot) => ({ ...snapshot, source: "akshare_eastmoney" }))]) {
      turn.conversation = { ...conversation, evidence_snapshots };
      expect(parseWorkflowSupportingEvidence(turn)).toEqual([]);
    }
    turn.conversation = null;
    expect(parseWorkflowSupportingEvidence(turn)).toEqual([]);
  });

  it("renders supporting text as plain text, never HTML or extra draft fields", () => {
    const turn = supportingReply().result;
    turn.research_workflow = { ...rawWorkflow(), supporting_evidence: [{ ...supportingEvidence(),
      text: "<script>not executable</script>", draft: "private model reasoning" }] };
    const { container } = render(<ResearchWorkflow turn={turn} />);
    expect(screen.getByText("<script>not executable</script>")).toBeVisible();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.queryByText("private model reasoning")).not.toBeInTheDocument();
  });

  it("keeps supplemental evidence collapsed while the natural answer stays visible", async () => {
    const api = mockApi();
    api.send.mockImplementationOnce(async (_session, id) => supportingReply(id));
    render(<App controller={new ResearchController(api)} />);
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("已结合本轮结果分析。")).toBeVisible();
    expect(screen.getByText(supportingEvidence().text)).not.toBeVisible();
    fireEvent.click(screen.getByText("本轮详情", { selector: "summary" }));
    expect(screen.getByText(supportingEvidence().text)).toBeVisible();
    expect(api.send).toHaveBeenCalledOnce();
  });

  it("keeps the answer visible and process records collapsed until details are opened", async () => {
    const api = mockApi();
    api.send.mockImplementationOnce(async (_session, id) => workflowReply(id));
    render(<App controller={new ResearchController(api)} />);
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("本轮直接回答，工具过程无需占据正文。")).toBeVisible();
    expect(screen.getByRole("region", { name: "研究过程", hidden: true })).not.toBeVisible();
    fireEvent.click(screen.getByText("本轮详情", { selector: "summary" }));
    expect(screen.getByRole("region", { name: "研究过程" })).toBeVisible();
    expect(screen.getByText("补充指标分析")).toBeVisible();
    expect(api.send).toHaveBeenCalledOnce();
  });

  it("keeps an accepted answer when optional workflow data cannot be parsed", async () => {
    const api = mockApi();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = workflowReply(id);
      value.result.research_workflow = { draft: "private model draft", request_id: "wrong" };
      return value;
    });
    render(<App controller={new ResearchController(api)} />);
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("本轮直接回答，工具过程无需占据正文。")).toBeVisible();
    expect(screen.getByText(/研究过程记录暂不可用/)).not.toBeVisible();
    expect(screen.queryByText(/private model draft/)).not.toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
  });
});

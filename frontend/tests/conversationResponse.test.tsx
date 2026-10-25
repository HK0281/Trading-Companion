import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { ResponseContent } from "../src/ResponseContent";
import { parseConversation, type ConversationAnswer } from "../src/presentation";
import { mockApi, presentationReply } from "./fixtures";

function conversationalReply() {
  const value = presentationReply();
  value.result.parameters!.source = "tencent";
  const conversation: ConversationAnswer = {
    version: "conversation-answer-v1", origin: "model", request_id: "research-fixture", instrument_id: "000001.SZ",
    trade_date: "2026-09-25", grounding_scope: "request_and_relation_claims_checked_prose_semantics_not_verified",
    paragraphs: ["目前表现偏强，但还不足以确认持续上涨。", "如果继续守住均线，偏强表现有望延续。", "接下来观察均线和动量能否配合。"],
    citations: ["answer", "outlook", "watch_for"].map((role) => ({ role: role as "answer" | "outlook" | "watch_for",
      fact_ids: ["research-fixture|000001.SZ|2026-09-25|sma(window=3)|price_position"] })),
  };
  value.result.conversation = conversation;
  return value;
}

describe("natural-language research response", () => {
  it("prioritizes the direct answer over indicator cards and the diagnostic report", () => {
    const turn = conversationalReply().result;
    render(<ResponseContent turn={turn} />);
    for (const paragraph of parseConversation(turn)!.paragraphs) expect(screen.getByText(paragraph)).toBeVisible();
    expect(screen.queryByRole("region", { name: "关键读数" })).not.toBeInTheDocument();
    expect(screen.queryByText(turn.answer!)).not.toBeInTheDocument();
    expect(screen.getByText("基于本轮历史日线的模型解读")).toBeVisible();
  });

  it.each(["version", "origin", "request_id", "instrument_id", "trade_date", "grounding_scope", "paragraphs", "citations"])(
    "isolates malformed %s instead of exposing the full report", (key) => {
      const turn = conversationalReply().result;
      turn.conversation = { ...(turn.conversation as object), [key]: { toString: 0 } };
      expect(parseConversation(turn)).toBeNull();
      render(<ResponseContent turn={turn} />);
      expect(screen.getByText(/本轮回答未能正常显示/)).toBeVisible();
      expect(screen.queryByText(turn.answer!)).not.toBeInTheDocument();
    });

  it.each(["request_id", "instrument_id", "trade_date"])("rejects another response's %s", (key) => {
    const turn = conversationalReply().result;
    turn.conversation = { ...(turn.conversation as object), [key]: "unrelated" };
    expect(parseConversation(turn)).toBeNull();
  });

  it("rejects citations from another request", () => {
    const turn = conversationalReply().result;
    const value = parseConversation(turn)!;
    value.citations[0].fact_ids = ["another|000001.SZ|2026-09-25|sma(window=3)|price_position"];
    expect(parseConversation(turn)).toBeNull();
  });

  it("accepts citations from primary and supplement snapshots only after scope checks", () => {
    const turn = conversationalReply().result;
    const value = parseConversation(turn)!;
    const scope = { instrument_id: "000001.SZ", trade_date: "2026-09-25", source: "tencent",
      start_date: turn.parameters!.start_date, as_of_date: turn.parameters!.as_of_date };
    value.evidence_snapshots = [
      { ...scope, role: "primary", request_id: "research-fixture" },
      { ...scope, role: "supplement", request_id: "supplement-fixture" },
    ];
    value.citations[0].fact_ids.push("supplement-fixture|000001.SZ|2026-09-25|rsi(window=2)|zone");
    expect(parseConversation(turn)?.citations[0].fact_ids).toHaveLength(2);
    value.citations[0].fact_ids.push("not-listed|000001.SZ|2026-09-25|rsi(window=2)|zone");
    expect(parseConversation(turn)).toBeNull();
  });

  it("keeps the legacy primary-only citation boundary when snapshots are absent", () => {
    const turn = conversationalReply().result;
    const value = parseConversation(turn)!;
    value.citations[0].fact_ids = ["supplement-fixture|000001.SZ|2026-09-25|rsi(window=2)|zone"];
    expect(parseConversation(turn)).toBeNull();
  });

  it.each([
    { role: "unknown" }, { instrument_id: "600000.SH" }, { trade_date: "2026-09-24" },
    { source: "akshare_eastmoney" }, { source: "csv" }, { start_date: "2026-09-01" },
    { as_of_date: "2026-09-26" }, { request_id: "research-fixture" }, { request_id: "bad|prefix" },
    { request_id: "has whitespace" }, { request_id: "x".repeat(101) }, { request_id: {} },
  ])("rejects a supplement outside the confirmed snapshot scope %j", (override) => {
    const turn = conversationalReply().result;
    const value = parseConversation(turn)!;
    const scope = { instrument_id: "000001.SZ", trade_date: "2026-09-25", source: "tencent",
      start_date: turn.parameters!.start_date, as_of_date: turn.parameters!.as_of_date };
    turn.conversation = { ...value, evidence_snapshots: [
      { ...scope, role: "primary", request_id: "research-fixture" },
      { ...scope, role: "supplement", request_id: "supplement-fixture", ...override },
    ] };
    expect(parseConversation(turn)).toBeNull();
  });

  it.each([undefined, null, [], "invalid", [{ role: "primary" }],
    [{ role: "supplement", request_id: "supplement-fixture" }],
  ])("rejects a present but incomplete snapshot allowlist %j", (evidence_snapshots) => {
    const turn = conversationalReply().result;
    turn.conversation = { ...parseConversation(turn)!, evidence_snapshots };
    expect(parseConversation(turn)).toBeNull();
  });

  it("requires exactly one correctly bound primary and at most one supplement", () => {
    const turn = conversationalReply().result;
    const value = parseConversation(turn)!;
    const primary = { role: "primary", request_id: "research-fixture", instrument_id: "000001.SZ",
      trade_date: "2026-09-25", source: "tencent", start_date: turn.parameters!.start_date,
      as_of_date: turn.parameters!.as_of_date };
    for (const evidence_snapshots of [
      [{ ...primary, request_id: "different-primary" }], [primary, primary],
      [primary, { ...primary, role: "supplement", request_id: "supplement-one" },
        { ...primary, role: "supplement", request_id: "supplement-two" }],
    ]) {
      turn.conversation = { ...value, evidence_snapshots };
      expect(parseConversation(turn)).toBeNull();
    }
    turn.conversation = { ...value, evidence_snapshots: [primary] };
    expect(parseConversation(turn)).not.toBeNull();
  });

  it("does not allow supplementary snapshot declarations on an application fallback", () => {
    const turn = conversationalReply().result;
    turn.conversation = { ...parseConversation(turn)!, origin: "application", grounding_scope: "application_data_readiness",
      paragraphs: ["本轮数据不足。"], citations: [], evidence_snapshots: [] };
    expect(parseConversation(turn)).toBeNull();
  });

  it("requires an explicit same-source scope for snapshot metadata without affecting legacy responses", () => {
    const turn = conversationalReply().result;
    const value = parseConversation(turn)!;
    const snapshot = { role: "primary", request_id: "research-fixture", instrument_id: "000001.SZ",
      trade_date: "2026-09-25", source: "tencent", start_date: turn.parameters!.start_date,
      as_of_date: turn.parameters!.as_of_date };
    turn.parameters!.source = undefined;
    expect(parseConversation(turn)).not.toBeNull();
    turn.conversation = { ...value, evidence_snapshots: [snapshot] };
    expect(parseConversation(turn)).toBeNull();
    turn.conversation = { ...value, evidence_snapshots: [{ ...snapshot, source: undefined }] };
    expect(parseConversation(turn)).toBeNull();
  });

  it("does not promote model prose for synthetic CSV or stopped/failed turns", () => {
    const turn = conversationalReply().result;
    for (const status of ["cancelled", "failed", "needs_clarification"]) {
      turn.status = status;
      expect(parseConversation(turn)).toBeNull();
    }
    turn.status = "completed"; turn.parameters!.source = "csv";
    expect(parseConversation(turn)).toBeNull();
  });

  it("shows a concise data-readiness answer without empty indicator cards", () => {
    const turn = conversationalReply().result;
    turn.status = "limited";
    turn.conversation = { ...parseConversation(turn)!, origin: "application", grounding_scope: "application_data_readiness",
      paragraphs: ["现在还不能判断真实走势。当前使用的是人工演示行情，只有少量日线。", "请在服务端切换真实行情后再研究。"], citations: [] };
    turn.parameters!.source = "csv";
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText(/现在还不能判断真实走势/)).toBeVisible();
    expect(screen.queryByText("基于本轮历史日线的模型解读")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "关键读数" })).not.toBeInTheDocument();
  });

  it("allows no-data responses with no trade date or fact fields", () => {
    const turn = conversationalReply().result;
    turn.conversation = { ...parseConversation(turn)!, origin: "application", grounding_scope: "application_data_readiness",
      trade_date: null, paragraphs: ["本区间没有返回日线。"], citations: [] };
    turn.report!.fields = [];
    expect(parseConversation(turn)?.paragraphs).toEqual(["本区间没有返回日线。"]);
  });

  it("renders model prose as escaped text, never HTML", () => {
    const turn = conversationalReply().result;
    parseConversation(turn)!.paragraphs[0] = "<script>alert('not executed')</script>";
    const { container } = render(<ResponseContent turn={turn} />);
    expect(screen.getByText("<script>alert('not executed')</script>")).toBeVisible();
    expect(container.querySelector("script")).toBeNull();
  });

  it("keeps details hidden and does not repeat the technical failure message above the answer", async () => {
    const api = mockApi();
    api.send.mockImplementationOnce(async () => {
      const response = conversationalReply();
      response.result.status = "limited";
      response.result.message = "technical detail not the main answer";
      return response;
    });
    render(<App controller={new ResearchController(api)} />);
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行的走势" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("目前表现偏强，但还不足以确认持续上涨。")).toBeVisible();
    expect(screen.queryByText("technical detail not the main answer")).not.toBeInTheDocument();
    expect(screen.getByText("模型引用的指标关系已核对；自然语言含义与未来方向判断并未由程序验证。")).not.toBeVisible();
    expect(screen.getByText(conversationalReply().result.answer!)).not.toBeVisible();
    fireEvent.click(screen.getByText("本轮详情", { selector: "summary" }));
    expect(screen.getByText("主要判断")).toBeVisible();
    expect(api.send).toHaveBeenCalledOnce();
  });
});

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { ResponseContent } from "../src/ResponseContent";
import { parsePresentation } from "../src/presentation";
import { ApiError } from "../src/api";
import { mockApi, presentationReply } from "./fixtures";

describe("optional answer presentation", () => {
  it("accepts only the report-bound research presentation", () => {
    expect(parsePresentation(presentationReply().result)?.kind).toBe("research");
  });
  it.each(["request_id", "instrument_id", "trade_date", "kind", "version", "headline", "sections"])("isolates malformed %s", (key) => {
    const turn = presentationReply().result;
    turn.presentation = { ...(turn.presentation as object), [key]: { toString: 0 } };
    expect(parsePresentation(turn)).toBeNull();
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText(turn.answer!)).toBeInTheDocument();
  });
  it.each(["another-research", null, "2026-09-24"])("rejects an unbound presentation date or identity %j", (value) => {
    const turn = presentationReply().result;
    turn.presentation = { ...(turn.presentation as object), trade_date: value };
    expect(parsePresentation(turn)).toBeNull();
  });
  it("rejects duplicate section identifiers", () => {
    const turn = presentationReply().result;
    const value = parsePresentation(turn)!;
    turn.presentation = { ...value, sections: [value.sections[0], value.sections[0]] };
    expect(parsePresentation(turn)).toBeNull();
  });
  it("shows verified metrics and compact observations rather than the report wall", () => {
    const turn = presentationReply().result;
    render(<ResponseContent turn={turn} />);
    expect(screen.getByRole("heading", { name: "先看已核对的指标表现" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "关键读数" })).getByText("10.31667")).toBeInTheDocument();
    expect(screen.queryByText(turn.answer!)).not.toBeInTheDocument();
    expect(screen.getByText("价格向上穿过均线。")).not.toBeVisible();
    fireEvent.click(screen.getByText("其余 1 项观察"));
    expect(screen.getByText("价格向上穿过均线。")).toBeVisible();
  });
  it("keeps missing values visible instead of displaying a zero", () => {
    const turn = presentationReply().result;
    turn.report!.fields[0] = { ...turn.report!.fields[0], value: null, status: "insufficient_history", missing_bars: 2 };
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText("1 项读数暂不可用，已保留缺失状态。")).toBeVisible();
    expect(screen.getByText("还缺 2 条日线")).toBeVisible();
    expect(screen.getByText("历史不足")).toBeVisible();
  });
  it("reveals additional metrics locally without truncating their availability state", () => {
    const turn = presentationReply().result;
    turn.report!.fields = Array.from({ length: 8 }, (_, index) => ({ ...turn.report!.fields[0], indicator_id: `sma:${index}`, label: `SMA-${index}` }));
    render(<ResponseContent turn={turn} />);
    expect(screen.queryByText("SMA-7")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看全部 8 项读数" }));
    expect(screen.getByText("SMA-7")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "收起读数" }));
    expect(screen.queryByText("SMA-7")).not.toBeInTheDocument();
  });
  it("renders knowledge as separate definitions, without stock metrics", () => {
    const turn = presentationReply().result;
    turn.parameters = null; turn.report = null;
    turn.presentation = { version: "answer-presentation-v1", kind: "knowledge", request_id: null, instrument_id: null,
      trade_date: null, headline: "把指标讲清楚", sections: [{ id: "dif", title: "DIF", items: ["快周期EMA减慢周期EMA。"] }] };
    render(<ResponseContent turn={turn} />);
    expect(screen.getByRole("heading", { name: "DIF" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "关键读数" })).not.toBeInTheDocument();
  });
  it("never promotes knowledge with attached research parameters", () => {
    const turn = presentationReply().result;
    turn.presentation = { version: "answer-presentation-v1", kind: "knowledge", request_id: null, instrument_id: null,
      trade_date: null, headline: "知识", sections: [] };
    expect(parsePresentation(turn)).toBeNull();
  });
  it("restricts follow-up metrics to the requested saved indicator", () => {
    const turn = presentationReply().result;
    const original = turn.report!.fields[0];
    turn.report!.fields.push({ ...original, indicator_id: "rsi(window=2)", label: "RSI(2)", value: 79.54 });
    turn.presentation = { ...parsePresentation(turn)!, kind: "followup", indicator_ids: ["rsi(window=2)"] };
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText("RSI(2)")).toBeVisible();
    expect(screen.queryByText("SMA(3)")).not.toBeInTheDocument();
    expect(screen.getByText("基于已保存结果 · 未重新取数")).toBeVisible();
  });
  it("rejects follow-up metric identifiers outside the saved report", () => {
    const turn = presentationReply().result;
    turn.presentation = { ...parsePresentation(turn)!, kind: "followup", indicator_ids: ["invented-indicator"] };
    expect(parsePresentation(turn)).toBeNull();
  });
  it("renders hostile section text literally", () => {
    const turn = presentationReply().result, value = parsePresentation(turn)!;
    value.sections[0].items = ["<script>alert(1)</script>"];
    const { container } = render(<ResponseContent turn={turn} />);
    expect(screen.getByText("<script>alert(1)</script>")).toBeVisible();
    expect(container.querySelector("script")).toBeNull();
  });
});

async function openChat() {
  const api = mockApi();
  api.send.mockImplementation(async (_session, id) => presentationReply(id));
  const controller = new ResearchController(api);
  const rendered = render(<App controller={controller} />);
  await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
  return { api, controller, ...rendered };
}

describe("conversation-first workspace", () => {
  it("omits retired research attachments and execution diagnostics", async () => {
    const { api } = await openChat();
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.keyDown(screen.getByLabelText("研究问题"), { key: "Enter" });
    await screen.findByRole("heading", { name: "先看已核对的指标表现" });
    expect(screen.queryByText("research_completed")).not.toBeInTheDocument();
    expect(screen.queryByText("参数与执行记录")).not.toBeInTheDocument();
    expect(screen.queryByText(presentationReply().result.answer!)).not.toBeInTheDocument();
    expect(screen.queryByText("完整报告", { selector: "summary" })).not.toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
    expect(api.history).not.toHaveBeenCalled();
  });
  it("supports Enter to send, Shift+Enter for a newline and IME composition", async () => {
    const { api } = await openChat();
    const input = screen.getByLabelText("研究问题");
    fireEvent.change(input, { target: { value: "研究问题" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(api.send).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(api.send).toHaveBeenCalledOnce());
  });
  it("keeps navigation local and does not create additional sessions", async () => {
    const { api } = await openChat();
    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "关闭导航" }));
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute("aria-expanded", "false");
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("does not hide a limited result's message behind the full report", async () => {
    const { api } = await openChat();
    api.send.mockImplementationOnce(async (_session, id) => { const value = presentationReply(id);
      value.result.status = "limited"; value.result.message = "模型解读未通过检查，仅保留核对的数据。"; return value; });
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("模型解读未通过检查，仅保留核对的数据。")).toBeVisible();
  });
  it("keeps session-creation failures visible without matching error-message keywords", async () => {
    const api = mockApi(); api.create.mockRejectedValueOnce(new ApiError(429, "session_capacity"));
    const controller = new ResearchController(api);
    render(<App controller={controller} />);
    await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("本机服务会话数已满");
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行"));
    expect(api.send).not.toHaveBeenCalled();
  });
});

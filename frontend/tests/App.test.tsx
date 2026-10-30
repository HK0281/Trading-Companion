import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ApiError, ConnectionError, type Health, type MessageResponse } from "../src/api";
import { ResearchController } from "../src/controller";
import { chartReply, credentials, deferred, health, marketContextReply, mockApi, reply } from "./fixtures";

const ANSWER = "人工样例 / 非真实行情。收盘价高于SMA(3)。";

async function workspace(paid = false) {
  const api = mockApi();
  if (paid) { api.health.mockResolvedValue({ ...health, provider: "deepseek" }); api.create.mockResolvedValue({ ...credentials, provider: "deepseek" }); }
  const controller = new ResearchController(api);
  const rendered = render(<App controller={controller} />);
  await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
  return { api, controller, ...rendered };
}
function send(question = "分析平安银行") {
  fireEvent.change(screen.getByRole("textbox", { name: "研究问题" }), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /发送问题/ }));
}

describe("research workspace", () => {
  it("shows read-only synthetic boundaries without creating a session on load", async () => {
    const { api } = await workspace();
    expect(screen.getAllByText("人工行情 · 非真实").length).toBeGreaterThan(0);
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /开始会话/ })).not.toBeInTheDocument();
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("fills examples without automatically sending or creating sessions", async () => {
    const { api } = await workspace(); fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByRole("textbox", { name: "研究问题" })).toHaveValue("分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2");
    expect(api.send).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled();
  });
  it("renders application facts and evidence but does not offer clarification for a completed answer", async () => {
    const { api } = await workspace(); send();
    await screen.findByText(ANSWER);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(screen.queryByText("指标字段与证据")).not.toBeInTheDocument();
    expect(document.querySelector(".answer-attachments")).toBeNull();
    expect(screen.queryByLabelText("候选股票")).not.toBeInTheDocument();
  });
  it("renders untrusted text literally instead of executing HTML", async () => {
    const { api, container } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => { const value = reply(id); value.result.answer = '<img src=x onerror="alert(1)">'; return value; });
    send(); await screen.findByText('<img src=x onerror="alert(1)">');
    expect(container.querySelector("img")).toBeNull();
  });
  it("integrates a request-bound chart without extra API calls", async () => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => chartReply(id));
    send(); await screen.findByText(ANSWER);
    await screen.findByRole("img", { name: /000001.SZ 历史日 K 线/ });
    expect(screen.getByText("人工样例 · 非真实行情")).toBeInTheDocument();
    const attachments = document.querySelector(".answer-attachments")!;
    expect(attachments.children).toHaveLength(1);
    expect(attachments.firstElementChild).toHaveClass("research-chart");
    expect(attachments.querySelector("details, table, input[type=range]")).toBeNull();
    expect(screen.queryByText(/指标字段与证据|研究依据|证券状态与交易日历|完整报告/)).not.toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
    expect(api.history).not.toHaveBeenCalled();
  });
  it.each(["source", "context", "data_authenticity"]) ("keeps confirmed text when optional chart %s is malformed", async (key) => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = chartReply(id); value.result.chart = { ...(value.result.chart as object), [key]: { toString: 0 } };
      return value;
    });
    send(); await screen.findByText(ANSWER);
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(screen.getByText(/图表暂不可用或未通过范围校验/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试原请求" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeEnabled();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("keeps market context in the data without showing the retired attachment", async () => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => marketContextReply(id));
    send(); await screen.findByText(ANSWER);
    expect(screen.queryByText("证券状态与交易日历")).not.toBeInTheDocument();
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce(); expect(api.history).not.toHaveBeenCalled();
  });
  it.each(["security", "request_id", "data_authenticity"])("isolates malformed optional market context %s", async (key) => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = marketContextReply(id);
      value.result.market_context = { ...(value.result.market_context as object), [key]: { toString: 0 } };
      return value;
    });
    send(); await screen.findByText(ANSWER);
    expect(screen.queryByText(/市场上下文暂不可用或未通过范围校验/)).not.toBeInTheDocument();
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试原请求" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeEnabled();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("keeps a saved answer historical without the retired market-context attachment", async () => {
    const { api, controller } = await workspace(); await act(async () => controller.start());
    api.history.mockImplementationOnce(async () => marketContextReply());
    fireEvent.click(screen.getByRole("button", { name: /查看上次结果/ }));
    await screen.findByText("已保存的研究 · 非新分析");
    expect(screen.getByText(ANSWER)).toBeInTheDocument();
    expect(screen.queryByText("证券状态与交易日历")).not.toBeInTheDocument();
    expect(api.history).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
  });
  it("fills a verified candidate only; the user must explicitly submit it", async () => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => { const value = reply(id);
      value.result = { ...value.result, status: "needs_clarification", code: "instrument_needs_clarification", answer: null,
        message: "请选择股票", parameters: null, report: null }; return value; });
    send("分析平安");
    fireEvent.click(await screen.findByRole("button", { name: "平安银行 000001.SZ" }));
    expect(screen.getByRole("textbox", { name: "研究问题" })).toHaveValue("000001.SZ");
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("sends to DeepSeek directly on explicit submission without consent cards or switches", async () => {
    const { api } = await workspace(true);
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    send();
    await screen.findByText(ANSWER);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
    expect(screen.queryByRole("group", { name: "确认发送到 DeepSeek" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /允许并发送|暂不发送/ })).not.toBeInTheDocument();
    send("继续解释");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.create).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("演示说明")).not.toBeInTheDocument();
  });
  it("offers retry and cancellation for an unconfirmed response without enabling new work", async () => {
    const { api, controller } = await workspace(); api.send.mockRejectedValueOnce(new ConnectionError());
    send(); await screen.findByRole("button", { name: "重试原请求" });
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "请求取消" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "新对话" })).toBeDisabled();
    api.send.mockImplementationOnce(async (_session, id) => ({ ...reply(id), replayed: true }));
    fireEvent.click(screen.getByRole("button", { name: "重试原请求" }));
    await screen.findByText(ANSWER); expect(api.send.mock.calls[1][1]).toBe(api.send.mock.calls[0][1]);
    expect(screen.getByText("原请求重放")).toBeInTheDocument();
    expect(controller.snapshot().entries[0].response?.replayed).toBe(true);
    expect(screen.queryByText(/原请求模型 3 次/)).not.toBeInTheDocument();
  });
  it("cancellation stays available while the original HTTP promise waits", async () => {
    const { api } = await workspace(); const waiting = deferred<MessageResponse>(); api.send.mockReturnValueOnce(waiting.promise);
    send(); fireEvent.click(await screen.findByRole("button", { name: "请求取消" }));
    await waitFor(() => expect(api.cancel).toHaveBeenCalledOnce());
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeDisabled();
    await act(async () => { waiting.resolve(reply(api.send.mock.calls[0][1])); });
    await screen.findByText(ANSWER);
  });
  it("requires confirmation for reset and retains quota counts", async () => {
    const { api } = await workspace(); send(); await screen.findByText(ANSWER);
    fireEvent.click(screen.getByText("本机服务已连接", { selector: "summary" }));
    fireEvent.click(screen.getByRole("button", { name: "清空研究条件" }));
    expect(api.reset).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认清空" }));
    await waitFor(() => expect(screen.queryByText(ANSWER)).not.toBeInTheDocument());
    expect(screen.getByText("3 / 60")).toBeInTheDocument();
  });
  it("keeps history explicitly labelled and separate from new research", async () => {
    const { api, controller } = await workspace(); await act(async () => controller.start());
    fireEvent.click(screen.getByRole("button", { name: /查看上次结果/ }));
    await screen.findByText("已保存的研究 · 非新分析"); expect(api.send).not.toHaveBeenCalled();
  });
  it("does not send Enter while using an IME, and supports Ctrl+Enter", async () => {
    const { api } = await workspace(); const input = screen.getByRole("textbox", { name: "研究问题" });
    fireEvent.change(input, { target: { value: "分析平安银行" } });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, isComposing: true }); expect(api.send).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true }); await screen.findByText(ANSWER);
  });
  it("allows drafting while the initial health check is still pending", async () => {
    const api = mockApi(), waiting = deferred<Health>(); api.health.mockReturnValueOnce(waiting.promise);
    render(<App controller={new ResearchController(api)} />);
    const input = screen.getByLabelText("研究问题");
    expect(input).toBeEnabled(); fireEvent.change(input, { target: { value: "先写下问题" } });
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    expect(screen.getByText("正在连接本机服务，你可以先输入问题。")).toBeVisible();
    await act(async () => waiting.resolve(health));
    expect(input).toHaveValue("先写下问题"); expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText(ANSWER); expect(api.send.mock.calls[0][2]).toBe("先写下问题");
  });
  it("shows reconnection beside the composer and keeps a draft after health failure", async () => {
    const api = mockApi(); api.health.mockRejectedValueOnce(new ConnectionError());
    render(<App controller={new ResearchController(api)} />);
    await screen.findByText("本机服务未连接，你的草稿仍保留在输入框。");
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "连接后再发送" } });
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
    expect(screen.getByLabelText("研究问题")).toHaveValue("连接后再发送");
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("displays creation progress and blocks rapid duplicate sends", async () => {
    const { api } = await workspace(), creation = deferred<typeof credentials>();
    api.create.mockReturnValueOnce(creation.promise); send();
    expect(screen.getByText("正在建立对话并发送问题…")).toBeVisible();
    expect(screen.getByLabelText("研究问题")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    fireEvent.keyDown(screen.getByLabelText("研究问题"), { key: "Enter" });
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
    await act(async () => creation.resolve(credentials));
    await screen.findByText(ANSWER);
    expect(api.send).toHaveBeenCalledOnce(); expect(screen.getByLabelText("研究问题")).toHaveValue("");
  });
  it("restores the draft on creation failure and resubmits only on a new user action", async () => {
    const { api } = await workspace(); api.create.mockRejectedValueOnce(new ApiError(429, "session_capacity"));
    send("  我的原问题  ");
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue("  我的原问题  "));
    expect(screen.getByRole("alert")).toHaveTextContent("问题尚未发送");
    expect(api.send).not.toHaveBeenCalled(); expect(api.create).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText(ANSWER);
    expect(api.create).toHaveBeenCalledTimes(2); expect(api.send.mock.calls[0][2]).toBe("我的原问题");
  });
  it("does not submit a paid-provider draft or starter question until explicitly sent", async () => {
    const { api } = await workspace(true);
    fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "待发送的问题" } });
    expect(screen.queryByRole("group", { name: "确认发送到 DeepSeek" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByLabelText("研究问题")).toHaveValue("待发送的问题");
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("sends a paid-provider draft with plain Enter while Shift+Enter only edits", async () => {
    const { api } = await workspace(true);
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "原问题" } });
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "修改后的问题" } });
    fireEvent.keyDown(screen.getByLabelText("研究问题"), { key: "Enter", shiftKey: true });
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "允许并发送" })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("研究问题"), { key: "Enter" });
    await screen.findByText(ANSWER);
    expect(api.send.mock.calls[0][2]).toBe("修改后的问题");
  });
  it("clears a draft with New Chat without creating an empty backend session", async () => {
    const { api } = await workspace();
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "尚未发送" } });
    fireEvent.click(screen.getByRole("button", { name: "新对话" }));
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue(""));
    expect(api.create).not.toHaveBeenCalled(); expect(api.end).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    expect(screen.getByLabelText("研究问题")).toHaveFocus();
  });
  it("discards the old chat and creates a fresh one only on the next send", async () => {
    const { api } = await workspace(); send(); await screen.findByText(ANSWER);
    fireEvent.click(screen.getByRole("button", { name: "新对话" }));
    await waitFor(() => expect(screen.queryByText(ANSWER)).not.toBeInTheDocument());
    expect(api.end).toHaveBeenCalledOnce(); expect(api.create).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("研究问题")).toBeEnabled();
    send("新问题"); await screen.findByText(ANSWER);
    expect(api.create).toHaveBeenCalledTimes(2); expect(api.send).toHaveBeenCalledTimes(2);
  });
  it("does not discard the current messages or draft if New Chat cleanup fails", async () => {
    const { api } = await workspace(); send(); await screen.findByText(ANSWER);
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "下一条草稿" } });
    api.end.mockRejectedValueOnce(new ConnectionError());
    fireEvent.click(screen.getByRole("button", { name: "新对话" }));
    await screen.findByRole("alert");
    expect(screen.getByText(ANSWER)).toBeInTheDocument();
    expect(screen.getByLabelText("研究问题")).toHaveValue("下一条草稿");
    expect(api.create).toHaveBeenCalledOnce();
  });
  it("sends directly after a paid-provider New Chat without creating an idle session", async () => {
    const { api } = await workspace(true); send();
    await screen.findByText(ANSWER);
    fireEvent.click(screen.getByRole("button", { name: "新对话" }));
    await waitFor(() => expect(screen.queryByText(ANSWER)).not.toBeInTheDocument());
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
    send("新问题"); await screen.findByText(ANSWER);
    expect(api.create).toHaveBeenCalledTimes(2); expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[1][2]).toBe("新问题");
    expect(screen.queryByRole("group", { name: "确认发送到 DeepSeek" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
  it("retries a paid-provider request directly with the original id and blocks rapid duplicates", async () => {
    const { api } = await workspace(true); api.send.mockRejectedValueOnce(new ConnectionError());
    send();
    await screen.findByRole("button", { name: "重试原请求" }); const original = api.send.mock.calls[0];
    const waiting = deferred<MessageResponse>(); api.send.mockReturnValueOnce(waiting.promise);
    const retryButton = screen.getByRole("button", { name: "重试原请求" });
    fireEvent.click(retryButton); fireEvent.click(retryButton);
    expect(screen.queryByRole("button", { name: "重试原请求" })).not.toBeInTheDocument();
    expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[1]).toEqual(original);
    expect(screen.queryByRole("button", { name: "允许并重试" })).not.toBeInTheDocument();
    await act(async () => waiting.resolve(reply(original[1])));
    await screen.findByText(ANSWER);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send.mock.calls[1]).toEqual(original);
  });
  it("keeps starter cards but omits fixed follow-up chips and internal execution diagnostics from chat", async () => {
    const { api } = await workspace();
    for (const label of [/个股研究/, /理解指标/, /调整条件/])
      expect(screen.getByRole("button", { name: label })).toBeVisible();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = reply(id);
      value.result.applied_defaults = ["cutoff:previous_shanghai_day"];
      value.result.research_workflow = { version: "research-workflow-v1", steps: [{ tool: "internal-debug-step" }] };
      return value;
    });
    send(); await screen.findByText(ANSWER);
    expect(screen.queryByText("本轮详情", { selector: "summary" })).not.toBeInTheDocument();
    expect(screen.queryByText("参数与执行记录")).not.toBeInTheDocument();
    expect(screen.queryByText("research_completed")).not.toBeInTheDocument();
    expect(screen.queryByText("cutoff:previous_shanghai_day")).not.toBeInTheDocument();
    expect(screen.queryByText("internal-debug-step")).not.toBeInTheDocument();
    expect(screen.queryByText(/本轮模型 .*分析 .*次/)).not.toBeInTheDocument();
    for (const label of ["追问 RSI", "DIF 与 DEA 是什么？", "调整指标"])
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
  });
});

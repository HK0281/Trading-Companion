import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ConnectionError, type MessageResponse } from "../src/api";
import { ResearchController } from "../src/controller";
import { chartReply, credentials, deferred, health, marketContextReply, mockApi, reply } from "./fixtures";

async function workspace(paid = false) {
  const api = mockApi();
  if (paid) { api.health.mockResolvedValue({ ...health, provider: "deepseek" }); api.create.mockResolvedValue({ ...credentials, provider: "deepseek" }); }
  const controller = new ResearchController(api);
  const rendered = render(<App controller={controller} />);
  await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
  return { api, controller, ...rendered };
}
async function start() {
  fireEvent.click(screen.getByRole("button", { name: /开始会话/ }));
  await screen.findByLabelText("会话额度");
}
function send(question = "分析平安银行") {
  fireEvent.change(screen.getByRole("textbox", { name: "研究问题" }), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /发送问题/ }));
}

describe("research workspace", () => {
  it("shows read-only synthetic boundaries without creating a session on load", async () => {
    const { api } = await workspace();
    expect(screen.getAllByText("人工行情 · 非真实").length).toBeGreaterThan(0);
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeDisabled();
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("fills examples without automatically sending or creating sessions", async () => {
    const { api } = await workspace(); fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByRole("textbox", { name: "研究问题" })).toHaveValue("分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2");
    expect(api.send).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled();
  });
  it("renders application facts and evidence but does not offer clarification for a completed answer", async () => {
    await workspace(); await start(); send();
    await screen.findByText("research_completed");
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(screen.getByText("指标字段与证据")).toBeInTheDocument();
    expect(screen.queryByLabelText("候选股票")).not.toBeInTheDocument();
  });
  it("renders untrusted text literally instead of executing HTML", async () => {
    const { api, container } = await workspace(); await start();
    api.send.mockImplementationOnce(async (_session, id) => { const value = reply(id); value.result.answer = '<img src=x onerror="alert(1)">'; return value; });
    send(); await screen.findByText('<img src=x onerror="alert(1)">');
    expect(container.querySelector("img")).toBeNull();
  });
  it("integrates a request-bound chart without extra API calls", async () => {
    const { api } = await workspace(); await start();
    api.send.mockImplementationOnce(async (_session, id) => chartReply(id));
    send(); await screen.findByText("research_completed");
    const summary = screen.getByText("历史 K 线与指标");
    const details = summary.closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    await screen.findByRole("img", { name: /000001.SZ 历史日 K 线/ });
    expect(screen.getByText("人工样例 · 非真实行情")).toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
    expect(api.history).not.toHaveBeenCalled();
  });
  it.each(["source", "context", "data_authenticity"]) ("keeps confirmed text when optional chart %s is malformed", async (key) => {
    const { api } = await workspace(); await start();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = chartReply(id); value.result.chart = { ...(value.result.chart as object), [key]: { toString: 0 } };
      return value;
    });
    send(); await screen.findByText("research_completed");
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(screen.getByText(/图表暂不可用或未通过范围校验/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试原请求" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeEnabled();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("integrates request-bound market context without extra calls or changing the answer", async () => {
    const { api } = await workspace(); await start();
    api.send.mockImplementationOnce(async (_session, id) => marketContextReply(id));
    send(); await screen.findByText("research_completed");
    fireEvent.click(screen.getByText("证券状态与交易日历", { selector: "summary" }));
    await screen.findByRole("region", { name: "证券状态与交易日历" });
    expect(screen.getByText("人工样例 · 非真实市场事实")).toBeInTheDocument();
    expect(screen.getByText("24 个自然日")).toBeInTheDocument();
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce(); expect(api.history).not.toHaveBeenCalled();
  });
  it.each(["security", "request_id", "data_authenticity"])("isolates malformed optional market context %s", async (key) => {
    const { api } = await workspace(); await start();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = marketContextReply(id);
      value.result.market_context = { ...(value.result.market_context as object), [key]: { toString: 0 } };
      return value;
    });
    send(); await screen.findByText("research_completed");
    expect(screen.getByText(/市场上下文暂不可用或未通过范围校验/)).toBeInTheDocument();
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试原请求" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeEnabled();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("marks saved market context as historical without a new research request", async () => {
    const { api } = await workspace(); await start();
    api.history.mockImplementationOnce(async () => marketContextReply());
    fireEvent.click(screen.getByRole("button", { name: /查看上次结果/ }));
    await screen.findByText("已保存上下文 · 非新查询");
    fireEvent.click(screen.getByText("证券状态与交易日历", { selector: "summary" }));
    expect(screen.getByText("展示上次保存的上下文，没有重新查询或刷新证券状态。")).toBeInTheDocument();
    expect(api.history).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
  });
  it("fills a verified candidate only; the user must explicitly submit it", async () => {
    const { api } = await workspace(); await start();
    api.send.mockImplementationOnce(async (_session, id) => { const value = reply(id);
      value.result = { ...value.result, status: "needs_clarification", code: "instrument_needs_clarification", answer: null,
        message: "请选择股票", parameters: null, report: null }; return value; });
    send("分析平安");
    fireEvent.click(await screen.findByRole("button", { name: "平安银行 000001.SZ" }));
    expect(screen.getByRole("textbox", { name: "研究问题" })).toHaveValue("000001.SZ");
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("requires consent before a real provider session can start", async () => {
    const { api } = await workspace(true);
    expect(screen.getByRole("button", { name: /开始会话/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox")); await start(); expect(api.create).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("演示说明")).not.toBeInTheDocument();
  });
  it("offers retry and cancellation for an unconfirmed response without enabling new work", async () => {
    const { api } = await workspace(); await start(); api.send.mockRejectedValueOnce(new ConnectionError());
    send(); await screen.findByRole("button", { name: "重试原请求" });
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "请求取消" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "清空研究条件" })).toBeDisabled();
    api.send.mockImplementationOnce(async (_session, id) => ({ ...reply(id), replayed: true }));
    fireEvent.click(screen.getByRole("button", { name: "重试原请求" }));
    await screen.findByText("research_completed"); expect(api.send.mock.calls[1][1]).toBe(api.send.mock.calls[0][1]);
    expect(screen.getByText("原请求重放")).toBeInTheDocument();
    expect(screen.getByText(/原请求模型 3 次/)).toHaveTextContent("本次未重做");
  });
  it("cancellation stays available while the original HTTP promise waits", async () => {
    const { api } = await workspace(); await start(); const waiting = deferred<MessageResponse>(); api.send.mockReturnValueOnce(waiting.promise);
    send(); fireEvent.click(screen.getByRole("button", { name: "请求取消" }));
    await waitFor(() => expect(api.cancel).toHaveBeenCalledOnce());
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeDisabled();
    await act(async () => { waiting.resolve(reply(api.send.mock.calls[0][1])); });
    await screen.findByText("research_completed");
  });
  it("requires confirmation for reset and retains quota counts", async () => {
    const { api } = await workspace(); await start(); send(); await screen.findByText("research_completed");
    fireEvent.click(screen.getByRole("button", { name: "清空研究条件" }));
    expect(api.reset).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认清空" }));
    await waitFor(() => expect(screen.queryByText("research_completed")).not.toBeInTheDocument());
    expect(screen.getByText("3 / 60")).toBeInTheDocument();
  });
  it("keeps history explicitly labelled and separate from new research", async () => {
    const { api } = await workspace(); await start();
    fireEvent.click(screen.getByRole("button", { name: /查看上次结果/ }));
    await screen.findByText("已保存的研究 · 非新分析"); expect(api.send).not.toHaveBeenCalled();
  });
  it("does not send Enter while using an IME, and supports Ctrl+Enter", async () => {
    const { api } = await workspace(); await start(); const input = screen.getByRole("textbox", { name: "研究问题" });
    fireEvent.change(input, { target: { value: "分析平安银行" } });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, isComposing: true }); expect(api.send).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true }); await screen.findByText("research_completed");
  });
});

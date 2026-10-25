import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ConnectionError, type Health, type MessageResponse } from "../src/api";
import { deferred, health, idle } from "./fixtures";
import { modeController, normalHealth } from "./modeFixtures";

const ANSWER = "人工样例 / 非真实行情。收盘价高于SMA(3)。";

async function workspace() {
  const fixture = modeController(); render(<App controller={fixture.controller} />);
  await waitFor(() => expect(fixture.controller.snapshot().health).not.toBeNull());
  return fixture;
}

function send(text = "分析平安银行") {
  fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
}

describe("development mode control", () => {
  it("defaults to normal mode with current-market prompts, not a dated synthetic example", async () => {
    const { normal, development } = await workspace();
    expect(screen.getByRole("switch", { name: "Dev Mode" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeEnabled();
    expect(screen.queryByText("测试 · 离线")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("演示说明")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行最近的走势，关注趋势与动量");
    expect(screen.getByLabelText("研究问题")).not.toHaveValue(expect.stringContaining("2026-09"));
    expect(normal.create).not.toHaveBeenCalled(); expect(normal.send).not.toHaveBeenCalled();
    expect(development.health).not.toHaveBeenCalled();
  });

  it("switches to explicit offline testing, clears the draft and waits for an explicit message", async () => {
    const { normal, development } = await workspace();
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "正常模式的草稿" } });
    const waiting = deferred<Health>(); development.health.mockReturnValueOnce(waiting.promise);
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeDisabled();
    expect(screen.getByLabelText("研究问题")).toBeDisabled();
    expect(screen.getByText("正在切换模式，暂不发送研究请求…")).toBeVisible();
    await act(async () => waiting.resolve(health));
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue(""));
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeEnabled();
    expect(screen.getByText("测试 · 离线")).toBeVisible();
    expect(screen.getByText(/已切换至测试模式，旧对话和草稿已清空/, { selector: "p.mode-notice" })).toBeVisible();
    expect(normal.create).not.toHaveBeenCalled(); expect(normal.send).not.toHaveBeenCalled();
    expect(development.create).not.toHaveBeenCalled(); expect(development.send).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2");
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText(ANSWER); expect(development.create).toHaveBeenCalledOnce(); expect(development.send).toHaveBeenCalledOnce();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("clears messages and draft on return to normal without creating another empty session", async () => {
    const { normal, development } = await workspace();
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeEnabled());
    send("测试研究"); await screen.findByText(ANSWER);
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "测试模式草稿" } });
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Dev Mode" })).not.toBeChecked());
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue(""));
    expect(screen.queryByText(ANSWER)).not.toBeInTheDocument();
    expect(development.end).toHaveBeenCalledOnce(); expect(normal.create).not.toHaveBeenCalled();
    expect(normal.send).not.toHaveBeenCalled(); expect(screen.queryByRole("group", { name: "确认会话操作" })).not.toBeInTheDocument();
    send("正常研究"); await screen.findByText(ANSWER);
    expect(normal.create).toHaveBeenCalledOnce(); expect(normal.send).toHaveBeenCalledOnce();
  });

  it("keeps the chat and draft intact when the old session could not be deleted", async () => {
    const { normal, development } = await workspace(); send(); await screen.findByText(ANSWER);
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "不能丢失的草稿" } });
    normal.end.mockRejectedValueOnce(new ConnectionError());
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("switch", { name: "Dev Mode" })).not.toBeChecked();
    expect(screen.getByLabelText("研究问题")).toHaveValue("不能丢失的草稿");
    expect(screen.getByText(ANSWER)).toBeVisible(); expect(development.health).not.toHaveBeenCalled();
  });

  it("does not fall back when test mode is unavailable and reconnects only that selected endpoint", async () => {
    const { normal, development } = await workspace(); development.health.mockRejectedValueOnce(new ConnectionError());
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "旧草稿" } });
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("没有自动回退或发起研究");
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue(""));
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "稍后再发送" } });
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
    expect(normal.health).toHaveBeenCalledOnce(); expect(development.health).toHaveBeenCalledTimes(2);
    expect(normal.create).not.toHaveBeenCalled(); expect(development.create).not.toHaveBeenCalled();
    expect(normal.send).not.toHaveBeenCalled(); expect(development.send).not.toHaveBeenCalled();
  });

  it("disables switching while research is running or the original response remains uncertain", async () => {
    const { normal, development } = await workspace(), waiting = deferred<MessageResponse>();
    normal.send.mockReturnValueOnce(waiting.promise); send();
    await waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    await act(async () => waiting.reject(new ConnectionError()));
    await screen.findByRole("button", { name: "重试原请求" });
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeDisabled();
    expect(development.health).not.toHaveBeenCalled(); expect(normal.end).not.toHaveBeenCalled();
  });

  it("disables switching while a remote session is still busy after returning a response", async () => {
    const { normal } = await workspace(); normal.inspect.mockResolvedValueOnce({ ...idle, state: "busy" });
    send(); await screen.findByText(ANSWER);
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeDisabled();
  });

  it("disables the control for an older normal backend without guessing a test endpoint", async () => {
    const { normal, development, controller } = modeController();
    const value = { ...normalHealth }; delete value.development_mode_available; normal.health.mockResolvedValueOnce(value);
    render(<App controller={controller} />);
    await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
    expect(screen.getByRole("switch", { name: "Dev Mode" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" })); expect(development.health).not.toHaveBeenCalled();
  });

  it("labels a standalone demo as testing and never pretends that normal mode is available", async () => {
    const { normal, development, controller } = modeController(); normal.health.mockResolvedValueOnce(health);
    render(<App controller={controller} />);
    await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Dev Mode" })).toBeDisabled();
    expect(screen.getByText("测试 · 离线")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /个股研究/ }));
    expect(screen.getByLabelText("研究问题")).toHaveValue("分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2");
    expect(development.health).not.toHaveBeenCalled(); expect(normal.create).not.toHaveBeenCalled();
  });
});

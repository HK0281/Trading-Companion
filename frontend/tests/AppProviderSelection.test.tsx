import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import { ConnectionError, type MessageResponse } from "../src/api";
import { deferred } from "./fixtures";
import { providerController, providerHealth } from "./providerFixtures";

async function workspace() {
  const fixture = providerController(); render(<App controller={fixture.controller} />);
  await screen.findByRole("combobox", { name: "研究模型" });
  return fixture;
}
function send() {
  fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "研究平安银行" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
}

describe("model choice in the chat header", () => {
  it("starts an empty chat on model change, then creates that model only on send", async () => {
    const { normal, controller } = await workspace();
    expect(screen.getByRole("combobox", { name: "研究模型" })).toHaveValue("deepseek");
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "旧草稿" } });
    fireEvent.change(screen.getByRole("combobox", { name: "研究模型" }), { target: { value: "openai" } });
    await waitFor(() => expect(screen.getByLabelText("研究问题")).toHaveValue(""));
    expect(screen.getByRole("combobox", { name: "研究模型" })).toHaveValue("openai");
    expect(controller.snapshot().health?.provider).toBe("deepseek");
    expect(screen.queryByRole("group", { name: "确认会话操作" })).not.toBeInTheDocument();
    expect(normal.create).not.toHaveBeenCalled(); expect(normal.send).not.toHaveBeenCalled();
    send(); await waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    expect(normal.create).toHaveBeenCalledWith("openai");
  });

  it("keeps the draft and messages when cleanup fails", async () => {
    const { normal } = await workspace(); send();
    await screen.findByText("人工样例 / 非真实行情。收盘价高于SMA(3)。");
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "不能丢失的草稿" } });
    normal.end.mockRejectedValueOnce(new ConnectionError());
    fireEvent.change(screen.getByRole("combobox", { name: "研究模型" }), { target: { value: "openai" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("尚未切换模型");
    expect(screen.getByRole("combobox", { name: "研究模型" })).toHaveValue("deepseek");
    expect(screen.getByLabelText("研究问题")).toHaveValue("不能丢失的草稿");
    expect(screen.getByText("人工样例 / 非真实行情。收盘价高于SMA(3)。")).toBeVisible();
  });

  it("disables model switching during a running or unconfirmed request", async () => {
    const { normal } = await workspace(); const response = deferred<MessageResponse>();
    normal.send.mockReturnValueOnce(response.promise); send();
    await waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    expect(screen.getByRole("combobox", { name: "研究模型" })).toBeDisabled();
    await act(async () => response.reject(new ConnectionError()));
    await screen.findByRole("button", { name: "重试原请求" });
    expect(screen.getByRole("combobox", { name: "研究模型" })).toBeDisabled();
  });

  it.each([undefined, ["deepseek"] as const])("hides the selector when multiple configured models are not advertised: %j", async (available) => {
    const { normal, controller } = providerController();
    const value = { ...providerHealth, available_providers: available && [...available] };
    if (available === undefined) delete value.available_providers;
    normal.health.mockResolvedValueOnce(value); render(<App controller={controller} />);
    await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
    expect(screen.queryByRole("combobox", { name: "研究模型" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "OpenAI" })).not.toBeInTheDocument();
  });

  it("hides real model choices in development and restores the normal choice on return", async () => {
    const { normal } = await workspace();
    fireEvent.change(screen.getByRole("combobox", { name: "研究模型" }), { target: { value: "openai" } });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "研究模型" })).toHaveValue("openai"));
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    await screen.findByText("测试 · 离线");
    expect(screen.queryByRole("combobox", { name: "研究模型" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "Dev Mode" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "研究模型" })).toHaveValue("openai"));
    expect(normal.create).not.toHaveBeenCalled();
  });
});

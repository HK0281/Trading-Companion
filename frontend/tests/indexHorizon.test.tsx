import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "../src/App";
import type { MessageResponse } from "../src/api";
import { ResearchController } from "../src/controller";
import { credentials, deferred, idle, indexHorizonReply, mockApi, reply } from "./fixtures";

async function workspace(configure?: (api: ReturnType<typeof mockApi>) => void) {
  const api = mockApi(); configure?.(api);
  const controller = new ResearchController(api); render(<App controller={controller} />);
  await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
  return { api, controller };
}
function send(text = "现在A股能买吗") {
  fireEvent.change(screen.getByRole("textbox", { name: "研究问题" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
}

describe("指数观察期限按钮", () => {
  it.each(["短线", "中线", "长线"])("%s 只发送固定回复一次，忙碌和旧轮次不能重用", async choice => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => indexHorizonReply(id)); send();
    const button = await screen.findByRole("button", { name: new RegExp(`^${choice}（`) });
    expect(screen.getByLabelText("观察期限")).toBeVisible();
    expect(screen.queryByLabelText("持有期限")).not.toBeInTheDocument();
    expect(screen.getByText("选择观察期限后自动继续研究。")).toBeVisible();
    const waiting = deferred<MessageResponse>(); api.send.mockReturnValueOnce(waiting.promise);
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[1][2]).toBe(choice); expect(button).toBeDisabled();
    await act(async () => waiting.resolve(reply(api.send.mock.calls[1][1])));
    expect(button).toBeDisabled();
  });
  it("过期显示原因并禁用，历史响应不重新提供选择", async () => {
    const { api, controller } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => indexHorizonReply(id, "2000-01-01T00:00:00Z")); send();
    expect(await screen.findByRole("button", { name: "中线（1-3个月）" })).toBeDisabled();
    expect(screen.getByText(/原期限选择已过期/)).toBeVisible();
    api.history.mockResolvedValueOnce(indexHorizonReply());
    await act(async () => controller.history());
    expect(screen.getAllByRole("button", { name: "中线（1-3个月）" })).toHaveLength(1);
  });
  it.each(["messages", "model_calls"] as const)("%s 额度用完不能继续期限研究", async kind => {
    const { api } = await workspace(api => {
      api.create.mockResolvedValue({ ...credentials, limits: { ...credentials.limits, [kind]: kind === "messages" ? 1 : 3 } });
      api.inspect.mockResolvedValue({ ...idle });
    });
    api.send.mockImplementationOnce(async (_session, id) => indexHorizonReply(id)); send();
    const button = await screen.findByRole("button", { name: "短线（1-2周）" });
    await waitFor(() => expect(button).toBeDisabled()); fireEvent.click(button);
    expect(api.send).toHaveBeenCalledOnce();
  });
  it.each(["reset", "end"] as const)("%s 清掉旧选择，不能跨会话复用", async action => {
    const { api, controller } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => indexHorizonReply(id)); send();
    await screen.findByRole("button", { name: "中线（1-3个月）" });
    await act(async () => { await controller[action](); });
    expect(screen.queryByLabelText("观察期限")).not.toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("取消问题后的旧选择保持禁用", async () => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => indexHorizonReply(id)); send();
    const button = await screen.findByRole("button", { name: "中线（1-3个月）" });
    send("取消"); await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(button).toBeDisabled(); fireEvent.click(button);
    expect(api.send).toHaveBeenCalledTimes(2);
  });
});

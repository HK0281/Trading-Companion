import { describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionError, type Health, type LiveProvider, type MessageResponse } from "../src/api";
import { credentials, deferred, idle, reply } from "./fixtures";
import { providerController, providerHealth } from "./providerFixtures";

describe("session-bound model switching", () => {
  it("selects the default without changing health and creates only when a question is sent", async () => {
    const { normal, controller } = providerController();
    await controller.load();
    expect(controller.snapshot().selectedProvider).toBe("deepseek");
    expect(await controller.switchProvider("openai")).toBe(true);
    expect(controller.snapshot().selectedProvider).toBe("openai");
    expect(controller.snapshot().health?.provider).toBe("deepseek");
    expect(normal.create).not.toHaveBeenCalled(); expect(normal.send).not.toHaveBeenCalled();
    await controller.send("研究平安银行");
    expect(normal.create).toHaveBeenCalledWith("openai");
    expect(normal.send.mock.calls[0][0].provider).toBe("openai");
  });

  it("holds the old model and conversation until cleanup succeeds, then starts an empty conversation", async () => {
    const { normal, controller } = providerController(); await controller.load(); await controller.send("原研究");
    const oldSession = controller.snapshot().session, oldEntries = controller.snapshot().entries;
    const cleanup = deferred<void>(); normal.end.mockReturnValueOnce(cleanup.promise);
    const switching = controller.switchProvider("openai");
    expect(controller.snapshot().selectedProvider).toBe("deepseek");
    expect(controller.snapshot().session).toBe(oldSession); expect(controller.snapshot().entries).toBe(oldEntries);
    expect(controller.snapshot().switching).toBe(true);
    expect(await controller.send("不能发送")).toBe(false);
    expect(await controller.switchProvider("openai")).toBe(false);
    expect(await controller.switchMode("development")).toBe(false);
    cleanup.resolve(); expect(await switching).toBe(true);
    expect(controller.snapshot().session).toBeNull(); expect(controller.snapshot().entries).toEqual([]);
    expect(normal.create).toHaveBeenCalledOnce(); expect(normal.send).toHaveBeenCalledOnce();
    await controller.send("新研究");
    expect(normal.create).toHaveBeenLastCalledWith("openai");
  });

  it("preserves the model, credentials and messages after unconfirmed cleanup", async () => {
    const { normal, controller } = providerController(); await controller.load(); await controller.send("原研究");
    const before = controller.snapshot(); normal.end.mockRejectedValueOnce(new ConnectionError());
    expect(await controller.switchProvider("openai")).toBe(false);
    const after = controller.snapshot();
    expect(after.selectedProvider).toBe(before.selectedProvider); expect(after.session).toBe(before.session);
    expect(after.entries).toBe(before.entries); expect(after.health).toBe(before.health);
    await controller.send("继续原研究");
    expect(normal.create).toHaveBeenCalledOnce(); expect(normal.send.mock.calls[1][0]).toBe(normal.send.mock.calls[0][0]);
  });

  it.each([401, 404])("can leave an already expired session after HTTP %s", async (status) => {
    const { normal, controller } = providerController(); await controller.load(); await controller.send("原研究");
    normal.end.mockRejectedValueOnce(new ApiError(status, "session_not_found"));
    expect(await controller.switchProvider("openai")).toBe(true);
    expect(controller.snapshot().session).toBeNull();
  });

  it("blocks changes while the original request is running or unresolved", async () => {
    const { normal, controller } = providerController(); await controller.load();
    const response = deferred<MessageResponse>(); normal.send.mockReturnValueOnce(response.promise);
    const sending = controller.send("研究平安银行"); await vi.waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    expect(await controller.switchProvider("openai")).toBe(false);
    response.reject(new ConnectionError()); await sending;
    expect(controller.snapshot().phase).toBe("uncertain");
    expect(await controller.switchProvider("openai")).toBe(false); expect(normal.end).not.toHaveBeenCalled();
  });

  it("blocks changes until a late cancellation response has settled", async () => {
    const { normal, controller } = providerController(); await controller.load();
    const response = deferred<MessageResponse>(); normal.send.mockReturnValueOnce(response.promise);
    const sending = controller.send("研究平安银行"); await vi.waitFor(() => expect(normal.send).toHaveBeenCalledOnce());
    const cancellation = deferred<void>(); normal.cancel.mockReturnValueOnce(cancellation.promise);
    const cancelling = controller.cancel();
    response.resolve(reply()); await sending;
    expect(controller.snapshot().phase).toBe("idle"); expect(controller.snapshot().cancelPending).toBe(true);
    expect(await controller.switchProvider("openai")).toBe(false);
    cancellation.resolve(); await cancelling;
    expect(await controller.switchProvider("openai")).toBe(true);
  });

  it("blocks changes while the server still reports a busy session", async () => {
    const { normal, controller } = providerController(); await controller.load();
    normal.inspect.mockResolvedValueOnce({ ...idle, state: "busy" }); await controller.send("研究平安银行");
    expect(await controller.switchProvider("openai")).toBe(false); expect(normal.end).not.toHaveBeenCalled();
  });

  it("rejects unconfigured or same-model choices without clearing the current conversation", async () => {
    const { normal, controller } = providerController();
    normal.health.mockResolvedValueOnce({ ...providerHealth, available_providers: ["deepseek"] });
    await controller.load(); await controller.send("原研究");
    const before = controller.snapshot();
    for (const target of ["openai", "deepseek", "fake"])
      expect(await controller.switchProvider(target as LiveProvider)).toBe(false);
    expect(controller.snapshot()).toBe(before); expect(normal.end).not.toHaveBeenCalled();
  });

  it("rejects substituted credentials before sending a question", async () => {
    const { normal, controller } = providerController(); await controller.load(); await controller.switchProvider("openai");
    normal.create.mockResolvedValueOnce({ ...credentials, provider: "deepseek", data_mode: "tencent" });
    expect(await controller.send("不能发送")).toBe(false);
    expect(controller.snapshot().session).toBeNull(); expect(normal.send).not.toHaveBeenCalled();
  });

  it("revalidates the remembered normal choice on return from development mode", async () => {
    const { normal, development, controller } = providerController(); await controller.load();
    await controller.switchProvider("openai"); await controller.switchMode("development");
    expect(await controller.switchProvider("deepseek")).toBe(false);
    await controller.send("离线研究"); expect(development.create).toHaveBeenCalledWith();
    normal.health.mockResolvedValueOnce({ ...providerHealth, available_providers: ["deepseek"] });
    await controller.switchMode("normal");
    expect(controller.snapshot().selectedProvider).toBe("deepseek");
    expect(normal.create).not.toHaveBeenCalled(); expect(normal.send).not.toHaveBeenCalled();
  });

  it("preserves a configured choice even if the service default changes", async () => {
    const { normal, controller } = providerController(); await controller.load(); await controller.switchProvider("openai");
    await controller.switchMode("development");
    normal.health.mockResolvedValueOnce({ ...providerHealth, provider: "openai" });
    await controller.switchMode("normal");
    expect(controller.snapshot().health?.provider).toBe("openai"); expect(controller.snapshot().selectedProvider).toBe("openai");
  });

  it("does not invent a model selection capability for older servers", async () => {
    const { normal, controller } = providerController();
    const legacy: Health = { ...providerHealth }; delete legacy.available_providers;
    normal.health.mockResolvedValueOnce(legacy); await controller.load();
    expect(await controller.switchProvider("openai")).toBe(false);
    await controller.send("研究平安银行"); expect(normal.create).toHaveBeenCalledWith();
  });
});

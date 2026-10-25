import { describe, expect, it } from "vitest";
import { ApiError, ConnectionError, type MessageResponse } from "../src/api";
import { deferred, health, idle, reply } from "./fixtures";
import { developmentCredentials, modeController, normalCredentials, normalHealth } from "./modeFixtures";

describe("safe development-mode switching", () => {
  it("starts in normal mode and discovers capability without creating sessions or research", async () => {
    const { normal, development, controller } = modeController();
    expect(controller.snapshot().mode).toBe("normal"); expect(controller.snapshot().developmentModeAvailable).toBe(false);
    await controller.load();
    expect(controller.snapshot().mode).toBe("normal"); expect(controller.snapshot().developmentModeAvailable).toBe(true);
    expect(normal.health).toHaveBeenCalledOnce(); expect(development.health).not.toHaveBeenCalled();
    expect(normal.create).not.toHaveBeenCalled(); expect(development.create).not.toHaveBeenCalled();
    expect(normal.send).not.toHaveBeenCalled(); expect(development.send).not.toHaveBeenCalled();
  });

  it("deletes the old session before switching and creates the target session only on explicit send", async () => {
    const { normal, development, controller } = modeController();
    const order: string[] = [];
    normal.end.mockImplementationOnce(async () => { order.push("normal-delete"); });
    development.health.mockImplementationOnce(async () => { order.push("development-health"); return health; });
    await controller.load(); await controller.send("正常研究");
    const previous = normal.send.mock.calls[0];
    expect(await controller.switchMode("development")).toBe(true);
    expect(order).toEqual(["normal-delete", "development-health"]);
    expect(controller.snapshot().mode).toBe("development"); expect(controller.snapshot().entries).toEqual([]);
    expect(controller.snapshot().session).toBeNull(); expect(controller.snapshot().developmentModeAvailable).toBe(true);
    expect(development.create).not.toHaveBeenCalled(); expect(development.send).not.toHaveBeenCalled();
    await controller.send("人工行情研究");
    expect(development.create).toHaveBeenCalledOnce();
    expect(development.send.mock.calls[0][0]).toBe(developmentCredentials);
    expect(development.send.mock.calls[0][1]).not.toBe(previous[1]);
    expect(normal.send).toHaveBeenCalledOnce();
    expect(await controller.switchMode("normal")).toBe(true);
    expect(development.end).toHaveBeenCalledWith(developmentCredentials);
    expect(normal.create).toHaveBeenCalledOnce();
    await controller.send("回到正常研究");
    expect(normal.create).toHaveBeenCalledTimes(2); expect(normal.send.mock.calls[1][0]).toBe(normalCredentials);
    expect(controller.snapshot().mode).toBe("normal");
    expect(JSON.stringify(controller.snapshot())).not.toContain(developmentCredentials.access_token);
  });

  it("retains the old endpoint, token, quota and messages when cleanup fails", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); await controller.send("原问题");
    const original = controller.snapshot(); normal.end.mockRejectedValueOnce(new ConnectionError());
    expect(await controller.switchMode("development")).toBe(false);
    expect(controller.snapshot().mode).toBe("normal"); expect(controller.snapshot().entries).toEqual(original.entries);
    expect(controller.snapshot().session).toEqual(original.session); expect(controller.snapshot().health).toEqual(original.health);
    expect(controller.snapshot().notice).toContain("尚未切换模式");
    expect(development.health).not.toHaveBeenCalled();
    await controller.send("原模式继续");
    expect(normal.create).toHaveBeenCalledOnce(); expect(normal.send).toHaveBeenCalledTimes(2);
  });

  it.each([401, 404])("can leave an already expired session after HTTP %s", async (status) => {
    const { normal, controller } = modeController();
    await controller.load(); await controller.send("原问题");
    normal.end.mockRejectedValueOnce(new ApiError(status, "session_not_found"));
    expect(await controller.switchMode("development")).toBe(true);
    expect(controller.snapshot().mode).toBe("development"); expect(controller.snapshot().session).toBeNull();
  });

  it("serializes repeated switches and blocks sends during old-session cleanup", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); await controller.send("原问题");
    const cleanup = deferred<void>(); normal.end.mockReturnValueOnce(cleanup.promise);
    const switching = controller.switchMode("development");
    expect(controller.snapshot().switching).toBe(true); expect(controller.snapshot().mode).toBe("normal");
    expect(await controller.switchMode("development")).toBe(false); expect(await controller.switchMode("normal")).toBe(false);
    expect(await controller.send("不应发送")).toBe(false); await controller.history(); await controller.reset();
    expect(normal.end).toHaveBeenCalledOnce(); expect(development.health).not.toHaveBeenCalled();
    cleanup.resolve(); await switching;
    expect(development.health).toHaveBeenCalledOnce(); expect(controller.snapshot().switching).toBe(false);
  });

  it("keeps a failed target selected and disconnected without falling back or creating sessions", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); development.health.mockRejectedValueOnce(new ConnectionError());
    expect(await controller.switchMode("development")).toBe(true);
    expect(controller.snapshot().mode).toBe("development"); expect(controller.snapshot().health).toBeNull();
    expect(controller.snapshot().developmentModeAvailable).toBe(true);
    expect(await controller.send("不得发送")).toBe(false);
    expect(normal.health).toHaveBeenCalledOnce(); expect(normal.create).not.toHaveBeenCalled();
    expect(development.create).not.toHaveBeenCalled(); expect(development.send).not.toHaveBeenCalled();
    await controller.load(); expect(development.health).toHaveBeenCalledTimes(2);
    expect(controller.snapshot().mode).toBe("development"); expect(controller.snapshot().health).toEqual({ ...health, development_mode_available: false });
    expect(development.create).not.toHaveBeenCalled();
    expect(await controller.switchMode("normal")).toBe(true);
    expect(normal.health).toHaveBeenCalledTimes(2); expect(normal.create).not.toHaveBeenCalled();
  });

  it("does not permit a live service to masquerade as the offline target", async () => {
    const { development, controller } = modeController();
    await controller.load(); development.health.mockResolvedValueOnce(normalHealth);
    expect(await controller.switchMode("development")).toBe(true);
    expect(controller.snapshot().mode).toBe("development"); expect(controller.snapshot().health).toBeNull();
    expect(await controller.send("不得发送")).toBe(false); expect(development.create).not.toHaveBeenCalled();
  });

  it("rejects switching while research is running or its response remains uncertain", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); const waiting = deferred<MessageResponse>(); normal.send.mockReturnValueOnce(waiting.promise);
    const send = controller.send("原问题");
    await Promise.resolve();
    expect(await controller.switchMode("development")).toBe(false);
    waiting.reject(new ConnectionError()); await send;
    expect(controller.snapshot().phase).toBe("uncertain");
    expect(await controller.switchMode("development")).toBe(false);
    expect(normal.end).not.toHaveBeenCalled(); expect(development.health).not.toHaveBeenCalled();
    await controller.retry();
    expect(normal.send.mock.calls[1][1]).toBe(normal.send.mock.calls[0][1]);
  });

  it("waits for a late cancellation response even when the research result already arrived", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); await controller.start(); const result = deferred<MessageResponse>(), cancel = deferred<void>();
    normal.send.mockReturnValueOnce(result.promise); normal.cancel.mockReturnValueOnce(cancel.promise);
    const sending = controller.send("原问题"); const cancelling = controller.cancel();
    result.resolve(reply(normal.send.mock.calls[0][1])); await sending;
    expect(controller.snapshot().phase).toBe("idle"); expect(controller.snapshot().cancelPending).toBe(true);
    expect(await controller.switchMode("development")).toBe(false);
    expect(normal.end).not.toHaveBeenCalled(); expect(development.health).not.toHaveBeenCalled();
    cancel.resolve(); await cancelling;
    expect(await controller.switchMode("development")).toBe(true);
  });

  it("refuses a switch when the server still reports a busy session", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); normal.inspect.mockResolvedValueOnce({ ...idle, state: "busy", active_request_id: "b".repeat(32) });
    await controller.send("原问题");
    expect(controller.snapshot().phase).toBe("idle");
    expect(await controller.switchMode("development")).toBe(false);
    expect(normal.end).not.toHaveBeenCalled(); expect(development.health).not.toHaveBeenCalled();
  });

  it.each([undefined, false])("does not guess a development route without root capability %s", async (capability) => {
    const { normal, development, controller } = modeController();
    const selected = { ...normalHealth }; delete selected.development_mode_available;
    normal.health.mockResolvedValueOnce(capability === undefined ? selected : { ...selected, development_mode_available: false });
    await controller.load(); expect(await controller.switchMode("development")).toBe(false);
    expect(development.health).not.toHaveBeenCalled(); expect(normal.create).not.toHaveBeenCalled();
  });

  it("represents a standalone offline service honestly and cannot pretend to switch to normal", async () => {
    const { normal, development, controller } = modeController(); normal.health.mockResolvedValueOnce(health);
    await controller.load();
    expect(controller.snapshot().mode).toBe("standalone_demo"); expect(controller.snapshot().developmentModeAvailable).toBe(false);
    expect(await controller.switchMode("normal")).toBe(false); expect(await controller.switchMode("development")).toBe(false);
    expect(development.health).not.toHaveBeenCalled();
  });

  it("does not silently turn a previously normal endpoint into demo after a server restart", async () => {
    const { normal, development, controller } = modeController();
    await controller.load(); await controller.switchMode("development");
    normal.health.mockResolvedValue(health);
    expect(await controller.switchMode("normal")).toBe(true);
    expect(controller.snapshot().mode).toBe("normal"); expect(controller.snapshot().health).toBeNull();
    expect(controller.snapshot().notice).toContain("已选择正常模式，但服务未连接");
    expect(controller.snapshot().notice).not.toContain("已切换至正常模式");
    expect(controller.snapshot().developmentModeAvailable).toBe(true);
    expect(await controller.send("不能用人工数据代替正常研究")).toBe(false);
    await controller.load(); expect(controller.snapshot().health).toBeNull();
    expect(controller.snapshot().mode).toBe("normal");
    expect(normal.create).not.toHaveBeenCalled(); expect(development.create).not.toHaveBeenCalled();
    expect(normal.send).not.toHaveBeenCalled(); expect(development.send).not.toHaveBeenCalled();
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionError, type MessageResponse } from "../src/api";
import { ResearchController } from "../src/controller";
import { credentials, deferred, health, ID, idle, mockApi, reply, chartReply } from "./fixtures";

describe("research controller", () => {
  let api: ReturnType<typeof mockApi>;
  let controller: ResearchController;
  beforeEach(() => { api = mockApi(); let next = 0; controller = new ResearchController(api, () => (++next).toString(16).padStart(32, "0")); });
  const ready = async () => { await controller.load(); await controller.start(); };

  it("binds chart fetching to the report id, preserves research state and rejects late results after reset", async () => {
    const pending = deferred<unknown>();
    const chart = vi.fn().mockResolvedValueOnce({ status: "unavailable" }).mockReturnValueOnce(pending.promise);
    Object.assign(api, { chart });
    await ready(); api.send.mockResolvedValue(chartReply()); await controller.send("问题");
    const entry = controller.snapshot().entries[0];
    const original = controller.snapshot();
    await controller.loadChart(entry.id, "1w");
    expect(chart.mock.calls[0][1]).toBe("research-fixture");
    expect(controller.snapshot()).toBe(original);
    const late = controller.loadChart(entry.id, "60m");
    const rejected = expect(late).rejects.toThrow(ConnectionError);
    await controller.reset(); pending.resolve({ status: "available" }); await rejected;
    expect(api.send).toHaveBeenCalledOnce();
  });

  it("page initialization checks health only; no session or model call", async () => {
    await controller.load();
    expect(api.health).toHaveBeenCalledOnce(); expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("serializes rapid session creation and never exposes tokens in public state", async () => {
    await controller.load();
    await Promise.all([controller.start(), controller.start()]);
    expect(api.create).toHaveBeenCalledOnce();
    expect(JSON.stringify(controller.snapshot())).not.toContain(credentials.access_token);
    expect(controller.snapshot().session?.shortId).toBe(credentials.session_id.slice(0, 8));
  });
  it("allows explicit real-provider messages directly without requesting separate permission", async () => {
    api.health.mockResolvedValue({ ...health, provider: "deepseek" });
    api.create.mockResolvedValue({ ...credentials, provider: "deepseek" });
    await controller.load();
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    await controller.send("问题"); await controller.send("后续问题");
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledTimes(2);
    expect(controller.snapshot()).not.toHaveProperty("consent");
  });
  it("refuses a mode changed between health check and creation", async () => {
    await controller.load(); api.create.mockResolvedValue({ ...credentials, provider: "deepseek" });
    await controller.start(); expect(controller.snapshot().session).toBeNull(); expect(api.send).not.toHaveBeenCalled();
  });
  it("uses one immutable question and request id for retries after a lost response", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ConnectionError());
    await controller.send("  分析平安银行  ");
    const original = api.send.mock.calls[0];
    expect(controller.snapshot().phase).toBe("uncertain");
    expect(await controller.send("换一个问题")).toBe(false);
    await controller.reset(); await controller.end();
    expect(api.reset).not.toHaveBeenCalled(); expect(api.end).not.toHaveBeenCalled();
    const replay = reply(original[1]); replay.replayed = true; api.send.mockResolvedValueOnce(replay);
    await controller.retry();
    expect(api.send.mock.calls[1]).toEqual(original);
    expect(original[2]).toBe("分析平安银行"); expect(controller.snapshot().entries).toHaveLength(1);
    expect(controller.snapshot().pendingId).toBeNull(); expect(controller.snapshot().entries[0].response?.replayed).toBe(true);
  });
  it("blocks a second question synchronously while the first model call is waiting", async () => {
    await ready(); const waiting = deferred<MessageResponse>(); api.send.mockReturnValueOnce(waiting.promise);
    const first = controller.send("第一条");
    expect(await controller.send("第二条")).toBe(false); expect(api.send).toHaveBeenCalledOnce();
    waiting.resolve(reply(api.send.mock.calls[0][1])); await first;
  });
  it("serializes concurrent direct retries of a live-provider request using the original id", async () => {
    api.health.mockResolvedValue({ ...health, provider: "deepseek" });
    api.create.mockResolvedValue({ ...credentials, provider: "deepseek" });
    await controller.load(); api.send.mockRejectedValueOnce(new ConnectionError());
    await controller.send("原问题");
    const original = api.send.mock.calls[0], waiting = deferred<MessageResponse>();
    api.send.mockReturnValueOnce(waiting.promise);
    const retry = controller.retry(); await controller.retry();
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[1]).toEqual(original);
    expect(await controller.send("不能并发的新问题")).toBe(false);
    waiting.resolve({ ...reply(original[1]), replayed: true }); await retry;
    expect(controller.snapshot().pendingId).toBeNull();
    expect(controller.snapshot().entries).toHaveLength(1);
    expect(controller.snapshot().entries[0].response?.replayed).toBe(true);
  });
  it("sends cancellation without releasing pending state or claiming the remote request has stopped", async () => {
    await ready(); const waiting = deferred<MessageResponse>(); api.send.mockReturnValueOnce(waiting.promise);
    const work = controller.send("问题"); await controller.cancel();
    expect(api.cancel.mock.calls[0][1]).toBe(api.send.mock.calls[0][1]);
    expect(controller.snapshot().phase).toBe("working"); expect(controller.snapshot().pendingId).not.toBeNull();
    expect(controller.snapshot().notice).toContain("仍需等待原请求返回");
    await controller.reset(); expect(api.reset).not.toHaveBeenCalled();
    const cancelled = reply(api.send.mock.calls[0][1]); cancelled.result.code = "turn_cancelled";
    waiting.resolve(cancelled); await work; expect(controller.snapshot().pendingId).toBeNull();
  });
  it("ignores a late cancellation response after the original message completed", async () => {
    await ready(); const work = deferred<MessageResponse>(); const cancel = deferred<void>();
    api.send.mockReturnValueOnce(work.promise); api.cancel.mockReturnValueOnce(cancel.promise);
    const sending = controller.send("问题"); const cancelling = controller.cancel();
    work.resolve(reply(api.send.mock.calls[0][1])); await sending;
    const notice = controller.snapshot().notice; cancel.resolve(); await cancelling;
    expect(controller.snapshot().notice).toBe(notice);
  });
  it("does not replay automatically after request_in_progress or service_busy", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ApiError(409, "request_in_progress"));
    await controller.send("问题"); expect(api.send).toHaveBeenCalledOnce();
    expect(controller.snapshot().phase).toBe("uncertain");
    api.inspect.mockResolvedValueOnce({ ...idle, state: "busy", active_request_id: ID });
    await controller.inspect(); expect(controller.snapshot().notice).toContain("仍在执行");
  });
  it("permits explicit same-id retry after a service-capacity rejection", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ApiError(429, "service_busy"));
    await controller.send("问题"); await controller.retry();
    expect(api.send.mock.calls[1][1]).toBe(api.send.mock.calls[0][1]);
  });
  it("terminal cached HTTP failures do not keep the browser stuck or auto retry", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ApiError(503, "directory_unusable"));
    await controller.send("问题"); expect(controller.snapshot().pendingId).toBeNull();
    expect(controller.snapshot().entries[0].error).toContain("目录"); expect(api.send).toHaveBeenCalledOnce();
  });
  it("an unknown gateway failure retains the original id rather than allowing duplicate research", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ApiError(502, "http_error"));
    await controller.send("问题"); expect(controller.snapshot().phase).toBe("uncertain");
    expect(await controller.send("新问题")).toBe(false); await controller.retry();
    expect(api.send.mock.calls[1][1]).toBe(api.send.mock.calls[0][1]);
  });
  it("invalidates expired session credentials without silently creating another", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ApiError(404, "session_not_found"));
    await controller.send("问题"); expect(controller.snapshot().session).toBeNull();
    expect(controller.snapshot().pendingId).toBeNull(); expect(api.create).toHaveBeenCalledOnce();
  });
  it("keeps a received answer when a later quota refresh fails", async () => {
    await ready(); api.inspect.mockRejectedValueOnce(new ConnectionError());
    await controller.send("问题"); expect(controller.snapshot().entries[0].response?.result.code).toBe("research_completed");
    expect(controller.snapshot().pendingId).toBeNull();
    expect(controller.snapshot().notice).toContain("答复已收到");
  });
  it("history is separate from a failed new research and makes no new send call", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ApiError(500, "research_internal_error"));
    await controller.send("新问题"); await controller.history();
    expect(controller.snapshot().entries[0].error).toBeTruthy(); expect(controller.snapshot().entries[1].history).toBe(true);
    expect(api.send).toHaveBeenCalledOnce(); expect(api.history).toHaveBeenCalledOnce();
  });
  it("reset clears displayed research but uses the server's unchanged quotas", async () => {
    await ready(); await controller.send("问题"); await controller.reset();
    expect(controller.snapshot().entries).toEqual([]); expect(controller.snapshot().session?.view.model_calls_forwarded).toBe(3);
    expect(api.create).toHaveBeenCalledOnce(); expect(controller.snapshot().notice).toContain("额度不退");
  });
  it("ending a session waits for the next explicit message before creating another", async () => {
    await ready(); expect(await controller.end()).toBe(true);
    expect(controller.snapshot().session).toBeNull(); expect(api.end).toHaveBeenCalledOnce();
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
    await controller.send("问题");
    expect(api.create).toHaveBeenCalledTimes(2); expect(api.send).toHaveBeenCalledOnce();
  });
  it("rejects blank and oversized input before creating any message", async () => {
    await ready(); expect(await controller.send(" \n ")).toBe(false); expect(await controller.send("中".repeat(2001))).toBe(false);
    expect(api.send).not.toHaveBeenCalled();
  });
  it("keeps only a bounded number of page entries", async () => {
    await ready(); for (let i = 0; i < 45; i++) await controller.history();
    expect(controller.snapshot().entries).toHaveLength(40);
  });
  it("reports an idle server without losing the original unresolved request", async () => {
    await ready(); api.send.mockRejectedValueOnce(new ConnectionError());
    await controller.send("问题"); const id = controller.snapshot().pendingId; await controller.inspect();
    expect(controller.snapshot().pendingId).toBe(id); expect(controller.snapshot().notice).toContain("重试原请求");
  });
  it("notifies subscribers and allows them to unsubscribe", async () => {
    let calls = 0; const unsubscribe = controller.subscribe(() => calls++);
    await controller.load(); expect(calls).toBeGreaterThan(0); unsubscribe();
    const previous = calls; await controller.start(); expect(calls).toBe(previous);
  });
  it("creates a session and submits the first message without an explicit start", async () => {
    await controller.load();
    expect(await controller.send("  第一条问题  ")).toBe(true);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
    expect(api.send.mock.calls[0][0]).toEqual(credentials);
    expect(api.send.mock.calls[0][2]).toBe("第一条问题");
    expect(controller.snapshot().entries).toHaveLength(1);
    expect(controller.snapshot().phase).toBe("idle");
  });
  it("reuses the same credentials for subsequent messages", async () => {
    await controller.load(); await controller.send("第一个问题"); await controller.send("追问");
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[0][0]).toBe(api.send.mock.calls[1][0]);
    expect(api.send.mock.calls[0][1]).not.toBe(api.send.mock.calls[1][1]);
  });
  it("locks the complete first-send sequence while session creation is waiting", async () => {
    await controller.load(); const creation = deferred<typeof credentials>();
    api.create.mockReturnValueOnce(creation.promise);
    const first = controller.send("第一条");
    expect(controller.snapshot().loading).toBe(true);
    expect(await controller.send("第二条")).toBe(false);
    await controller.start(); expect(await controller.end()).toBe(false);
    await controller.load(); await controller.reset();
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
    expect(api.end).not.toHaveBeenCalled(); expect(api.reset).not.toHaveBeenCalled();
    creation.resolve(credentials); await first;
    expect(api.send).toHaveBeenCalledOnce(); expect(api.send.mock.calls[0][2]).toBe("第一条");
  });
  it.each([new ConnectionError(), new ApiError(429, "session_capacity")])("does not submit or auto-retry after a creation failure %s", async (error) => {
    await controller.load(); api.create.mockRejectedValueOnce(error);
    expect(await controller.send("保留的问题")).toBe(false);
    expect(controller.snapshot().session).toBeNull(); expect(controller.snapshot().entries).toEqual([]);
    expect(controller.snapshot().pendingId).toBeNull(); expect(controller.snapshot().loading).toBe(false);
    expect(controller.snapshot().notice).toContain("问题尚未发送");
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
    expect(await controller.send("保留的问题")).toBe(true);
    expect(api.create).toHaveBeenCalledTimes(2); expect(api.send).toHaveBeenCalledOnce();
  });
  it.each([{ provider: "deepseek" as const }, { data_mode: "tencent" as const }])("does not send the first message after a creation mode mismatch %j", async (change) => {
    await controller.load(); api.create.mockResolvedValueOnce({ ...credentials, ...change });
    expect(await controller.send("问题")).toBe(false);
    expect(controller.snapshot().session).toBeNull(); expect(api.send).not.toHaveBeenCalled();
  });
  it("never starts live-provider research merely by loading or creating an idle session", async () => {
    api.health.mockResolvedValue({ ...health, provider: "deepseek" });
    api.create.mockResolvedValue({ ...credentials, provider: "deepseek" });
    await controller.load();
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    await controller.start();
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
    await controller.send("问题");
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
  });
  it("waits for matching live-provider credentials before sending the first explicit message", async () => {
    api.health.mockResolvedValue({ ...health, provider: "deepseek" });
    const creation = deferred<typeof credentials>(); api.create.mockReturnValueOnce(creation.promise);
    await controller.load();
    const first = controller.send("问题");
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).not.toHaveBeenCalled();
    expect(await controller.send("并发问题")).toBe(false);
    creation.resolve({ ...credentials, provider: "deepseek" });
    expect(await first).toBe(true);
    expect(controller.snapshot().session).not.toBeNull(); expect(controller.snapshot().entries).toHaveLength(1);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
    expect(api.send.mock.calls[0][2]).toBe("问题");
  });
  it("rejects invalid input and an unavailable service before creating a session", async () => {
    expect(await controller.send("问题")).toBe(false);
    await controller.load();
    expect(await controller.send(" \n ")).toBe(false);
    expect(await controller.send("中".repeat(2001))).toBe(false);
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("does not create or delete a server session when clearing an empty chat", async () => {
    await controller.load(); expect(await controller.end()).toBe(true);
    expect(api.create).not.toHaveBeenCalled(); expect(api.end).not.toHaveBeenCalled();
  });
  it("keeps the current chat when server cleanup is unconfirmed", async () => {
    await controller.load(); await controller.send("原问题");
    api.end.mockRejectedValueOnce(new ConnectionError());
    expect(await controller.end()).toBe(false);
    expect(controller.snapshot().session).not.toBeNull(); expect(controller.snapshot().entries).toHaveLength(1);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
  });
  it.each([401, 404])("can explicitly discard a session already unavailable with HTTP %i", async (status) => {
    await controller.load(); await controller.send("原问题");
    api.end.mockRejectedValueOnce(new ApiError(status, "session_not_found"));
    expect(await controller.end()).toBe(true);
    expect(controller.snapshot().session).toBeNull(); expect(controller.snapshot().entries).toEqual([]);
    expect(api.create).toHaveBeenCalledOnce();
  });
  it("does not start a new session to bypass the current message quota", async () => {
    await controller.load(); api.inspect.mockResolvedValueOnce({ ...idle, messages_used: credentials.limits.messages });
    await controller.send("最后一个问题"); expect(await controller.send("额度外的问题")).toBe(false);
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send).toHaveBeenCalledOnce();
  });
  it("keeps the original session and request id after the first message response is lost", async () => {
    await controller.load(); api.send.mockRejectedValueOnce(new ConnectionError());
    await controller.send("第一条问题"); const original = api.send.mock.calls[0];
    expect(await controller.send("新的问题")).toBe(false);
    await controller.retry();
    expect(api.create).toHaveBeenCalledOnce(); expect(api.send.mock.calls[1]).toEqual(original);
  });
});

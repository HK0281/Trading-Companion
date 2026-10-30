import { describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionError, createApi } from "../src/api";
import { credentials, health, horizonReply, ID, idle, reply } from "./fixtures";

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { "Content-Type": "application/json" } });

describe("same-origin HTTP client", () => {
  it("accepts horizon choices and old responses without choices", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(horizonReply()))
      .mockResolvedValueOnce(response(reply()));
    const api = createApi(fetcher);
    expect((await api.send(credentials, ID, "问题")).result.clarification_options).toHaveLength(3);
    expect((await api.send(credentials, ID, "问题")).result.clarification_options).toBeUndefined();
  });
  it.each(["reply", "order", "expiry", "extra"])("rejects malformed horizon %s", async kind => {
    const payload = horizonReply();
    if (kind === "reply") payload.result.clarification_options![0].reply = "任意消息";
    if (kind === "order") payload.result.clarification_options!.reverse();
    if (kind === "expiry") payload.result.clarification_expires_at = "invalid";
    if (kind === "extra") Object.assign(payload.result.clarification_options![0], { arbitrary: true });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(payload));
    await expect(createApi(fetcher).send(credentials, ID, "问题")).rejects.toThrow(ConnectionError);
  });
  it("loads only a research-bound chart using bearer auth and forwards cancellation", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ version: "research-chart-interval-v1", status: "unavailable", code: "chart_timeout" }));
    const abort = new AbortController();
    await createApi(fetcher).chart!(credentials, "report-fixture", "60m", abort.signal);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe(`/v1/sessions/${credentials.session_id}/chart?research_id=report-fixture&interval=60m`);
    expect(options?.headers).toEqual({ Authorization: `Bearer ${credentials.access_token}` });
    expect(options?.body).toBeUndefined(); expect(fetcher).toHaveBeenCalledOnce();
  });
  it("bounds chart response bytes and makes no automatic retry", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ version: "research-chart-interval-v1", status: "available", extra: "x".repeat(256 * 1024) }));
    await expect(createApi(fetcher).chart!(credentials, "report-fixture", "1w")).rejects.toThrow(ConnectionError);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("loads health without cookies or bearer credentials", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(health));
    expect(await createApi(fetcher).health()).toEqual(health);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0][0]).toBe("/health");
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: "omit", cache: "no-store", redirect: "error", headers: {} });
  });
  it("sends only the question and stable id, with the capability in a header", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(reply()));
    await createApi(fetcher).send(credentials, ID, "分析平安银行");
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe(`/v1/sessions/${credentials.session_id}/messages`);
    expect(options?.body).toBe(JSON.stringify({ request_id: ID, question: "分析平安银行" }));
    expect(options?.headers).toEqual({ "Content-Type": "application/json", Authorization: `Bearer ${credentials.access_token}` });
    expect(String(url) + options?.body).not.toContain(credentials.access_token);
  });
  it("does not retry a network failure or leak its exception", async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error("private-socket-details"));
    await expect(createApi(fetcher).send(credentials, ID, "问题")).rejects.toThrow(ConnectionError);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("keeps confirmed text responses when an optional chart is invalid", async () => {
    const value = reply(); value.result.chart = { status: "available", bars: "invalid" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(value));
    expect(await createApi(fetcher).send(credentials, ID, "问题")).toEqual(value);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("does not treat malformed optional market context as an unconfirmed response", async () => {
    const value = reply(); value.result.market_context = { security: { toString: 0 }, request_id: "wrong" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(value));
    expect(await createApi(fetcher).send(credentials, ID, "问题")).toEqual(value);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("keeps the confirmed answer when optional workflow records are invalid", async () => {
    const value = reply(); value.result.research_workflow = { steps: "invalid", request_id: "wrong" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(value));
    expect(await createApi(fetcher).send(credentials, ID, "问题")).toEqual(value);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("maps server codes without displaying untrusted messages", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ error: { code: "service_busy", message: "private-key-details" } }, 429));
    await expect(createApi(fetcher).send(credentials, ID, "问题")).rejects.toThrow("服务的并行研究数已满");
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("treats malformed success or wrong request id as an unconfirmed response", async () => {
    const wrong = reply("c".repeat(32));
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(wrong)).mockResolvedValueOnce(response({ result: "raw draft" }));
    const api = createApi(fetcher);
    await expect(api.send(credentials, ID, "问题")).rejects.toThrow(ConnectionError);
    await expect(api.send(credentials, ID, "问题")).rejects.toThrow(ConnectionError);
  });
  it("refuses incompatible service modes and enabled trading", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...health, provider: "unknown" }))
      .mockResolvedValueOnce(response({ ...health, trading_enabled: true }));
    const api = createApi(fetcher);
    await expect(api.health()).rejects.toThrow(ConnectionError);
    await expect(api.health()).rejects.toThrow(ConnectionError);
  });
  it("creates a session only with an empty JSON body", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(credentials, 201));
    expect(await createApi(fetcher).create()).toEqual(credentials);
    expect(fetcher.mock.calls[0][1]?.body).toBe("{}");
  });
  it("supports history, reset, cancellation and delete without inventing new questions", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(reply()))
      .mockResolvedValueOnce(response({ session: idle })).mockResolvedValueOnce(response({ request_id: ID, status: "cancellation_requested" }, 202))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const api = createApi(fetcher);
    await api.history(credentials); await api.reset(credentials); await api.cancel(credentials, ID); await api.end(credentials);
    expect(fetcher.mock.calls.map((call) => call[1]?.method)).toEqual(["GET", "POST", "POST", "DELETE"]);
    expect(fetcher.mock.calls[2][1]?.body).toBe(JSON.stringify({ request_id: ID }));
  });
  it("aborts a timed-out browser wait without calling the cancellation endpoint", async () => {
    vi.useFakeTimers();
    try {
      const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, options) => new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new Error("timeout")));
      }));
      const result = createApi(fetcher).send(credentials, ID, "问题");
      const assertion = expect(result).rejects.toThrow(ConnectionError);
      await vi.advanceTimersByTimeAsync(120_000);
      await assertion;
      expect(fetcher).toHaveBeenCalledOnce();
      expect(fetcher.mock.calls[0][0]).not.toContain("cancel");
    } finally { vi.useRealTimers(); }
  });
  it("rejects unsafe numeric fields and does not render raw debug exports", async () => {
    const malformed = reply(); malformed.result.report!.fields[0].value = Infinity;
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ ...malformed,
      result: { ...malformed.result, report: { ...malformed.result.report, fields: [{ ...malformed.result.report!.fields[0], value: "NaN" }] } } }));
    await expect(createApi(fetcher).send(credentials, ID, "问题")).rejects.toThrow(ConnectionError);
  });
  it("handles non-JSON HTTP failures without displaying their body", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("<script>private</script>", { status: 502 }));
    await expect(createApi(fetcher).send(credentials, ID, "问题")).rejects.toEqual(new ApiError(502, "http_error"));
  });
});

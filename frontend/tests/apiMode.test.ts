import { describe, expect, it, vi } from "vitest";
import { ConnectionError, createApi, type ApiMode } from "../src/api";
import { credentials, health, ID, idle, reply } from "./fixtures";

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { "Content-Type": "application/json" } });

describe("endpoint-bound API clients", () => {
  it("accepts only the two named modes, never an arbitrary prefix or external URL", () => {
    const fetcher = vi.fn<typeof fetch>();
    for (const mode of ["https://other.example", "/dev-api", "../", "auto"])
      expect(() => createApi(fetcher, mode as ApiMode)).toThrow(ConnectionError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([true, false, undefined])("preserves the optional root capability %s", async (capability) => {
    const value = capability === undefined ? health : { ...health, development_mode_available: capability };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(value));
    expect(await createApi(fetcher).health()).toEqual(value);
    expect(fetcher.mock.calls[0][0]).toBe("/health");
  });

  it.each(["true", 1, null, {}])("rejects a nonboolean development capability %j", async (capability) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ ...health, development_mode_available: capability }));
    await expect(createApi(fetcher).health()).rejects.toThrow(ConnectionError);
  });

  it("pins all development endpoints and sends bearer credentials only in headers", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(credentials, 201))
      .mockResolvedValueOnce(response(idle))
      .mockResolvedValueOnce(response(reply()))
      .mockResolvedValueOnce(response(reply()))
      .mockResolvedValueOnce(response({ request_id: ID, status: "cancellation_requested" }, 202))
      .mockResolvedValueOnce(response({ session: idle }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const api = createApi(fetcher, "development");
    expect(api.mode).toBe("development");
    await api.health(); const session = await api.create();
    await api.inspect(session); await api.send(session, ID, "分析平安银行"); await api.history(session);
    await api.cancel(session, ID); await api.reset(session); await api.end(session);
    const base = `/dev-api/v1/sessions/${session.session_id}`;
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      "/dev-api/health", "/dev-api/v1/sessions", base, base + "/messages", base + "/result",
      base + "/cancel", base + "/reset", base,
    ]);
    expect(fetcher.mock.calls[3][1]?.body).toBe(JSON.stringify({ request_id: ID, question: "分析平安银行" }));
    expect(fetcher.mock.calls[3][1]?.headers).toEqual({ "Content-Type": "application/json", Authorization: `Bearer ${session.access_token}` });
    for (const [url, options] of fetcher.mock.calls)
      expect(String(url) + (options?.body ?? "")).not.toContain(session.access_token);
    expect(session).not.toHaveProperty("mode");
  });

  it("blocks credentials created by the other endpoint before making any network call", async () => {
    const normalFetcher = vi.fn<typeof fetch>().mockResolvedValue(response(credentials, 201));
    const developmentFetcher = vi.fn<typeof fetch>().mockResolvedValue(response(credentials, 201));
    const normal = createApi(normalFetcher), development = createApi(developmentFetcher, "development");
    const normalSession = await normal.create(), developmentSession = await development.create();
    await expect(development.send(normalSession, ID, "跨模式请求")).rejects.toThrow(ConnectionError);
    await expect(normal.send(developmentSession, ID, "跨模式请求")).rejects.toThrow(ConnectionError);
    await expect(development.end(normalSession)).rejects.toThrow(ConnectionError);
    await expect(normal.inspect(developmentSession)).rejects.toThrow(ConnectionError);
    expect(normalFetcher).toHaveBeenCalledOnce(); expect(developmentFetcher).toHaveBeenCalledOnce();
  });

  it("does not accept unbound manual credentials on the development endpoint", async () => {
    const fetcher = vi.fn<typeof fetch>(), api = createApi(fetcher, "development");
    await expect(api.send({ ...credentials }, ID, "问题")).rejects.toThrow(ConnectionError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    { provider: "deepseek" }, { data_mode: "tencent" }, { data_mode: "akshare_eastmoney" },
  ])("rejects a nonoffline service from the development endpoint %j", async (change) => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response({ ...health, ...change }))
      .mockResolvedValueOnce(response({ ...credentials, ...change }, 201));
    const api = createApi(fetcher, "development");
    await expect(api.health()).rejects.toThrow(ConnectionError);
    await expect(api.create()).rejects.toThrow(ConnectionError);
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual(["/dev-api/health", "/dev-api/v1/sessions"]);
  });
});

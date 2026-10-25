import { describe, expect, it, vi } from "vitest";
import { ConnectionError, createApi, type LiveProvider } from "../src/api";
import { credentials, health, ID } from "./fixtures";
import { providerHealth } from "./providerFixtures";

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { "Content-Type": "application/json" } });

describe("configured session models", () => {
  it("accepts OpenAI and keeps the default separate from the available choices", async () => {
    const value = { ...providerHealth, provider: "openai" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(value));
    expect(await createApi(fetcher).health()).toEqual(value);
  });

  it.each([null, "deepseek", [], ["openai"], ["deepseek", "unknown"], ["deepseek", "deepseek"]])(
    "rejects an unusable provider capability %j", async (available) => {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...providerHealth, available_providers: available }));
      await expect(createApi(fetcher).health()).rejects.toThrow(ConnectionError);
    });

  it.each(["deepseek", "openai"] as const)("binds the requested %s provider when creating a normal session", async (provider) => {
    const value = { ...credentials, provider, data_mode: "tencent" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(value, 201));
    expect(await createApi(fetcher).create(provider)).toEqual(value);
    expect(fetcher.mock.calls[0][0]).toBe("/v1/sessions");
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ provider });
  });

  it("preserves an empty creation body for callers using the service default", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(credentials, 201));
    await createApi(fetcher).create();
    expect(fetcher.mock.calls[0][1]?.body).toBe("{}");
  });

  it("rejects a silently substituted model instead of accepting its credentials", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...credentials, provider: "deepseek" }, 201));
    await expect(createApi(fetcher).create("openai")).rejects.toThrow(ConnectionError);
  });

  it("never sends a real provider choice to a development endpoint", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const development = createApi(fetcher, "development");
    await expect(development.create("deepseek")).rejects.toThrow(ConnectionError);
    await expect(development.create("openai")).rejects.toThrow(ConnectionError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects unsupported choices before a request is made", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(createApi(fetcher).create("fake" as LiveProvider)).rejects.toThrow(ConnectionError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not treat a development capability as permission to use a real provider", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...health, available_providers: ["fake", "openai"] }));
    await expect(createApi(fetcher, "development").health()).rejects.toThrow(ConnectionError);
  });

  it("keeps OpenAI session credentials bound to the normal endpoint", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...credentials, provider: "openai", data_mode: "tencent" }, 201));
    const session = await createApi(fetcher).create("openai");
    const devFetcher = vi.fn<typeof fetch>();
    await expect(createApi(devFetcher, "development").send(session, ID, "研究平安银行")).rejects.toThrow(ConnectionError);
    expect(devFetcher).not.toHaveBeenCalled();
  });
});

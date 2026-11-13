import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionError, createApi, parsePersonalMemory, type Health, type PersonalMemoryResult,
  type Watchlist, type WatchlistItem, type WatchlistSearch } from "../src/api";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { WatchlistActions } from "../src/Watchlist";
import { credentials, deferred, health, idle, mockApi, reply } from "./fixtures";

const VERSION = "personal-memory-watchlist-v1";
const stock: WatchlistItem = { instrument_id: "000001.SZ", name: "平安银行", created_at: "2026-11-12T08:00:00+00:00", updated_at: "2026-11-12T08:00:00+00:00" };
const list = (items: WatchlistItem[] = [], enabled = true): Watchlist => ({ version: VERSION, enabled, limit: 50, items });
const search = (candidates = [{ instrument_id: stock.instrument_id, name: stock.name }]): WatchlistSearch =>
  ({ version: VERSION, enabled: true, candidates, truncated: false });
const personal = (action: PersonalMemoryResult["action"] = "list", candidates: PersonalMemoryResult["candidates"] = []): PersonalMemoryResult =>
  ({ version: VERSION, action, status: candidates.length ? "needs_confirmation" : "completed", items: [], candidates });
const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const normalHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent", personal_memory_enabled: true, development_mode_available: true };

function personalController(enabled = true) {
  let current = list([], enabled);
  const api = { ...mockApi(), mode: "normal" as const,
    health: vi.fn(async (): Promise<Health> => ({ ...normalHealth, personal_memory_enabled: enabled })),
    create: vi.fn(async () => ({ ...credentials, provider: "deepseek" as const, data_mode: "tencent" as const })),
    watchlist: vi.fn(async () => structuredClone(current)),
    searchWatchlist: vi.fn(async (_query: string) => search()),
    addWatchlist: vi.fn(async (_id: string) => { current = list([stock]); return structuredClone(current); }),
    removeWatchlist: vi.fn(async (_id: string) => { current = list(); return structuredClone(current); }),
    clearWatchlist: vi.fn(async () => { current = list(); return structuredClone(current); }),
    deleteConversation: vi.fn(async (_id: string) => {}),
  };
  const development = { ...mockApi(), mode: "development" as const };
  let next = 0;
  return { api, development, controller: new ResearchController(api, () => (++next).toString(16).padStart(32, "0"), development),
    setList: (value: Watchlist) => { current = value; } };
}

describe("personal watchlist HTTP contracts", () => {
  it("accepts ordinary messages with the new optional field set to null", async () => {
    const value = reply(); value.result.personal_memory = null;
    const api = createApi(vi.fn<typeof fetch>().mockResolvedValue(response(value)));
    expect(await api.send(credentials, value.request_id, "研究平安银行")).toEqual(value);
  });
  it("uses only local personal endpoints without creating sessions or forwarding bearer tokens", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(list([stock])))
      .mockResolvedValueOnce(response(search()))
      .mockResolvedValueOnce(response(list([stock])))
      .mockResolvedValueOnce(response(list()))
      .mockResolvedValueOnce(response(list()));
    const api = createApi(fetcher);
    expect(await api.watchlist!()).toEqual(list([stock]));
    expect(await api.searchWatchlist!("平安银行")).toEqual(search());
    await api.addWatchlist!(stock.instrument_id);
    await api.removeWatchlist!(stock.instrument_id);
    await api.clearWatchlist!();
    expect(fetcher.mock.calls.map(call => call[0])).toEqual([
      "/v1/personal/watchlist", `/v1/personal/watchlist/search?q=${encodeURIComponent("平安银行")}`,
      "/v1/personal/watchlist", "/v1/personal/watchlist/000001.SZ", "/v1/personal/watchlist",
    ]);
    expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({ instrument_id: stock.instrument_id });
    expect(fetcher.mock.calls[3][1]?.method).toBe("DELETE");
    expect(fetcher.mock.calls[4][1]?.method).toBe("DELETE");
    for (const [, options] of fetcher.mock.calls) {
      expect(options?.headers).not.toHaveProperty("Authorization");
      expect(options?.cache).toBe("no-store");
    }
    expect(() => api.addWatchlist!("../secret")).toThrow(ConnectionError);
    expect(() => api.searchWatchlist!(" ")).toThrow(ConnectionError);
    expect(fetcher).toHaveBeenCalledTimes(5);
  });

  it.each([
    list([stock, stock]), list([stock], false), { ...list(), limit: 51 }, { ...list(), extra: true },
    list([{ ...stock, instrument_id: "000001" }]), list([{ ...stock, created_at: "yesterday" }]),
    list([{ ...stock, updated_at: "2026-11-11T08:00:00Z" }]), list([{ ...stock, name: "" }]),
    list(Array.from({ length: 51 }, (_, index) => ({ ...stock, instrument_id: `${index.toString().padStart(6, "0")}.SZ` }))),
  ])("rejects malformed or over-capacity personal lists %#", async value => {
    const api = createApi(vi.fn<typeof fetch>().mockResolvedValue(response(value)));
    await expect(api.watchlist!()).rejects.toThrow(ConnectionError);
  });

  it("validates bounded search responses and management candidate replies", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response({ ...search(), candidates: [...search().candidates, ...search().candidates] }))
      .mockResolvedValueOnce(response({ ...search(), candidates: [{ ...search().candidates[0], extra: true }] }))
      .mockResolvedValueOnce(response({ ...normalHealth, personal_memory_enabled: "true" }));
    const api = createApi(fetcher);
    await expect(api.searchWatchlist!("银行")).rejects.toThrow(ConnectionError);
    await expect(api.searchWatchlist!("银行")).rejects.toThrow(ConnectionError);
    await expect(api.health()).rejects.toThrow(ConnectionError);
    const candidate = { instrument_id: stock.instrument_id, name: stock.name, reply: "把000001.SZ加入自选" };
    expect(parsePersonalMemory(personal("add", [candidate]))).toEqual(personal("add", [candidate]));
    expect(parsePersonalMemory(personal("remove", [{ ...candidate, reply: "从自选移除000001.SZ" }]))).not.toBeNull();
    expect(parsePersonalMemory(personal("add", [{ ...candidate, reply: "000001.SZ" }]))).toBeNull();
    expect(parsePersonalMemory(personal("remove", [candidate]))).toBeNull();
    expect(parsePersonalMemory({ ...personal(), status: "done" })).toBeNull();
    expect(parsePersonalMemory({ ...personal(), action: ["add"] })).toBeNull();
    expect(parsePersonalMemory({ ...personal(), status: ["completed"] })).toBeNull();
    expect(parsePersonalMemory({ ...personal(), private_data: "secret" })).toBeNull();
    expect(parsePersonalMemory({ ...personal(), items: [{ ...stock, extra: true }] })).toBeNull();
  });

  it("blocks development access before any personal request", async () => {
    const fetcher = vi.fn<typeof fetch>(), api = createApi(fetcher, "development");
    expect(await api.watchlist!()).toEqual(list([], false));
    expect(() => api.searchWatchlist!("平安银行")).toThrow(ApiError);
    expect(() => api.addWatchlist!(stock.instrument_id)).toThrow(ApiError);
    expect(() => api.removeWatchlist!(stock.instrument_id)).toThrow(ApiError);
    expect(() => api.clearWatchlist!()).toThrow(ApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects management responses that could fall through to bare-code research candidates", async () => {
    const value = reply(); value.result.personal_memory = personal("add", [{ instrument_id: stock.instrument_id, name: stock.name, reply: "把000001.SZ加入自选" }]);
    const api = createApi(vi.fn<typeof fetch>().mockResolvedValue(response(value)));
    await expect(api.send(credentials, value.request_id, "把银行加入自选")).rejects.toThrow(ConnectionError);
  });
});

describe("watchlist state and research isolation", () => {
  it("loads without research and keeps personal data across reset, new chat and conversation deletion", async () => {
    const { api, controller } = personalController();
    await controller.load();
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    expect(await controller.addWatchlist(stock.instrument_id)).toBe(true);
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    await controller.send("研究平安银行");
    await controller.reset(); await controller.end(); await controller.deleteConversation("c".repeat(32));
    expect(controller.snapshot().watchlist?.items).toEqual([stock]);
    expect(api.removeWatchlist).not.toHaveBeenCalled(); expect(api.clearWatchlist).not.toHaveBeenCalled();
  });

  it("keeps the panel usable after the research message quota is exhausted", async () => {
    const { api, controller } = personalController();
    api.inspect.mockResolvedValue({ ...idle, messages_used: credentials.limits.messages });
    await controller.load(); await controller.send("研究平安银行");
    expect(await controller.send("查看自选")).toBe(false);
    expect(await controller.addWatchlist(stock.instrument_id)).toBe(true);
    expect(api.send).toHaveBeenCalledOnce();
    expect(controller.snapshot().watchlist?.items).toEqual([stock]);
  });

  it("discards a list read that began before a completed write", async () => {
    const { api, controller } = personalController();
    await controller.load();
    const older = deferred<Watchlist>(); api.watchlist.mockReturnValueOnce(older.promise);
    const refresh = controller.refreshWatchlist();
    await controller.addWatchlist(stock.instrument_id);
    older.resolve(list()); await refresh;
    expect(controller.snapshot().watchlist?.items).toEqual([stock]);
    expect(controller.snapshot().watchlistLoading).toBe(false);
  });

  it("discards old searches and late personal results across development switches", async () => {
    const { api, controller, setList } = personalController();
    setList(list([stock])); await controller.load();
    const older = deferred<WatchlistSearch>(); api.searchWatchlist.mockReturnValueOnce(older.promise);
    const pendingSearch = controller.searchWatchlist("银行");
    controller.clearWatchlistSearch();
    await controller.searchWatchlist("平安银行");
    older.resolve(search([{ instrument_id: "600000.SH", name: "浦发银行" }])); await pendingSearch;
    expect(controller.snapshot().watchlistSearch?.candidates).toEqual(search().candidates);
    const delayed = deferred<Watchlist>(); api.watchlist.mockReturnValueOnce(delayed.promise);
    const refresh = controller.refreshWatchlist();
    await controller.switchMode("development");
    expect(controller.snapshot().watchlist?.items ?? []).toEqual([]);
    expect(controller.snapshot().watchlistSearch).toBeNull();
    delayed.resolve(list([stock])); await refresh;
    expect(controller.snapshot().watchlist?.items ?? []).toEqual([]);
    await controller.switchMode("normal");
    await vi.waitFor(() => expect(controller.snapshot().watchlist?.items).toEqual([stock]));
  });

  it("distinguishes disabled and failed reads, retaining the last confirmed list on failure", async () => {
    const disabled = personalController(false); await disabled.controller.load();
    expect(disabled.api.watchlist).not.toHaveBeenCalled();
    expect(disabled.controller.snapshot().watchlist?.enabled).toBe(false);
    const { api, controller, setList } = personalController(); setList(list([stock])); await controller.load();
    api.watchlist.mockRejectedValueOnce(new ApiError(503, "personal_memory_store_unavailable"));
    await controller.refreshWatchlist();
    expect(controller.snapshot().watchlist?.items).toEqual([stock]);
    expect(controller.snapshot().watchlistError).toContain("读取失败");
    api.addWatchlist.mockRejectedValueOnce(new ApiError(409, "watchlist_full"));
    expect(await controller.addWatchlist("600000.SH")).toBe(false);
    expect(controller.snapshot().watchlistError).toContain("50 只");
    expect(controller.snapshot().watchlistNotice).toBeNull();
  });

  it("does not reintroduce personal data when a write finishes after entering development mode", async () => {
    const { api, controller } = personalController(); await controller.load();
    const delayed = deferred<Watchlist>(); api.addWatchlist.mockReturnValueOnce(delayed.promise);
    const writing = controller.addWatchlist(stock.instrument_id);
    expect(controller.snapshot().watchlistSaving).toBe(true);
    await controller.switchMode("development");
    delayed.resolve(list([stock])); await writing;
    expect(controller.snapshot().watchlist?.items ?? []).toEqual([]);
    expect(controller.snapshot().watchlistNotice).toBeNull();
    expect(controller.snapshot().watchlistSaving).toBe(false);
  });

  it("refreshes after replayed management receipts instead of copying their saved list", async () => {
    const { api, controller, setList } = personalController(); setList(list([stock]));
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = reply(id); value.replayed = true; value.result.personal_memory = personal(); value.result.candidates = [];
      return value;
    });
    await controller.load(); await controller.send("查看自选");
    await vi.waitFor(() => expect(api.watchlist).toHaveBeenCalledTimes(2));
    expect(controller.snapshot().watchlist?.items).toEqual([stock]);
  });
});

describe("watchlist controls", () => {
  it.each(["slow", "failed"])("does not label an unconfirmed %s list as absent when searching and updates the accessible label after refresh", async condition => {
    const { api, controller, setList } = personalController();
    const first = deferred<Watchlist>();
    if (condition === "slow") api.watchlist.mockReturnValueOnce(first.promise);
    else api.watchlist.mockRejectedValueOnce(new ApiError(503, "personal_memory_store_unavailable"));
    render(<App controller={controller} />);
    const panel = screen.getByRole("region", { name: "自选股" });
    const query = await within(panel).findByRole("textbox", { name: "股票名称或完整代码" });
    fireEvent.change(query, { target: { value: stock.name } });
    fireEvent.click(within(panel).getByRole("button", { name: "搜索" }));
    expect(await within(panel).findByRole("button", { name: "自选状态待确认：平安银行（000001.SZ）" })).toBeDisabled();
    expect(api.addWatchlist).not.toHaveBeenCalled();
    if (condition === "slow") await act(async () => { first.resolve(list([stock])); });
    else { setList(list([stock])); await act(async () => { await controller.refreshWatchlist(); }); }
    expect(await within(panel).findByRole("button", { name: "已在自选：平安银行（000001.SZ）" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "已在自选：平安银行（000001.SZ）" })).toHaveTextContent("已在自选");
  });

  it("disables result-card list actions when the current list refresh failed", async () => {
    const { api, controller, setList } = personalController(); setList(list([stock])); await controller.load();
    api.watchlist.mockRejectedValueOnce(new ApiError(503, "personal_memory_store_unavailable")); await controller.refreshWatchlist();
    render(<WatchlistActions securities={[stock]} state={controller.snapshot()} controller={controller} />);
    const button = screen.getByRole("button", { name: "自选状态待确认：平安银行（000001.SZ）" });
    expect(button).toBeDisabled(); fireEvent.click(button); expect(api.removeWatchlist).not.toHaveBeenCalled();
  });

  it("shows the saved receipt list in chat while leaving the independently refreshed sidebar current", async () => {
    const { api, controller } = personalController();
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = reply(id); value.replayed = true;
      Object.assign(value.result, { personal_memory: { ...personal(), items: [stock] }, candidates: [], answer: "已读取本机自选。", parameters: null, report: null });
      return value;
    });
    render(<App controller={controller} />);
    await screen.findByRole("textbox", { name: "股票名称或完整代码" });
    await act(async () => { await controller.send("查看自选"); });
    const receipt = screen.getByLabelText("自选管理记录");
    expect(within(receipt).getByText("请求时的自选清单：")).toBeVisible();
    expect(within(receipt).getByText(stock.instrument_id)).toBeVisible();
    expect(within(receipt).getByText(stock.name, { exact: false })).toBeVisible();
    expect(screen.getByText("这是原请求的处理结果；当前自选请看侧栏或刷新。")).toBeVisible();
    const panel = screen.getByRole("region", { name: "自选股" });
    expect(within(panel).queryByText(stock.instrument_id)).not.toBeInTheDocument();
    expect(controller.snapshot().watchlist?.items).toEqual([]);
  });

  it("searches, adds and confirms remove and clear without creating a research session", async () => {
    const { api, controller } = personalController(); render(<App controller={controller} />);
    const panel = await screen.findByRole("region", { name: "自选股" });
    await within(panel).findByRole("textbox", { name: "股票名称或完整代码" });
    fireEvent.change(within(panel).getByRole("textbox"), { target: { value: "平安银行" } });
    fireEvent.click(within(panel).getByRole("button", { name: "搜索" }));
    fireEvent.click(await within(panel).findByRole("button", { name: "加入自选：平安银行（000001.SZ）" }));
    await vi.waitFor(() => expect(api.addWatchlist).toHaveBeenCalledWith(stock.instrument_id));
    const remove = await within(panel).findByRole("button", { name: "移除自选：平安银行" });
    await vi.waitFor(() => expect(remove).not.toBeDisabled());
    fireEvent.click(remove); expect(api.removeWatchlist).not.toHaveBeenCalled();
    fireEvent.click(within(panel).getByRole("button", { name: "确认移除" }));
    await vi.waitFor(() => expect(api.removeWatchlist).toHaveBeenCalledWith(stock.instrument_id));
    await vi.waitFor(() => expect(controller.snapshot().watchlistSaving).toBe(false));
    await act(async () => { await controller.addWatchlist(stock.instrument_id); });
    fireEvent.click(within(panel).getByRole("button", { name: "清空自选" }));
    expect(api.clearWatchlist).not.toHaveBeenCalled();
    expect(within(panel).getByText("清空全部自选股？聊天历史不会删除。")).toBeVisible();
    fireEvent.click(within(panel).getByRole("button", { name: "确认清空自选" }));
    await vi.waitFor(() => expect(api.clearWatchlist).toHaveBeenCalledOnce());
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });

  it("offers the same confirmed removal from validated company-result securities", async () => {
    const { api, controller, setList } = personalController(); setList(list([stock])); await controller.load();
    render(<WatchlistActions securities={[stock]} state={controller.snapshot()} controller={controller} />);
    fireEvent.click(screen.getByRole("button", { name: "移除自选：平安银行（000001.SZ）" }));
    expect(api.removeWatchlist).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await vi.waitFor(() => expect(api.removeWatchlist).toHaveBeenCalledWith(stock.instrument_id));
    expect(api.send).not.toHaveBeenCalled();
  });

  it("sends complete management candidates even when the model-call allowance is exhausted", async () => {
    const { api, controller } = personalController();
    api.inspect.mockResolvedValue({ ...idle, model_calls_forwarded: credentials.limits.model_calls });
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = reply(id);
      Object.assign(value.result, { status: "needs_clarification", code: "personal_memory_needs_confirmation", answer: "请选完整代码。",
        parameters: null, report: null, candidates: [], personal_memory: personal("add", [{ instrument_id: stock.instrument_id, name: stock.name, reply: "把000001.SZ加入自选" }]) });
      return value;
    });
    render(<App controller={controller} />);
    await screen.findByRole("textbox", { name: "股票名称或完整代码" });
    await act(async () => { await controller.send("把银行加入自选"); });
    const candidates = screen.getByLabelText("自选管理候选股票");
    fireEvent.click(within(candidates).getByRole("button", { name: "平安银行 000001.SZ" }));
    await vi.waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[1][2]).toBe("把000001.SZ加入自选");
  });

  it("explains the opt-in setting when personal memory is disabled", async () => {
    const { controller } = personalController(false); render(<App controller={controller} />);
    expect(await screen.findByText(/自选股未开启。请用 --personal-memory-db/)).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "股票名称或完整代码" })).not.toBeInTheDocument();
  });
});

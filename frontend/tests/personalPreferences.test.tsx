import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionError, createApi, parsePersonalMemory, type Health, type PreferenceChanges, type PreferenceRecord,
  type Preferences, type PreferencesMemoryResult, type Watchlist, type WatchlistSearch } from "../src/api";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { PersonalPreferenceHint } from "../src/PersonalPreferences";
import { credentials, deferred, health, idle, mockApi, reply } from "./fixtures";

const REV = "a".repeat(32), NEXT = "b".repeat(32), COLLECTION = "c".repeat(64);
const stock = { instrument_id: "000001.SZ", name: "平安银行" };
const record: PreferenceRecord = { ...stock, holding_status: "held", horizon: "long", risk: "low",
  created_at: "2026-10-01T08:00:00+00:00", updated_at: "2026-11-12T08:00:00+00:00", revision: REV };
const prefs = (items: PreferenceRecord[] = [record], enabled = true): Preferences =>
  ({ version: "personal-memory-preferences-v1", enabled, limit: 50, items, collection_revision: enabled ? COLLECTION : null });
const management = (items: PreferenceRecord[] = [record]): PreferencesMemoryResult =>
  ({ version: "personal-memory-preferences-v1", action: "list", status: "completed", items, candidates: [] });
const search: WatchlistSearch = { version: "personal-memory-watchlist-v1", enabled: true, candidates: [stock], truncated: false };
const watchlist: Watchlist = { version: "personal-memory-watchlist-v1", enabled: true, limit: 50,
  items: [{ ...stock, created_at: record.created_at, updated_at: record.updated_at }] };
const normalHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent", personal_memory_enabled: true, development_mode_available: true };
const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
function companyReply(id: string) {
  const value = reply(id);
  value.result.company_research = { version: "product-research-answer-v1", origin: "model", paragraphs: ["本轮公开资料的研究结论。"],
    securities: [stock], daily_cutoff: "2026-11-12", evidence: [] };
  return value;
}

function preferencesController(initial = prefs(), enabled = true) {
  let current = structuredClone(initial);
  const api = { ...mockApi(), mode: "normal" as const,
    health: vi.fn(async (): Promise<Health> => ({ ...normalHealth, personal_memory_enabled: enabled })),
    create: vi.fn(async () => ({ ...credentials, provider: "deepseek" as const, data_mode: "tencent" as const })),
    watchlist: vi.fn(async () => structuredClone(watchlist)), searchWatchlist: vi.fn(async (_query: string) => structuredClone(search)),
    preferences: vi.fn(async () => structuredClone(current)),
    patchPreferences: vi.fn(async (id: string, _expected: string | null, changes: PreferenceChanges) => {
      const saved = current.items.find(item => item.instrument_id === id) ?? { ...record, holding_status: null, horizon: null, risk: null };
      current = prefs([...current.items.filter(item => item.instrument_id !== id), { ...saved, ...changes, revision: NEXT }]);
      return structuredClone(current);
    }),
    deletePreference: vi.fn(async (id: string, _revision: string) => { current = prefs(current.items.filter(item => item.instrument_id !== id)); return structuredClone(current); }),
    clearPreferences: vi.fn(async (_revision: string) => { current = prefs([]); return structuredClone(current); }),
    deleteConversation: vi.fn(async (_id: string) => {}),
  };
  api.send.mockImplementation(async (_session, id) => companyReply(id));
  const development = { ...mockApi(), mode: "development" as const };
  let next = 0;
  const controller = new ResearchController(api, () => (++next).toString(16).padStart(32, "0"), development);
  return { api, controller, setPreferences: (value: Preferences) => { current = structuredClone(value); } };
}

describe("preferences public API", () => {
  it("keeps partial changes, omitted fields and delete revisions explicit without forwarding session tokens", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => response(prefs()));
    const api = createApi(fetcher);
    expect(await api.preferences!()).toEqual(prefs());
    await api.patchPreferences!(stock.instrument_id, REV, { horizon: null });
    await api.patchPreferences!(stock.instrument_id, null, { holding_status: "unknown" });
    await api.deletePreference!(stock.instrument_id, REV);
    await api.clearPreferences!(COLLECTION);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(["/v1/personal/preferences", "/v1/personal/preferences/000001.SZ",
      "/v1/personal/preferences/000001.SZ", "/v1/personal/preferences/000001.SZ", "/v1/personal/preferences"]);
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({ expected_revision: REV, changes: { horizon: null } });
    expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({ expected_revision: null, changes: { holding_status: "unknown" } });
    expect(fetcher.mock.calls[3][1]).toEqual(expect.objectContaining({ method: "DELETE", body: undefined, headers: { "If-Match": REV } }));
    expect(fetcher.mock.calls[4][1]?.headers).toEqual({ "If-Match": COLLECTION });
    for (const [, options] of fetcher.mock.calls) expect(options?.headers).not.toHaveProperty("Authorization");
    expect(() => api.patchPreferences!(stock.instrument_id, REV, {})).toThrow(ConnectionError);
    expect(() => api.patchPreferences!(stock.instrument_id, REV, { cost: 12 } as PreferenceChanges)).toThrow(ConnectionError);
    expect(() => api.patchPreferences!(stock.instrument_id, "not-a-revision", { risk: "low" })).toThrow(ConnectionError);
    expect(() => api.clearPreferences!(REV)).toThrow(ConnectionError);
    expect(fetcher).toHaveBeenCalledTimes(5);
  });

  it.each([
    { ...prefs(), extra: true }, prefs([record, record]), prefs([record], false), { ...prefs(), collection_revision: null },
    { ...prefs([], false), collection_revision: COLLECTION }, prefs([{ ...record, revision: COLLECTION }]),
    prefs([{ ...record, holding_status: "owned" as PreferenceRecord["holding_status"] }]),
    prefs([{ ...record, horizon: "forever" as PreferenceRecord["horizon"] }]),
    prefs([{ ...record, risk: ["low"] as unknown as PreferenceRecord["risk"] }]),
    prefs([{ ...record, holding_status: null, horizon: null, risk: null }]),
    prefs([{ ...record, cost: 12 } as PreferenceRecord]), prefs([{ ...record, updated_at: "yesterday" }]),
    prefs(Array.from({ length: 51 }, (_, index) => ({ ...record, instrument_id: `${String(index).padStart(6, "0")}.SZ` }))),
  ])("rejects malformed, unknown or excessive preference fields %#", async value => {
    const api = createApi(vi.fn<typeof fetch>().mockResolvedValue(response(value)));
    await expect(api.preferences!()).rejects.toThrow(ConnectionError);
  });

  it("accepts both local-management versions and only complete supported preference candidate commands", async () => {
    expect(parsePersonalMemory(management())).toEqual(management());
    for (const label of ["持有", "未持有", "不确定", "未填写"]) {
      const value = { ...management([]), action: "update", status: "needs_confirmation",
        candidates: [{ ...stock, reply: `把${stock.instrument_id}的持有状态设为${label}` }] };
      expect(parsePersonalMemory(value)).toEqual(value);
    }
    expect(parsePersonalMemory({ ...management([]), action: "update", status: "needs_confirmation", candidates: [{ ...stock, reply: stock.instrument_id }] })).toBeNull();
    expect(parsePersonalMemory({ ...management(), action: ["list"] })).toBeNull();
    expect(parsePersonalMemory({ ...management(), items: [{ ...record, account: "private" }] })).toBeNull();
    const value = reply(); value.result.personal_memory = management(); value.result.candidates = [];
    expect(await createApi(vi.fn<typeof fetch>().mockResolvedValue(response(value))).send(credentials, value.request_id, "查看持仓与偏好")).toEqual(value);
  });

  it("does not access personal endpoints in development mode", async () => {
    const fetcher = vi.fn<typeof fetch>(), api = createApi(fetcher, "development");
    expect(await api.preferences!()).toEqual(prefs([], false));
    expect(() => api.patchPreferences!(stock.instrument_id, REV, { risk: "low" })).toThrow(ApiError);
    expect(() => api.deletePreference!(stock.instrument_id, REV)).toThrow(ApiError);
    expect(() => api.clearPreferences!(COLLECTION)).toThrow(ApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("preferences state and independence", () => {
  it("keeps records independent of research reset, new chat, message quota and conversation deletion", async () => {
    const { api, controller } = preferencesController();
    api.inspect.mockResolvedValue({ ...idle, messages_used: credentials.limits.messages });
    await controller.load(); expect(api.create).not.toHaveBeenCalled();
    await controller.send("分析平安银行");
    expect(await controller.send("查看持仓与偏好")).toBe(false);
    expect(await controller.savePreferences(stock.instrument_id, REV, { risk: "medium" })).toBe(true);
    await controller.reset(); await controller.end(); await controller.deleteConversation("d".repeat(32));
    expect(controller.snapshot().preferences?.items[0].risk).toBe("medium");
    expect(controller.snapshot().watchlist?.items).toEqual(watchlist.items);
    expect(api.deletePreference).not.toHaveBeenCalled(); expect(api.clearPreferences).not.toHaveBeenCalled();
    expect(api.send).toHaveBeenCalledOnce();
  });

  it("keeps editing content available after conflicts instead of silently overwriting another page", async () => {
    const { api, controller } = preferencesController(); await controller.load();
    expect(controller.openPreferenceEditor(stock)).toBe(true);
    api.patchPreferences.mockRejectedValueOnce(new ApiError(409, "preference_revision_conflict"));
    expect(await controller.savePreferences(stock.instrument_id, REV, { risk: "high" })).toBe(false);
    expect(controller.snapshot().preferencesEditor?.security.instrument_id).toBe(stock.instrument_id);
    expect(controller.snapshot().preferences?.items[0].revision).toBe(REV);
    expect(controller.snapshot().preferencesError).toContain("另一个操作修改");
    expect(api.patchPreferences).toHaveBeenCalledOnce();
  });

  it("ignores old list reads that resolve after a successful save", async () => {
    const { api, controller } = preferencesController(); await controller.load();
    const old = deferred<Preferences>(); api.preferences.mockReturnValueOnce(old.promise);
    const reading = controller.refreshPreferences();
    await controller.savePreferences(stock.instrument_id, REV, { horizon: "short" });
    old.resolve(prefs()); await reading;
    expect(controller.snapshot().preferences?.items[0].horizon).toBe("short");
    expect(controller.snapshot().preferencesLoading).toBe(false);
  });

  it("clears editors and ignores late reads, searches and writes after switching to development", async () => {
    const { api, controller } = preferencesController(); await controller.load(); controller.openPreferenceEditor(stock);
    const old = deferred<Preferences>(), searching = deferred<WatchlistSearch>(), writing = deferred<Preferences>();
    api.preferences.mockReturnValueOnce(old.promise); api.searchWatchlist.mockReturnValueOnce(searching.promise); api.patchPreferences.mockReturnValueOnce(writing.promise);
    const reading = controller.refreshPreferences(), searchTask = controller.searchPreferences("平安"), writeTask = controller.savePreferences(stock.instrument_id, REV, { risk: "high" });
    await controller.switchMode("development");
    old.resolve(prefs()); searching.resolve(search); writing.resolve(prefs([{ ...record, risk: "high" }]));
    await Promise.all([reading, searchTask, writeTask]);
    expect(controller.snapshot().preferences?.items ?? []).toEqual([]);
    expect(controller.snapshot().preferencesEditor).toBeNull(); expect(controller.snapshot().preferencesSearch).toBeNull();
    expect(controller.snapshot().preferencesNotice).toBeNull(); expect(controller.snapshot().preferencesSaving).toBe(false);
    await controller.switchMode("normal"); await vi.waitFor(() => expect(controller.snapshot().preferences?.items).toEqual([record]));
  });

  it("keeps disabled, failed reads and replay receipts separate from the current list", async () => {
    const disabled = preferencesController(prefs([], false), false); await disabled.controller.load();
    expect(disabled.api.preferences).not.toHaveBeenCalled(); expect(disabled.controller.snapshot().preferences?.enabled).toBe(false);
    const { api, controller } = preferencesController(); await controller.load();
    api.preferences.mockRejectedValueOnce(new ApiError(503, "personal_memory_store_unavailable")); await controller.refreshPreferences();
    expect(controller.snapshot().preferences?.items).toEqual([record]); expect(controller.snapshot().preferencesError).toContain("读取失败");
    api.send.mockImplementationOnce(async (_session, id) => { const value = reply(id); value.replayed = true; value.result.personal_memory = management([]); value.result.candidates = []; return value; });
    await controller.send("查看持仓与偏好");
    await vi.waitFor(() => expect(controller.snapshot().preferencesError).toBeNull());
    expect(controller.snapshot().preferences?.items).toEqual([record]);
  });
});

describe("preferences local forms and explicit drafts", () => {
  it("opens a form from a watchlist item and saves only changed fixed categories without research", async () => {
    const { api, controller } = preferencesController(prefs([])); render(<App controller={controller} />);
    const open = await screen.findByRole("button", { name: "管理自选资料：平安银行" });
    await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    const form = screen.getByRole("form", { name: "平安银行的持仓与偏好" });
    expect(within(form).getByRole("combobox", { name: "持有状态" })).toHaveValue("");
    fireEvent.change(within(form).getByRole("combobox", { name: "持有状态" }), { target: { value: "unknown" } });
    fireEvent.change(within(form).getByRole("combobox", { name: "风险偏好" }), { target: { value: "medium" } });
    fireEvent.click(within(form).getByRole("button", { name: "保存持仓与偏好" }));
    await vi.waitFor(() => expect(api.patchPreferences).toHaveBeenCalledWith(stock.instrument_id, null, { holding_status: "unknown", risk: "medium" }));
    await vi.waitFor(() => expect(controller.snapshot().preferencesSaving).toBe(false));
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    expect(controller.snapshot().watchlist?.items).toEqual(watchlist.items);
  });

  it("keeps an edited form after conflict and requires explicit reload of the newer record", async () => {
    const { api, controller, setPreferences } = preferencesController(); render(<App controller={controller} />);
    const edit = await screen.findByRole("button", { name: "编辑资料：平安银行" }); fireEvent.click(edit);
    const form = screen.getByRole("form", { name: "平安银行的持仓与偏好" });
    fireEvent.change(within(form).getByRole("combobox", { name: "风险偏好" }), { target: { value: "high" } });
    api.patchPreferences.mockRejectedValueOnce(new ApiError(409, "preference_revision_conflict"));
    fireEvent.click(within(form).getByRole("button", { name: "保存持仓与偏好" }));
    await screen.findByText(/这份资料已被另一个操作修改/);
    expect(screen.queryByText(/资料变更未确认，请刷新后核对。/)).not.toBeInTheDocument();
    expect(within(form).getByRole("combobox", { name: "风险偏好" })).toHaveValue("high");
    setPreferences(prefs([{ ...record, revision: NEXT, horizon: "short" }]));
    await act(async () => { await controller.refreshPreferences(); });
    expect(within(form).getByRole("combobox", { name: "风险偏好" })).toHaveValue("high");
    fireEvent.click(within(form).getByRole("button", { name: "使用当前资料重新填表" }));
    expect(screen.getByRole("combobox", { name: "风险偏好" })).toHaveValue("low");
    expect(screen.getByRole("combobox", { name: "期限偏好" })).toHaveValue("short");
  });

  it("requires deletion confirmation with the captured row or collection revision and preserves watchlist/history", async () => {
    const { api, controller } = preferencesController(); render(<App controller={controller} />);
    const panel = screen.getByRole("region", { name: "持仓与偏好" });
    fireEvent.click(await within(panel).findByRole("button", { name: "删除资料：平安银行" }));
    expect(api.deletePreference).not.toHaveBeenCalled();
    fireEvent.click(within(panel).getByRole("button", { name: "确认删除资料" }));
    await vi.waitFor(() => expect(api.deletePreference).toHaveBeenCalledWith(stock.instrument_id, REV));
    await vi.waitFor(() => expect(controller.snapshot().preferencesSaving).toBe(false));
    await act(async () => { await controller.savePreferences(stock.instrument_id, null, { holding_status: "held" }); });
    fireEvent.click(within(panel).getByRole("button", { name: "清空持仓与偏好" })); expect(api.clearPreferences).not.toHaveBeenCalled();
    fireEvent.click(within(panel).getByRole("button", { name: "确认删除资料" }));
    await vi.waitFor(() => expect(api.clearPreferences).toHaveBeenCalledWith(COLLECTION));
    expect(controller.snapshot().watchlist?.items).toEqual(watchlist.items); expect(api.deleteConversation).not.toHaveBeenCalled();
  });

  it("fills only a same-stock question, makes no request on click and protects an existing draft", async () => {
    const { api, controller } = preferencesController(); render(<App controller={controller} />);
    await screen.findByRole("button", { name: "编辑资料：平安银行" });
    await act(async () => { await controller.send("分析平安银行"); });
    const question = screen.getByRole("textbox", { name: "研究问题" });
    fireEvent.change(question, { target: { value: "保留我自己写的问题" } });
    const reads = api.preferences.mock.calls.length, creates = api.create.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "放入退出条件研究草稿" }));
    expect(question).toHaveValue("保留我自己写的问题"); expect(api.send).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "保留当前草稿" })); expect(question).toHaveValue("保留我自己写的问题");
    fireEvent.click(screen.getByRole("button", { name: "放入退出条件研究草稿" }));
    fireEvent.click(screen.getByRole("button", { name: "替换为研究草稿" }));
    expect(question).toHaveValue("请分析 000001.SZ 的退出观察条件");
    expect(api.send).toHaveBeenCalledOnce(); expect(api.preferences).toHaveBeenCalledTimes(reads); expect(api.create).toHaveBeenCalledTimes(creates);
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await vi.waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[1][2]).toBe("请分析 000001.SZ 的退出观察条件");
    expect(JSON.stringify(api.send.mock.calls[1])).not.toContain("holding_status");
    expect(JSON.stringify(api.send.mock.calls[1])).not.toContain("2026-10-01");
    expect(JSON.stringify(api.send.mock.calls[1])).not.toContain("long");
  });

  it("explains a disabled draft next to its button and keeps unknown write failures unconfirmed", async () => {
    const { api, controller } = preferencesController(); await controller.load(); await controller.send("分析平安银行");
    api.patchPreferences.mockRejectedValueOnce(new ConnectionError());
    await controller.savePreferences(stock.instrument_id, REV, { risk: "high" });
    expect(controller.snapshot().preferencesError).toContain("资料变更未确认，请刷新后核对。");
    render(<PersonalPreferenceHint securities={[stock]} state={controller.snapshot()} openEditor={vi.fn()} onDraft={vi.fn()} />);
    const draft = screen.getByRole("button", { name: "放入退出条件研究草稿" });
    expect(draft).toBeDisabled(); expect(draft).toHaveAccessibleDescription("资料待刷新核对，请先在面板刷新资料。");
    expect(screen.getByText("资料待刷新核对，请先在面板刷新资料。")).toBeVisible();
  });

  it.each(["unknown", null] as const)("offers no generated question for an uncertain or unfilled holding status %s", async holding_status => {
    const { controller } = preferencesController(prefs([{ ...record, holding_status }])); await controller.load();
    render(<PersonalPreferenceHint securities={[stock]} state={controller.snapshot()} openEditor={vi.fn()} onDraft={vi.fn()} />);
    expect(screen.getByText(new RegExp(holding_status === null ? "持有状态：未填写" : "持有状态：不确定"))).toBeVisible();
    expect(screen.queryByRole("button", { name: /放入.*研究草稿/ })).not.toBeInTheDocument();
    expect(screen.getByText(/记录最后修改/)).toBeVisible();
  });

  it("offers an entry question only for the same-stock not-held record and never attaches today's hints to history", async () => {
    const { api, controller } = preferencesController(prefs([{ ...record, holding_status: "not_held" }]));
    api.history.mockImplementation(async () => companyReply("e".repeat(32)));
    render(<App controller={controller} />); await screen.findByRole("button", { name: "编辑资料：平安银行" });
    await act(async () => { await controller.send("分析平安银行"); await controller.history(); });
    expect(screen.getAllByLabelText("当前研究对象的本机资料")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "放入买入条件研究草稿" }));
    expect(screen.getByRole("textbox", { name: "研究问题" })).toHaveValue("请分析 000001.SZ 的买入观察条件");
    expect(api.send).toHaveBeenCalledOnce();
  });

  it("shows receipt categories locally and sends complete preference candidates without a model allowance", async () => {
    const { api, controller } = preferencesController(); api.inspect.mockResolvedValue({ ...idle, model_calls_forwarded: credentials.limits.model_calls });
    api.send.mockImplementationOnce(async (_session, id) => {
      const value = reply(id); Object.assign(value.result, { parameters: null, report: null, candidates: [], personal_memory: {
        ...management(), action: "update", status: "needs_confirmation", candidates: [{ ...stock, reply: "把000001.SZ的持有状态设为未持有" }] } }); return value;
    });
    render(<App controller={controller} />); await screen.findByRole("button", { name: "编辑资料：平安银行" });
    await act(async () => { await controller.send("记住我未持有银行"); });
    expect(within(screen.getByLabelText("持仓与偏好管理记录")).getByText(/持有状态：持有/)).toBeVisible();
    const choices = screen.getByLabelText("持仓与偏好管理候选股票"); fireEvent.click(within(choices).getByRole("button"));
    await vi.waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[1][2]).toBe("把000001.SZ的持有状态设为未持有");
  });
});

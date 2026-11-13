import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { ApiError, ConnectionError, createApi, type Health, type Watchlist } from "../src/api";
import { ResearchController } from "../src/controller";
import { inboxVersion, parseInboxCheck, parseMessageInbox, type InboxCheckResult, type InboxFilters, type InboxMessage, type InboxSourceState, type MessageInboxData } from "../src/inboxMessages";
import { credentials, deferred, health, mockApi } from "./fixtures";

const REV = "a".repeat(64), NEXT = "b".repeat(64), REQUEST = "c".repeat(32), STAMP = "2026-11-12T03:00:00+00:00";
const stock = { instrument_id: "000001.SZ", name: "平安银行" };
function message(overrides: Partial<InboxMessage> = {}): InboxMessage {
  return { ...stock, message_id: "d".repeat(64), source: "sina_company_news", title: "平安银行发布公开资料", url: "https://finance.sina.com.cn/stock/doc-abc123.shtml",
    published_date: "2026-11-12", published_at: null, publication_precision: "date", first_seen_at: STAMP, retrieved_at: STAMP,
    discovery: "manual", baseline: true, read: false, read_at: null, ...overrides };
}
function sourceState(overrides: Partial<InboxSourceState> = {}): InboxSourceState {
  return { ...stock, source: "sina_company_news", initialized: true, last_success_at: STAMP, last_attempt_at: STAMP, status: "available",
    code: "inbox_source_available", skipped_count: 0, evicted_unread_count: 0, evicted_at: null, ...overrides };
}
function page(items = [message()], overrides: Partial<MessageInboxData> = {}): MessageInboxData {
  return { version: inboxVersion, enabled: true, limit: 20, items, unread_count: items.filter(value => !value.read).length,
    source_states: [sourceState(), sourceState({ source: "eastmoney_announcements", initialized: false, last_success_at: null, status: "unavailable", code: "source_unavailable" })],
    collection_revision: REV, has_more: false, next_cursor: null, ...overrides };
}
function receipt(requestId = REQUEST, inbox = page(), overrides: Partial<InboxCheckResult> = {}): InboxCheckResult {
  return { version: inboxVersion, enabled: true, request_id: requestId, replayed: false, status: "partial", attempted_requests: 2,
    results: [{ ...stock, source: "sina_company_news", status: "available", code: "inbox_source_available", saved: 1, duplicates: 0, filtered_count: 0, baseline: true },
      { instrument_id: stock.instrument_id, source: "eastmoney_announcements", status: "unavailable", code: "source_unavailable", saved: 0, duplicates: 0, filtered_count: 0, baseline: false }]
      .map(({ instrument_id, source, status, code, saved, duplicates, filtered_count, baseline }) => ({ instrument_id, source, status, code, saved, duplicates, filtered_count, baseline })) as InboxCheckResult["results"],
    next_cursor: "check-cursor", remaining_sources: 8, checked_stocks: 1, total_stocks: 5, inbox, ...overrides };
}
const normalHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent", personal_memory_enabled: true, memory_enabled: true, development_mode_available: true };
function inboxController(initial = page()) {
  let current = structuredClone(initial), watched = [stock];
  const watchlist = (): Watchlist => ({ version: "personal-memory-watchlist-v1", enabled: true, limit: 50,
    items: watched.map(value => ({ ...value, created_at: STAMP, updated_at: STAMP })) });
  const api = { ...mockApi(), mode: "normal" as const, health: vi.fn(async () => ({ ...normalHealth })),
    create: vi.fn(async () => ({ ...credentials, provider: "deepseek" as const, data_mode: "tencent" as const })),
    watchlist: vi.fn(async () => watchlist()),
    removeWatchlist: vi.fn(async (_instrument: string) => { watched = []; current = page([], { source_states: [], collection_revision: NEXT }); return watchlist(); }),
    inbox: vi.fn(async (filters: InboxFilters, _signal?: AbortSignal) => ({ ...structuredClone(current), items: structuredClone(current.items.filter(value => (!filters.unread_only || !value.read)
      && (!filters.instrument_id || filters.instrument_id === value.instrument_id))) })),
    checkInbox: vi.fn(async (requestId: string, _cursor?: string | null, _signal?: AbortSignal) => receipt(requestId, structuredClone(current))),
    markInboxRead: vi.fn(async (id: string, _signal?: AbortSignal) => {
      current = { ...current, collection_revision: NEXT, items: current.items.map(value => value.message_id === id ? { ...value, read: true, read_at: STAMP } : value) };
      current.unread_count = current.items.filter(value => !value.read).length;
      return { ...structuredClone(current), items: structuredClone(current.items.filter(value => !value.read)) };
    }),
    clearInbox: vi.fn(async (_revision: string, _signal?: AbortSignal) => {
      current = page([], { collection_revision: NEXT, source_states: current.source_states.map(value => ({ ...value, initialized: false, last_success_at: null, last_attempt_at: null, status: "not_checked", code: "inbox_not_checked" })) });
      return structuredClone(current);
    }),
  };
  const dev = { ...mockApi(), mode: "development" as const };
  let counter = 0;
  return { api, controller: new ResearchController(api, () => (++counter).toString(16).padStart(32, "0"), dev),
    setInbox: (value: MessageInboxData) => { current = structuredClone(value); } };
}
const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });

describe("strict inbox contracts", () => {
  it("accepts source precision, read state, research discovery and bounded loss notices", () => {
    expect(parseMessageInbox(page())).toEqual(page());
    const old = message({ read: true, read_at: STAMP, discovery: "research", baseline: false });
    expect(parseMessageInbox(page([old]), { unread_only: false })).not.toBeNull();
    expect(parseMessageInbox(page([old]), { unread_only: true })).toBeNull();
    expect(parseMessageInbox(page([], { source_states: [sourceState({ evicted_unread_count: 3, evicted_at: STAMP })] }))).not.toBeNull();
    expect(parseInboxCheck(receipt(), REQUEST)).not.toBeNull();
    expect(parseMessageInbox(page([], { sidecar_status: null }))).not.toBeNull();
    expect(parseMessageInbox(page([], { sidecar_status: "inbox_sidecar_saved" }))).not.toBeNull();
    expect(parseMessageInbox({ ...page(), sidecar_status: "raw source failure" })).toBeNull();
  });
  it.each([
    { url: "javascript:alert(1)" }, { url: "https://finance.sina.com.cn.evil.example/doc-a.shtml" },
    { source: "eastmoney_announcements", url: "https://data.eastmoney.com/notices/detail/600519/AN20261112000001.html" },
    { publication_precision: "datetime", published_at: null }, { publication_precision: "date", published_at: STAMP },
    { published_date: "2026-02-30" }, { read: false, read_at: STAMP }, { source: "jev_important" },
  ])("rejects unsafe or inconsistent item fields %o", override => {
    expect(parseMessageInbox(page([{ ...message(), ...override } as InboxMessage]))).toBeNull();
  });
  it("rejects duplicate, oversized, mismatched and expanded checking receipts", () => {
    expect(parseMessageInbox(page([message(), message()]))).toBeNull();
    expect(parseMessageInbox(page(Array.from({ length: 21 }, (_, index) => message({ message_id: index.toString(16).padStart(64, "0") }))))).toBeNull();
    expect(parseMessageInbox(page(), { unread_only: true, instrument_id: "600519.SH" })).toBeNull();
    expect(parseMessageInbox({ ...page(), has_more: true })).toBeNull();
    expect(parseInboxCheck(receipt(), "f".repeat(32))).toBeNull();
    expect(parseInboxCheck({ ...receipt(), attempted_requests: 11 }, REQUEST)).toBeNull();
    expect(parseInboxCheck({ ...receipt(), results: Array.from({ length: 6 }, (_, index) => ({ ...receipt().results[0], instrument_id: `${String(index).padStart(6, "0")}.SZ` })) }, REQUEST)).toBeNull();
  });
  it.each([undefined, -1, true, 51, 1.5, Number.MAX_SAFE_INTEGER + 1, "1"])("rejects missing or invalid batch filtered counts %s", filtered_count => {
    expect(parseInboxCheck({ ...receipt(), results: [{ ...receipt().results[0], filtered_count }] }, REQUEST)).toBeNull();
  });
  it("accepts normal filtering without converting a successful source to failure", () => {
    const filtered = page([], { source_states: [sourceState({ status: "empty" })] });
    expect(parseMessageInbox(filtered)?.source_states[0]).toEqual(filtered.source_states[0]);
    const checked = receipt(REQUEST, filtered, { status: "completed", results: [{ ...receipt().results[0], status: "empty", saved: 0, filtered_count: 8 }] });
    expect(parseInboxCheck(checked, REQUEST)?.results[0].status).toBe("empty");
    expect(parseInboxCheck({ ...checked, results: [{ ...checked.results[0], filtered_count: 50 }] }, REQUEST)).not.toBeNull();
    const { filtered_count: _omitted, ...withoutCount } = checked.results[0];
    expect(parseInboxCheck({ ...checked, results: [withoutCount] }, REQUEST)).toBeNull();
  });
});

describe("inbox HTTP client", () => {
  it("keeps local reads, one batch POST and CAS mutations separate, with no session credentials", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(page())).mockResolvedValueOnce(response(receipt())).mockResolvedValueOnce(response(page([]))).mockResolvedValueOnce(response(page([])));
    const api = createApi(fetcher), abort = new AbortController();
    await api.inbox!({ unread_only: false, instrument_id: stock.instrument_id, cursor: "list+cursor" }, abort.signal);
    expect(fetcher).toHaveBeenCalledOnce();
    await api.checkInbox!(REQUEST, "check-cursor", abort.signal); await api.markInboxRead!(message().message_id, abort.signal); await api.clearInbox!(REV, abort.signal);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(["/v1/personal/inbox?unread_only=false&instrument_id=000001.SZ&cursor=list%2Bcursor", "/v1/personal/inbox/check", `/v1/personal/inbox/${message().message_id}`, "/v1/personal/inbox"]);
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({ request_id: REQUEST, cursor: "check-cursor" });
    expect(fetcher.mock.calls[2][1]).toEqual(expect.objectContaining({ method: "PATCH", body: JSON.stringify({ read: true }) }));
    expect(fetcher.mock.calls[3][1]?.headers).toEqual({ "If-Match": REV });
    for (const [, options] of fetcher.mock.calls) expect(options?.headers).not.toHaveProperty("Authorization");
    expect(() => api.inbox!({ unread_only: true, instrument_id: "名称" })).toThrow(ConnectionError);
    expect(() => api.checkInbox!("not-a-request")).toThrow(ConnectionError);
    expect(() => api.clearInbox!("old")).toThrow(ConnectionError);
  });
  it("waits 25 seconds for a manual check instead of the normal 10 seconds", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | undefined;
      const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, options) => new Promise((_resolve, reject) => {
        signal = options?.signal as AbortSignal; signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      }));
      const pending = createApi(fetcher).checkInbox!(REQUEST), rejected = expect(pending).rejects.toThrow(ConnectionError);
      await vi.advanceTimersByTimeAsync(10_001); expect(signal?.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(14_999); await rejected; expect(signal?.aborted).toBe(true);
    } finally { vi.useRealTimers(); }
  });
  it("blocks all development methods before fetching", () => {
    const fetcher = vi.fn<typeof fetch>(), api = createApi(fetcher, "development");
    expect(() => api.inbox!({ unread_only: true })).toThrow(ApiError); expect(() => api.checkInbox!(REQUEST)).toThrow(ApiError);
    expect(() => api.markInboxRead!(message().message_id)).toThrow(ApiError); expect(() => api.clearInbox!(REV)).toThrow(ApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("inbox state and explicit batch controls", () => {
  it("initializes and paginates with only local reads, independent of research quota", async () => {
    const { api, controller } = inboxController(page([message()], { has_more: true, next_cursor: "list-cursor" })); await controller.load();
    expect(api.inbox).toHaveBeenCalledOnce(); expect(api.checkInbox).not.toHaveBeenCalled(); controller.openMessageInbox();
    await vi.waitFor(() => expect(controller.snapshot().inboxLoading).toBe(false));
    const before = controller.snapshot(); await controller.nextInboxPage();
    expect(api.inbox.mock.calls.at(-1)?.[0]).toEqual({ unread_only: true, cursor: "list-cursor" });
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled(); expect(api.checkInbox).not.toHaveBeenCalled();
    expect(controller.snapshot().session).toBe(before.session); expect(controller.snapshot().entries).toBe(before.entries);
    expect(controller.snapshot().phase).toBe(before.phase);
  });
  it("runs one user batch and uses a different continuation from local pagination", async () => {
    const { api, controller } = inboxController(); await controller.load(); controller.openMessageInbox();
    await controller.startInboxCheck(); expect(api.checkInbox).toHaveBeenCalledOnce();
    expect(api.checkInbox.mock.calls[0][1]).toBeNull(); expect(controller.snapshot().inboxNextCheckCursor).toBe("check-cursor");
    await controller.startInboxCheck(true); expect(api.checkInbox).toHaveBeenCalledTimes(2);
    expect(api.checkInbox.mock.calls[1][1]).toBe("check-cursor");
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
  });
  it("keeps the same request and cursor after an unknown response or busy result", async () => {
    const { api, controller } = inboxController(); await controller.load(); controller.openMessageInbox();
    api.checkInbox.mockRejectedValueOnce(new ConnectionError()); await controller.startInboxCheck();
    const original = api.checkInbox.mock.calls[0].slice(0, 2); expect(controller.snapshot().inboxCheckUncertain).toBe(true);
    expect(await controller.startInboxCheck()).toBe(false); expect(api.checkInbox).toHaveBeenCalledOnce();
    api.checkInbox.mockRejectedValueOnce(new ApiError(409, "inbox_busy")); await controller.retryInboxCheck();
    expect(controller.snapshot().inboxCheckUncertain).toBe(true); await controller.retryInboxCheck();
    expect(api.checkInbox.mock.calls[1].slice(0, 2)).toEqual(original); expect(api.checkInbox.mock.calls[2].slice(0, 2)).toEqual(original);
    expect(controller.snapshot().inboxCheckUncertain).toBe(false);
  });
  it.each(["inbox_invalid_request", "inbox_invalid_cursor", "inbox_snapshot_changed", "inbox_request_conflict", "inbox_request_retired", "inbox_request_limit"])("retires a definitively rejected check %s", async code => {
    const { api, controller } = inboxController(); await controller.load(); controller.openMessageInbox();
    api.checkInbox.mockRejectedValueOnce(new ApiError(409, code)); await controller.startInboxCheck();
    expect(controller.snapshot().inboxCheckUncertain).toBe(false); await controller.refreshInbox(); await controller.startInboxCheck();
    expect(api.checkInbox.mock.calls[0][0]).not.toBe(api.checkInbox.mock.calls[1][0]);
  });
  it("never copies replayed messages over the current read or clear state", async () => {
    const { api, controller, setInbox } = inboxController(); await controller.load(); controller.openMessageInbox();
    setInbox(page([])); api.checkInbox.mockImplementationOnce(async id => receipt(id, page(), { replayed: true }));
    await controller.startInboxCheck(); expect(controller.snapshot().inboxData?.items).toEqual([]);
    expect(controller.snapshot().inboxCheckResult?.replayed).toBe(true); expect(controller.snapshot().inboxNotice).toContain("原批回执");
  });
  it.each(["close", "mode", "remove", "cancel"])("aborts and ignores a late check after %s", async action => {
    const { api, controller } = inboxController(); await controller.load(); controller.openMessageInbox();
    const future = deferred<InboxCheckResult>(); api.checkInbox.mockReturnValueOnce(future.promise);
    const pending = controller.startInboxCheck(); const call = api.checkInbox.mock.calls[0];
    if (action === "close") controller.closeMessageInbox();
    if (action === "mode") await controller.switchMode("development");
    if (action === "remove") await controller.removeWatchlist(stock.instrument_id);
    if (action === "cancel") controller.cancelInboxCheck();
    expect(call[2]?.aborted).toBe(true); future.resolve(receipt(call[0])); await pending;
    expect(controller.snapshot().inboxCheckResult).toBeNull(); expect(controller.snapshot().inboxCheckLoading).toBe(false);
    if (action === "close") { controller.openMessageInbox(); expect(controller.snapshot().inboxCheckUncertain).toBe(true); }
  });
  it("does not restore a list or read result from before a mode switch", async () => {
    const { api, controller } = inboxController(); await controller.load(); controller.openMessageInbox();
    const old = deferred<MessageInboxData>(), writing = deferred<MessageInboxData>(); api.inbox.mockReturnValueOnce(old.promise); api.markInboxRead.mockReturnValueOnce(writing.promise);
    const reading = controller.refreshInbox(), marking = controller.markInboxRead(message().message_id);
    await controller.switchMode("development"); old.resolve(page()); writing.resolve(page()); await Promise.all([reading, marking]);
    expect(controller.snapshot().inboxData?.items ?? []).toEqual([]); expect(controller.snapshot().inboxWriting).toBe(false); expect(controller.snapshot().inboxOpen).toBe(false);
  });
  it("marks read and clears with captured revision, preserving unrelated state and uncertainty", async () => {
    const { api, controller } = inboxController(); await controller.load(); controller.openMessageInbox();
    const watchlist = controller.snapshot().watchlist;
    await controller.markInboxRead(message().message_id); expect(controller.snapshot().inboxData?.items).toEqual([]);
    expect(controller.snapshot().inboxData?.unread_count).toBe(0);
    api.clearInbox.mockRejectedValueOnce(new ApiError(409, "inbox_conflict"));
    expect(await controller.clearInbox(REV)).toBe(false); expect(controller.snapshot().inboxError).toContain("重新确认");
    await controller.refreshInbox(); api.clearInbox.mockRejectedValueOnce(new ConnectionError());
    expect(await controller.clearInbox(NEXT)).toBe(false); expect(controller.snapshot().inboxError).toContain("变更未确认");
    await controller.refreshInbox(); await controller.clearInbox(NEXT);
    expect(api.clearInbox.mock.calls.at(-1)?.[0]).toBe(NEXT); expect(controller.snapshot().watchlist).toBe(watchlist);
    expect(controller.snapshot().inboxData?.source_states.every(value => !value.initialized)).toBe(true); expect(api.send).not.toHaveBeenCalled();
  });
  it("keeps first-read failures distinct from an empty inbox and never fetches when disabled", async () => {
    const { api, controller } = inboxController(); api.inbox.mockRejectedValueOnce(new ConnectionError()); await controller.load();
    expect(controller.snapshot().inboxData).toBeNull(); expect(controller.snapshot().inboxError).toContain("读取失败");
    const other = inboxController(); other.api.health.mockResolvedValueOnce({ ...normalHealth, personal_memory_enabled: false }); await other.controller.load();
    expect(other.api.inbox).not.toHaveBeenCalled(); expect(other.controller.openMessageInbox()).toBe(false); expect(await other.controller.startInboxCheck()).toBe(false);
  });
});

describe("local inbox page", () => {
  it("opens only local unread titles and never marks a link read or checks automatically", async () => {
    const { api, controller } = inboxController(); render(<App controller={controller} />);
    const open = screen.getByRole("button", { name: "消息收件箱" }); await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    await screen.findByRole("link", { name: /平安银行发布公开资料/ }); expect(screen.getByText("有限检查，非全量监控")).toBeVisible();
    expect(screen.getByText(/仅日期，未提供时分/)).toBeVisible(); expect(screen.getByText(/初次建立基线/)).toBeVisible();
    const link = screen.getByRole("link", { name: /平安银行发布公开资料/ }); expect(link).toHaveAttribute("rel", "noopener noreferrer");
    fireEvent.click(link); expect(api.markInboxRead).not.toHaveBeenCalled(); expect(api.checkInbox).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "检查自选消息" })); await screen.findByRole("region", { name: "本批消息检查回执" });
    expect(api.checkInbox).toHaveBeenCalledOnce(); expect(screen.getByRole("button", { name: "检查下一批" })).toBeVisible();
    expect(within(screen.getByRole("region", { name: "本批消息检查回执" })).getByText("失败")).toBeVisible();
  });
  it("shows retention loss even when unread is zero and requires explicit clear confirmation", async () => {
    const { api, controller } = inboxController(page([], { source_states: [sourceState({ evicted_unread_count: 3, evicted_at: STAMP })] }));
    render(<App controller={controller} />); const open = screen.getByRole("button", { name: "消息收件箱" }); await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    await screen.findByText(/累计清理了 3 条未读消息/); expect(screen.getByText(/本机暂无未读消息/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "清空消息收件箱" })); expect(api.clearInbox).not.toHaveBeenCalled();
    expect(screen.getByRole("group", { name: "确认清空消息收件箱" })).toHaveTextContent("下次主动检查重新建立基线");
    fireEvent.click(screen.getByRole("button", { name: "确认清空消息及基线" })); await vi.waitFor(() => expect(api.clearInbox).toHaveBeenCalledWith(REV, expect.any(AbortSignal)));
    expect(api.checkInbox).not.toHaveBeenCalled();
  });
  it("offers same-id recovery and cancellation, without starting a new batch automatically", async () => {
    const { api, controller } = inboxController(); api.checkInbox.mockRejectedValueOnce(new ConnectionError()); render(<App controller={controller} />);
    const open = screen.getByRole("button", { name: "消息收件箱" }); await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    await screen.findByRole("link", { name: /平安银行发布公开资料/ }); fireEvent.click(screen.getByRole("button", { name: "检查自选消息" }));
    const retry = await screen.findByRole("button", { name: "重查原批结果" }); const first = api.checkInbox.mock.calls[0].slice(0, 2);
    expect(screen.getByRole("button", { name: "检查自选消息" })).toBeDisabled();
    const future = deferred<InboxCheckResult>(); api.checkInbox.mockReturnValueOnce(future.promise); fireEvent.click(retry);
    expect(api.checkInbox.mock.calls[1].slice(0, 2)).toEqual(first); fireEvent.click(screen.getByRole("button", { name: "停止等待消息检查" }));
    expect(screen.getByText(/已停止等待检查/)).toBeVisible(); expect(api.checkInbox).toHaveBeenCalledTimes(2);
    await act(async () => { future.resolve(receipt(api.checkInbox.mock.calls[1][0])); await future.promise; });
    expect(screen.queryByRole("region", { name: "本批消息检查回执" })).not.toBeInTheDocument();
  });
  it("shows a fixed local notice when research titles were not confirmed saved", async () => {
    const { api, controller } = inboxController(page([], { sidecar_status: "inbox_sidecar_unavailable" }));
    render(<App controller={controller} />); const open = screen.getByRole("button", { name: "消息收件箱" });
    await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    expect(await screen.findByText("最近一次研究标题未确认保存；原研究结果不受影响，可刷新核对。")).toBeVisible();
    expect(api.checkInbox).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled();
  });
  it("shows ordinary filtered titles in the current batch receipt without reporting a verification failure", async () => {
    const filtered = page([], { source_states: [sourceState({ status: "empty" })] });
    const { api, controller } = inboxController(filtered);
    api.checkInbox.mockImplementation(async requestId => receipt(requestId, filtered, { status: "completed", next_cursor: null, remaining_sources: 0,
      results: [{ ...receipt().results[0], status: "empty", saved: 0, filtered_count: 8 }] }));
    render(<App controller={controller} />); const open = screen.getByRole("button", { name: "消息收件箱" });
    await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    const notice = "另有 8 条因公司关联、日期窗口或新浪链接格式限制未保存。";
    await screen.findByText(/本机暂无未读消息/);
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
    expect(screen.getByText(/逐股来源检查状态/)).not.toHaveTextContent("失败");
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "检查自选消息" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "检查自选消息" }));
    const result = await screen.findByRole("region", { name: "本批消息检查回执" });
    expect(within(result).getByText(notice)).toBeVisible();
    expect(within(result).getByText("本次有限范围为空")).toBeVisible();
    expect(within(result).queryByText(/失败/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭收件箱" }));
    fireEvent.click(screen.getByRole("button", { name: "消息收件箱" }));
    await screen.findByText(/本机暂无未读消息/);
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
    expect(api.checkInbox).toHaveBeenCalledOnce(); expect(api.create).not.toHaveBeenCalled();
  });
});

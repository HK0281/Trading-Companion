import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionError, createApi, type Health } from "../src/api";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { ResearchReviewChart } from "../src/ResearchReviewChart";
import { legacyReviewNotice, parseResearchReviewDetail, parseResearchReviewItem, parseResearchReviewPrices, parseResearchReviews,
  reviewVersion, type ReviewFilters, type ReviewIdentity, type ResearchReviewDetail, type ResearchReviewItem, type ResearchReviewPrices, type ResearchReviews } from "../src/researchReviews";
import { credentials, deferred, health, mockApi, reply } from "./fixtures";

const identity: ReviewIdentity = { conversation_id: "a".repeat(32), sequence: 1, request_id: "b".repeat(32), instrument_id: "000001.SZ" };
const filters: ReviewFilters = { instrument_id: identity.instrument_id };
const timestamp = "2026-10-01T08:00:00+00:00";
function item(overrides: Partial<ResearchReviewItem> = {}): ResearchReviewItem {
  return { ...identity, name: "平安银行", question: "分析平安银行的退出观察条件", asked_at: "2026-10-01T07:59:00+00:00", completed_at: timestamp,
    daily_cutoff: "2026-09-30", historical_type: "current", status: "available", code: "decision_ready", rating: "减持", direction: "偏空", conviction: "中", horizon: "未来2周",
    trade_plan: { version: "trade-plan-v1", origin: "application", execution_allowed: false, instrument_id: identity.instrument_id, plan_id: "old-exit-plan",
      purpose: "holding_exit", status: "available", code: "trade_plan_ready", source_date: "2026-09-30", source: "tencent", data_authenticity: "provider_reported",
      price_adjustment: "unadjusted", method: "atr_reference_plan_v1", values: { reference_close: "10.12", exit_below: "9.1" } },
    prices: { enabled: true, code: "review_prices_ready", message: "可以加载后续日线。", start_date: "2026-10-02", end_date: "2026-10-06",
      basis: "completed_at", basis_at: timestamp, notice: null, source: "tencent", price_adjustment: "unadjusted", data_authenticity: "provider_reported", source_date: "2026-09-30", technical_request_id: "technical-original" },
    ...overrides };
}
function page(items = [item()]): ResearchReviews {
  return { version: reviewVersion, enabled: true, items, scanned: 20, skipped_invalid: 0, has_more: true, next_cursor: "opaque-cursor" };
}
function detail(record = item()): ResearchReviewDetail {
  return { version: reviewVersion, item: record, question: "第1轮完整原问题：分析平安银行的退出观察条件", result: reply().result };
}
function prices(record = item()): ResearchReviewPrices {
  return { version: reviewVersion, status: "available", item: record, source: "tencent", price_adjustment: "unadjusted", data_authenticity: "provider_reported",
    retrieved_at: "2026-10-07T02:00:00Z", points: [{ date: "2026-10-02", close: 10.2 }, { date: "2026-10-06", close: 10.8 }] };
}
function legacyItem() { const record = item({ completed_at: null }); record.prices = { ...record.prices, basis: "conversation_updated_at", basis_at: timestamp, notice: legacyReviewNotice }; return record; }
const normalHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent", memory_enabled: true, development_mode_available: true };
function reviewController() {
  const api = { ...mockApi(), mode: "normal" as const, health: vi.fn(async () => ({ ...normalHealth })),
    create: vi.fn(async () => ({ ...credentials, provider: "deepseek" as const, data_mode: "tencent" as const })),
    researchReviews: vi.fn(async (_filters: ReviewFilters, _signal?: AbortSignal) => page()),
    researchReview: vi.fn(async (_identity: ReviewIdentity, _signal?: AbortSignal) => detail()),
    researchReviewPrices: vi.fn(async (_identity: ReviewIdentity, _signal?: AbortSignal) => prices()),
    deleteConversation: vi.fn(async (_id: string) => {}),
    conversations: vi.fn(async () => []),
    conversation: vi.fn(async (id: string) => ({ conversation_id: id, title: "保存的对话", provider: "deepseek", created_at: timestamp, updated_at: timestamp, turns: [] })),
  };
  const development = { ...mockApi(), mode: "development" as const };
  return { api, controller: new ResearchController(api, undefined, development) };
}
const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });

describe("strict review projections and price contracts", () => {
  it("keeps missing values and historical types explicit and refuses a plan belonging to another stock", () => {
    expect(parseResearchReviewItem(item())).toEqual(item());
    const missing = item({ historical_type: "unknown", status: "unrecorded", rating: null, direction: null, conviction: null, horizon: null, trade_plan: null });
    expect(parseResearchReviewItem(missing)).toEqual(missing);
    const cross = item(); cross.trade_plan!.instrument_id = "600519.SH";
    expect(parseResearchReviewItem(cross)).toBeNull();
    const badPrice = item(); badPrice.trade_plan!.values.exit_below = "请买入";
    expect(parseResearchReviewItem(badPrice)).toBeNull();
    const precise = item(); precise.trade_plan!.values.exit_below = "9.123456789012345678901234567890123456789";
    expect(parseResearchReviewItem(precise)?.trade_plan?.values.exit_below).toBe(precise.trade_plan!.values.exit_below);
    precise.trade_plan!.values.exit_below = "1000000000000001";
    expect(parseResearchReviewItem(precise)).toBeNull();
    expect(parseResearchReviewItem(item({ historical_type: "live" as "current" }))).toBeNull();
  });
  it("requires the conservative legacy notice and completion basis to match the original record", () => {
    expect(parseResearchReviewItem(legacyItem())).not.toBeNull();
    const missing = legacyItem(); missing.prices.notice = null;
    expect(parseResearchReviewItem(missing)).toBeNull();
    const mismatch = item(); mismatch.prices.basis_at = "2026-10-01T09:00:00Z";
    expect(parseResearchReviewItem(mismatch)).toBeNull();
    const wide = item(); wide.prices.end_date = "2028-01-01";
    expect(parseResearchReviewItem(wide)).toBeNull();
  });
  it("binds pagination, source identity, dates, uniqueness and bounded output", () => {
    expect(parseResearchReviews(page(), identity.instrument_id)).not.toBeNull();
    expect(parseResearchReviews(page(), "600519.SH")).toBeNull();
    expect(parseResearchReviews({ ...page(), scanned: 21 })).toBeNull();
    expect(parseResearchReviews({ ...page(), next_cursor: null })).toBeNull();
    expect(parseResearchReviews(page([item(), item()]))).toBeNull();
    expect(parseResearchReviewDetail(detail(), { ...identity, sequence: 450 })).toBeNull();
    expect(parseResearchReviewDetail(detail(item({ sequence: 450 })), { ...identity, sequence: 450 })).not.toBeNull();
  });
  it("rejects out-of-range, duplicate, adjusted, synthetic and malformed future points", () => {
    expect(parseResearchReviewPrices(prices(), identity)).not.toBeNull();
    for (const value of [
      { ...prices(), source: "csv" }, { ...prices(), price_adjustment: "qfq" }, { ...prices(), data_authenticity: "synthetic" },
      { ...prices(), points: [{ date: "2026-10-01", close: 10 }] }, { ...prices(), points: [{ date: "2026-10-02", close: 0 }] },
      { ...prices(), points: [{ date: "2026-10-02", close: 10 }, { date: "2026-10-02", close: 11 }] },
      { ...prices(), item: item({ sequence: 2 }) }, { ...prices(), points: [] },
    ]) expect(parseResearchReviewPrices(value, identity)).toBeNull();
    const adjusted = prices(); adjusted.item.trade_plan!.price_adjustment = "unknown";
    expect(parseResearchReviewPrices(adjusted, identity)).toBeNull();
    expect(parseResearchReviewPrices({ ...prices(), status: "unavailable", retrieved_at: null, points: [], code: "review_prices_empty" }, identity)).not.toBeNull();
  });
});

describe("review HTTP client", () => {
  it("uses explicit filters and one raw record, sends no session token and only posts prices on demand", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(page())).mockResolvedValueOnce(response(detail())).mockResolvedValueOnce(response(prices()));
    const api = createApi(fetcher); const abort = new AbortController();
    await api.researchReviews!({ ...filters, from_date: "2026-10-01", to_date: "2026-10-06", cursor: "a+b" }, abort.signal);
    await api.researchReview!(identity, abort.signal); expect(fetcher).toHaveBeenCalledTimes(2);
    await api.researchReviewPrices!(identity, abort.signal);
    expect(fetcher.mock.calls[0][0]).toBe("/v1/research-reviews?instrument_id=000001.SZ&from_date=2026-10-01&to_date=2026-10-06&cursor=a%2Bb");
    expect(fetcher.mock.calls[1][0]).toBe(`/v1/research-reviews/${identity.conversation_id}/1?request_id=${identity.request_id}&instrument_id=000001.SZ`);
    expect(fetcher.mock.calls[2][0]).toBe(`/v1/research-reviews/${identity.conversation_id}/1/prices`);
    expect(fetcher.mock.calls[2][1]?.method).toBe("POST");
    expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({ request_id: identity.request_id, instrument_id: identity.instrument_id });
    for (const [, options] of fetcher.mock.calls) { expect(options?.headers).not.toHaveProperty("Authorization"); expect(options?.cache).toBe("no-store"); }
    expect(() => api.researchReviews!({ instrument_id: "平安银行" })).toThrow(ConnectionError);
    expect(() => api.researchReviews!({ ...filters, from_date: "2026-10-07", to_date: "2026-10-06" })).toThrow(ConnectionError);
    expect(() => api.researchReview!({ ...identity, sequence: -1 })).toThrow(ConnectionError);
  });
  it("rejects mismatched payloads and prevents development calls before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(page([item({ instrument_id: "600519.SH", trade_plan: null })])));
    await expect(createApi(fetcher).researchReviews!(filters)).rejects.toThrow(ConnectionError);
    fetcher.mockClear(); const dev = createApi(fetcher, "development");
    expect(() => dev.researchReviews!(filters)).toThrow(ApiError); expect(() => dev.researchReview!(identity)).toThrow(ApiError);
    expect(() => dev.researchReviewPrices!(identity)).toThrow(ApiError); expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("independent review controller", () => {
  it("has no automatic scan or price call and leaves research state and quotas unchanged", async () => {
    const { api, controller } = reviewController(); await controller.load();
    expect(api.researchReviews).not.toHaveBeenCalled(); controller.openResearchReviews();
    expect(api.researchReviews).not.toHaveBeenCalled(); const original = controller.snapshot();
    await controller.findResearchReviews(filters); await controller.loadResearchReview(item());
    expect(api.researchReviewPrices).not.toHaveBeenCalled(); await controller.loadResearchReviewPrices(item());
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled(); expect(api.cancel).not.toHaveBeenCalled();
    expect(controller.snapshot().session).toBe(original.session); expect(controller.snapshot().entries).toBe(original.entries);
    expect(controller.snapshot().phase).toBe(original.phase); expect(controller.snapshot().notice).toBe(original.notice);
    expect(api.researchReview.mock.calls[0][0]).toEqual(identity); expect(api.researchReviewPrices.mock.calls[0][0]).toEqual(identity);
  });
  it("continues one bounded page at a time without automatic looping", async () => {
    const { api, controller } = reviewController(); await controller.load(); controller.openResearchReviews();
    api.researchReviews.mockResolvedValueOnce(page([])); await controller.findResearchReviews(filters);
    expect(api.researchReviews).toHaveBeenCalledOnce(); expect(controller.snapshot().reviews?.has_more).toBe(true);
    await controller.continueResearchReviews(); expect(api.researchReviews).toHaveBeenCalledTimes(2);
    expect(api.researchReviews.mock.calls[1][0]).toEqual({ ...filters, cursor: "opaque-cursor" });
  });
  it.each(["filter", "close", "mode", "delete"])("cancels and ignores late list results after %s", async action => {
    const { api, controller } = reviewController(); await controller.load(); controller.openResearchReviews();
    const pending = deferred<ResearchReviews>(); api.researchReviews.mockReturnValueOnce(pending.promise);
    const loading = controller.findResearchReviews(filters), signal = api.researchReviews.mock.calls[0][1];
    if (action === "filter") controller.clearResearchReviews();
    if (action === "close") controller.closeResearchReviews();
    if (action === "mode") await controller.switchMode("development");
    if (action === "delete") await controller.deleteConversation(identity.conversation_id);
    expect(signal?.aborted).toBe(true); pending.resolve(page()); await loading;
    expect(controller.snapshot().reviews).toBeNull(); expect(controller.snapshot().reviewsLoading).toBe(false);
  });
  it.each(["filter", "close", "mode", "delete", "cancel"])("rejects late detail and price results after %s", async action => {
    const { api, controller } = reviewController(); await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    const raw = deferred<ResearchReviewDetail>(), future = deferred<ResearchReviewPrices>();
    api.researchReview.mockReturnValueOnce(raw.promise); api.researchReviewPrices.mockReturnValueOnce(future.promise);
    const reading = controller.loadResearchReview(item()), loading = controller.loadResearchReviewPrices(item());
    if (action === "filter") controller.clearResearchReviews();
    if (action === "close") controller.closeResearchReviews();
    if (action === "mode") await controller.switchMode("development");
    if (action === "delete") await controller.deleteConversation(identity.conversation_id);
    if (action === "cancel") controller.cancelResearchReviewPrices();
    expect(api.researchReviewPrices.mock.calls[0][1]?.aborted).toBe(true);
    future.resolve(prices()); raw.resolve(detail()); await Promise.all([reading, loading]);
    expect(controller.snapshot().reviewPrices).toBeNull();
    if (action !== "cancel") expect(controller.snapshot().reviewDetail).toBeNull();
    expect(controller.snapshot().reviewPricesLoading).toBe(false);
  });
  it("preserves original judgment on a source failure and never sends a disabled request", async () => {
    const { api, controller } = reviewController(); await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    const saved = controller.snapshot().reviews; api.researchReviewPrices.mockRejectedValueOnce(new ConnectionError());
    await controller.loadResearchReviewPrices(item()); expect(controller.snapshot().reviews).toBe(saved);
    expect(controller.snapshot().reviewPricesError).toContain("加载失败");
    const disabled = item(); disabled.prices.enabled = false;
    expect(await controller.loadResearchReviewPrices(disabled)).toBe(false); expect(api.researchReviewPrices).toHaveBeenCalledOnce();
  });
  it("refreshes list cutoffs from fresh detail and price receipts without fetching automatically", async () => {
    const { api, controller } = reviewController(); const old = legacyItem(), fresh = legacyItem();
    fresh.prices = { ...fresh.prices, basis_at: "2026-10-03T08:00:00Z", start_date: "2026-10-04" };
    api.researchReviews.mockResolvedValue(page([old])); api.researchReviewPrices.mockResolvedValue(prices(old));
    await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters); await controller.loadResearchReviewPrices(old);
    expect(controller.snapshot().reviewPrices).not.toBeNull();
    api.researchReview.mockResolvedValue(detail(fresh)); await controller.loadResearchReview(old);
    expect(controller.snapshot().reviews?.items[0].prices.start_date).toBe("2026-10-04");
    expect(controller.snapshot().reviewPrices).toBeNull(); expect(api.researchReviewPrices).toHaveBeenCalledOnce();
    const latest = { ...prices(fresh), points: [{ date: "2026-10-06", close: 10.8 }] };
    api.researchReviewPrices.mockResolvedValue(latest); await controller.loadResearchReviewPrices(fresh);
    expect(controller.snapshot().reviewPrices?.item.prices.start_date).toBe("2026-10-04");
    expect(controller.snapshot().reviewDetail?.item.prices.start_date).toBe("2026-10-04");
  });
  it("invalidates deleted records returned by another tab and disables synthetic/no-memory modes", async () => {
    const { api, controller } = reviewController(); await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    api.researchReview.mockRejectedValueOnce(new ApiError(404, "review_not_found")); await controller.loadResearchReview(item());
    expect(controller.snapshot().reviews).toBeNull(); expect(controller.snapshot().selectedReview).toBeNull();
    for (const healthPatch of [{ memory_enabled: false }, { data_mode: "synthetic" as const }]) {
      const other = reviewController(); other.api.health.mockResolvedValueOnce({ ...normalHealth, ...healthPatch }); await other.controller.load();
      expect(other.controller.openResearchReviews()).toBe(false); expect(await other.controller.findResearchReviews(filters)).toBe(false);
      expect(other.api.researchReviews).not.toHaveBeenCalled();
    }
  });
});

describe("review page and chart", () => {
  it("rounds only historical ratios to fixed two decimals and preserves all source strings", async () => {
    const { api, controller } = reviewController();
    const original = item({ rating: "买入", direction: "偏多" });
    original.trade_plan = { ...original.trade_plan!, purpose: "conditional_entry", values: {
      reference_entry: "10.123456789012345", reference_stop: "9.1", target_1: "11.5", target_2: "12.5",
      reward_risk_1: "2.000000", reward_risk_2: "1.485714", atr: "0.6667123456789012", atr_multiple: "1.5",
    } };
    const frozen = structuredClone(original); api.researchReviews.mockResolvedValue(page([original]));
    await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    render(<App controller={controller} />);
    expect(screen.getByRole("cell", { name: "2.00 倍" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "1.49 倍" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "10.123456789012345 元" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "0.6667123456789012 元" })).toBeVisible();
    expect(original).toEqual(frozen); expect(controller.snapshot().reviews?.items[0]).toEqual(frozen);
  });

  it("shows bounded empty-page continuation and loads a single old original question without research", async () => {
    const { api, controller } = reviewController(); api.researchReviews.mockResolvedValueOnce(page([]));
    render(<App controller={controller} />); const open = screen.getByRole("button", { name: "研判回顾" });
    await vi.waitFor(() => expect(open).not.toBeDisabled()); fireEvent.click(open);
    expect(api.researchReviews).not.toHaveBeenCalled(); fireEvent.change(screen.getByRole("textbox", { name: "完整证券代码" }), { target: { value: identity.instrument_id } });
    fireEvent.click(screen.getByRole("button", { name: "查找原研判" }));
    await screen.findByText("本页暂无匹配，请继续查找。"); expect(api.researchReviewPrices).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "继续查找" }));
    await screen.findByRole("button", { name: "查看原问答" }); fireEvent.click(screen.getByRole("button", { name: "查看原问答" }));
    await screen.findByText("第1轮完整原问题：分析平安银行的退出观察条件");
    expect(api.conversation).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled();
    expect(api.researchReviewPrices).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "加载之后走势" }));
    await screen.findByRole("region", { name: "平安银行的后续日线" }); expect(api.researchReviewPrices).toHaveBeenCalledOnce();
  });
  it("shows legacy cutoff limitations in the list, original detail and chart", async () => {
    const { api, controller } = reviewController(); const record = legacyItem();
    api.researchReviews.mockResolvedValue(page([record])); api.researchReview.mockResolvedValue(detail(record)); api.researchReviewPrices.mockResolvedValue(prices(record));
    await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    render(<App controller={controller} />); expect(screen.getByText(legacyReviewNotice)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "查看原问答" })); await screen.findByRole("region", { name: "当时原问答，只读" });
    expect(screen.getAllByText(legacyReviewNotice)).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "加载之后走势" })); await screen.findByRole("region", { name: "平安银行的后续日线" });
    expect(screen.getAllByText(legacyReviewNotice)).toHaveLength(3);
  });
  it("explains ineligible prices next to the disabled control and keeps date edits local", async () => {
    const { api, controller } = reviewController(); const record = item(); record.prices = { ...record.prices, enabled: false, message: "原价位复权口径未知，不能叠加后续走势。" };
    api.researchReviews.mockResolvedValue(page([record])); await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    render(<App controller={controller} />);
    const load = screen.getByRole("button", { name: "加载之后走势" }); expect(load).toBeDisabled();
    expect(load).toHaveAccessibleDescription(record.prices.message); fireEvent.click(load); expect(api.researchReviewPrices).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("提问起始日期"), { target: { value: "2026-10-03" } });
    expect(controller.snapshot().reviews).toBeNull(); expect(api.researchReviews).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "加载之后走势" })).not.toBeInTheDocument();
  });
  it("renders only original reference lines and actual closes with keyboard inspection", () => {
    render(<ResearchReviewChart data={prices()} />);
    const chart = screen.getByRole("region", { name: "平安银行的后续日线" });
    expect(within(chart).getByText(/腾讯行情 · 不复权/)).toBeVisible(); expect(within(chart).getByText(/行情 2026-09-30/)).toBeVisible();
    expect(within(chart).getByText("2026-10-02 · 实际收盘 10.2 元")).toBeVisible();
    fireEvent.keyDown(within(chart).getByRole("group"), { key: "End" }); expect(within(chart).getByText("2026-10-06 · 实际收盘 10.8 元")).toBeVisible();
    fireEvent.keyDown(within(chart).getByRole("group"), { key: "Home" }); expect(within(chart).getByText("2026-10-02 · 实际收盘 10.2 元")).toBeVisible();
    expect(chart.querySelectorAll(".review-reference-line")).toHaveLength(2);
    expect(chart.querySelector(".review-close-line")?.getAttribute("points")?.split(" ")).toHaveLength(2);
    expect(chart.textContent).not.toMatch(/\d+(?:\.\d+)?%|最大回撤|命中率：|收益：/);
  });
  it("retains judgments when cancelling price waiting and clears the panel when closed", async () => {
    const { api, controller } = reviewController(); await controller.load(); controller.openResearchReviews(); await controller.findResearchReviews(filters);
    const pending = deferred<ResearchReviewPrices>(); api.researchReviewPrices.mockReturnValueOnce(pending.promise);
    render(<App controller={controller} />); fireEvent.click(screen.getByRole("button", { name: "加载之后走势" }));
    expect(screen.getByRole("button", { name: "停止等待走势" })).toBeVisible(); fireEvent.click(screen.getByRole("button", { name: "停止等待走势" }));
    expect(screen.getByText("当时已形成研判")).toBeVisible(); expect(api.researchReviewPrices.mock.calls[0][1]?.aborted).toBe(true);
    pending.resolve(prices()); await act(async () => { await pending.promise; });
    expect(screen.queryByRole("region", { name: "平安银行的后续日线" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭回顾" })); expect(screen.queryByRole("region", { name: "研判回顾" })).not.toBeInTheDocument();
    expect(controller.snapshot().reviews).toBeNull();
  });
});

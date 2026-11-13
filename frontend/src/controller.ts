/** In-memory browser state machine: credentials and unresolved work never enter persistent storage. */
import { ApiError, ConnectionError, availableLiveProviders, type ConversationSummary, type Health, type LiveProvider,
  type MessageResponse, type ResearchApi, type SessionCredentials, type SessionView, type Watchlist, type WatchlistSearch } from "./api";
import type { PreferenceChanges, Preferences, SecuritySummary } from "./api";
import { parseResearchReviewDetail, parseResearchReviewPrices, parseResearchReviews, sameReview, validReviewFilters,
  type ReviewFilters, type ReviewIdentity, type ResearchReviews, type ResearchReviewDetail, type ResearchReviewPrices, type ResearchReviewItem } from "./researchReviews";

export interface Entry {
  id: string;
  question: string;
  history: boolean;
  response?: MessageResponse;
  error?: string;
}

export interface WorkspaceState {
  mode: "normal" | "development" | "standalone_demo";
  developmentModeAvailable: boolean;
  switching: boolean;
  health: Health | null;
  selectedProvider: LiveProvider | null;
  session: { shortId: string; limit: number; modelLimit: number; view: SessionView } | null;
  phase: "idle" | "working" | "uncertain";
  loading: boolean;
  cancelPending: boolean;
  pendingId: string | null;
  entries: Entry[];
  notice: string;
  noticeLevel: "info" | "error";
  /** Local history list; null when the service keeps no conversation record. */
  conversations: ConversationSummary[] | null;
  conversationId: string | null;
  watchlist: Watchlist | null;
  watchlistLoading: boolean;
  watchlistSaving: boolean;
  watchlistError: string | null;
  watchlistNotice: string | null;
  watchlistSearch: WatchlistSearch | null;
  watchlistSearchLoading: boolean;
  watchlistSearchError: string | null;
  preferences: Preferences | null;
  preferencesLoading: boolean;
  preferencesSaving: boolean;
  preferencesError: string | null;
  preferencesNotice: string | null;
  preferencesSearch: WatchlistSearch | null;
  preferencesSearchLoading: boolean;
  preferencesSearchError: string | null;
  preferencesEditor: { security: SecuritySummary; generation: number } | null;
  reviewsOpen: boolean;
  reviewFilters: ReviewFilters | null;
  reviews: ResearchReviews | null;
  reviewsLoading: boolean;
  reviewsError: string | null;
  selectedReview: ReviewIdentity | null;
  reviewDetail: ResearchReviewDetail | null;
  reviewDetailLoading: boolean;
  reviewDetailError: string | null;
  reviewPrices: ResearchReviewPrices | null;
  reviewPricesLoading: boolean;
  reviewPricesError: string | null;
}

const emptyView = (): SessionView => ({ state: "idle", active_request_id: null, messages_used: 0, model_calls_forwarded: 0 });
const explanation = (error: unknown) => error instanceof ApiError || error instanceof ConnectionError
  ? error.message : "本次操作未得到可确认结果，没有自动重试。";
const terminalCodes = new Set(["session_message_limit", "session_not_found", "session_token_required", "request_id_conflict",
  "request_id_retired", "directory_unusable", "research_internal_error", "invalid_request", "service_closed",
  "json_required", "request_too_large", "invalid_content_length", "origin_not_allowed"]);

export class ResearchController {
  private api: ResearchApi;
  private readonly normalApi: ResearchApi;
  private normalServiceEstablished = false;
  private normalProvider: Health["provider"] | null = null;
  private credentials: SessionCredentials | null = null;
  private pending: { id: string; question: string } | null = null;
  private running = false;
  private personalEpoch = 0;
  private watchlistRead = 0;
  private watchlistSearchRead = 0;
  private preferencesRead = 0;
  private preferencesSearchRead = 0;
  private preferencesEditorGeneration = 0;
  private reviewEpoch = 0;
  private reviewListAbort: AbortController | null = null;
  private reviewDetailAbort: AbortController | null = null;
  private reviewPricesAbort: AbortController | null = null;
  private listeners = new Set<() => void>();
  private state: WorkspaceState = { mode: "normal", developmentModeAvailable: false, switching: false,
    health: null, selectedProvider: null, session: null, phase: "idle", loading: false,
    cancelPending: false, pendingId: null, entries: [], notice: "正在检查本机服务…", noticeLevel: "info",
    conversations: null, conversationId: null, watchlist: null, watchlistLoading: false, watchlistSaving: false,
    watchlistError: null, watchlistNotice: null, watchlistSearch: null, watchlistSearchLoading: false, watchlistSearchError: null,
    preferences: null, preferencesLoading: false, preferencesSaving: false, preferencesError: null, preferencesNotice: null,
    preferencesSearch: null, preferencesSearchLoading: false, preferencesSearchError: null, preferencesEditor: null,
    reviewsOpen: false, reviewFilters: null, reviews: null, reviewsLoading: false, reviewsError: null, selectedReview: null,
    reviewDetail: null, reviewDetailLoading: false, reviewDetailError: null,
    reviewPrices: null, reviewPricesLoading: false, reviewPricesError: null };

  constructor(api: ResearchApi,
    private readonly nextId: () => string = () => crypto.randomUUID().replaceAll("-", ""),
    private readonly developmentApi: ResearchApi | null = null) {
    if (api.mode === "development" || developmentApi !== null
      && (developmentApi === api || developmentApi.mode !== "development")) throw new ConnectionError();
    this.api = api;
    this.normalApi = api;
  }

  snapshot = () => this.state;
  loadChart = async (entryId: string, interval: "1w" | "60m" | "120m", signal?: AbortSignal): Promise<unknown> => {
    const credentials = this.credentials, api = this.api;
    const entry = this.state.entries.find((item) => item.id === entryId);
    const researchId = entry?.response?.result.report?.request_id;
    if (!credentials || !researchId || !api.chart || this.state.switching) throw new ConnectionError();
    const result = await api.chart(credentials, researchId, interval, signal);
    if (this.credentials !== credentials || this.api !== api || signal?.aborted
      || !this.state.entries.some((item) => item === entry)) throw new ConnectionError();
    return result;
  };
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };

  private update(patch: Partial<WorkspaceState>) {
    this.state = { ...this.state, ...patch,
      ...(patch.notice !== undefined ? { noticeLevel: patch.noticeLevel ?? "info" } : {}) };
    this.listeners.forEach((listener) => listener());
  }
  private append(entry: Entry) { this.update({ entries: [...this.state.entries, entry].slice(-40) }); }
  private amend(id: string, patch: Partial<Entry>) {
    this.update({ entries: this.state.entries.map((entry) => entry.id === id ? { ...entry, ...patch } : entry) });
  }
  private forget() {
    this.credentials = null;
    this.pending = null;
    this.update({ session: null, pendingId: null, phase: "idle" });
  }
  private failure(error: unknown) {
    if (error instanceof ApiError && [401, 404].includes(error.status)) this.forget();
    this.update({ notice: explanation(error), noticeLevel: "error" });
  }
  private view(view: SessionView) {
    if (this.state.session) this.update({ session: { ...this.state.session, view } });
  }
  private allowed() { return this.state.health !== null; }

  private acceptHealth(health: Health) {
    if (this.api === this.normalApi) {
      const offline = health.provider === "fake" && health.data_mode === "synthetic";
      if (this.normalServiceEstablished && offline || this.normalProvider !== null && this.normalProvider !== "fake" && health.provider === "fake")
        throw new ConnectionError();
      const standalone = offline && health.development_mode_available !== true;
      if (!standalone) { this.normalServiceEstablished = true; this.normalProvider = health.provider; }
      const available = availableLiveProviders(health);
      const current = this.state.selectedProvider;
      const selectedProvider = current && available.includes(current) ? current
        : health.provider !== "fake" && (health.available_providers === undefined || available.includes(health.provider))
          ? health.provider : available[0] ?? null;
      this.update({ health, mode: standalone ? "standalone_demo" : "normal",
        selectedProvider,
        developmentModeAvailable: health.development_mode_available === true && this.developmentApi !== null });
    } else {
      if (health.provider !== "fake" || health.data_mode !== "synthetic") throw new ConnectionError();
      // The development endpoint cannot revoke the capability advertised by the root endpoint.
      this.update({ health, mode: "development" });
    }
    if (!this.reviewsAvailable()) this.closeResearchReviews();
  }

  async load() {
    if (this.running || this.credentials) return;
    this.running = true;
    this.update({ loading: true });
    try {
      const health = await this.api.health();
      this.acceptHealth(health);
      this.update({ notice: "服务已连接。直接输入问题，首次发送时自动建立对话。" });
      await this.refreshConversations();
      await this.refreshWatchlist();
      await this.refreshPreferences();
    } catch (error) { this.update({ health: null, notice: explanation(error), noticeLevel: "error" }); }
    finally { this.running = false; this.update({ loading: false }); }
  }

  async switchMode(target: "normal" | "development"): Promise<boolean> {
    if (this.running || this.pending || this.state.phase !== "idle" || this.state.cancelPending
      || this.state.session?.view.state === "busy" || !this.state.developmentModeAvailable
      || target === this.state.mode || this.state.mode === "standalone_demo"
      || target !== "normal" && target !== "development") return false;
    const targetApi = target === "development" ? this.developmentApi : this.normalApi;
    if (!targetApi) return false;
    this.running = true;
    this.clearPersonalState();
    this.closeResearchReviews();
    this.update({ loading: true, switching: true });
    try {
      if (this.credentials) {
        try { await this.api.end(this.credentials); }
        catch (error) {
          if (!(error instanceof ApiError) || ![401, 404].includes(error.status)) throw error;
        }
      }
      this.credentials = null;
      this.pending = null;
      this.api = targetApi;
      this.update({ mode: target, health: null, session: null, pendingId: null, entries: [], phase: "idle",
        conversationId: null, conversations: null,
        notice: `正在切换至${target === "development" ? "测试" : "正常"}模式…` });
      try {
        this.acceptHealth(await targetApi.health());
        this.update({ notice: `已切换至${target === "development" ? "测试" : "正常"}模式，旧对话和草稿已清空。发送问题后才会开始研究。` });
      } catch (error) {
        this.update({ health: null, notice: `已选择${target === "development" ? "测试" : "正常"}模式，但服务未连接。旧对话和草稿已清空；没有自动回退或发起研究。${explanation(error)}`,
          noticeLevel: "error" });
      }
      return true;
    } catch (error) {
      // Unconfirmed cleanup preserves the old endpoint, credentials, messages and draft.
      this.update({ notice: `未能清理原会话，尚未切换模式。${explanation(error)}`, noticeLevel: "error" });
      return false;
    } finally {
      this.running = false; this.update({ loading: false, switching: false });
      void this.refreshWatchlist();
      void this.refreshPreferences();
    }
  }

  async start() {
    if (this.running || this.credentials || !this.allowed()) return;
    this.running = true;
    this.update({ loading: true });
    try {
      await this.createSession();
    } catch (error) { this.failure(error); }
    finally { this.running = false; this.update({ loading: false }); }
  }

  async switchProvider(target: LiveProvider): Promise<boolean> {
    if (this.running || this.pending || this.state.phase !== "idle" || this.state.cancelPending
      || this.state.mode !== "normal" || this.state.session?.view.state === "busy"
      || !availableLiveProviders(this.state.health).includes(target) || target === this.state.selectedProvider) return false;
    this.running = true;
    this.update({ loading: true, switching: true });
    try {
      if (this.credentials) {
        try { await this.api.end(this.credentials); }
        catch (error) {
          if (!(error instanceof ApiError) || ![401, 404].includes(error.status)) throw error;
        }
      }
      this.credentials = null;
      this.pending = null;
      this.update({ selectedProvider: target, session: null, pendingId: null, entries: [], phase: "idle", conversationId: null,
        notice: `已切换至${target === "deepseek" ? "DeepSeek" : "OpenAI"}，旧对话和草稿已清空。发送问题后才会开始研究。` });
      return true;
    } catch (error) {
      // Keep the selected model and all conversation state until cleanup is confirmed.
      this.update({ notice: `未能清理原会话，尚未切换模型。${explanation(error)}`, noticeLevel: "error" });
      return false;
    } finally { this.running = false; this.update({ loading: false, switching: false }); }
  }

  private async createSession() {
    const selected = this.state.mode === "normal" ? this.state.selectedProvider : null;
    const conversation = this.state.conversationId ?? undefined;
    const provider = selected && this.state.health?.available_providers !== undefined ? selected : undefined;
    const credentials = conversation ? await this.api.create(provider, conversation)
      : provider ? await this.api.create(provider) : await this.api.create();
    if (credentials.provider !== (selected ?? this.state.health?.provider) || credentials.data_mode !== this.state.health?.data_mode)
      throw new ConnectionError();
    this.credentials = credentials;
    // Continuing a saved conversation keeps its earlier entries on screen; a new one starts empty.
    this.update({ session: { shortId: credentials.session_id.slice(0, 8), limit: credentials.limits.messages,
      modelLimit: credentials.limits.model_calls, view: emptyView() }, entries: conversation ? this.state.entries : [],
      notice: conversation ? "已接着这段历史对话继续；会重新查询行情，不沿用旧价位。" : "对话已建立。" });
  }

  async send(question: string): Promise<boolean> {
    question = question.trim();
    if (this.running || this.pending || !this.allowed() || this.state.phase !== "idle"
      || !question || Array.from(question).length > 2000 || this.state.session?.view.state === "busy"
      || this.state.session && this.state.session.view.messages_used >= this.state.session.limit) return false;
    // Hold one synchronous gate across creation and sending, not one gate per HTTP call.
    this.running = true;
    this.update({ loading: true, notice: this.credentials ? "正在发送问题…" : "正在建立对话…" });
    try {
      if (!this.credentials) await this.createSession();
      const id = this.nextId();
      this.pending = { id, question };
      this.append({ id, question, history: false });
      await this.attempt();
      return true;
    } catch (error) {
      this.update({ notice: error instanceof ConnectionError
        ? "未能确认对话创建成功，问题尚未发送。请检查连接，再点击发送重试。"
        : `未能建立对话，问题尚未发送。${explanation(error)}`, noticeLevel: "error" });
      return false;
    } finally { this.running = false; this.update({ loading: false }); }
  }

  async retry() {
    if (!this.running && this.pending && this.credentials && this.allowed()) await this.attempt();
  }

  private async attempt() {
    const pending = this.pending, credentials = this.credentials;
    if (!pending || !credentials) return;
    this.running = true;
    this.update({ phase: "working", loading: true, pendingId: pending.id, notice: "正在处理。没有自动重试；取消仅发出协作信号。" });
    this.amend(pending.id, { error: undefined });
    try {
      const response = await this.api.send(credentials, pending.id, pending.question);
      this.amend(pending.id, { response });
      this.pending = null;
      // A replay is an earlier receipt, never the current personal list.
      if (response.result.personal_memory != null) {
        void this.refreshWatchlist(); void this.refreshPreferences();
      }
      if (response.conversation_id && this.credentials === credentials) {
        this.invalidateReviewConversation(response.conversation_id);
        const created = response.conversation_id !== this.state.conversationId;
        this.update({ conversationId: response.conversation_id });
        if (created || this.state.conversations !== null) void this.refreshConversations();
      }
      this.update({ phase: "idle", pendingId: null, notice: response.replayed
        ? "已取回原请求响应，没有再次调用模型。" : "本轮回答已完成。" });
      // Quota refresh is read-only and must not hide a received research result.
      try { this.view(await this.api.inspect(credentials)); }
      catch (error) {
        if (error instanceof ApiError && [401, 404].includes(error.status)) this.failure(error);
        else this.update({ notice: "本轮答复已收到，但会话状态刷新失败。可手动检查状态；不会重做研究。", noticeLevel: "error" });
      }
    } catch (error) {
      this.amend(pending.id, { error: explanation(error) });
      // Unknown proxy/server failures may have happened after execution; retain the id.
      const unresolved = !(error instanceof ApiError)
        || ![401, 404].includes(error.status) && !terminalCodes.has(error.code);
      if (unresolved) this.update({ phase: "uncertain", notice: explanation(error), noticeLevel: "error" });
      else {
        this.pending = null;
        this.update({ phase: "idle", pendingId: null });
        this.failure(error);
      }
    } finally { this.running = false; this.update({ loading: false }); }
  }

  async cancel() {
    const pending = this.pending, credentials = this.credentials;
    if (!pending || !credentials || this.state.cancelPending) return;
    this.update({ cancelPending: true });
    try {
      await this.api.cancel(credentials, pending.id);
      if (this.pending?.id === pending.id)
        this.update({ notice: "取消信号已发送。仍需等待原请求返回；远程请求可能还在执行或计费。" });
    } catch (error) {
      if (this.pending?.id === pending.id) this.update({ notice: explanation(error), noticeLevel: "error" });
    } finally { this.update({ cancelPending: false }); }
  }

  async inspect() {
    if (this.running || !this.credentials) return;
    this.running = true;
    this.update({ loading: true });
    try {
      const view = await this.api.inspect(this.credentials);
      this.view(view);
      this.update({ notice: view.state === "busy" ? "服务端仍在执行；请稍后检查或取消。"
        : this.pending ? "服务端当前空闲。请重试原请求，取回原响应或执行尚未接纳的请求。"
          : "会话状态与额度已更新。" });
    } catch (error) { this.failure(error); }
    finally { this.running = false; this.update({ loading: false }); }
  }

  async history() {
    if (this.running || this.pending || !this.credentials) return;
    this.running = true;
    this.update({ loading: true });
    try {
      const response = await this.api.history(this.credentials);
      this.append({ id: this.nextId(), question: "上次保存的研究 · 历史查看", history: true, response });
      this.update({ notice: "已查看历史，没有新研究、模型调用或行情请求。历史不代表新问题成功。" });
    } catch (error) { this.failure(error); }
    finally { this.running = false; this.update({ loading: false }); }
  }

  async reset() {
    if (this.running || this.pending || !this.credentials) return;
    this.running = true;
    this.update({ loading: true });
    try {
      this.view(await this.api.reset(this.credentials));
      this.update({ entries: [], conversationId: null,
        notice: "研究条件与结果已清空，额度不退。已保存的历史对话不受影响；之后的问题会记为新对话。" });
    } catch (error) { this.failure(error); }
    finally { this.running = false; this.update({ loading: false }); }
  }

  async end(): Promise<boolean> {
    if (this.running || this.pending) return false;
    this.running = true;
    this.update({ loading: true });
    try {
      if (this.credentials) {
        try { await this.api.end(this.credentials); }
        catch (error) {
          // An explicitly discarded, expired session needs no further server cleanup.
          if (!(error instanceof ApiError) || ![401, 404].includes(error.status)) throw error;
        }
      }
      this.forget();
      this.update({ entries: [], conversationId: null, notice: "已回到新对话。直接发送问题即可开始。" });
      return true;
    } catch (error) { this.failure(error); return false; }
    finally { this.running = false; this.update({ loading: false }); }
  }

  private clearPersonalState() {
    this.personalEpoch++; this.watchlistRead++; this.watchlistSearchRead++;
    this.preferencesRead++; this.preferencesSearchRead++; this.preferencesEditorGeneration++;
    this.update({ watchlist: null, watchlistLoading: false, watchlistSaving: false, watchlistError: null,
      watchlistNotice: null, watchlistSearch: null, watchlistSearchLoading: false, watchlistSearchError: null,
      preferences: null, preferencesLoading: false, preferencesSaving: false, preferencesError: null, preferencesNotice: null,
      preferencesSearch: null, preferencesSearchLoading: false, preferencesSearchError: null, preferencesEditor: null });
  }

  private personalAvailable() {
    return this.state.mode === "normal" && this.api === this.normalApi && this.state.health?.personal_memory_enabled === true
      && this.state.health.data_mode !== "synthetic";
  }

  async refreshWatchlist(): Promise<void> {
    if (this.state.switching) return;
    const api = this.api, epoch = this.personalEpoch, request = ++this.watchlistRead;
    if (!this.personalAvailable() || !api.watchlist) {
      this.watchlistSearchRead++;
      this.update({ watchlist: { version: "personal-memory-watchlist-v1", enabled: false, limit: 50, items: [] },
        watchlistLoading: false, watchlistError: null, watchlistSearch: null, watchlistSearchLoading: false, watchlistSearchError: null });
      return;
    }
    this.update({ watchlistLoading: true, watchlistError: null });
    try {
      const watchlist = await api.watchlist!();
      if (this.api === api && this.personalEpoch === epoch && request === this.watchlistRead) {
        if (!watchlist.enabled) this.watchlistSearchRead++;
        this.update({ watchlist, ...(!watchlist.enabled ? { watchlistSearch: null, watchlistSearchLoading: false, watchlistSearchError: null } : {}) });
      }
    } catch (error) {
      if (this.api === api && this.personalEpoch === epoch && request === this.watchlistRead)
        this.update({ watchlistError: `自选读取失败。${explanation(error)}` });
    } finally {
      if (this.api === api && this.personalEpoch === epoch && request === this.watchlistRead) this.update({ watchlistLoading: false });
    }
  }

  clearWatchlistSearch() {
    this.watchlistSearchRead++;
    this.update({ watchlistSearch: null, watchlistSearchLoading: false, watchlistSearchError: null });
  }

  async searchWatchlist(query: string): Promise<boolean> {
    query = query.trim();
    if (!this.personalAvailable() || this.state.switching || !this.api.searchWatchlist || !query || Array.from(query).length > 80) return false;
    const api = this.api, epoch = this.personalEpoch, request = ++this.watchlistSearchRead;
    this.update({ watchlistSearch: null, watchlistSearchLoading: true, watchlistSearchError: null });
    try {
      const result = await api.searchWatchlist!(query);
      if (this.api === api && this.personalEpoch === epoch && request === this.watchlistSearchRead)
        this.update(result.enabled ? { watchlistSearch: result }
          : { watchlist: { version: "personal-memory-watchlist-v1", enabled: false, limit: 50, items: [] }, watchlistSearch: null });
      return true;
    } catch (error) {
      if (this.api === api && this.personalEpoch === epoch && request === this.watchlistSearchRead)
        this.update({ watchlistSearchError: `目录搜索失败。${explanation(error)}` });
      return false;
    } finally {
      if (this.api === api && this.personalEpoch === epoch && request === this.watchlistSearchRead)
        this.update({ watchlistSearchLoading: false });
    }
  }

  addWatchlist = (id: string) => this.changeWatchlist("add", id);
  removeWatchlist = (id: string) => this.changeWatchlist("remove", id);
  clearWatchlist = () => this.changeWatchlist("clear");

  private async changeWatchlist(action: "add" | "remove" | "clear", id?: string): Promise<boolean> {
    if (!this.personalAvailable() || this.state.switching || this.state.watchlistSaving) return false;
    const api = this.api, epoch = this.personalEpoch;
    const operation = action === "add" ? api.addWatchlist : action === "remove" ? api.removeWatchlist : api.clearWatchlist;
    if (!operation) return false;
    this.watchlistRead++; this.watchlistSearchRead++;
    this.update({ watchlistSaving: true, watchlistLoading: false, watchlistError: null, watchlistNotice: null,
      watchlistSearchLoading: false });
    try {
      const result = action === "clear" ? await api.clearWatchlist!()
        : action === "add" ? await api.addWatchlist!(id!) : await api.removeWatchlist!(id!);
      if (!result.enabled) throw new ApiError(409, "personal_memory_disabled");
      if (this.api === api && this.personalEpoch === epoch) {
        this.update({ watchlist: result, watchlistNotice: action === "add" ? "已保存自选。" : action === "remove" ? "已移除自选。" : "已清空自选；聊天历史未删除。" });
        await this.refreshWatchlist();
      }
      return true;
    } catch (error) {
      if (this.api === api && this.personalEpoch === epoch)
        this.update({ watchlistError: `自选变更未确认，请刷新后核对。${explanation(error)}` });
      return false;
    } finally {
      if (this.api === api && this.personalEpoch === epoch) this.update({ watchlistSaving: false });
    }
  }

  async refreshPreferences(): Promise<void> {
    if (this.state.switching) return;
    const api = this.api, epoch = this.personalEpoch, request = ++this.preferencesRead;
    if (!this.personalAvailable() || !api.preferences) {
      this.preferencesSearchRead++;
      this.update({ preferences: { version: "personal-memory-preferences-v1", enabled: false, limit: 50, items: [], collection_revision: null },
        preferencesLoading: false, preferencesError: null, preferencesSearch: null, preferencesSearchLoading: false,
        preferencesSearchError: null, preferencesEditor: null });
      return;
    }
    this.update({ preferencesLoading: true, preferencesError: null });
    try {
      const preferences = await api.preferences();
      if (this.api === api && this.personalEpoch === epoch && request === this.preferencesRead) {
        if (!preferences.enabled) this.preferencesSearchRead++;
        this.update({ preferences, ...(!preferences.enabled ? { preferencesSearch: null, preferencesSearchLoading: false,
          preferencesSearchError: null, preferencesEditor: null } : {}) });
      }
    } catch (error) {
      if (this.api === api && this.personalEpoch === epoch && request === this.preferencesRead)
        this.update({ preferencesError: `持仓与偏好读取失败。${explanation(error)}` });
    } finally {
      if (this.api === api && this.personalEpoch === epoch && request === this.preferencesRead) this.update({ preferencesLoading: false });
    }
  }

  clearPreferencesSearch() {
    this.preferencesSearchRead++;
    this.update({ preferencesSearch: null, preferencesSearchLoading: false, preferencesSearchError: null });
  }

  async searchPreferences(query: string): Promise<boolean> {
    query = query.trim();
    if (!this.personalAvailable() || this.state.switching || !this.api.searchWatchlist || !query || Array.from(query).length > 80) return false;
    const api = this.api, epoch = this.personalEpoch, request = ++this.preferencesSearchRead;
    this.update({ preferencesSearch: null, preferencesSearchLoading: true, preferencesSearchError: null });
    try {
      const result = await api.searchWatchlist!(query);
      if (this.api === api && this.personalEpoch === epoch && request === this.preferencesSearchRead) {
        if (!result.enabled) throw new ApiError(404, "personal_memory_disabled");
        this.update({ preferencesSearch: result });
      }
      return true;
    } catch (error) {
      if (this.api === api && this.personalEpoch === epoch && request === this.preferencesSearchRead)
        this.update({ preferencesSearchError: `目录搜索失败。${explanation(error)}` });
      return false;
    } finally {
      if (this.api === api && this.personalEpoch === epoch && request === this.preferencesSearchRead)
        this.update({ preferencesSearchLoading: false });
    }
  }

  openPreferenceEditor(security: SecuritySummary): boolean {
    if (!this.personalAvailable() || this.state.switching || this.state.preferencesSaving || this.state.preferencesLoading
        || this.state.preferencesError || !this.state.preferences?.enabled) return false;
    const stored = this.state.preferences.items.find(item => item.instrument_id === security.instrument_id);
    this.update({ preferencesEditor: { security: { instrument_id: security.instrument_id, name: stored?.name ?? security.name },
      generation: ++this.preferencesEditorGeneration }, preferencesNotice: null });
    return true;
  }

  closePreferenceEditor(generation?: number) {
    if (generation !== undefined && this.state.preferencesEditor?.generation !== generation) return;
    this.update({ preferencesEditor: null });
  }

  savePreferences = (id: string, expectedRevision: string | null, changes: PreferenceChanges) =>
    this.changePreferences("save", id, expectedRevision, changes);
  deletePreference = (id: string, revision: string) => this.changePreferences("delete", id, revision);
  clearPreferences = (revision: string) => this.changePreferences("clear", undefined, revision);

  private async changePreferences(action: "save" | "delete" | "clear", id?: string, revision?: string | null, changes?: PreferenceChanges): Promise<boolean> {
    if (!this.personalAvailable() || this.state.switching || this.state.preferencesSaving) return false;
    const api = this.api, epoch = this.personalEpoch;
    if (action === "save" ? !api.patchPreferences : action === "delete" ? !api.deletePreference : !api.clearPreferences) return false;
    this.preferencesRead++; this.preferencesSearchRead++;
    this.update({ preferencesSaving: true, preferencesLoading: false, preferencesError: null, preferencesNotice: null, preferencesSearchLoading: false });
    try {
      const result = action === "save" ? await api.patchPreferences!(id!, revision ?? null, changes!)
        : action === "delete" ? await api.deletePreference!(id!, revision!) : await api.clearPreferences!(revision!);
      if (!result.enabled) throw new ApiError(404, "personal_memory_disabled");
      if (this.api === api && this.personalEpoch === epoch) {
        const close = action === "clear" || action === "delete" && this.state.preferencesEditor?.security.instrument_id === id;
        this.update({ preferences: result, ...(close ? { preferencesEditor: null } : {}),
          preferencesNotice: action === "save" ? "已保存本机资料。" : "已删除本机资料；自选和聊天历史未删除。" });
        await this.refreshPreferences();
      }
      return true;
    } catch (error) {
      if (this.api === api && this.personalEpoch === epoch)
        this.update({ preferencesError: error instanceof ApiError && error.code === "preference_revision_conflict"
          ? explanation(error) : `资料变更未确认，请刷新后核对。${explanation(error)}` });
      return false;
    } finally {
      if (this.api === api && this.personalEpoch === epoch) this.update({ preferencesSaving: false });
    }
  }

  async refreshConversations() {
    const api = this.api;
    if (!api.conversations || this.state.health?.memory_enabled !== true) {
      this.update({ conversations: null });
      return;
    }
    try {
      const conversations = await api.conversations();
      if (this.api === api) this.update({ conversations });
    } catch {
      // The history list is optional; research continues without it.
    }
  }

  /** Review browsing is independent of research credentials, work and message allowances. */
  private reviewsAvailable() {
    return this.state.mode === "normal" && this.api === this.normalApi && !this.state.switching
      && this.state.health?.memory_enabled === true && this.state.health.data_mode !== "synthetic"
      && this.state.health.provider !== "fake";
  }

  openResearchReviews(): boolean {
    if (!this.reviewsAvailable() || !this.api.researchReviews) return false;
    this.update({ reviewsOpen: true });
    return true;
  }

  clearResearchReviews() {
    this.reviewEpoch++;
    this.reviewListAbort?.abort(); this.reviewDetailAbort?.abort(); this.reviewPricesAbort?.abort();
    this.reviewListAbort = this.reviewDetailAbort = this.reviewPricesAbort = null;
    this.update({ reviewFilters: null, reviews: null, reviewsLoading: false, reviewsError: null, selectedReview: null,
      reviewDetail: null, reviewDetailLoading: false, reviewDetailError: null,
      reviewPrices: null, reviewPricesLoading: false, reviewPricesError: null });
  }

  closeResearchReviews() { this.clearResearchReviews(); this.update({ reviewsOpen: false }); }

  private invalidateReviewConversation(id: string) {
    if (this.state.reviews?.items.some(item => item.conversation_id === id) || this.state.selectedReview?.conversation_id === id
      || this.state.reviewsLoading) {
      this.clearResearchReviews();
      if (this.state.reviewsOpen) this.update({ reviewsError: "历史已变化，请重新查找；旧记录的后续走势起点可能已后移。" });
    }
  }

  async findResearchReviews(filters: ReviewFilters): Promise<boolean> {
    if (!this.reviewsAvailable() || !this.state.reviewsOpen || !this.api.researchReviews || !validReviewFilters(filters)) return false;
    const api = this.api;
    this.clearResearchReviews();
    const epoch = this.reviewEpoch, abort = new AbortController(); this.reviewListAbort = abort;
    const saved = { ...filters };
    this.update({ reviewFilters: saved, reviewsLoading: true });
    try {
      const response = await api.researchReviews!(saved, abort.signal);
      if (this.reviewEpoch !== epoch || this.api !== api || abort.signal.aborted) return false;
      const reviews = parseResearchReviews(response, saved.instrument_id);
      if (!reviews) throw new ConnectionError();
      this.update({ reviews });
      return true;
    } catch (error) {
      if (this.reviewEpoch === epoch && this.api === api && !abort.signal.aborted)
        this.update({ reviewsError: `研判回顾读取失败。${explanation(error)}` });
      return false;
    } finally {
      if (this.reviewEpoch === epoch && this.api === api && !abort.signal.aborted) this.update({ reviewsLoading: false });
    }
  }

  continueResearchReviews() {
    const filters = this.state.reviewFilters, page = this.state.reviews;
    if (!filters || !page?.has_more || !page.next_cursor || this.state.reviewsLoading) return Promise.resolve(false);
    return this.findResearchReviews({ ...filters, cursor: page.next_cursor });
  }

  private selectReview(item: ResearchReviewItem): boolean {
    if (!this.reviewsAvailable() || !this.state.reviewsOpen || !this.state.reviews?.items.some(value => sameReview(value, item))) return false;
    if (!this.state.selectedReview || !sameReview(this.state.selectedReview, item)) {
      this.reviewDetailAbort?.abort(); this.reviewPricesAbort?.abort();
      this.update({ selectedReview: { conversation_id: item.conversation_id, sequence: item.sequence, request_id: item.request_id, instrument_id: item.instrument_id },
        reviewDetail: null, reviewDetailLoading: false, reviewDetailError: null,
        reviewPrices: null, reviewPricesLoading: false, reviewPricesError: null });
    }
    return true;
  }

  private refreshReviewItem(item: ResearchReviewItem, fromDetail: boolean) {
    const page = this.state.reviews, previous = page?.items.find(value => sameReview(value, item));
    // A slower old detail must not move a legacy cutoff back after a newer response.
    if (!page || !previous || previous.prices.basis_at && item.prices.basis_at
      && Date.parse(item.prices.basis_at) < Date.parse(previous.prices.basis_at)) throw new ConnectionError();
    const changed = JSON.stringify(previous.prices) !== JSON.stringify(item.prices);
    if (fromDetail && changed) this.reviewPricesAbort?.abort();
    this.update({ reviews: { ...page, items: page.items.map(value => sameReview(value, item) ? item : value) },
      ...(this.state.reviewDetail && sameReview(this.state.reviewDetail.item, item) ? { reviewDetail: { ...this.state.reviewDetail, item } } : {}),
      ...(fromDetail && changed ? { reviewPrices: null, reviewPricesLoading: false, reviewPricesError: null } : {}) });
  }

  async loadResearchReview(item: ResearchReviewItem): Promise<boolean> {
    if (!this.api.researchReview || !this.selectReview(item)) return false;
    this.reviewDetailAbort?.abort();
    const api = this.api, epoch = this.reviewEpoch, abort = new AbortController(); this.reviewDetailAbort = abort;
    this.update({ reviewDetail: null, reviewDetailLoading: true, reviewDetailError: null });
    try {
      const response = await api.researchReview!(this.state.selectedReview!, abort.signal);
      if (this.reviewEpoch !== epoch || this.api !== api || abort.signal.aborted) return false;
      const detail = parseResearchReviewDetail(response, item);
      if (!detail) throw new ConnectionError();
      this.refreshReviewItem(detail.item, true);
      this.update({ reviewDetail: detail });
      return true;
    } catch (error) {
      if (this.reviewEpoch === epoch && this.api === api && !abort.signal.aborted) {
        if (error instanceof ApiError && ["review_not_found", "conversation_not_found"].includes(error.code)) this.clearResearchReviews();
        this.update({ reviewDetailError: `原记录读取失败。${explanation(error)}` });
      }
      return false;
    } finally {
      if (this.reviewEpoch === epoch && this.api === api && !abort.signal.aborted) this.update({ reviewDetailLoading: false });
    }
  }

  async loadResearchReviewPrices(item: ResearchReviewItem): Promise<boolean> {
    if (this.state.reviewPricesLoading || !item.prices.enabled || !this.api.researchReviewPrices || !this.selectReview(item)) return false;
    this.reviewPricesAbort?.abort();
    const api = this.api, epoch = this.reviewEpoch, abort = new AbortController(); this.reviewPricesAbort = abort;
    this.update({ reviewPrices: null, reviewPricesLoading: true, reviewPricesError: null });
    try {
      const response = await api.researchReviewPrices!(this.state.selectedReview!, abort.signal);
      if (this.reviewEpoch !== epoch || this.api !== api || abort.signal.aborted) return false;
      const prices = parseResearchReviewPrices(response, item);
      if (!prices) throw new ConnectionError();
      this.refreshReviewItem(prices.item, false);
      this.update({ reviewPrices: prices });
      return true;
    } catch (error) {
      if (this.reviewEpoch === epoch && this.api === api && !abort.signal.aborted) {
        if (error instanceof ApiError && ["review_not_found", "conversation_not_found", "review_source_changed"].includes(error.code)) this.clearResearchReviews();
        this.update({ reviewPricesError: `后续走势加载失败。${explanation(error)}` });
      }
      return false;
    } finally {
      if (this.reviewEpoch === epoch && this.api === api && !abort.signal.aborted) this.update({ reviewPricesLoading: false });
    }
  }

  cancelResearchReviewPrices() {
    this.reviewPricesAbort?.abort(); this.reviewPricesAbort = null;
    this.update({ reviewPricesLoading: false, reviewPricesError: "已停止等待走势。服务端可能仍在收尾；没有自动重试。" });
  }

  /** Show a saved conversation; the next question continues it in a fresh session. */
  async openConversation(id: string): Promise<boolean> {
    if (this.running || this.pending || this.state.phase !== "idle" || this.state.session?.view.state === "busy"
      || !this.api.conversation) return false;
    this.running = true;
    this.update({ loading: true });
    try {
      const stored = await this.api.conversation(id);
      if (this.credentials) {
        try { await this.api.end(this.credentials); }
        catch (error) {
          if (!(error instanceof ApiError) || ![401, 404].includes(error.status)) throw error;
        }
      }
      this.forget();
      const entries: Entry[] = stored.turns.slice(-40).map((turn) => ({ id: turn.request_id, question: turn.question,
        history: true, response: { request_id: turn.request_id, replayed: false, result: turn.result } }));
      this.update({ entries, conversationId: stored.conversation_id,
        notice: `已打开「${stored.title}」。以下是当时的结果；继续提问会接着这段对话，并重新查询行情。` });
      return true;
    } catch (error) { this.failure(error); return false; }
    finally { this.running = false; this.update({ loading: false }); }
  }

  async deleteConversation(id: string): Promise<boolean> {
    if (this.running || !this.api.deleteConversation) return false;
    this.running = true;
    this.update({ loading: true });
    try {
      await this.api.deleteConversation(id);
      this.invalidateReviewConversation(id);
      if (this.state.conversationId === id) {
        const keep = this.pending !== null;
        this.update({ conversationId: null, ...(keep ? {} : { entries: this.credentials ? this.state.entries : [] }) });
      }
      this.update({ notice: "已删除这段历史对话。" });
      await this.refreshConversations();
      return true;
    } catch (error) { this.failure(error); return false; }
    finally { this.running = false; this.update({ loading: false }); }
  }
}

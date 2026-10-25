/** In-memory browser state machine: credentials and unresolved work never enter persistent storage. */
import { ApiError, ConnectionError, type Health, type MessageResponse, type ResearchApi,
  type SessionCredentials, type SessionView } from "./api";

export interface Entry {
  id: string;
  question: string;
  history: boolean;
  response?: MessageResponse;
  error?: string;
}

export interface WorkspaceState {
  health: Health | null;
  session: { shortId: string; limit: number; modelLimit: number; view: SessionView } | null;
  phase: "idle" | "working" | "uncertain";
  loading: boolean;
  cancelPending: boolean;
  pendingId: string | null;
  consent: boolean;
  entries: Entry[];
  notice: string;
  noticeLevel: "info" | "error";
}

const emptyView = (): SessionView => ({ state: "idle", active_request_id: null, messages_used: 0, model_calls_forwarded: 0 });
const explanation = (error: unknown) => error instanceof ApiError || error instanceof ConnectionError
  ? error.message : "本次操作未得到可确认结果，没有自动重试。";
const terminalCodes = new Set(["session_message_limit", "session_not_found", "session_token_required", "request_id_conflict",
  "request_id_retired", "directory_unusable", "research_internal_error", "invalid_request", "service_closed",
  "json_required", "request_too_large", "invalid_content_length", "origin_not_allowed"]);

export class ResearchController {
  private credentials: SessionCredentials | null = null;
  private pending: { id: string; question: string } | null = null;
  private running = false;
  private listeners = new Set<() => void>();
  private state: WorkspaceState = { health: null, session: null, phase: "idle", loading: false,
    cancelPending: false, pendingId: null, consent: false, entries: [], notice: "正在检查本机服务…", noticeLevel: "info" };

  constructor(private readonly api: ResearchApi,
    private readonly nextId: () => string = () => crypto.randomUUID().replaceAll("-", "")) {}

  snapshot = () => this.state;
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
    this.update({ session: null, pendingId: null, phase: "idle", consent: false });
  }
  private failure(error: unknown) {
    if (error instanceof ApiError && [401, 404].includes(error.status)) this.forget();
    this.update({ notice: explanation(error), noticeLevel: "error" });
  }
  private view(view: SessionView) {
    if (this.state.session) this.update({ session: { ...this.state.session, view } });
  }
  private allowed() { return this.state.health !== null && (this.state.health.provider === "fake" || this.state.consent); }

  setConsent(value: boolean) { this.update({ consent: value }); }

  async load() {
    if (this.running || this.credentials) return;
    this.running = true;
    this.update({ loading: true });
    try {
      const health = await this.api.health();
      this.update({ health, consent: false, notice: "服务已连接。直接输入问题，首次发送时自动建立对话。" });
    } catch (error) { this.update({ health: null, notice: explanation(error), noticeLevel: "error" }); }
    finally { this.running = false; this.update({ loading: false }); }
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

  private async createSession() {
    const credentials = await this.api.create();
    if (credentials.provider !== this.state.health?.provider || credentials.data_mode !== this.state.health?.data_mode)
      throw new ConnectionError();
    this.credentials = credentials;
    this.update({ session: { shortId: credentials.session_id.slice(0, 8), limit: credentials.limits.messages,
      modelLimit: credentials.limits.model_calls, view: emptyView() }, entries: [],
      notice: "对话已建立。" });
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
      // Permission can be withdrawn while session creation is in flight.
      if (!this.allowed()) {
        this.update({ notice: "模型发送已暂停，问题尚未发送。再次发送时可重新确认。", noticeLevel: "error" });
        return false;
      }
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
      this.update({ entries: [], notice: "研究条件与结果已清空，额度不退。离线脚本不会重启；重跑演示请结束后新建会话。" });
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
      this.update({ entries: [], notice: "已回到新对话。直接发送问题即可开始。" });
      return true;
    } catch (error) { this.failure(error); return false; }
    finally { this.running = false; this.update({ loading: false }); }
  }
}

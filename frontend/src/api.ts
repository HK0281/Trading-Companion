/** Browser contracts for the public API, never the Agent's debug export. */
export interface Health {
  status: "ok";
  provider: "fake" | "deepseek";
  data_mode: "synthetic" | "tencent";
  trading_enabled: false;
}

export interface SessionCredentials {
  session_id: string;
  access_token: string;
  provider: Health["provider"];
  data_mode: Health["data_mode"];
  limits: { messages: number; model_calls: number };
}

export interface SessionView {
  state: string;
  active_request_id: string | null;
  messages_used: number;
  model_calls_forwarded: number;
}

export interface ReportField {
  indicator_id: string;
  field: string;
  label: string;
  trade_date: string;
  status: string;
  value: number | null;
  unit: string;
  missing_bars: number;
  fact_id: string | null;
}

export interface PublicTurn {
  status: string;
  code: string;
  message: string;
  stage: string;
  answer: string | null;
  parameters: { instrument_id: string; start_date: string; as_of_date: string } | null;
  candidates: { name: string; instrument_id: string }[];
  applied_defaults: string[];
  model_calls_attempted: number;
  analyses_used: number;
  report: { fields: ReportField[]; limitations: string[] } | null;
}

export interface MessageResponse {
  request_id: string;
  replayed: boolean;
  result: PublicTurn;
}

export interface ResearchApi {
  health(): Promise<Health>;
  create(): Promise<SessionCredentials>;
  inspect(session: SessionCredentials): Promise<SessionView>;
  send(session: SessionCredentials, id: string, question: string): Promise<MessageResponse>;
  history(session: SessionCredentials): Promise<MessageResponse>;
  cancel(session: SessionCredentials, id: string): Promise<void>;
  reset(session: SessionCredentials): Promise<SessionView>;
  end(session: SessionCredentials): Promise<void>;
}

const errors: Record<string, string> = {
  request_in_progress: "原请求仍在执行。请等待，或检查状态后用原编号重试。",
  session_busy: "会话仍有工作在执行；不要创建第二条研究。",
  service_busy: "服务的并行研究数已满；可稍后重试原请求。",
  session_capacity: "本机服务会话数已满，请稍后再创建。",
  session_message_limit: "本会话问题额度已用完，仍可查看历史结果。",
  session_not_found: "会话已失效或服务已重启。请重新开始会话。",
  session_token_required: "会话凭据不可用，请重新开始会话。",
  request_not_cancellable: "该请求已结束或暂不可取消，请检查状态。",
  request_id_conflict: "请求编号冲突；未再次研究，请检查状态。",
  request_id_retired: "请求编号已在重置时失效。",
  directory_unusable: "本地证券目录或演示夹具不可用，请在服务端检查。",
  model_configuration_error: "服务端模型配置不可用，请在服务端检查；不要在网页中输入密钥。",
  research_internal_error: "服务端处理失败，没有自动重试。",
  invalid_request: "请求格式不符合接口要求，没有自动重试。",
};

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super(errors[code] ?? `服务返回 HTTP ${status}，没有自动重试。`);
  }
}

export class ConnectionError extends Error {
  constructor() {
    super("未收到可确认的响应。断网或停止等待不代表服务端已停止；请检查状态或重试原请求。");
  }
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const identifier = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
const count = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value >= 0;
const text = (value: unknown): value is string => typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(text);
const mode = (value: unknown): value is Record<string, unknown> & Pick<Health, "provider" | "data_mode"> => record(value)
  && ["fake", "deepseek"].includes(String(value.provider)) && ["synthetic", "tencent"].includes(String(value.data_mode));
const sessionView = (value: unknown): value is SessionView => record(value) && text(value.state)
  && (value.active_request_id === null || identifier(value.active_request_id))
  && count(value.messages_used) && count(value.model_calls_forwarded);
const message = (value: unknown): value is MessageResponse => {
  if (!record(value) || !identifier(value.request_id) || typeof value.replayed !== "boolean" || !record(value.result)) return false;
  const result = value.result;
  return [result.status, result.code, result.message, result.stage].every(text)
    && (result.answer === null || text(result.answer)) && count(result.model_calls_attempted) && count(result.analyses_used)
    && strings(result.applied_defaults)
    && Array.isArray(result.candidates) && result.candidates.length <= 5
    && result.candidates.every((item) => record(item) && text(item.name) && text(item.instrument_id))
    && (result.parameters === null || record(result.parameters)
      && [result.parameters.instrument_id, result.parameters.start_date, result.parameters.as_of_date].every(text))
    && (result.report === null || record(result.report) && strings(result.report.limitations)
      && Array.isArray(result.report.fields) && result.report.fields.length <= 100
      && result.report.fields.every((item) => record(item)
        && [item.indicator_id, item.field, item.label, item.trade_date, item.status, item.unit].every(text)
        && (item.value === null || typeof item.value === "number" && Number.isFinite(item.value))
        && count(item.missing_bars) && (item.fact_id === null || text(item.fact_id))));
};

export function createApi(fetcher: typeof fetch = (...args) => fetch(...args)): ResearchApi {
  async function request<T>(path: string, check: (value: unknown) => value is T,
    options: { method?: string; session?: SessionCredentials; body?: unknown; long?: boolean } = {}): Promise<T> {
    const abort = new AbortController();
    // Client timeout is not server-side cancellation; never retry automatically.
    const timer = setTimeout(() => abort.abort(), options.long ? 120_000 : 10_000);
    try {
      const response = await fetcher(path, {
        method: options.method ?? "GET", cache: "no-store", credentials: "omit", redirect: "error",
        referrerPolicy: "no-referrer", signal: abort.signal,
        headers: { ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
          ...(options.session ? { Authorization: `Bearer ${options.session.access_token}` } : {}) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      if (!response.ok) {
        let code = "http_error";
        try {
          const problem: unknown = await response.json();
          if (record(problem) && record(problem.error) && text(problem.error.code)) code = problem.error.code;
        } catch { /* Untrusted response bodies are never displayed. */ }
        throw new ApiError(response.status, code);
      }
      const value: unknown = response.status === 204 ? null : await response.json();
      if (!check(value)) throw new ConnectionError();
      return value;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ConnectionError();
    } finally { clearTimeout(timer); }
  }
  const path = (session: SessionCredentials) => `/v1/sessions/${session.session_id}`;
  return {
    health: () => request("/health", (value): value is Health => mode(value) && record(value)
      && value.status === "ok" && value.trading_enabled === false),
    create: () => request("/v1/sessions", (value): value is SessionCredentials => mode(value)
      && record(value) && identifier(value.session_id) && text(value.access_token)
      && /^[A-Za-z0-9_-]{20,128}$/.test(value.access_token) && record(value.limits)
      && count(value.limits.messages) && count(value.limits.model_calls), { method: "POST", body: {} }),
    inspect: (session) => request(path(session), sessionView, { session }),
    send: (session, id, question) => request(path(session) + "/messages",
      (value): value is MessageResponse => message(value) && value.request_id === id,
      { method: "POST", session, body: { request_id: id, question }, long: true }),
    history: (session) => request(path(session) + "/result", message, { session }),
    cancel: async (session, id) => { await request(path(session) + "/cancel",
      (value): value is Record<string, unknown> => record(value) && value.request_id === id
        && value.status === "cancellation_requested", { method: "POST", session, body: { request_id: id } }); },
    reset: async (session) => {
      const value = await request(path(session) + "/reset", (item): item is { session: SessionView } =>
        record(item) && sessionView(item.session), { method: "POST", session, body: {} });
      return value.session;
    },
    end: async (session) => { await request(path(session), (value): value is null => value === null,
      { method: "DELETE", session }); },
  };
}

import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import { createApi, type PublicTurn } from "./api";
import { ResearchController, type Entry } from "./controller";

const INITIAL = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2";
const samples = [
  { label: "01 初始研究", question: INITIAL },
  { label: "02 追问 RSI", question: "这里的RSI为什么偏高" },
  { label: "指标知识", question: "解释一下MACD里的DIF和DEA" },
  { label: "03 修改指标", question: "把SMA3改成5日，其他不变" },
];
const units: Record<string, string> = { "CNY/share": "元/股", shares: "股", multiple: "倍", index_points: "点" };
const statuses: Record<string, string> = { completed: "已完成", limited: "部分可用", needs_clarification: "需要澄清",
  failed: "未完成", rejected: "范围受限", cancelled: "已取消" };
const missing: Record<string, string> = { insufficient_history: "历史不足", undefined: "未定义", calculation_error: "计算失败" };

function Facts({ turn }: { turn: PublicTurn }) {
  const fields = turn.report?.fields ?? [];
  if (!fields.length) return null;
  return <details className="facts">
    <summary>指标字段与证据 <span>{fields.length} 个字段</span></summary>
    <div className="table-scroll"><table>
      <thead><tr><th>字段 / 交易日</th><th>数值</th><th>可用性</th><th>证据编号</th></tr></thead>
      <tbody>{fields.map((field) => <tr key={`${field.indicator_id}-${field.field}`}>
        <td>{field.label}<small>{field.trade_date}</small></td>
        <td className="number">{field.value === null ? "—" : new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 8 }).format(field.value)}
          {field.value !== null && <small>{units[field.unit] ?? field.unit}</small>}</td>
        <td>{field.status === "available" ? "可用" : missing[field.status] ?? "不可用"}
          {field.missing_bars > 0 && <small>还缺 {field.missing_bars} 条</small>}</td>
        <td><code>{field.fact_id ?? "无可用证据"}</code></td>
      </tr>)}</tbody>
    </table></div>
  </details>;
}

function Answer({ entry, choose, canChoose }: { entry: Entry; choose: (text: string) => void; canChoose: boolean }) {
  const response = entry.response;
  const turn = response?.result;
  return <article className={`exchange ${entry.history ? "historical" : ""}`}>
    <div className="question"><span className="eyebrow">{entry.history ? "历史记录" : "你的问题"}</span><p>{entry.question}</p></div>
    <div className="answer">
      <div className="answer-heading"><span className="assistant-mark" aria-hidden="true">TC</span>
        <h3>{entry.history ? "已保存的研究 · 非新分析" : "研究助手"}</h3>
        {turn && <span className={`status-tag ${turn.status === "completed" ? "positive" : ""}`}>
          {statuses[turn.status] ?? "处理结果"}</span>}
        {response?.replayed && <span className="status-tag">原请求重放</span>}
      </div>
      {turn && <>
        {turn.parameters && <div className="scope"><strong>{turn.parameters.instrument_id}</strong>
          <span>{turn.parameters.start_date} — {turn.parameters.as_of_date}</span><span>已确认范围</span></div>}
        <div className="answer-text">{turn.answer ?? turn.message}</div>
        {turn.status === "needs_clarification" && !entry.history && !!turn.candidates.length && <div className="candidates" aria-label="候选股票">
          <p>请选择要研究的股票。点击仅填入输入框，确认后再发送。</p>
          {turn.candidates.map((candidate) => <button className="candidate" type="button" disabled={!canChoose}
            key={candidate.instrument_id} onClick={() => choose(candidate.instrument_id)}>
            {candidate.name} <span>{candidate.instrument_id}</span>
          </button>)}
        </div>}
        {!!turn.applied_defaults.length && <div className="defaults"><strong>本轮默认口径</strong>
          <ul>{turn.applied_defaults.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
        <Facts turn={turn} />
        <div className="answer-meta"><code>{turn.code}</code><span>{response?.replayed ? "原请求" : "本轮"}模型 {turn.model_calls_attempted} 次 · 分析 {turn.analyses_used} 次
          {response?.replayed && " · 本次未重做"}</span></div>
      </>}
      {entry.error && <p className="error-text" role="alert">{entry.error}</p>}
      {!turn && !entry.error && <p className="waiting">正在处理问题，请稍候…</p>}
    </div>
  </article>;
}

export default function App({ controller: supplied }: { controller?: ResearchController }) {
  const [controller] = useState(() => supplied ?? new ResearchController(createApi()));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [question, setQuestion] = useState("");
  const [confirmation, setConfirmation] = useState<"reset" | "end" | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const latest = useRef<HTMLDivElement>(null);
  const { health, session, loading, phase } = state;
  const locked = loading || phase !== "idle";
  const paid = health?.provider === "deepseek";
  const consented = !paid || state.consent;
  const exhausted = !!session && session.view.messages_used >= session.limit;
  const canSend = !!session && !locked && consented && !exhausted && session.view.state !== "busy";
  const characters = Array.from(question.trim()).length;

  useEffect(() => { void controller.load(); }, [controller]);
  useEffect(() => { if (session) textarea.current?.focus(); }, [session?.shortId]);
  useEffect(() => {
    if (state.entries.length) latest.current?.scrollIntoView?.({ block: "nearest", behavior: "auto" });
  }, [state.entries.length]);

  const choose = (value: string) => { setQuestion(value); textarea.current?.focus(); };
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend || !characters || characters > 2000) return;
    const original = question;
    setQuestion("");
    void controller.send(original);
  };
  const confirmAction = () => {
    const action = confirmation;
    setConfirmation(null);
    if (action === "reset") void controller.reset();
    else if (action === "end") { setQuestion(""); void controller.end(); }
  };

  return <div className="workspace" id="workspace">
    <aside className="sidebar">
      <a href="#workspace" className="brand" aria-label="Trading Companion 页首"><span className="brand-icon" aria-hidden="true">↗</span>
        <span>Trading<br /><strong>Companion</strong></span></a>
      <div className="sidebar-section"><span className="eyebrow">WORKSPACE / 01</span><h1>A 股研究工作台</h1>
        <p className="muted">把问题交给助手，<br />把交易决定留给自己。</p></div>
      <section className="mode-panel" aria-label="服务模式">
        <div className="mode-top"><span className={`connection-dot ${health ? "connected" : ""}`} />
          <strong>{health ? "本机服务已连接" : loading ? "正在连接" : "服务未连接"}</strong></div>
        <dl><div><dt>模型</dt><dd>{health?.provider === "fake" ? "Fake · 固定脚本" : paid ? "DeepSeek · 可能计费" : "—"}</dd></div>
          <div><dt>数据</dt><dd>{health?.data_mode === "synthetic" ? "人工行情 · 非真实" : health?.data_mode === "tencent" ? "腾讯历史日线" : "—"}</dd></div>
          <div><dt>交易权限</dt><dd>无 · 仅研究</dd></div></dl>
        {!health && <button type="button" disabled={loading} onClick={() => void controller.load()}>重新连接</button>}
      </section>
      {paid && <label className="consent"><input type="checkbox" checked={state.consent}
        onChange={(event) => controller.setConsent(event.target.checked)} />
        <span>我同意将问题、参数和证据发送至 DeepSeek，并了解可能产生费用。密钥只在服务端配置。</span></label>}
      {!session ? <button type="button" className="primary start" disabled={!health || loading || !consented}
        onClick={() => void controller.start()}>开始会话 <span aria-hidden="true">＋</span></button> : <>
        <section className="session-panel" aria-label="会话额度"><div className="session-title"><strong>当前会话</strong><code>{session.shortId}</code></div>
          <div className="quota"><span>已接纳问题</span><strong>{session.view.messages_used} / {session.limit}</strong></div>
          <div className="quota"><span>模型方法转发</span><strong>{session.view.model_calls_forwarded} / {session.modelLimit}</strong></div>
          <p className="muted">额度以最近一次状态查询为准。创建后 30 分钟失效，重置不退额度。</p>
        </section>
        <div className="session-actions">
          <button type="button" disabled={locked} onClick={() => void controller.history()}>查看上次结果 <span>0 模型调用</span></button>
          <button type="button" disabled={loading} onClick={() => void controller.inspect()}>检查会话状态</button>
          <button type="button" disabled={locked} onClick={() => setConfirmation("reset")}>清空研究条件</button>
          <button type="button" disabled={locked} onClick={() => setConfirmation("end")}>结束会话</button>
        </div>
      </>}
      {confirmation && <div className="confirm-panel" role="group" aria-label="确认会话操作">
        <p>{confirmation === "reset" ? "清空研究条件和结果？额度不会返还，也不会重启离线脚本。" : "结束并删除服务端会话？当前页面记录也会清空。"}</p>
        <button type="button" disabled={locked} onClick={confirmAction}>确认{confirmation === "reset" ? "清空" : "结束"}</button>
        <button type="button" onClick={() => setConfirmation(null)}>返回</button>
      </div>}
      <div className="sidebar-footer"><a href="/docs" target="_blank" rel="noreferrer">接口文档 ↗</a>
        <p>会话凭据与页面记录仅在内存中。刷新会丢失访问凭据，旧会话等待失效。</p></div>
    </aside>

    <main className="main">
      <header className="topbar"><div><span className="eyebrow">RESEARCH, NOT EXECUTION</span><h2>先看证据，再做判断。</h2></div>
        <span className="readonly-badge"><span aria-hidden="true">◈</span> 只读研究</span></header>
      {health?.provider === "fake" && <section className="demo-banner" aria-label="演示说明"><strong>离线演示</strong>
        <p>人工数据固定在 2026-09-21 至 09-25，不代表真实行情。按下方 01 → 02 → 03 顺序尝试；指标知识可穿插。任意自由提问可能超出脚本。</p></section>}
      <div className="live-notice" role="status" aria-live="polite">{state.notice}</div>

      <section className="conversation" aria-label="研究对话" aria-busy={phase === "working"}>
        {!state.entries.length ? <div className="empty-state"><div className="empty-mark" aria-hidden="true">↗</div>
          <span className="eyebrow">YOUR RESEARCH STARTS HERE</span><h2>从一个具体问题开始</h2>
          <p>写下股票名称、日期和想看的指标。信息不明确时，助手会先澄清；指标计算和事实检查由程序完成。</p>
          <div className="capabilities"><span>单股票 · 多指标</span><span>证据可查</span><span>不下单</span></div>
          <div className="example-box"><span className="eyebrow">示例问题</span><p>{INITIAL}</p>
            <button type="button" disabled={locked} onClick={() => choose(INITIAL)}>填入这个问题 ↗</button></div>
        </div> : state.entries.map((entry, index) => <Answer key={entry.id} entry={entry} choose={choose}
          canChoose={canSend && index === state.entries.length - 1} />)}
        <div ref={latest} />
      </section>

      {state.pendingId && <div className="request-controls" aria-label="当前请求控制">
        <span>{phase === "uncertain" ? "响应未确认" : "处理中"} <code>{state.pendingId.slice(0, 8)}</code></span>
        {phase === "uncertain" && <button type="button" disabled={loading || !consented} onClick={() => void controller.retry()}>重试原请求</button>}
        {phase === "uncertain" && <button type="button" disabled={loading} onClick={() => void controller.inspect()}>检查原请求状态</button>}
        <button type="button" disabled={state.cancelPending} onClick={() => void controller.cancel()}>
          {state.cancelPending ? "发送取消信号中…" : "请求取消"}</button>
      </div>}
      <section className="composer-area" aria-label="输入研究问题">
        <div className="samples">{samples.map((sample) => <button type="button" key={sample.label}
          disabled={locked} onClick={() => choose(sample.question)}>{sample.label}</button>)}</div>
        <form className="composer" onSubmit={submit}>
          <label className="sr-only" htmlFor="question">研究问题</label>
          <textarea ref={textarea} id="question" rows={3} maxLength={4000} value={question} disabled={!session || locked}
            placeholder={session ? "例如：分析平安银行，指定日期区间和指标…" : "请先在左侧开始会话"}
            onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => {
              if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing) submit(event);
            }} />
          <div className="composer-bottom"><span className={characters > 2000 ? "error-text" : "muted"}>
            {characters} / 2000 · Ctrl + Enter 发送</span><button className="primary" type="submit"
              disabled={!canSend || !characters || characters > 2000}>{phase === "working" ? "正在研究…" : "发送问题"} <span aria-hidden="true">↑</span></button></div>
        </form>
        {exhausted && <p className="error-text">本会话问题额度已用完；仍可查看历史，结束后可新建会话。</p>}
        <p className="disclaimer">仅用于研究与知识学习，不承诺走势或收益。无下单权限，交易由你自行完成。当前无 K 线图、新闻或选股。</p>
      </section>
    </main>
  </div>;
}

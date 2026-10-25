import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent, type Ref } from "react";
import { createApi } from "./api";
import { ResearchController, type Entry } from "./controller";
import { ResearchChart } from "./ResearchChart";
import { MarketContext } from "./MarketContext";
import { ResearchWorkflow } from "./ResearchWorkflow";
import { EvidenceTable, ReadableText, ResponseContent } from "./ResponseContent";
import { parseConversation, parsePresentation, sourceLabel } from "./presentation";

const INITIAL = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2";
const samples = [
  { label: "个股研究", question: INITIAL, description: "从价格、均线和动量开始", icon: "chart" },
  { label: "理解指标", question: "解释一下MACD里的DIF和DEA", description: "弄懂指标背后的计算逻辑", icon: "book" },
  { label: "调整条件", question: "把SMA3改成5日，其他不变", description: "沿用当前研究，修改指标参数", icon: "sliders" },
];
const statuses: Record<string, string> = { completed: "已完成", limited: "部分可用", needs_clarification: "需要补充",
  failed: "未完成", rejected: "范围受限", cancelled: "已取消" };

function Icon({ name, className = "" }: { name: string; className?: string }) {
  const paths: Record<string, string> = {
    chart: "M4 19h16M6 15l4-5 4 2 5-7M15 5h4v4", plus: "M12 5v14M5 12h14",
    chat: "M5 4h14a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9l-5 4V5a1 1 0 0 1 1-1Z",
    book: "M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Zm0 0v14",
    sliders: "M5 4v7m0 4v5M12 4v2m0 4v10M19 4v10m0 4v2M2 11h6m1-5h6m1 8h6",
    arrow: "M12 19V5m-6 6 6-6 6 6", menu: "M4 6h16M4 12h16M4 18h16",
    clock: "M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  };
  return <svg className={`icon ${className}`} width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.chart} /></svg>;
}

function Answer({ entry, choose, canChoose, elementRef }: { entry: Entry; choose: (text: string) => void; canChoose: boolean; elementRef?: Ref<HTMLElement> }) {
  const response = entry.response, turn = response?.result;
  const structured = turn && parsePresentation(turn);
  const conversation = turn && parseConversation(turn);
  return <article ref={elementRef} className={`exchange ${entry.history ? "historical" : ""}`} id={`message-${entry.id}`}>
    <div className="question"><span className="sr-only">{entry.history ? "历史记录" : "你的问题"}</span><p>{entry.question}</p></div>
    <div className="answer">
      <div className="answer-heading"><span className="assistant-mark" aria-hidden="true"><Icon name="chart" /></span>
        <h3>{entry.history ? "已保存的研究 · 非新分析" : "Trading Companion"}</h3>
        {turn && <span className={`status-tag ${turn.status === "completed" ? "positive" : ""}`}>{statuses[turn.status] ?? "处理结果"}</span>}
        {response?.replayed && <span className="status-tag">原请求重放</span>}
      </div>
      <div className="answer-body">{turn && <>
        {turn.parameters && <div className="scope"><strong>{turn.parameters.instrument_id}</strong>
          <span>{turn.parameters.start_date} — {turn.parameters.as_of_date}</span>
          {turn.applied_defaults.length > 0 && <span>已采用 {turn.applied_defaults.length} 项默认设置 · 见本轮详情</span>}
          {turn.parameters.source !== undefined && <span className={turn.parameters.source === "csv" ? "synthetic-label" : ""}>来源：{sourceLabel(turn.parameters.source)}</span>}</div>}
        {turn.status !== "completed" && structured && turn.conversation == null && <p className="data-note" role="note">{turn.message}</p>}
        <ResponseContent turn={turn} />
        {turn.status === "needs_clarification" && !entry.history && !!turn.candidates.length && <div className="candidates" aria-label="候选股票">
          <p>选择股票填入输入框，再发送确认。</p>
          {turn.candidates.map((candidate) => <button className="candidate" type="button" disabled={!canChoose}
            key={candidate.instrument_id} onClick={() => choose(candidate.instrument_id)}>
            {candidate.name} <span>{candidate.instrument_id}</span></button>)}
        </div>}
        <div className="answer-attachments">
          <ResearchChart value={turn.chart} parameters={turn.parameters} reportRequestId={turn.report?.request_id} historical={entry.history} />
          <EvidenceTable turn={turn} />
          {turn.market_context != null && <details className="disclosure"><summary>证券状态与交易日历 <span>查看背景</span></summary>
            <MarketContext value={turn.market_context} parameters={turn.parameters} reportRequestId={turn.report?.request_id} historical={entry.history} /></details>}
          {(structured || turn.conversation != null) && <details className="disclosure full-report"><summary>完整报告 <span>详细口径与限制</span></summary>
            <ReadableText text={turn.answer ?? turn.message} /></details>}
          <details className="disclosure diagnostics"><summary>本轮详情 <span>参数与执行记录</span></summary>
            <ResearchWorkflow turn={turn} />
            {conversation?.origin === "model" && <div className="response-citations">
              <p>模型引用的指标关系已核对；自然语言含义与未来方向判断并未由程序验证。</p>
              {conversation.citations.map((item) => <div key={item.role}><strong>{({ answer: "主要判断", outlook: "后续方向", watch_for: "观察条件" })[item.role]}</strong>
                <ul>{item.fact_ids.map((id) => <li key={id}><code>{id}</code></li>)}</ul></div>)}
            </div>}
            {!!turn.applied_defaults.length && <div className="defaults"><strong>本轮默认口径</strong>
              <ul>{turn.applied_defaults.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
            <div className="answer-meta"><code>{turn.code}</code><span>{response?.replayed ? "原请求" : "本轮"}模型 {turn.model_calls_attempted} 次 · 分析 {turn.analyses_used} 次
              {response?.replayed && " · 本次未重做"}</span></div>
          </details>
        </div>
      </>}
      {entry.error && <p className="error-text" role="alert">{entry.error}</p>}
      {!turn && !entry.error && <div className="waiting" role="status"><span className="thinking-dots" aria-hidden="true"><i /><i /><i /></span>正在整理数据与指标…</div>}
      </div>
    </div>
  </article>;
}

export default function App({ controller: supplied }: { controller?: ResearchController }) {
  const [controller] = useState(() => supplied ?? new ResearchController(createApi()));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [question, setQuestion] = useState("");
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [confirmation, setConfirmation] = useState<"reset" | "end" | null>(null);
  const [sendConfirmation, setSendConfirmation] = useState<"send" | "retry" | null>(null);
  const draftRevision = useRef(0);
  const textarea = useRef<HTMLTextAreaElement>(null), latest = useRef<HTMLElement>(null);
  const scrollArea = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const { health, session, loading, phase } = state;
  const locked = loading || phase !== "idle", paid = health?.provider === "deepseek";
  const consented = !paid || state.consent;
  const exhausted = !!session && session.view.messages_used >= session.limit;
  const canSend = !!health && !locked && !exhausted && session?.view.state !== "busy";
  const characters = Array.from(question.trim()).length;

  useEffect(() => { void controller.load(); }, [controller]);
  useEffect(() => { if (!locked) textarea.current?.focus(); }, [locked, session?.shortId]);
  useEffect(() => {
    const container = scrollArea.current, message = latest.current;
    if (followBottom.current && container && message) {
      // Scroll the message pane only; scrollIntoView would also move the document on mobile.
      const top = container.scrollTop + message.getBoundingClientRect().top - container.getBoundingClientRect().top - 24;
      container.scrollTo?.({ top: Math.max(0, top), behavior: "auto" });
    }
  }, [state.entries]);
  useEffect(() => {
    if (textarea.current) {
      textarea.current.style.height = "auto";
      textarea.current.style.height = `${Math.min(textarea.current.scrollHeight || 54, 160)}px`;
    }
  }, [question]);

  const editQuestion = (value: string) => { draftRevision.current++; setSendConfirmation(null); setQuestion(value); };
  const choose = (value: string) => { editQuestion(value); setNavigationOpen(false); textarea.current?.focus(); };
  const sendQuestion = () => {
    const original = question;
    const revision = ++draftRevision.current;
    followBottom.current = true;
    setQuestion("");
    setSendConfirmation(null);
    void controller.send(original).then((accepted) => {
      // A failed creation must not lose the draft or overwrite a newer edit.
      if (!accepted && draftRevision.current === revision) setQuestion(original);
    });
  };
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend || !characters || characters > 2000) return;
    if (!consented) { setSendConfirmation("send"); return; }
    sendQuestion();
  };
  const confirmSending = () => {
    if (!sendConfirmation || loading || sendConfirmation === "send" && locked) return;
    if (sendConfirmation === "send" && (!canSend || !characters || characters > 2000)) return;
    controller.setConsent(true);
    if (sendConfirmation === "send") sendQuestion();
    else { setSendConfirmation(null); void controller.retry(); }
  };
  const newChat = async () => {
    setNavigationOpen(false);
    setConfirmation(null);
    setSendConfirmation(null);
    if (await controller.end()) { editQuestion(""); followBottom.current = true; textarea.current?.focus(); }
  };
  const confirmAction = () => {
    const action = confirmation;
    setConfirmation(null);
    if (action === "reset") void controller.reset();
    else if (action === "end") void newChat();
  };

  return <div className={`workspace ${navigationOpen ? "navigation-open" : ""}`} id="workspace"
    onKeyDown={(event) => { if (event.key === "Escape") setNavigationOpen(false); }}>
    {navigationOpen && <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setNavigationOpen(false)} />}
    <aside className="sidebar" id="navigation" aria-label="工作台导航">
      <a href="#workspace" className="brand" aria-label="Trading Companion 页首"><span className="brand-icon"><Icon name="chart" /></span>
        <span>Trading Companion<small>你的 A 股研究助手</small></span></a>
      <button type="button" className="new-research" disabled={locked} onClick={() => void newChat()}><Icon name="plus" />新对话</button>
      <div className="nav-item active"><Icon name="chat" /><span>研究对话</span><span className="nav-badge">A 股</span></div>
      <section className="conversation-index" aria-label="本次对话记录"><span className="sidebar-caption">本次对话</span>
        {!state.entries.length ? <p className="empty-index">你的研究会显示在这里</p> : <nav>{state.entries.map((entry, index) =>
          <a key={entry.id} href={`#message-${entry.id}`} onClick={() => { followBottom.current = false; setNavigationOpen(false); }}>
            <span>{String(index + 1).padStart(2, "0")}</span>{entry.question}</a>)}</nav>}
      </section>
      <div className="sidebar-bottom">
        {session && <>
          <button className="history-button" type="button" disabled={locked} onClick={() => { setNavigationOpen(false); followBottom.current = true; void controller.history(); }}>
            <Icon name="clock" />查看上次结果</button>
          <section className="session-panel" aria-label="会话额度"><div className="quota"><span>问题</span><strong>{session.view.messages_used} / {session.limit}</strong></div>
            <div className="quota"><span>模型调用</span><strong>{session.view.model_calls_forwarded} / {session.modelLimit}</strong></div></section>
        </>}
        <details className="settings"><summary><span className={`connection-dot ${health ? "connected" : ""}`} />
          {health ? "本机服务已连接" : loading ? "正在连接" : "服务未连接"}<span className="settings-chevron">⌄</span></summary>
          <div className="settings-content"><section aria-label="服务模式">
            <dl><div><dt>模型</dt><dd>{health?.provider === "fake" ? "Fake · 固定脚本" : paid ? "DeepSeek" : "—"}</dd></div>
              <div><dt>数据</dt><dd>{health ? sourceLabel(health.data_mode) : "—"}</dd></div></dl>
            <p>来源由服务启动固定；失败不自动切换。不能通过对话切换。</p>
          </section>
          {paid && <label className="consent"><input type="checkbox" checked={state.consent} onChange={(event) => controller.setConsent(event.target.checked)} />
            <span>允许向 DeepSeek 发送问题与研究数据（可能计费）</span></label>}
          {session && <div className="session-actions"><p>会话 {session.shortId} · 创建后 30 分钟到期。刷新会丢失当前记录；重置不退额度。</p>
            <button type="button" disabled={loading} onClick={() => void controller.inspect()}>检查会话状态</button>
            <button type="button" disabled={locked} onClick={() => setConfirmation("reset")}>清空研究条件</button>
            <button type="button" disabled={locked} onClick={() => setConfirmation("end")}>结束会话</button></div>}
          <details className="technical-status"><summary>连接与诊断</summary><p>{state.notice}</p><a href="/docs" target="_blank" rel="noreferrer">接口文档 ↗</a></details>
        </div></details>
        {confirmation && <div className="confirm-panel" role="group" aria-label="确认会话操作">
          <p>{confirmation === "reset" ? "清空当前对话与研究条件？已使用额度不会返还。" : "结束会话并清空当前页面记录？"}</p>
          <button type="button" disabled={locked} onClick={confirmAction}>确认{confirmation === "reset" ? "清空" : "结束"}</button>
          <button type="button" onClick={() => setConfirmation(null)}>返回</button>
        </div>}
      </div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="topbar-title"><button className="mobile-menu" aria-label="展开导航" aria-expanded={navigationOpen}
        aria-controls="navigation" onClick={() => setNavigationOpen(!navigationOpen)}><Icon name="menu" /></button>
        <h1>研究对话</h1><span className="header-divider" /><span className="market-badge">A 股</span></div>
        <div className="header-status"><span className={health?.data_mode === "synthetic" ? "source-pill synthetic-label" : "source-pill"}>
          {health?.data_mode === "synthetic" ? "人工行情 · 非真实" : health ? sourceLabel(health.data_mode) : "连接中"}</span>
          <span className="model-pill">{paid ? "DeepSeek" : health ? "演示模式" : "—"}</span></div></header>

      <div className="conversation-scroll" ref={scrollArea} onScroll={() => {
        const element = scrollArea.current;
        if (element) followBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
      }}>
        <section className={`conversation ${!state.entries.length ? "is-empty" : ""}`} aria-label="研究对话" aria-busy={phase === "working"}>
          {!state.entries.length ? <div className="empty-state"><div className="empty-mark" aria-hidden="true"><Icon name="chart" /></div>
            <span className="eyebrow">TRADING COMPANION</span><h2>把行情，聊清楚。</h2>
            <p>研究一只股票，读懂一组指标。<br />从你的问题开始。</p>
            <div className="starter-grid">{samples.map((sample) => <button type="button" key={sample.label} className="starter-card"
              disabled={locked} onClick={() => choose(sample.question)}><Icon name={sample.icon} /><strong>{sample.label}</strong>
              <span>{sample.description}</span><span className="starter-arrow" aria-hidden="true">↗</span></button>)}</div>
            {health?.provider === "fake" && <details className="demo-guide" aria-label="演示说明"><summary>{health.data_mode === "synthetic" ? "离线演示" : "固定模型脚本"} · 查看使用方式</summary>
              <p>{health.data_mode === "synthetic" ? "示例使用 2026-09-21 至 09-25 的人工行情。建议先发送个股研究，再追问 RSI，最后调整条件。"
                : "模型采用固定脚本；行情仍来自顶部标注的数据源。自由提问可能超出脚本范围。"}</p></details>}
          </div> : state.entries.map((entry, index) => <Answer key={entry.id} entry={entry} choose={choose}
            elementRef={index === state.entries.length - 1 ? latest : undefined} canChoose={canSend && index === state.entries.length - 1} />)}
        </section>
      </div>

      <div className="composer-dock"><div className="composer-container">
        {state.pendingId && <div className="request-controls" aria-label="当前请求控制">
          <span>{phase === "uncertain" ? "响应未确认" : "正在研究"}</span>
          {phase === "uncertain" && <button type="button" disabled={loading} onClick={() => {
            if (!consented) setSendConfirmation("retry"); else void controller.retry();
          }}>重试原请求</button>}
          {phase === "uncertain" && <button type="button" disabled={loading} onClick={() => void controller.inspect()}>检查原请求状态</button>}
          <button type="button" disabled={state.cancelPending} onClick={() => void controller.cancel()}>{state.cancelPending ? "正在请求取消…" : "请求取消"}</button>
        </div>}
        <div className="sr-only" role="status" aria-live="polite">{state.notice}</div>
        {state.noticeLevel === "error" || phase === "uncertain"
          ? <p className="connection-notice" role="alert">{state.notice}</p> : null}
        {!health && <div className="composer-connection"><span>{loading ? "正在连接本机服务，你可以先输入问题。" : "本机服务未连接，你的草稿仍保留在输入框。"}</span>
          {!loading && <button type="button" onClick={() => void controller.load()}>重新连接</button>}</div>}
        {loading && !session && health && <p className="composer-progress" role="status">正在建立对话并发送问题…</p>}
        {sendConfirmation && <section className="send-consent" role="group" aria-label="确认发送到 DeepSeek">
          <div><strong>使用 DeepSeek 处理本次对话？</strong><p>问题与研究数据将发送到 DeepSeek，可能产生 API 费用。
            {health?.data_mode !== "synthetic" && <>股票与日期将用于查询{sourceLabel(health?.data_mode)}。</>}</p></div>
          <div className="send-consent-actions"><button type="button" className="primary" disabled={loading || sendConfirmation === "send" && locked} onClick={confirmSending}>
            {sendConfirmation === "retry" ? "允许并重试" : "允许并发送"}</button>
            <button type="button" onClick={() => { setSendConfirmation(null); textarea.current?.focus(); }}>暂不发送</button></div>
        </section>}
        {session && !!state.entries.length && !locked && <div className="followup-chips">
          <button type="button" onClick={() => choose("这里的RSI为什么偏高")}>追问 RSI</button>
          <button type="button" onClick={() => choose("解释一下MACD里的DIF和DEA")}>DIF 与 DEA 是什么？</button>
          <button type="button" onClick={() => choose("把SMA3改成5日，其他不变")}>调整指标</button>
        </div>}
        <form className="composer" onSubmit={submit} aria-label="输入研究问题">
          <label className="sr-only" htmlFor="question">研究问题</label>
          <textarea ref={textarea} id="question" rows={2} maxLength={4000} value={question} disabled={(loading && !!health) || phase !== "idle"}
            placeholder="输入股票名称、日期，或直接问一个问题…"
            onChange={(event) => editQuestion(event.target.value)} onKeyDown={(event) => {
              if (event.key === "Escape") setNavigationOpen(false);
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) submit(event);
            }} />
          <div className="composer-bottom"><div><span className="composer-context"><Icon name="chart" />A 股研究</span>
            <span className={characters > 2000 ? "error-text" : "character-count"}>{characters} / 2000</span></div>
            <button className="primary send-button" type="submit" aria-label={phase === "working" ? "正在研究" : "发送问题"}
              disabled={!canSend || !characters || characters > 2000}><Icon name="arrow" /></button></div>
        </form>
        <div className="composer-hint"><span>Enter 发送 · Shift + Enter 换行</span><span>会话仅保留在当前页面</span></div>
        {exhausted && <p className="error-text">本次对话的问题额度已用完；仍可查看历史，点击「新对话」后可继续。</p>}
      </div></div>
    </main>
  </div>;
}

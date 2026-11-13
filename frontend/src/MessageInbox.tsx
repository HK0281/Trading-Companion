import { useId, useState, type FormEvent } from "react";
import type { ResearchController, WorkspaceState } from "./controller";
import { inboxSourceLabel, inboxStatusLabel, inboxTime, validInboxInstrument } from "./inboxMessages";

export function MessageInbox({ state, controller }: { state: WorkspaceState; controller: ResearchController }) {
  const [instrument, setInstrument] = useState(state.inboxFilters.instrument_id ?? "");
  const [unreadOnly, setUnreadOnly] = useState(state.inboxFilters.unread_only);
  const [clearingRevision, setClearingRevision] = useState<string | null>(null);
  const id = useId(), data = state.inboxData, result = state.inboxCheckResult;
  const code = instrument.trim().toUpperCase();
  const valid = !code || validInboxInstrument(code);
  const current = data?.enabled === true;
  const writeLocked = state.inboxWriting || state.inboxLoading || !!state.inboxError || !current;
  const checkLocked = writeLocked || state.inboxCheckLoading || state.inboxCheckUncertain;
  const submit = (event: FormEvent) => {
    event.preventDefault(); if (!valid) return;
    setClearingRevision(null);
    void controller.refreshInbox({ unread_only: unreadOnly, ...(code ? { instrument_id: code } : {}) });
  };
  const states = data?.source_states ?? [];
  const failures = states.filter(value => value.status === "partial" || value.status === "unavailable");
  const losses = states.filter(value => value.evicted_unread_count > 0);
  return <section className="message-inbox" aria-label="消息收件箱">
    <div className="review-heading"><div><span className="eyebrow">仅保存在本机</span><h2>消息收件箱</h2></div>
      <button type="button" onClick={() => controller.closeMessageInbox()}>关闭收件箱</button></div>
    <p className="inbox-limit">有限检查，非全量监控</p>
    <p className="review-intro">打开页面只读取本机已有消息。主动检查每批最多5股，新闻与公告分别请求一次；每源只看最近30天中的最多5条，可能遗漏密集发布或离线期间的消息。</p>
    <p className="review-meta">只收集原始标题，不作重要性或利好利空判断，不调用模型、不取正文，不在后台定时检查或推送。不占研究会话额度。</p>
    <div className="inbox-check-actions" aria-label="主动检查消息">
      <button type="button" disabled={checkLocked} onClick={() => void controller.startInboxCheck()}>检查自选消息</button>
      {state.inboxNextCheckCursor && <button type="button" disabled={checkLocked} onClick={() => void controller.startInboxCheck(true)}>检查下一批</button>}
      {state.inboxCheckUncertain && <button type="button" disabled={state.inboxCheckLoading || state.inboxWriting || state.inboxLoading || !current}
        onClick={() => void controller.retryInboxCheck()}>重查原批结果</button>}
      {state.inboxCheckLoading && <button type="button" onClick={() => controller.cancelInboxCheck()}>停止等待消息检查</button>}
    </div>
    {state.inboxCheckLoading && <p role="status">正在检查这一批；不会自动开始下一批或重试失败来源…</p>}
    {state.inboxCheckUncertain && <p className="review-caution">上批结果尚未确认，原编号已保留。“重查原批结果”使用同一编号；刷新本机消息只读取已保存的部分。</p>}
    {state.inboxNotice && <p className="inbox-notice" role="status">{state.inboxNotice}</p>}
    {state.inboxError && <p className="error-text" role="alert">{state.inboxError}{data && " 以下为上次确认的本机结果。"}</p>}
    {data?.sidecar_status === "inbox_sidecar_unavailable" && <p className="review-caution" role="status">最近一次研究标题未确认保存；原研究结果不受影响，可刷新核对。</p>}
    {result && <section className="inbox-check-receipt" aria-label="本批消息检查回执">
      <h3>{result.replayed ? "原批检查回执" : "本批检查回执"}</h3>
      <p className="review-meta">本批实际来源请求 {result.attempted_requests} 次；已检查股票 {result.checked_stocks}，本轮自选 {result.total_stocks} 只；尚有 {result.remaining_sources} 个来源待检查。</p>
      {result.replayed && <p className="review-caution">这是原批结果，不是当前收件箱；当前消息和已读状态以本机刷新为准。</p>}
      <ul className="inbox-check-results">{result.results.map(value => <li key={`${value.instrument_id}:${value.source}`}>
        <strong>{value.instrument_id} · {inboxSourceLabel(value.source)}</strong><span className={value.status === "partial" || value.status === "unavailable" ? "error-text" : ""}>{inboxStatusLabel[value.status]}</span>
        <small>保存 {value.saved} 条；已见 {value.duplicates} 条{value.baseline ? "；初次建立基线" : ""}。</small>
        {value.filtered_count > 0 && <small>另有 {value.filtered_count} 条因公司关联、日期窗口或新浪链接格式限制未保存。</small>}
      </li>)}</ul>
      {result.remaining_sources > 0 && <p className="review-meta">未检查部分尚无结果；继续下一批仍需主动点击。</p>}
    </section>}

    <form className="inbox-filters" aria-label="筛选本机消息" onSubmit={submit}>
      <label htmlFor={`${id}-read`}>消息阅读状态<select id={`${id}-read`} value={unreadOnly ? "unread" : "all"}
        onChange={event => setUnreadOnly(event.target.value === "unread")}><option value="unread">未读消息</option><option value="all">全部本机消息</option></select></label>
      <label htmlFor={`${id}-code`}>消息证券代码筛选<input id={`${id}-code`} value={instrument} maxLength={9} placeholder="全部自选，或000001.SZ" autoComplete="off"
        onChange={event => setInstrument(event.target.value)} /></label>
      <button type="submit" disabled={!valid || state.inboxLoading || state.inboxWriting}>筛选本机消息</button>
    </form>
    {!valid && <p className="error-text">请输入如 000001.SZ 的完整代码，或留空查看全部。</p>}
    <div className="inbox-local-actions"><button type="button" disabled={state.inboxLoading || state.inboxWriting}
      onClick={() => void controller.refreshInbox({ ...state.inboxFilters, cursor: undefined })}>刷新本机消息</button>
      <button type="button" disabled={writeLocked || state.inboxCheckLoading || !data?.collection_revision}
        onClick={() => setClearingRevision(data!.collection_revision)}>清空消息收件箱</button>
      {current && <span>本机未读 {data.unread_count} 条</span>}</div>
    {clearingRevision && <div className="confirm-panel inbox-clear-confirm" role="group" aria-label="确认清空消息收件箱">
      <p>清空全部本机消息和检查记录？自选、持仓与聊天保留；下次主动检查重新建立基线。未确认或其他页面的检查也将失效。</p>
      <button type="button" disabled={state.inboxWriting || state.inboxLoading || state.inboxCheckLoading} onClick={() => {
        void controller.clearInbox(clearingRevision).then(done => { if (done) setClearingRevision(null); });
      }}>确认清空消息及基线</button>
      <button type="button" disabled={state.inboxWriting} onClick={() => setClearingRevision(null)}>取消清空</button>
      <p className="review-meta">如消息状态已变化，请取消、刷新本机消息后重新确认。</p>
    </div>}
    {state.inboxLoading && <p role="status">正在读取本机消息，不会请求新闻或公告来源…</p>}
    {data && !data.enabled && <p className="review-caution">个人记忆未开启，当前不读取消息或联网检查。</p>}
    {losses.map(value => <p className="review-caution" key={`${value.instrument_id}:${value.source}`}>
      {value.name}（{value.instrument_id}）· {inboxSourceLabel(value.source)}：因90天保留期或容量上限，累计清理了 {value.evicted_unread_count} 条未读消息
      {value.evicted_at && `，最近清理 ${inboxTime(value.evicted_at)}（上海时间）`}。未读数减少不代表全部已经阅读。
    </p>)}
    {current && <>
      <p className="review-meta">当前显示{state.inboxFilters.unread_only ? "未读" : "全部本机"}消息{state.inboxFilters.instrument_id ? ` · ${state.inboxFilters.instrument_id}` : ""}，每页最多20条。最旧消息可能因90天或每股500条上限被清理。</p>
      {!data.items.length && !state.inboxLoading && <p>{state.inboxFilters.unread_only ? "本机暂无未读消息。" : "本机暂无匹配消息。"}主动检查后才会获取来源的新标题。</p>}
      <div className="inbox-messages">{data.items.map(message => <article className="inbox-message" key={message.message_id} aria-label={`本机消息：${message.title}`}>
        <div className="inbox-message-heading"><strong>{message.name} <span>{message.instrument_id}</span></strong><span className="status-tag">{message.read ? "已读" : "未读"}</span></div>
        <h3><a href={message.url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{message.title}<span aria-hidden="true"> ↗</span></a></h3>
        <p className="review-meta">{inboxSourceLabel(message.source)} · {message.discovery === "research" ? "研究中发现，未推进手动检查水位" : message.baseline ? "初次建立基线" : "自上次检查以来新发现"}<br />
          发表 {message.publication_precision === "date" ? `${message.published_date}（仅日期，未提供时分）` : `${inboxTime(message.published_at!)}（上海时间，来源显示时刻）`}<br />
          首次发现 {inboxTime(message.first_seen_at)} · 来源取数 {inboxTime(message.retrieved_at)}（上海时间）</p>
        <button type="button" disabled={message.read || writeLocked} aria-label={`${message.read ? "已读" : "标为已读"}：${message.title}`}
          onClick={() => void controller.markInboxRead(message.message_id)}>{state.inboxReadSavingId === message.message_id ? "正在标记…" : message.read ? "已读" : "标为已读"}</button>
      </article>)}</div>
      {data.has_more && <button type="button" disabled={state.inboxLoading || state.inboxWriting} onClick={() => void controller.nextInboxPage()}>下一页本机消息</button>}
      <details className="inbox-source-states" open={failures.length > 0}><summary>逐股来源检查状态 · {states.length} 项{failures.length ? `，${failures.length} 项失败或部分失败` : ""}</summary>
        {states.length === 0 && <p className="review-meta">没有来源检查记录；先加入自选，再主动检查。</p>}
        <ul>{states.map(value => <li key={`${value.instrument_id}:${value.source}`}><strong>{value.name} · {value.instrument_id} · {inboxSourceLabel(value.source)}</strong>
          <span className={value.status === "partial" || value.status === "unavailable" ? "error-text" : ""}>{inboxStatusLabel[value.status]}{value.skipped_count ? `，跳过 ${value.skipped_count} 条未核验标题` : ""}</span>
          <small>{value.initialized ? "已建立手动检查基线" : "尚未建立手动检查基线"}；上次成功检查 {value.last_success_at ? `${inboxTime(value.last_success_at)}（上海时间）` : "未记录"}
            {value.last_attempt_at ? `；最近尝试 ${inboxTime(value.last_attempt_at)}（上海时间）` : ""}。成功时间也不代表此前全量覆盖。</small></li>)}</ul>
      </details>
    </>}
  </section>;
}

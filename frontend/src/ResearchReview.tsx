import { useId, useState, type FormEvent } from "react";
import type { ResearchController, WorkspaceState } from "./controller";
import { ReadableText } from "./ResponseContent";
import { ResearchReviewChart } from "./ResearchReviewChart";
import { reviewKey, reviewPriceLabels, reviewTime, reviewType, sameReview, validReviewFilters, type ResearchReviewItem } from "./researchReviews";
import { sourceLabel } from "./presentation";

function OriginalReview({ item }: { item: ResearchReviewItem }) {
  const plan = item.trade_plan;
  return <>
    <div className="review-record-heading"><h3>{item.name} <span>{item.instrument_id}</span></h3><span className="status-tag">{reviewType(item)}</span></div>
    <p>{item.question || "原问题未记录"}</p>
    <p className="review-meta">提问 {reviewTime(item.asked_at)}（上海时间） · 资料截止 {item.daily_cutoff ?? "未记录"}<br />
      回答完成 {item.completed_at ? `${reviewTime(item.completed_at)}（上海时间）` : "未记录"}</p>
    <p className="review-state">{({ available: "当时已形成研判", unavailable: "当时未形成研判", unrecorded: "原结构化研判未记录" })[item.status]}</p>
    <dl className="review-verdict"><div><dt>原评级</dt><dd>{item.rating ?? "未记录"}</dd></div><div><dt>原方向</dt><dd>{item.direction ?? "未记录"}</dd></div>
      <div><dt>原把握</dt><dd>{item.conviction ?? "未记录"}</dd></div><div><dt>原期限</dt><dd>{item.horizon ?? "未记录"}</dd></div></dl>
    {plan ? <><div className="table-scroll"><table className="reference-plan"><caption>当时程序参考价位 · 行情 {plan.source_date}</caption><thead><tr><th>项目</th><th>原参考值</th></tr></thead>
      <tbody>{Object.entries(plan.values).map(([key, value]) => <tr key={key}><th scope="row">{reviewPriceLabels[key] ?? key}</th><td>{value}{key.startsWith("reward_risk") || key === "atr_multiple" ? " 倍" : " 元"}</td></tr>)}</tbody></table></div>
      <p className="review-meta">来源 {plan.source ? sourceLabel(plan.source) : "未记录"} · {plan.price_adjustment === "unadjusted" ? "不复权" : "复权口径未确认"} ·
        {plan.data_authenticity === "provider_reported" ? "供应商报告，未经独立核实" : plan.data_authenticity === "synthetic" ? "人工样例，非真实行情" : "真实性未确认"}<br />
        方法 {plan.method === "atr_reference_plan_v2" ? "按期限的 ATR 参考计划" : "ATR 参考计划"}；原价位不代表当前建议或真实成交。</p></>
      : <p className="review-meta">未记录可核对的原程序参考价位。</p>}
    {item.prices.notice && <p className="review-caution">{item.prices.notice}</p>}
  </>;
}

export function ResearchReview({ state, controller, openConversation }: { state: WorkspaceState; controller: ResearchController; openConversation: (id: string) => void }) {
  const [instrumentId, setInstrumentId] = useState("");
  const [fromDate, setFromDate] = useState(""); const [toDate, setToDate] = useState("");
  const formId = useId();
  const filters = { instrument_id: instrumentId.trim().toUpperCase(), ...(fromDate ? { from_date: fromDate } : {}), ...(toDate ? { to_date: toDate } : {}) };
  const valid = validReviewFilters(filters);
  const edit = (set: (value: string) => void, value: string) => { controller.clearResearchReviews(); set(value); };
  const submit = (event: FormEvent) => { event.preventDefault(); if (valid) void controller.findResearchReviews(filters); };
  const detail = state.reviewDetail, prices = state.reviewPrices, page = state.reviews;
  return <section className="research-review" aria-label="研判回顾">
    <div className="review-heading"><div><span className="eyebrow">保存在本机的原记录</span><h2>研判回顾</h2></div>
      <button type="button" onClick={() => controller.closeResearchReviews()}>关闭回顾</button></div>
    <p className="review-intro">按股票查找过去的评级和参考价位。查看原记录不调用模型或行情；只有点击加载走势才请求后续日线，不占研究会话额度。</p>
    <form className="review-filters" aria-label="查找研判回顾" onSubmit={submit}>
      <label htmlFor={`${formId}-code`}>完整证券代码<input id={`${formId}-code`} value={instrumentId} maxLength={9} placeholder="000001.SZ"
        autoComplete="off" onChange={event => edit(setInstrumentId, event.target.value)} /></label>
      <label htmlFor={`${formId}-from`}>提问起始日期<input id={`${formId}-from`} type="date" value={fromDate} onChange={event => edit(setFromDate, event.target.value)} /></label>
      <label htmlFor={`${formId}-to`}>提问截止日期<input id={`${formId}-to`} type="date" value={toDate} onChange={event => edit(setToDate, event.target.value)} /></label>
      <button type="submit" disabled={!valid || state.reviewsLoading}>查找原研判</button>
    </form>
    <p className="review-meta">日期按上海时间筛选，可留空。每次最多扫描 20 条原始记录，匹配数可能较少；按保存的完整代码查询，不依赖当前证券目录。</p>
    {instrumentId && !valid && <p className="error-text">请输入如 000001.SZ 的完整代码，并确认起始日期不晚于截止日期。</p>}
    {state.reviewsLoading && <p role="status">正在扫描本机原记录…</p>}
    {[state.reviewsError, state.reviewDetailError, state.reviewPricesError].filter(Boolean).map((error, index) => <p key={index} className="error-text" role="alert">{error}</p>)}
    {page && !page.enabled && <p>本服务未开启研判回顾。</p>}
    {page?.enabled && <>
      <p className="review-scan-status" role="status">本次扫描 {page.scanned} 条原始记录，匹配 {page.items.length} 条，跳过异常 {page.skipped_invalid} 条。
        {page.has_more ? "尚未扫描完，可继续查找。" : "已扫描到本次筛选的末尾。"}</p>
      {page.items.length === 0 && <p>{page.has_more ? "本页暂无匹配，请继续查找。" : "本次筛选未找到可展示的研判。"}</p>}
      <div className="review-records">{page.items.map(item => {
        const selected = state.selectedReview && sameReview(state.selectedReview, item);
        const reasonId = `${formId}-${item.sequence}-${item.conversation_id}`;
        return <article key={reviewKey(item)} className={`review-record${selected ? " selected" : ""}`} aria-label={`${item.name}原研判 ${item.sequence}`}>
          <OriginalReview item={item} />
          <div className="review-actions"><button type="button" onClick={() => void controller.loadResearchReview(item)} disabled={!!selected && state.reviewDetailLoading}>查看原问答</button>
            <button type="button" aria-describedby={reasonId} disabled={!item.prices.enabled || state.reviewPricesLoading}
              onClick={() => void controller.loadResearchReviewPrices(item)}>加载之后走势</button>
            <button type="button" disabled={state.loading || state.phase !== "idle" || !!state.pendingId} onClick={() => openConversation(item.conversation_id)}>打开原对话</button></div>
          <p className="review-meta" id={reasonId}>{item.prices.enabled ? `点击后加载 ${item.prices.start_date} — ${item.prices.end_date} 的后续已完成日线；不自动重试。` : item.prices.message}</p>
          {selected && state.reviewDetailLoading && <p role="status">正在读取这一条原问答…</p>}
          {selected && detail && <section className="review-detail" aria-label="当时原问答，只读">
            <h4>当时原问答 · 只读 · 非新分析</h4>
            <p className="review-meta">第 {detail.item.sequence} 轮 · {reviewType(detail.item)} · 提问 {reviewTime(detail.item.asked_at)}（上海时间） · 资料截止 {detail.item.daily_cutoff ?? "未记录"}</p>
            {detail.item.prices.notice && <p className="review-caution">{detail.item.prices.notice}</p>}
            <p className="review-original-question">{detail.question}</p>
            <ReadableText text={detail.result.answer ?? detail.result.message} />
          </section>}
          {selected && state.reviewPricesLoading && <div className="review-price-loading" role="status"><p>正在加载这一只股票的后续日线…</p>
            <button type="button" onClick={() => controller.cancelResearchReviewPrices()}>停止等待走势</button></div>}
          {selected && prices && <ResearchReviewChart key={`${reviewKey(prices.item)}:${prices.retrieved_at}`} data={prices} />}
        </article>;
      })}</div>
      {page.has_more && <button type="button" disabled={state.reviewsLoading} onClick={() => void controller.continueResearchReviews()}>继续查找</button>}
    </>}
  </section>;
}

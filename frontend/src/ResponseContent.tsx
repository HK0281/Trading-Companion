import { useState } from "react";
import type { PublicTurn } from "./api";
import { formatNumber, parseConversation, parsePresentation, unavailableLabel, unitLabel } from "./presentation";

export function ReadableText({ text }: { text: string }) {
  // Render as text nodes; provider or source content never becomes executable HTML.
  return <div className="readable-text">{text.split(/\n\s*\n/).filter(Boolean).map((paragraph, index) =>
    <p key={index}>{paragraph}</p>)}</div>;
}

export function ResponseContent({ turn }: { turn: PublicTurn }) {
  const presentation = parsePresentation(turn);
  const conversation = parseConversation(turn);
  const [allMetrics, setAllMetrics] = useState(false);
  const fields = (turn.report?.fields ?? []).filter((field) => !presentation?.indicator_ids?.length
    || presentation.indicator_ids.includes(field.indicator_id));
  const unavailable = fields.filter((field) => field.status !== "available");
  if (conversation) return <div className="conversation-response readable-text">
    {conversation.paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)}
    {conversation.origin === "model" && <small className="response-origin">基于本轮历史日线的模型解读</small>}
  </div>;
  if (turn.conversation != null) return <ReadableText text="本轮回答未能正常显示，请查看下方研究数据。本次没有重新调用模型。" />;
  if (!presentation) return <ReadableText text={turn.answer ?? turn.message} />;
  const isResearch = presentation.kind !== "knowledge";
  return <div className="response-content">
    <h4 className="response-headline">{presentation.headline}</h4>
    {presentation.kind === "followup" && <p className="saved-result-label">基于已保存结果 · 未重新取数</p>}
    {isResearch && <>
      {unavailable.length > 0 && <p className="data-note" role="note">{unavailable.length} 项读数暂不可用，已保留缺失状态。</p>}
      <section className="key-readings" aria-label="关键读数">
        <div className="section-label"><span>关键读数</span><span>截至 {presentation.trade_date}</span></div>
        <dl className="metric-grid" data-count={Math.min(fields.length, 6)}>{(allMetrics ? fields : fields.slice(0, 6)).map((field) =>
          <div className={`metric ${field.status !== "available" ? "metric-missing" : ""}`} key={`${field.indicator_id}:${field.field}`}>
            <dt>{field.label}</dt><dd>{field.status !== "available" || field.value === null ? "—" : formatNumber(field.value)}
              <small>{field.status !== "available" ? unavailableLabel[field.status] ?? "不可用" : unitLabel[field.unit] ?? field.unit}</small></dd>
            {field.missing_bars > 0 && <span>还缺 {field.missing_bars} 条日线</span>}
          </div>)}</dl>
        {fields.length > 6 && <button className="text-button" type="button" aria-expanded={allMetrics}
          onClick={() => setAllMetrics(!allMetrics)}>{allMetrics ? "收起读数" : `查看全部 ${fields.length} 项读数`}</button>}
      </section>
    </>}
    <div className={isResearch ? "insight-grid" : "knowledge-stack"}>
      {presentation.sections.map((section) => <section className="insight" key={section.id}>
        <h5>{section.title}</h5>
        <ul>{section.items.slice(0, 2).map((item, index) => <li key={index}>{item}</li>)}</ul>
        {section.items.length > 2 && <details className="more-insights"><summary>其余 {section.items.length - 2} 项观察</summary>
          <ul>{section.items.slice(2).map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
      </section>)}
    </div>
  </div>;
}

export function EvidenceTable({ turn }: { turn: PublicTurn }) {
  const fields = turn.report?.fields ?? [];
  if (!fields.length) return null;
  return <details className="disclosure facts"><summary>指标字段与证据 <span>{fields.length} 个字段</span></summary>
    <div className="table-scroll"><table>
      <thead><tr><th>字段 / 交易日</th><th>数值</th><th>可用性</th><th>证据编号</th></tr></thead>
      <tbody>{fields.map((field) => <tr key={`${field.indicator_id}-${field.field}`}>
        <td>{field.label}<small>{field.trade_date}</small></td>
        <td className="number">{field.value === null ? "—" : formatNumber(field.value)}
          {field.value !== null && <small>{unitLabel[field.unit] ?? field.unit}</small>}</td>
        <td>{field.status === "available" ? "可用" : unavailableLabel[field.status] ?? "不可用"}
          {field.missing_bars > 0 && <small>还缺 {field.missing_bars} 条</small>}</td>
        <td><code>{field.fact_id ?? "无可用证据"}</code></td>
      </tr>)}</tbody>
    </table></div>
  </details>;
}

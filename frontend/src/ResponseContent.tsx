import { useState } from "react";
import type { PublicTurn } from "./api";
import { formatNumber, parseConversation, parsePresentation, unavailableLabel, unitLabel } from "./presentation";
import { CompanyParagraphs, CompanyCoverageNote, parseCompanyAnswer } from "./ProductResearch";
import { KnowledgeSources } from "./KnowledgeSources";
import { parseScreeningResult, ScreeningResults } from "./ScreeningResults";

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
  const company = parseCompanyAnswer(turn.company_research);
  if (turn.screening != null) {
    const screening = parseScreeningResult(turn.screening);
    return screening ? <ScreeningResults data={screening} activityDefault={turn.applied_defaults.includes("screening:activity_shortlist")} />
      : <ReadableText text={turn.answer ?? turn.message} />;
  }
  if (turn.knowledge != null) return <>
    <ReadableText text={turn.answer ?? turn.message} />
    <KnowledgeSources value={turn.knowledge} answer={turn.answer} />
  </>;
  if (company) return <>
    {turn.status === "limited" && <CompanyCoverageNote data={company} />}
    <CompanyParagraphs data={company} />
  </>;
  if (turn.company_research != null) return <ReadableText text="本轮研究回答未能正常显示，请重新发送研究问题。" />;
  if (conversation) return <div className="conversation-response readable-text">
    {conversation.paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)}
    {conversation.origin === "model" && <small className="response-origin">基于本轮历史日线的模型解读</small>}
  </div>;
  if (turn.conversation != null) return <ReadableText text="本轮回答未能正常显示，请重新发送研究问题。本次没有重新调用模型。" />;
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

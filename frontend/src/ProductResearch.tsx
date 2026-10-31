/** Checked research paragraphs; source metadata remains validated without display. */
import { parseDecision, ResearchDecision } from "./ResearchDecision";
import type { DecisionData } from "./ResearchDecision";
import { parseNarratives, ResearchNarrative } from "./ResearchNarrative";
import type { Narrative, NarrativeQuality } from "./ResearchNarrative";

type Evidence = { id: string; instrument_id: string; kind: string; label: string; display: string;
  source: string; date: string | null; url: string | null; content_kind: string; report_period: string | null;
  temporal_scope: string };
type CompanyAnswer = { paragraphs: string[]; evidence: Evidence[]; origin: string;
  decision?: DecisionData;
  narratives?: Narrative[]; quality?: NarrativeQuality;
  paragraph_evidence?: string[][];
  coverage?: unknown;
  excluded_domains?: unknown;
  securities: { name: string; instrument_id: string }[]; daily_cutoff: string };
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max = 1600): value is string => typeof value === "string" && value.length > 0 && value.length <= max;
const optionalText = (value: unknown) => value === null || value === undefined || typeof value === "string" && value.length <= 2000;
const hosts = ["finance.sina.com.cn", "news.sina.com.cn", "money.finance.sina.com.cn", "vip.stock.finance.sina.com.cn",
  "finance.eastmoney.com", "data.eastmoney.com", "data.eastmoney.com.cn", "data.eastmoney.com", "www.cninfo.com.cn", "static.cninfo.com.cn",
  "emweb.securities.eastmoney.com", "data.eastmoney.com", "notice.eastmoney.com", "pdf.dfcfw.com",
  "www.sse.com.cn", "edu.sse.com.cn", "www.szse.cn", "investor.szse.cn", "www.sec.gov", "www.investor.gov",
  "lautorite.qc.ca", "www.idx.id", "github.com"];

export function sourceUrl(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 2000 || !value.startsWith("https://") || /[\s\\\u0000-\u001f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port
      && hosts.includes(url.hostname)
      && (url.hostname !== "github.com" || url.pathname.startsWith("/HK0281/Trading-Companion/")) ? url.href : null;
  } catch { return null; }
}

export function parseCompanyAnswer(value: unknown): CompanyAnswer | null {
  if (!object(value) || !["product-research-answer-v1", "product-research-answer-v2", "product-research-answer-v3"].includes(String(value.version))
      || !["model", "application"].includes(String(value.origin))
      || !Array.isArray(value.paragraphs) || value.paragraphs.length < 1 || value.paragraphs.length > 6
      || !value.paragraphs.every(item => text(item, value.version === "product-research-answer-v3" ? 24000 : 1800))
      || !Array.isArray(value.evidence) || value.evidence.length > (value.version === "product-research-answer-v3" ? 480 : 240)
      || !Array.isArray(value.securities) || value.securities.length > 3
      || !value.securities.every(item => object(item) && text(item.name, 80) && /^[0-9]{6}\.(SH|SZ|BJ)$/.test(String(item.instrument_id)))
      || !text(value.daily_cutoff, 10) || !/^\d{4}-\d{2}-\d{2}$/.test(value.daily_cutoff)) return null;
  const allowed = new Set(value.securities.map(item => item.instrument_id));
  if (!value.evidence.every(item => object(item) && text(item.id, 200) && allowed.has(item.instrument_id)
      && ["profile", "financial", "news", "announcements", "technical", "risk"].includes(String(item.kind))
      && text(item.label, 100) && text(item.display, 2000) && text(item.source, 80)
      && optionalText(item.date) && optionalText(item.url) && optionalText(item.report_period)
      && text(item.content_kind, 40) && text(item.temporal_scope, 80))) return null;
  const evidenceIds = new Set(value.evidence.map(item => item.id));
  if (evidenceIds.size !== value.evidence.length) return null;
  // Older and application fallback answers may omit paragraph mappings entirely.
  if (Object.prototype.hasOwnProperty.call(value, "paragraph_evidence")) {
    const mappings = value.paragraph_evidence;
    if (!Array.isArray(mappings) || mappings.length !== 0 && mappings.length !== value.paragraphs.length
      || !mappings.every(items => Array.isArray(items) && items.length <= evidenceIds.size
        && items.every(id => text(id, 200) && evidenceIds.has(id)) && new Set(items).size === items.length)) return null;
  }
  if (value.version === "product-research-answer-v3") {
    const narrative = parseNarratives(value, allowed, new Map(value.evidence.map(item => [item.id, item.instrument_id])));
    if (!narrative) return null;
    const decision = Array.isArray(value.decisions) && value.decisions.length > 0
      ? parseDecision(value, allowed, new Map(value.evidence.map(item => [item.id, item.instrument_id]))) : undefined;
    if (decision === null) return null;
    return { ...value, ...narrative, decision } as unknown as CompanyAnswer;
  }
  if (value.version === "product-research-answer-v2") {
    const decision = parseDecision(value, allowed, new Map(value.evidence.map(item => [item.id, item.instrument_id])));
    if (!decision) return null;
    return { ...value, decision } as unknown as CompanyAnswer;
  }
  return value as unknown as CompanyAnswer;
}

const kinds: Record<string, string> = { profile: "公司资料", financial: "财务报告", news: "新闻", announcements: "公司公告", technical: "技术分析", risk: "风险情景" };
const coverageKinds: Record<string, string> = { ...kinds, financial: "财务", announcements: "公告", market_context: "交易所背景" };

export function CompanyCoverageNote({ data }: { data: CompanyAnswer }) {
  if (!Array.isArray(data.coverage)) return null;
  const missing: string[] = [];
  for (const company of data.coverage.slice(0, 3)) {
    if (data.quality?.state !== "complete") continue;
    if (!object(company) || !Array.isArray(company.sections)) continue;
    const security = data.securities.find(s => s.instrument_id === company.instrument_id);
    if (!security) continue;
    const labels = [...new Set(company.sections.slice(0, 8).flatMap(section =>
      object(section) && ["unavailable", "empty", "blocked", "insufficient_history"].includes(String(section.status))
        && typeof section.kind === "string" && Object.hasOwn(coverageKinds, section.kind) ? [coverageKinds[section.kind]] : []))];
    if (labels.length) missing.push(`${data.securities.length > 1 ? security.name + "：" : ""}${labels.join("、")}本轮未取得`);
  }
  const excluded = Array.isArray(data.excluded_domains) ? [...new Set(data.excluded_domains.slice(0, 5).flatMap(kind =>
    typeof kind === "string" && Object.hasOwn(kinds, kind) ? [kinds[kind]] : []))] : [];
  if (!missing.length && !excluded.length) return null;
  return <>
    {missing.length > 0 && <p className="data-note" role="note">回答已通过本轮校验；{missing.join("；")}。现有资料仍可使用。</p>}
    {excluded.length > 0 && <p className="data-note" role="note">{excluded.join("、")}按要求未研究。</p>}
  </>;
}
export function CompanyParagraphs({ data }: { data: CompanyAnswer }) {
  return <div className="conversation-response readable-text">
    {data.narratives && data.quality && <ResearchNarrative narratives={data.narratives} quality={data.quality} securities={data.securities} />}
    {data.decision && <ResearchDecision data={data.decision} securities={data.securities} />}
    {(!data.narratives || data.narratives.length === 0) && data.paragraphs.map((paragraph, index) =>
      <div className="company-paragraph" key={index}><p>{paragraph}</p></div>)}
  </div>;
}

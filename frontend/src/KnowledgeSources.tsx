/** Local reference membership does not establish full-text or current-rule verification. */
import { sourceUrl } from "./ProductResearch";

type KnowledgeCitation = {
  id: string; snippet_id: string; title: string; excerpt: string; version: string; maintained_on: string;
  content_origin: "maintainer_explanation"; source_type: "official_reference" | "project_reference";
  source_title: string; publisher: string; url: string; reference_version: string; live_verified: false;
  paragraph_indices: number[];
};
type KnowledgeReferences = { paragraphs: string[]; citations: KnowledgeCitation[] };
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max: number): value is string => typeof value === "string" && value.trim().length > 0
  && value.length <= max && !/[\u0000-\u001f\u007f-\u009f\u200b]/.test(value);
const date = (value: unknown): value is string => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().startsWith(value);
const officialHosts = new Set(["www.sse.com.cn", "edu.sse.com.cn", "investor.szse.cn", "www.sec.gov",
  "www.investor.gov", "lautorite.qc.ca", "www.idx.id"]);

export function knowledgeSourceUrl(citation: Pick<KnowledgeCitation, "url" | "source_type">): string | null {
  const accepted = sourceUrl(citation.url);
  if (!accepted) return null;
  const url = new URL(accepted);
  return citation.source_type === "project_reference" ? url.hostname === "github.com" ? accepted : null
    : officialHosts.has(url.hostname) ? accepted : null;
}

export function parseKnowledgeReferences(value: unknown, answer: string | null): KnowledgeReferences | null {
  if (!object(value) || value.version !== "general-knowledge-answer-v1" || value.status !== "accepted"
    || value.origin !== "model" || value.market_data_requested !== false || value.trading_enabled !== false
    || value.validation_scope !== "format_scope_and_citation_membership_not_prose_semantics"
    || !Array.isArray(value.paragraphs) || value.paragraphs.length < 1 || value.paragraphs.length > 6
    || !value.paragraphs.every(item => text(item, 800)) || value.paragraphs.join("\n\n") !== value.text || value.text !== answer
    || !Array.isArray(value.citations) || value.citations.length < 1 || value.citations.length > 4) return null;
  const paragraphCount = value.paragraphs.length;
  const bound = new Set<number>();
  for (const item of value.citations) {
    if (!object(item) || !text(item.id, 2) || !/^K[1-4]$/.test(item.id)
      || !text(item.snippet_id, 64) || !/^[a-z][a-z0-9_]{1,63}$/.test(item.snippet_id)
      || !text(item.title, 100) || !text(item.excerpt, 500) || !text(item.version, 100) || !date(item.maintained_on)
      || item.content_origin !== "maintainer_explanation" || item.source_type !== "official_reference" && item.source_type !== "project_reference"
      || !text(item.source_title, 500) || !text(item.publisher, 500) || !text(item.url, 2000)
      || !text(item.reference_version, 500) || item.live_verified !== false
      || !Array.isArray(item.paragraph_indices) || item.paragraph_indices.length < 1 || item.paragraph_indices.length > paragraphCount
      || !item.paragraph_indices.every(index => Number.isInteger(index) && index >= 0 && index < paragraphCount)
      || new Set(item.paragraph_indices).size !== item.paragraph_indices.length) return null;
    for (const index of item.paragraph_indices) bound.add(index);
  }
  if (bound.size !== paragraphCount || new Set(value.citations.map(item => item.id)).size !== value.citations.length
    || new Set(value.citations.map(item => item.snippet_id)).size !== value.citations.length) return null;
  return value as unknown as KnowledgeReferences;
}

export function KnowledgeSources({ value, answer }: { value: unknown; answer: string | null }) {
  const data = parseKnowledgeReferences(value, answer);
  if (!data) return null;
  return <details className="disclosure company-sources" aria-label="知识来源">
    <summary>知识来源 <span>{data.citations.length} 条本地说明</span></summary>
    <p className="workflow-note">说明由项目维护者整理；参考链接未在本轮联网核验。</p>
    <div className="company-source-list">{data.citations.map(item => {
      const url = knowledgeSourceUrl(item);
      return <section className="company-source" key={item.snippet_id}>
        <div className="company-source-heading"><strong>{item.title}</strong>
          <span>第{[...item.paragraph_indices].sort((a, b) => a - b).map(index => index + 1).join("、")}段参考</span></div>
        <p>{item.excerpt}</p>
        <small>本地版本 {item.version} · 维护于 {item.maintained_on}</small>
        <p>{item.source_type === "official_reference" ? "官方参考资料" : "项目参考资料"}：{item.source_title} · {item.publisher}</p>
        <small>{item.reference_version}{url && <> · <a href={url} target="_blank" rel="noopener noreferrer">查看参考资料</a></>}</small>
      </section>;
    })}</div>
  </details>;
}

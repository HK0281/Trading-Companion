/** Optional, bounded excerpts; document failures do not change the research answer. */

type Excerpt = { paragraph_index: number; text: string; sha256: string };
type DocumentCoverage = {
  complete: boolean; omitted_tables: number; page_count: number;
  private_use_characters_replaced: number; omitted_trailing_characters: number;
};
type DocumentItem = {
  instrument_id: string; kind: "news" | "announcements"; parent_title_id: string;
  title: string; url: string; source: string; status: "available" | "unavailable"; code: string;
  document_id?: string; sha256?: string; extractor_version?: string;
  extracted_characters?: number; paragraph_count?: number; excerpts?: Excerpt[];
} & Partial<DocumentCoverage>;
export type ResearchDocumentData = {
  version: "research-documents-v1"; status: "available" | "partial" | "unavailable" | "skipped";
  code: string; items: DocumentItem[];
};
type ParentEvidence = { id: string; instrument_id: string; kind: string; source: string; display: string;
  content_kind: string; url: string | null };
type SourceUrl = (value: unknown) => string | null;
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, maximum: number): value is string => typeof value === "string" && value.trim().length > 0 && value.length <= maximum;
const code = (value: unknown): value is string => typeof value === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(value);
const digest = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const count = (value: unknown, maximum = 32000): value is number => Number.isSafeInteger(value) && Number(value) > 0 && Number(value) <= maximum;
const coverageCount = (value: unknown, minimum = 0, maximum = 512000): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;

function documentCoverage(item: Record<string, unknown>, kind: DocumentItem["kind"]): DocumentCoverage | false | undefined {
  const keys = ["complete", "omitted_tables", "page_count", "private_use_characters_replaced", "omitted_trailing_characters"];
  const supplied = keys.filter(key => Object.hasOwn(item, key)).length;
  const legacyVersion = kind === "news" ? "sina-artibody-v2" : "eastmoney-announcement-body-v1";
  const currentVersion = kind === "news" ? "sina-artibody-v3" : "eastmoney-announcement-body-v2";
  if (!supplied) return item.extractor_version === legacyVersion ? undefined : false;
  if (supplied !== keys.length || item.extractor_version !== currentVersion || typeof item.complete !== "boolean"
      || !coverageCount(item.omitted_tables) || !coverageCount(item.page_count, 1)
      || !coverageCount(item.private_use_characters_replaced)
      || !coverageCount(item.omitted_trailing_characters, 0, kind === "news" ? 16000 : 32000)
      || kind === "news" && (item.page_count !== 1 || item.private_use_characters_replaced !== 0)
      || kind === "announcements" && item.omitted_tables !== 0) return false;
  const reason = kind === "news" ? (item.omitted_tables ? "document_tables_omitted" : "document_read")
    : (item.page_count > 1 ? "document_first_page_only" : "document_read");
  if (item.complete !== (reason === "document_read") || item.code !== reason
      || item.complete && item.omitted_trailing_characters !== 0) return false;
  return { complete: item.complete, omitted_tables: item.omitted_tables, page_count: item.page_count,
    private_use_characters_replaced: item.private_use_characters_replaced,
    omitted_trailing_characters: item.omitted_trailing_characters };
}

function articleUrl(value: unknown, kind: string, instrument: string, validate: SourceUrl): string | null {
  const url = validate(value);
  if (!url) return null;
  if (kind === "news") return /^https:\/\/(?:finance\.sina\.com\.cn\/(?:[A-Za-z0-9_-]+\/)*doc-[a-z0-9]+\.shtml|cj\.sina\.cn\/articles\/view\/[0-9]+\/[A-Za-z0-9]+)$/.test(url) ? url : null;
  return new RegExp("^https://data\\.eastmoney\\.com/notices/detail/" + instrument.slice(0, 6) + "/AN[0-9]{12,40}\\.html$").test(url) ? url : null;
}

export function parseResearchDocuments(value: unknown, allowed: Set<unknown>, evidence: ParentEvidence[], validateUrl: SourceUrl): ResearchDocumentData | undefined {
  if (!object(value) || value.version !== "research-documents-v1"
      || !["available", "partial", "unavailable", "skipped"].includes(String(value.status)) || !code(value.code)
      || !Array.isArray(value.items) || value.items.length > 3) return undefined;
  const items: DocumentItem[] = [], seen = new Set<string>();
  for (const item of value.items) {
    if (!object(item) || typeof item.instrument_id !== "string" || !allowed.has(item.instrument_id)
        || !["news", "announcements"].includes(String(item.kind)) || !text(item.parent_title_id, 200)
        || seen.has(item.parent_title_id) || !text(item.title, 600) || !code(item.code)
        || !["available", "unavailable"].includes(String(item.status))) continue;
    const source = item.kind === "news" ? "sina_company_news" : "eastmoney_announcements";
    const url = articleUrl(item.url, String(item.kind), item.instrument_id, validateUrl);
    const parent = evidence.find(record => record.id === item.parent_title_id);
    if (item.source !== source || !url || !parent || parent.instrument_id !== item.instrument_id
        || parent.kind !== item.kind || parent.content_kind !== "title" || parent.source !== source || parent.display !== item.title
        || parent.url !== url) continue;
    const base = { instrument_id: item.instrument_id, kind: item.kind as DocumentItem["kind"],
      parent_title_id: item.parent_title_id, title: item.title, url, source, status: item.status as DocumentItem["status"], code: item.code };
    if (item.status === "unavailable") {
      items.push(base);
      seen.add(item.parent_title_id);
      continue;
    }
    const suppliedExcerpts = item.excerpts === undefined ? [] : item.excerpts;
    const maximumCharacters = base.kind === "news" ? 16000 : 32000;
    const maximumParagraphs = base.kind === "news" ? 128 : 400;
    if (!text(item.document_id, 200) || !digest(item.sha256) || !text(item.extractor_version, 100)
        || !count(item.extracted_characters, maximumCharacters) || !count(item.paragraph_count, maximumParagraphs)
        || !Array.isArray(suppliedExcerpts) || suppliedExcerpts.length > 2) continue;
    const coverage = documentCoverage(item, base.kind);
    if (coverage === false || coverage && item.extracted_characters + coverage.omitted_trailing_characters > maximumCharacters) continue;
    const excerptKeys = new Set<string>();
    const excerpts: Excerpt[] = [];
    for (const excerpt of suppliedExcerpts) {
      if (!object(excerpt) || !Number.isSafeInteger(excerpt.paragraph_index) || Number(excerpt.paragraph_index) < 1
          || Number(excerpt.paragraph_index) > item.paragraph_count
          || !text(excerpt.text, 480) || !digest(excerpt.sha256)
          || excerptKeys.has(`${excerpt.paragraph_index}:${excerpt.sha256}`)) break;
      excerpts.push({ paragraph_index: Number(excerpt.paragraph_index), text: excerpt.text, sha256: excerpt.sha256 });
      excerptKeys.add(`${excerpt.paragraph_index}:${excerpt.sha256}`);
    }
    if (excerpts.length !== suppliedExcerpts.length) continue;
    items.push({ ...base, document_id: item.document_id, sha256: item.sha256, extractor_version: item.extractor_version,
      extracted_characters: item.extracted_characters, paragraph_count: item.paragraph_count, excerpts, ...coverage });
    seen.add(item.parent_title_id);
  }
  return { version: "research-documents-v1", status: value.status as ResearchDocumentData["status"], code: value.code, items };
}

export function ResearchDocuments({ data, securities }: { data: ResearchDocumentData; securities: { name: string; instrument_id: string }[] }) {
  if (!data.items.length) return null;
  return <details className="research-documents">
    <summary>正文片段</summary>
    <div className="research-document-list">{data.items.map(item => {
      const name = securities.find(security => security.instrument_id === item.instrument_id)?.name;
      return <details className="research-document" key={item.parent_title_id}>
        <summary>{securities.length > 1 && name ? `${name} · ` : ""}{item.title}</summary>
        {item.status === "available" ? <div className="research-document-content">
          {item.complete === false ? <p className="research-document-note">{item.kind === "announcements" ? "只读取了第 1 页" : "表格未读取"}</p> : null}
          <p className="research-document-note">已提取 {item.extracted_characters} 字；{item.excerpts?.length ? "供回答的正文片段，非全文。" : "本次未用于回答。"}</p>
          {item.excerpts?.length ? item.excerpts.map(excerpt => <blockquote key={`${excerpt.paragraph_index}:${excerpt.sha256}`}><p>{excerpt.text}</p></blockquote>)
            : null}
          <a href={item.url} target="_blank" rel="noopener noreferrer">查看原文</a>
        </div> : <p className="research-document-note">正文未取得，仍可参考标题。</p>}
      </details>;
    })}</div>
  </details>;
}

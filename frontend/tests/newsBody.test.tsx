import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CompanyParagraphs, parseCompanyAnswer, sourceUrl } from "../src/ProductResearch";
import narrative from "./decisionFixture.json";
import decision from "./decisionLegacyFixture.json";

const title = "平安银行：经营情况说明";
const url = "https://finance.sina.com.cn/stock/doc-abcdefghijkl.shtml";
const sha = "a".repeat(64);
const parent = () => ({ id: "title-1", instrument_id: "000001.SZ", kind: "news", label: "新闻标题",
  display: title, source: "sina_company_news", date: "2026-11-12", url, content_kind: "title",
  report_period: null, temporal_scope: "published_before_cutoff" });
const document = () => ({ instrument_id: "000001.SZ", kind: "news", parent_title_id: "title-1", title, url,
  source: "sina_company_news", status: "available", code: "document_body_available", document_id: "document-1", sha256: sha,
  extractor_version: "sina-artibody-v2", extracted_characters: 1200, paragraph_count: 8,
  excerpts: [{ paragraph_index: 2, text: "公司披露了经营进展，同时说明仍存在不确定性。", sha256: sha }] });
const completeDocument = () => ({ ...document(), code: "document_read", extractor_version: "sina-artibody-v3",
  complete: true, omitted_tables: 0, page_count: 1, private_use_characters_replaced: 0, omitted_trailing_characters: 0 });
const documents = () => ({ version: "research-documents-v1", status: "available", code: "document_bodies_available", items: [document()] });
function payload(version = "product-research-answer-v1") {
  const base = version === "product-research-answer-v3" ? structuredClone(narrative)
    : version === "product-research-answer-v2" ? structuredClone(decision) : {
      version, origin: "model", paragraphs: ["研究回答保持可读。"], daily_cutoff: "2026-11-12",
      securities: [{ instrument_id: "000001.SZ", name: "平安银行" }], evidence: [] as ReturnType<typeof parent>[] };
  return { ...base, evidence: [...base.evidence, parent()], documents: documents() };
}
function useAnnouncement(value: ReturnType<typeof payload>) {
  const fields = { kind: "announcements", source: "eastmoney_announcements",
    url: "https://data.eastmoney.com/notices/detail/000001/AN123456789012.html" };
  Object.assign(value.evidence[value.evidence.length - 1], fields);
  Object.assign(value.documents.items[0], fields, { extractor_version: "eastmoney-announcement-body-v2" });
}
function openDocuments() {
  fireEvent.click(screen.getByText("正文片段"));
  fireEvent.click(screen.getByText(title));
}

describe("新闻与公告的正文片段", () => {
  it.each(["product-research-answer-v1", "product-research-answer-v2", "product-research-answer-v3"])("在%s回答下折叠展示片段、原文和读取边界", version => {
    const value = payload(version), original = structuredClone(value);
    render(<CompanyParagraphs data={parseCompanyAnswer(value)!} />);
    expect(screen.getByText("正文片段")).toBeVisible();
    expect(screen.getByText(value.documents.items[0].excerpts[0].text)).not.toBeVisible();
    openDocuments();
    expect(screen.getByText(value.documents.items[0].excerpts[0].text)).toBeVisible();
    expect(screen.getByText("已提取 1200 字；供回答的正文片段，非全文。")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看原文" })).toHaveAttribute("href", url);
    expect(screen.queryByText(/document_body_available|sina-artibody-v2|document-1/)).not.toBeInTheDocument();
    expect(value).toEqual(original);
  });

  it("失败时保留回答并仅展示可理解的未读取说明", () => {
    const value = payload();
    value.documents.items[0].status = "unavailable";
    value.documents.items[0].code = "source_connection_failed";
    render(<CompanyParagraphs data={parseCompanyAnswer(value)!} />);
    openDocuments();
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
    expect(screen.getByText("正文未取得，仍可参考标题。")).toBeVisible();
    expect(screen.queryByText(/source_connection_failed|已提取/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByText(document().excerpts[0].text)).not.toBeInTheDocument();
  });

  it("未进入回答的文档不展示原始提取文本", () => {
    const value = payload();
    value.documents.items[0].excerpts = [];
    render(<CompanyParagraphs data={parseCompanyAnswer(value)!} />);
    openDocuments();
    expect(screen.getByText("已提取 1200 字；本次未用于回答。")).toBeVisible();
    expect(screen.queryByText(document().excerpts[0].text)).not.toBeInTheDocument();
  });

  it.each(["news", "announcements"])("保留%s完整正文的新版覆盖信息", kind => {
    const value = payload();
    Object.assign(value.documents.items[0], completeDocument());
    if (kind === "announcements") {
      useAnnouncement(value);
      Object.assign(value.documents.items[0], { private_use_characters_replaced: 512000 });
    }
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items[0].complete).toBe(true);
    expect(parsed.documents!.items[0].page_count).toBe(1);
    render(<CompanyParagraphs data={parsed} />);
    openDocuments();
    expect(screen.queryByText(/只读取了第 1 页|表格未读取/)).not.toBeInTheDocument();
    expect(screen.getByText(document().excerpts[0].text)).toBeVisible();
  });

  it.each([
    ["news", true], ["news", false], ["announcements", true], ["announcements", false],
  ] as const)("%s局部正文在片段供应=%s时仍显示固定读取边界", (kind, supplied) => {
    const value = payload();
    Object.assign(value.documents.items[0], completeDocument(), { complete: false });
    if (kind === "announcements") {
      useAnnouncement(value);
      Object.assign(value.documents.items[0], { code: "document_first_page_only", page_count: 512000,
        private_use_characters_replaced: 3, omitted_trailing_characters: 30800 });
    } else {
      Object.assign(value.documents.items[0], { code: "document_tables_omitted", omitted_tables: 512000,
        omitted_trailing_characters: 14800 });
    }
    if (!supplied) value.documents.items[0].excerpts = [];
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items[0].complete).toBe(false);
    render(<CompanyParagraphs data={parsed} />);
    openDocuments();
    expect(screen.getByText(kind === "announcements" ? "只读取了第 1 页" : "表格未读取")).toBeVisible();
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
    if (supplied) expect(screen.getByText(document().excerpts[0].text)).toBeVisible();
    else expect(screen.getByText("已提取 1200 字；本次未用于回答。")).toBeVisible();
    expect(screen.queryByText(/document_tables_omitted|document_first_page_only/)).not.toBeInTheDocument();
  });

  it.each(["news", "announcements"])("%s的提取器版本与新旧字段组严格配对", kind => {
    const legacyVersion = kind === "news" ? "sina-artibody-v2" : "eastmoney-announcement-body-v1";
    const currentVersion = kind === "news" ? "sina-artibody-v3" : "eastmoney-announcement-body-v2";
    for (const supplied of [false, true]) {
      for (const version of ["sina-artibody-v2", "sina-artibody-v3", "eastmoney-announcement-body-v1", "eastmoney-announcement-body-v2", "unknown-extractor"]) {
        const value = payload();
        if (supplied) Object.assign(value.documents.items[0], completeDocument());
        if (kind === "announcements") useAnnouncement(value);
        value.documents.items[0].extractor_version = version;
        const accepted = version === (supplied ? currentVersion : legacyVersion);
        expect(parseCompanyAnswer(value)!.documents!.items, `${kind}, coverage=${supplied}, ${version}`).toHaveLength(accepted ? 1 : 0);
      }
    }
  });

  it.each(["news", "announcements"])("%s新版局部标志全删后不能降级成旧完整记录", kind => {
    const value = payload();
    const item: Record<string, unknown> = Object.assign(value.documents.items[0], completeDocument(), { complete: false });
    if (kind === "announcements") {
      useAnnouncement(value);
      Object.assign(item, { code: "document_first_page_only", page_count: 2 });
    } else Object.assign(item, { code: "document_tables_omitted", omitted_tables: 1 });
    for (const key of ["complete", "omitted_tables", "page_count", "private_use_characters_replaced", "omitted_trailing_characters"]) delete item[key];
    item.code = "document_read";
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items).toHaveLength(0);
    render(<CompanyParagraphs data={parsed} />);
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
    expect(screen.queryByText("正文片段")).not.toBeInTheDocument();
  });

  it.each(["news", "announcements"])("%s提取正文和裁尾字数之和不能超过原文上限", kind => {
    const value = payload(), maximum = kind === "news" ? 16000 : 32000;
    const item = Object.assign(value.documents.items[0], completeDocument(),
      { complete: false, omitted_trailing_characters: maximum - 1200 });
    if (kind === "announcements") {
      useAnnouncement(value);
      Object.assign(item, { code: "document_first_page_only", page_count: 2 });
    } else Object.assign(item, { code: "document_tables_omitted", omitted_tables: 1 });
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(1);
    item.omitted_trailing_characters += 1;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
  });

  it.each(["news", "announcements"])("%s旧完整记录仍受对应正文长度上限约束", kind => {
    const value = payload(), maximum = kind === "news" ? 16000 : 32000;
    if (kind === "announcements") {
      useAnnouncement(value);
      value.documents.items[0].extractor_version = "eastmoney-announcement-body-v1";
    }
    value.documents.items[0].extracted_characters = maximum;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(1);
    value.documents.items[0].extracted_characters += 1;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
  });

  it.each([false, true])("新闻在新版字段组=%s时允许128段并拒绝129段", coverage => {
    const value = payload();
    if (coverage) Object.assign(value.documents.items[0], completeDocument());
    const item = value.documents.items[0];
    item.paragraph_count = 128;
    item.excerpts[0].paragraph_index = 128;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(1);
    item.paragraph_count = 129;
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items).toHaveLength(0);
    render(<CompanyParagraphs data={parsed} />);
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
  });

  it.each([
    [false, 129], [false, 256], [false, 400], [true, 129], [true, 256], [true, 400],
  ] as const)("公告在新版字段组=%s时保留第%s段的有效片段", (coverage, paragraph) => {
    const value = payload();
    if (coverage) Object.assign(value.documents.items[0], completeDocument());
    useAnnouncement(value);
    if (!coverage) value.documents.items[0].extractor_version = "eastmoney-announcement-body-v1";
    const item = value.documents.items[0];
    item.paragraph_count = 400;
    item.excerpts[0].paragraph_index = paragraph;
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items[0].paragraph_count).toBe(400);
    expect(parsed.documents!.items[0].excerpts![0].paragraph_index).toBe(paragraph);
    render(<CompanyParagraphs data={parsed} />);
    openDocuments();
    expect(screen.getByText(item.excerpts[0].text)).toBeVisible();
  });

  it.each([false, true])("公告在新版字段组=%s时拒绝401段及超过实际段数的片段", coverage => {
    const value = payload();
    if (coverage) Object.assign(value.documents.items[0], completeDocument());
    useAnnouncement(value);
    if (!coverage) value.documents.items[0].extractor_version = "eastmoney-announcement-body-v1";
    const item = value.documents.items[0];
    item.paragraph_count = 401;
    item.excerpts[0].paragraph_index = 400;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
    item.paragraph_count = 400;
    item.excerpts[0].paragraph_index = 401;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
    item.paragraph_count = 128;
    item.excerpts[0].paragraph_index = 129;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
  });

  it.each(["complete", "omitted_tables", "page_count", "private_use_characters_replaced", "omitted_trailing_characters"])("拒绝缺少%s的新字段组并保留回答", key => {
    const value = payload();
    const item: Record<string, unknown> = Object.assign(value.documents.items[0], completeDocument());
    delete item[key];
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items).toHaveLength(0);
    render(<CompanyParagraphs data={parsed} />);
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
  });

  it.each([
    ["complete", "true"], ["complete", 1], ["complete", undefined],
    ["omitted_tables", -1], ["omitted_tables", true], ["omitted_tables", 512001],
    ["page_count", 0], ["page_count", 1.5], ["page_count", 512001],
    ["private_use_characters_replaced", -1], ["private_use_characters_replaced", "0"],
    ["private_use_characters_replaced", 512001], ["omitted_trailing_characters", -1],
    ["omitted_trailing_characters", Number.NaN], ["omitted_trailing_characters", 1],
    ["complete", false], ["code", "document_tables_omitted"],
  ])("拒绝%s=%s的覆盖矛盾或错误类型", (key, invalid) => {
    const value = payload();
    const item: Record<string, unknown> = Object.assign(value.documents.items[0], completeDocument());
    item[String(key)] = invalid;
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
  });

  it.each(["news-pages", "news-private-use", "ann-tables", "news-tail", "ann-tail", "partial-complete", "wrong-reason"])("拒绝%s造成的来源或局部状态矛盾", fault => {
    const value = payload();
    const item: Record<string, unknown> = Object.assign(value.documents.items[0], completeDocument());
    if (fault.startsWith("ann-")) useAnnouncement(value);
    if (fault === "news-pages") Object.assign(item, { complete: false, code: "document_first_page_only", page_count: 2 });
    if (fault === "news-private-use") item.private_use_characters_replaced = 1;
    if (fault === "ann-tables") Object.assign(item, { complete: false, code: "document_tables_omitted", omitted_tables: 1 });
    if (fault === "news-tail") Object.assign(item, { complete: false, code: "document_tables_omitted", omitted_tables: 1, omitted_trailing_characters: 16001 });
    if (fault === "ann-tail") Object.assign(item, { complete: false, code: "document_first_page_only", page_count: 2, omitted_trailing_characters: 32001 });
    if (fault === "partial-complete") Object.assign(item, { code: "document_tables_omitted", omitted_tables: 1 });
    if (fault === "wrong-reason") Object.assign(item, { complete: false, code: "document_first_page_only", omitted_tables: 1 });
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(0);
  });

  it("允许同一原文段落的两段不同片段，并按段号与片段散列去重", () => {
    const value = payload();
    value.documents.items[0].excerpts = [
      { paragraph_index: 2, text: "公司披露了经营进展，" + "主营业务仍按原计划推进，".repeat(21) + "后续以正式披露为准。",
        sha256: "c1d6299826bf2c898b572407462716536b0c28d995c3450e0671b3cd890744fa" },
      { paragraph_index: 2, text: "同时说明仍存在不确定性，" + "经营计划受市场环境影响，".repeat(21) + "具体结果尚需持续观察。",
        sha256: "6915f91a40d6f66db21288144ca17e6122cea2e1eb2a7408ed280f09926eb6af" },
    ];
    expect(value.documents.items[0].excerpts.reduce((total, excerpt) => total + excerpt.text.length, 0)).toBeGreaterThan(480);
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed.documents!.items[0].excerpts).toHaveLength(2);
    const { container } = render(<CompanyParagraphs data={parsed} />);
    openDocuments();
    expect(container.querySelectorAll("blockquote")).toHaveLength(2);
    for (const excerpt of value.documents.items[0].excerpts) expect(screen.getByText(excerpt.text)).toBeVisible();
    const duplicated = structuredClone(value);
    duplicated.documents.items[0].excerpts[1] = { ...duplicated.documents.items[0].excerpts[0] };
    expect(parseCompanyAnswer(duplicated)!.documents!.items).toHaveLength(0);
  });

  it("正文不是可执行HTML，来源只使用核验后的文章链接", () => {
    const value = payload();
    value.documents.items[0].excerpts[0].text = '<img src=x onerror="alert(1)"><script>run()</script>';
    const { container } = render(<CompanyParagraphs data={parseCompanyAnswer(value)!} />);
    openDocuments();
    expect(screen.getByText(value.documents.items[0].excerpts[0].text)).toBeVisible();
    expect(container.querySelector("img,script")).toBeNull();
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });

  it("接受已核验新浪移动文章并拒绝扩大主机或PDF范围", () => {
    const value = payload(), mobile = "https://cj.sina.cn/articles/view/1234567/abcdef";
    value.documents.items[0].url = mobile;
    value.evidence[value.evidence.length - 1].url = mobile;
    expect(sourceUrl(mobile)).toBe(mobile);
    expect(parseCompanyAnswer(value)!.documents!.items).toHaveLength(1);
    for (const unsafe of ["https://cj.sina.cn.evil.test/articles/view/1234567/abcdef", "https://cj.sina.cn/other",
      "https://pdf.dfcfw.com/pdf/H2_AN123456789012_1.pdf", "javascript:alert(1)"]) {
      const changed = structuredClone(value);
      changed.documents.items[0].url = unsafe;
      changed.evidence[changed.evidence.length - 1].url = unsafe;
      expect(parseCompanyAnswer(changed)!.documents!.items).toHaveLength(0);
    }
  });

  it("接受同证券公告详情，而不把PDF附件当作公告网页正文", () => {
    const value = payload(), announcement = "https://data.eastmoney.com/notices/detail/000001/AN123456789012.html";
    Object.assign(value.evidence[value.evidence.length - 1], { kind: "announcements", source: "eastmoney_announcements", url: announcement });
    Object.assign(value.documents.items[0], { kind: "announcements", source: "eastmoney_announcements", url: announcement,
      extractor_version: "eastmoney-announcement-body-v1" });
    render(<CompanyParagraphs data={parseCompanyAnswer(value)!} />);
    openDocuments();
    expect(screen.getByRole("link", { name: "查看原文" })).toHaveAttribute("href", announcement);
  });

  it.each(["outside-company", "wrong-parent-company", "wrong-parent", "wrong-source", "wrong-kind", "wrong-url", "wrong-title"])("隐藏%s正文元数据，但不隐藏有效回答", fault => {
    const value = payload(), item = value.documents.items[0];
    if (fault === "outside-company") item.instrument_id = "600000.SH";
    if (fault === "wrong-parent-company") {
      value.securities.push({ instrument_id: "600000.SH", name: "浦发银行" });
      item.instrument_id = "600000.SH";
    }
    if (fault === "wrong-parent") item.parent_title_id = "not-supplied";
    if (fault === "wrong-source") item.source = "cninfo_announcements";
    if (fault === "wrong-kind") item.kind = "announcements";
    if (fault === "wrong-url") item.url = "https://finance.sina.com.cn/stock/doc-other.shtml";
    if (fault === "wrong-title") item.title = "另一家公司的标题";
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed).not.toBeNull();
    render(<CompanyParagraphs data={parsed} />);
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
    expect(screen.queryByText("正文片段")).not.toBeInTheDocument();
  });

  it.each(["items", "excerpts", "characters", "index", "digest", "metadata"])("有界拒绝%s，不因附件格式错误使回答消失", fault => {
    const value = payload(), item = value.documents.items[0];
    if (fault === "items") value.documents.items = Array.from({ length: 4 }, () => document());
    if (fault === "excerpts") item.excerpts = Array.from({ length: 3 }, (_, index) => ({ ...item.excerpts[0], paragraph_index: index + 1 }));
    if (fault === "characters") item.excerpts[0].text = "字".repeat(481);
    if (fault === "index") item.excerpts[0].paragraph_index = item.paragraph_count + 1;
    if (fault === "digest") item.excerpts[0].sha256 = "not-a-digest";
    if (fault === "metadata") item.extracted_characters = Number.MAX_SAFE_INTEGER;
    const parsed = parseCompanyAnswer(value)!;
    expect(parsed).not.toBeNull();
    render(<CompanyParagraphs data={parsed} />);
    expect(screen.queryByText("正文片段")).not.toBeInTheDocument();
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
  });

  it("未开启、旧记录和空读取状态不新增正文面板", () => {
    const value = payload();
    const { documents: omitted, ...old } = value;
    expect(omitted).toBeDefined();
    const { rerender } = render(<CompanyParagraphs data={parseCompanyAnswer(old)!} />);
    expect(screen.queryByText("正文片段")).not.toBeInTheDocument();
    rerender(<CompanyParagraphs data={parseCompanyAnswer({ ...old, documents: { version: "research-documents-v1", status: "skipped",
      code: "document_no_eligible_title", items: [] } })!} />);
    expect(screen.queryByText("正文片段")).not.toBeInTheDocument();
    expect(screen.getByText("研究回答保持可读。")).toBeVisible();
  });
});

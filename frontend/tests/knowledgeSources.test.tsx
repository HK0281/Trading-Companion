import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KnowledgeSources, knowledgeSourceUrl, parseKnowledgeReferences } from "../src/KnowledgeSources";
import { ResponseContent } from "../src/ResponseContent";
import { sourceUrl } from "../src/ProductResearch";
import { presentationReply } from "./fixtures";

function fixture() {
  const paragraphs = ["PE比较价格与盈利，PB比较价格与账面权益。", "两个指标应结合经营和资产口径理解。"];
  const citation = {
    id: "K1", snippet_id: "valuation_pe", title: "PE、市盈率与每股收益", excerpt: "PE通常是股价除以每股收益，分母口径会影响解释。",
    version: "trading-knowledge-2026-10-26-v1", maintained_on: "2026-10-26", content_origin: "maintainer_explanation",
    source_type: "official_reference", source_title: "Price-earnings (P/E) Ratio", publisher: "Investor.gov",
    url: "https://www.investor.gov/introduction-investing/investing-basics/glossary/price-earnings-pe-ratio",
    reference_version: "网页参考；未固定全文版本", live_verified: false, paragraph_indices: [0, 1],
  };
  const value = { version: "general-knowledge-answer-v1", origin: "model", status: "accepted", code: "general_knowledge_answered",
    text: paragraphs.join("\n\n"), paragraphs, citations: [citation], market_data_requested: false, trading_enabled: false,
    validation_scope: "format_scope_and_citation_membership_not_prose_semantics" };
  const turn = { ...presentationReply().result, answer: value.text, knowledge: value, report: null, parameters: null };
  return { turn, value, citation };
}

describe("local educational references", () => {
  it("keeps the answer once and deduplicates a note used by several paragraphs", () => {
    const { turn, value, citation } = fixture();
    const fetcher = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("No requests"));
    render(<ResponseContent turn={turn} />);
    for (const paragraph of value.paragraphs) expect(screen.getAllByText(paragraph)).toHaveLength(1);
    expect(screen.getByText("知识来源")).toBeVisible();
    expect(screen.getByText(citation.excerpt)).not.toBeVisible();
    fireEvent.click(screen.getByText("知识来源"));
    expect(screen.getAllByText(citation.excerpt)).toHaveLength(1);
    expect(screen.getByText(citation.excerpt)).toBeVisible();
    expect(screen.getByText("第1、2段参考")).toBeVisible();
    expect(screen.getByText(/本地版本 trading-knowledge-2026-10-26-v1 · 维护于 2026-10-26/)).toBeVisible();
    expect(screen.getByText(/说明由项目维护者整理/)).toBeVisible();
    expect(screen.getByRole("link", { name: "查看参考资料" })).toHaveAttribute("href", citation.url);
    expect(screen.getByRole("link", { name: "查看参考资料" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByText(/全文已核验|事实已验证/)).not.toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled(); fetcher.mockRestore();
  });

  it("supports multiple actual sources for one paragraph without repeating the answer", () => {
    const { turn, value, citation } = fixture();
    value.citations.push({ ...citation, id: "K2", snippet_id: "valuation_pb", title: "PB与资产质量", excerpt: "PB比较价格与每股净资产。", paragraph_indices: [0] });
    render(<ResponseContent turn={turn} />); fireEvent.click(screen.getByText("知识来源"));
    expect(screen.getByText("2 条本地说明")).toBeVisible();
    expect(screen.getByText("第1段参考")).toBeVisible();
    expect(screen.getByText("第1、2段参考")).toBeVisible();
    expect(screen.getAllByText(value.paragraphs[0])).toHaveLength(1);
  });

  it.each(["https://www.sse.com.cn/lawandrules/test", "https://edu.sse.com.cn/tib/qa/", "https://investor.szse.cn/knowledge/test",
    "https://www.sec.gov/investor/test", "https://www.investor.gov/test", "https://lautorite.qc.ca/en/test", "https://www.idx.id/en/test"])(
    "accepts an exact official reference host: %s", (url) => {
      expect(knowledgeSourceUrl({ url, source_type: "official_reference" })).toBe(url);
    });

  it("keeps project and official reference labels separate", () => {
    const { value, citation } = fixture();
    citation.source_type = "project_reference";
    citation.url = "https://github.com/HK0281/Trading-Companion/blob/main/docs/technical-analysis-design.md";
    render(<KnowledgeSources value={value} answer={value.text} />); fireEvent.click(screen.getByText("知识来源"));
    expect(screen.getByText(/项目参考资料：/)).toBeVisible();
    expect(screen.getByRole("link")).toHaveAttribute("href", citation.url);
    expect(knowledgeSourceUrl({ url: citation.url, source_type: "official_reference" })).toBeNull();
    expect(knowledgeSourceUrl({ url: "https://www.sec.gov/test", source_type: "project_reference" })).toBeNull();
  });

  it.each(["javascript:alert(1)", "http://www.sse.com.cn/test", "https://www.sse.com.cn.evil.test/test",
    "https://user:pass@www.sse.com.cn/test", "https://www.sse.com.cn:8080/test", "https://evil.test/",
    "https://github.com/evil/repo/", "https://finance.sina.com.cn/test", "https:\\www.sse.com.cn/test"])(
    "keeps source text but removes an unsafe or out-of-scope link: %s", (url) => {
      const { turn, citation } = fixture(); citation.url = url;
      render(<ResponseContent turn={turn} />); fireEvent.click(screen.getByText("知识来源"));
      expect(screen.getByText(citation.excerpt)).toBeVisible();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    });

  it("renders a source excerpt and title as inert text", () => {
    const { turn, citation } = fixture();
    citation.excerpt = '<img src=x onerror="alert(1)">'; citation.title = "<script>bad()</script>";
    const { container } = render(<ResponseContent turn={turn} />); fireEvent.click(screen.getByText("知识来源"));
    expect(screen.getByText(citation.excerpt)).toBeVisible(); expect(screen.getByText(citation.title)).toBeVisible();
    expect(container.querySelector("img, script, iframe")).toBeNull();
  });

  it.each([null, {}, [], { version: "wrong" }])("ignores malformed optional metadata and keeps confirmed answer: %j", (knowledge) => {
    const { turn, value } = fixture(); turn.knowledge = knowledge as typeof value;
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText(value.paragraphs[0])).toBeVisible();
    expect(screen.queryByText("知识来源")).not.toBeInTheDocument();
  });

  it("does not bind notes to a different displayed answer", () => {
    const { value } = fixture(); expect(parseKnowledgeReferences(value, "another answer")).toBeNull();
  });

  it.each([
    { id: "K9" }, { snippet_id: "bad id" }, { paragraph_indices: [-1] }, { paragraph_indices: [0, 0] },
    { paragraph_indices: [0] }, { paragraph_indices: [0, 2] }, { paragraph_indices: [false] },
    { maintained_on: "2026-02-30" }, { live_verified: true }, { source_type: { toString: null } },
    { content_origin: "official_quote" }, { excerpt: "x".repeat(501) },
  ])("rejects invalid citation association or provenance: %j", (changes) => {
    const { value } = fixture(); Object.assign(value.citations[0], changes);
    expect(parseKnowledgeReferences(value, value.text)).toBeNull();
  });

  it("rejects duplicate identifiers, while no-match answers simply omit the references", () => {
    const { value, citation } = fixture(); value.citations.push({ ...citation });
    expect(parseKnowledgeReferences(value, value.text)).toBeNull();
    value.citations = [];
    render(<KnowledgeSources value={value} answer={value.text} />);
    expect(screen.queryByText("知识来源")).not.toBeInTheDocument();
  });

  it("also permits official references used by company evidence without opening arbitrary GitHub paths", () => {
    expect(sourceUrl("https://www.szse.cn/lawrules/test")).toBe("https://www.szse.cn/lawrules/test");
    expect(sourceUrl("https://github.com/HK0281/Trading-Companion/blob/main/docs/test.md")).not.toBeNull();
    expect(sourceUrl("https://github.com/HK0281/Trading-Companion-evil/test")).toBeNull();
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CompanySources, parseCompanyAnswer, sourceUrl } from "../src/ProductResearch";
import { ResponseContent } from "../src/ResponseContent";
import { presentationReply } from "./fixtures";

function turn() {
  const result = presentationReply().result;
  result.company_research = {
    version: "product-research-answer-v1", origin: "model", status: "accepted", daily_cutoff: "2026-09-25",
    paragraphs: ["先看盈利和价格是否相互支持。", "营收增长是经营线索，仍需观察现金流。"],
    securities: [{ instrument_id: "000001.SZ", name: "平安银行" }],
    evidence: [{ id: "company-f1", instrument_id: "000001.SZ", kind: "financial", label: "营业收入", display: "100亿元",
      source: "eastmoney_financial", date: "2026-08-20", url: "https://data.eastmoney.com/finance/000001.html",
      content_kind: "fact", report_period: "2026-06-30", temporal_scope: "published_before_cutoff" }],
  };
  return result;
}

describe("company research conversations", () => {
  it("shows the direct company answer instead of legacy metric cards", () => {
    const data = turn();
    render(<ResponseContent turn={data} />);
    expect(screen.getByText("先看盈利和价格是否相互支持。")).toBeVisible();
    expect(screen.queryByText("关键读数")).not.toBeInTheDocument();
    expect(screen.queryByText(data.answer!)).not.toBeInTheDocument();
  });

  it("keeps references collapsed until requested, with report period and safe source link", () => {
    render(<CompanySources turn={turn()} />);
    expect(screen.getByText("营业收入：100亿元")).not.toBeVisible();
    fireEvent.click(screen.getByText("研究依据"));
    expect(screen.getByText("营业收入：100亿元")).toBeVisible();
    expect(screen.getByText("报告期：2026-06-30")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看来源" })).toHaveAttribute("rel", "noopener noreferrer");
  });

  it.each(["javascript:alert(1)", "http://data.eastmoney.com/a", "https://evil.test/a", "https://data.eastmoney.com.evil.test/a",
    "https://user:pass@data.eastmoney.com/a", "https://data.eastmoney.com:8080/a"])("does not link unsafe URL %s", (url) => {
    expect(sourceUrl(url)).toBeNull();
  });

  it("renders source content as text and disables an unsafe link", () => {
    const data = turn();
    const company = data.company_research as { evidence: { display: string; url: string }[] };
    company.evidence[0].display = '<img src=x onerror="alert(1)">';
    company.evidence[0].url = "javascript:alert(1)";
    const { container } = render(<CompanySources turn={data} />);
    fireEvent.click(screen.getByText("研究依据"));
    expect(screen.getByText(/img src=x/)).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("rejects evidence for a security outside the response scope", () => {
    const data = turn();
    const company = data.company_research as { evidence: { instrument_id: string }[] };
    company.evidence[0].instrument_id = "600000.SH";
    expect(parseCompanyAnswer(company)).toBeNull();
    render(<ResponseContent turn={data} />);
    expect(screen.getByText(/本轮研究回答未能正常显示/)).toBeVisible();
    expect(screen.queryByText(data.answer!)).not.toBeInTheDocument();
  });

  it("supports the old response contract when company research is absent", () => {
    const data = presentationReply().result;
    expect(parseCompanyAnswer(data.company_research)).toBeNull();
    render(<ResponseContent turn={data} />);
    expect(screen.getByText("关键读数")).toBeVisible();
  });
});

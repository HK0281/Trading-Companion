import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parseCompanyAnswer } from "../src/ProductResearch";
import { ResponseContent } from "../src/ResponseContent";
import { presentationReply } from "./fixtures";

function fixture() {
  const company = {
    version: "product-research-answer-v1", origin: "model", status: "accepted", daily_cutoff: "2026-09-25",
    paragraphs: ["盈利提供了经营线索，但还需要看现金流。", "价格位于均线之上，后续观察能否延续。", "新闻暂时无法获取。"],
    paragraph_evidence: [["income", "profit"], ["trend"], []],
    securities: [{ instrument_id: "000001.SZ", name: "平安银行" }],
    evidence: [
      { id: "income", instrument_id: "000001.SZ", kind: "financial", label: "营业收入", display: "100亿元",
        source: "eastmoney_financial", date: "2026-08-20", url: "https://data.eastmoney.com/finance/000001.html",
        content_kind: "fact", report_period: "2026-06-30", temporal_scope: "published_before_cutoff" },
      { id: "profit", instrument_id: "000001.SZ", kind: "financial", label: "归母净利润", display: "20亿元",
        source: "eastmoney_financial", date: "2026-08-20", url: "https://data.eastmoney.com/finance/000001.html",
        content_kind: "fact", report_period: "2026-06-30", temporal_scope: "published_before_cutoff" },
      { id: "trend", instrument_id: "000001.SZ", kind: "technical", label: "均线位置", display: "收盘价高于SMA(20)",
        source: "tencent", date: "2026-09-25", url: null,
        content_kind: "relation", report_period: null, temporal_scope: "historical_daily" },
      { id: "unreferenced", instrument_id: "000001.SZ", kind: "news", label: "未用于段落的新闻", display: "另一篇资料标题",
        source: "sina_news", date: "2026-09-24", url: "https://finance.sina.com.cn/stock/example.html",
        content_kind: "title", report_period: null, temporal_scope: "published_before_cutoff" },
    ],
  };
  const turn = { ...presentationReply().result, company_research: company };
  return { turn, company };
}

describe("paragraph-level research evidence", () => {
  it("keeps paragraphs readable without rendering their evidence or changing the payload", () => {
    const { turn } = fixture(), original = structuredClone(turn);
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText("盈利提供了经营线索，但还需要看现金流。")).toBeVisible();
    expect(screen.getByText("价格位于均线之上，后续观察能否延续。")).toBeVisible();
    expect(screen.queryByText(/^依据 \d/)).not.toBeInTheDocument();
    expect(screen.queryByText("营业收入：100亿元")).not.toBeInTheDocument();
    expect(screen.queryByText("均线位置：收盘价高于SMA(20)")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("第3段依据")).not.toBeInTheDocument();
    expect(screen.queryByText("另一篇资料标题")).not.toBeInTheDocument();
    expect(screen.queryByText("关键读数")).not.toBeInTheDocument();

    expect(screen.queryByRole("link", { name: "查看来源" })).not.toBeInTheDocument();
    expect(turn).toEqual(original);
  });

  it("preserves large evidence mappings without rendering their facts", () => {
    const { turn, company } = fixture();
    for (let index = 0; index < 6; index++) {
      const id = `fact-${index}`;
      company.evidence.push({ ...company.evidence[0], id, label: `指标${index}`, display: `${index}亿元` });
      company.paragraph_evidence[0].push(id);
    }
    render(<ResponseContent turn={turn} />);
    expect(parseCompanyAnswer(company)!.paragraph_evidence![0]).toHaveLength(8);
    expect(screen.queryByText("依据 8")).not.toBeInTheDocument();
    expect(screen.queryByText("指标5：5亿元")).not.toBeInTheDocument();
  });

  it("keeps weekly and derived signal data valid without source disclosures", () => {
    const { turn, company } = fixture();
    company.paragraphs = ["日线与周线方向有分歧，先观察价格能否获得量能支持。"];
    company.paragraph_evidence = [["weekly", "support", "alignment"]];
    company.evidence = [
      { ...company.evidence[2], id: "weekly", label: "周线方向", display: "已完成周线偏弱。", date: "2026-09-18", temporal_scope: "completed_week_derived" },
      { ...company.evidence[2], id: "support", label: "支撑候选", display: "历史局部低点聚集区。", temporal_scope: "historical_daily" },
      { ...company.evidence[2], id: "alignment", label: "日周方向", display: "日线偏强，与已完成周线方向不同。", temporal_scope: "cross_timeframe_derived" },
    ];
    expect(parseCompanyAnswer(company)).not.toBeNull();
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText(company.paragraphs[0])).toBeVisible();
    expect(screen.queryByText(/已完成周线偏弱/)).not.toBeInTheDocument();
    expect(screen.queryByText("依据 3")).not.toBeInTheDocument();
    expect(parseCompanyAnswer(company)!.evidence).toEqual(company.evidence);
    expect(screen.queryByText("参数与执行记录")).not.toBeInTheDocument();
  });

  it.each([undefined, []])("allows missing or empty mappings in older and fallback responses: %j", (mapping) => {
    const { turn, company } = fixture();
    if (mapping === undefined) delete (company as Partial<typeof company>).paragraph_evidence;
    else { company.origin = "application"; company.status = "fallback"; company.paragraph_evidence = mapping; }
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText("盈利提供了经营线索，但还需要看现金流。")).toBeVisible();
    expect(screen.queryByText(/^依据 \d/)).not.toBeInTheDocument();
    expect(screen.queryByText("研究依据")).not.toBeInTheDocument();
    expect(screen.queryByText("另一篇资料标题")).not.toBeInTheDocument();
  });

  it.each([null, {}, ["income"], [["income"]], [["missing"], ["trend"], []], [["income", "income"], ["trend"], []]])(
    "rejects invalid or unknown paragraph associations: %j", (mapping) => {
      const { turn, company } = fixture();
      (company as unknown as { paragraph_evidence: unknown }).paragraph_evidence = mapping;
      expect(parseCompanyAnswer(company)).toBeNull(); render(<ResponseContent turn={turn} />);
      expect(screen.getByText(/本轮研究回答未能正常显示/)).toBeVisible();
      expect(screen.queryByText(/^依据 \d/)).not.toBeInTheDocument();
    });

  it("rejects ambiguous duplicate evidence identifiers", () => {
    const { company } = fixture(); company.evidence.push({ ...company.evidence[0], display: "不同的内容" });
    expect(parseCompanyAnswer(company)).toBeNull();
  });

  it("preserves current observation and rule publication metadata without displaying it", () => {
    const { turn, company } = fixture();
    company.paragraphs = ["当前背景与适用规则应分别核对。"];
    company.paragraph_evidence = [["observed", "rule"]];
    company.evidence = [
      { ...company.evidence[0], id: "observed", kind: "profile", label: "上市信息", display: "交易所目录中的当前背景。",
        source: "sse", date: "2026-10-26", report_period: null, temporal_scope: "current_context", url: "https://www.sse.com.cn/test" },
      { ...company.evidence[0], id: "rule", kind: "technical", label: "规则", display: "当前版本化规则说明。",
        source: "application_market_rules", date: "2026-04-24", report_period: null,
        temporal_scope: "market_rule_explanation", url: "https://www.sse.com.cn/lawandrules/test" },
    ];
    render(<ResponseContent turn={turn} />);
    expect(screen.getByText(company.paragraphs[0])).toBeVisible();
    expect(screen.queryByText(/观测日 2026-10-26/)).not.toBeInTheDocument();
    expect(screen.queryByText(/规则发布日 2026-04-24/)).not.toBeInTheDocument();
    expect(parseCompanyAnswer(company)!.evidence).toEqual(company.evidence);
    expect(screen.queryByText(/发布时间 2026-10-26/)).not.toBeInTheDocument();
  });

  it.each(["javascript:alert(1)", "https://data.eastmoney.com.evil.test/source", "http://data.eastmoney.com/source", "https://unknown.example/source"])(
    "does not render hidden source HTML or links: %s", (url) => {
      const { turn, company } = fixture(); company.evidence[0].url = url; company.evidence[1].url = url;
      company.evidence[0].display = '<img src=x onerror="alert(1)">';
      const { container } = render(<ResponseContent turn={turn} />);
      expect(screen.queryByText(/img src=x/)).not.toBeInTheDocument(); expect(container.querySelector("img")).toBeNull();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    });
});

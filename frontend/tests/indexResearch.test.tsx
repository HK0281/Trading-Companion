import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PublicTurn } from "../src/api";
import { IndexResearch, parseIndexResearch } from "../src/IndexResearch";
import { ResponseContent } from "../src/ResponseContent";
import { ResearchChart } from "../src/ResearchChart";
import { parseResearchChart, parseIntervalChart } from "../src/charts";
import fixture from "./indexFixture.json";

const turn = fixture.turn as unknown as PublicTurn;
const daily = () => parseResearchChart(turn.chart, turn.parameters, turn.report?.request_id)!;

describe("独立指数研究", () => {
  it("显示默认基准、人工标记和正文，隐藏段落依据但保留数据", () => {
    expect(parseIndexResearch(turn.index_research, turn)).not.toBeNull();
    render(<IndexResearch turn={turn} />);
    expect(screen.getByText("本次以上证指数作为大盘观察基准。")).toBeVisible();
    expect(screen.getByText("人工指数样例，非真实行情")).toBeVisible();
    expect(screen.getByRole("heading", { name: /上证指数 · 暂不判断/ })).toBeVisible();
    expect(screen.queryByText(/依据：/)).not.toBeInTheDocument();
    const data = parseIndexResearch(turn.index_research, turn)!;
    for (const block of data.blocks) expect(screen.getByText(block.text)).toBeVisible();
    expect(data.blocks[0].evidence_ids.length).toBeGreaterThan(0);
    expect(data.evidence.length).toBeGreaterThan(0);
  });

  it("拒绝跨对象、编号、日期、来源、单位或段落引用污染", () => {
    const view = turn.index_research as Record<string, unknown>;
    for (const change of [
      { instrument_id: "000001.SZ" }, { name: "平安银行" }, { request_id: "other" },
      { price_unit: "CNY/share" }, { direction: "买入" }, { source: "akshare_eastmoney" },
      { latest_date: "2026-10-01" }, { benchmark_defaulted: "true" },
      { blocks: [{ title: "结论", text: "污染", evidence_ids: ["E999"] }, ...(view.blocks as unknown[]).slice(1)] },
      { evidence: (view.evidence as Record<string, unknown>[]).map((e, i) => i ? e : { ...e, unit: "CNY/share" }) },
      { evidence: (view.evidence as Record<string, unknown>[]).map((e, i) => i ? e : { ...e, instrument_id: "000300.SH" }) },
    ]) expect(parseIndexResearch({ ...view, ...change }, turn)).toBeNull();
  });

  it("用点显示日线，未知量不画成零量柱，键盘读数仍可用", () => {
    const { container } = render(<ResearchChart value={turn.chart} parameters={turn.parameters} reportRequestId={turn.report?.request_id} />);
    expect(daily()).not.toBeNull();
    expect(screen.getByText("点位 · 点")).toBeVisible();
    expect(screen.getByText("成交量单位未核实，暂不展示")).toBeVisible();
    expect(container.querySelector(".chart-volume")).toBeNull();
    const plot = screen.getByRole("group", { name: "互动日线图" });
    fireEvent.focus(plot); fireEvent.keyDown(plot, { key: "Home" });
    expect(screen.getByRole("status")).toHaveTextContent(/收盘 .* 点/);
    expect(screen.queryByText(/元\/股/)).not.toBeInTheDocument();
  });

  it("周线切换显示未完周，小时能力保持关闭", async () => {
    expect(parseIntervalChart(fixture.weekly, daily(), "1w")).not.toBeNull();
    const load = vi.fn().mockResolvedValue(fixture.weekly);
    const capabilities = { version: "research-chart-capabilities-v1", research_id: turn.report?.request_id, intervals: {
      "1w": { enabled: true, code: "ready" }, "60m": { enabled: false, code: "chart_hourly_source_unsupported" },
      "120m": { enabled: false, code: "chart_hourly_source_unsupported" },
    } };
    render(<ResearchChart value={turn.chart} parameters={turn.parameters} reportRequestId={turn.report?.request_id}
      capabilities={capabilities} loadInterval={load} />);
    expect(screen.getByRole("button", { name: "1小时" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "2小时" })).toBeDisabled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "周线" })));
    expect(screen.getByRole("group", { name: "互动周线图" })).toBeVisible();
    expect(screen.getByText(/本周未完/)).toBeVisible();
  });

  it("指数新版本拒绝股单位、数值量和股票版本冒用", () => {
    for (const change of [{ version: "research-chart-v1" }, { price_unit: "CNY/share" },
      { bars: daily().bars.map((b) => ({ ...b, volume: 0 })) },
      { series: daily().series.map((s) => ({ ...s, unit: "CNY/share" })) }]) {
      expect(parseResearchChart({ ...(turn.chart as object), ...change }, turn.parameters, turn.report?.request_id)).toBeNull();
    }
    expect(parseIntervalChart({ ...fixture.weekly, version: "research-chart-interval-v1" }, daily(), "1w")).toBeNull();
  });

  it("图表损坏不会抹掉已确认的指数正文", () => {
    render(<><ResponseContent turn={turn} /><ResearchChart value={{}} parameters={turn.parameters} reportRequestId={turn.report?.request_id} /></>);
    expect(screen.getByRole("heading", { name: /上证指数 · 暂不判断/ })).toBeVisible();
    expect(screen.getByText(/图表暂不可用或未通过范围校验/)).toBeVisible();
  });

  it("保留单段有效结论、低把握和部分移除提示", () => {
    const value = { ...(turn.index_research as Record<string, unknown>), direction: "偏空", conviction: "低", origin: "model",
      blocks: [{ title: "结论", text: "本轮技术观察偏空。", evidence_ids: ["E1"] }],
      quality: { version: "index-answer-quality-v1", generated_sentences: 4, retained_sentences: 1,
        deleted_sentences: 3, retained_paragraphs: 1, rejected_paragraphs: 2,
        partially_retained_sentences: 0, repair_attempts: 0, partial: true } };
    expect(parseIndexResearch(value, turn)).not.toBeNull();
    render(<IndexResearch turn={{ ...turn, index_research: value }} />);
    expect(screen.getByRole("heading", { name: "上证指数 · 偏空" })).toBeVisible();
    expect(screen.getByText(/把握低/)).toBeVisible();
    expect(screen.getByText("部分内容未通过核对已移除")).toBeVisible();
  });

  it("显示局限与合法原标题，但未分类标题不能代替结论", () => {
    const value = { ...(turn.index_research as Record<string, unknown>), quality: undefined,
      blocks: [{ title: "结论", text: "暂不判断。", evidence_ids: ["E1"] },
        { title: "局限", text: "本轮不含成交量、资金流、板块或宏观信息。", evidence_ids: ["E1"] },
        { title: "结构说明", text: "仅观察历史点位。", evidence_ids: ["E1"] }] };
    expect(parseIndexResearch(value, turn)).not.toBeNull();
    render(<IndexResearch turn={{ ...turn, index_research: value }} />);
    expect(screen.getByRole("heading", { name: "局限" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "结构说明" })).toBeVisible();
    expect(parseIndexResearch({ ...value, blocks: value.blocks.slice(1) }, turn)).toBeNull();
  });

  it("拒绝质量计数篡改和修复调用越界", () => {
    const quality = { version: "index-answer-quality-v1", generated_sentences: 2, retained_sentences: 1,
      deleted_sentences: 1, retained_paragraphs: 1, rejected_paragraphs: 1,
      partially_retained_sentences: 0, repair_attempts: 0, partial: true };
    const value = { ...(turn.index_research as Record<string, unknown>), origin: "model", quality,
      blocks: [{ title: "结论", text: "偏空。", evidence_ids: ["E1"] }] };
    for (const change of [{ retained_sentences: -1 }, { deleted_sentences: 0 }, { partial: false },
      { retained_paragraphs: 3 }, { repair_attempts: 2 }, { partially_retained_sentences: 2 }]) {
      expect(parseIndexResearch({ ...value, quality: { ...quality, ...change } }, turn)).toBeNull();
    }
  });
});

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PublicTurn } from "../src/api";
import { IndexResearch, parseIndexResearch } from "../src/IndexResearch";
import { parseIndexScenario } from "../src/IndexScenario";
import { ResearchChart } from "../src/ResearchChart";
import fixture from "./indexScenarioFixture.json";
import legacy from "./indexFixture.json";
import { reply } from "./fixtures";

type View = typeof fixture.views.short_bull;
const turn = (view: unknown): PublicTurn => ({ ...reply().result, ...(view as object) });
const copy = () => structuredClone(fixture.views.short_bull) as View;
const parse = (v: unknown) => { const t = turn(v), research = parseIndexResearch(t.index_research, t)!;
  expect(research).not.toBeNull(); return parseIndexScenario(research.scenario_plan, research); };

describe("指数独立点位情景卡片", () => {
  it.each(Object.entries(fixture.views))("接受程序生成的人工 %s 公开契约", (_name, v) => {
    expect(parse(v)).not.toBeNull();
  });
  it.each([
    ["short_bull", "97.00 点", "104.50 点", "107.50 点"],
    ["four_months", "95.00 点", "110.00 点", "115.00 点"],
    ["long_bull", "96.00 点", "108.00 点", "114.00 点"],
  ])("%s 与独立手算的止损和两档止盈一致", (name, stop, first, second) => {
    render(<IndexResearch turn={turn(fixture.views[name as keyof typeof fixture.views])} />);
    const table = screen.getByRole("table", { name: "指数点位参考" });
    for (const value of [stop, first, second]) expect(within(table).getByText(value)).toBeVisible();
    expect(screen.getByText("指数不可直接交易，点位为情景参考。")).toBeVisible();
    expect(screen.getByText("人工指数样例，非真实行情")).toBeVisible();
    expect(table).not.toHaveTextContent(/元|conditional_entry|holding_exit/);
  });
  it("明确4个月不改写为40交易日，完整周ATR与日收盘分别显示日期", () => {
    const { unmount } = render(<IndexResearch turn={turn(fixture.views.four_months)} />);
    expect(screen.getAllByText(/未来4个月/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/未来40个交易日/)).not.toBeInTheDocument();
    unmount(); render(<IndexResearch turn={turn(fixture.views.long_bull)} />);
    expect(screen.getByText(/完整周线.*ATR14/)).toBeVisible();
    expect(screen.getByText("收盘日期 2026-09-25 · 波动日期 2026-09-18")).toBeVisible();
  });
  it.each(["bear", "wait"] as const)("%s 只展示所选用途的字段，没有目标和入场", name => {
    render(<IndexResearch turn={turn(fixture.views[name])} />);
    const table = screen.getByRole("table", { name: "指数点位参考" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(table).not.toHaveTextContent(/止盈|入场|目标|实际盈亏比/);
    if (name === "bear") expect(screen.getByText(/假如已持有跟踪该指数的基金/)).toBeVisible();
    else expect(screen.getByText(/等待观察，触及区间边界/)).toBeVisible();
  });
  it.each(["unknown", "long_insufficient", "excluded_atr"] as const)("%s 显示具体原因，不补出点位", name => {
    render(<IndexResearch turn={turn(fixture.views[name])} />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText(`点位计划不可用：${fixture.views[name].index_research.scenario_plan.reason}`)).toBeVisible();
    expect(screen.getByRole("heading", { name: /上证指数 · / })).toBeVisible();
  });
  it("保存原始小数精度，实际盈亏比独立核对后最多显示两位", () => {
    render(<IndexResearch turn={turn(fixture.views.precision)} />);
    expect(screen.getByRole("cell", { name: "100.00000000000001 点" })).toBeVisible();
    expect(screen.getByRole("row", { name: /实际盈亏比1/ })).toHaveTextContent("1.5 倍");
    expect(screen.getByRole("row", { name: /实际盈亏比2/ })).toHaveTextContent("2.5 倍");
  });
  it("计划说明的删句提示独立于有效走势", () => {
    const v = copy();
    Object.assign(v.index_research.scenario_plan.quality, { generated_sentences: 2, deleted_sentences: 1, partial: true });
    expect(parse(v)).not.toBeNull();
    render(<IndexResearch turn={turn(v)} />);
    expect(screen.getByText("部分计划说明未通过核对已移除")).toBeVisible();
    expect(screen.queryByText("部分内容未通过核对已移除")).not.toBeInTheDocument();
  });
  it("已保存计划保留快照标记，不表示重新取数", () => {
    const v = copy(); Object.assign(v.index_research, { context: "saved", saved_at: "2026-09-26T00:00:00Z" });
    expect(parse(v)).not.toBeNull(); render(<IndexResearch turn={turn(v)} />);
    expect(screen.getByText("基于已保存快照，未重新取数")).toBeVisible();
    expect(screen.getByRole("table", { name: "指数点位参考" })).toBeVisible();
  });
  const corruptions: [string, (p: Record<string, any>) => void][] = [
    ["版本", p => p.version = "trade-plan-v2"], ["方法", p => p.method = "atr_reference_plan_v2"],
    ["股票对象", p => p.instrument_id = "000001.SZ"], ["研究编号", p => p.request_id = "other"],
    ["来源", p => p.source = "tencent"], ["真实性", p => p.data_authenticity = "provider_reported"],
    ["单位", p => p.price_unit = "CNY/share"], ["算法", p => p.atr_algorithm = "other"],
    ["期限", p => p.horizon = "未来40个交易日"], ["档位", p => p.horizon_profile = "medium"],
    ["周期", p => p.atr_timeframe = "weekly"], ["波动日期", p => p.atr_source_date = "2026-09-24"],
    ["收盘日期", p => p.source_date = "2026-09-24"], ["目标倍数", p => p.target_multiples = ["2", "3"]],
    ["目标倍数合并", p => p.target_multiples = ["1.5,2.5"]],
    ["用途", p => p.purpose = "holding_exit"], ["身份摘要", p => p.input_snapshot_sha256 = "f".repeat(64)],
    ["计划编号", p => p.plan_id = "other"], ["执行许可", p => p.execution_allowed = true],
    ["点位顺序", p => p.values.reference_stop = "101.00"], ["盈亏比", p => p.values.reward_risk_1 = "1.500001"],
    ["目标公式", p => p.values.target_1 = "104.51"], ["额外目标", p => p.values.extra_target = "200"],
    ["非字符串价位", p => p.values.reference_entry = 100], ["证据计划编号", p => p.evidence[0].plan_id = "other"],
    ["证据数值", p => p.evidence[0].raw_value = "100.01"], ["证据字段", p => p.evidence[0].field = "target_1"],
    ["证据单位", p => p.evidence[0].unit = "CNY/share"], ["证据日期", p => p.evidence[0].date = "2026-09-24"],
    ["证据对象", p => p.evidence[0].instrument_id = "000300.SH"], ["缺证据", p => p.evidence.pop()],
    ["事实冒充计划", p => p.blocks[0].evidence_ids = ["E1"]], ["未选用途引用", p => p.blocks[0].evidence_ids = ["S2_1"]],
    ["重复引用", p => p.blocks[0].evidence_ids.push(p.blocks[0].evidence_ids[0])],
    ["内部用途名", p => p.blocks[0].text = "holding_exit"], ["删句计数", p => p.quality.deleted_sentences = 1],
  ];
  it.each(corruptions)("%s 损坏只拒绝计划卡片，保留走势", (_name, mutate) => {
    const v = copy(); mutate(v.index_research.scenario_plan);
    expect(parse(v)).toBeNull(); render(<IndexResearch turn={turn(v)} />);
    expect(screen.getByRole("heading", { name: "上证指数 · 偏多" })).toBeVisible();
    expect(screen.getByText(/指数点位计划未通过显示校验/)).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
  it("无计划的旧结果正常显示，污染的新计划不删除图表", () => {
    const t = structuredClone(legacy.turn) as unknown as PublicTurn;
    const { unmount } = render(<IndexResearch turn={t} />);
    expect(screen.queryByLabelText("指数点位情景计划")).not.toBeInTheDocument(); unmount();
    (t.index_research as Record<string, unknown>).scenario_plan = {};
    render(<><IndexResearch turn={t} /><ResearchChart value={t.chart} parameters={t.parameters} reportRequestId={t.report?.request_id} /></>);
    expect(screen.getByText(/指数点位计划未通过显示校验/)).toBeVisible();
    expect(screen.getByRole("group", { name: "互动日线图" })).toBeVisible();
  });
});

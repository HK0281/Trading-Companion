import type { PublicTurn } from "./api";
import { parseConversation } from "./presentation";

export const workflowToolLabels = {
  analyze_daily_indicators: "历史行情与指标",
  get_indicator_knowledge: "指标知识",
  inspect_research_snapshot: "已有研究快照",
  analyze_additional_indicators: "补充指标分析",
} as const;

export const workflowStatusLabels = {
  executed: "已完成", rejected: "未执行", failed: "执行失败", limited: "部分可用",
} as const;

export const workflowStopLabels = {
  answered: "已完成本轮回答",
  data_unavailable: "可用数据不足，已结束研究",
  invalid_answer: "回答未通过检查，已结束研究",
  model_error: "模型未能完成本轮回答",
  tool_error: "工具执行遇到问题，已结束研究",
  iteration_limit: "已达到研究轮数上限",
  cancelled: "本轮研究已取消",
  budget_or_deadline: "已达到调用预算或时间上限",
  context_limit: "已达到本轮上下文上限",
} as const;

export interface ResearchWorkflow {
  version: "research-workflow-v1";
  request_id: string;
  instrument_id: string;
  mode: "bounded_read_only_research";
  steps: {
    tool: keyof typeof workflowToolLabels;
    status: keyof typeof workflowStatusLabels;
  }[];
  tool_calls_used: number;
  model_calls_attempted: number;
  analyses_used: number;
  limits: { tool_calls: 6; model_calls: 6; analyses: 2 };
  stop_reason: keyof typeof workflowStopLabels;
}

export interface WorkflowSupportingEvidence {
  request_id: string;
  instrument_id: string;
  trade_date: string;
  fact_id: string;
  text: string;
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const count = (value: unknown, maximum: number): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= maximum;
const key = <T extends object>(value: unknown, labels: T): value is keyof T =>
  typeof value === "string" && Object.prototype.hasOwnProperty.call(labels, value);

/** Keep optional tool records separate from the accepted answer and raw model messages. */
export function parseResearchWorkflow(turn: PublicTurn): ResearchWorkflow | null {
  const value = turn.research_workflow;
  if (!record(value) || value.version !== "research-workflow-v1"
    || value.mode !== "bounded_read_only_research"
    || !turn.parameters || !turn.report?.request_id
    || value.request_id !== turn.report.request_id || value.instrument_id !== turn.parameters.instrument_id
    || !Array.isArray(value.steps) || value.steps.length > 6
    || !value.steps.every((step) => record(step) && key(step.tool, workflowToolLabels)
      && key(step.status, workflowStatusLabels)
      && typeof step.code === "string" && step.code.length > 0 && step.code.length <= 120)
    || !record(value.limits) || value.limits.tool_calls !== 6
    || value.limits.model_calls !== 6 || value.limits.analyses !== 2
    || !count(value.tool_calls_used, 6) || !count(value.model_calls_attempted, 6)
    || !count(value.analyses_used, 2) || !key(value.stop_reason, workflowStopLabels)) return null;

  // Project known display fields only; error codes and debug extras never reach the view.
  return {
    version: "research-workflow-v1", mode: "bounded_read_only_research",
    request_id: turn.report.request_id, instrument_id: turn.parameters.instrument_id,
    steps: value.steps.map((step) => ({ tool: step.tool, status: step.status })),
    tool_calls_used: value.tool_calls_used, model_calls_attempted: value.model_calls_attempted,
    analyses_used: value.analyses_used, limits: { tool_calls: 6, model_calls: 6, analyses: 2 },
    stop_reason: value.stop_reason,
  };
}

/** Scope binding protects the optional view; it is not a semantic proof of the text. */
export function parseWorkflowSupportingEvidence(turn: PublicTurn): WorkflowSupportingEvidence[] {
  const value = turn.research_workflow;
  const conversation = parseConversation(turn);
  if (!parseResearchWorkflow(turn) || !record(value) || !conversation || conversation.origin !== "model"
    || !Array.isArray(value.supporting_evidence) || value.supporting_evidence.length > 8) return [];
  const supplements = conversation.evidence_snapshots?.filter((snapshot) => snapshot.role === "supplement") ?? [];
  if (!supplements.length || !value.supporting_evidence.every((item) => record(item)
    && typeof item.text === "string" && item.text.length > 0 && item.text.length <= 2000
    && typeof item.fact_id === "string" && item.fact_id.length <= 2000
    && supplements.some((snapshot) => item.request_id === snapshot.request_id
      && item.instrument_id === snapshot.instrument_id && item.trade_date === snapshot.trade_date
      && (item.fact_id as string).startsWith(`${snapshot.request_id}|${snapshot.instrument_id}|${snapshot.trade_date}|`)
      && (item.fact_id as string).length > `${snapshot.request_id}|${snapshot.instrument_id}|${snapshot.trade_date}|`.length))
    || new Set(value.supporting_evidence.map((item) => item.fact_id)).size !== value.supporting_evidence.length) return [];
  return value.supporting_evidence.map((item) => ({
    request_id: item.request_id, instrument_id: item.instrument_id, trade_date: item.trade_date,
    fact_id: item.fact_id, text: item.text,
  }));
}

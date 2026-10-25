import type { PublicTurn } from "./api";
import { parseResearchWorkflow, parseWorkflowSupportingEvidence, workflowStatusLabels, workflowStopLabels, workflowToolLabels } from "./workflowProjection";

export function ResearchWorkflow({ turn }: { turn: PublicTurn }) {
  if (turn.research_workflow == null) return null;
  const workflow = parseResearchWorkflow(turn);
  if (!workflow) return <p className="workflow-note">研究过程记录暂不可用；本次没有重新调用模型。</p>;
  const supportingEvidence = parseWorkflowSupportingEvidence(turn);
  return <section className="research-workflow" aria-label="研究过程">
    <h4>研究过程</h4>
    {workflow.steps.length > 0 && <ol>{workflow.steps.map((step, index) => <li key={index}>
      <span>{workflowToolLabels[step.tool]}</span><span>{workflowStatusLabels[step.status]}</span>
    </li>)}</ol>}
    <p>{workflowStopLabels[workflow.stop_reason]}</p>
    <p className="workflow-counts">工具调用 {workflow.tool_calls_used} / {workflow.limits.tool_calls} 次 · 模型尝试 {workflow.model_calls_attempted} / {workflow.limits.model_calls} 次 · 行情分析 {workflow.analyses_used} / {workflow.limits.analyses} 次</p>
    {supportingEvidence.length > 0 && <section className="workflow-evidence" aria-label="已核对的补充指标关系">
      <h5>已核对的补充指标关系</h5>
      <ul>{supportingEvidence.map((item) => <li key={item.fact_id}><p>{item.text}</p><code>{item.fact_id}</code></li>)}</ul>
    </section>}
  </section>;
}

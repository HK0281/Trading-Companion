# 决策回答接入的路由与行为记录

2026-10-29，基线 b714276，实施方案 #149/#150。本次是未提交工作区候选，等待 Claude 审核；不是干净提交捕获。

原v1/v2语料、观察器和目标辅助层均保留。原35场景/105轮全部继续匹配v2完整观察，预期差异为0；这同时检验每轮后的会话、累计调用和缓存，而不只是回答文字。特别是“上市后第一个交易日以来的走势”仍按历史日期规则处理，不误升级为未来决策。

新增[request-routing-v3.json](../tests/fixtures/request-routing-v3.json)引用v2的LF规范摘要，增加4场景、12轮明确决策输入。人工目标先写在[routing_decision_support.py](../tests/routing_decision_support.py)，经真实服务入口检查后才捕获完整观察的投影。大字段保留摘要和字节长度，规则与原观察器一致；正常测试不会重录。

| 场景 | 轮次及人工目标 | 与旧v2关系 |
| --- | --- | --- |
| decision-direct-and-history | 直接决策v2，随后无模型历史查看 | 新增，无旧对应轮次 |
| decision-paraphrases | 原基本面v1后，三种省略对象的决策追问转v2 | 新增，无旧对应轮次 |
| decision-identity-boundary | 无对象澄清；具名决策；新问题缺对象澄清 | 新增，无旧对应轮次 |
| decision-domain-limits | 只看新闻不取日线；禁ATR不造价；禁计划不显示价位 | 新增，无旧对应轮次 |

由于旧105轮逐字段无变化，没有需列出的旧字段改前/改后值。新增12轮分别记录完整投影，调用、取数、保存状态和后续连带结果都纳入比较，不隐藏意外变化或数组重排。

审核意见 #152 后更新本次未提交的 v3 候选：ATR 展示固定四位小数、收盘参考补齐两位小数，相应计划证据与公开回答的完整观察摘要随之更新。人工路由目标、调用和取数次数保持不变；旧 v1/v2 及其105轮观察未改动。展示精度不参与价位计算，具体约定见[计划设计](research-decision-plan-design.md)。

额外真实入口和专项用例覆盖过期结果、多公司歧义、执行拒绝、来源与快照身份、取消、修正次数、历史重放、计划用途和公式。独立的40场景/59步工作流评估仍按原目标回归，不拿本次人工演示代表真实模型正确率。

候选生成显式开启，拒绝覆盖已有输出：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m tests.routing_decision_support --capture --output .agentbus/tmp/request-routing-v3-candidate.json
```

测试命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_routing_baseline tests.test_decision_routing_baseline tests.test_decision_routing_api
```

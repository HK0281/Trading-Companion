# A2 对象识别与历史路由差异记录

2026-10-29，基线 `858a063`，方案 #175、批准 #176。本记录随未提交工作区交审；Codex不创建提交。A2在B完整上下文交接之前实施。

原v1～v4文件、观察器与人工目标均保留，不重新录制。[A2独立清单](../tests/fixtures/request-routing-a2-deltas.json)引用v4的LF规范SHA-256摘要，并记录两份变更源码的v3历史摘要、当前精确摘要及原因。原35场景105轮和决策4场景12轮共117轮逐字段比较，**所有117轮差异为0**；清单逐轮 `fields=[]`，没有需列出的改前/改后值。测试仍使用原完整观察投影，不只比较路由标签。

`product_agent.py` 的缺对象前置检查与 `product_plan.py` 的完整性检查共享 `research_instruments.py`：扫描和查询采用同一名称规范化，保留原始位置；仅补用户原文中目录可唯一解析的对象；末尾评价谓语按语法消歧。D02的“贵州茅台……值得买吗”不再误加“值得买”股票，D12“五粮液”可匹配目录“五 粮 液”。这两个真实目录特征未包含在旧人工基线，独立[专项测试](../tests/test_research_instruments.py)覆盖全部18道具名题、模型遗漏、真实“值得买”股票、比较、空格/组合字符、歧义和冲突代码；D05/D20保持B依赖。

2a的304题语料、manifest及逐题复核状态保持原样。其manifest也冻结了上述两份源码；`intent_router_task.SOURCE_REVISIONS` 只登记相同的两个历史/当前摘要对，其他未知变更仍拒绝。新意图评估计划另列实际 `actual_rule_source_sha256_lf`，兼容性核对包含实际摘要，历史来源不冒充当前源码；实现摘要变化也使旧阈值锁失效。冻结记录变动的边界有逐文件未知摘要拒绝测试。

验证：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_routing_baseline tests.test_decision_routing_baseline tests.test_research_instruments tests.test_intent_router_task
```

本批数字匹配仅增加诊断，不调整容差、单位、角色、日期或窗口规则；评估接收版本及局部理由变化另见[回答评估设计](decision-answer-evaluation-design.md)。离线机械通过不代表真实模型质量。

一次性离线对照还检查了基线 `858a063` 的8640组数字核对与768组舍入判断，结果差异为0；304题逐条比较旧/新名称扫描下的完整规则分类及保护分支输出，差异为0。规则适配器调用的四个受影响模块辅助函数抽象语法树（去掉排版的代码结构）保持一致。脚本及结果留在被忽略的 `.agentbus/tmp/a2_*compare.py` 和对应JSON中，不依赖Git运行永久测试。

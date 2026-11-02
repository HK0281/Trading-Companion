# 研究意图 v2.6 候选实现

本候选依据[公开护栏规范](research-intent-v2.6-guard-principles.md)和[分批方案](research-intent-v2.6-plan.md)，实现第三批独立意图理解和公开评估，正式聊天仍使用既有路由。协议为 `research-intent-v2.6`，护栏版本为 `intent-v2-guards-v10`，`eligible_for_switch=false`。

模型负责排除类别、任务操作、产品角色和结构化指代；程序检查原文、通用否定方向、可信身份范围以及价位授权。不再用目标词表证明收窄，不从最近研究继承期限、目标或价位，不把模型澄清恢复为研究。`subject.reference` 必填，旧协议不补字段。范围为最近研究、保存名单或唯一有效待办，程序只按合法范围与整数索引选择，不再重解中文序号或 selection 文本。

本地前置仅识别规范 D 的完整命令、历史、执行/注入拦截、有效待办的完整窄期限、固定方向/评级回顾及完整指标定义。带附句、无效待办和其他语义输入交给模型。产品拒绝只核对声明对象片段，不因背景词覆盖股票研究。所有路径只给意图提案，不执行新研究或价位计算。

冻结 JSON 及其绑定的公开文件保留出题时的字节和历史状态；当前实现记录于本文和新评估工件，不回签旧冻结、旧锁或历史成绩。首次自动收件附带的 #436 仅为封存完成汇总，#439 确认不影响隔离；实现会话不读取新封存材料、出题线程、作者日志、总转录或审核方私有笔记。

#442 批准共享传输的格式修复白名单仅增加 v2.6 精确单版本，其他工具、多版本和未开启诊断仍拒绝，默认不开启。旧 v2.5 检验程序只调整一条拒绝提示为冻结测试要求的 `Locked file mismatch` 类别，判断和顺序不变；该程序自身也在旧锁绑定范围中，源码变化仍导致真实旧锁拒绝，旧一次性记录不变。旧机制测试继续使用独立合成快照，不重开真实检验。

公开入口仅接受 `dev`、`regression`。默认只输出计划及状态构建预检，没有付费调用、目标预测或评分：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B evaluation_tools/preflight_research_intents_v26.py --split dev --plan-only
.\.venv\Scripts\python.exe -X utf8 -B evaluation_tools/preflight_research_intents_v26.py --split regression --plan-only
.\.venv\Scripts\python.exe -X utf8 -B evaluation_tools/evaluate_research_intents_v26.py --split dev --plan-only
.\.venv\Scripts\python.exe -X utf8 -B evaluation_tools/evaluate_research_intents_v26.py --split regression --plan-only
```

主门槛为开发至少 61/64、回归至少 392/412；回归完整 414 题另作参考，V2002 的旧构建缺口留在 412 分母，2 题仅按事前批准记录单列不适用。仅应用已批准的 60 题/76 字段叠加，不修改原题。保留原样/规范化/批准标注三列和 14 个评分字段，reference、绑定和授权另作诊断；失败、无效和未测不缩分母。还须共同可观测字段不退步及可测红线零违规。

Claude 在 #439 单独批准首轮付费策略：开发最多 80 次，回归最多 520 次，合计 600 次，额度不互借；每题最多两次，只在首次 `tool_arguments_json_invalid` 时允许一次同消息格式重问。首次有限修复允许 control_char、code_fence、trailing_text、unquoted_scalar，重问只允许 unquoted_scalar。模型为 deepseek-flash、temperature=0、thinking 关闭，每次输出上限 1500，每题两次共享 45 秒和既有已观察用量限制。未知输入用量及费用明确记 unknown；请求预留、失败原文、格式修复和每次实际调用分开持久化。

真实运行由 Claude 在代码审核、提交推送及干净版本上执行，使用同一公开入口的 `--live`；预算可调小。占用固定在 `.agentbus/evaluation-runs/research-intent-v2.6/public/<split>`，不因提交号变化而重置额度。源码漂移、日志持久化失败或中断使运行无效，不重开该版本同 split 的占用，不遗留通过报告。缺少相关指代/价位授权诊断时记未测，不满足对应门槛。当前没有 v2.6 封存运行入口；公开门槛全部通过后，第四批再审核候选锁和一次性检验。正式接入及 G 的规则回退属于第五批，尚未实现或测量。

第三批实现已通过离线验证，待 Claude 审核工作区差异。2026-11-01 执行 `.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python`，退出 0：3140 项 unittest、244 项 pytest 函数测试、4 个 Agent 场景、90 次 MVP 重复、5 个公司流程、API 演练和依赖检查均通过，没有 skip 或 xfail。新增候选 15 项、新评估 12 项、共享传输 28 项、旧 v2.5 检验 25 项亦分别通过；追踪诊断仍逐项说明，没有按前缀豁免。

开发预检 64/64 可构建；回归 414 总数、412 适用、411 可构建，V2002 继续留分母，2 题按事前记录不适用。开发/回归预检和计划检查的目标调用、预测行、评分行均为零，付费调用为零，冻结及全部绑定公开文件校验通过。完整结果和日志路径随 #437 线程审核请求交付。

初版完整验证曾出现 129 failures、69 errors，主要是新协议夹具、已撤销的旧语义修正断言、传输版本限制及拒绝错误类别；#442 批准后已逐项迁移和修复。三个合成响应构造器只为新测试输入增加 reference，不为真实旧模型响应补字段；旧题库、gold、锁、冻结及历史成绩不改。

按 #440，README 由 Claude 维护，本批进展仅记录于本文。公开真实模型及新封存成绩尚未产生，不能将合成模型测试或旧版成绩当成本版准确率；最终离线验证结果另随审核请求提供。

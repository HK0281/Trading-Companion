# 意图换说法评估：第二步2a

本步只交付独立评估工具，不改变产品路由、聊天行为或启动参数。第一步研判和ATR参考计划保持原样。当前题库仍为待Claude逐条复核的草稿；仅运行过模拟传输，没有本任务的真实模型效果数字。达到门槛也只支持提出第二步2b，接入产品仍须用户同意。

入口：[评估命令](../scripts/evaluate_intent_router.py)；数据：[304条人工题](../data/evaluation/intent-paraphrase-v1.jsonl)及[复核清单文件](../data/evaluation/intent-paraphrase-v1.manifest.json)。manifest 是记录数据摘要、划分和逐题复核状态的文件。

## 标签与输入边界

分类器只负责选择语义意图，没有执行工具、修改会话或增加权限的能力。七个业务值直接取现有 `IntentKind`（产品路由类别），另加仅用于实验的 `clarify`（需要反问）：

| 标签 | 判定标准 |
| --- | --- |
| indicator_knowledge | 技术指标定义、公式和一般用法，不分析特定证券当前情况。 |
| general_knowledge | 金融、交易制度或市场风险等一般概念，不针对具体证券研究。 |
| screening | 发起新的股票候选寻找。 |
| screening_edit | 有筛选上下文，修改原条件、排序或数量。 |
| screening_followup | 研究已有名单的成员或序号，不修改筛选条件。 |
| company_research | 公司业务、财务、新闻和综合研究；买卖评级、未来方向、止盈止损等决策与计划统一归此类，最近是技术研究也不改变。名称歧义交身份澄清。 |
| technical_research | 明确的个股指标、历史行情，或接续技术研究本身的解释、修改、重做；不负责买卖评级、未来方向或计划。 |
| clarify | 完全没有对象且没有可接续上下文，或多对象中指代不唯一；名称的身份歧义不属于本标签。 |

定义和中文说明只有[一份来源](../src/trading_companion/intent_router_task.py)，同时生成Jev的固定选项和DeepSeek的JSON提示。DeepSeek只能返回一个 `intent` 字段；不要求它自报概率。Jev复用既有协议，保留供应商置信度和概率；非法标签、重复键、额外决定字段或格式错误记失败，不修正、不重试。

模型仅收到 `{"question": "人工问题原文", "context": {...}}`。上下文字段如下：

| 字段 | 含义 |
| --- | --- |
| has_saved_company / has_saved_technical | 是否保存了公司／技术研究记录。 |
| has_shortlist | 是否有筛选名单，不发送名单内容。 |
| company_enabled / workflow_enabled | 可信配置中对应功能是否开启；关闭时入口先保护。 |
| has_multiple_research_subjects | 最近研究是否涉及多个对象，不发送对象和数量。 |
| latest_valid_research_kind | 最近可接续类别：none/company/technical/screening/unknown；unknown表示无法确认，不猜测。 |

不发送题号、标签、理由、gold（人工标准答案）、划分、规则答案、聊天历史、证券资料或目录。最近类别由人工场景预先指定，用来验证同时保存公司和技术记录时省略主语的归属；本步尚未在产品中实现这个状态派生器。

命令、历史、新问题前缀、公司待澄清、技术待澄清、指标待澄清、多待澄清冲突、执行或注入请求共8组保护题，每组8条。保护记录不进入语义模型或语义准确率分母。入口复用现有命令常量、执行检查等，并接受人工声明的待澄清归属。这里证明的是**独立评估入口**的零调用边界，产品原有边界由现有回归测试保护，不声称本步新增了生产保护层。

## 题库与复核

240条语义题：8类各30条，每类10个家庭、每家庭3条输入案例；每类开发18条、留出12条，总计开发144、留出96。家庭指同一个语义场景的说法和上下文对照，整组进入一个划分，避免近义句同时出现在开发与留出中。稳定性对照会使用同句、不同最近类别，不能把这些案例称为彼此独立的自然表达。

按审核#159修正决策标签后，公司／技术共享家庭只保留“解释原研究”和“重做原研究”两个，因此共78个语义家庭。18条最近类别为technical的决策／方向题统一改为公司研究，理由记录对象可以经可信交接沿用；跨执行器交接尚属2b，不在产品中实施。

24个配对、48条消融题在重新验证之前写定于manifest的 `ablation_design`，开发和留出各12对／24条。其中6对的标签随最近类别变化（开发解释3对、留出重做3对），18对标签应保持不变（开发9对概念题、留出8对概念题及1对纯历史行情）。稳定性题检查元信息不应干扰明确请求，不能当成“最近类别有用”的证据；报告按两组分别给出完整／去字段准确率和双端正确率。旧24对选择已废弃，不按任何模型结果挑配对，也不在消融里重新选策略。

为维持各类18／12的配额，原公司研究槽位补入明确的日线、EMA、成交量、布林带、MACD、周线等技术题和无对象的财报反问题；I229–I231“平安”三题改为公司研究并整家庭移至开发，I058–I060历史行情家庭整组移至留出。clarify另补无对象的比较家庭，移除旧题中直接说明“最近类别未知”的元语言。每条变化均按人工语义与配额确定，未参考规则或模型输出。

所有语义题均带 `paraphrase`（换说法）标签，因此本版本换说法子集等于语义全集；它不是另一个独立证据集。另有60条近似反例，留出24条，逐题列出禁止误触发的类别。覆盖口语、换词、省略对象、长句和少量错别字。

先写题再运行规则，不按得分挑题或修改冻结规则。#159要求的全量自然表达自查去掉了题目中主动描述隐藏状态、排除其他标签、提示标准答案的说法；状态只放context，理由只放标注字段。但人工作者和规则实现者相同，仍可能有偏差。题库为合成数据，不代表真实请求分布。Claude逐条检查问题、输入是否足够、标准答案、理由和禁止类别；歧义须交用户确认，不能照模型答案改标签。

复核时在manifest每条 `reviews` 中填写 `status=approved`、`reviewer=claude` 和说明，保留与题目匹配的 `row_sha256`；全部通过后再设置顶层 `annotation_status=reviewed`、`reviewer=claude`、`reviewed_at=YYYY-MM-DD`。修改题目必须重新计算摘要并重新审核。仅翻转顶层标记不能通过检查。数据、定义、家庭划分和冻结生产文件均校验摘要。

## 冻结规则与公平口径

A2（#175/#176）因统一原文对象识别修改了 `product_agent.py` 和 `product_plan.py`。原304题及manifest不改写；仅登记这两份源码的精确历史/当前摘要对，仍拒绝未知变更。新计划公开实际源码摘要，参考报告兼容检查也核对它；历史摘要继续标明题库编写时的来源。117轮零差异与独立名称测试见[A2记录](request-routing-a2-deltas.md)。这不是2a标签或阈值调整，也未接入模型路由。

[基线适配器](../src/trading_companion/intent_router_baseline.py)调用真实的知识提前分支和 `route_research_request`，固定人工证券目录、合法筛选状态和时钟，不另写简化关键词分类器，不读行情、不调用模型、不修改生产源文件。

模型输入包含本步拟评估的最近类别，冻结的生产路由尚不支持该字段，因此不把它强塞进既有路由状态。规则只接收目前支持的状态；消融去掉最近类别时，规则预测不变。

**语义路由不等于最终执行结果。** 例如无上下文“能买吗”，真实路由可能选择公司研究，执行器随后安全询问对象。规则没有通用clarify，所以按真实返回值计分，不替规则编造反问，也不把标签不符说成最终回答一定错误。

总表同时展示全部8类与 `gold != clarify` 的7类子集准确率和macro-F1，避免只利用规则缺少clarify取得优势。gold=clarify单列正确反问、缺信息强行路由，以及规则执行器能否继续澄清（本实验未运行执行器，写“未判定”）。“追踪一下消息”可属于公司研究，“上市以来走势”是纯技术历史分析；本实验不能证明company内部decision/analysis目标判断的改进。

“平安怎么样”已明确是在研究公司，究竟是平安银行还是中国平安交现有执行器的身份澄清，不要求拿不到证券目录的语义模型消歧。clarify仅处理对象完全缺失或多对象指代不明。未来方向、买卖评级和止盈止损计划始终交公司研究；最近技术上下文只可提供可信对象，不把决策变成技术研究的解释。

## 开发、锁定、留出与消融

先跑开发集选择Jev采用阈值；候选为0.50、0.55…0.95和0.99。采用条件为合法非clarify且置信度不低于阈值。开发候选要求：准确率至少95%、采用至少40条、覆盖至少40%、Wilson 95%区间下界至少85%。在符合条件者中取覆盖最大者，同覆盖取较高阈值；没有合格候选则锁定null，含义为不直接采用。

只能从完整144题、每题一次、包含Jev且模型结果合法的开发报告导出锁。锁包含开发报告路径与摘要、选择规则、阈值和请求／返回模型身份，初始为draft。Claude审查开发结果后把锁设为 `review_status=approved`、`reviewer=claude`。正式留出运行必须提供该锁，且题库、实现、定义、价格和所选模型组合与开发一致。开发报告不能移动后不更新引用；任何修改都会使锁失效。offline来源的锁不能用于live。

留出仅输出锁定阈值的选择性指标，不扫描留出最优阈值。离线允许无锁诊断，采用阈值为null且永远不能满足切换门槛。预算截断保留全部题目和not_run，报告partial，不能导出有效开发锁。

消融必须引用同划分、同条件、完整且每题一次的主报告；只运行预选24条，删除最近类别字段，其他输入不变。不自动补主实验，不额外选择配对，不再调阈值。报告完整／去字段准确率、差值、反问率和一对两端都答对的比例；返回模型身份不一致时不计算改善差值。live留出消融同样需要已复核锁。

## 指标与费用

[指标实现](../src/trading_companion/intent_router_metrics.py)保留每个比率的分子、分母和值，空分母为null：

- 准确率＝正确条数／全部计划题数；每类准确率实际为该类召回率，另给precision、recall、F1。F1＝2TP/(2TP+FP+FN)，macro-F1固定8类等权平均；非clarify子集固定7类。混淆矩阵另设failed和not_run列。
- 误路由率＝业务题被判成其他业务类别的数／N；反问率＝合法clarify输出／N。正确反问、过度反问及gold=clarify时强行选业务分列，失败或未运行不伪装为反问。
- ECE为置信度校准误差：固定10个等宽桶，按桶内有效样本数加权平均“平均把握与实际正确率之差”。给有效覆盖M/N，另给非clarify输出口径。DeepSeek不虚构置信度。
- 选择性准确率＝阈值以上非clarify采用中正确数／采用数，附覆盖、样本数和Wilson 95%区间。家庭内说法及稳定性对照有关联，区间仅为案例比例参考，不代表独立真实请求的置信范围，也不能据此宣称统计显著。
- 近似反例误触发率只检查该题预先列出的禁止类别，另给反例总体错误率。
- 延迟用最近秩p50/p95，分成功、失败、超时、全部已尝试口径，列出样本数。未尝试不补零毫秒。
- 级联是用保存结果模拟：Jev达到锁定阈值则采用，否则反问或使用DeepSeek已有结果。不给模拟追加请求，未运行的必需结果保留缺失；分别统计准确率、误路由、反问、强行路由、反例误触发、后续需求和费用。串行耗时是已需请求耗时之和，不能当作部署链路延迟或实际节省费用。

仅把新闻指标中的 `ratio` 和 `percentiles` 两个纯函数抽入 [共用模块](../src/trading_companion/classification_metrics.py)，新闻仍使用相同函数、同一统计口径。传输、DeepSeek解析、费用、网络限制均复用既有实现。

请求用量乘显式价格，保留请求和返回模型身份。失败、未知用量、价格不适用或模型版本无法匹配时，费用仍未知，不补零；不同币种不相加。预览可给输入／输出token（供应商的文本计量单位）假设，产生费用情景估算，不是实际账单或硬费用上限。运行调用上限是请求次数的硬约束。

## 执行与复现

PowerShell项目根目录运行。下面均不读取密钥或联网：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --annotation-review
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --plan-only
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --split dev --report-dir .agentbus/tmp/intent-dev
```

用输出 `artifacts` 对象中的 `json` 路径定位开发report.json并导出锁，再跑模拟留出；路径中的RUN_ID替换成实际生成目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --lock-from-report .agentbus/tmp/intent-dev/RUN_ID/report.json --lock-output .agentbus/tmp/intent-dev-lock.json
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --split holdout --threshold-lock .agentbus/tmp/intent-dev-lock.json --report-dir .agentbus/tmp/intent-holdout
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --suite context-ablation --reference-report .agentbus/tmp/intent-dev/RUN_ID/report.json --report-dir .agentbus/tmp/intent-ablation-dev
```

留出消融用 `--split holdout`、留出主报告的 `--reference-report` 和同一 `--threshold-lock`。报告输出到新的随机目录，不覆盖旧报告或锁文件，附完整JSON、Markdown总表、304题复核清单。

真实计划预览也不读取密钥或发请求：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_intent_router.py --mode live --classifier jev --classifier deepseek --deepseek-model deepseek-flash --call-limit 288 --prices data/evaluation/model-prices.json --assumed-input-tokens 1000 --assumed-output-tokens 100 --plan-only
```

预览分开发144×K、留出96×K、开发消融24×K、留出消融24×K和旧输入上界164×K列请求与各家估价，K为选择的分类器数。两家、单次的总上界904，其中已实现主线与消融576，旧套件328尚未实现。实际运行仅执行所选一个套件／划分，不一次运行全部。

**真实运行另经用户授权，由Claude执行。** 本轮不执行。live要求显式分类器、调用上限、价格，选DeepSeek再显式模型。先打印计划，再检查题库和锁，最后只读取所选提供方密钥；禁止重定向、其他提供方和行情连接，无重试。超时默认5秒，可选1～15秒；复用现有单请求限制，DNS和慢分块不保证绝对墙钟上限。offline清理提供方环境并禁止网络，假响应仅由输入散列生成，不读取gold。

## 预定门槛与未完成项

完整、已复核的真实留出集才可判断是否支持2b提案：

1. 全部和非clarify两种口径的准确率、macro-F1均不低于规则；预先标记换说法题在两种口径上准确率均至少高5个百分点。
2. 反例误触发、gold=clarify时强行路由均不高于规则。
3. 锁定阈值采用的准确率≥95%、n≥40、覆盖≥40%、Wilson 95%下界≥85%。
4. Jev已尝试请求p95≤1500ms、成功≥99%；级联另列完整性与串行p95≤3000ms，缺失不能通过。
5. 题库、模型身份、费用用量和运行均完整，锁已复核，开发／留出返回模型身份一致；保护64题模型调用为0。

全部通过也不自动改变产品，必须另提2b并获用户同意。未证明提升时保留原门槛，不按结果降低标准。

旧59步／105轮输入的**模型与规则一致率**按 #157 允许后置：需通过观察包装从真实离线会话派生状态，不能简单把现有期望标签当人工gold。为保持本步主线可审核，本版显式返回 `intent_legacy_deferred`，不生成假一致率，也不将该项标完成。原有产品105轮回归测试照常执行。

# 研究意图 v2.5：事前出题、隔离与冻结计划

依据：agentbus #389 完整方案、Claude #390 批准、#392 审核补充及 #394 最终批准。用户已选择修补后重新出题。本批只准备题目、统一标注登记和前置状态预检；正式聊天入口及 v2.4 理解协议、提示词保持原状。

当前状态：**原 64 道开发题和 96 道检验题已由 Claude 在 #392 逐题批准；新增 14 道公开开发题、4 个前置状态及 120 条规则覆盖已获 #394 批准，共 78 道开发题和 96 道检验题。题库、规范和门槛已冻结，未运行 v2.5 目标理解或评分。** 对外记录及冻结时间见 [冻结记录](../data/evaluation/research-intent-v2.5-freeze.json)。检验文件、96 道题及 gold 完全不变，SHA256 仍为 `99a5e163cb7840a1f9e135a68578da8ff096185c3efc3fae33262944debc5a6e`。原 160 题保留 #392 审核记录，新增 14 题登记 #394；未用 finalize 脚本覆盖审核结果。整个出题目录尚待 Claude 移到仓库外封存，公开文件待 Claude 提交、推送，完成后才能安排全新会话实现。

## 三个实施批次

1. 本批：独立 Codex 出题会话编写 78 道公开开发题和 96 道检验题、全部 14 字段 gold（标准答案）、原文依据和理由；登记旧题已批准修订；只构建前置状态；Claude 逐题审核后封存并提交、推送。
2. 修补批：Claude 用 `--new-session` 唤醒一个全新的实现会话。只使用 78 道新开发题、原 240 道已暴露回归题和公开规范。实现、生产回归、自测、审核及提交完成后，由 Claude 另行执行有调用上限的付费开发/回归评估。233 道开发题 = 原 155 + 新 78；旧留出 85 道全部称已暴露回归。
3. 一次性检验批：锁定最终候选及运行器，再绑定代码、提示词、模型参数和运行器指纹。Claude 在干净提交上先只读预检，再执行一次检验。不重试、不续跑、不换目录补跑；达标只允许申请正式接入和端到端验收，不自动切换产品。

各批 Codex 只改文件、自测及发审核请求。Claude 审核通过后提交清单内文件并推送，通知同步完成才开始下一批。

## 出题隔离与如实披露

- 统一表述：**检验题由独立 Codex 会话起草、Claude 审核；实现会话在调参期间不可见。** 这不是独立人类盲测，也不提供防篡改或作者完全遗忘的证明。
- 出题会话 ID 为 `01a19543-2291-7490-a1db-5e6080d437fe`，记入冻结记录；不得再承担第二批实现。
- 检验题只写在 Git 忽略的 `.agentbus/sealed/research-intent-v2.5-holdout.jsonl`。逐题审核包、详细构建结果及作者脚本也只放同一隔离目录。提交文件中不出现检验题干、gold、逐题场景或逐题反馈。
- Claude 审完后把整个出题材料包移至仓库外保存（例如 `C:\Users\james\Desktop\TC-sealed\`），保留精确文件字节。仓库仅保留检验文件 SHA256（用于核对文件是否改变的校验值）、题数、族和类别计数、门槛及审核摘要。
- 第二批会话不读取封存目录、出题材料或出题会话的 `.agentbus/logs/` 日志；每次 review-request 都声明未读。日志限制同样适用于后续排查和搜索。
- 第三批运行器只接受显式封存路径，先核对精确字节 SHA256 再解析或运行。运行结束后由 Claude 将检验文件连同结果提交以便核对。
- 初始“实现期间未用于调参”子集为全部 96 题；作者见过另列。若实现期接触目标或逐题反馈，立即登记暴露并重新审定试验，不能恢复未见资格或在成绩出来后删分母求通过。

## 统一语义口径及修补边界

以下是已批准的 v2.5 目标规范；本批尚未改变理解程序。

- 个股未来方向/评级和购买价值为 `decision`；指数纯未来方向为 `outlook`，指数条件点位情景为 `decision`。价位授权 `wants_plan` 单独表达。个股未来观察期限仍保留未来目标，否决计划仅撤销价位。
- `analysis` 是历史或现状观察；本地拒绝、失效待办澄清中的 `analysis` 是中性占位，不代表旧研究目标或新的研究授权。无效待办不借原问题继承目标、价位和对象来源。
- 明确日线、周线、K线、技术面和指标属于 `technical`；日线不是指标，成交量是明确指标。**指数明确请求的技术领域也保留**；只有隐含默认领域不填。修补批撤销 `index_default_domain`（当前会一律删除指数 technical 的规范化），同步提示词。
- `strict_scope` 只限定**研究领域或指标**。仅限定时间范围取 false；被否定的“只看/只用”取 false，不把它误当领域或指标排除，仍保留实际肯定请求。V25D072 是时间限定，V25D073 是否定唯一指标限制。当前护栏只按词形命中的问题留给第二批修补。
- “公司情况”“业务情况”“主营业务”属于 `profile`（公司概况）；单说“某公司的情况”不自动限定领域。仅“走势/表现/趋势”也不自动限定技术面。
- “这 N 天/周/月”与“近 N 天/周/月”采用相对自然日窗口，月按 N×30 天、周按 N×7 天，截止参考日前一天；“这三个月”即最近 90 天。必须是完整表达，不将“本月”“自然季度”“这三个月以来”套入该规则。生产日期解析默认行为不变，候选显式启用扩展。
- 日期原文提取与是否可执行分开：非法日历日期、倒置、过长窗口、当日未完成日线保留原文并澄清。索取未来已实现事实不能改成可执行情景；未来相对事实如 V2198 不产生合法历史窗口或未来观察期限。方向预测和条件参考情景仍可研究。
- “删掉/去掉/不再分析/移除”等为通用否定；完整否定片段、领域/指标映射、长句尾部和肯否冲突须保留。“不要只看”否定唯一范围，不等于排除该领域。明确计划/价位/止损否决撤销统一计划授权。
- 具体 ETF、基金、期货和期权研究拒绝，不能绑定其跟踪指数。概念问法可解释；作为公司背景的产品名不能误拒绝实际股票研究。原 V2161–V2164 保留一次语义调用契约；已知不支持指数走本地零意图调用。
- `history` 历史命令复用生产路由优先级和规范化，本地处理，只有通过已有检查的保存对象才可引用；有无结果都不调用目标理解。纯解释只在现有整句本地语法与真实有效缓存均满足时零调用，其他解释仍用一次语义理解。
- 原生待办有 15 分钟有效期，899 秒有效，900/901 秒失效；未来创建时间、来源/模式/目录身份变化同样失效。严格整句期限回复本地澄清，空绑定、不继承旧授权；附句不能被吞掉；完整独立新任务不能被失效待办永久阻断。
- 多待办冲突显式标为 `multi_pending`（在真实对象上注入两个原生待办）。普通继续/序号直接本地澄清冲突；取消、历史命令优先；完整独立新任务可绕过旧待办。
- “同一只/同一只股票”先按完整最近单对象指代核验，不能被“一只”误作序号。多对象、身份失效、未来发布时间和无记录不猜对象；真实序号仍由既有选择器核验。
- 身份歧义与任务分类分离：股票任务可确定时保留 `stock_research`，空绑定并 `clarify_identity`。V2156–V2160 不改标注，只修程序。

理解行为变化仅属于 v2.5 候选。若提取共享路由纯函数，只允许生产默认行为等价，并跑原生产回归。不接入行情执行器，不改会话，不增加普通肯定研究分类词表，不改变模型和调用上限。

## 旧题修订登记

[v2.5 修订文件](../data/evaluation/research-intent-v2.5-amendments.json) 叠加 #390 批准的 24 题、39 字段变更：每项绑定原始行指纹、字段前后值、理由、审批人 `claude` 和消息 390。同题允许多字段，重复字段拒绝；必须匹配原 240 题指纹及 #366 修订文件指纹。新版本读取器独立，旧读取器继续保持只读开发、三个字段、每题单项的历史限制。

| 题号 | 已批准变更 |
| --- | --- |
| V2013、V2017、V2020、V2151、V2152、V2154、V2155、V2196 | research_kinds → [technical] |
| V2153 | research_kinds → [technical]；indicator_mentions → [成交量] |
| V2165 | operation → local；objective_kind → analysis；zero_intent_calls → true |
| V2199 | research_kinds → [technical]；date_expression → 过去十年 |
| V2200 | research_kinds → [technical]；date_expression → 今天 |
| V2216–V2220 | zero_intent_calls → true |
| V2221–V2225 | objective_kind → analysis；wants_plan → false；subject_source → none |
| V2226、V2229 | research_kinds → [profile] |

#366 的 V2020 均线、V2089 technical、V2038 decision 三项继续有效。原题库、manifest（逐题审核及指纹记录）、旧修订、旧基线、v2.4 锁与报告都不覆盖。V2222/V2223 的会话/轮次归属在架构中不存在，继续登记不适用；V2002 的旧搭建缺口保留。旧检验失败、零调用违规及用量也不因重新标注追认通过。新修订只提供评分副本，不进入模型输入。

## 78 / 96 题分配与变体分组

沿用原 31 个 D 族和 17 个 H 族作为能力标签，每族两侧至少一题。#390 原 64/96 的分配保留，#392 再向开发侧补 14 题形成 78/96，检验侧不增删、不改题。最终分配如下，原 240 题不重新划分。

| 能力族 | 新开发 | 新检验 |
| --- | ---: | ---: |
| H01 历史窗口 | 2 | 4 |
| H02 歧义身份 | 3 | 4 |
| H03 不支持产品 | 4 | 8 |
| H05 增加指标/指代 | 3 | 4 |
| H06 移除领域 | 4 | 6 |
| H10 非法日期 | 3 | 6 |
| H14 待办冲突 | 3 | 4 |
| H15 失效待办 | 8 | 6 |
| H16 原故障族的正常语义 | 2 | 4 |
| D01、D08，各族 | 各1 | 各3 |
| D09 | 2 | 3 |
| D12、D20、D31，各族 | 各1 | 各2 |
| D16 | 3 | 2 |
| D19 | 2 | 2 |
| D23、D25、H07，各族 | 各2 | 各1 |
| 其余28族，各族 | 各1 | 各1 |
| 合计 | 78 | 96 |

新增公开示例：V25D065/066 指数有效及900秒到期待办，V25D067 冲突中独立新任务，V25D068 过期后独立新任务，V25D069 目录身份改变，V25D070 未知股票名，V25D071 筛选刷新，V25D072/073 两条严格范围规则。逐条规则检查又补 V25D074 未支持指标、V25D075 严格领域全部被否定、V25D076 指数保存结果历史命令、V25D077 待办模式切换、V25D078 关闭记忆。后五项也保持新措辞和不同前置组合；没有调整原160题来迁就新覆盖检查。

能力族两侧出现；同一变体组不可跨侧。组描述任务形式、会话前置及对象/状态组合，相近改写和仅换参数的边界网格放在一侧。有限词表的命令可以用于新的状态组合，但不能复制完整旧输入。机械检查同时拦截重复完整输入，以及去掉对象、日期、期限、指标参数和标点后的两侧同形题。它不能证明语义独立，Claude 仍须逐题核对组边界；不宣称机械检查可以识别所有同义改写。

横向覆盖采用 `coverage`（正例、反例、边界的代表题标签）：否定、历史命令、明确技术领域、歧义身份、产品范围、有效/失效待办、冲突待办、同一对象、未来事实，九项在两侧各具三种代表。不是逐题全量标签，也不是精确率或统计总体抽样。

H16 只评价正常语言理解。格式错、低把握、矛盾、超时、不可用另以固定替身做协议故障验收，不计入语言准确率，不靠预期失败提高得分。

<!-- v25-rule-coverage:start -->
## 检验所用规则的公开示例

按 #392 对全部96道检验题的14字段和会话分支生成120个规则标签，并逐项找到公开开发题或已暴露回归题作为示例。对照的可校验数据见[规则覆盖登记](../data/evaluation/research-intent-v2.5-rule-coverage.json)。逐检验题的标签、原行指纹只在隔离包中保存，公开表只列标签和公开题号。

字段规则包含任务、操作、目标、授权、对象来源及数量、期限/日期、指标、排除项、领域、严格范围、动作和零调用；会话分支另列身份、范围、日期、待办和本地优先级。标签按规则分解，不把每个字段组合单独当作规则。此检查可发现标签缺少公开示例；Claude 已在 #394 接受标签粒度并批准公开对照，变体分组与跨侧同形检查继续单独执行。V2002、V2222、V2223这些已知不可测/不适用描述不作公开见证。

| 规则标签 | 公开开发或已暴露回归示例 |
| --- | --- |
| 最近多对象单数指代不得猜选（`ambiguous_previous_subject`） | V25D012、V2056、V2057 |
| 歧义股票名保留任务并澄清身份（`ambiguous_stock_identity`） | V25D034 |
| 大盘默认上证指数（`benchmark_default`） | V25D002、V2006、V2008 |
| 对象绑定数量：multiple（`binding:multiple`） | V25D040、V25D068、V25D075 |
| 对象绑定数量：none（`binding:none`） | V25D005、V25D010、V25D012 |
| 对象绑定数量：single（`binding:single`） | V25D001、V25D002、V25D003 |
| 取消或清空命令优先于待办（`command_before_pending`） | V25D028 |
| 完整新任务绕过待办冲突（`conflict_independent_new`） | V25D067 |
| 已消耗最近身份不从旧缓存回流（`consumed_subject`） | V25D054、V2206、V2207 |
| 日周K线是领域而非指标（`daily_weekly_not_indicator`） | V25D003、V25D018、V25D032 |
| 历史日期原文：absent（`date_expression:absent`） | V25D001、V25D002、V25D005 |
| 历史日期原文：present（`date_expression:present`） | V25D003、V25D004、V25D018 |
| 非法或未完成历史窗口保留原文并澄清（`date_unexecutable`） | V25D052、V2196、V2197 |
| 方向评级与价位授权分开（`direction_without_prices`） | V25D006、V25D007、V25D009 |
| 严格领域全被否定须澄清（`empty_strict_domain_conflict`） | V25D075 |
| 明确排除：financial（`exclusion:financial`） | V25D075、V2177 |
| 明确排除：indicator:atr（`exclusion:indicator:atr`） | V25D017、V25D044、V2081 |
| 明确排除：indicator:ema（`exclusion:indicator:ema`） | V25D067 |
| 明确排除：news（`exclusion:news`） | V25D045、V2176、V2189 |
| 明确排除：plan（`exclusion:plan`） | V25D007、V25D008、V25D009 |
| 明确排除：profile（`exclusion:profile`） | V25D043、V2179 |
| 明确排除：technical（`exclusion:technical`） | V25D068 |
| 排除项：absent（`exclusions:absent`） | V25D001、V25D002、V25D003 |
| 排除项：present（`exclusions:present`） | V25D007、V25D008、V25D009 |
| 完整新任务绕过过期待办（`expired_independent_new`） | V25D068 |
| 其他解释使用一次语义理解（`explain_semantic`） | V2214 |
| 多个明确对象可比较（`explicit_comparison`） | V25D040、V25D068、V2166 |
| 未来实际事实不得伪装成条件情景（`future_actual_unavailable`） | V25D050、V2198 |
| 未来发布时间不得授权绑定（`future_publication`） | V25D055、V2211、V2212 |
| 历史命令优先于待办冲突（`history_before_conflict`） | V25D057 |
| 历史命令引用合法已存指数结果（`history_saved_index`） | V25D076 |
| 无保存结果的历史命令仍本地处理（`history_without_result`） | V25D057 |
| 未来期限：absent（`horizon:absent`） | V25D001、V25D002、V25D003 |
| 未来期限：present（`horizon:present`） | V25D010、V25D014、V25D016 |
| 未来期限区间按统一中点口径（`horizon_interval`） | V25D073 |
| 非法量化未来期限不得当省略（`horizon_invalid`） | V25D053、V2201、V2202 |
| 短中长档映射代表期限（`horizon_named_band`） | V25D017、V25D046、V25D065 |
| 指数明确技术领域保留（`index_explicit_technical`） | V25D003、V25D015、V25D035 |
| 失效或到期边界的指数期限待办本地澄清（`index_horizon_local_invalid`） | V25D066 |
| 有效指数期限待办零调用续接（`index_horizon_local_valid`） | V25D065 |
| 明确支持指标：boll（`indicator:boll`） | V25D073 |
| 明确支持指标：ema（`indicator:ema`） | V25D016、V25D020、V25D041 |
| 明确支持指标：macd（`indicator:macd`） | V2173 |
| 明确支持指标：sma（`indicator:sma`） | V25D015、V2020、V2076 |
| 明确支持指标：volume（`indicator:volume`） | V25D032、V2153 |
| 正向指标：absent（`indicator_mentions:absent`） | V25D001、V25D002、V25D003 |
| 正向指标：present（`indicator_mentions:present`） | V25D015、V25D016、V25D020 |
| 知识插曲不改变最近身份（`knowledge_interlude`） | V25D014、V2066、V2067 |
| 已知不支持指数本地拒绝（`known_index_local_reject`） | V2165 |
| 关闭记忆不沿用对象（`memory_off_no_binding`） | V25D078 |
| 已有对象要价位先确认期限（`missing_horizon_for_prices`） | V25D001、V25D011、V25D031 |
| 缺对象优先澄清对象（`missing_subject_before_horizon`） | V25D010、V2046、V2047 |
| 模式改变阻断旧身份（`mode_mismatch`） | V25D022、V2106、V2107 |
| 目标：analysis（`objective_kind:analysis`） | V25D002、V25D003、V25D004 |
| 目标：decision（`objective_kind:decision`） | V25D001、V25D006、V25D007 |
| 目标：outlook（`objective_kind:outlook`） | V2009、V2014 |
| 操作：explain（`operation:explain`） | V25D026、V2126、V2127 |
| 操作：local（`operation:local`） | V25D005、V25D028、V25D029 |
| 操作：new（`operation:new`） | V25D001、V25D002、V25D003 |
| 操作：refresh（`operation:refresh`） | V25D027、V25D071、V2054 |
| 操作：update（`operation:update`） | V25D019、V25D020、V25D025 |
| 序号在当前对象或名单中核验（`ordinal_selection`） | V25D024、V25D042、V2116 |
| 预计动作：clarify_conditions（`outcome:clarify_conditions`） | V25D050、V25D052、V25D053 |
| 预计动作：clarify_conflict（`outcome:clarify_conflict`） | V25D009、V25D056、V25D075 |
| 预计动作：clarify_horizon（`outcome:clarify_horizon`） | V25D001、V25D011、V25D031 |
| 预计动作：clarify_identity（`outcome:clarify_identity`） | V25D034、V25D035、V25D070 |
| 预计动作：clarify_subject（`outcome:clarify_subject`） | V25D010、V25D012、V25D021 |
| 预计动作：local（`outcome:local`） | V25D005、V25D028、V25D030 |
| 预计动作：reject（`outcome:reject`） | V25D029、V25D036、V25D037 |
| 预计动作：research（`outcome:research`） | V25D002、V25D003、V25D004 |
| 明确指标参数完整保留（`parameterized_indicator`） | V25D016、V25D020、V25D041 |
| 时钟回拨使待办失效（`pending_clock_rollback`） | V2224 |
| 普通冲突待办本地澄清（`pending_conflict_local`） | V25D056、V2216、V2217 |
| 目录变化使待办身份失效（`pending_directory_changed`） | V25D069 |
| 模式变化使待办失效（`pending_mode_changed`） | V25D077 |
| 待办回复附句必须理解完整条件（`pending_suffix_semantic`） | V25D020、V2096、V2097 |
| 肯定价位与否决计划冲突（`plan_conflict`） | V25D009、V2041、V2042 |
| 产品只作股票背景不误拒绝（`product_background`） | V25D039 |
| 产品概念解释可以回答（`product_concept`） | V25D038 |
| 公司或业务情况明确为概况（`profile_explicit_situation`） | V25D013、V25D034、V25D062 |
| 购买价值默认包含参考价位（`purchase_value_prices`） | V25D017、V25D031、V25D036 |
| 这N天周月为相对历史窗口（`relative_this_window`） | V25D033、V2152、V2155 |
| 明确领域：announcements（`research_kind:announcements`） | V25D045、V25D048、V25D078 |
| 明确领域：financial（`research_kind:financial`） | V25D012、V25D027、V25D043 |
| 明确领域：news（`research_kind:news`） | V25D021、V25D037、V25D048 |
| 明确领域：profile（`research_kind:profile`） | V25D013、V25D024、V25D034 |
| 明确领域：technical（`research_kind:technical`） | V25D003、V25D015、V25D016 |
| 明确研究领域：absent（`research_kinds:absent`） | V25D001、V25D002、V25D004 |
| 明确研究领域：present（`research_kinds:present`） | V25D003、V25D012、V25D013 |
| 最近唯一同一对象可指代（`same_single_subject`） | V25D041、V2173 |
| 按旧条件刷新筛选（`screening_refresh`） | V25D071 |
| 泛称某公司情况不限定领域（`situation_unspecified_domain`） | V25D063、V2055、V2133 |
| 来源改变阻断旧身份（`source_mismatch`） | V25D021、V2101、V2102 |
| 失效公司期限待办零调用澄清并清除授权（`stock_horizon_local_invalid`） | V25D059、V25D060、V25D061 |
| 有效公司期限待办零调用续接（`stock_horizon_local_valid`） | V25D019、V25D058、V2091 |
| 被否定的唯一范围不设严格范围（`strict_negated_only_false`） | V25D073 |
| 严格领域或指标范围：false（`strict_scope:false`） | V25D001、V25D002、V25D003 |
| 严格领域或指标范围：true（`strict_scope:true`） | V25D016、V25D043、V25D048 |
| 仅限时间不设严格领域（`strict_time_only_false`） | V25D072 |
| 对象来源：none（`subject_source:none`） | V25D005、V25D010、V25D023 |
| 对象来源：previous（`subject_source:previous`） | V25D001、V25D011、V25D012 |
| 对象来源：question（`subject_source:question`） | V25D002、V25D003、V25D004 |
| 任务：clarify（`task:clarify`） | V25D010、V25D012、V25D021 |
| 任务：command（`task:command`） | V25D028、V2136、V2137 |
| 任务：history（`task:history`） | V25D047、V25D057、V25D076 |
| 任务：index_research（`task:index_research`） | V25D002、V25D003、V25D004 |
| 任务：knowledge（`task:knowledge`） | V25D030、V25D038、V2146 |
| 任务：recall（`task:recall`） | V25D005、V2021、V2022 |
| 任务：screening（`task:screening`） | V25D025、V25D049、V25D071 |
| 任务：stock_research（`task:stock_research`） | V25D001、V25D006、V25D007 |
| 任务：unsupported（`task:unsupported`） | V25D029、V25D036、V25D037 |
| 不明确指数身份须澄清（`uncertain_index_identity`） | V25D035 |
| 未知股票名保留任务并澄清身份（`unknown_stock_identity`） | V25D070 |
| 不支持指标保留原文并澄清（`unsupported_indicator`） | V25D074 |
| 具体不支持产品研究拒绝（`unsupported_product`） | V25D036、V25D037、V2161 |
| 计划否决保留未来方向（`veto_preserves_direction`） | V25D007、V25D009、V2031 |
| 价位授权：false（`wants_plan:false`） | V25D002、V25D003、V25D004 |
| 价位授权：true（`wants_plan:true`） | V25D001、V25D010、V25D011 |
| 零理解调用：false（`zero_intent_calls:false`） | V25D001、V25D002、V25D003 |
| 零理解调用：true（`zero_intent_calls:true`） | V25D005、V25D019、V25D026 |

<!-- v25-rule-coverage:end -->
## 中性状态构建和只构建预检

[新辅助模块](../evaluation_tools/research_intent_v25_cases.py) 复用旧题严格解码、14 字段验证和真实程序状态构建，不改变原 240 题固定大小、整族划分契约。工具放在独立的 `evaluation_tools/`：v2.4 已冻结 `src/`、`scripts/` 的完整源码清单，本批不改变这两处原文件、不放宽旧锁，也不将新评估工具打包为产品代码。

公开文件指纹先把 CRLF 换行为 LF，再计算 SHA256，以兼容 Git 在 Windows 检出时转换换行；封存文件始终校验精确原字节，不做换行规范化。

- [中性状态库](../data/evaluation/research-intent-v2.5-setups.json) 不含任何目标题干或 gold。公司缓存、公司期限待办复用 #341/#366 已审核固定前置脚本；原前置保留 #392 审核记录，新添科创50期限/保存结果、筛选与公司并存、关闭记忆共四个前置组合已由 #394 批准。逐项审核人及批准消息在状态库的审核记录中可查。
- 公司/技术/指数对象快照用真实目录及原生研究身份对象表示；快照不冒充已经生成完整答案。名单用真实 `SavedScreening` 对象。需要显示或解释保存结果的题必须构建真实保存缓存。
- 指数期限待办由真实指数执行器在模型调用前创建；已保存指数由真实执行器、固定演示响应及合成日线形成。模型替身调用只计前置，不计目标理解或语言成绩。
- 时间故障只调整真实对象时间；来源、模式和目录更新在真实待办生成后施加。冲突显式记录双槽注入。不存在的 session_matches/turn_matches=false 不纳入新 96 题。
- 预检用中性替身问题构建，输入只含 question/context/setup_id；gold、证据、题号、划分和变体组不进入构建器。逐题记录描述符/模型上下文指纹、前置替身调用及故障元数据。网络、凭据和目标理解/评分入口均被禁止。
- 96 题全可构建才允许冻结，无新题不适用排除。出题预检的详细记录保存在隔离包中，公开仅汇总；后续实现会话不执行封存预检。

公开开发预检：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B evaluation_tools/preflight_research_intents_v25.py --plan-only
```

检验预检只供 Claude/出题会话执行，通过同一脚本显式传 `--split holdout --corpus <仓库外封存文件> --expected-sha256 <冻结记录值> --plan-only`。此脚本没有 live 或评分选项。最终一次性运行器属于第三批，不在本批冒称已经实现。

## 冻结门槛

- 全部 96 题，14 字段联合全对至少 **92** 题。
- 实现期间未用于调参子集至少 **95%**；初始也是 96 题。该列与“作者未见”不是一回事。
- 语义调用子集 S 在出题时按 `zero_intent_calls=false` 固定，联合全对至少 `ceil(0.95×S)`。本地简单题不能抬高语义门槛。
- 检验题数 n≥4 的族，至少 `ceil(0.8×n)` 全对；n<4 的族不设逐族门槛，仅计总分，报告仍列各族分数。删除“单题族必须全对”。
- 同一输入及有效标注下，规则可共同观测字段不能退步；规则不能输出的字段不虚构基线。可实测零调用、对象、否决、严格范围等红线零违规。跨对象来源及端到端未实现检查仍列未测。
- 三列成绩并列：原样+原标注、规范化+原标注、规范化+批准标注。第一列仍通过当前通用核验，不是旧版本原样重演；主门槛用第三列。新题最初即统一标注，三列的标注相同。
- 失败、未测、输出无效保留分母；不以实际成功测到的题当分母。门槛随题库冻结后不再调整。
- 最多 96 次目标理解尝试，每题一次；本地题零尝试。无重试，前置替身调用另列。模型维持 deepseek-flash、temperature=0、thinking=disabled、1500 输出 token 上限；每次付费计划先列调用和费用上限，由 Claude 执行。

这 96 题可以检验所覆盖的构造场景，不能证明真实用户总体准确率或 95% 统计置信下限，也不能替代正式产品端到端验收。

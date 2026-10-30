# 聊天工作流评估（5a + 5b）

2026-10-30 持有期限版本：原40场景59步原文不变，新`research-api-horizon-delta-v1.json`绑定原语料、B覆盖层和技术覆盖层摘要。S05止盈止损原问题缺期限，预期为取数前澄清，不插入额外回复；固定离线脚本调用数68→67。计划同时保存technical_delta与horizon_delta，当前规则标记`decision-horizon-v1`。117轮冻结路由用独立v7覆盖层记录变化，见[期限规则](holding-horizon-design.md)。

2026-10-29技术共用版本：原40场景59步的问题、质量及安全要求保留。新增`research-api-technical-delta-v1.json`封存原语料与B差异摘要，明确C02两步、C03三步、D02一步的执行器、澄清载体、结果码及脚本阶段变化；有效语料摘要和差异描述随报告输出。固定调用由72降至68，全部59步仍在分母内；意图与执行器分别统计。见[技术共用设计](technical-shared-narrative-design.md)。

日期：2026-10-28。状态：5a 已提交，5b 已实现并自测，工作区待审核；真实模型和真实来源仅以模拟传输自测，没有发出真实请求。

默认入口检查真实聊天流程在固定脚本下是否遵守契约。另有显式真实模式供后续评估；本次没有真实理解能力、投资效果或真实来源性能数据。假模型通过率不能写成真实效果数字。

## 1. 运行与边界

在项目根目录使用已有虚拟环境：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_research_workflows.py
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_research_workflows.py --case C01 --repeat 3
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_research_workflows.py --collection-workers 1 --trace-dir data/traces/evaluation
```

默认执行全部 40 个场景、59 个消息步骤、重复一次。每个场景的新会话保留自己的完整步骤，每次重复重新开始。没有向会话私有字段预先写入名单或待澄清状态。所有消息均通过 `ResearchApiService.message()`，包含准入、目录检查、实际路由、受控执行、结果检查和公开展示。

支持的选项：

| 选项 | 当前含义 |
| --- | --- |
| `--mode offline/live-model/live` | 默认 offline；真实模式必须显式配置，见第 7 节 |
| `--case ID` | 可重复，选择静态语料里的场景；重复 ID、空选择和未知 ID 被拒绝；执行按语料顺序 |
| `--repeat 1/2/3` | 独立重复，不挑选最好一次 |
| `--collection-workers 1/2/3` | 公司和腾讯报价共享同一实际执行器，配置每次采集的宽度 |
| `--model-call-limit N` | offline 默认 `6T`；真实模式必须显式给出且不超过 `6T`；异常仍占额度 |
| `--trace-dir PATH` | 在新运行子目录中保留记录，不覆盖已有运行；不指定则临时收集并删除，结果中路径为 null |
| `--plan-only` | 只校验计划，不读凭据、不构造客户端、不联网 |
| `--report-dir PATH` | 在唯一新子目录导出 report.json、report.md、manual-review.json |

T 是所有选中场景的步骤数乘重复次数，而不是场景数量。完整一次 T=59，默认额度 354；三次 T=177，默认额度 1062。当前完整固定脚本一次实际转发 69 次模型方法，没有真实供应商 HTTP 请求；脚本次数是框架事实，不是模型成本或理解能力。

程序把版本化 JSON 写到标准输出；指定 `--report-dir` 才导出文件，导出文件使用 UTF-8。可重定向输出；PowerShell 5.1 默认重定向编码与 Python 不同，后续程序读取时应明确编码。退出码：全部通过或预览成功 0，场景/框架/写报告失败 1，输入无效 2。真实执行先把计划写到标准错误输出并刷新，再读凭据；标准输出仍只有一份合法 JSON。

计划校验失败时，预览和正式执行返回相同的固定 `code` 与中文 `detail`，说明参数名、合法范围及必要的计算值，不复述路径原文、文件内容、密钥或底层异常。例如：

| 错误码 | 提示内容 |
| --- | --- |
| `evaluation_model_call_limit_out_of_range` | live 默认 44 步时，上限为 `1..264`（6 × 44）；选择子集或重复运行后按实际计划步数重新计算 |
| `evaluation_price_file_invalid` | 说明 prices 文件无法读取或配置无效，提示核查价格结构、单价和核对元数据 |
| `evaluation_live_directory_stale` | 给出目录原始带时区时间戳和当前北京时间日期，说明快照须在当前日期前 0..7 天内，并提示运行 `python -m trading_companion.security_directory_refresh --live` |

模式、重复次数、并发宽度、场景选择、token 假设、提供方、模型和输出目录等也有对应提示；仅最后兜底保留 `evaluation_input_invalid`。过期目录仍被拒绝，预览不会自动刷新；目录刷新命令需显式联网，和计划预览分开执行。

offline 不读取模型环境或 `.env`，不建立真实来源连接。离线模式复用 [verify.py](../scripts/verify.py) 的清洁环境和网络封锁，即使业务捕获了 DNS/连接异常，整次评估仍判失败。模型环境读取另有拦截和计数。仅允许固定数据配置、有限模型脚本和已知参数，不从语料加载任意 URL、文件、类名或代码。

## 2. 语料与独立预期

[research-api-v1.json](../data/evaluation/research-api-v1.json) 是版本化语料。40 个场景覆盖知识、单股、多股比较、筛选、编辑、名单研究、各种澄清、缺数据、拒绝、来源截止及修复/降级；59 个步骤包含全部准备和续接消息。每个场景都有 `tags`，多轮含 `multi_turn`，人工故障含 `fault_injected`，部分覆盖含 `partial_coverage`。

语义语料版本保持 `research-api-v1`，结构扩展为 `research-api-corpus-v2`，运行结果为 `research-workflow-evaluation-v2`；运行器与语料须一起更新。原 offline 的问题、脚本、60 条理由和各层预期不变。每个场景增加 `modes` 及其补集 `mode_exclusions`，不适用的模式必须附具体理由：

| 模式 | 默认兼容场景/步骤 | 因模式不适用排除 |
| --- | --- | --- |
| offline | 40 / 59 | 无 |
| live-model | 39 / 57 | R01 依赖假模型刻意给出坏草稿 |
| live | 30 / 44 | R01；F03 固定 PE 值；L01/L02/C04 固定名单身份；D01/D02/D03/P01/P02 人工缺项、来源故障或截止 |

以上为重复一次。未给 `--case` 时选择所有兼容场景，预览及报告公开列出全部不适用 ID、理由和 `not_applicable_to_mode` 场景数、`not_applicable_steps` 步数（含重复）。这些排除项不算失败或未运行，不进所选任务的分母；显式选择不适用场景会在任何客户端构造前报错。真实模式不消费假模型脚本，`offline_expected_matched=null`；R01 不能要求真实模型照脚本犯错，也不能在 live 注入人工超时冒充来源故障。

预期由 Codex 依据语义要求和现有设计逐项编写，交 Claude 逐场景审核；不把这个过程称为已经完成的人工质量抽查。路由 v1/v2 基线与本语料分开维护。没有从路由结果生成预期、自动更新快照或取当前输出作为答案的功能。修改预期应注明需求依据并再次审核。

每一步的 `expectation` 分开保存三类要求，另有确定的离线基准：

- `required`：安全和正确性的硬性要求，例如拒绝时不取数、不调用模型、证券身份正确、不得输出某些错误码、如实保留缺失领域及筛选数量。`require_codes`/`forbid_codes` 对照公开结果和安全追踪中的结果码。
- `acceptable`：若干可接受的路由、意图、原始终态、语义类别和结果码组合，每项附具体的场景理由和设计依据。不是独立枚举任意拼接成宽松通行证。多数确定性规则只有一种；M03 的超量比较允许“要求缩小名单”或“明确拒绝”，两者都不能截断后取数。60 条理由逐项静态编写，测试检查全部互不重复；这不代表已完成人工回答质量抽查。
- `quality`：回答质量要求，保存 `answer_origin_if_answered`（回答来自模型还是应用）、`answer_status_if_answered`（检查接纳还是降级）和 `answer_contains`（必须展示的文字）。模型回答未通过检查而使用 `application/fallback`，属于安全降级；若本步期望模型回答，则单列质量失败，不计为硬性违反。R01 第二步专门验证降级，其质量目标就是 `application/fallback`。
- `offline_expected`：可接受范围内的一个确定结果。离线严格比较它，并要求上述三类要求全部满足；将来真实模式保留安全、范围和质量的分别判定，不能要求模型与脚本字面一致。

逐步报告分开给出 `hard_requirement_violations`、`outside_acceptable`、`quality_requirement_failures`，另列 `offline_expected_matched`。`required_checks` 和 `quality_checks` 分别保存具体检查结果。摘要里的前三项按“有此问题的步骤数”统计，不是约束条目数；一步有两条质量失败仍只计一步。未运行项的 `quality_checks` 为 null，失败列表为空，不能视为检查通过。带 `_if_answered` 的要求仅在完成类别下检查；没有回答时不生成这些检查，仍由可接受范围和离线基准判断结果。

模型脚本在独立的 `fixture_script`，仅声明有限协议阶段和固定提取参数，不能读取预期。它会核对工具名称和消息结构；意外调用、顺序或未消费脚本使步骤失败。真实执行器仍核对参数是否来自问题、身份、预算和证据。错误路由不会得到一份自动适配的新答案。

## 3. 固定数据与真实采集路径

[workflow_evaluation_fixtures.py](../src/trading_companion/workflow_evaluation_fixtures.py) 提供固定目录、100 条人工日线、人工财务、新闻/公告和腾讯协议响应，参考时刻固定为 2026-09-26。缺日线配置只有 5 条。所有公司数值、名称对应的描述、标题均为人工示例，不能据此评价真实公司。

`synthetic` 产品模式会关闭筛选，因此本运行器在受限作用域内注入来源：

1. 目录和报价源通过实际服务构造参数传入；公司资料使用实际 `ResearchDataService`，报价使用实际 `TencentQuoteSnapshotSource`。
2. 两者注入固定传输工厂，仍经过实际响应解析、身份/时间验证、共享 `CollectionExecutor`、请求和字节预算、确定性合并及发布规则。公司和报价共用最多三个未完成任务，不用不透明假服务绕过多公司并发。
3. 两个日线来源工厂绑定固定实现；仅固定分析/工具模块的日期时钟，以保持准入日期和取数元数据可复现。没有替换计算函数、检查函数、路由或执行器，追踪和延迟使用实际单调时钟。
4. 作用域退出后恢复工厂与日期时钟；每场景释放闸门并等待自己的实际池退出。

内部 `tencent`、`eastmoney_*` 等名称只标识走了哪个解析器，不代表这些人工响应来自网络。顶层、每步及导出的证据真实性明确为 `fixed_synthetic`；不能把内部产品的来源类型标签用于宣称真实数据。输入清单包含固定数据参数、夹具实现摘要和语料 SHA256。运行同时记录 Git 提交、工作区是否有未提交内容以及运行器实现摘要；不能只凭相同 HEAD 推断两次工作区代码相同。

所有人工传输带 2ms 等待，只用于让独立任务存在可观察的重叠机会；这不是来源延迟模型，也不是性能验收门槛。

## 4. 超时、失败和预算

P01 的三家公司各取财务和新闻，只有第三家新闻被闸门挡住；控制线程必须等它实际进入且协调器接纳其余五项后，才推进来源时钟超过共用 20 秒窗口。`CollectionScope.accept` 的只读观察包装先执行原接纳，不改变接纳规则。P02 在任何证据接纳前推进截止，P03 使用同样请求完整获取，便于区分：

- 部分覆盖：五项按时证据保留，第三家新闻为 `research_data_deadline_exceeded`，显示“未取得”，来源摘要 `stopped=1`、`failed=0`。
- 全缺：没有可用证据，原始终态为失败，不能计作部分成功。
- 普通来源错误：D03 返回人工 HTTP 503，统计为失败来源，不能混成来源截止。

API 返回后才释放迟到响应，关闭执行器后再核对追踪文件与返回前完全相同。同步 HTTP 工作退出前仍占用许可，不能把逻辑超时当成线程已经终止。这些场景都带人工故障标签，未来不能混入真实来源超时比例。

[evaluation_common.py](../src/trading_companion/evaluation_common.py) 在委托前原子扣整次额度，调用抛错也保留未知用量位置。它独立于单轮和会话上限；支持跨场景、会话及重复的总限制。达到上限后剩余计划步骤标记 `not_run`，不补问法、不补上下文、不重跑。某步失败后依序执行后续步骤，并继续其他独立场景。原 [agent_evaluation.py](../src/trading_companion/agent_evaluation.py) 和 [mvp_evaluation.py](../src/trading_companion/mvp_evaluation.py) 的首错停止、输出和契约没有改变。

公司评估原 `_SuiteModel`/`_usage` 名称保留为兼容层，继续默认五场景/九次假模型方法调用。用量总计只加已报告字段，缺失保留 null；未知、部分和不一致用量不算完整，报告完整请求数及未知请求数。零模型尝试与真实零 token 也不是同一件事。

## 5. 追踪与原始结果

运行器使用既有 `JsonlTraceSink`，每场景/重复独立子目录，保留原 64 事件、64KB 单条及轮换限制。一次同步 `message()` 后只接受一个新增且合法的 `run_id`；缺失、重复、不合法或多条都记 `trace_incomplete`，不猜测配对。

记录通过 `TraceReader` 和 `parse_record` 的有界验证。解析投影新增固定意图和白名单事件元数据；未知意图/来源不会携带原始字符串进入输出。v1、v2、已有 CLI 文本和 JSON 汇总的统计口径不变。`TraceSummary` 直接计算总/执行耗时、模型调用分布、用量和事件摘要，没有第二套百分位算法：p50/p95 使用排序后的最近秩，即 `ceil(p*n)`，空集合为 null。

每步保存原始终态、意图和路由、语义结果类别、应用检查后展示的回答、证券和覆盖信息、公开事实报告、预期检查结果及安全追踪。公司回答和技术对话的来源也保留，防止受限结果掩盖回答降级；是否达到回答质量目标独立统计。只有需要引用/数字检查的路径才有相应事件，不能把“未执行”补为通过。事件截断时仍保留原摘要，但精细事件数据不可推断为完整。

当前公司入口会把部分 `out_of_scope` 计划投影为公开 `failed`，同时保留 `product_trade_execution_forbidden`、`product_unsafe_request`、`product_request_out_of_scope` 等拒绝码。评估用这三个固定拒绝码判语义类别为拒绝；保留原始终态及 trace 的 `error` 类别，不修改产品行为。其余 completed/limited 映射完成，needs_clarification 映射澄清，invalid_request/out_of_scope 映射拒绝，其他终态映射错误。“完成类别”不等于任务成功，仍须通过静态硬要求、可接受范围和质量要求。

不会导出凭据、模型原文、提示全文、完整工具返回或底层异常文本。公司与技术的已检查显示、公开事实和证据允许用于人工抽查；它们不能证明任意观点被证据支持。运行完整性另列：即使逐步结果符合预期，只要检测到越出离线边界的访问，整次仍失败。

## 6. 指标及分母

[workflow_evaluation_metrics.py](../src/trading_companion/workflow_evaluation_metrics.py) 由逐步结果和已验证追踪计算整体及 `by_tag` 分组。每个比例含分子、分母、未知数，零分母为 null；标签可重叠，不能把分组相加当总数。

- 路由和意图正确率分别核对静态可接受取值，整体以全部所选兼容步骤为分母，另列已执行口径。准入失败、未运行、缺追踪不能消失。
- 逐步任务成功沿用三层要求，offline 再核对精确基准；场景成功要求本次重复的全部步骤通过。四类混淆表以静态主目标为预期行，合法备选另计；不根据观测换行。保留原始终态、受限数量，未运行单列。
- 引用/数字是联合机械检查，按计划要求模型回答的完成步骤列适用分母，未生成回答保留为未执行。按原检查事件和最终来源解释，保留初次拒绝/最终接纳与尝试数，不把同一个码拆成两个语义通过率。截断或缺追踪时细项未知。
- 规则纠正只计 `intent_rule/rule_applied` 的实际进入，跳过不计；模型重写按实际进入 `model_rewrite`。分别列受影响步骤数、进入次数、全部步骤和计划适用步骤的比例。程序降级按最终应用来源或 fallback 状态计，不是安全违规。
- 截止部分覆盖同时要求领域截止码、实际保留证据及显示“未取得”的缺失领域。按计划公司研究及实际进入采集分别列分母，全缺、普通来源错误、整轮停止、人工故障和非人工截止另列。未知不补成“没有超时”，聚合 stopped 不能代替证据。
- p50/p95 和调用/用量直接复用 `TraceSummary`，失败但有记录的耗时也参加，另给延迟覆盖 n/N。细项截断不会抹掉原聚合摘要，但不能据此编造细项。

## 7. 价格、计划与真实模式

[evaluation_pricing.py](../src/trading_companion/evaluation_pricing.py) 校验 [model-prices.json](../data/evaluation/model-prices.json)。Claude于2026-10-28核对并通过消息 #133 提供TypeSafe/Jev和DeepSeek报价，仓库已记录这两项，不改变本工作流的提供方和模型身份检查约定。每项使用以下字段，精确选择提供方和模型，不模糊匹配、不混加币种：

```json
{
  "provider": "deepseek",
  "model": "MODEL_ID",
  "currency": null,
  "verified_at": null,
  "source_url": null,
  "input_per_million": null,
  "output_per_million": null,
  "input_pricing": "uniform"
}
```

这是结构示例，单价单位为每百万 token；token 是供应商的文本用量单位。填价格时还须给币种（支持 USD/CNY/EUR/GBP/JPY/HKD）、不晚于今天的核对日期和不含凭据的 HTTPS 依据链接。拒绝重复键/条目、未知字段、负数、非有限单价和不完整的核对元数据。Decimal 十进制数用于计算，展示时才保留六位小数。

默认核对价：typesafe/jev-latest为USD输入0.042、输出0（[官网](https://typesafe.ai)，输出目前免费）；deepseek/deepseek-flash为CNY输入2、输出8（[官方中文价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)）。DeepSeek实际有峰谷和缓存价：缓存未命中输入谷/峰为1/2元、缓存命中输入为0.02/0.04元、输出为4/8元；峰时为北京时间工作日9–12点及14–18点。现有价格格式不表达这两种分档，因此以uniform记录最高单价；**费用按峰时、缓存未命中计算，是对应已报告token用量的上限，而不是账单**。不猜缓存命中或根据时钟下调，缺用量/请求记录仍未知，预览token假设也不是整次账单硬上限。

`input_pricing=cache_split` 表示输入涉及缓存分档；现有公开用量无法可靠拆分时输入费用未知，已知输出费用可作小计。缺价格、部分或不一致用量、超出有界计量范围、模型身份不匹配也单列未知。`known_subtotal` 不等于完整的 `estimated_total`；请求数和用量完整性均保留，零尝试不是零 token。

以下只预览，不读密钥、不联网：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_research_workflows.py --mode live-model --provider deepseek --model MODEL_ID --prices data/evaluation/model-prices.json --model-call-limit 300 --assumed-input-tokens 2000 --assumed-output-tokens 1000 --plan-only
```

[workflow_evaluation_runtime.py](../src/trading_companion/workflow_evaluation_runtime.py) 先校验模式、兼容选集、价格、额度和本地路径。真实模式必须显式给 `--mode`、`--provider`、`--model`、`--prices`、`--model-call-limit`。支持既有 DeepSeek/OpenAI 适配器，只读取所选提供方配置，请求模型使用显式参数。计划在任何凭据读取/客户端构造前写入标准错误输出并刷新。

计划列出 T、单轮最多 6 次、整次最坏 `6T` 和实际转发上限 B。仅 offline 可给精确的脚本次数；真实模式实际次数未知，不能沿用假脚本的 69 次。费用预览使用上限 B 与明确的每次输入/输出 token 假设，缺假设或价格则无法估算，不输出预计免费；这不是账单硬上限。运行后按不含正文的逐请求模型/用量账目估算有依据的部分。

live-model 使用同一固定资料接线，资料侧 DNS/外连被封锁；只有所选模型端点的同步 POST 通道可联网。被捕获的禁止访问仍记录并使整次失败。live 使用真实日期、可信本地目录和实际资料/报价采集器；`--data-source tencent/akshare_eastmoney` 选择日线来源，报价仍用实际腾讯来源，公司/报价共享实际并发池。可用 `--directory` 指定本地目录；只校验和使用，不刷新。目录的摘要、时间与元数据写入计划，运行时变更或过期会失败。真实证据保持产品原有真实性标记，不改标为固定数据。没有模型/来源回退、传输自动重试或额外放宽预算；所有资源在退出时关闭。

本次真实模式只以模拟传输自测，官方报价已按上述日期记录。最终付费评估仍由Claude确认价格适用于请求和实际模型，向用户说明并取得费用同意后另行执行。

## 8. 报告、人工清单与并排比较

[workflow_evaluation_reporting.py](../src/trading_companion/workflow_evaluation_reporting.py) 输出同一对象的 JSON/Markdown，标题及范围声明后先给可摘进 README 的 `Verified facts` 总表。表格包含模式、语料与场景/步骤规模、运行状态、任务与路由、三类失败、延迟覆盖和费用；下面保留分组、混淆表、逐步结果、未知及人工清单。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_research_workflows.py --collection-workers 1 --report-dir .agentbus/tmp/eval-workers1
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_research_workflows.py --collection-workers 3 --report-dir .agentbus/tmp/eval-workers3
```

每次创建唯一子目录，不覆盖旧文件；标准输出中的 `artifacts` 给出 `report.json`、`report.md`、`manual-review.json` 的准确路径。抽样使用局部随机数生成器、固定种子 42 和稳定 case/repeat/step 顺序，最多十条应用允许展示的回答；附问题、证据索引（最多 64 项，截断另记）、有界公开事实、run_id、机械检查及空白人工栏，另列失败/未运行。相关性、观点支持与覆盖必须由人工填写。Markdown 对展示文字转义，不执行其中的链接或 HTML。

将两个 `artifacts.json` 路径分别代入 LEFT/RIGHT：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/compare_research_workflows.py --left LEFT/report.json --right RIGHT/report.json --report-dir .agentbus/tmp/eval-comparison
```

比较脚本只读输入，不运行模型、不重跑案例。按 case/repeat/step 的并集对齐，重复键拒绝，单侧缺项显示未知。先核对语料摘要、选集、重复、模式、模型、额度、价格、目录、固定夹具及实现摘要；仅 workers 差异被明确允许，其余不兼容时输出“不可直接比较”。退出码：可比较 0，不兼容 1，输入/输出无效 2。

失败的已记录耗时仍参加 p50/p95；只有同条件且每步两侧都有耗时，才给耗时变化百分比，缺追踪时留空。live 即使配置一致，也因来源时间与先后顺序存在干扰而不计算受控改善百分比。人工 2ms 等待不是实际来源性能；同条件离线对照不能证明真实提速。

## 9. 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m pytest tests/test_evaluation_pricing.py tests/test_workflow_evaluation_metrics.py tests/test_workflow_evaluation_reporting.py tests/test_workflow_evaluation_runtime.py tests/test_workflow_evaluation.py tests/test_evaluation_common.py tests/test_company_evaluation.py tests/test_trace_cli.py -q
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python
```

专项测试覆盖两种宽度的完整场景、独立会话/请求 ID、额度和异常、不读取预期的假模型、错误路由、网络/密钥读取被捕获后的整次失败、追踪丢失/重复/截断、原 CLI 汇总一致、超时与迟到隔离、拒绝语义、修复及降级。另检查理由去重、质量字段的输入边界、正确证券下的降级仅计质量失败，以及证券错误或覆盖缺失仍计硬性违反。测试依靠事件闸门验证重叠和截止边界，不依靠毫秒性能阈值。

两个宽度完整运行都通过，只证明固定输入的契约一致。原路由基线和两套技术评估不变；没有前端改动或真实模型/来源效果数字。

5b 自测另覆盖手算分母/混淆表、联合检查的初次失败与最终接纳、规则跳过不计纠正、真实模式兼容集合、预览早于凭据读取、仅加载所选模型、模拟 HTTP 上的实际模型适配器与资料采集、失败不重试/不切换、整次额度、资源关闭、价格未知/部分用量/缓存/币种隔离、稳定抽样、Markdown 转义、报告不覆盖、只读比较及缺项不丢失。真实模式执行测试在外连封锁下运行，不发真实请求。

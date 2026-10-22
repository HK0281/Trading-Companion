# 受控日线指标工具执行器

日期：2026-10-16（America/Los_Angeles）

## 这一步解决什么问题

M2第一切口让模型协议能够表达工具调用和结果。这一步把白名单调用接到已有多指标研究服务：程序校验参数、控制实际执行次数、读取指定数据源并计算，再按原调用ID返回结构化结果。

工具消息合法不等于业务请求获准。工具声明用于向模型描述能力；执行器仍独立校验名称、参数和权限。产品保持单个受控研究Agent的方向，开发时的ProductManager、Engineer、CodeReviewer和UserProxy只是协作分工。

本次只实现工具执行与离线Fake链路。真实提供方工具适配、自然语言理解、参数澄清、解释检查、会话管理与总截止时间仍需后续实现。

后续实施更新：2026-10-19完成独立DeepSeek工具适配及模拟HTTP/人工CSV往返，见[工具适配设计](deepseek-tool-model-design.md)；2026-10-20记录开发者一次真实工具固定任务反馈，另增[明确格式单轮runner](single-turn-agent-design.md)，全量371项离线测试通过。本文的Fake演示和历史验收记录不变；通用语言理解、回答检查与全轮时限仍待实现。

## 权限与执行边界

后续实施更新（2026-10-20）：为支持[安全事实报告](safe-report-design.md)，构造器新增可选可信 `request_id`。编排层传入本轮ID后，分析和事实沿用同一标识；不传时仍由执行器生成。只接受有界ASCII标识，模型参数依旧禁止 `request_id`，不会因这个配置获得读取旧结果的权限。原单分析预算及紧凑投影规则不变。

后续同日更新：编排层已消费 `ToolExecution.fact_aliases` 实现[受限解读检查](grounded-explanation-design.md)，检查当前请求、关系和数值依赖后生成中文，不再取数或计算指标；54项新增测试后当前全量593项通过。本文独立工具演示仍是原有限脚本流程，不代表真实模型或自由文本检查已验收。

- 仅开放一个只读日线指标分析工具；复用 `IndicatorSpec`、`MultiIndicatorRequest` 和 `analyze_indicators()`。
- 股票与起止日期必须明确，周期及组合遵守已有研究契约；不自动扩展区间补预热数据。
- 在线来源只允许现有腾讯或东方财富，默认腾讯，不自动切换。上海当日或未来日线在创建数据源之前拒绝。
- 模型不能传文件路径、URL、代码、请求编号或CSV启用开关。开发CSV由可信调用方固定配置，结果保留真实性和复权限制。
- 每轮最多一次实际分析；开始执行前消费额度，数据源失败或结果超限不恢复额度。多调用批次在任何读取前整体拒绝。

每用户轮的执行状态由可信应用创建和持有，不能交给模型重置。这个局部次数限制尚不等于完整Agent调度器的token预算、90秒总截止时间或跨会话隔离。

## 为什么需要紧凑结果

研究服务保留完整历史、逐字段状态、初始化信息、关系和事实。工具协议的单JSON对象上限为10,000个ASCII转义字符、2,000个结构节点；完整请求累计工具声明与消息不能超过20,000字符。直接发送完整 `MultiIndicatorResult.to_dict()` 容易超过这些限制，也会重复发送大量历史。

因此完整结果留在可信本地，模型接收带版本与列定义的紧凑视图。查询条件集中存一次；保留请求指标的最新字段、不可用原因、初始化限制及关系依赖的事实。证据可使用请求内短别名，可信本地维护其与原始事实的对应。别名以 `request_id` 为作用域：不同请求都可以出现 `f0`，但不能用另一请求的映射解析它。数值保持计算精度，不为缩小结果而舍入。

容量仍需经过实际工具消息及完整历史校验。超限必须明确失败，禁止默默删除指标、依赖、警告或截断JSON；已执行的计算不能因此自动再取数。

`research-view-v1`集中记录请求区间、实际最新/前一日、读取时间、源、复权、真实性、记录数和数据/计算状态。表格的 `columns` 描述各行含义：

| 表 | 内容 |
| --- | --- |
| `indicators` | 请求实例短编号、类型、完整参数和算法版本；源失败时仍保留请求组合 |
| `states` | 状态与原因共享表，区分历史不足、零基准未定义及计算错误 |
| `latest_fields` | 最新字段、状态引用、所需/缺少记录数和事实别名 |
| `components` | 组件周期、最少记录、种子、首个可用日、递推次数和起点提示 |
| `numeric_facts` | 当前/紧邻前一点的数值事实及日期索引；数值不舍入 |
| `relations` | 关系值、状态、缺少数量及完整依赖别名 |

单位集中在 `units`。关系规则 `research-rules-v1` 和种子字典 `research-seeds-v1` 在唯一工具声明中发送一次，结果显式引用其版本；组件种子使用 `c0` 等字典键。模型必须结合声明和结果解释这些键，不能将包单独当作无须版本信息的通用数据格式。警告采用含义明确的固定英文标识压缩，未知警告保留原文，完整中文警告仍在本地结果中。

## 调用接口

`RESEARCH_TOOL_DEFINITION`是唯一工具声明，名称为 `analyze_daily_indicators`。参数为 `instrument_id`、`start_date`、`as_of_date`，可选 `indicators` 和 `source`；日期使用严格 `YYYY-MM-DD`。指标数组的每项是 `kind` 与该指标允许的参数，例如 `{"kind":"sma","window":3}`；省略数组使用现有默认8实例，不能传空数组。

`ResearchToolExecutor(development_source=...)`可由可信程序固定注入 `CsvDailyBarSource`。开发模式中模型必须省略 `source`，实际结果明确记为CSV；模型显式选择任何源时拒绝，避免将在线请求静默改成本地数据。非开发模式只接受 `tencent` 或 `akshare_eastmoney`，省略时选腾讯；模型选择 `csv` 始终拒绝。

`execute(response, *, messages)`接收状态为 `tool_calls` 的 `ToolModelResponse`。`messages`必须是**模型产生该提议之前**的合法历史，不含这次assistant提议。执行器预检原调用ID、名称、可回填空间，再尝试执行；调用方继续模型时按下面的顺序拼接：

```python
executor = ResearchToolExecutor(development_source=trusted_csv)
proposal = model.complete(history, tools=(RESEARCH_TOOL_DEFINITION,))
execution = executor.execute(proposal, messages=history)
next_history = [*history, proposal.as_message(), *execution.messages]
```

这只是结构化调用用法，`trusted_csv`、`model`与 `history`由调用方装配；完整可运行示例是 `research_tool_demo.py`。

返回的 `ToolExecution` 包含：

| 字段 | 用途 |
| --- | --- |
| `status` | 执行层状态；不能代替结果内 `data_status` 与 `calculation_status` |
| `messages` | 只有本次tool结果消息；ID与原提议配对，多调用拒绝也回齐整个批次 |
| `full_result` | 本地完整研究结果；没有开始分析或执行未产生结果时为空 |
| `fact_aliases` | 请求内别名到原始 `Fact` 的独立副本，包含原始ID和依赖；未产生事实时为空 |
| `projection_metrics` | 完整紧凑视图的ASCII字符及节点计数；极端整数无法序列化时仅含节点及 `serialization_failed=1`，不能当成成功状态 |

同一执行器的 `analyses_used`最多为1，检查与消费使用锁。业务参数被拒绝不消费分析额度；正常数据失败、投影超限及已开始的异常会消费。下轮由可信应用创建新执行器；模型不能自行重置。

`status=executed`仅表示拿到了可回填的分析结果，源失败仍可能得到 `data_status=source_error`，不能据此生成走势解释。`rejected`表示参数、多调用或额度拒绝；`failed`表示受控执行未产生分析结果；`limited`表示结果/剩余历史容量不足，回填 `result_too_large` 且保留本地完整结果。所有拒绝/容量失败均设置 `synthesis_allowed=false`。

未知工具、复用历史调用ID或非工具提议抛分类 `ToolExecutionError`。数据源构造/研究服务执行段的未知异常分类为 `internal_error`，固定错误不泄露该原始异常，不能冒充正常数据状态；投影等其余未知编程错误仍可传播，并非全部代码都被脱敏包装。非法历史或回填空间不足可在执行前抛 `ModelRequestError`。调用方必须结束或处理这些状态，不能假装已有可用tool结果继续请求模型。

## 离线验证范围

演示使用Fake工具模型与可信配置的人工CSV，实际运行现有指标计算，再返回匹配调用ID的工具结果。模型最后的文本仍来自固定脚本；通过演示不代表真实模型理解了行情，也不代表回答语义检查完成。

验收覆盖白名单与参数拒绝、整批预检、单次执行额度、真实离线数值、部分可用、零基准、无数据与故障、证据依赖、结果容量和旧接口回归。所有正式测试使用模拟源或本地样例，不依赖真实市场或模型请求。

在项目根目录运行：

```powershell
$env:PYTHONPATH='src'
python -m trading_companion.research_tool_demo
python -m unittest discover -s tests -p test_research_tool.py -v
python -m unittest discover -s tests -v
```

演示明确指定SMA3、SMA20和量能3。五条人工记录末尾的SMA3约为10.166667，SMA20仍缺15条，量能倍率使用严格此前3条的均量。缩短周期只为同时展示可用与历史不足，不作为产品默认或市场参数建议。外层结果标明 `executed_tools=true`、`network_used=false`、`real_provider_tool_validated=false`，最后文本来自Fake脚本。

## 最终验证记录

2026-10-16，Windows / Python 3.12.10：新增22项正式测试通过；完整292项离线测试（原270项加22项）0失败、0错误。Windows沙箱限制了旧测试临时目录访问，经自动审核批准后在沙箱外原样运行完整测试通过，没有修改测试或业务代码绕过权限，也未联网或使用真实凭据。

UserProxy另完成204个可计数独立检查，覆盖状态/原因还原、种子及规则字典、证据依赖、巨整数和临界历史预算等。其验收夹具中，默认8实例的30/365条视图分别为6509/6649个ASCII字符，常用12实例视图为9678字符、1502节点；这些是指定夹具的测量，不保证所有合法12实例组合都能装入消息。12均线等高关系数量组合可明确返回 `result_too_large`，保留本地结果且不再取数。

CodeReviewer独立复核了最大回退空间预检和巨整数容量降级：不足以回填最大错误信封时零构造/读取、额度为0；已执行后超限则保留完整结果并消费一次额度。先前发现的两处容量问题已修复。主代理另以手算核对人工样例SMA3为 `10.166666666666666`，并通过新模块的Python3.11语法解析；这项静态检查不等于在Python3.11运行测试。

UserProxy最终追加14个入口断言：旧CSV行情CLI、Fake纯文本CLI、旧 `tool_model_demo` 与新 `research_tool_demo` 全部退出0，输出可严格JSON解析并拒绝非有限数。新演示SMA3与手算一致，SMA20缺15条且没有虚构事实，状态为 `ok / limited / synthetic`；网络及凭据访问拦截均为零调用。最终代码审查无剩余问题。

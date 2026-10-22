# 单轮研究 Agent：受控编排的第一个版本

日期：2026-10-20（America/Los_Angeles）

后续同日更新：本文保留明确格式入口初版及其33项/全量371项测试的阶段记录。后来增加[自然语言入口](natural-input-design.md)、[真实目录刷新](security-directory-refresh-design.md)、[股票澄清续接](identity-clarification-design.md)和[程序事实报告](safe-report-design.md)。当前又增加[受限关系检查](grounded-explanation-design.md)：最后模型响应必须是JSON论断，程序通过检查后才生成中文，完整事实加任意草稿不再算 `completed`。原两次模型调用上限不变；新增54项测试后全量593项通过，任意自由文本语义检查仍未完成。

## 本步范围

把固定工具联通测试之外的业务流程封装为可复用的 `run_research_turn()`：接收明确格式的中文研究问题，检查参数，接收模型工具提议，执行一次分析，保留事实和模型解释草稿。

这不是任意自然语言理解，也不是完整对话 Agent。没有名称解析、隐式日期或指标默认值、跨轮记忆、自动重试、回答检查或全轮截止时间。产品目标与默认值提案仍以 [PRD](prd-agent-v0.1.md) 为准，不把本步的收窄实现写成整个 M2 已完成。

## 文件和阅读顺序

| 文件 | 职责 |
| --- | --- |
| [agent_input.py](../src/trading_companion/agent_input.py) | 将明确输入转换为已有研究契约，检查模型提议是否仍符合该请求 |
| [agent.py](../src/trading_companion/agent.py) | 管理单轮阶段、模型与分析次数、退出状态，以及可信事实与草稿的分离 |
| [agent_demo.py](../src/trading_companion/agent_demo.py) | 默认离线，使用脚本 Fake 和人工 CSV 实际计算 |
| [test_agent.py](../tests/test_agent.py) | 33 项专项测试，覆盖歧义、范围绑定、部分结果和失败停止 |

先读 `run_research_turn()` 的早退分支，再看 `parse_research_question()` 和 `proposal_matches_request()`，最后结合测试理解哪些输入不会触发任何查询。原 [tool_smoke.py](../src/trading_companion/tool_smoke.py) 继续只验证固定任务，不改成产品入口。

## 输入先明确，执行才有依据

当前支持的输入示例：

```text
分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3、SMA20、VOLUME3
```

必须提供一只股票的完整代码、起止日期及明确指标参数。简称、裸代码、“最近”、省略指标或额外不明指令都会返回澄清提示。代码语法合法不代表股票确实存在，也不代表具备 ST 或上市状态识别能力。在线来源仅允许上海当日之前的截止日。

| 指标族 | 示例 |
| --- | --- |
| SMA / EMA | `SMA3`、`SMA(20)`、`EMA20` |
| MACD | `MACD(12,26,9)` |
| RSI / ATR | `RSI14`、`ATR14` |
| BOLL | `BOLL(20,2)` |
| 量能 | `VOLUME20`、`量能20`、`成交量20` |

多个实例用顿号、顶层逗号或分号分隔；不会把 MACD/BOLL 参数中的逗号拆成多个指标。周期、区间、最多 12 个不同实例等业务限制复用 `IndicatorSpec` 和 `MultiIndicatorRequest`，不另建一套互相冲突的范围规则。没有自动补预热历史。

## 谁负责澄清，谁有执行权

模型可以负责生成“你指哪只股票？”这样的澄清问题；但一句自然语言追问不等于执行屏障。程序必须同时保存“尚未明确”的状态，在补充信息通过校验前禁止查询。

本步先使用固定澄清消息，信息不足时不调用模型也不读行情。原因是当前缺少名称解析和跨轮状态，固定提示足够解释缺项，并能稳定验证零执行。以后可以替换提问措辞，但不能把执行权转交给模型。

适配器只负责消息和 HTTP 转换，执行器只负责白名单及业务执行；是否澄清、何时停止、是否保留部分结果由单轮编排层决定。

## 单轮数据流

```text
明确格式输入 → 研究契约校验 → 规范参数
  不明确：needs_clarification，零模型/行情调用
规范参数 → 模型提议 → 与已识别请求逐项比对
  不一致：阻断，零分析
一致的单次提议 → 同一个执行器 → 完整本地研究 + 紧凑工具结果
  无可解释字段：保留实际结果，不请求综合解释
可解释事实 → 第二次模型请求 → 未校验草稿 + 本轮状态
```

模型收到程序生成的规范参数，而不是原始自由文本指令。提议必须与已识别请求的股票、日期、指标及可信来源一致。仅仅符合通用 JSON/业务范围还不够：换成另一只合法股票仍然是本轮范围错误。

每轮最多两次模型接口调用、一个执行器、一次实际分析。第二次再提工具、拒绝、截断或失败都结束；不换源、不重建执行器、不重置额度。次数记录的是接口尝试，不能当作成功次数或严格 HTTP 次数。全轮 token/时间预算仍待实现。

模型实例和在线来源由可信调用方注入。开发 CSV 只能通过 `development_source` 注入，不暴露路径或 CSV 开关给模型。本步不读取环境凭据、不增加付费命令入口。

## 状态与部分结果

数据、计算和产品状态是三个层次，不能都用一个 `ok` 判断：

| 层次 | 例子及含义 |
| --- | --- |
| 数据 | `data_status=ok` 表示数据读取及校验正常，不代表所有指标可算 |
| 计算 | `calculation_status=complete/limited/unavailable` 表示字段就绪程度；完整状态是 `complete`，不是 `ok` |
| 本轮 | `needs_clarification/out_of_scope/failed/limited/completed` 表示本轮流程结果 |

五条样例数据上 SMA3 和量能3可用，SMA20缺15条：保留全部有效事实和缺项，返回 `limited / partial_research`。不会为了算出 SMA20 静默改变用户给定区间。

没有日线、全部请求字段不可解释或取数失败时，不请求模型编出综合分析；已有实际结果仍尽量保留。若事实已取得而第二次模型失败，返回 `limited / explanation_unavailable`，不丢掉已计算结果，也不重新取数。

## 事实不是模型草稿

`AgentTurnResult` 分开保存 `confirmed_request`、`research_result`、`model_draft` 和调用元数据。原始用户问题不由 runner 留存；首次直接生成的模型文字不能冒充研究结果。

初版 `completed` 仅代表字段就绪与模型往返完成。后续受限解读升级后，它还要求本轮所选结构化关系通过检查；检查失败为 `limited/explanation_rejected`，有字段缺项仍为 `limited/partial_research`。`explanation_validation`单独报告关系检查；`answer_validation=not_performed`保留并明确范围为任意自由文本语义，不能夸大为所有回答均正确。提示词要求“依据事实”不能替代程序检查。

`to_dict()` 是显式结果导出，不是生产脱敏日志：包含研究证据和模型草稿，不应随意上传。导出的模型响应仅含状态、型号、用量等元数据，不包含初次提议中的任意文字。

## 运行与验证

在项目根目录运行，不需要密钥或网络：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.agent_demo
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_agent.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

演示预期：`mode=offline_synthetic_agent_demo`，`is_mock=true`，模型/行情网络均为 false；本轮 `limited`，两次模型调用、一次分析，`answer_validation=not_performed`。最终文字来自脚本，不能作为真实模型理解能力的证据。

2026-10-20，Windows / Python 3.11.9：33 项新增专项测试及完整 371 项离线测试通过；演示输出严格 JSON，状态和次数符合上述预期。测试覆盖在线源选择时使用模拟源，无真实行情或付费模型请求。

此前开发者已提供一次真实 DeepSeek 工具往返记录，见 [工具验证更新](tool-smoke-design.md)：它验证固定人工数据任务，不是本 runner 的真实端到端验收。

## 后续边界

后续已完成自然入口、有限股票续接、事实展示和受限结构化关系检查。接下来应验证新契约的真实模型表现，再扩展通用参数修改、结果追问及产品入口。全轮截止时间及真实场景评估仍需单独验收；不靠一个离线演示宣称全部完成。

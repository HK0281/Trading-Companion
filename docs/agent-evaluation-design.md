# 固定场景 Agent 契约评估

日期：2026-10-20

状态：入口与离线测试已有；用户已回传一次 `complete` 真实模型场景通过，其他场景待实测

## 1. 这一步解决什么

上一版 `explanation_demo.py` 使用脚本模型，只能证明程序可以处理一份预先安排的正确输出，不能证明真实 DeepSeek 会遵循 `indicator-explanation-v1`。

本入口复用现有 Agent，将模型选择与行情选择分开：默认假模型＋固定人工 CSV；显式选择 DeepSeek 后，只有模型请求联网，数据和证券目录仍使用固定人工夹具。先隔离模型协议、工具参数和关系检查问题，不把行情端点故障混进来。

它是小型契约检查，不是完整产品入口、实时行情评估、历史策略回放或语言质量评估。原 `tool_smoke.py` 保持兼容，仍只用于旧固定任务的联通检查。

## 2. 四个独立预期场景

固定区间为2026-09-21至2026-09-25，目录检查参考日为2026-09-26。CSV只有5条人工日线；短周期是为了在小样本中覆盖计算和缺项，不是推荐的交易参数。

| `--case` | 问题与指标 | 预期 Agent 状态 | 报告 / 解读 | 模型调用上限 | 分析次数 |
| --- | --- | --- | --- | --- | --- |
| `complete` | 明确代码；SMA3、EMA3、MACD(2,3,2)、RSI2、BOLL(3,2)、ATR2、VOLUME3 | `completed/research_completed` | `complete/accepted` | 2 | 1 |
| `partial` | 明确代码；SMA3、SMA20、VOLUME3 | `limited/partial_research` | `limited/accepted` | 2 | 1 |
| `natural` | 平安银行；SMA3、RSI2 | `completed/research_completed` | `complete/accepted` | 3 | 1 |
| `clarification` | 平安；SMA3 | `needs_clarification/instrument_needs_clarification` | 无报告 / `not_performed` | 1 | 0 |

`partial`必须保留SMA20缺15条记录，不能补值；`clarification`必须保留平安银行和中国平安两个候选及原日期/指标条件，不能猜股票或取行情。按预期受限或澄清，同样是场景通过。

默认只运行 `complete`。`all`顺序运行四项，最多转发8次模型调用、执行3次分析；第一个失败后，后续标为 `skipped`，不重试。当前阶段流程固定，成功场景还要求调用次数恰好符合表中预期；以后若有意修改流程，也要修改版本与场景预期。

## 3. 数据流与职责

```text
固定场景和人工夹具预检
  → 显式选择 Fake 或 DeepSeek
  → 独立计数包装器
  → 现有明确格式 / 自然语言 Agent
  → 原受控工具、计算、事实报告和关系检查
  → 对照场景预期逐项判定
  → 仅向终端输出 JSON 报告
```

- `EvaluationCase`：冻结的场景输入和预期，不按实际返回结果反推答案。
- `_MeteredModel`：在委托调用前消耗额度，独立记录尝试、转发与阻断次数，核对Agent计数。原执行器继续限制每轮最多一次分析。
- `_grade()`：检查状态、入口、请求股票/区间/来源/指标集合、同轮ID、人工数据标记、5条记录、报告与关系检查结果；另核对收盘价10.4和原精度SMA3参考值。部分结果和澄清有额外断言。
- `run_evaluation()`：运行一次所选场景、遇错停止、汇总结果；不修改主Agent算法、模型提供方或交易权限。

检查通过不要求模型选定唯一的一组论断，也不评判其选择的相关性、覆盖率和解释价值。七类指标的计算已有独立测试；本入口不另造一套计算器，也不以两个参考数值覆盖所有数值正确性。

## 4. 夹具与请求边界

仅允许代码中固定的 `data/sample_daily_bars_synthetic.csv` 和 `data/security_directory_fixture.json`。在读取真实模型配置前，先校验两份文件的SHA256；只容许CRLF/LF换行差异。文件变更或缺失则本地停止，不能静默发送未知内容后仍称其为已知人工样本。

不支持自选路径、行情源、输出文件、重复次数或自动刷新目录。固定历史参考日只用于开发夹具，不授权旧目录用于当前真实行情。这不是通用文件沙箱，也不保证抵御运行时外部修改夹具。

真实模式沿用现有适配器：单请求默认30秒超时、512输出token、关闭thinking，不自动重试。记录的 `elapsed_ms` 是耗时测量，不是全轮硬截止时间；调用次数上限也不等于货币成本上限或完整token预算。

## 5. 如何运行

在项目根目录的PowerShell中：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.agent_evaluation
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.agent_evaluation --case all
```

这两条命令均离线、不读取模型凭据、不写报告文件。只想运行一个场景时，使用表中的名称。

自行确认模型配置后，显式运行真实模型单场景：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.agent_evaluation --provider deepseek --case complete
```

此命令最多转发2次可能计费的模型调用；不请求真实行情或目录。无需在报告中提供密钥。建议先检查这一项的JSON，再决定是否显式运行其余场景，而不是因失败反复重试。

## 6. 如何阅读报告

- `version=agent-contract-evaluation-v1`，`evaluation_scope`明确仅检查固定人工场景契约。
- 顶层和每个场景的 `status=passed` 表示符合预期，不等于研究状态必须为 `completed`。例如 `partial`可以通过，同时 `observed.agent_status=limited`。
- `checks`列出布尔断言；`failed_checks`说明哪些不符合。模型HTTP成功或状态碰巧相同，都不足以通过。
- `summary`区分选中、执行、通过、失败和未运行；跳过的场景不能算通过。夹具或配置失败时，全部未运行。
- `provider_calls_forwarded`是交给适配器的次数，不保证HTTP已发出或一定计费。`token_usage`只加总供应商已报告的字段，缺失保持 `null`，并报告完整用量覆盖了多少次请求；不估算费用。
- 不导出模型原文、工具完整结果、原始消息、凭据或底层异常文本。模型名称和响应状态保留为诊断元数据；未知失败代码归为 `unclassified_failure`。

CLI退出码：全部选中场景通过为0，模型配置错误为2，场景失败或夹具错误为1；非法CLI参数由参数解析器退出2。不自动保存报告或创建评估目录。

## 7. 已验证与尚未验证

2026-10-20，Windows / Python 3.11.9：新增40项专项测试，全量633项离线测试通过。覆盖四种预期、夹具篡改、预算、错误股票、错误关系、澄清猜码、缺失用量、模型拒绝/截断/超时、失败后停止、脱敏和CLI。DeepSeek适配器路径使用模拟HTTP，不读取真实凭据，不请求真实模型、行情或目录。

入口开发与自动化验证时未执行真实模型请求；随后收到一次用户回传的新契约真实模型成功报告，见下节。即使4项各运行一次通过，也不能说明模型稳定、理解任意中文、预测准确或真实行情可用，更不替代PRD中至少30场景、每项重复3次的较完整评估与人工复核。M2/M3/M4不因有了这个入口或一次成功而整体完成。

## 8. 用户回传：一次完整指标场景通过

登记日期：2026-10-20。依据是用户在对话中提供的JSON；报告没有执行时间戳，本日期不是推断的运行时刻。主Agent没有读取密钥或另行请求模型。

- 版本 `agent-contract-evaluation-v1`；仅选中 `complete`，执行1项，通过1项；所有断言为真，`failed_checks=[]`。
- 请求及返回型号均为 `deepseek-flash`，两次响应依次是 `tool_calls`、`complete`，均标记 `is_mock=false`。
- Agent为 `completed/research_completed`，事实报告为 `complete`，解读为 `accepted/indicator_relations_checked`；2次模型调用、1次分析、无预算阻断或失败码。
- 场景运行耗时3520毫秒；供应商报告5452输入token、421输出token，总计5873，2次请求均有完整用量。这是单次观测，不是p50/p95或费用结论。
- 数据仍为 `synthetic`，没有请求真实行情或网络目录。检查通过的是本轮有限关系，不等于自由文本语义、关系选择覆盖率或预测能力通过。

| 场景 | 当前真实模型反馈 |
| --- | --- |
| `complete` | 用户回传1次通过 |
| `partial` | 尚未收到报告 |
| `natural` | 尚未收到报告 |
| `clarification` | 尚未收到报告 |

未运行的场景不计为失败，也不能算通过。下一项运行 `--provider deepseek --case partial`，最多2次可能计费的模型调用，重点检查SMA20缺15条时能否保留空值与已验证的可用关系；顶层场景预期为 `passed`，Agent预期仍为 `limited/partial_research`。本次无修复依据，不改变提示词、校验门槛或供应商配置。

## 9. 后续开发：共用夹具与终端入口

用户随后要求直接进行下一步，故先实现[终端交互入口](terminal-agent-design.md)；剩余3项真实反馈仍未收到，不记作通过。原 `_fixtures()`的固定哈希、文件和日期检查提取到 `development_fixtures.py`，本评估入口沿用同一函数，不改变场景预期、预算或版本。终端与评估均可复用预检，不互相依赖内部交互逻辑。

新增49项终端测试后，全量682项离线通过，原40项评估测试保持通过；本次未另发真实模型、行情或目录请求。

## 10. MVP扩展：独立的30场景重复入口

2026-10-22保留本四场景入口作为低额度排错，新增 `mvp_evaluation.py` / `mvp_evaluation_cases.py`。30个固定场景最多3次独立重复，完整计划201次模型方法调用、72次分析；默认只跑一项fake场景，显式全局额度不足在读取配置前拒绝。失败即停止，不重试挑选好结果。

新评分额外核对 `explanation_status`、无失败码、`synthesis_status`、数值证据和真实显示。修复审查发现的两种假通过：`partial_research`不能掩盖模型解释被拒绝，顶层`completed`也不能掩盖综合摘要失败。历史概览须实际显示原请求重新核对的综合，局部追问不被强行扩大。

完整离线调用为：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.mvp_evaluation --case all --repeat 3 --model-call-limit 201
```

30场景×3已离线90/90通过；固定回答只证明行为契约，不证明语言理解。加 `--provider deepseek` 才会显式启用潜在付费调用，开发中未执行。报告保留日期、逐响应型号、系统/工具指纹、已知usage和步骤耗时；缺失保持未知，质量始终待人工验收。完整场景、命令和边界见 [MVP交付说明](mvp-completion.md#5-新的30场景评估)。本文件以前的一次真实`complete`记录不因此升级为90项真实通过。

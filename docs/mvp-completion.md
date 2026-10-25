# 单股票日线 MVP：代码交付与验收清单

日期：2026-10-22。范围以 [产品 PRD](prd-agent-v0.1.md) 的本期单股票日线研究为准，不是完整交易助手。

2026-10-24研究循环增量：Web/API和真实终端支持模型逐步选择日线分析、指标知识、快照检查、同快照补充计算，最终提交有引用的自然答复；保留主报告和图表，补充关系与研究过程在详情中展示。新profile每轮最多6次模型（含意图）/6次工具/2次计算，保留原共享时间/token/取消机制；下文“一次分析”等为原MVP交付记录，最新机制以[有界研究工作流设计](bounded-research-workflow-design.md)为准。本轮没有调用真实模型或行情；新闻/基本面和完整对话记忆仍未实现。

2026-10-24增量：网页研究回答不再只展示程序指标综合，已接入基于原问题和工具结果的模型自然语言正文，以及简短数据就绪回退。原报告/关系/综合作为事实附件保留；新正文语义与真实模型质量不由离线测试证明。见[新答复协议与启动说明](natural-research-response-design.md)。

## 1. 本轮补齐了什么

| 缺口 | 实现 | 关键边界 |
| --- | --- | --- |
| 单项指标之外的综合解读 | `research_synthesis.py`，接入 `AgentTurnResult.display_answer` 和历史概览 | 趋势、动量、波动、量能分别描述；有数值依赖，不投票、不预测 |
| 模糊窗口修改不能接短答 | `research_edit.py` / `research_session.py` / `agent.py` | 保存原请求及目标窗口；用户只回复“SMA3”后，仍执行原来的“改成5日” |
| 相对日期修改与刷新 | `research_update.py`，复用日期解析和原研究链路 | “最近一个月”按30自然日；“看最新的”截止上海前一自然日，不是实时行情 |
| 东方财富只在底层可用 | `agent_cli.py` / `api_server.py` / `api_service.py` 及 Web 来源展示 | 显式 `--data akshare_eastmoney`；不自动换源，不接受对话或 HTTP 请求任意改来源 |
| 完整场景集缺少可重复执行入口 | `mvp_evaluation_cases.py` / `mvp_evaluation.py` | 固定30场景、最多3次独立重复；默认 fake，真实模型须显式选择并给足调用额度 |

此前已有七类指标、证券名称解析、模型工具提议检查、只读研究执行器、结果快照与有限追问、运行预算、本机 API、同源 Web、K线/指标图及可选本地证券背景。这一轮复用它们，没有另建第二套 Agent。

## 2. 最终数据流

```text
Web / 终端
  -> 本机应用服务与会话
  -> 初始问题由模型提取候选；有限修改从已校验状态继承
  -> 程序核对证券身份、日期、指标和启动来源
  -> 模型提议工具调用；程序严格比对本轮请求
  -> 只读工具：一次分析，读取所选来源并做确定性计算
  -> 事实报告 + 结构化关系检查 + 程序四维度综合
  -> 同一结果的文字、证据、图表与已冻结的可选背景
```

`safe_answer`、`explanation_validation`、`research_synthesis` 是不同的验证结果：事实成功不等于模型解读成功；模型失败时，程序仍可展示自己独立核对的参考比较，但 Agent 保持 `limited`，不伪装模型通过。

综合模块不采用任意模型文字，也不信任现有关系标签；实际用到的最新/紧邻前一条数值及其 fact 必须重新匹配。历史概览从原请求/结果重新生成，0次模型、0次行情；只问 RSI 的追问不被扩成全套综合。停止只留此前核验的事实，取消不展示本轮结果；所有请求的计算字段都不可用时，不生成综合解读。

本轮保留模块化单体、原进程内接口和既有额度，没有增加代理数量、模型调用或数据库依赖。开发协作子 Agent 不等于产品使用多 Agent。

## 3. 会话与来源的明确取舍

- 窗口澄清绑定原请求、原证券、可信来源和目标窗口；15分钟有效，不能用短答延长原研究基准寿命。额外条件不能被静默忽略。
- “看最新的”保留股票、指标、来源及自然日跨度，创建新研究。日期相同也可按明确刷新重跑，但须说明没有发现新行情。人工模式仅重跑固定样例。
- 来源仅在启动时选择。同一服务内固定一种来源；要换源，停止并按新来源重启、创建新会话。旧结果不能改标签冒充另一个来源。
- AKShare 缺依赖时先返回 `data_dependency_missing`，不先读取模型配置；包存在不等于供应商连通或数据正确。

详细规则见 [综合解读](research-synthesis-design.md)、[会话收口](mvp-conversation-design.md) 和 [来源选择](source-selection-design.md)。

## 4. 如何运行

真实 DeepSeek 模式现在支持源码项目根目录 `.env`：填写 `DEEPSEEK_API_KEY` 与 `DEEPSEEK_MODEL` 后启动，不再需要每次终端输入；终端环境变量仍优先。见[本地配置与启动](local-environment.md)。离线 `--demo` 不读取文件或密钥。

项目根目录中，先完成已有安装与构建，不必激活虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[api,model,dev]'
pnpm.cmd --dir frontend install --frozen-lockfile
pnpm.cmd --dir frontend build
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --demo
```

打开 <http://127.0.0.1:8000/>。`--demo` 是有限的固定脚本，不是能自由理解任意输入的假模型。后端更新后需重启服务；无请求执行时再刷新页面，重启会丢失内存会话。

自由输入须显式启用真实 DeepSeek；以下命令可能计费，需用户自行配置模型凭据：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --provider deepseek --data synthetic
```

先问 `分析平安银行，2026-09-21 至 2026-09-25，只看SMA3、SMA4和RSI2`，随后可试 `改成5日` → `SMA3`、`把日期改成最近一个月`、`看最新的`、`查看上次结果`。人工模式日期锚点固定，数值不是真实行情。

真实历史日线模式：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --provider deepseek --data tencent --directory data/security_directory_live.json
```

东方财富需额外安装 `.[data]`，然后把 `--data tencent` 改为 `--data akshare_eastmoney`。两个命令都需当前可用的真实本地证券目录；目录刷新是单独的显式联网操作，不由服务自动执行。终端入口 `trading_companion.agent_cli` 接受相同数据模式。

## 5. 新的30场景评估

默认只运行一项离线场景：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.mvp_evaluation
```

完整离线执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.mvp_evaluation --case all --repeat 3 --model-call-limit 201
```

场景涵盖七类单指标、完整组合、历史不足、默认参数、名称/代码/歧义、多股票、日期边界、知识、不支持的交易/新闻任务、澄清续接、指标修改、刷新、相对日期及保存结果。

30场景每轮计划67次模型方法调用，3次共201次；90个场景实例包含72次分析。离线的调用只是固定 `FixtureModel` 方法，不是付费请求，也不证明自然语言理解能力。重复之间重建模型与会话，失败即停止，不以重试挑选好结果。

报告逐步核对状态、完整研究范围、调用预算、模型解读状态、失败码、综合摘要和显示内容。部分成功不能掩盖模型解释被拒绝，程序综合失败也不能被顶层 `completed` 掩盖。

报告只写标准输出，不自动保存文件；含执行时间、参考日期、逐响应模型名、系统提示词与工具定义的 SHA-256、可见安全回答、已知 token 加总及步骤墙钟时间 p50/p95。不导出原始模型草稿、提示词原文或凭据。指纹不是完整上下文/模型权重版本，保存结果的动态请求编号也会影响系统提示词指纹；复核时还应记录 Git 提交和运行环境。缺失 usage 保持未知；耗时不是生产延迟基准。

原 `agent_evaluation` 四场景入口保留，适合小额度排错；新入口用于更宽的 MVP 固定行为集。两者都不评收益或预测准确率。

若用户决定做真实模型评估，可在上述完整命令增加 `--provider deepseek`，显式允许最多201次潜在付费调用。**本次开发没有执行它。** 即使真实固定场景全部通过，报告仍标记 `pending_live_repeats_and_human_review`，需要人工检查回答、失败原因和条件，而不是自动宣布产品验收。

## 6. 代码完成不等于真实验收完成

统一工程检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py all
```

本轮 Windows 本地：Python 1172项、前端269项零跳过；原 Agent 4/4、新 MVP 90/90、API 8/8；TypeScript、Vite、ASGI 实际资源与干净 wheel 检查通过。测试和评估使用人工行情/假模型，不请求真实提供方。

仍需单独完成：

1. 真实 DeepSeek 固定场景的重复运行与人工复核，记录实际结果，不能引用 fake 的90/90作为效果。
2. 当前网络环境下腾讯/AKShare 真实历史日线端到端试用，包括失败时提示和不换源行为。
3. 增量浏览器验收，尤其图表、背景、综合正文及来源标签。当前工具的本地浏览器访问此前被策略拒绝，不绕过；组件/ASGI检查不替代浏览器。

新闻、基本面、选股、数值止盈止损、美股、Jev、OpenAI提供方切换、严格PIT存储/回放、账户与公网部署仍属后续，不作为这次 MVP 代码前置。真实证券状态/交易日历适配器也不阻塞当前日线 MVP；缺资料显示未知，不冒充已实现。

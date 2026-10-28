# 每轮运行记录：路由、模型、工具、来源及检查修复

2026-10-27。第一部分已审核并提交为 `a082a29`；第二部分按 #61 分为事件记录（2b-i）和只读查看器（2b-ii）。2b-i 已在工作区实现并通过离线验证，待 Claude 审核；查看器待 2b-i 提交后实现。只接入 Web/API 消息服务，不增加真实模型或行情验收，不代表模型质量、在线稳定性或账单统计已经验证。

## 开启与关闭

JSONL 是每行一条 JSON 的文本文件。本功能默认关闭；只有服务端启动参数 `--trace-dir` 可以开启，不读取追踪环境变量，聊天输入、模型和 HTTP 客户端不能改变目录或开关。

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --trace-dir .\data\traces
```

正常服务及独立 Dev Mode 共用一个写入器，分别标为 `normal`、`development`；会话、来源和模型仍然独立。完全离线演示可用：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --demo --trace-dir .\data\traces
```

`data/traces/` 已加入 Git 忽略规则。自选目录应在仓库外或自行忽略，不把诊断记录提交到版本库。不传参数时不创建目录、文件或后台线程；开启后第一次处理消息才尝试建目录。写盘不可用时研究照常返回，启动器最多输出一次固定提示，不打印路径、异常或凭据。

## 边界与职责

- [消息入口](../src/trading_companion/api_service.py)在 `message()` 进入时创建随机运行号，先于 `_admit()` 的会话核验、额度和幂等检查。知识、研究、筛选、澄清、命令、历史以及目录失败、准入拒绝均经过此边界。
- [追踪模块](../src/trading_companion/turn_trace.py)提供 `TurnTrace` 收集器、`TraceLimits` 容量配置和 `JsonlTraceSink` 写入器。它不解释问题、不重算路由、不控制预算、不发布会话。只在消息全部原收尾完成后写一条记录。
- [整轮控制器](../src/trading_companion/turn_control.py)仍拥有执行期限、取消、预算和迟到结果处理；追踪复用其 `to_dict()` 的白名单快照，不另建控制器，不改变执行起点或增加业务时钟读取。
- `ControlledToolModel` 在成功预留原预算后开始模型事件，在原用量/取消检查之前记录有效响应；DeepSeek 的[共享传输](../src/trading_companion/deepseek_model.py)与[OpenAI 适配器](../src/trading_companion/openai_tool_model.py)在实际 HTTP 客户端调用前标记发送。
- [启动入口](../src/trading_companion/api_server.py)仅把同一个可信写入器注入正常服务与独立 demo/Dev 服务，不改变公共响应结构。运行号仅存于内存请求缓存和追踪文件，不加入 HTTP 返回。

消息服务之前的 HTTP 来源、格式、请求体和缺失凭据校验，以及创建/删除会话、健康检查等非消息操作不记录为研究轮次。`agent_cli`、独立文本 CLI、目录刷新与独立评估脚本本次没有持久化开关。`trace-v2` 增加工具、来源、检查和修复事件；`workflow` 仍只复用已有结束原因和工具总次数，不能整份复制其中的股票或业务请求号。专用只读查看命令留给 2b-ii。

## 三种模型计数

| 字段 | 实际含义 | 不代表什么 |
| --- | --- | --- |
| `attempted` | 原 `reserve_model()` 成功后调用模型包装层的次数 | 供应商请求数；会话额度或本地协议仍可能拒绝 |
| `provider_requests_started` | 已知供应商适配器算好剩余超时后，开始调用 `requests.post()` 的次数 | 供应商已收到、计费或完成；连接失败也算开始发送 |
| `responses_received` | `ControlledToolModel` 收到有效、已校验的 `ToolModelResponse` 的次数 | HTTP 200 的次数；无效 JSON/协议不算有效模型响应 |

本地消息无效、会话调用额度耗尽、发送前截止：有预留成功的尝试，没有发送或有效响应。单轮模型预算已经耗尽：预留失败，不增加尝试，终态保留预算停止码。假模型：有尝试/返回，没有供应商 HTTP 请求。

`transport` 为 `http`、`mock` 或 `unknown`。供应商和请求模型标识仅从已知适配器的可信配置读取；模型标识限 128 个 ASCII 字母、数字及 `_.-`，不符合者为 `null`。不照抄供应商返回的模型名，不遍历未知包装器。未知适配器没有发送钩子时，发送数只是已观测值；`attempts_with_unknown_transport` 明确提示不完整，不能把零解释成没有计费。

迟到有效响应先计入响应和 usage（供应商报告的 token 用量），再按原规则拒绝内容，取消后也不会发布会话。usage 缺失为 `null`；部分字段累加已知部分，不能把缺失补成零。完整性要求三个字段齐全且总量等于输入加输出。值超出追踪可表达的 10¹² 计数上界时记未知；这是记录边界，不是放宽预算或估算费用。

## 时间与结束方式

`total_elapsed_ms` 从进入消息服务到原结果投影、缓存、额度释放及异常收尾结束，包含本地目录工作，排除本条记录最终写盘。它不是浏览器渲染或 HTTP 网络往返延迟。`execution_elapsed_ms` 来自原 `TurnControl`，只计算受控执行阶段；执行未开始时为 `null`，`execution_started=false`。追踪另用独立单调时钟，不消耗原注入的业务时钟。

`phase` 是最后进入的阶段：`admission`、`directory`、`routing`、`execution` 或 `projection`。路由尚未作出时 `route=null`，不伪造技术后备。终态分别记录完成、澄清、受限、拒绝、错误、停止、中断或重放。非预期内部错误仍按原公共错误返回；中断仍传播，不被记录逻辑吞掉。可捕获的异常/中断尽力留下终态，不保证强杀进程或掉电时落盘。

成功与失败缓存的幂等重放都会生成新的访问记录，`original_run_id` 指向原随机运行号；本次模型次数为零，没有复制旧模型事件或执行时间。冲突/进行中/会话忙等拒绝不冒充重放。鉴权成功后才关联缓存和提供方；无效令牌既不获取原运行号，也不显示会话提供方。运行号关联仅在当前内存缓存存续时可用，不恢复聊天。

固定代码使用明确词表；未知业务/异常代码仍记 `other`，不能仅凭字符串格式看起来安全就写入。源码完整性测试通过 AST（把源码解析为结构的抽象语法树）检查所有 Python 模块的代码字段、位置参数构造、辅助函数、默认值、赋值、字典字段和固定 `(code, message)` 返回，以及已审核的模型状态派生码。每个发现的码必须进入固定词表，或列在附逐项理由的 `NON_TURN_CODES` 排除清单；没有按目录或前缀整批豁免。过时、无理由或同时出现在两份清单中的排除项也会失败。

排除项只用于代码完整性审核，例如图表/证据附件、归档或独立评估状态；新增公开结束码应明确加入词表，不能借排除清单跳过。一般知识的模型不可用、输出无效、本地检索失败、拒绝和未完成已通过真实消息入口回归，确认公开结果与追踪原因一致。生产记录仍只查固定词表，不在运行时扫描源码、自动注册新值或记录原始异常。扫描器是受约定约束的静态检查，不是通用数据流证明；新增计算码形式或辅助函数签名需同步审核测试。

## 隐私、容量和故障隔离

只按白名单组装记录。问题、提示词、答案、草稿、工具参数/响应、证据、证券身份/业务日期、用户请求编号、会话编号、问题指纹、请求头、URL、文件路径、密钥、令牌及异常文本均不写入。UTC 起始时间是运行元数据。没有自动脱敏原文开关；`AgentTurnResult.to_dict()`、模型 `to_dict()` 以及完整 `workflow_trace` 都不能直接用作追踪记录。

| 限制 | 默认硬上限与行为 |
| --- | --- |
| 全部详细事件 | 模型与工具/来源/检查/修复合计每轮 64 条；超限只保留总计并增加 `dropped_events` |
| 单条 JSONL | 包含换行最多 64 KiB；再丢弃详细事件，保留总计、控制器与终态，设置 `truncated` |
| 单个文件 | 最多 5 MiB；写入新行前按字节大小轮换 |
| 文件数 | `turns.jsonl` 加 `turns.1.jsonl` 至 `turns.3.jsonl`，总共最多 4 个，即 20 MiB |

可信程序代码可以收紧限制，不能扩大；单条最低 2 KiB。只操作这些固定名字，不递归删除目录，不清理其他文件。已有同名文件异常超大或为符号链接时拒绝写入，保留原文件；容量承诺针对正常由本写入器生成的文件，不会修复外部篡改/损坏的数据。发生短写时尽力退回原完整行边界，磁盘持续故障时不承诺最后一行可恢复；后续查看器应检测残缺行。

单进程共用独立写入锁防止混行，写盘在 API 会话锁、控制器取消/发布锁、会话发布回调及收集器锁之外。打开、写入、序列化或轮换失败不会改研究回复、缓存、额度释放或状态发布；本地同步磁盘 I/O 不承诺硬超时。不同服务进程应选择不同目录，不支持多进程共享一个目录的写入。

收集器用 `ContextVar`（按执行上下文保存值）传递。新建线程不会自动继承；第 3 阶段加入并发时，每个任务必须分别调用 `contextvars.copy_context()`，再以该副本的 `run()` 执行，不能让多个线程同时进入同一份上下文。单元测试锁定：线程池未复制上下文时没有该轮事件，逐任务复制后能归入原轮；独立消息和嵌套关闭范围不会串记。本次没有新增并发取数。

## 历史 v1 实际离线示例与字段

下面是第一部分保留的历史 `trace-v1` 行，来自实际 `api_server.main(["--demo", "--trace-dir", ...])`：用 `TestClient`（进程内 HTTP 契约客户端）执行启动器生成的真实 ASGI 应用，替代 `uvicorn.run` 的监听，不读取 `.env`、不访问模型/行情网络。固定人工研究确实经过路由、三次假模型调用和一次分析。以下仅把该原始 JSONL 行格式化，数值和运行号没有改写；毫秒值属于这次离线观测，不作为性能指标。已检查问题、会话编号和令牌均不在原记录中。新生成的行使用后文的 `trace-v2`。

```json
{
  "schema_version": "trace-v1",
  "run_id": "807542523273415bb4ebc9f99331e5dd",
  "original_run_id": null,
  "started_at": "2026-10-28T04:04:35.823877+00:00",
  "entry": "api_message",
  "mode": "demo",
  "provider": "fake",
  "history": false,
  "phase": "projection",
  "route": {
    "kind": "technical_research",
    "intent": "technical_research",
    "rule_id": "research.technical"
  },
  "outcome": {
    "category": "completed",
    "code": "research_completed"
  },
  "total_elapsed_ms": 31,
  "execution_started": true,
  "execution_elapsed_ms": 16,
  "control": {
    "state": "finished",
    "elapsed_ms": 16,
    "analyses_used": 1,
    "model_calls_attempted": 3,
    "responses_received": 3,
    "observed_tokens": 0,
    "responses_with_complete_consistent_usage": 0,
    "usage_complete_for_attempts": false,
    "stop_code": null,
    "reported_usage": {
      "prompt_tokens": null,
      "completion_tokens": null,
      "total_tokens": null
    },
    "limits": {
      "timeout_seconds": 90.0,
      "model_calls": 6,
      "analyses": 2,
      "observed_tokens": 24000
    }
  },
  "workflow": {
    "stop_reason": "answered",
    "tool_calls_used": 1
  },
  "models": {
    "attempted": 3,
    "provider_requests_started": 0,
    "responses_received": 3,
    "reported_usage": {
      "prompt_tokens": null,
      "completion_tokens": null,
      "total_tokens": null
    },
    "responses_with_complete_usage": 0,
    "usage_complete_for_attempts": false,
    "attempts_with_unknown_transport": 0,
    "events": [
      {
        "sequence": 1,
        "start_ms": 0,
        "elapsed_ms": 0,
        "provider": "fake",
        "requested_model": "fake-tools-v1",
        "transport": "mock",
        "provider_requests_started": 0,
        "response_received": true,
        "response_status": "tool_calls",
        "usage": {
          "prompt_tokens": null,
          "completion_tokens": null,
          "total_tokens": null
        },
        "usage_complete": false,
        "error_code": null
      },
      {
        "sequence": 2,
        "start_ms": 16,
        "elapsed_ms": 0,
        "provider": "fake",
        "requested_model": "fake-tools-v1",
        "transport": "mock",
        "provider_requests_started": 0,
        "response_received": true,
        "response_status": "tool_calls",
        "usage": {
          "prompt_tokens": null,
          "completion_tokens": null,
          "total_tokens": null
        },
        "usage_complete": false,
        "error_code": null
      },
      {
        "sequence": 3,
        "start_ms": 16,
        "elapsed_ms": 0,
        "provider": "fake",
        "requested_model": "fake-tools-v1",
        "transport": "mock",
        "provider_requests_started": 0,
        "response_received": true,
        "response_status": "complete",
        "usage": {
          "prompt_tokens": null,
          "completion_tokens": null,
          "total_tokens": null
        },
        "usage_complete": false,
        "error_code": null
      }
    ]
  },
  "truncated": false,
  "dropped_events": 0
}
```

| 字段 | 解释 |
| --- | --- |
| `schema_version` | 此历史样本为 `trace-v1`；现在生成 `trace-v2`，按版本解释，旧记录不原地重写 |
| `run_id` / `original_run_id` | 随机运行号 / 仅重放时关联的原运行号，均非用户请求编号 |
| `started_at` | UTC 起始时间，和业务研究日期无关 |
| `entry` / `mode` | 当前入口固定为 `api_message`；模式为正常、开发服务或离线 demo |
| `provider` / `history` | 经鉴权的会话提供方，无法确认时为 `null`；是否通过可信历史查看入口 |
| `phase` | 最后进入的处理阶段，失败时帮助定位发生在执行之前还是之后 |
| `route.kind` / `intent` / `rule_id` | 实际执行入口、语义意图及静态路由原因，不记录匹配原文或额外扫描候选 |
| `outcome.category` / `code` | 终态类别及固定结果/停止码；重放引用原轮而不重复研究 |
| `total_elapsed_ms` | 消息服务的完整处理耗时，不含最终记录写盘 |
| `execution_started` / `execution_elapsed_ms` | 是否开始受控执行及原控制器的执行耗时；未开始时耗时为 `null` |
| `control.state` / `stop_code` | 原控制器状态及固定停止原因；执行前失败则整个 `control=null` |
| `control.analyses_used` / `model_calls_attempted` / `responses_received` | 原控制器的预算与返回计数，保持原口径 |
| `control.observed_tokens` | 已观察到的停止阈值计数。示例为 0 因为假模型无 usage，不代表真实成本为零 |
| `control.reported_usage` / `responses_with_complete_consistent_usage` / `usage_complete_for_attempts` | 原控制器报告的已知输入/输出/总 token、完整一致响应数及完整性标志 |
| `control.limits` | 原轮次时限、模型次数、分析次数及已观察 token 停止阈值，不是账单硬上限 |
| `workflow.stop_reason` / `tool_calls_used` | 现有工作流的固定结束原因和工具次数；不含详细工具事件 |
| `models.attempted` / `provider_requests_started` / `responses_received` | 三种明确分开的计数，见上表 |
| `models.reported_usage` / `responses_with_complete_usage` / `usage_complete_for_attempts` | 从有效模型响应得到的已知用量和一致性，截断详细事件后仍保留总计 |
| `models.attempts_with_unknown_transport` | 发送边界未能确定的尝试数，防止用零发送数推断零费用 |
| `models.events[].sequence` / `start_ms` / `elapsed_ms` | 轮内顺序、相对轮次起点的开始偏移和该次尝试耗时 |
| `models.events[].provider` / `requested_model` / `transport` | 已知提供方、可信请求模型标识及 `http/mock/unknown` 传输类别 |
| `models.events[].provider_requests_started` / `response_received` / `response_status` | 该次尝试的发送次数、是否有效返回及 `complete/tool_calls/refused/incomplete` 状态 |
| `models.events[].usage` / `usage_complete` / `error_code` | 该次已知 token、完整性及固定错误/停止码；迟到响应可同时有 usage 和停止码 |
| `truncated` / `dropped_events` | 是否因数量/字节上限丢弃详细事件，以及丢弃条数 |

示例生成方式由[启动器测试](../tests/test_turn_trace_api.py)中的离线 demo 测试覆盖；启动上述 `--demo` 命令后，在网页发送固定演示问法也会生成相同结构，运行号、时间和毫秒值自然不同。假模型调用证明流程可执行，不是模型理解能力或在线延迟的证据。

## v2 的事件格式与统一接口

2b-i 开始写 `schema_version="trace-v2"`。原根字段和模型统计口径保留，新增根 `events` 数组及 `tools`、`sources`、`checks`、`repairs` 四个汇总；模型事件仍在 `models.events`，增加关联字段。不复制历史行，也不把 v1 没有的新信息解释成零次。一个轮换文件可以包含两个版本；将来的只读查看器须逐行分版本解释，未知版本不能猜读。2b-i 没有交付查看命令。

[统一接口](../src/trading_companion/turn_trace.py)只提供两种业务用法：`trace_call` 包围已有调用或作装饰器，`trace_event` 包围需要跨多条语句的事件。业务模块不拼记录字典、不维护词表，也不访问收集器内部字段：

```python
@trace_call("check", "research_response")
def validate_research_response(text, *, execution, expected_request):
    ...

bars = trace_call("source", "daily_bars", source.read_daily_bars)
```

关闭追踪时 `trace_call` 直接执行原调用，不检查参数或返回值，不读诊断时钟；多语句的上下文接口只产生空句柄。接口内部隔离元数据检查、计时、词表和写盘错误；原业务异常继续传播，原调用至多执行一次。只检查已知结果类型的元数据，不调用通用 `to_dict()`、字符串化或未知对象属性来猜来源。

| 非模型事件字段 | 含义 |
| --- | --- |
| `kind` / `operation` | `tool/source/check/repair` 与固定操作；工具名限现有四个只读工具，未知提议不回显名字 |
| `event_id` / `parent_event_id` | 全类别共享的轮内整数号及父事件号；不使用用户或供应商编号 |
| `phase` | 事件开始时的原阶段，尤其区分 `execution` 和 `projection` |
| `start_ms` / `elapsed_ms` | 相对轮次开始的偏移及实际经过时间；由独立追踪时钟计算 |
| `source` / `access` | 已知类型或受信配置给出的固定来源；访问类别为 `network/local/snapshot/unknown`；非来源事件为 `null/unknown` 占位 |
| `entered` | 是否进入该语义操作；工具通过原授权和额度后才置真；跳过为假，不表示供应商收到请求 |
| `status` / `code` | 固定状态与固定结果码；未知字符串为 `other`，没有异常文本 |

状态为 `ok`、`limited`、`rejected`、`failed`、`stopped`、`skipped`、`unfinished`。`unfinished` 表示整轮收尾时该事件尚未结束；`elapsed_ms=null`，不把它算成功或零耗时。状态 `ok` 只表示此操作按原接口返回；数据是否有效由对应检查事件表达，并不证明回答的全部自由文本为真。

四个汇总都有 `seen`、`entered` 和七个同名状态计数。`seen` 为观察到的入口次数，含跳过；`entered` 为实际进入次数，跳过不计；状态计数之和等于 `seen`。规则修复的“不需要纠正”可使 `repairs.seen=1`、`skipped=1`、`entered=0`，不能说模型重试了一次。进入模型重写流程后仍可能被共享预算挡住；实际模型尝试仍只按原预算预留成功计入 `models.attempted`。

### 工具、来源与检查的实际覆盖

- [工作流工具](../src/trading_companion/workflow_tools.py)调用[单次研究工具](../src/trading_companion/research_tool.py)完成主分析时共用一次逻辑工具事件；进入原执行分支后才标记 `entered`。提议不匹配、重复和额度拒绝仍有结果，未读取来源。知识和快照摘要是本地工具；补充指标只重用原快照。
- [日线入口](../src/trading_companion/multi_analysis.py)记录已有读取、来源校验和记录校验，覆盖技术和公司研究；已知 CSV 为 `local`，补算快照为 `snapshot`。来源抛错、空记录、无效记录分别保留原语义。
- [公司来源](../src/trading_companion/company_research.py)按章节记录，区分东方财富公司/财务/新闻/公告、新浪和巨潮；历史约束跳过资料时 `entered=false`。内置服务不再重复记录外层 `collect`；未知注入服务仅以来源未知的批次调用记录，不能凭其自报标签推断供应商。一次章节或筛选读取可能包含多次 HTTP，事件数不是 HTTP 请求数。
- [消息服务](../src/trading_companion/api_service.py)记录已有证券目录与可选市场背景加载；[公司研究](../src/trading_companion/product_research.py)记录已有可选在线背景读取、批次校验；[筛选](../src/trading_companion/screening_agent.py)记录原快照读取；[知识问答](../src/trading_companion/open_knowledge.py)记录本地检索与原格式/引用检查。注入闭包或替身的访问类别无法确认时保留 `unknown`，不按函数名或返回标签猜测。归档写入不计来源读取。
- 技术/工作流回答及指标关系检查按实际调用次数记录，包括展示阶段的重复核验，不为追踪新增、缓存或省略校验。旧式关系回答可能跳过新的会话格式检查，但仍完成子级关系检查，不能把 `no_conversational_response` 解读为全部检查失败。低层每个字段断言不建事件。

已发生的读取可在返回后被原取消检查挡住：来源事件仍表示读取已发生，父工具或整轮记录停止；不会因为记录读取成功而发布迟到结果。根 `outcome` 和控制器仍负责表达整轮是否停止。

### 修复与父子关联

[公司回答](../src/trading_companion/product_answer.py)第一次输出被原检查判为可修复后，`rewrite_decision` 只记录 `rewrite_allowed/disabled/not_repairable/limit_reached` 等固定理由。仅第二次实际进入重写流程时创建 `model_rewrite`，作为第二次模型与检查事件的父事件；第一次拒绝保持独立。仍最多重写一次，模型错误、再次无效、取消和预算停止都留固定结果，没有第三次调用，也不记录草稿和修复提示词。

[请求理解](../src/trading_companion/research_intent.py)的有限形状检查记录为 `intent_shape`；通过所有原身份/参数门禁、真正形成可执行请求后，`intent_rule` 表示原程序是否纠正了模型不必要的澄清。普通研究为 `rule_not_needed` 并跳过，纠正为 `rule_applied`。不把这类规则纠正叫模型重写；未形成可执行请求的早期拒绝保留原检查和轮次结果，不捏造一次成功修复。

父子号允许表示嵌套和重叠；事件号按开始分配，两个数组并非全局时间排序。子事件耗时不能相加当总耗时。父事件可能因详细上限而未保留，将来查看器须提示关联不全。

### 共享上限、词表和未来并发

模型与非模型详细事件合计最多 64 条；超限后继续统计四类固定摘要及模型用量。序列化超过字节上限时按事件号从后向前丢详细项，`dropped_events` 包含数量与字节两种丢弃。摘要不按来源、操作或码值扩展字典；未来细码分布只能声称覆盖保留事件，必须同时标明截断。最小 2 KiB 配置也验证了终态、控制器、模型和四类摘要保留，原文件容量与故障隔离规则不变。

`_CODES` 仍是整轮公开结果词表；`_EVENT_CODES` 是新增事件可记录的固定细码。原“非轮次结果”中现在作为工具、来源或检查结果的项移出排除清单，进入事件词表；它们不会因此变成整轮公开码。源码守卫支持固定 `(status, code)` 元数据返回，并另核对公司/新闻私有拒绝理由，避免 `str(error)` 包装后的真实来源原因漏登记。未知值仍不自动注册。

共享收集器的事件号、计数、保留额度、阶段及完成标志均受同一独立锁保护；业务执行、诊断类型检查及磁盘 IO 不在该锁内。每个线程池任务分别复制 `ContextVar` 上下文，保留自己的父事件；同一轮共享收集器，不同轮隔离。`finish()` 后所有更新被忽略，写出的是独立标量结构，迟到句柄不能修改已完成记录或二次写盘。未来调度器仍须先收束任务再结束轮次；追踪不等待线程、不接管取消或发布。

### v2 的实际离线示例摘录

以下来自同样的实际离线启动器入口，完整原行保留于本次审核的本地临时样本；这里只摘录四个真实事件与原汇总，不是完整行。它有 3 次假模型、1 个工具、3 个逻辑来源调用、20 次检查入口和 1 个“无需纠正”的规则机会；修复实际进入为 0。目录和可选本地背景是注入加载器，类别未知；另一次日线读取为 CSV。本次由网络守卫证明离线，不能凭来源总数推断真实联网请求。执行和展示都确实重复调用了旧式关系检查。毫秒值属于单次离线观测。

```json
{
  "schema_version": "trace-v2",
  "run_id": "d33150054c014e02a04bf171a5b391af",
  "outcome": {
    "category": "completed",
    "code": "research_completed"
  },
  "tools": {
    "seen": 1,
    "entered": 1,
    "ok": 1,
    "limited": 0,
    "rejected": 0,
    "failed": 0,
    "stopped": 0,
    "skipped": 0,
    "unfinished": 0
  },
  "sources": {
    "seen": 3,
    "entered": 3,
    "ok": 3,
    "limited": 0,
    "rejected": 0,
    "failed": 0,
    "stopped": 0,
    "skipped": 0,
    "unfinished": 0
  },
  "checks": {
    "seen": 20,
    "entered": 16,
    "ok": 16,
    "limited": 0,
    "rejected": 0,
    "failed": 0,
    "stopped": 0,
    "skipped": 4,
    "unfinished": 0
  },
  "repairs": {
    "seen": 1,
    "entered": 0,
    "ok": 0,
    "limited": 0,
    "rejected": 0,
    "failed": 0,
    "stopped": 0,
    "skipped": 1,
    "unfinished": 0
  },
  "events": [
    {
      "kind": "tool",
      "operation": "analyze_daily_indicators",
      "phase": "execution",
      "start_ms": 16,
      "elapsed_ms": 0,
      "source": null,
      "access": "unknown",
      "entered": true,
      "status": "ok",
      "code": "primary_analysis_completed",
      "event_id": 6,
      "parent_event_id": null
    },
    {
      "kind": "source",
      "operation": "daily_bars",
      "phase": "execution",
      "start_ms": 16,
      "elapsed_ms": 0,
      "source": "csv",
      "access": "local",
      "entered": true,
      "status": "ok",
      "code": "ok",
      "event_id": 17,
      "parent_event_id": 6
    },
    {
      "kind": "check",
      "operation": "workflow_response",
      "phase": "execution",
      "start_ms": 31,
      "elapsed_ms": 0,
      "source": null,
      "access": "unknown",
      "entered": false,
      "status": "skipped",
      "code": "no_conversational_response",
      "event_id": 22,
      "parent_event_id": null
    },
    {
      "kind": "check",
      "operation": "workflow_response",
      "phase": "projection",
      "start_ms": 31,
      "elapsed_ms": 0,
      "source": null,
      "access": "unknown",
      "entered": false,
      "status": "skipped",
      "code": "no_conversational_response",
      "event_id": 25,
      "parent_event_id": null
    }
  ]
}
```

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_turn_trace tests.test_turn_trace_events tests.test_turn_trace_api tests.test_request_routing tests.test_routing_baseline tests.test_routing_conflicts_api
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python
```

专项覆盖关闭/开启兼容、隐私哨兵、失败与重放、两家适配器的发送/响应边界、迟到用量、停止、不持锁写盘、短写/轮换故障、容量以及逐任务上下文复制。开启追踪后额外复跑全部 35 场景、105 轮路由目标，对照完整输出、状态、调用和业务时钟事件。原 v1/v2、捕获器及人工目标不修改。

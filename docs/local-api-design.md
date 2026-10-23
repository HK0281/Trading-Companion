# 本机 FastAPI 接口层

日期：2026-10-22。状态：已实现本机单进程接口及离线验证，不是公网或多用户生产部署。

后续同日已增加[最小Web工作台](web-workspace-design.md)、[历史图表](chart-workspace-design.md)及[本地证券状态／交易日历背景](market-context-design.md)：构建后访问根路径 `/`，同源调用下列API，不需要Swagger手动复制令牌。路径不变，公共结果增加可选chart和market_context；下文906项为接口模块完成时记录，当前Python1054项、前端256项检查通过。

## 1. 这个模块做什么

让终端以外的客户端也能调用已有Agent，不把指标计算、模型编排或事实检查搬进HTTP路由。

```text
HTTP请求 → FastAPI / Pydantic校验
         → ResearchApiService：令牌、会话、额度、幂等及取消
         → 原run_natural_research_turn / TurnControl
         → 原目录、工具、指标与证据检查
         → 白名单公共响应，不使用Agent调试导出
```

代码分工：

- [api.py](../src/trading_companion/api.py)：HTTP输入/响应模型、路由、请求体限制、同源与Host保护、统一错误。
- [api_service.py](../src/trading_companion/api_service.py)：独立于FastAPI的服务层，拥有进程内会话和模型实例，协调配额及幂等记录。
- [api_server.py](../src/trading_companion/api_server.py)：显式启动模式、服务端模型/数据配置，固定127.0.0.1及一个worker。
- [api_demo.py](../src/trading_companion/api_demo.py)：TestClient进程内HTTP演示，不开监听端口、不请求真实模型或行情。

FastAPI/Pydantic只在HTTP边界使用，内部仍复用dataclass。没有引入数据库、任务队列、SSE、WebSocket或新Agent框架。

## 2. 启动与尝试

在项目根目录安装可选依赖；原行情测试仍需要此前的data依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[api,model,dev]"
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_demo
```

演示检查创建、研究、重复重放、追问、历史、重置、旧编号失效、删除，共8项。4次Fake模型调用、1次人工CSV分析，不代表真实语言理解质量。

启动可交互的本机HTTP服务：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --demo
```

无参数只显示帮助，不启动、不读密钥。运行后打开 <http://127.0.0.1:8000/docs> 查看Swagger接口；Ctrl+C停止服务。默认演示使用原 `ConversationDemoModel`，不是通用Fake语言模型：先研究固定问题，再问固定追问，可选原SMA替换脚本；重新创建会话才得到新的脚本实例。重置不重建模型或返还额度。

另开一个PowerShell终端，可实际调用：

```powershell
$apiBase = 'http://127.0.0.1:8000'
$apiSession = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/sessions" -ContentType 'application/json' -Body '{}'
$apiHeaders = @{ Authorization = "Bearer $($apiSession.access_token)" }
$apiMessage = @{
    request_id = [guid]::NewGuid().ToString('N')
    question = '分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2'
} | ConvertTo-Json
$apiReply = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/sessions/$($apiSession.session_id)/messages" -Headers $apiHeaders -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($apiMessage))
$apiReply.result.answer
```

随后用新的 `request_id`发送“这里的RSI为什么偏高”；也可GET `.../result`查看历史。重复发送完全相同的 `$apiMessage`只重放响应，不再调用模型。Swagger里先POST `/v1/sessions`，将返回的 `access_token`粘贴到Authorize的Bearer输入框，再调用会话接口；不要分享该令牌。

仅在自行确认愿意付费、且同一终端已配置模型时，显式启用：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --provider deepseek --data synthetic
```

真实腾讯模式改为 `--data tencent`，需当前有效的本地目录，可用 `--directory`指定服务端路径。不会刷新目录、静默换源或让HTTP客户端提交模型密钥/路径。问题、确认参数与紧凑证据会发给显式选择的DeepSeek，可能计费；HTTP层没有新增模型提供方。

## 3. 接口契约

| 接口 | 功能 | 正常HTTP状态 |
| --- | --- | --- |
| GET `/health` | 服务模式及只读边界，不检查供应商连通 | 200 |
| POST `/v1/sessions`，body `{}` | 创建会话，返回ID和一次可见的访问令牌 | 201 |
| GET `/v1/sessions/{id}` | 当前状态、已用额度；忙时不读正在修改的研究槽 | 200 |
| POST `.../{id}/messages` | body仅 `request_id`、`question` | 200 |
| GET `.../{id}/result` | 显式历史查看；0模型/0分析，不消耗问题额度 | 200 |
| POST `.../{id}/cancel` | body仅 `request_id`，对准当前轮发取消信号 | 202 |
| POST `.../{id}/reset`，body `{}` | 清研究状态，旧请求编号退役，额度不退 | 200 |
| DELETE `.../{id}` | 删除空闲会话及其状态 | 204 |

除health、文档和创建会话外，必须有 `Authorization: Bearer <access_token>`。这是一份会话能力凭据，不是DeepSeek密钥，也不是用户账户登录。ID不是授权依据；另一会话令牌不能访问该会话。

ID与 `request_id`均为32位小写十六进制字符串。问题为严格字符串、1–2000字符，不接受空白句；首尾空白规范化后参与幂等比较。所有请求禁止额外字段，不能用body配置模型、来源、目录、文件、预算或会话对象。POST需要JSON，总请求体最多16KiB；即使流式上传或伪报Content-Length，也检查实际累计字节。

HTTP 200表示服务处理了这条合法消息，`result.status/code`才表示研究成功、澄清、范围拒绝或模型失败。不能把200当作投资判断正确。结构错误422，缺令牌401，不匹配/未知/过期会话统一404，编号冲突或会话忙409，容量/额度满429，目录不可用503，未预期内部失败500。错误只含固定 `error.code/message`，不回显输入、底层异常或Pydantic错误详情。

公共响应只选取程序安全答案、事实报告、已确认参数、候选、默认口径、运行统计及有界chart。chart来自本轮分析或接受追问所绑定的已验证快照，最多366条/48字段/256KiB紧凑UTF-8 JSON，不额外取数。图表不可用不会抹掉文字；历史/追问附原参数与报告，失败新研究不附旧图。该大小界限不是整个响应或内存上限。禁止直接返回 `AgentTurnResult.to_dict()`：它包含调试草稿和完整研究结果，不适合作为HTTP协议。

可选 `market_context` 独立返回板块、上市／ST／全天停牌、逐日交易所日历及来源冲突；最多64KiB紧凑UTF-8 JSON，不发送模型、不参与计算或筛选、不声称PIT。仅已提交且内部研究ID相同的保存结果可附背景；新研究读取可选本地loader，历史和追问只深复制原缓存，首次失败也冻结，cache miss不补读当前文件。缓存每会话最多20项，与幂等响应同锁发布，reset／自然重置／delete／TTL／close清理。停止轮保留的指标事实不能据此升级为已保存背景。未配置或读取失败只让背景unavailable，正文仍有效。

启动器 `--market-context` 仅接受应用选定的可信本地JSON，原始≤5MiB；禁止在消息body或模型工具里配置。demo固定人工夹具，Tencent默认未配置，不自动联网或刷新。默认夹具的真实股票代码配有故意虚构的上市/ST/停牌和日历属性，仅用于程序验证，不能作为市场事实。`source=sse/szse/bse` 也是快照自报标签，不代表应用已认证官方内容。详见[背景契约](market-context-design.md)。

## 4. 幂等、并发与取消

每会话维护一个有限 `request_id → 问题摘要/响应或错误`记录。摘要使用SHA-256，不保存原始问题；记录上限由20条问题额度限制。相同编号/问题已结束时，返回原响应并标 `replayed=true`；仍在执行时409，不发第二次请求；同编号换问题409。缓存包括已接纳请求的目录/内部失败，不能通过网络重试偷偷再调用。

重放是“原消息的原结果”，不是重新查询最新状态。重置清报告缓存，但保留编号退役标记，重用返回409。达到服务并行上限而尚未接纳的请求不消耗问题编号，可用同一编号稍后重试。

同会话同时只能一个操作；默认整个服务最多2轮在执行，忙时立即拒绝，不建立无界队列。同步路由 `def`由框架放在线程池运行，不能把同步模型HTTP直接塞入 `async def`阻塞ASGI事件循环。取消路由仍能响应：只发送 `TurnControl.cancel()`，不从另一个线程直接清研究状态。

取消必须匹配当前API `request_id`，不是内部研究事实的request ID。202只表示已发信号；正在执行的远程请求未必已经中断，也可能计费。原消息连接会在协作检查后返回 `turn_cancelled`。取消不提前释放运行槽，否则旧工作尚未结束就可能启动更多工作。重置/删除忙会话先返回409，应等待原请求结束再操作。

客户端断开或取消HTTP等待，不等于Agent收到取消信号；需要显式调用取消接口。当前没有持久任务ID、后台任务、流式进度或断线续传。

## 5. 配额、安全和生命周期

- 默认最多32个会话，每会话20条新问题、60次模型方法转发，创建后30分钟绝对失效；访问和重置不续期。
- 失效按请求惰性清理；发现正在运行的失效会话时发取消，返回后清理。没有独立定时清理线程。
- 原Agent的15分钟结果/条件有效期和每轮90秒协作限制仍独立生效。上传及本地目录准备发生在Agent入口之前，不宣称整个HTTP请求90秒硬结束。
- 创建新会话/重启会重获本机额度，因此这不是跨会话的账户限流、付费花费上限或抗滥用系统。
- 访问令牌来自随机生成并以常量时间比较；服务内存保存，重启后失效。不使用浏览器Cookie，不开启跨域通配CORS。
- 启动器固定loopback、一个worker。Host仅接受localhost/127.0.0.1/IPv6 loopback；拒绝跨Origin及cross-site浏览器请求，防止网页诱发本地付费操作。未来不同端口前端需要另行明确配置可信Origin。
- 所有HTTP响应no-store；启动器关闭访问日志。服务不写聊天、报告或凭据文件，不将会话令牌作为模型请求配置。
- 关闭服务时撤销空闲会话并向在途轮发取消；不能声称强制杀死供应商请求。

会话隔离不等于生产用户鉴权。不能把此服务绑定0.0.0.0、启用多个worker或放进公网反向代理就称部署完成；还需用户身份/资源归属、TLS、账户配额、共享状态、真正任务生命周期、可观察性与上线验证。HTTP工厂默认是离线演示，不能据此误判真实提供方已经验收。

## 6. 验证与协作

新增50项HTTP/服务测试，全量906项离线测试通过；原四场景契约评估4/4，进程内API演示8/8。覆盖令牌隔离、请求与上传边界、Origin/Host、幂等与退役、配额、并发取消、过期/关闭、响应脱敏、启动器选择和实际指标研究链路。另起一个临时loopback演示进程，实际TCP调用health→创建会话→固定研究，返回HTTP 200/`research_completed`，随后停止该测试进程。没有新增真实付费模型或行情请求；实际端口检查不等于真实模型验证。

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p 'test_api*.py'
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests
```

按用户最新要求，从本模块起：每个完整模块通过测试和提交检查后commit并push，不为每条命令或小修改单独提交。本次也同步此前仍留在工作区的已验证项目代码；不提交虚拟环境、密钥、实时目录或个人文档。

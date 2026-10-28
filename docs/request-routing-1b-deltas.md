# 路由1b：用户变化与逐轮差异

2026-10-27，已实现并离线验证，未提交，待Claude审核。路由1a已审核提交、推送为`03bfe1c`。

| 修正 | 你会看到什么变化 | 保留的边界 |
| --- | --- | --- |
| B1 | 等你选股票时，插问“MACD是什么”会获得定义，原来的问题继续保留。 | 固定定义零模型、零取数；插问不延长有效期。 |
| B2 | 技术分析问你是哪只股票，回复“平安银行”会继续原SMA3请求。 | 不能扩大股票、日期和指标；候选外、过期、来源变化仍拒绝。 |
| B3 | 等你确认要改哪个指标时，公司名或名单序号由原提问流程判断。 | 不因非法回复自动开始公司研究或选择旧名单。 |
| B4 | 一般知识功能关闭时，PE/PB知识题明确提示未开启。 | 不调用模型，不创建公司澄清；固定RSI等定义仍可询问，原目录门禁保留。 |
| B5 | 等你回答澄清时，“筛选条件把PE改为低于15”不会把原任务冲掉。 | 明确“帮我选5只股票”仍可开始新筛选；修改不因包含“筛选”二字升级为新任务。 |
| B6 | 异常地同时存在多个待回答问题时，请你取消或重新开始。 | 不猜所有者、不自动清理、不取数；取消和重置可恢复。 |

## 旧记录如何保留

[v1](../tests/fixtures/request-routing-v1.json)的LF内容仍为299,874字节，SHA-256为`d751c6a46db4e09a8eebb14f0c07b93f5d9cb6a12024b97dfaff3ced229eee6b`，摘要口径为UTF-8字节先将CRLF归一为LF（`sha256_utf8_normalized_lf`）；[原观察器](../tests/routing_baseline_support.py)没有修改。v1描述旧`09cd366`实际行为，1a版本`03bfe1c`仍应全部通过，错误行为也不能从历史记录中删掉。

审核#49发现Windows新克隆会把未指定属性的JSON换成CRLF，原始字节指纹因此误报。现用`.gitattributes`仅将`tests/fixtures/*.json`固定为LF检出，并让消费者与候选生成器共用LF归一指纹；元信息新增`v1_hash_format`。不统一Python/Markdown换行，不改v1内容。v2只刷新元信息和测试源码摘要，35场景/105轮、目标与53处差异均未重录。新增回归用真实v1的CRLF副本核对指纹相同，改变内容仍能被发现；另将该副本代入原来失败的两项测试，两项都通过。

[v2](../tests/fixtures/request-routing-v2.json)复用相同35场景、105轮输入。语义意图和改变的结果先按批准方案人工声明，再执行观察；新工具只能提出未提交工作区候选，不冒充干净提交捕获。投影是对大块完整结果保存摘要与字节长度，其余关键状态/事件/调用保留原文；它不是随意挑几个正确字段。

105轮中100轮与v1完整一致，另5轮共53处投影字段变化，全部关联B1、B2、B4。B3、B5、B6未修改这35个历史场景的结果，由新增真实入口反例单独验证。v2每个场景的`contracts`保存独立目标，`deltas`逐轮保存`behavior`与完整`fields`，字段项包含路径、改前值和改后值。列表按完整有序值比较；不把数组重排或删元素当作归一化。

## 五轮变化及传递影响

| 场景 / 轮次 | 修正 | 修改前 | 修改后 | 传递影响 |
| --- | --- | --- | --- | --- |
| `workflow-off` / 1 | B4 | PE/PB知识题进入公司入口；`missing_product_instrument`；1次模型并创建公司待澄清。 | 本地提示`general_knowledge_disabled`；0次模型、不创建待澄清。 | 本轮缓存码、累计模型次数和下一轮起始状态改变。 |
| `workflow-off` / 2 | B4 | RSI定义被上轮公司待澄清截走，`product_explicit_selection_required`。 | 技术知识入口给`indicator_knowledge_answered`。 | 上轮缓存和累计模型次数沿用新值；不再保留错误创建的公司待澄清。 |
| `company-pending-owns-replies` / 5 | B1 | MACD定义进入公司入口，要求明确股票。 | 技术知识入口回答定义；原公司待澄清对象与时限原样保留。 | 本轮缓存结果码改变，模型和数据次数均不变。 |
| `company-pending-owns-replies` / 6 | B1 | 原公司选择续接，缓存里第5轮是澄清错误。 | 相同公司选择、数据和公开回答；缓存里第5轮是知识已回答。 | 仅本轮前后缓存中第5轮结果码改变，不能因此跳过整轮状态检查。 |
| `technical-pending` / 5 | B2 | “平安银行”变成公司研究；默认扩大技术指标，1次公司读取、1次日线读取、2次模型。 | 技术身份续接，原SMA3；0次公司读取、1次日线读取、2次模型。 | 保存技术基准与结果、关联市场背景缓存；不再保存公司结果。 |

完整答案发生变化时，其完整指纹和字节长度也列入比较；不是因为输出太长就忽略正文。最后一轮B2的公开结果由57,182字节变为20,984字节，对应公司研究结构变为单指标技术回答，不是正文截断。

## 命令与历史的执行器核对

优先级0整句取消/重置：单一`product_pending`继续由原公司执行器清理；技术身份、指标目标或没有待澄清时由原技术执行器清理。两者的取消均只清三类待澄清，保留已有可信研究条件、结果和名单；重置清全部会话。正常单一状态的原执行器、消息及状态变化未改变，没有B7。

优先级1历史：可信history请求在有公司保存结果或公司待澄清时沿用公司入口；文字历史遵循原公司追问匹配，否则交技术入口。例如“查看上次结果”可命中公司已保存结果，“查看上一次结果”保留原技术入口归属。公司历史依旧校验保存结果；技术历史依旧校验可信基准，不重新取数或调用模型。两种历史的事件序列也参与v1对照。

异常多待澄清的取消属于B6的恢复：统一交原技术命令清理所有待澄清，不先报状态冲突。原有三个状态字段及存储结构没有迁移。

## 前端兼容与验证

`general_knowledge_disabled`和`routing_state_conflict`都使用已有`needs_clarification`状态，返回非空`message`及空研究附件；`answer`可以为null。前端`api.ts`接受字符串结果码，`App.tsx`显示“需要补充”，`ResponseContent.tsx`无结构化研究时使用`answer ?? message`展示文字，不依赖新增结果码白名单。真实入口测试检查这两个公开返回形状，无需修改前端。

验证命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_request_routing tests.test_routing_baseline tests.test_routing_conflicts_api
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python
```

定向35项测试通过；Python门禁2051项unittest、228项pytest及全部固定场景通过，零跳过、零xfail。固定问法矩阵覆盖25句×4种会话上下文；额外真实入口测试覆盖候选回复、过期、开关、修改前缀、冲突恢复、取消及幂等。变异检查故意删除固定知识、技术澄清或知识关闭规则，三个目标场景均能发现错误。首次定向测试暴露了新测试对指标内部表示、空answer回退及脚本模型复用位置的错误，已修正测试夹具；目标候选首次校验还发现观察器中内部指标与公开指标表示不同，改为按相同语义核对，未改v1或原观察器。

两个新结果码另以真实后端公共投影喂给现有`createApi`与`ResponseContent`：用已安装TypeScript在进程内转译原前端源码，再由React服务端渲染，两种码均通过响应检查并渲染出完整消息，未改前端文件。最初尝试Vite加载配置时，esbuild子进程被沙箱以`spawn EPERM`阻止，改用上述不创建子进程的方法后通过；这项检查没有执行浏览器交互，也不替代完整web/bundle门禁。

没有联网、付费模型或浏览器交互验收；以上是离线流程证据，不是真实语言准确率或真实来源速度。知识与目录的旧边界、名单比较的已有计划拒绝及“再加RSI2”的假模型理解限制仍保留。

## 全部变化字段路径

下面逐轮列出所有53处字段路径。完整改前/改后值存于v2同场景、同轮的`deltas[*].fields`；嵌套对象、事件数组和答案完整指纹均保留，没有隐藏字段白名单。

### `workflow-off` / 1 / B4

- `/after/model_calls_forwarded`
- `/after/product_pending`
- `/after/records/turn-1/result_code`
- `/calls/model_forwards`
- `/events`
- `/executor_chain`
- `/outcome/response/result/code`
- `/outcome/response/result/full_json/sha256`
- `/outcome/response/result/full_json/utf8_bytes`
- `/outcome/response/result/model_calls_attempted`
- `/outcome/response/result/runtime/responses_received`
- `/route`

### `workflow-off` / 2 / B4

- `/after/model_calls_forwarded`
- `/after/product_pending`
- `/after/records/turn-1/result_code`
- `/after/records/turn-2/result_code`
- `/before/model_calls_forwarded`
- `/before/product_pending`
- `/before/records/turn-1/result_code`
- `/events`
- `/executor_chain`
- `/outcome/response/result/code`
- `/outcome/response/result/full_json/sha256`
- `/outcome/response/result/full_json/utf8_bytes`
- `/outcome/response/result/stage`
- `/outcome/response/result/status`
- `/route`

### `company-pending-owns-replies` / 5 / B1

- `/after/records/turn-5/result_code`
- `/events`
- `/executor_chain`
- `/outcome/response/result/code`
- `/outcome/response/result/full_json/sha256`
- `/outcome/response/result/full_json/utf8_bytes`
- `/outcome/response/result/stage`
- `/outcome/response/result/status`
- `/route`

### `company-pending-owns-replies` / 6 / B1

- `/after/records/turn-5/result_code`
- `/before/records/turn-5/result_code`

### `technical-pending` / 5 / B2

- `/after/market_contexts/<uuid-1>`
- `/after/product_saved`
- `/after/records/turn-5/result_code`
- `/after/technical/confirmed`
- `/after/technical/saved_result`
- `/calls/company_reads`
- `/events`
- `/executor_chain`
- `/outcome/response/result/code`
- `/outcome/response/result/company_research`
- `/outcome/response/result/full_json/sha256`
- `/outcome/response/result/full_json/utf8_bytes`
- `/outcome/response/result/parameters/indicators`
- `/outcome/response/result/stage`
- `/route`

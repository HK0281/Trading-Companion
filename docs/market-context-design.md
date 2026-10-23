# 证券主数据与交易日历背景

日期：2026-10-22。状态：本地快照契约、API附加投影、会话内冻结与Web卡片已实现；真实主数据／日历适配器和历史版本库未实现。

## 1. 解决的问题与边界

指标能算出来，并不代表我们知道股票当时属于什么板块、是否ST、是否全天停牌。缺少某天K线也不能直接推断停牌。此模块先把这些背景的信息来源、业务有效期、发布时间和未知状态明确表达出来。

它是附在原研究答案旁边的只读背景，不是新工具或交易规则引擎：

- 不改变Agent提示词、指标计算、研究答案或模型调用次数，也不把背景发送给模型。
- 不按ST、科创板或新上市类别排除股票；不判断涨跌停、委托资格、成交可能性或止盈止损。
- 不从代码前缀猜板块、不从名称猜ST、不从缺失K线猜停牌、不按工作日猜开市。
- 来源字段只是本地文件的标注；严格解析不等于认证其官方来源或事实正确。
- 始终声明 `historical_availability=not_established`，不称为point-in-time系统。

名称目录 `SecurityDirectory` 仍只负责身份核实。今天的名称→代码匹配不能成为过去某天ST或停牌的证据，因此两份契约分开。

## 2. 数据流与文件职责

```text
原Agent完成研究并保存结果
  → API重新核对已提交快照和内部研究ID
  → 新研究：读取可选本地背景 → build_market_context
  → 同锁冻结背景DTO与原HTTP响应
  → Web独立校验 → MarketContext卡片

追问／历史：原已保存研究ID → 原背景缓存（不读当前文件）
```

| 文件 | 职责 |
| --- | --- |
| `src/trading_companion/market_context.py` | 严格bytes/文件解析、状态有效期与发布时间过滤、日历对照、固定提示投影 |
| `src/trading_companion/development_fixtures.py` | 有界读取一次，同份规范化字节校验哈希并解析人工夹具 |
| `data/market_context_fixture.json` | 明确编造的证券属性和日历，仅供离线验证 |
| `src/trading_companion/api_service.py` | 可选loader、内部研究绑定、冻结缓存及生命周期清理 |
| `src/trading_companion/api.py` / `api_server.py` | 公共DTO、默认人工背景和可信本地路径配置 |
| `frontend/src/marketContextData.ts` | 从unknown检查请求、日期、来源、状态和完整日历覆盖 |
| `frontend/src/MarketContext.tsx` / `marketContext.css` | 显示来源、时间、未知、自然日年龄、日历与冲突，不产生请求 |

配置入口是应用启动参数，不是HTTP body、聊天输入或模型工具参数。没有增加第三方依赖。

## 3. 本地快照契约

实际完整样例见 [人工夹具](../data/market_context_fixture.json)。根对象只允许以下字段：

```json
{
  "version": "market-context-snapshot-v1",
  "snapshot_id": "example-local-v1",
  "retrieved_at": "2026-09-20T02:00:00+08:00",
  "is_fixture": false,
  "security_states": [],
  "calendars": []
}
```

空数组是合法的“未提供”，不是正常状态。不要把人工夹具简单改成 `is_fixture=false` 来冒充真实数据。

### 证券状态记录

`security_states` 每项必须完整给出：

| 字段 | 约束 |
| --- | --- |
| `instrument_id` | 完整6位代码加 `.SH` / `.SZ` / `.BJ`；不扩大原行情源支持范围 |
| `effective_from` / `effective_to` | 闭区间ISO日期，同股票记录不得重叠；区间空档是未知 |
| `board` | `sse_main`、`star`、`szse_main`、`chinext`、`bse`、`unknown`，必须与交易所一致 |
| `listing_date` | ISO日期或null；允许未来上市日，不据此定义“新股”阈值 |
| `st_status` | `not_st`、`st`、`star_st`、`unknown` |
| `suspension_status` | `full_day_suspended`、`not_full_day_suspended`、`unknown` |
| `published_at` | 带时区ISO时间或null；若给出，不得晚于采集时间 |
| `source` / `reference` | 来源枚举与安全引用标签；标签不是URL、任意描述或认证凭证 |

“非全天停牌”不保证没有盘中临停或能够成交。上市年龄为 `as_of_date - listing_date` 的自然日差，上市当日为0、尚未上市为null；它不是交易日数，也不代表适用任何交易规则。

来源枚举是 `synthetic_fixture`、`sse`、`szse`、`bse`、`curated_local`。交易所来源必须匹配记录的交易所；人工快照只能标synthetic_fixture，非人工快照不能标该值。ID/reference首位字母或数字，总长1–100，只允许字母数字及 `_.:-`；原研究ID继续遵守其已有契约。

### 交易所日历

`calendars` 每项必须给出 `exchange`、`coverage_start`、`coverage_end`、`published_at`、`source`、`reference`、`days`。每个交易所至多一块，总数不超过SH/SZ/BJ三块。

`days` 是按日期递增的 `{ "date": "YYYY-MM-DD", "status": "open|closed|unknown" }` 列表，覆盖期内每个自然日都必须显式存在。少一天、重复一天或乱序均拒绝；覆盖外日期保持unknown。没有BSE日历时不能借用沪深日历。

## 4. 时间语义

| 时间 | 含义与使用 |
| --- | --- |
| 状态有效区间 | 业务上这条状态适用哪些日期；只选择覆盖分析日的证券状态 |
| `published_at` | 文件声称的发布时间；未知或晚于上海分析日结束时，状态值隐藏／日历按未知处理 |
| `retrieved_at` | 应用获得快照的时间；晚于本轮参考时间则拒绝这份未来快照 |

采集晚于分析日，但不晚于本轮参考时间时，可以显示回溯业务背景，同时提示采集较晚。即使声称的发布时间较早，也没有证明保存过当时版本、没有后来修订或遗漏，因此仍明确“不是PIT”。本模块按日线分析日截止，不支持日内时点。

ST、上市或停牌状态不是从今天反推过去。查不到有效记录、未公开或部分字段未知时，就保留unknown；不能把“没查到ST记录”写成“非ST”。

## 5. 日历对照与失败行为

返回查询范围内的 `open_dates`、`closed_dates`、`unknown_dates`，三者有序互斥且完整覆盖所有自然日；再与同一研究结果的实际bars对照：

- 开市但无日线：列入 `missing_bar_dates`，提示未匹配，不推断停牌。
- 休市却有日线：列入 `unexpected_bar_dates` 并标来源冲突。
- 所报上市前有日线、已公开全天停牌区间有日线：只标冲突，不删改原行情，也不擅自裁定哪个来源正确。

背景 `available` 只表示分析日状态完整、查询日历已覆盖且无冲突，不表示内容已独立核实、全区间证券状态已知或可交易。未知或冲突时为 `limited`。未配置、文件不合法、未来快照、真实性混用、结果错配或体积超限时，返回固定unavailable，不暴露路径或原始异常。

`data_authenticity` 为 `synthetic` 或 `unverified_local_snapshot`。人工背景只允许配人工行情；真实行情不能显示测试属性。前端独立校验这个可选字段；失败只降级卡片，正文、图表和请求确认不受影响，不触发任何自动重试。

## 6. 冻结、边界与生命周期

缓存按内部研究ID，而不是外层HTTP幂等ID绑定。只有重新核实已提交的 `SavedResearch` 后才加载背景。停止轮即使保留安全指标事实，若未提交保存结果，也不会加载或缓存背景。

首次背景失败同样冻结；幂等重放、追问和历史查看不能因文件后来修复而偷偷补读。缓存缺失返回 `context_cache_miss`。新的成功研究才读新快照。返回值和缓存深复制，防止调用者修改原记录；同锁发布背景和幂等响应。HTTP重置、自然语言重置、删除、会话过期和关闭服务均清理背景。

| 边界 | 限制 |
| --- | --- |
| 原始快照文件 | 最多5MiB，只读MAX+1字节检查边界 |
| 证券状态 | 最多10,000条 |
| 日历 | 每交易所最多3,660自然日；最多3个交易所 |
| 研究范围 | 最多366自然日，沿用原请求边界 |
| 公共背景DTO | 最多64KiB紧凑UTF-8 JSON，不含原始记录全集 |
| 背景缓存 | 每会话最多20项（可随问题额度收紧），不跨会话共享 |

JSON序列化大小不是Python对象总内存或整个HTTP响应的上限。可选文件准备在Agent结束后进行，不宣称受Agent的90秒协作截止强制中断；本地文件不自动刷新，也不支持网络流式源。

## 7. 配置与验证

离线演示默认加载固定人工背景，无需新配置：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --demo --port 8765
```

`000001.SZ` 的夹具上市日为**虚构的2026-09-01**；`688981.SH` 的夹具上市日、*ST和全天停牌同样是**故意编造的测试属性**。2026-09-21至25均开市等日历安排也**不代表真实交易日历**。这些安排用于检验状态展示、包含科创板／ST／新上市及来源冲突，不能据此研究实际股票。

真实模型模式可显式指定自己的可信本地文件（可能产生模型费用，不由本模块自动执行）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --provider deepseek --data tencent --market-context .\data\market_context_local.json
```

仍需原入口所要求的当前本地名称目录与服务端模型配置。`data/market_context_local.json` 已Git忽略；自选其他路径不自动获得忽略保护。Tencent模式未提供该文件时显示背景未配置，不自动请求外部数据。`--demo` 不接受自选背景；DeepSeek+synthetic默认仍用人工背景，自定义背景必须通过真实性匹配。

新增37项核心、6项固定夹具、22项服务与3项HTTP/启动器测试；前端新增114项独立背景检查和6项集成检查。Windows全量1054项Python、256项前端零跳过，Agent 4/4、API 8/8、TypeScript/Vite、真实编译资产ASGI与干净wheel验证通过。仅离线／模拟，不访问真实模型、行情、目录刷新或读取凭据。上次浏览器访问被安全策略拒绝，本轮真实卡片浏览器验收仍未完成，不以组件测试代替。

下一增量应先接入并核实真实证券主数据／交易日历的来源、覆盖与更新方式，再考虑将可信状态提供给Agent或规则引擎。公司行动、完整历史版本、持久化与PIT证明仍需独立模块，不用本契约冒充完成。

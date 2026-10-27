# 有界 A 股报价样本筛选

## 已实现的能力

本模块从明确的报价样本中执行数值条件筛选，再按用户指定的一个字段排序。它没有跨指标综合得分，不预测“潜力”或未来收益，也不拥有下单能力。

- 支持多个 AND 条件：`gt`、`gte`、`eq`、`lte`、`lt`；最多12项。
- 支持单字段升序或降序；相同原始值按证券代码升序稳定排列，最多展示50项。
- 缺失值保持 `null`。条件所需字段缺失时，该条件为 `unknown`，不会填0或当成已验证的条件失败。符合条件但缺少排序字段的记录单独计数，不排名。
- 返回逐项条件、实际数值、单位、报价更新时间、样本覆盖和来源。过旧报价单独计数，不与条件失败混在一起。
- 科创板、ST、新股、停牌股票没有被默认排除。不过，本模块没有独立的历史证券状态、上市日期或实时可交易性证明；没有出现在样本里的股票不能被说成“未通过筛选”。
- 提供完全离线的人工样例，也提供两个显式可选的公开报价适配器。真实来源失败不会变成人工样例、空结果或另一个供应商。

本模块不负责解析自然语言、选择模型或定义 HTTP 路由。Agent/API可调用下述公共接口；它们必须继续说明筛选范围是实际覆盖样本。

## 公共接口

### 请求

```python
from trading_companion.screening import ScreeningRequest

request = ScreeningRequest.from_dict({
    "conditions": [
        {"field": "pe_dynamic", "operator": "gt", "value": 0},
        {"field": "pe_dynamic", "operator": "lte", "value": 20},
        {"field": "amount_cny", "operator": "gte", "value": 100_000_000},
    ],
    "sort_by": "amount_cny",
    "sort_direction": "desc",
    "limit": 10,
    "max_quote_age_days": 7,
})
```

| 字段 | 含义 | 规范单位 |
| --- | --- | --- |
| `price` | 最新价；没有报价或来源返回0时不可用 | 元/股 |
| `change_percent` | 供应商涨跌幅 | 百分数；`2.5`表示2.5% |
| `amount_cny` | 供应商当日报价中的成交额 | 元 |
| `turnover_percent` | 供应商换手率 | 百分数 |
| `pe_dynamic` | 供应商动态市盈率 | 倍 |
| `pb` | 供应商市净率 | 倍 |
| `market_cap_cny` | 供应商总市值 | 元 |
| `float_market_cap_cny` | 供应商流通市值 | 元 |

阈值必须是有限数字，不接受字符串、布尔值或模型添加的未知字段。PE/PB负数作为来源实际观察值保留。如果用户要求“盈利且PE不超过20”，应显式使用 `pe_dynamic > 0 AND pe_dynamic <= 20`，不能把负PE自动解释为便宜。

`max_quote_age_days`按上海日历日计算，允许0至14天。它限制供应商报价更新时间，不等于验证实际交易发生时间。采集快照本身只允许复用24小时，身份目录必须满足现有的7日新鲜度规则。

### 完全离线演示

```python
from trading_companion.screening import (
    ScreeningService, demo_screening_snapshot, render_screening_result,
)

snapshot = demo_screening_snapshot()
result = ScreeningService().screen(snapshot, request)
payload = result.to_dict()
print(render_screening_result(result))
```

人工样例含5条记录，覆盖沪市主板、深市主板、科创、创业和北交，还包含明确人工构造的ST名称。它没有网络或模型调用，`data_authenticity`固定为 `synthetic`。样例名称和数值不能用于真实投资研究。

### 真实报价采集

```python
from trading_companion.security_directory import load_security_directory
from trading_companion.screening import ScreeningService
from trading_companion.screening_source import TencentQuoteSnapshotSource

directory = load_security_directory("data/security_directory_live.json")
snapshot = TencentQuoteSnapshotSource().read_snapshot(directory)
result = ScreeningService().screen(snapshot, request)
```

也可显式选择 `EastmoneyQuoteSnapshotSource()`，调用方式相同。两者是启动配置选项，没有“腾讯失败后自动用东财”或反向切换。

`ScreeningService(clock=...)`、`demo_screening_snapshot(clock=...)`以及两种来源的`session`和`clock`均可注入，便于固定时间及离线测试。底层身份、快照、分层覆盖等对象都为不可变dataclass，并在构造时检查契约。

### 返回内容

`result.to_dict()`的版本为 `quote-screening-result-v1`，主要字段如下：

- `status/code`：有结果为 `completed`；已核验、日期合格的样本确实没有符合条件项为 `empty`；没有日期合格的已核验报价为 `unavailable`。网络或无效响应由异常明确报告。
- `rows`：证券ID、目录名称、分层、报价时间、8个数值字段、逐条件 `passed/failed/unknown`、排序原始值及样本内展示位置。
- `conditions`：每个条件在日期合格的实际样本中的通过/不通过/未知数量。
- `coverage`：扫描数量、身份已核验数量、身份未知数量、没有报价数量、日期过旧数量、符合条件数量、缺少排序值数量、截断情况和逐字段覆盖。
- `coverage.strata`：每个分层的总量依据、实际选取或扫描数量、返回的核验报价数量、缺报价数量及请求次数。
- `source`：供应商、采集时间、身份目录元数据、采样方法和同一组分层覆盖。
- `overall_score/forecast`：始终为 `null`；`trading_enabled`始终为 `false`。

腾讯按可信目录选择样本，因此 `directory_universe_records`有值、`vendor_declared_records`为 `null`。东财有供应商列表总量，因此后者有值；这个数字仍不是独立证明的全市场总量。两种来源的 `independent_full_market_completeness_proven`始终为 `false`。

`quote_date`是供应商报价更新时间在上海时区的日期。它没有被包装成已经交易所核验的实际成交日期。跨请求的报价不是原子快照。

## 腾讯来源：默认可选方案

固定公开端点：`https://qt.gtimg.cn/q=`。请求中的证券只能来自调用方预先配置、已验证且新鲜的非人工目录，不能由模型提供任意URL或任意身份映射。

1. 按沪市主板、深市主板、科创、创业和北交5层划分当前目录。
2. 每层按代码升序选择前50个，可信配置可调到每层最多100个，总数最多500个。这个选择有代码顺序偏差，不能称为随机代表样本或全市场筛选。
3. 每批最多80个代码；允许的可信批次配置为50至80，因此每层最多2个请求、整个采集最多10个请求。没有5000个逐股HTTP请求。
4. 对返回的变量名、代码、名称、币种和完整上海时间逐项核对。来源未返回某个已选ID时，计为 `missing_quote_records`，不当作条件失败或身份未知。
5. 正常名称只做Unicode和空格规范化；科创来源额外的 `-U`尾缀允许与目录中的同代码完整基础名称匹配。其他改名、ST变化等不自动放宽。最终输出保留可信目录名称，不根据这个后缀宣称已核验盈利状态。
6. 响应只按固定赋值语法解析；没有执行JavaScript、`eval`或动态导入。

逐字段映射已经通过实际原始响应检查，不能将相近字段混用：

| 返回索引 | 规范字段 | 转换 |
| --- | --- | --- |
| 3 | `price` | 元/股 |
| 30 | `quoted_at` | `YYYYMMDDHHMMSS`，上海时区 |
| 32 | `change_percent` | 原始百分数 |
| 38 | `turnover_percent` | 原始百分数 |
| 46 | `pb` | 原始比率 |
| 52 | `pe_dynamic` | 原始动态PE；不用索引39或53替代 |
| 57 | `amount_cny` | 万元×10000；与索引35内人民币成交额交叉核对，允许1元显示精度差 |
| 45 | `market_cap_cny` | 亿元×100000000 |
| 44 | `float_market_cap_cny` | 亿元×100000000 |

例如2026-10-26实测平安银行，动态PE索引52为4.20；索引39为5.04，不能替代。成交额索引57为148599.9504万元，与索引35中的1485999504元一致。此记录用于验证协议映射，不保证未来接口不变。

当前的交易所目录刷新来源覆盖沪深，未提供独立北交目录。腾讯本身实测能返回`bj920000`报价，但正式服务只有在可信目录包含北交身份时才采集它。目录未覆盖北交时，该层的目录数和采样数都为0；不能说北交股票没有通过条件。

## 东方财富来源：显式备选

固定公开端点：`https://82.push2.eastmoney.com/api/qt/clist/get`。它是供应商公开接口，不是本项目拥有的有SLA的官方交易所API。

5层`fs`筛选器固定为：

| 分层 | 固定筛选器 |
| --- | --- |
| 沪主板 | `m:1 t:2` |
| 深主板 | `m:0 t:6` |
| 科创 | `m:1 t:23` |
| 创业 | `m:0 t:80` |
| 北交 | `m:0 t:81 s:2048` |

每层默认1页，每页最多100条；最多每层2页、合计10请求/1000条。使用代码升序，不按返回的候选数冒充全市场完整性。检查逐页长度、总量一致性、重复ID、代码顺序和分层身份，任何页失败则本次采集失败。

字段映射：`f2`最新价、`f3`涨跌幅、`f6`人民币成交额、`f8`换手率、`f9`动态PE、`f23`PB、`f20/f21`人民币总/流通市值、`f12/f13/f14`身份、`f124`带日期的Unix报价更新时间。未被当前可信目录核验的合法来源证券计入 `unverified_records`，不进入筛选，也不计为条件失败。

实现参考了[AKShare的公开报价列表适配器](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_em.py)使用的接口类别，但本模块直接进行有界请求、验证和单位转换，不导入AKShare进行整市场扫描。具体公开端点参数以本模块固定配置和实际响应核验为准。

## 请求与错误边界

两种来源都只执行固定域名的GET请求。连接等待最多3秒，单次读取等待默认6秒、最多10秒；本次采集的合作式总截止默认25秒、最多30秒。超时不会强制终止已经发生的远端工作。

每个响应最多200000字节，总响应最多2000000字节。关闭响应对象，禁重定向，检查流式大小，不重试、不自动换源。原有取消和回合截止检查同样生效。

异常遵循现有项目契约：

- `InvalidRequestError`：不支持的字段、条件、排序、上限或可信配置。
- `DataValidationError`：身份目录不合格、重复或错配身份、协议改变、数值/时间无效、响应超限等。
- `DataSourceError`：来源连接、HTTP或采集截止失败。返回固定安全错误描述，不透出底层连接异常中的任意文本。

## Windows终端操作

在项目根目录运行，离线演示不需要模型密钥：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.screening_cli --demo --condition pe_dynamic gt 0 --condition pe_dynamic lte 10 --sort-by amount_cny
```

增加 `--json`可查看逐条件、单位和覆盖字段。无参数仅显示帮助，不访问网络。

使用默认腾讯来源：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.screening_cli --live --directory data/security_directory_live.json --condition pe_dynamic gt 0 --condition pe_dynamic lte 20 --condition amount_cny gte 100000000 --sort-by amount_cny --limit 10
```

显式选择东财来源：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.screening_cli --live --source eastmoney --directory data/security_directory_live.json --condition turnover_percent gte 2 --sort-by amount_cny --json
```

真实模式要求至少一个明确条件及可信目录。`--per-stratum`只用于腾讯目录采样，`--pages-per-stratum`只用于东财列表分页。安装现有`.[data]`或`.[model]`中的`requests`即可运行采集，核心筛选和人工样例不需要网络依赖。

## 正常聊天接线

`screening_agent.py`用一次受预算约束的模型调用提取条件，`ResearchApiService.message`再调用服务端固定的报价源。模型给出的字段、比较方向、数值、单位、排序、数量和原文片段都必须通过核对，不能修改阈值或漏掉条件。字段顺序目前采用“字段→比较符→数字”，复杂、反向或歧义表达会请用户重述，不扩大样本范围后继续。

只支持AND与单字段排序。OR、范围、行业/板块过滤、指定股票名单、财报/技术信号条件和非动态PE口径等尚未接入；应用会在取数前说明缺口。泛泛选股没有阈值时，明确默认正价格样本、成交额降序、5只，通过`applied_defaults`传递活跃候选标记，网页不会隐藏该默认解释或把它当作潜力评分。

对于完整匹配的简单无过滤问句，如“帮我选几个潜力股”或“选出10只股票”，这些默认值与数量由程序决定，不让模型的clarify提示或自行添加的阈值覆盖它们；其他请求继续核对模型提取与原问题。数量仍限定1–20，带行业、风险、板块等限定的句子不能借此获得默认筛选。

筛选开始前消费旧研究基准，后续“它”不会误指上一次公司。名单暂没有专门的追问记忆，可直接输入某只股票名称研究。授权、幂等重放、同编号冲突、取消和超时共用原会话服务；取消/超时后不发布迟到名单。Dev服务强制隔离正常来源，不只依赖启动器恰好没传在线对象。

## 验证记录与目前的限制

`tests/test_screening.py`与`tests/test_screening_source.py`全部使用固定数据或注入HTTP对象，覆盖条件边界、缺值、负估值、排序、日期、采样、批次、身份、单位转换、协议解析、超限、失败处理及CLI；测试套件不会访问真实行情或模型。

2026-10-26额外做过以下真实协议验证，未保存或提交行情文件：

- 腾讯单次请求返回沪深、科创、创业及北交5只证券的完整数据；据原始索引核对PE、PB、成交额、市值和时间戳。
- 使用当前5224项可信目录，默认4层各50只，共200只身份及报价全部通过；一次实测用时3.27秒。条件 `0 < 动态PE <= 20`在这200只中匹配48只，按成交额展示10只。这不是全市场命中数。
- 东方财富首次小批响应返回HTTP200和所需字段；后续请求出现远端断开连接，按契约显式失败。因此不能保证该公开源持续可用。

尚未实现全市场完整性证明、历史截面回放、真实上市日期筛选、行业与财务报告条件、新闻事件全市场过滤、多字段混合评分或实时推送。新股/ST不默认排除不意味着已经验证了这些状态。需要更全面条件时，应新增可信字段和独立测试，而不是把缺失字段交给模型猜测。

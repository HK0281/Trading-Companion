# 真实证券背景与A股规则解释

更新：2026-10-26。实现范围为真实公开接口适配器、可保存的来源快照和纯规则解释函数；会话、模型证据和前端由产品层组合。

## 数据流

```text
已核实的股票ID（最多3只）
  → ExchangeMarketContextSource.read_snapshot(instrument_ids=...)
  → 固定交易所接口 / 严格身份与完整性校验
  → MarketContextSnapshot v2
  → current_security_background（明确当前观测与历史状态的区别）
  → explain_market_rules（按分析日期选择政策版本）
  → 产品层引用说明或归档
```

模块不调用大模型，不读取API密钥，不请求或提交交易。科创板、创业板、ST及新上市股票都保留在结果中；未知分类返回unknown。

| 文件 | 职责 |
| --- | --- |
| `src/trading_companion/market_context.py` | 兼容v1、解析v2来源记录、当前观测投影、原请求绑定和日历对照 |
| `src/trading_companion/market_context_live.py` | 官方元数据纯解析、可注入HTTP适配器、显式刷新CLI和原子保存 |
| `src/trading_companion/market_rules.py` | 有版本、生效日期和官方链接的规则解释；不依赖网络 |
| `tests/test_market_context_live.py` | 离线接口样例、时间边界、来源完整性、保存与规则版本验证 |

## 来源与覆盖

| 字段 | 来源与处理 |
| --- | --- |
| 沪市板块 | 官方主板A股、科创板两个已校验列表片段的STOCK_TYPE；不从代码前缀推断业务板块 |
| 沪市上市日 | 同一官方列表的LIST_DATE；支持YYYYMMDD，缺失保留null，非法日期拒绝 |
| 深市板块、上市日 | 官方ShowReport报表的板块、A股上市日期；按股票过滤时使用其JSON接口 |
| 沪市ST/*ST | 官方风险警示名单的正向成员；类别由名单中的官方简称标记细分，不能只凭普通名称目录猜测 |
| 名单中未出现 | 保持unknown，不能把未取得风险证据解释为“确定非ST” |
| 深市ST、停复牌 | 本增量没有接入能够独立核实的状态接口，显式unknown |
| 交易所日历 | 本增量没有取得可靠完整日历，快照calendars为空并明确not_fetched；不按工作日推断开市 |
| 北交所 | 当前适配器不抓取；规则表暂不覆盖，保留unknown，不据此把股票从其他研究范围剔除 |

固定公开入口：

- [上交所股票与存托凭证列表](https://www.sse.com.cn/assortment/stock/list/share/)，查询ID `COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L`。
- [上交所风险警示板](https://www.sse.com.cn/disclosure/listedinfo/riskplate/)，查询ID `PL_SSGSXX_FXJSBGPLB`；科创板风险名单查询ID `SSE_PL_SSGSXX_KCBFXTS_L`，`type=0`。
- [深交所股票列表](https://www.szse.cn/market/product/stock/list/index.html)，报表 `CATALOGID=1110 / TABKEY=tab1`；按股票路径固定访问 `https://www.szse.cn/api/report/ShowReport/data`。

按股票获取最多3只：深市每只一次已过滤的官方JSON请求；沪市使用两个完整列表片段和两个可降级的风险名单请求。JSON分页声明、股票代码和报表业务日期必须匹配。没有自动抓取全市场146页，没有把分页第一屏当完整市场。

不提供instrument_ids时保留整表刷新路径：两个沪市列表加深市XLSX导出。三块上市元数据必须全部成功才保存；可选沪市风险名单失败时保留上市字段并明确风险状态未知。2026-10-26的真实探测中，深市XLSX下载失败，而按股票JSON查询可用；两条模式是显式选择，不是悄悄切换提供方。

## 时间与真实性

v1人工夹具和现有本地快照继续原样解析。v2增加：

- `SecurityState.observed_at`：抓取时间；`st_reference`：另一个风险名单来源引用，可为null。
- 根级 `source_records`：`reference / source / source_url / as_of_date / retrieved_at / published_at / publication_precision / field_scope / coverage`。
- 接口没有可靠发布时间时，`published_at=null`、`publication_precision=unknown`，不会用抓取时间冒充发布时间。
- `as_of_date`是来源标注的业务日期。深交所清晨仍可能展示上一交易日的报表；来源日期和抓取上海日期分开保存。来源业务日期只接受距采集0–7天。

在线状态只覆盖来源业务日期这一天，不能创建跨月历史区间。`build_market_context`仍按原分析日和可用时间过滤。独立的`current_security_background`可以展示“当前取得的背景”，但其`analysis_date_st_status`只有分析日、来源日期、抓取上海日一致时才可能给出已核实ST；否则为unknown。来源较晚取得不能证明过去当时就拥有这条记录。

例如：10月27日抓取、报表日期10月26日，可以显示“来源报表截至10月26日，10月27日取得；板块和上市日为当前报表所示”，不能说“已验证10月26日历史ST”。当前观察接口还有7天新鲜度上限，超期或未来快照返回unknown。

来源链接限制到固定交易所HTTPS域名；解析检查不能证明本地文件未经人为篡改，因此公共背景仍沿用`unverified_local_snapshot`，并保留`historical_availability=not_established`。本模块不是PIT版本数据库。

## 规则表

规则核实截止日期为2026-10-26；更晚日期不会自动沿用为已核实。当前表仅覆盖2023-04-10及之后沪深A股，之前返回规则版本未覆盖。表的覆盖起点不表示所有条款在该日首次出现。

| 版本适用区间 | 主板普通日 | 主板风险警示普通日 | 科创板、创业板普通日 | 首次公开发行上市初期 |
| --- | --- | --- | --- | --- |
| 2023-04-10—2026-07-05 | 10% | 5% | 20%，不直接套用主板ST比例 | 前5个交易日无涨跌幅限制 |
| 2026-07-06起，核实至2026-10-26 | 10% | 10% | 20% | 前5个交易日无涨跌幅限制 |

主板ST比例调整在2026-04-24公布，**2026-07-06生效**；不能在公布日就把旧比例改成10%。规则函数分别返回`policy_published_date`、`publication_precision=day`和`policy_effective_from/to`。

官方可核实依据：

- [上海证券交易所交易规则（2026年修订）及生效通知](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)。
- [上交所2026年交易规则修订说明](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml)，明确主板风险警示比例调整和实施日期。
- [深圳证券交易所交易规则（2026年修订）通知](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)及[正式附件](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)：3.1.4、3.3.13、3.3.15等。
- [上海证券交易所交易规则（2023年修订）通知](https://www.sse.com.cn/lawandrules/sselawsrules/stocks/exchange/c/c_20230216_5716380.shtml)。
- [深圳证券交易所交易规则（2023年修订）通知](https://www.szse.cn/lawrules/rule/stock/trade/t20230217_598773.html)。
- [深交所对新股上市首五日交易机制的官方解释](https://www.szse.cn/www/investor/knowledge/t20230509_600303.html)。
- [上交所对科创板风险警示股票20%比例的官方说明](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20240412_10753148.shtml)。

规则结果区分：

- `ordinary_daily_limit_percent`：在普通竞价日、给定板块和风险分类下的条件性基准。
- `applied_daily_limit_percent`：只有上市交易日序号、普通/特殊事件等输入齐全时才给出；默认null。
- `price_limit_status`：普通、条件性普通、IPO无涨跌幅限制、特殊事件无涨跌幅限制或unknown。
- `listing_trading_day`：必须由独立可信的交易日历等依据提供，不能由自然日或K线数量替代。适配器日历缺失时不自动计算。
- `same_day_resale`：解释普通A股T+1卖出限制；不判定账户可卖余额，也不推导资金取用规则。

没有涨跌幅限制不代表没有临停、有效申报价格范围或其他交易约束。规则函数不判断涨停价、委托是否有效、个人账户权限、保证成交或实际可交易性；返回`tradability=not_assessed / execution_allowed=false`。缺日线也不能用于推断停牌。

## Python接口

```python
source = ExchangeMarketContextSource(timeout_seconds=10)
snapshot = source.read_snapshot(instrument_ids=("000001.SZ",))

background = current_security_background(
    snapshot,
    instrument_id="000001.SZ",
    analysis_date=analysis_date,
    reference_at=reference_at,
)

rules = explain_market_rules(
    board=verified_board,
    st_status=verified_analysis_date_st_status,
    as_of_date=analysis_date,
    listing_date=verified_listing_date,
)
```

代码导入路径分别为`trading_companion.market_context_live`、`trading_companion.market_context`、`trading_companion.market_rules`。`transport`和`clock`可注入，测试全部使用固定内存响应；不需要新API key。

## Windows刷新

无`--live`只输出未请求，不进行网络或文件写入：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.market_context_live
```

按股票获取并原子保存到已忽略的本地路径：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.market_context_live --live --instrument 000001.SZ --output .\data\market_context_local.json
```

可以重复`--instrument`，最多3只。若要显式整表刷新，移除`--instrument`；整表模式遇到必需接口失败时保留原文件。不能覆盖人工夹具、无关JSON或符号链接目标。

离线验证：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_market_context_live.py tests/test_market_context.py tests/test_market_context_fixtures.py -q
```

仍未完成的是历史状态版本库、深圳ST专门状态源、真实完整交易日历、公司行动、盘中临停及逐笔交易规则验证。这里将缺失直接作为数据结果暴露，不用名字、代码或工作日猜测补齐。

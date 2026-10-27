"""Maintainer-written educational notes with explicit reference provenance.

These are local explanations, not copied source excerpts or live rule checks.
Python data ships in the wheel without a separate package-data configuration.
"""

CORPUS_VERSION = "trading-knowledge-2026-10-26-v1"
MAINTAINED_ON = "2026-10-26"

# key, title, publisher, authority, reference version, URL
SOURCES = (
    ("sse_rules", "上海证券交易所交易规则（2026年修订）发布页", "上海证券交易所",
     "official_reference", "2026年修订发布页；非实时条文核验",
     "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml"),
    ("sse_star", "科创板投资者教育问答", "上海证券交易所", "official_reference", "网页参考；未固定全文版本",
     "https://edu.sse.com.cn/tib/qa/"),
    ("sse_risk", "风险警示板说明", "上海证券交易所", "official_reference", "网页参考；未固定全文版本",
     "https://www.sse.com.cn/disclosure/listedinfo/riskplate/"),
    ("szse_dividend", "除权、除息的定义", "深圳证券交易所", "official_reference", "2019-01-31投教页面",
     "https://investor.szse.cn/institute/video/classroom/operation/t20190131_564541.html"),
    ("sec_statements", "Beginners' Guide to Financial Statements", "美国证券交易委员会",
     "official_reference", "2007-02-04投资者教育；概念参考而非A股会计准则",
     "https://www.sec.gov/investor/pubs/begfinstmtguide.htm"),
    ("investor_pe", "Price-earnings (P/E) Ratio", "Investor.gov / 美国证券交易委员会",
     "official_reference", "网页参考；未固定全文版本",
     "https://www.investor.gov/introduction-investing/investing-basics/glossary/price-earnings-pe-ratio"),
    ("amf_ratios", "Principal ratios for evaluating shares", "魁北克金融市场管理局",
     "official_reference", "网页参考；通用财务概念",
     "https://lautorite.qc.ca/en/general-public/investments/investing-on-your-own-its-not-for-novices/do-you-know-the-principal-ratios-for-evaluating-shares"),
    ("idx_ratios", "Stock Screener Column Explanation", "印度尼西亚证券交易所",
     "official_reference", "网页参考；筛选器口径不等于A股加权ROE口径",
     "https://www.idx.id/en/investor/stock-screener-column-explanation-en/"),
    ("investor_diversification", "Asset Allocation and Diversification", "Investor.gov / 美国证券交易委员会",
     "official_reference", "网页参考；通用风险概念而非A股规则",
     "https://www.investor.gov/introduction-investing/getting-started/asset-allocation"),
    ("investor_orders", "Understanding Order Types – Investor Bulletin", "Investor.gov / 美国证券交易委员会",
     "official_reference", "美国订单示例；不能直接移植为A股券商订单功能",
     "https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-14"),
    ("investor_fees", "How Fees and Expenses Affect Your Investment Portfolio", "Investor.gov / 美国证券交易委员会",
     "official_reference", "网页参考；未提供当前中国费率",
     "https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/updated"),
    ("project_indicators", "本项目指标计算实现", "Trading Companion维护者", "project_reference",
     "technical-analysis-v1；链接指向可变main分支",
     "https://github.com/HK0281/Trading-Companion/blob/main/src/trading_companion/indicators.py"),
    ("project_technical", "本项目技术分析设计", "Trading Companion维护者", "project_reference",
     "2026-10-26技术设计；链接指向可变main分支",
     "https://github.com/HK0281/Trading-Companion/blob/main/docs/technical-analysis-design.md"),
    ("project_architecture", "本项目数据与研究边界", "Trading Companion维护者", "project_reference",
     "2026-10-26维护说明；链接指向可变main分支",
     "https://github.com/HK0281/Trading-Companion/blob/main/docs/architecture.md"),
)

# id, category, title, search aliases, maintainer explanation, reference key
ENTRIES = (
    ("a_share_t1", "market_rules", "A股T+1与交易日", ("t+1", "t+0", "当天买", "当日买", "交易日"),
     "普通A股常说的T+1，是当日买入的股票通常不能当日卖出；T表示交易日，不是任意自然日。"
     "它与卖出后资金何时可用、何时可取不是同一问题。ETF、可转债等品种可能不同，不能把普通股票规则套给所有证券。",
     "sse_rules"),
    ("a_share_lots", "market_rules", "整手、零股与申报数量", ("一手", "整手", "零股", "100股", "申报数量", "最小买入"),
     "一手、最小申报量和申报增量是不同概念。普通主板股票常见整手申报，科创板等板块的最小数量和增量另有安排；"
     "卖出零股也有专门规则。教学示例中的理论股数不能直接当作任何板块都能提交的订单数量。",
     "sse_star"),
    ("a_share_limits", "market_rules", "涨跌幅限制与新股", ("涨跌停", "涨停", "跌停", "涨跌幅", "新股", "新上市", "科创板", "创业板"),
     "涨跌幅限制需同时核对交易所、板块、上市阶段、风险警示状态及规则生效日，不能用一个百分比覆盖全部A股。"
     "新上市阶段可能有特殊安排；没有价格涨跌幅限制也不等于没有有效申报价格范围、临时停牌或流动性风险。",
     "sse_rules"),
    ("a_share_st", "market_rules", "ST与退市风险警示", ("st", "*st", "风险警示", "退市"),
     "ST和*ST用于提示不同类别的风险警示，不能只凭简称判断具体触发原因、当前交易安排或是否必然退市。"
     "需要结合当时公告、板块规则和实施日期核实；不同板块的风险警示交易安排可能不同，历史资料可能已经过期。",
     "sse_risk"),
    ("a_share_suspension", "market_rules", "停牌、缺失行情与流动性", ("停牌", "复牌", "查不到行情", "缺失行情", "流动性"),
     "日线缺一条可能来自非交易日、供应商漏数、代码问题或停牌，不能仅凭缺失记录断言停牌。"
     "应核对交易所公告与交易日历。没有成交或排队无法成交也不等于已停牌；流动性不足会让计划价格和实际成交偏离。",
     "sse_rules"),
    ("adjusted_prices", "market_rules", "除权除息与前后复权", ("前复权", "后复权", "复权", "除权", "除息", "分红"),
     "分红送转等权益变动会影响价格比较。前复权、后复权是数据商调整历史序列的方式，锚点和因子口径会影响读数；"
     "复权价格不应直接当作当时可成交价。比较指标与回测时应统一口径，并另外处理现金分红和股数变化。",
     "szse_dividend"),
    ("financial_statements", "financial", "三张主要财务报表", ("财报", "财务报表", "报表", "资产负债", "利润表", "现金流量表"),
     "资产负债表观察期末资产、负债和权益；利润表描述一段期间的收入、费用及利润；现金流量表解释现金的流入流出。"
     "读财报应把三张表和附注联系起来。此处是通用会计概念，不替代中国会计准则或具体公司的审计意见。",
     "sec_statements"),
    ("valuation_pe", "financial", "PE、市盈率与每股收益", ("pe", "p/e", "市盈率", "eps", "每股收益", "估值"),
     "PE通常是股价除以每股收益。静态、滚动和预测PE的分母区间或假设不同；亏损、一次性收益和周期高点会让倍数失去直观意义。"
     "低PE本身不证明便宜，应结合盈利持续性、行业和会计口径；假设数字只能作为算例，不能代替当前个股事实。",
     "investor_pe"),
    ("valuation_pb", "financial", "PB、市净率与资产质量", ("pb", "p/b", "市净率", "净资产", "账面价值"),
     "PB通常是股价除以每股净资产，观察市值相对账面权益的倍数。账面金额不是随时可变现价值，减值、负债和无形资产会影响解释。"
     "PB与PE分母不同，应结合资产质量和盈利能力，不能把PB低于1直接解释为无风险折价。",
     "amf_ratios"),
    ("financial_roe", "financial", "ROE与杠杆", ("roe", "roa", "净资产收益率", "杠杆", "杜邦"),
     "ROE反映利润相对权益的回报，平均权益、期末权益和加权ROE的计算口径可能不同。"
     "高ROE可能来自经营效率，也可能来自高杠杆、小权益基数或一次性利润；比较时核对期间、归属口径和异常项目。",
     "idx_ratios"),
    ("profit_and_cash", "financial", "利润不等于经营现金流", ("经营现金流", "现金流", "净利润", "盈利质量", "应收账款"),
     "收入和费用的确认时点不一定与收付款相同，所以净利润与经营现金流可能不同。"
     "分析差异时可查看应收、存货、预收预付和非现金项目；单期背离既不能直接证明造假，也不能忽略持续收现困难。",
     "sec_statements"),
    ("financial_periods", "financial", "报告期、同比与披露时点", ("同比", "环比", "报告期", "披露", "营业收入", "营收", "增长率"),
     "同比应比较相同长度、相同口径的期间，环比比较相邻期间；低基数或负基数会影响增长率含义。"
     "报告期结束日不等于披露日。历史研究只能用当时已经披露的资料，并留意后续更正；当前供应商返回值不自动证明历史可得。",
     "project_architecture"),
    ("position_sizing", "risk", "按风险预算计算理论仓位", ("仓位", "头寸", "本金", "风险预算", "position sizing"),
     "一种教学方法是先设单笔可承受损失，再除以假设入场价与止损参考的每股差额，得到理论股数。"
     "该方法是维护者解释，不是监管推荐比例；还要考虑总敞口、相关性、费用、整手规则及跳空，止损价不保证成交。",
     "project_technical"),
    ("diversification", "risk", "分散、相关性与总风险", ("分散", "相关性", "组合风险", "资产配置", "集中持仓"),
     "分散投资是避免把风险集中在少数暴露上，不能只数股票数量：同一行业或同一驱动的资产可能一起下跌。"
     "分散不能消除市场整体风险；仓位安排应结合时间跨度、资金需求和能承受的损失，而不是套用固定安全比例。",
     "investor_diversification"),
    ("drawdown", "risk", "回撤与回本所需涨幅", ("回撤", "回本", "收益率", "复利"),
     "回撤通常衡量从此前净值高点到后续低点的相对下降，不等于单笔亏损。"
     "若净值下降比例为d，回到原值需要的涨幅是d除以1-d；应标明是否计入现金流、费用及分红。历史最大回撤不是未来损失上限。",
     "project_architecture"),
    ("stop_and_execution", "risk", "止盈止损计划与成交风险", ("止损", "止盈", "滑点", "跳空", "限价", "止损单"),
     "止盈止损参考是预先描述应对条件，不等于已经提交或能够按该价格成交。跳空、流动性和价格限制可能让实际损失扩大。"
     "美国投教中的stop订单概念不能直接当成A股券商支持的订单类型，具体功能与触发机制需另行核对。",
     "investor_orders"),
    ("expectancy_costs", "risk", "胜率、盈亏比与费用", ("胜率", "盈亏比", "期望收益", "手续费", "印花税", "费用", "交易计划"),
     "维护者教学公式：单笔平均结果可近似分解为胜率乘平均盈利，减败率乘平均亏损，再扣成本。"
     "高胜率可能被少数大亏抵消，样本过少也不可靠。真实成本包括佣金、适用税费和滑点；此片段不提供当前费率或收益保证。",
     "investor_fees"),
    ("technical_macd", "technical", "MACD位置关系不等于交叉", ("macd", "dif", "dea", "金叉", "死叉", "柱值"),
     "本项目DIF为快EMA减慢EMA，DEA为DIF的EMA，柱值采用2乘DIF与DEA之差。"
     "DIF高于DEA只是当前关系，判断上穿需要前后有效记录；不同库的初始化和柱值口径可能不同，金叉不等于未来必涨。",
     "project_indicators"),
    ("technical_rsi", "technical", "RSI超买超卖的常见误读", ("rsi", "超买", "超卖", "背离"),
     "RSI描述一定窗口上涨与下跌变化的相对强弱。本项目采用Wilder平滑。"
     "高读数不是立即卖出的命令，强趋势可以持续高位；低读数也不保证反弹。背离应说明极值确认方法与时间，不能把候选当作反转概率。",
     "project_indicators"),
    ("technical_adx", "technical", "ADX强度与DI方向", ("adx", "+di", "-di", "趋势强度", "平均趋向"),
     "ADX衡量趋势强度，不直接给上涨或下跌方向；方向可结合+DI与-DI及价格观察。"
     "本项目使用Wilder平滑，DI与ADX的预热长度不同；参考阈值不是胜率，历史不足也不能补成零。",
     "project_indicators"),
    ("technical_obv", "technical", "OBV不是资金流入金额", ("obv", "能量潮", "资金流", "量价"),
     "OBV按收盘涨跌对成交股数作加减。本项目第一条记录以零起算，绝对值依赖快照起点，负值也不等于资金净流出金额。"
     "宜结合走势和同口径区间观察；不能把OBV与成交额或真实账户资金流混为一谈。",
     "project_indicators"),
    ("technical_vwma", "technical", "VWMA、均线与零成交量", ("vwma", "成交量加权", "均线", "sma", "ema"),
     "VWMA用窗口内成交股数对收盘价加权，SMA给各条记录相同权重，EMA使用递推权重。"
     "本项目VWMA要求完整窗口，总成交量为零则不可定义，不能填零；这些均线是历史平滑，不能单独证明未来方向。",
     "project_indicators"),
    ("technical_combinations", "technical", "指标组合、布林带与ATR", ("指标组合", "指标搭配", "技术指标", "boll", "布林", "atr", "动量", "波动指标", "真实波幅"),
     "多指标应提供互补信息，而不是把同一价格序列的相关变化当作独立投票。"
     "布林带描述相对均值与波动，ATR描述真实波幅而非上涨方向；可联合趋势、动量、量能检查冲突，但不能用指标数量换算胜率。",
     "project_technical"),
    ("replay_biases", "research_methods", "回测、前视与幸存者偏差", ("回测", "前视", "幸存者", "过拟合", "历史数据", "point-in-time"),
     "历史测试需只使用当时可得的数据与证券名单，避免把后来发布或修订的信息提前使用；只测试存活公司会漏掉退市样本。"
     "反复调参也可能只记住过去。日期过滤、离线契约测试或漂亮曲线都不能单独证明严格历史可得或未来有效。",
     "project_architecture"),
)

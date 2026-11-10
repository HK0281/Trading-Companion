# 有来源的交易知识问答

更新：2026-11-09。一般交易问答默认先检索本地知识片段，再用一次无工具模型调用解释。原有指标定义快捷路径保持零模型调用；本文不把知识说明扩展为个股实时研究或交易操作。

## 1. 解决的问题与流程

原来的`open_knowledge.py`只把问题交给模型，虽然限制了格式和范围，但无法指出回答参考了什么。现在使用随Python包发布的24条维护者说明，先选相关片段，再要求模型逐段引用本轮片段别名。

```mermaid
flowchart LR
    Question[通过原知识路由的问题] --> Search[本地别名门控与BM25排序]
    Search --> Context[最多4条版本化片段]
    Context --> Model[一次模型调用 无工具]
    Model --> Check[格式 范围 逐段引用成员检查]
    Check -->|通过| Answer[自然段与可追溯出处]
    Check -->|不通过| Fallback[简短应用回退]
```

`knowledge_data.py`保存内容与参考元数据；`knowledge_retrieval.py`负责确定性检索；`open_knowledge.py`组织上下文、检查输出和生成公开答复。没有新增向量服务、数据库、抓取任务或模型提供方，也不自动访问片段里的链接。

## 2. 知识范围与来源含义

| 分类 | 当前内容 |
| --- | --- |
| A股交易概念 | T+1、申报数量、板块/新股涨跌幅差异、ST、停牌与缺失行情、除权复权 |
| 财务概念 | 报表关系、PE、PB、ROE、利润与现金流、报告期及披露时点 |
| 风险与计划 | 理论仓位、分散与相关性、回撤、止损与成交风险、胜率/盈亏比/成本 |
| 技术指标 | MACD位置与交叉、RSI、ADX、OBV、VWMA、布林带/ATR及指标相关性 |
| 研究方法 | 前视偏差、幸存者偏差、过拟合与历史可得性 |

所有中文片段均为`content_origin=maintainer_explanation`，并非官方原文引述：

- `source_type=official_reference`：维护者说明附有交易所或监管机构的参考页面。例如上交所交易规则发布页、SEC/Investor.gov投资者教育、AMF及印度尼西亚交易所的通用财务概念说明。引用外国投教不表示将外国交易规则移植到A股。
- `source_type=project_reference`：维护者说明引用本项目的计算实现或设计文档。例如MACD柱值、OBV起点、仓位教学公式与历史研究边界。

每条保留本地内容版本、维护日期、标题、说明片段、参考标题、发布方、链接和参考版本说明。外部网页未存全文或完整历史版本；项目链接指向可变的`main`分支。`live_verified=false`明确表示本次问答没有实时核验链接或规则有效性，维护日期也不是外部资料采集时间。

片段避免给所有A股套用同一个涨跌幅百分比或最小申报量。具体证券、板块、生效日和订单能力仍需另行核实；本模块不执行这些核实。

## 3. 检索为何这样实现

24条内容无需先引入外部向量库。当前算法为`alias-gated-cjk-bigram-bm25-v1`：

1. NFKC归一化全角字符，统一大小写、`P/E`与`PE`、`T + 1`等写法。
2. 先匹配精选主题别名，避免把弱相关的字词当成出处。例如未收录DCF时，不因问题里也有一个泛化词就提供无关片段。
3. 中文使用相邻双字词元，英文保留词元，按BM25及命中别名排序；同分用稳定内容ID排序。
4. 最多返回4条，绑定本轮`K1`等别名。若真实序列化后的模型消息超过既有协议限额，按排名移除整条低优先片段，不截断一句解释或悄悄扩大限额。

这是可解释的词法检索，不是语义召回或规则自动更新。没有匹配时仍可做一次通用概念解释，但由应用在正文开头明确提示本地知识库未覆盖；不补造来源。自定义`KnowledgeRetriever(())`可用于测试这种路径。

## 4. 模型与公开接口

旧调用保持兼容：

```python
answer_general_knowledge(question, model=model, directory=directory, mentions=())
```

新增可选`retriever`用于注入本地语料，不赋予浏览或市场权限。原有知识路由仍先拒绝个股数值查询、保存结果追问和交易执行请求。

消息顺序保持现有协议：系统指令、原始用户问题、`LOCAL_KNOWLEDGE_CONTEXT\n`开头的应用资料、最终格式请求。资料以数据处理，不作为新的系统指令；模型不能执行其中内容。

输出仍使用`general-knowledge-answer-v1`和字符串段落，新增逐段引用：

```json
{
  "version": "general-knowledge-answer-v1",
  "paragraphs": ["PE与PB观察不同分母，比较时需要统一口径。"],
  "citations": [{"paragraph": 0, "sources": ["K1", "K2"]}]
}
```

命中资料时每段必须引用至少一条已送入本次模型上下文的片段；索引从0开始。拒绝未知别名、漏引段落、重复引用、错误索引、额外字段和正文内另造的链接或引文标记。没有命中时引用必须为空；旧的无`citations`模型JSON仅在这个路径兼容。任何不合格输出直接回退，不重试或二次取数。

`GeneralKnowledgeAnswer`原位置参数及`to_dict()`的版本、状态、正文、段落、无行情/无交易字段继续存在。公开`citations`从空列表扩展为维护者片段与可信元数据，按片段聚合`paragraph_indices`；可选`retrieval`给出语料版本、分类、命中情况和无网络标记，不暴露原始问题或模型内部响应。

公开答复的`validation_scope`为`format_scope_and_citation_membership_not_prose_semantics`。引用链接和片段只能来自程序已知资料，不能由模型生成。返回字典和列表均与内部冻结对象分离，客户端修改不会改动保存结果。

## 5. 验证与边界

验证覆盖主题检索、别名和全角写法、未知主题、稳定排序、不可变结果、版本/来源区分、逐段引用、未知或重复别名、无匹配路径、协议容量、取消、共享调用预算及既有API/启动/快照路径。只使用假模型和本地内容，不新增真实模型或行情请求。

仍有明确边界：成员检查只证明引用确实在本次检索结果中，不能证明自由文字的每个结论由片段推出。假设算例没有全面数值推导认证，词法召回也可能漏掉近义表达。现有24条内容并非完整知识库、实时法规服务或基于全文的检索系统；真实回答相关性与解释质量需要独立抽查。

扩充语料时应逐条核对参考链接与解释，区分维护日期和外部发布时间，修改内容版本，并给重要近义问题补检索案例。不要通过给所有主题添加泛化别名来提高表面的命中率。

## 6. 仅记录类别的拒绝诊断

知识结果的 `to_dict()` 新增 `diagnostics` 字段，既有回答版本、状态、大类错误码和失败提示保持不变。例如：

```json
{
  "version": "knowledge-validation-v1",
  "state": "rejected",
  "reason": "knowledge_bracket_reference"
}
```

`state=accepted` 表示输出检查通过；`rejected` 表示输出检查失败；`not_run` 表示还没有检查输出，例如检索失败、模型拒答或返回不完整。前后两种非拒绝状态的 `reason` 为 `null`。拒绝原因只取下面的固定白名单，未知值归为 `other`，不返回问题、模型正文、引用别名、命中字词或异常详情。返回的字典与内部对象分离。

每个原因均以 `knowledge_` 开头，后缀如下：

| 类别 | 固定原因后缀 |
| --- | --- |
| 响应文本 | `response_type_invalid`、`response_too_long` |
| JSON 解析 | `json_invalid`、`json_too_deep`、`json_duplicate_key`、`json_nonfinite_number` |
| 顶层结构 | `payload_type_invalid`、`payload_keys_invalid`、`version_invalid`、`paragraphs_type_invalid`、`paragraph_count_invalid` |
| 段落格式 | `paragraph_type_invalid`、`paragraph_empty`、`paragraph_too_long`、`paragraph_nonprintable` |
| 内容范围 | `html_forbidden`、`link_forbidden`、`claimed_access`、`guaranteed_return`、`current_number_claim`、`bracket_reference` |
| 引用集合 | `citations_type_invalid`、`citation_count_invalid`、`citation_shape_invalid` |
| 段落索引 | `citation_index_invalid`、`citation_index_duplicate` |
| 引用来源 | `citation_sources_type_invalid`、`citation_source_count_invalid`、`citation_source_unknown`、`citation_source_duplicate`、`citation_missing` |
| 无匹配声明 | `no_match_notice_too_long` |

共 32 类，按原来的检查顺序只报告首次拒绝点；不能据此认为其余规则都已通过。重复 JSON 键与非有限数字的原因不会被通用解析错误覆盖。现有 `check/knowledge_answer` 追踪事件使用同一原因类别，仍走固定白名单；原有追踪格式和旧记录读取方式不变。

本阶段只补诊断，没有放宽规则或增加生产模型调用。例如【计算步骤】、EMA[12] 仍会被原来的引用标记规则拒绝，段内换行仍被拒绝；12/26/9、DIF/DEA、普通 EMA 递推式本身不会触发当前行情数字规则。有资料时仍要求每段引用本轮片段，无资料时仍要求空引用；引用检查不证明自由文字的真实性。

### 有界真实诊断

`scripts/diagnose_general_knowledge.py` 默认仅输出包含公开测试问题的计划，不读取模型配置、不联网、不创建目录或会话数据：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/diagnose_general_knowledge.py
```

固定批次是四种 MACD 问法各三次，加“ETF是什么”两次，共 14 题。只调用生产的通用知识实现，使用现有 DeepSeek 回答模型配置、提示词与知识资料；不调用意图模型、不改提示词、不保存对话、不创建行情数据源。该诊断不能替代新旧入口的完整验收，也不评价解释质量。

真实诊断预算由总线消息 #541 预先批准；第一阶段审核、提交后由 Claude 执行以下命令。输出目录必须全新且位于 `.agentbus/tmp/` 内，每个批次另取目录名：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/diagnose_general_knowledge.py --live --max-calls 14 --output .agentbus/tmp/macd-diagnostic-run1
```

`--live` 必须同时给出 `--max-calls` 和 `--output`；上限只能是 1–14，失败尝试也计数。每题只有一次模型额度，连接错误不重发、输出失败不重写，全局请求观察器在发请求前计数。达到上限后剩余题目标为 `not_measured`；中断或启动失败也保留完整的 14 题分母，不自动追加调用。

批次目录里的 `report.json` 仅保存 case_id、重复序号、状态、固定错误类别、调用次数、用量、耗时及 run_id，不保存计划中的问题文本、任何模型正文或 reasoning、引用别名、请求消息、凭据或异常原文。`summary.reason_by_case` 给出“拒绝原因码 × case_id”的次数表，`case_counts` 保留每种问法的计划数、通过数、失败数和未测数。缺失的 token 用量保留为 `null`，不当作零。

同目录 `trace/` 仅保存现有的无正文追踪元数据，可用终端工具汇总原因：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.trace_cli --trace-dir .agentbus/tmp/macd-diagnostic-run1/trace --format json
```

小批次未复现失败时，只能报告“本批未复现”；后续修复与追加真实调用另行讨论。诊断材料不会写入 Git。

当前源码登记更新为 `intent-knowledge-diagnostics-v1`，上一版 `intent-integration-default-on-v1` 完整移入历史登记。既有冻结题目、观察器、快照和历史报告不改写；当前回归测试先严格核对新增诊断字段，再移除这一已核对字段，与原始快照比较其余全部结果、状态和调用数。额外字段、错误诊断状态或正文变化仍会让测试失败。

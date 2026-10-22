# 安全回答展示：程序事实摘要与模型草稿分离

日期：2026-10-20（America/Los_Angeles）

后续同日更新：事实报告仍独立保持本文职责；另已实现[受限指标解读](grounded-explanation-design.md)，模型提交有界JSON，程序核对关系后生成中文。默认可展示 `display_answer`（事实＋通过的关系），只需事实则继续用 `safe_answer.text`。下文539项是报告阶段历史记录；新增54项解读测试后当前全量593项通过，不等于任意自由文本已获校验。

## 1. 解决的问题与本步范围

此前研究事实与 `model_draft` 已分开，但调用方仍需自行整理事实。模型即使说错股票、编出价格或保证上涨，也只被标记为未校验，没有独立的默认展示内容。

本步增加程序生成的 `safe_answer`：只使用本轮请求和确定性研究结果，展示价格、七类指标最新字段、缺项、来源、日期和限制；失败时提供无数值的固定说明。它不读取模型草稿，也不再联网、调用模型或计算一次指标。

**这不是自由文本回答校验。** 不解析模型语义，不将已存在的引用视为结论正确，不展示模型生成的走势判断，也未实现受限修正。`answer_validation=not_performed` 仍然成立。确定性关系的中文解读、结构化模型论断检查、知识回答及完整产品入口另行开发。

## 2. 文件与数据流

| 文件 | 职责 |
| --- | --- |
| [research_report.py](../src/trading_companion/research_report.py) | 纯函数投影、请求/证据一致性检查、字段状态和固定中文摘要 |
| [agent.py](../src/trading_companion/agent.py) | 通过 `safe_answer` 属性和JSON字段暴露报告；原状态及调用次数不变 |
| [research_tool.py](../src/trading_companion/research_tool.py) | 可选可信 `request_id`，让编排、分析及证据使用同一标识 |
| [report_demo.py](../src/trading_companion/report_demo.py) | 只打印离线事实摘要，不打印草稿的命令入口 |
| [test_research_report.py](../tests/test_research_report.py) | 报告边界、请求ID、失败回退、自然会话和运行入口测试 |

```text
已确认请求 → 受控执行器 → 本轮结构化研究结果
                               ├─ 模型解释 → model_draft（仅调试，未校验）
                               └─ 程序投影 → safe_answer（面向用户的事实摘要）
```

`build_research_report(result, expected_request=...)` 不接收模型文本。`AgentTurnResult.safe_answer` 按当前结果生成新报告；没有研究结果时为 `None`，由原来的消息说明澄清或执行前失败。即使模型解释失败，已保留的事实仍可生成报告。

## 3. 请求ID为什么需要衔接

原来解析条件与执行器创建分析请求时各生成一个ID，参数相同却不是同一个请求对象。若直接按ID严格绑定，正常执行也会被报告拒绝。

现在编排层将已确认请求的ID作为 `ResearchToolExecutor(request_id=...)` 的可信配置传入。执行器独立使用时不传该参数，仍自行生成ID；格式在执行前验证。模型工具参数仍禁止 `request_id`，不能借此挑选旧证据。

报告同时核对请求ID、股票、起止日期、来源及全部指标实例。仅允许已有契约认可的指标重排，输出仍按已确认顺序展示。ID用于关联，不是用户身份、授权令牌或幂等键。

## 4. 报告检查什么

- 结果必须属于本轮请求；来源、真实性标记与复权口径必须符合当前模式。
- 行情属于同一股票和区间，日期严格递增；最新字段与最新日线对齐。
- 每个请求指标必须存在，参数、算法版本和预期字段集合匹配；不能少报或重复实例。
- 可用数值必须有限且不是布尔值，有当前请求、股票、日期、字段、参数、单位和值一致的事实。最新收盘价及成交量也必须有对应证据。
- 缺失或重复事实ID、过往日期的替代值、错股票、旧请求，以及数值/单位不一致都会阻断整个数值报告，不展示混杂的部分。
- 不可用字段必须保持 `None` 和无引用，不能填零。历史不足显示缺少记录数；零均量倍率未定义与数值计算错误分别展示。

这是对已有服务结果的展示边界检查，不重新运行指标公式、不证明供应商数据真实，也不是接收任意Python对象的通用安全沙箱。

## 5. 展示与失败行为

报告的 `status` 为 `complete`、`limited`、`unavailable` 或 `blocked`，与Agent整轮状态不同。模型解释超时可能让Agent为 `limited`，但全部事实字段可用时，报告仍可为 `complete`；这不意味着模型解释通过检查。

- 人工CSV在首行标明“人工样例 / 非真实行情”；未验证CSV另行标注，不能冒充在线研究。
- 在线数据只称供应商历史日线，不称实时、经独立认证或完整市场覆盖。
- 显示请求区间、实际最新交易日及采集时间，三者不混同。未知采集时间不补造时间。
- 数值文本使用8位有效数字作显示；结构化字段保持原精度，不根据显示舍入后的值推导比较关系。
- 保留非严格point-in-time、未知历史风险状态、复权、量额精度、初始化及数据截止缺口等适用提醒。未知警告、异常文本和供应商自由文本不直接插入报告。
- 无数据不推断停牌、退市或证券不存在；源失败/无效数据不展示数值、不自动换源或重试。

报告导出包含 `origin=application`、`validation_scope=deterministic_report_only_not_model_prose`、逐字段原值和完整事实ID。可在同轮 `research_result.facts` 中追溯，不使用跨请求的短别名。

兼容调试的JSON仍包含 `model_draft`，并增加 `model_draft_policy=debug_only_unchecked_not_for_default_display`。调用方应默认展示 `safe_answer.text`，不能把整个调试JSON当作已经净化的产品输出。本步没有删除草稿，也没有保证任意客户端一定遵守该策略；专用演示只打印事实摘要。

## 6. 运行与验证

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.report_demo
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_research_report.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

演示沿用固定任务、Fake模型和5条人工CSV，返回SMA3/量能3事实与SMA20缺少15条记录的说明，不打印模型脚本草稿。没有自由输入交互或真实市场建议。

Windows演示显式使用 `-X utf8`，让中文输出及重定向采用UTF-8；不需要激活虚拟环境或改变系统执行策略。

2026-10-20，Windows / Python 3.11.9：新增40项测试，全量539项离线测试通过，覆盖七类指标、错误上下文、证据/单位/日期、缺项、模型及数据失败、指标重排、请求ID、自然会话与独立进程入口。没有读取真实凭据或请求真实模型、目录及行情；不能据此宣称真实模型的语言质量或预测能力已验收。

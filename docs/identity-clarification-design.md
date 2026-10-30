# 股票澄清续接：保留条件，不依赖模型记忆

2026-10-29 B批覆盖：候选接受当前显示顺序的无歧义序号，如“第一个”，也可用名称/代码附一次合法条件更新，如“平安银行，改看近60日”。选择与句尾分别解析后整句校验，候选外裸名称、越界序号和不能理解的尾句仍澄清，不丢弃后半句。完整具名新研究可开始替换任务；在每轮副本中执行，成功或有效新澄清发布才替换原pending，失败、预算耗尽和取消不提前清除。知识插问继续保留pending。下文只接完整名称、禁止序号/附加条件的限制为旧实现记录；明确“新问题：”仍保留原清空且不继承语义。

更新：2026-10-20（America/Los_Angeles）

2026-10-22增量：[明确换股票](condition-update-design.md)有未知或歧义身份时也复用本续接流程，保留旧研究日期/指标/来源；没有额外意图模型调用，目录与候选仍重查。日期及指标也可分别明确修改，不等于任意复合指代已实现。

2026-10-21后续能力：[明确指标修改](indicator-edit-design.md)增加独立confirmed条件槽，[结果追问与指标知识](result-followup-design.md)增加独立成功结果槽。待选身份不覆盖原confirmed或成功结果；取消只清pending，重置/退出清三者。本文其余部分记录身份续接的实现与历史验证。

## 1. 本步范围

支持如下两轮流程：

> 用户：看看平安最近一个月走势，只看SMA3和RSI2。
>
> 系统：你指平安银行还是中国平安？
>
> 用户：平安银行。
>
> 系统：确认股票，保留此前日期与指标，进入原有研究流程。

缺股票、单个未知名称或单个部分名称，也可保存已校验的非股票条件，等待完整名称/代码。多个不同证券或名称/代码冲突不简化成普通选股补全，需要用户重述单股票问题。

身份续接本身不保存聊天、结果或长期偏好。后续同一会话独立加入有限指标替换、一份15分钟成功结果及有限追问/知识定义，仍不是完整聊天记忆。日期/指标的通用多轮修复、任意指代、开放问答或数据库会话未实现。默认无状态入口保持兼容，应用显式传入 `session=` 才启用续接；待选时知识和显式历史查看不会消费股票选择，其他含糊追问仍优先完成身份确认。

## 2. 代码职责

| 文件 | 作用 |
| --- | --- |
| [multi_contracts.py](../src/trading_companion/multi_contracts.py) | 提取 `validate_research_conditions()`，待确认条件和完整请求共用日期/指标限制 |
| [research_intent.py](../src/trading_companion/research_intent.py) | 新增不可变 `ResearchConditions`，股票未确定时也可校验日期、指标、默认值与来源 |
| [research_session.py](../src/trading_companion/research_session.py) | 每会话独立的待选状态、短答匹配、15分钟有效期及续接校验 |
| [agent.py](../src/trading_companion/agent.py) | 可选会话分流，所有有效请求仍复用同一工具提议核对和执行器 |
| [session_agent_demo.py](../src/trading_companion/session_agent_demo.py) | 固定日期、Fake模型和人工CSV的两轮演示，附重复回复检查 |
| [test_research_session.py](../tests/test_research_session.py) | 条件保存、选择、取消、时效、目录、来源、隔离和并发边界测试 |

原先股票歧义在解析日期/指标前返回，没有完整 `request`。现在先校验独立条件，再判断是否可生成最终请求；不能临时填入一只“占位股票”来绕过契约。只有身份确定后，才通过 `conditions.for_security()` 构造并再次校验 `MultiIndicatorRequest`。

`PreparedResearchInput` 增加 `conditions`、`clarification_resumable`。导出的 `pending_parameters` 表示已校验、待补股票的条件，不是已授权的完整请求；此时 `confirmed_parameters` 仍为空。

## 3. 状态与数据流

```text
idle
  └─ 模型理解 → 独立条件校验 → 股票需澄清 → awaiting_instrument
       ├─ 完整名称/代码，当前校验通过 → 清除pending → 原有工具/解释流程
       ├─ 回复仍模糊或有额外修改 → 保留pending，不联网、不续期
       ├─ 取消/重置/过期/配置或目录失效 → 清除pending，不执行
       └─ “新问题：完整问题” → 清除旧pending → 按新请求重新理解
```

每个 `ResearchSession` 只保存一个不可变 `PendingResearch`：已校验条件、原候选、创建时间，以及用于核对的可信开发CSV配置。条件包含具体起止日期、指标、来源、原上海日期锚点和默认值。不会保存原始聊天、模型响应或密钥；导出也不含开发CSV路径。查询条件本身仍是用户数据，不称为完整生产脱敏日志。

同一会话必须复用同一个对象；不同会话必须使用不同对象。应用负责对象与用户/会话的绑定，这不是身份认证或服务器授权机制。状态只在当前进程内，重启后消失。

## 4. 回复如何处理

- 完整名称、唯一裸代码、完整代码以及目录验证的唯一别名可以选中；兼容全角字符和简单说法，如“我指的是平安银行”。
- 匹配整个短答，不从长句中提取名字后丢掉剩余内容。“中国平安，另外把SMA3改成SMA5”不会按旧SMA3执行，而是提示本步尚不支持这种修改，要求完整新问题。
- 不支持“第一个”“就它”等依赖候选顺序/指代的选择，也不把部分匹配当作确认。
- 原来已有候选时，只接受原候选内的身份；选择别的已知股票不会自动套用旧条件。原来缺股票/未知且没有候选时，可用完整名称或代码明确新身份。
- 每次用当前注入的目录重新匹配，并检查新鲜度；不从旧候选直接拿代码执行。原候选代码的名称发生变化时清除旧请求，要求完整重述。
- 当前目录新增的候选不会自动加入之前的选择范围；可通过完整新问题重新理解。

取消命令：`取消`、`取消分析`、`取消研究`、`算了`。重置命令：`新对话`、`重新开始`、`清空会话`。它们完整匹配且不调用模型/行情；不表示取消已经在执行的网络请求。当前进程内，同一会话忙时会拒绝同时到达的消息。

`新问题：` 或 `新问题:` 明确开始新请求，旧条件先清除；即使新理解失败，也不恢复旧请求或偷偷沿用旧日期。空的新问题也清除旧状态并提示补充。

会话为空时，仅回复一个可识别的股票名称/代码，会提示“没有待补全的选股请求”，不自动分析。这避免成功、取消或失败后的重复短答重新触发查询；新研究可说“分析中国平安最近走势”。无状态入口不受此会话分流影响。

## 5. 时间、配置与执行边界

- 有效期固定为创建后15分钟，恰好到期或时钟回退时拒绝；模糊回复不续期。没有后台清理任务，过期在尝试续接时检查；应用也应适时释放不用的会话对象。
- 相对日期只在原问题时换算。跨午夜选股仍保留原具体区间，不把“最近一个月”静默按新一天重算。
- 目录时效按续接时的上海日期重新检查，不沿用原锚点的通过结果。目录过期、未来日期或在线模式使用夹具都会阻断，无自动刷新。
- 行情源改变，或开发CSV路径/真实性配置改变，旧请求作废；不静默切源。模型可由可信应用更换，不依靠旧模型对话记忆。
- 最终请求继续校验股票前缀、日期、指标及当前来源限制；目录认识CDR/302证券，不代表腾讯研究可以分析它们。
- 有效选择先清除pending，再调用模型或工具。提议不符、模型失败、行情失败均不恢复它；重试必须是明确的新研究，而不是重复选股短答。
- 同一对象使用非阻塞进程内锁串行化整轮；并发消息返回 `session_busy`，不会等待后把同一回复执行第二次。锁在异常时释放；它不是分布式幂等、HTTP请求ID去重或跨进程锁。

`reference_date` 和带时区的 `current_time` 只能由可信应用提供，主要用于可复现测试；模型和用户文本不能设置它们。生产默认使用实际时间，不把固定演示日期当作今天。

## 6. 调用次数和结果

| 阶段 | 本轮模型请求上限 | 本轮分析上限 |
| --- | --- | --- |
| 首轮自然请求直接可执行 | 3 | 1 |
| 首轮进入股票澄清 | 1 | 0 |
| 短答成功续接 | 2 | 1 |
| 无效短答、取消、过期、无pending或会话忙 | 0 | 0 |

选股续接不再调用意图模型，`understanding.model_calls_attempted=0`；工具提议/解释仍按原有流程执行。一次“首轮澄清＋成功续接”合计最多3次模型请求、1次分析，计数分别属于各轮。没有新增自动重试、完整token或耗时预算。

结果增加 `session_event` 和脱离内部状态的 `session_state` 快照。研究事实与草稿仍分开，`answer_validation=not_performed`；流程通过不代表解释正确。

## 7. 运行与验证

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.session_agent_demo
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_research_session.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

演示固定上海时间为2026-09-26 10:00，目录仍是4条人工夹具，选择平安银行以匹配已有人工CSV。保留区间2026-08-27至2026-09-25，只有5条实际样例日线。首轮 `needs_clarification`（1次模型、0次分析）；第二轮 `completed`（2次模型、1次分析）；重复选择返回 `no_pending_clarification`（0/0）。全部模型回答都是脚本，不证明真实模型理解质量。

集成时只需为每个会话创建并重复传入 `ResearchSession()`：

```python
from trading_companion.agent import run_natural_research_turn
from trading_companion.research_session import ResearchSession

session = ResearchSession()
# model and directory are supplied by trusted application configuration.
first = run_natural_research_turn(
    "看看平安最近一个月走势，只看SMA20和RSI", model=model, directory=directory, session=session)
second = run_natural_research_turn(
    "中国平安", model=model, directory=directory, session=session)
```

上面的集成示意未在本步调用真实模型；它不是完整交互CLI。2026-10-20，Windows / Python 3.11.9：新增44项专项测试，全量499项离线测试通过，覆盖当前目录变化、跨午夜、失效时间、取消/新任务、会话隔离、实际CSV计算及同会话并发。未读取真实凭据，未请求真实模型、目录或行情。

# DeepSeek 工具调用适配器

日期：2026-10-19（America/Los_Angeles）

## 本步范围

M2已经有工具消息契约和受控执行器，但原 `DeepSeekTextModel` 只接受文本。这一步新增独立 `DeepSeekToolModel`，把内部工具消息转换成DeepSeek请求，再把响应转换回 `ToolModelResponse`。原文本接口和CLI保持兼容。

适配器只负责通信和协议转换，不执行工具、不维护会话、不检查回答中的金融结论。适配器实现阶段只做模拟HTTP验证；文末后续更新记录开发者的真实工具反馈及初版单轮编排。通用自然语言理解、回答检查和整体运行预算仍待实现。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| [tool_model.py](../src/trading_companion/tool_model.py) | 已有内部契约、消息顺序、调用ID和JSON容量校验 |
| [deepseek_tool_model.py](../src/trading_companion/deepseek_tool_model.py) | 新增工具适配器；消息转换、响应状态与调用提议解析 |
| [deepseek_model.py](../src/trading_companion/deepseek_model.py) | 复用配置；提取共享 `_post_chat()`，保留原文本解析 |
| [research_tool.py](../src/trading_companion/research_tool.py) | 已有执行器；独立决定提议能否执行及如何返回证据 |
| [test_deepseek_tool_model.py](../tests/test_deepseek_tool_model.py) | 新增24项离线测试，含模拟HTTP与真实人工CSV计算往返 |

`DeepSeekToolModel.complete(messages, tools=...)` 满足已有 `ToolCapableModel` 接口。业务调用方不必了解DeepSeek字段，也不能把工具执行权限交给适配器。

## 消息如何转换

| 内部对象 | DeepSeek格式 | 保留的约束 |
| --- | --- | --- |
| `ToolDefinition` | `tools[].type="function"`，定义放入 `function` | 发送前检查工具声明及名称唯一性 |
| 助手的 `ToolCall` | `tool_calls[].id/type/function` | 名称与原调用ID不变；`function.arguments` 是JSON字符串，不是对象 |
| `ToolMessage(role="tool")` | `role="tool"`、`tool_call_id`、`content` | 结果JSON编码成字符串；通过原ID匹配提议，不把内部 `name/result` 字段直接发送 |
| 普通文本消息 | `role/content` | 保留系统、用户和助手文本 |

例如内部 `arguments={"window":3}` 在HTTP请求中表示为 `"arguments":"{\"window\":3}"`。这是编码格式转换，不是重新生成参数或修改数值。返回参数用已有 `ToolCall` 解析，拒绝重复JSON键、非有限数、非对象及超限结构。

调用前必须通过 `validate_tool_messages()`：历史中的每个提议都有且仅有一个匹配结果，不能把缺失或孤立的结果交给模型。响应提议还要检查声明白名单和历史ID复用。协议最多表达8个调用，不意味着本产品允许执行8次；A4 有界研究工作流按顺序执行预算内的提议，工具尝试总数仍为6、主分析与补算合计最多2次。旧明确格式单轮执行器仍保持原契约，见[有界研究工作流](bounded-research-workflow-design.md)。

## HTTP成功不等于可以继续执行

| 提供方输出 | 内部处理 |
| --- | --- |
| `stop` | `complete`；必须有非空文本，不能同时含调用 |
| `tool_calls` | `tool_calls`；必须有合法、非空且有界的调用提议 |
| `length / insufficient_system_resource / aborted` | `incomplete`；丢弃所有调用，不解析或执行半截提议 |
| `content_filter` 或非空 `refusal` | `refused`；丢弃调用，返回固定说明，不暴露原始拒绝详情 |
| 未知结束原因或旧 `function_call` | `unsupported_response` 错误，不猜测如何兼容 |
| 非法结构、参数或元数据 | `invalid_response`；未声明工具单独归类为 `unknown_tool` |

即使HTTP是200，也可能只有半截输出；不能仅凭 `tool_calls` 字段存在就执行。`as_message()` 也禁止将拒绝或不完整响应转成正常助手历史。

返回值记录请求/实际模型名、提供方和可用token用量。未知用量保留 `None`，不是0。`is_mock=False` 表示走这个提供方适配器，而不是Fake；单靠该字段不能证明联网成功，测试可以模拟HTTP。

A4 保留有界的 `finish_reason`，区分长度耗尽和其他中断。产品工厂的结构化回答额度为4096 tokens；带工具声明的请求取配置值与1500中的较小值。没有工具声明的业务路径全部要求 JSON 对象，适配器发送 `response_format={"type":"json_object"}`；它不保证语义正确，也不替代本地检查。调用方清单由离线测试审计：通用知识、旧公司回答、旧决策回答、当前自然语言回答。纯文本调用继续使用 `DeepSeekTextModel`，不启用 JSON 模式。

## 为什么这样实现

- **文本与工具接口分开，共享传输。** `_post_chat()` 统一固定地址、鉴权、超时、错误分类和响应关闭；各适配器自行处理各自的消息语义，避免修改旧CLI的输入契约。
- **底层单次传输、不接受模型指定URL。** 地址固定为DeepSeek官方Chat Completions端点，禁用重定向。默认连接/读取等待45秒，HTTP超时不等于整个Agent回合的截止时间。2026-10-30起产品入口的[共享运行控制器](turn-control-design.md)可在剩余整轮时间和原调用额度内，对超时或连接错误重试一次，每次尝试单独计量；直接调用适配器仍只发一次。
- **显式关闭thinking，采用非流式。** 当前内部协议没有推理历史字段；官方说明工具调用的思考模式需要回传相应推理历史。因此本步使用 `thinking={"type":"disabled"}`，不能只改开关就宣称支持思考模式。提供方额外的推理字段不暴露或回放。
- **复用配置，不扩大功能。** 使用已有 `DeepSeekConfig`、显式型号和输出token限制。有工具时发送 `tool_choice="auto"`，没有则省略工具字段；不新增依赖、CLI或自动付费请求。

## 验证与复习顺序

2026-10-19：新增24项专项测试通过，完整316项离线测试通过（原292项加24项）。全部HTTP均被模拟，配置使用人工凭据，不读取真实密钥或请求市场。

往返测试比固定结果演示多验证了一层：模拟DeepSeek提议 → `ResearchToolExecutor` 实际读取可信人工CSV并计算 → 按原ID序列化真实计算结果 → 模拟DeepSeek最终响应。它证明转换与计算连接正确，不证明真实模型会选对参数或解释正确。

在项目根目录运行：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_deepseek_tool_model.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

建议先读 `DeepSeekToolModel.complete()` 的调用顺序，再看 `_wire_message()` 和 `_parse_response()`，最后读往返测试。重点理解“内部对象如何变成HTTP字段”和“为什么返回了工具提议也不能直接执行”。

下一步先显式验证真实账号/型号的工具协议兼容性，再完成受控单轮编排和回答检查。旧的真实文本 `OK` 记录不替代工具验证；本步不宣称完整Agent已完成。

后续实施更新（2026-10-19）：已新增默认离线、显式选择DeepSeek的固定任务验证入口 `tool_smoke.py`，最多两次模型调用和一次人工CSV分析，见[联通验证设计](tool-smoke-design.md)。新增22项测试后全量338项离线测试通过；尚未运行真实工具请求，本文适配器阶段的316项记录保留为历史验证。

后续反馈与实施更新（2026-10-20）：开发者提供一次真实 `deepseek-flash` 工具往返输出，两次非Fake响应、一次人工CSV计算，总计4,500 tokens；人工核对了解释，尚无程序回答检查。此记录不外推至其他型号或真实市场数据。另增[明确格式的单轮runner](single-turn-agent-design.md)，将业务编排留在适配器之外；新增33项测试，全量371项离线测试通过，没有由主Agent发送真实请求。

## 协议依据

以下DeepSeek官方文档于2026-10-19核对；账号和型号的真实兼容性仍需单独验证：

- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)

# 工具模型协议与离线 Fake 往返

日期：2026-10-16（America/Los_Angeles）

## 本次交付与边界

这是M2的第一个切口：让统一模型契约能够表达工具定义、模型提出的调用、调用方返回的结果和完整文本。通过脚本Fake验证协议往返，尚未完成自然语言Agent、正式工具注册/执行器或模型解释检查。

后续实施更新：受控执行器已在独立 `research_tool.py` 中接入多指标服务，见[受控工具设计](research-tool-design.md)；2026-10-19补上[DeepSeek适配](deepseek-tool-model-design.md)与模拟HTTP/人工CSV计算。2026-10-20记录开发者一次真实固定任务工具反馈，新增[明确格式单轮runner](single-turn-agent-design.md)，全量371项离线测试通过。本文以下仍描述第一切口的固定结果演示；旧演示保持不执行工具。通用请求理解、解释检查与完整对话仍待做。

旧 `ChatMessage`、`TextModel.complete()`、`FakeTextModel`、`DeepSeekTextModel` 与 `model_cli` 保留纯文本语义。新协议在独立 `tool_model.py` 中实现，演示入口是 `tool_model_demo.py`。不增加依赖，不读取真实密钥，不发送模型或行情请求。

开发继续使用ProductManager明确范围、Engineer实现与测试、CodeReviewer复审、UserProxy独立运行验收；产品运行时的单Agent架构不因此改变。

## 公开契约

| 类型 | 表达的内容 |
| --- | --- |
| `ToolDefinition(name, description, parameters)` | 工具名称、用途及参数结构描述；不是Python函数或执行权限 |
| `ToolCall(call_id, name, arguments)` | 模型提出的调用标识、工具名称与JSON对象参数 |
| `ToolMessage` | `system/user/assistant/tool`消息，以及所属角色允许的文本、调用或结果字段 |
| `ToolModelResponse` | 完整文本、工具调用、拒绝或截断等互斥状态 |
| `ToolCapableModel.complete(messages, *, tools)` | 接收合法历史和本次声明工具，返回一个经过校验的响应 |
| `FakeToolModel(responses)` | 按固定脚本返回响应，用于离线验证，不执行工具或推理 |

`ToolMessage`通过 `tool_calls` 表达assistant调用，通过 `call_id/name/result` 表达tool结果。`ToolModelResponse.as_message()`只允许完整文本或工具调用转换成可继续使用的assistant消息；拒绝和截断不能伪装为完整回答。

状态含义：

- `complete`：完整非空文本，没有待执行工具调用。
- `tool_calls`：一个或多个工具调用提议，尚未执行。
- `refused`：未提供正常答案，保持拒绝状态。
- `incomplete`：输出不完整，不能作为完成文本展示。

2026-10-29 A4：`ToolModelResponse.finish_reason` 是可选的固定枚举，保留供应商的 stop/tool_calls/length/content_filter/insufficient_system_resource/aborted；旧 Fake 脚本可省略。长度或资源中断不能伪装成完成响应；供应商的拒绝优先于其他状态。`to_dict()` 与 Fake 透传该元数据。`complete(messages, tools=...)` 签名不变；没有工具声明的现有业务调用方均约定 JSON，对纯文本的需求使用独立文本模型接口。

这些是内部协议状态，不宣称供应商HTTP字段或行为已验证。后续适配器需显式转换提供方消息、结束原因和错误。

## 一次往返

```text
调用方构造用户消息与工具定义
    ↓
Fake返回assistant工具调用提议
    ↓
调用方保留该assistant消息
    ↓
调用方显式加入固定人工tool结果，带原call_id与工具名
    ↓
Fake接收合法的完整消息组
    ↓
Fake返回完整文本
```

这个旧演示中的工具结果来自程序写明的固定人工数据。Fake没有查行情、没有调用 `analyze_indicators()`，也没有根据用户问题决定执行动作。现有独立受控执行器在业务校验后调用M1b研究服务，运行入口见[受控工具设计](research-tool-design.md)。

## 调用标识与消息顺序

- assistant提出的每个调用都有可定位的call ID。tool结果必须精确匹配已提出的调用ID和工具名称。
- 不能先出现tool结果，也不能重复、伪造或遗漏结果。一个assistant批次中的多个结果可以按不同顺序返回，但必须全部回齐。
- pending调用未解决时，不接受用户消息或新的assistant消息插入，也不能提前再次请求模型。
- 传入模型的历史必须停在合法的新用户消息或已完成的tool结果组边界。
- 工具名只能来自本次传入的声明集合，重复工具定义和未知名称明确拒绝。

本模块检查协议完整性，不负责证明工具结果内容真实。调用方仍须使用程序实际执行得到的结果；不能仅凭消息能解析就把它当行情证据。

## JSON、边界与隐私

参数和结果采用严格JSON对象：只接受合法类型、字符串键和有限数值；拒绝非对象参数、重复JSON键、非有限数、循环或超出资源上限的结构。数据进入契约后保存为经过校验的规范JSON，属性与序列化返回独立内容，避免外部修改嵌套字典后绕过验证。

本轮声明schema只承担参数结构描述和实现中明确检查的局部结构约束，不实现完整JSON Schema校验器。缺少业务参数、非法股票/日期、未知指标、数据源范围与权限，由独立工具执行层和已有 `MultiIndicatorRequest` 等业务契约验证，不能依赖本协议容器。

工具名称和调用ID有明确格式与长度限制。以下是本轮选择的本地协议上限，超限报错，不静默截断：

| 项目 | 上限 |
| --- | --- |
| 输入历史 | 20条消息 |
| 单条用户文本 | 2,000字符 |
| 完整请求 | 20,000字符，累计工具声明和消息各自ASCII转义JSON的序列化长度 |
| 单个JSON对象 | 10,000字符的规范JSON |
| JSON嵌套与节点 | 最大深度12、结构节点最多2,000 |
| 工具名称与调用ID | 最多64字符，限定各自标识字符范围 |
| 提供方和模型元数据 | 最多128字符，无空白或不可打印字符 |
| 工具声明 / 单响应调用 | 最多12个声明 / 8个调用 |
| Fake脚本 | 最多100项响应 |
| 单响应内容 | 文本、参数和标识等按实现计数，最多20,000字符 |

完整JSON内容也参与请求预算；模型/提供方名称是不透明元数据，不复用工具名和call ID的窄字符规则。这些是通用协议容器上限，不能扩大产品权限：首版执行器仍按PRD只注册一个日线分析工具、限制每轮行情执行次数。协议允许多调用配对，不意味着产品允许多次取数。这些限制不等于M3的token预算、整轮截止时间或会话管理。

`repr()`隐藏消息、参数、schema、工具结果和脚本响应中的敏感文本；错误使用固定脱敏描述。显式 `to_dict()` 是调用方主动访问协议内容，不是日志脱敏器；不能直接把其完整输出作为生产日志。

## Fake脚本行为

Fake先验证输入历史和工具声明，再验证下一条脚本响应。无效输入抛 `ModelRequestError`，无效脚本/模型输出抛分类 `ModelCallError`；错误不复制用户或脚本原文。校验失败不推进脚本，耗尽时明确失败，不自动重启或重试。`refused/incomplete`属于合法返回状态，返回时同样消费一项脚本；“已消费响应”和“完成了用户回答”是不同含义。

Fake返回结果明确标记模拟来源，token用量保留未知。即使离线往返通过，也只证明协议及覆盖到的行为成立，不代表DeepSeek真实工具调用可用、模型理解质量达标或完整Agent已完成。

## 运行和验证

在项目根目录：

```powershell
$env:PYTHONPATH='src'
python -m trading_companion.tool_model_demo
python -m unittest discover -s tests -p test_tool_model.py -v
python -m unittest discover -s tests -v
```

验收覆盖完整文本与工具往返、多调用ID配对、非法顺序、未解决调用、JSON边界、可变数据隔离、响应状态、脚本消费、脱敏以及旧接口兼容。演示必须明确标注固定人工结果与离线Fake，不把模拟输出写成真实行情或模型验证。

2026-10-16最终验证：Windows / Python 3.12.10，新增34项正式测试通过；UserProxy独立运行全量270项离线测试（原236项加34项），0失败、0错误。另有协议往返、JSON、资源前置检查与元数据边界的独立断言通过；CodeReviewer最终审查无剩余问题。新模块另经Python3.11语法解析检查；没有将这项静态检查称为3.11运行验证。

`tool_model_demo`、旧CSV行情CLI和Fake纯文本CLI均退出0。演示输出 `offline_mock_demo`，提议状态 `tool_calls`，调用方注入固定人工值7后响应为 `complete`；`is_mock=true`，`network_used=false`，`executed_tools=false`，token用量为未知。完整测试经自动审核批准后在沙箱外原样运行，以避开本机沙箱临时目录权限限制；没有修改代码规避测试，也未联网、读取真实密钥或调用真实模型。

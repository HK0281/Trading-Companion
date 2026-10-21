# 工具调用联通验证入口

日期：2026-10-19（America/Los_Angeles）

## 这一步解决什么问题

工具适配器的离线测试已通过，但还没有一个供开发者显式运行的真实工具往返入口。[tool_smoke.py](../src/trading_companion/tool_smoke.py) 补上固定任务的验证流程，不接管自然语言对话或实现完整Agent。

默认Fake完全离线，不读取模型凭据。只有 `--provider deepseek` 才读取已有环境配置，并可能向DeepSeek发送最多两次付费请求。此入口实现时仅做离线验证；开发者随后提供一次真实工具往返记录，见文末更新，不把两类验证混为一谈。

## 固定任务与数据流

测试任务固定为 `000001.SZ`、2026-09-21至2026-09-25、SMA3/SMA20/量能3。数据由程序注入仓库内的人工CSV，短周期仅用于展示可用与历史不足，不是产品默认或交易参数建议。

1. 模型接收固定指令和工具声明，提出一次调用。
2. 程序检查恰好一个提议，名称和参数与测试任务一致；指标顺序可以变化，参数不能遗漏、增加或改写。
3. 同一个 `ResearchToolExecutor` 实际计算人工CSV，保留原调用ID、精确数值和不可用字段。
4. 原助手提议及工具结果一起交回模型；第二次必须返回完整文本，否则停止。

没有循环、自动重试或提供方回退。不会补造工具提议、纠正参数、扩大日期、访问真实行情，或新建执行器重置额度。工具声明仍复用已有白名单；固定任务检查进一步收紧本入口的范围，不替代执行器自身的业务与权限校验。

首次直接回答、拒绝、截断、多提议或参数不符，都不能算工具验证通过。执行失败/无可用数据后不发送第二次请求；第二次再提工具也不会发送第三次。

## 在PowerShell运行

在项目根目录，先运行离线版本，无需密钥和虚拟环境激活：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.tool_smoke
```

如需真实验证，先在同一个终端按[模型配置说明](../README.md#text-model-layer-offline-first)设置 `DEEPSEEK_MODEL` 和 `DEEPSEEK_API_KEY`；使用账号实际支持的型号，密钥只输入本机的掩码提示，不粘贴到聊天或保存进代码。若尚未安装HTTP依赖，使用已有可选依赖 `pip install -e ".[model]"`，本步没有新增依赖。

只有你愿意把固定测试指令、工具声明和人工计算结果发送给DeepSeek并承担API费用时，才运行：

```powershell
.\.venv\Scripts\python.exe -B -m trading_companion.tool_smoke --provider deepseek
```

这条命令不是免费预览或dry run。默认配置每次最多512个输出token、30秒HTTP等待；不自动重试。两次模型调用加本地计算仍没有实现产品Agent的全轮截止时间。测试后可用 `Remove-Item Env:DEEPSEEK_API_KEY` 清理当前终端密钥。

## 怎么读输出

| 字段 | 含义 |
| --- | --- |
| `status / code / stage` | 是否完成、本次原因，以及停在配置/提议/执行/最终回答哪一阶段 |
| `provider / live_provider_selected` | 选择Fake还是DeepSeek；选择真实路径本身不证明联网成功 |
| `model_calls_attempted / analyses_used` | 调用了几次模型接口、消费了几次分析额度；不是成功数或实际HTTP次数保证 |
| `model_responses` | 每次返回的状态、请求/实际型号、模拟标志和可用token用量；不伪造未知用量 |
| `tool_round_trip_completed` | 工具提议、计算、结果回填和完整文本往返是否完成 |
| `tool_result` | 人工CSV的结构化结果及原调用ID；SMA20缺15条仍保留不可用状态 |
| `answer / answer_validation` | 最终模型原文；本入口恒标记 `not_performed`，不宣称金融事实已检查 |

成功时 `status=ok`、`tool_round_trip_completed=true`、模型调用2次、分析1次。默认运行仍是Fake，不能作为真实接通记录；DeepSeek路径也可在自动化测试中被模拟HTTP替换。真实兼容性需由开发者的显式实际运行记录确认，不能只看 `is_mock=false`。

退出码：0表示固定往返完成；1表示未验证成功或运行失败；2表示配置/命令参数需修改。失败输出固定原因或适配器的脱敏错误码，不打印密钥、原始异常或提供方错误正文。模型原文和工具结果不是生产脱敏日志，不应任意上传。

## 本轮验证与阅读顺序

新增 [22项测试](../tests/test_tool_smoke.py)，完整338项离线测试通过；默认CLI也实际运行并输出严格JSON。HTTP和配置均使用模拟值，真实行情源被拦截。测试覆盖默认离线、严格任务范围、失败停止、两次调用上限、原ID回填，以及模拟DeepSeek HTTP与人工CSV真实计算的组合。

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_tool_smoke.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

先读 `run_smoke_test()` 的几个退出分支，再读 `_matches_fixture()`，最后看模拟HTTP往返测试。关键是理解“测试链路成立”“真实服务兼容”“回答事实正确”是三个不同结论。真实工具验证通过后，再进入单轮Agent编排和回答检查；本步不提前实现它们。

## 后续反馈与实施更新：2026-10-20

开发者提供了本机显式运行的成功输出：`provider=deepseek`，请求与返回型号均为 `deepseek-flash`，两次非Fake响应分别为 `tool_calls` 和 `complete`；两次模型调用、一次人工CSV分析，`tool_round_trip_completed=true`。用量分别为1,332与3,168 tokens，总计4,500。读取时间为 `2026-10-20T07:00:06.224814+00:00`。

实际研究仍是固定5条人工日线：SMA3约10.1667，SMA20缺15条，相对量能1.2。最终解释经人工阅读与事实/关系核对，`answer_validation=not_performed` 仍然成立。它只验证这一真实型号与固定任务，不证明其他模型、真实市场接入或全面回答质量。主Agent没有读取密钥或额外发送付费请求。

已另增[单轮研究runner](single-turn-agent-design.md)，不把本验证CLI改成产品对话。runner当前支持明确格式输入、固定澄清和请求范围绑定，33项新增测试后全量371项离线测试通过；本文338项记录保留为入口实现阶段的历史结果。回答检查仍待实现。

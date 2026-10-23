# 产品入口的显式行情来源选择

## 目的与边界

产品启动时可选择 `synthetic`、`tencent` 或 `akshare_eastmoney`。同一个服务进程中的会话使用同一个可信来源配置；用户问题、模型工具建议和 HTTP 请求体都不能修改它。

这一步补齐已经存在的 AKShare 适配器与产品入口之间的连接，不增加新的行情接口，也不声称上游接口当前可用。没有自动重试、自动换源或将多个供应商数据混在一起的流程。

| 启动模式 | 实际研究来源 | 数据性质 | 身份目录 |
| --- | --- | --- | --- |
| `synthetic` | 可信配置绑定的 `csv` | 固定人工样例，非真实行情 | 固定开发夹具，不接受自选目录 |
| `tencent` | `tencent` | 供应商历史日线，不复权，未独立核实 | 显式本地目录，逐轮重查，不自动刷新 |
| `akshare_eastmoney` | `akshare_eastmoney` | AKShare / 东方财富历史日线，不复权，未独立核实 | 与腾讯模式相同的本地目录规则 |

默认离线演示保持不变。Agent 的默认线上来源仍为腾讯；已有腾讯启动方式、模型调用额度和证券身份校验不变。新增模式必须显式选择。

## 启动方式

在项目根目录安装所需依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[api,model,data]'
```

AKShare 包可选，腾讯和人工模式不要求安装它。选择 AKShare 时先检查包是否存在；缺失会输出固定的 `data_dependency_missing`，提示安装 `.[data]`，不会先读取模型密钥、发起模型请求或悄悄改用腾讯。

包存在检查不导入 AKShare、不访问网络，也不证明所有传递依赖、网络环境或上游响应可用。后续实际行情失败仍由适配器与受控工具返回受限结果，不自动换源。

Web / API 启动示例：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --provider deepseek --data akshare_eastmoney --directory data/security_directory_live.json
```

终端启动示例：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.agent_cli --provider deepseek --data akshare_eastmoney --directory data/security_directory_live.json
```

上述命令会显式启用可能计费的 DeepSeek。实际运行前需自行配置有效模型凭据和当前可用的本地证券目录；不要把密钥写进命令、问题、测试文件或 Git。本文中的命令不是自动验收的一部分，开发测试不执行真实模型或行情请求。

`--demo` 仍只使用固定人工夹具，不能与 `--data`、自选目录或真实来源混用。

## 数据如何流动

1. `api_server.py` 或 `agent_cli.py` 将白名单启动参数解析为可信配置，并检查 AKShare 可选依赖。
2. `ResearchApiService` 将该配置同时传给每轮 `run_natural_research_turn` 和已保存结果的重查。终端入口同样传入所选来源。
3. Agent 用该来源构建并核对请求；原有工具执行器按请求选择适配器。模型如果建议了不匹配的来源，不会获得绕过请求绑定的权限。
4. 已确认参数、报告和图表保留实际来源；历史查看按同一来源重新校验原快照，不会重新取数。
5. `/health` 和创建会话响应都返回同一个 `data_mode`。网页在两次响应来源不一致时拒绝建立会话。

人工模式在 Agent 内通过显式开发数据配置绑定 `csv`；它不是用户可自由选择路径的入口。API 服务仍不接受每请求的 `source`、`data_mode`、目录路径或任意 CSV 路径。

## 页面与会话行为

- 侧栏显示真实启动模式，AKShare 明确标为“AKShare / 东方财富历史日线”。
- DeepSeek 同意提示区分模型发送与行情查询：问题、参数和证据可能发送给模型；股票与日期用于查询启动时选定的行情源。
- 每份报告展示自己的已确认来源，图表继续校验来源绑定。
- 页面与终端明确提示：来源由启动固定，失败不自动切换；换源须结束并重新启动服务/终端。自然语言切源不是本模块支持的功能。

选择行情来源不等于选择证券状态来源。真实模式不会自动加载人工市场上下文，也不会自动查询证券状态或交易所日历；可选市场上下文仍只能在服务启动时配置本地快照，且真实性隔离规则保持不变。

## 验证范围

`tests/test_source_selection.py` 使用固定假模型与人工日线，模拟被选中供应商的接口契约，覆盖：

- AKShare 依赖缺失、错误信息脱敏及其他模式不受影响。
- 显式启动、来源一致性、禁止人工与真实模式混用。
- 同一来源完成分析、报告、图表及零调用历史查看。
- 被选中来源失败后不重试、不换源。
- HTTP 拒绝每请求自由配置来源，真实市场上下文保持显式可选。

`frontend/tests/sourceSelection.test.tsx` 覆盖三种模式的运行时校验、健康检查与会话来源不一致、同意提示、报告来源展示，以及请求体不加入自由来源配置。

这些测试证明程序选源与安全边界，不证明真实 AKShare 请求成功、供应商数据质量或投资建议有效性。

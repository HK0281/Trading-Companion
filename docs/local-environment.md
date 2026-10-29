# 本地模型配置与启动

更新：2026-10-25。Web 服务默认正常模式；离线演示保留显式入口。

## 1. 只需填写什么

源码项目根目录中的 `.env` 是本机配置。首次克隆时可以复制 `.env.example` 为 `.env`；如果文件已存在，不要覆盖。

```dotenv
DEEPSEEK_API_KEY=your_actual_api_key
DEEPSEEK_MODEL=deepseek-flash
```

- `DEEPSEEK_API_KEY`：替换为你自己在 DeepSeek API 平台创建的密钥，不是网页登录密码。没有填写时，真实模式会在发请求前拒绝启动。
- `DEEPSEEK_MODEL`：你账号支持的模型标识。`deepseek-flash` 是本项目此前由开发者回传验证过的标识，不保证所有账号当前都有权限。

只需这两个字段。当前请求地址固定，不需要 `OPENAI_API_KEY`、自定义 API URL、数据库密码或行情密钥。`.env` 只提供模型配置，不决定行情来源；`api_server` 无参数启动本身就选择正常模式（DeepSeek + 腾讯历史日线），`--provider` / `--data` 仍可显式覆盖。

不要把真实密钥发到聊天、截图或日志。`.env` 已被 Git 忽略，但它仍是明文文件，不是加密保险箱；`.env.example` 才是可提交的空模板。

## 2. 填好后如何启动

首次克隆或换电脑时，先安装运行依赖；开发和测试可另外加 `dev`：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[api,model]'
```

构建过前端后，从项目根目录启动正常产品：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server
```

打开 `http://127.0.0.1:8765/`。默认使用真实 DeepSeek、腾讯不复权历史日线及 `data/security_directory_live.json`；这不是实时行情。使用可编辑安装后不用激活虚拟环境或反复设置 `PYTHONPATH`。不需要再逐次输入模型名和密钥。修改 `.env` 后，先在服务终端按 `Ctrl+C` 停止，再执行启动命令；运行中的服务不会自动刷新密钥。

启动只做本地配置、依赖和目录检查，不向 DeepSeek 请求、不取行情、不自动刷新目录；网页载入也只读 health。用户明确发送研究问题时才按需求调用模型或行情，DeepSeek 请求可能计费。启动失败会明确报错，不会自动切成人工数据或更换供应商。

### 真实目录缺失或过期

正常模式需要当前有效、非人工的本地证券目录；已有有效目录时不用每次刷新。目录缺失、格式错误、未来日期或超过允许新鲜度时会拒绝正常启动，而不是让模型凭记忆补股票代码。需要更新时手动执行：

```powershell
.\.venv\Scripts\python.exe -B -m trading_companion.security_directory_refresh --live --timeout 15
```

该命令明确联网下载交易所名单并更新本地目录；不要把它放进每次启动或页面载入流程。目录覆盖沪深 A 股，不代表北交所也已支持。目录刷新成功也不等于模型或腾讯行情连接已通过验证，详见[目录设计](security-directory-refresh-design.md)。

### 离线开发与显式覆盖

正常网页的 **Dev Mode** 按钮切换到服务端预先创建的独立 `fake + synthetic` 服务，不用重启。只有空闲且没有结果不确定的请求时才能切；原会话清理成功后即切换目标并清空聊天和草稿，再检查目标连接。目标不可用会留在目标的断连状态，不恢复已删除聊天、不自动回退；清理失败则保留旧内容。刷新页面回正常模式。人工目录只有四个证券，行情只有平安银行的五条人工日线，不能分析任意股票。详见[模式设计](dev-mode-design.md)。

完全不需要真实配置的离线启动：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --demo
```

`--demo` 完全离线、不读取 `.env`，也不能从网页切回正常服务。正常启动时可用 `--port` 改端口，或显式 `--provider deepseek --data tencent` 指定正常后端；`--data akshare_eastmoney` 需要额外 `.[data]` 依赖。`--provider deepseek --data synthetic` 仍是可能计费的真实模型配人工数据，**不是**零模型费用的 Dev Mode。

以上默认行为只调整 `api_server`；`agent_cli` 无参数仍显示帮助、真实模式仍需显式选择提供方/数据，固定评估和演示入口的选择规则不变。前端安装与构建参见[工作台文档](web-workspace-design.md)。

## 3. 加载规则与排错

`local_env.load_model_environment()` 只在六个真实模型启动路径调用：网页服务、终端 Agent、文本联通、工具联通、Agent 固定评估和 MVP 重复评估。

[新闻分诊评估](news-triage-design.md)另用 `load_evaluation_key_environment()`，只在显式 live 输出计划且通过标注复核检查后，提取所选的 `TYPESAFE_API_KEY` 或 `DEEPSEEK_API_KEY`。它沿用下述固定路径、环境优先及禁用规则，不读取模型名、不改变原六入口的提供方语义；offline 和 `--plan-only` 不读文件或密钥。

读取固定的源码项目根目录 `.env`，不会因为终端位于 `src` 而改读另一个文件，也不会向父目录搜索。仅在识别到源码布局及 `pyproject.toml` 标记时启用；独立 wheel 安装不会尝试读取虚拟环境上层的 `.env`，应显式提供进程环境变量。

优先级：**当前进程环境变量 > `.env`**。现有环境变量即使为空，也不会被文件静默替换。两个变量已全部存在时，完全不打开文件；缺文件时仍允许只使用环境变量。

如果之前在当前终端配置过错误值，而现在希望完全使用 `.env`，先移除终端覆盖项：

```powershell
Remove-Item Env:DEEPSEEK_API_KEY, Env:DEEPSEEK_MODEL -ErrorAction SilentlyContinue
```

然后重启服务。不要通过打印密钥来排错。

文件按 UTF-8 读取，兼容 BOM、引号与注释；不做 `${...}` 变量展开。只提取上述两个模型字段，不把文件里的其他字段注入系统环境。`DeepSeekConfig.from_env()` 保留原有校验，空值、空格和控制字符仍会被拒绝。

需要显式禁用本地文件时，可在当前终端设置：

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
```

需要恢复读取时移除该变量，再重启：

```powershell
Remove-Item Env:PYTHON_DOTENV_DISABLED -ErrorAction SilentlyContinue
```

## 4. 为什么在入口加载

`.env` 是配置来源，不是 Agent 的业务逻辑。入口合并配置后，把结果交给 `DeepSeekConfig` 校验；底层模型和 Agent 不需要知道密钥来自文件还是终端。

不在模块导入时读取文件，也不修改全局 `os.environ`。因此离线演示不触碰密钥，缺依赖/无效本地目录先失败，多个测试之间也不会因配置加载而留下隐式全局状态。

专项测试只用临时文件和人工密钥，覆盖六个入口、环境变量优先、空值、BOM、缺文件、读取失败、离线分支和敏感信息隔离。离线验收脚本额外设置 `PYTHON_DOTENV_DISABLED=1`，旧配置测试也隔离文件读取；不访问用户的真实 `.env`，不发送实际模型请求。

历史验证（2026-10-24，本地 `.env` 加载增量）：新增25项专项测试，全量1,197项Python测试零跳过通过；原Agent 4场景、MVP 90次重复、API 8检查及ASGI静态资源/wheel打包检查通过。另用普通`unittest discover`复跑全量，确认不依赖验收脚本才能隔离本机文件。该次未修改前端源码，也未新增真实模型或行情请求；这些数字不是此次正常/开发模式改动的验收结果。

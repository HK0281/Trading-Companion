# 本地模型配置与启动

更新：2026-10-24

## 1. 只需填写什么

源码项目根目录中的 `.env` 是本机配置。首次克隆时可以复制 `.env.example` 为 `.env`；如果文件已存在，不要覆盖。

```dotenv
DEEPSEEK_API_KEY=your_actual_api_key
DEEPSEEK_MODEL=deepseek-flash
```

- `DEEPSEEK_API_KEY`：替换为你自己在 DeepSeek API 平台创建的密钥，不是网页登录密码。没有填写时，真实模式会在发请求前拒绝启动。
- `DEEPSEEK_MODEL`：你账号支持的模型标识。`deepseek-flash` 是本项目此前由开发者回传验证过的标识，不保证所有账号当前都有权限。

只需这两个字段。当前请求地址固定，不需要 `OPENAI_API_KEY`、自定义 API URL、数据库密码或行情密钥。提供方与行情来源仍由命令行显式选择，不能通过 `.env` 自动开启真实服务。

不要把真实密钥发到聊天、截图或日志。`.env` 已被 Git 忽略，但它仍是明文文件，不是加密保险箱；`.env.example` 才是可提交的空模板。

## 2. 填好后如何启动

本次已在项目虚拟环境安装新增依赖。首次克隆或换电脑时，先安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[api,model,dev]'
```

从项目根目录启动真实 DeepSeek + 人工行情：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -B -m trading_companion.api_server --provider deepseek --data synthetic --port 8765
```

打开 `http://127.0.0.1:8765/`。不需要再逐次输入模型名和密钥。修改 `.env` 后，先在服务终端按 `Ctrl+C` 停止，再执行启动命令；运行中的服务不会自动刷新密钥。

选择 DeepSeek 后，提交问题可能产生模型费用；`synthetic` 仍是人工行情，不是真实股票数据。`--demo` 继续完全离线，且不会读取 `.env`。前端构建流程不受本次修改影响。

## 3. 加载规则与排错

`local_env.load_model_environment()` 只在六个真实模型启动路径调用：网页服务、终端 Agent、文本联通、工具联通、Agent 固定评估和 MVP 重复评估。

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

2026-10-24验证结果：新增25项专项测试，全量1,197项Python测试零跳过通过；原Agent 4场景、MVP 90次重复、API 8检查及ASGI静态资源/wheel打包检查通过。另用普通`unittest discover`复跑全量，确认不依赖验收脚本才能隔离本机文件。本次未修改前端源码，也未新增真实模型或行情请求。

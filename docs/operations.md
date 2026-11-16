# 运行环境与持续集成

本机环境配置（.env、启动方式）和 CI 门禁设计。

> 本文由多份原设计文档按主题合并而成（2026-11-16）。各节保留原文，包括当时的日期、提交号、总线消息号和命令；其中提到的早期演示模块（如 `agent_demo`）和研究意图 v2.5/v2.6 封存评测脚本已在同次清理中删除，需要时请查看 git 历史（标签 `research-intent-v2.6-freeze-valid`）。

## 目录

- [本地模型配置与启动](#本地模型配置与启动)
- [自动化验收与持续集成](#自动化验收与持续集成)


## 本地模型配置与启动

更新：2026-10-28。Web 服务默认正常模式；Windows 启动脚本按需刷新默认证券名单。

### 1. 只需填写什么

源码项目根目录中的 `.env` 是本机配置。首次克隆时可以复制 `.env.example` 为 `.env`；如果文件已存在，不要覆盖。

```dotenv
DEEPSEEK_API_KEY=your_actual_api_key
DEEPSEEK_MODEL=deepseek-flash
```

- `DEEPSEEK_API_KEY`：替换为你自己在 DeepSeek API 平台创建的密钥，不是网页登录密码。没有填写时，真实模式会在发请求前拒绝启动。
- `DEEPSEEK_MODEL`：你账号支持的模型标识。`deepseek-flash` 是本项目此前由开发者回传验证过的标识，不保证所有账号当前都有权限。

只需这两个字段。当前请求地址固定，不需要 `OPENAI_API_KEY`、自定义 API URL、数据库密码或行情密钥。`.env` 只提供模型配置，不决定行情来源；`api_server` 无参数启动本身就选择正常模式（DeepSeek + 腾讯历史日线），`--provider` / `--data` 仍可显式覆盖。

不要把真实密钥发到聊天、截图或日志。`.env` 已被 Git 忽略，但它仍是明文文件，不是加密保险箱；`.env.example` 才是可提交的空模板。

### 2. 填好后如何启动

首次克隆或换电脑时，先安装运行依赖；开发和测试可另外加 `dev`：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[api,model]'
```

API 依赖包含刷新深交所 Excel 名单所需的 `openpyxl`；已有环境缺少它时，重新执行上面的安装命令即可，不必为腾讯模式安装 AKShare。

构建过前端后，双击根目录 `start.cmd`，或从项目根目录执行：

```powershell
.\start.cmd
```

脚本使用项目虚拟环境，在服务成功监听后打开浏览器。参数照常传入，例如 `.\start.cmd --port 8767`、`.\start.cmd --demo`；`--no-open-browser` 可关闭自动打开浏览器。

也可以直接启动 Python 入口，默认不自动刷新名单：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server
```

打开 `http://127.0.0.1:8765/`。默认使用真实 DeepSeek、腾讯不复权历史日线及 `data/security_directory_live.json`；这不是实时行情。使用可编辑安装后不用激活虚拟环境或反复设置 `PYTHONPATH`。不需要再逐次输入模型名和密钥。修改 `.env` 后，先在服务终端按 `Ctrl+C` 停止，再执行启动命令；运行中的服务不会自动刷新密钥。

启动不向 DeepSeek 请求、不取行情；网页载入也只读 health。`start.cmd` 会在需要时先联网刷新默认名单，再检查模型配置；直接 Python 启动默认只检查现有名单。用户明确发送研究问题时才按需求调用模型或行情，DeepSeek 请求可能计费。启动失败会明确报错，不会自动切成人工数据或更换供应商。

#### 真实目录缺失或过期

正常模式需要当前有效、非人工的本地证券目录。`start.cmd` 自动传入 `--refresh-stale-directory`：仅当正常模式使用真实数据、未显式传入 `--directory`，且默认 `data/security_directory_live.json` 缺失、上海日期下超过 7 天或日期在未来时，刷新一次。名单有效时不联网；`--demo`、`--data synthetic` 或任何显式 `--directory`（即使指向默认路径）都不自动刷新。

窗口先显示等待原因和“正在刷新证券名单”，成功后显示条数与日期。刷新沿用上交所主板、科创板和深交所导出三份必需数据，全部校验后才原子替换旧文件，再重新加载检查；刷新前不读 `.env`，失败就停止启动并保留旧文件。格式无效、人工名单或读取失败直接报错，不通过联网尝试覆盖修复。

只在启动阶段刷新。运行中名单过期时，继续按原规则拒绝需要有效名单的研究，不在对话中自动联网更新。直接 Python 启动默认不刷新；也可显式使用该参数启用相同的启动检查。需要手动更新时执行：

```powershell
.\.venv\Scripts\python.exe -B -m trading_companion.security_directory_refresh --live --timeout 15
```

自选目录须在刷新命令后追加 `--output` 指定同一路径。该命令明确联网下载交易所名单并更新本地目录，不调用模型或取日线；页面载入不会刷新名单。目录覆盖沪深 A 股，不代表北交所也已支持。目录刷新成功也不等于模型或腾讯行情连接已通过验证，详见[目录设计](research-data-design.md#真实证券目录显式刷新与安全保存)。

#### 启动错误原因

启动失败返回非零码，窗口按固定原因提示，不输出异常原文或配置值：

| 原因码 | 含义及处理 |
| --- | --- |
| `directory_missing` | 名单缺失，按提示手动刷新，或使用 `start.cmd` |
| `directory_stale` | 名单过期或日期在未来，按提示刷新 |
| `directory_invalid` | 格式无效或属于人工名单，检查文件；自动刷新不会覆盖它 |
| `directory_unavailable` | 文件无法读取，检查访问权限 |
| `directory_refresh_failed` | 自动刷新失败，检查网络、交易所页面、安装依赖和本地写入条件，稍后重试或手动刷新 |
| `model_configuration_error` | 检查 `.env` 或环境中的 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`；显式 OpenAI 模式检查对应字段 |
| `snapshot_store_unavailable` | 检查 `--snapshot-db` 指定的本地证据快照库及权限 |
| `configuration_error` | 其他本地配置或夹具问题，检查启动配置 |

#### 离线开发与显式覆盖

正常网页的 **Dev Mode** 按钮切换到服务端预先创建的独立 `fake + synthetic` 服务，不用重启。只有空闲且没有结果不确定的请求时才能切；原会话清理成功后即切换目标并清空聊天和草稿，再检查目标连接。目标不可用会留在目标的断连状态，不恢复已删除聊天、不自动回退；清理失败则保留旧内容。刷新页面回正常模式。人工目录只有四个证券，行情只有平安银行的五条人工日线，不能分析任意股票。详见[模式设计](agent-runtime-design.md#正常模式与-dev-mode)。

完全不需要真实配置的离线启动：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --demo
```

`--demo` 完全离线、不读取 `.env`，也不能从网页切回正常服务。正常启动时可用 `--port` 改端口，或显式 `--provider deepseek --data tencent` 指定正常后端；`--data akshare_eastmoney` 需要额外 `.[data]` 依赖。`--provider deepseek --data synthetic` 仍是可能计费的真实模型配人工数据，**不是**零模型费用的 Dev Mode。

以上默认行为只调整 `api_server`；`agent_cli` 无参数仍显示帮助、真实模式仍需显式选择提供方/数据，固定评估和演示入口的选择规则不变。前端安装与构建参见[工作台文档](web-and-api-design.md#本机-web-研究工作台)。

### 3. 加载规则与排错

`local_env.load_model_environment()` 只在六个真实模型启动路径调用：网页服务、终端 Agent、文本联通、工具联通、Agent 固定评估和 MVP 重复评估。

新闻分诊评估另用 `load_evaluation_key_environment()`，只在显式 live 输出计划且通过标注复核检查后，提取所选的 `TYPESAFE_API_KEY` 或 `DEEPSEEK_API_KEY`。它沿用下述固定路径、环境优先及禁用规则，不读取模型名、不改变原六入口的提供方语义；offline 和 `--plan-only` 不读文件或密钥。

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

### 4. 为什么在入口加载

`.env` 是配置来源，不是 Agent 的业务逻辑。入口合并配置后，把结果交给 `DeepSeekConfig` 校验；底层模型和 Agent 不需要知道密钥来自文件还是终端。

不在模块导入时读取文件，也不修改全局 `os.environ`。因此离线演示不触碰密钥，缺依赖/无效本地目录先失败，多个测试之间也不会因配置加载而留下隐式全局状态。

专项测试只用临时文件和人工密钥，覆盖六个入口、环境变量优先、空值、BOM、缺文件、读取失败、离线分支和敏感信息隔离。离线验收脚本额外设置 `PYTHON_DOTENV_DISABLED=1`，旧配置测试也隔离文件读取；不访问用户的真实 `.env`，不发送实际模型请求。

历史验证（2026-10-24，本地 `.env` 加载增量）：新增25项专项测试，全量1,197项Python测试零跳过通过；原Agent 4场景、MVP 90次重复、API 8检查及ASGI静态资源/wheel打包检查通过。另用普通`unittest discover`复跑全量，确认不依赖验收脚本才能隔离本机文件。该次未修改前端源码，也未新增真实模型或行情请求；这些数字不是此次正常/开发模式改动的验收结果。


## 自动化验收与持续集成

更新：2026-10-25。当前仅支持 Windows，使用 PowerShell 和单一 Windows Actions 任务。范围：离线工程契约、前端构建、同源资源及 wheel 内容，不是投资效果评估或生产部署。文中的2026-10-22和2026-10-24结果保留为对应日期的历史记录。

### 1. 为什么需要这一层

原有测试已覆盖 Agent 和界面，但开发者仍要记住多条命令，而且存在两种假通过：

- HTTP 依赖缺失时，`unittest` 允许跳过测试，退出码仍可能为0。
- Python 路由测试使用临时假资源，不能证明真正的 Vite 构建能由 FastAPI 提供。

现在由 [verify.py](../scripts/verify.py) 实现验收规则，[GitHub Actions](../.github/workflows/ci.yml) 只负责准备环境和调用同一入口。这样本地与远端不会维护两套不同标准。

### 2. 检查顺序

| 入口 | 验收内容 | 必须失败的例子 |
| --- | --- | --- |
| `python` | unittest类与pytest函数各运行一次；明确fake的4场景Agent、30场景×3 MVP、5项公司研究流程和API演示 | 空测试集、收集错误、skip/xfail/xpass、测试阶段失败、遗漏或重复场景 |
| `web` | 解析 Vitest JSON，要求所有断言通过；TypeScript检查；Vite生产构建 | skipped/todo、缺失断言结果、测试失败、类型错误 |
| `bundle` | 构建 HTML 的本地引用；实际 ASGI 响应字节、MIME和安全头；wheel资产集合和字节 | 构建缺失、资源404、旧哈希文件混入、wheel漏资源或包含前端源码/环境文件 |
| `all`（默认） | 按上述顺序运行，任一步失败即停止；最后 `pip check` | 前一步失败却继续宣布整体成功 |

单独运行入口同样会执行 `pip check`。成功退出0；验收失败、子进程失败或超时退出非0，不自动安装、重试、换源或修复问题。测试数量随项目增长，不硬编码为当前数量；要求发现的测试全部运行且零跳过。

2026-10-26：发现原`unittest.discover`不会收集顶层pytest函数。新增`run_strict_pytest`，在同一清洁环境/离线保护内运行，排除已运行的TestCase类、关闭机器插件与继承选项，核对全部发现项与执行计划一致，并要求每个setup/call/teardown阶段通过，拒绝skip、xfail、xpass和空跑。不得只增加测试文件而不确认它进入CI。

MVP评估门槛另独立固定30个场景名、三次重复身份、201次fake模型方法调用与72次人工分析，并检查每一步的解读/综合/显示断言，拒绝空checks、缺断言或用绿summary掩盖失败。该门槛只允许人工/fake模式且质量仍待人工复核，不启动真实模型或行情；详见[MVP说明](architecture.md#单股票日线-mvp代码交付与验收清单)。

### 3. 本地运行

在 Windows 的项目根目录打开 PowerShell，使用已有虚拟环境，不必激活或改变执行策略：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[api,model,dev]"
pnpm.cmd --dir frontend install --frozen-lockfile
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py all
```

也可只复查一层：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py web
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py bundle
```

`bundle` 必须在 `web` 构建后运行。脚本自行设置源码导入路径，不要求另设 `PYTHONPATH`。本机直接使用 `.venv\Scripts\python.exe` 和 `pnpm.cmd`，避免依赖终端激活或执行 `.ps1` 包装脚本。

`dev` 增加了 `openpyxl`，因为证券目录测试需要在内存中生成 Excel 样例；不需要安装整个 `data` extra。AKShare适配测试注入假模块，不访问该服务。实际使用真实数据源时仍按原文档安装相应依赖。

### 4. 离线意味着什么

- `python` 和 `bundle` 的进程内应用检查使用环境白名单，不把提供方密钥、代理、自定义Python钩子传进去；不读取 `.env`。
- 固定 Agent 评估显式使用 `provider="fake"`；API 使用人工CSV和固定目录，不刷新目录、不请求行情或付费模型。
- 进程内保护拒绝 DNS、`connect` 和 `connect_ex`，即使业务代码捕获异常，最后仍使验收失败。
- Windows Python 3.11 的 `socketpair()` 内部通过 loopback 实现；只在该标准库调用期间、当前线程内放行它，供异步事件循环内部通信。普通 loopback 请求并未被普遍放行。
- 现有独立进程测试均启动固定离线演示，并继承清理后的环境；父进程的 socket patch 不会自动传播到这些子进程。
- 前端测试也使用 fake fetch/API；`web` 阶段清理继承环境，但没有对 Node 进程实施网络沙箱。

这属于防止意外外联的测试保护，**不是隔离恶意代码的系统级断网沙箱**。依赖安装、Actions下载及PEP517构建隔离可能访问软件包服务，不能称整个工作流完全不联网。工作流不配置任何模型密钥，不部署，也不接入交易权限。

### 5. 构建与打包检查

`built_assets()` 解析实际 `index.html`，要求本机 `/assets/` 下有 JS 和 CSS，拒绝外部URL、源码地址、路径穿越、空资源及未引用文件。同一 `TestClient(create_app())` 再访问根页、健康检查和每个资源，比较实际字节和安全头。

打包时把 `pyproject.toml`、README及当前 Python 包复制到临时干净目录，再运行默认隔离的 `pip wheel --no-deps`。**不删除或复用开发者原有的 `build/`**。这是因为本轮实际发现：setuptools 复用旧构建目录时，可能把上一版哈希 JS 留在 wheel 里。测试必须检查最终包，而不只是看“build succeeded”。

最后要求 wheel 的HTML/JS/CSS集合和字节与已检查构建完全一致，并拒绝 `.env*`、`node_modules`、TS/TSX及source map。临时报告和临时wheel结束后自动清理，编译资产仍按原设计留在被Git忽略的目录，供本机服务使用。

这里只验收**编译资源随包完整提供**。开发演示仍由 `development_fixtures.py` 从源码项目的 `data/` 读取夹具，因此没有宣称 wheel 已能脱离源码运行整个产品，也未验收 sdist 或正式发行流程。

### 6. GitHub Actions 边界

- `push`、`pull_request` 和手动触发。
- 单一 `windows-latest` 任务，使用 PowerShell、Python 3.11和Node 24；pnpm版本读取前端 `packageManager`。
- 权限仅 `contents: read`，checkout不保留Git凭据；不使用 `pull_request_target` 或模型密钥。
- 任务20分钟上限，同分支新运行取消旧运行；任一验收步骤失败，该任务停止。
- 前端使用已提交的锁文件及 `--frozen-lockfile`，安装脚本继续只允许esbuild。

Python仍使用 `pyproject.toml` 的版本范围和隔离构建工具；本模块没有新增Python哈希锁或固定完整传递依赖，所以这是**统一、可重复执行的检查流程**，不是逐字节可复现的依赖环境。Windows CI目前验收最低支持版本3.11，不代表所有更高Python版本均已测试。需要精确环境复现或扩大Python版本支持时，应单独补锁文件和版本验收。

### 7. 结果与排错

2026-10-25 Windows专用收口：本机 `scripts/verify.py all` 通过1791项Python测试、533项前端测试，以及TypeScript、Vite构建、ASGI资源、干净wheel与 `pip check`。这是当前提交前的本机离线工程验收记录；远端Windows Actions状态仍按对应提交查询，不代表真实模型或行情质量验收。

2026-10-22首次验收新增29项验收器测试：跳过、空测试、假成功报告、网络尝试、Windows内部socketpair、构建引用、wheel内容及干净打包输入。当时Windows本地记录为Python946项零跳过，前端43项零跳过；4/4 Agent契约及8/8 API检查通过，类型/构建、同源资产与wheel检查通过。当前远端Windows任务结果以对应提交的 [Actions](https://github.com/HK0281/Trading-Companion/actions) 页面为准；历史本机结果不能代替新提交的远端验收。

首次远端运行也发现原测试存在时间相关误报：用 `assertNotIn("999", 整段答案)` 检查假价格，但采集时间的微秒可能恰含这三位数字。现改为核对实际收盘价字段、快照指标值和错误价格表述，并固定含 `899928` 微秒的时间重现该条件；报告及快照测试都保留合法时间戳。未更改产品校验、跳过用例或降低CI标准。

2026-10-24 的 Windows 运行另发现 `.env` 定位测试的路径误报：临时目录使用 `RUNNER~1` 短名称，而产品的 `Path.resolve()` 返回 `runneradmin` 长名称。测试将预期路径同样规范化，并补充含 `..` 的输入路径用例；产品的定位逻辑和密钥读取边界未因此改变。不能使用 `samefile()` 替代，因为定位时 `.env` 可以尚不存在。

| 现象 | 先看哪里 |
| --- | --- |
| HTTP模块跳过或导入失败 | 是否安装 `.[api,model,dev]`，以及测试输出里的原始导入错误 |
| Vitest没有生成完整结果 | 是否使用锁文件安装，查看前端测试及断言结果 |
| `Web build is missing` | 先运行 `web`；不要把503当成构建验收成功 |
| wheel缺资源或字节不同 | 查看构建结果及package-data；不要手动改文件绕过检查 |
| 远端红灯、本机绿灯 | 对比提交、系统、Python/Node和依赖安装日志；不先降低验收要求 |

CI通过只表示这些固定工程检查满足预期，不证明真实提供方可用、任意自然语言正确、模型稳定、收益或生产安全。原真实场景评估仍需用户明确选择并单独记录。

2026-10-22 MVP收口后本地记录：Python1172项、前端269项零跳过；Agent 4/4、MVP 90/90、API 8/8，以及TypeScript/Vite/ASGI/wheel检查通过。远端结果仍以本次提交的Actions运行为准。

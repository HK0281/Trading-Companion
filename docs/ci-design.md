# 自动化验收与持续集成

更新：2026-10-25。当前仅支持 Windows，使用 PowerShell 和单一 Windows Actions 任务。范围：离线工程契约、前端构建、同源资源及 wheel 内容，不是投资效果评估或生产部署。文中的2026-10-22和2026-10-24结果保留为对应日期的历史记录。

## 1. 为什么需要这一层

原有测试已覆盖 Agent 和界面，但开发者仍要记住多条命令，而且存在两种假通过：

- HTTP 依赖缺失时，`unittest` 允许跳过测试，退出码仍可能为0。
- Python 路由测试使用临时假资源，不能证明真正的 Vite 构建能由 FastAPI 提供。

现在由 [verify.py](../scripts/verify.py) 实现验收规则，[GitHub Actions](../.github/workflows/ci.yml) 只负责准备环境和调用同一入口。这样本地与远端不会维护两套不同标准。

## 2. 检查顺序

| 入口 | 验收内容 | 必须失败的例子 |
| --- | --- | --- |
| `python` | unittest类与pytest函数各运行一次；明确fake的4场景Agent、30场景×3 MVP、5项公司研究流程和API演示 | 空测试集、收集错误、skip/xfail/xpass、测试阶段失败、遗漏或重复场景 |
| `web` | 解析 Vitest JSON，要求所有断言通过；TypeScript检查；Vite生产构建 | skipped/todo、缺失断言结果、测试失败、类型错误 |
| `bundle` | 构建 HTML 的本地引用；实际 ASGI 响应字节、MIME和安全头；wheel资产集合和字节 | 构建缺失、资源404、旧哈希文件混入、wheel漏资源或包含前端源码/环境文件 |
| `all`（默认） | 按上述顺序运行，任一步失败即停止；最后 `pip check` | 前一步失败却继续宣布整体成功 |

单独运行入口同样会执行 `pip check`。成功退出0；验收失败、子进程失败或超时退出非0，不自动安装、重试、换源或修复问题。测试数量随项目增长，不硬编码为当前数量；要求发现的测试全部运行且零跳过。

2026-10-26：发现原`unittest.discover`不会收集顶层pytest函数。新增`run_strict_pytest`，在同一清洁环境/离线保护内运行，排除已运行的TestCase类、关闭机器插件与继承选项，核对全部发现项与执行计划一致，并要求每个setup/call/teardown阶段通过，拒绝skip、xfail、xpass和空跑。不得只增加测试文件而不确认它进入CI。

MVP评估门槛另独立固定30个场景名、三次重复身份、201次fake模型方法调用与72次人工分析，并检查每一步的解读/综合/显示断言，拒绝空checks、缺断言或用绿summary掩盖失败。该门槛只允许人工/fake模式且质量仍待人工复核，不启动真实模型或行情；详见[MVP说明](mvp-completion.md)。

## 3. 本地运行

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

## 4. 离线意味着什么

- `python` 和 `bundle` 的进程内应用检查使用环境白名单，不把提供方密钥、代理、自定义Python钩子传进去；不读取 `.env`。
- 固定 Agent 评估显式使用 `provider="fake"`；API 使用人工CSV和固定目录，不刷新目录、不请求行情或付费模型。
- 进程内保护拒绝 DNS、`connect` 和 `connect_ex`，即使业务代码捕获异常，最后仍使验收失败。
- Windows Python 3.11 的 `socketpair()` 内部通过 loopback 实现；只在该标准库调用期间、当前线程内放行它，供异步事件循环内部通信。普通 loopback 请求并未被普遍放行。
- 现有独立进程测试均启动固定离线演示，并继承清理后的环境；父进程的 socket patch 不会自动传播到这些子进程。
- 前端测试也使用 fake fetch/API；`web` 阶段清理继承环境，但没有对 Node 进程实施网络沙箱。

这属于防止意外外联的测试保护，**不是隔离恶意代码的系统级断网沙箱**。依赖安装、Actions下载及PEP517构建隔离可能访问软件包服务，不能称整个工作流完全不联网。工作流不配置任何模型密钥，不部署，也不接入交易权限。

## 5. 构建与打包检查

`built_assets()` 解析实际 `index.html`，要求本机 `/assets/` 下有 JS 和 CSS，拒绝外部URL、源码地址、路径穿越、空资源及未引用文件。同一 `TestClient(create_app())` 再访问根页、健康检查和每个资源，比较实际字节和安全头。

打包时把 `pyproject.toml`、README及当前 Python 包复制到临时干净目录，再运行默认隔离的 `pip wheel --no-deps`。**不删除或复用开发者原有的 `build/`**。这是因为本轮实际发现：setuptools 复用旧构建目录时，可能把上一版哈希 JS 留在 wheel 里。测试必须检查最终包，而不只是看“build succeeded”。

最后要求 wheel 的HTML/JS/CSS集合和字节与已检查构建完全一致，并拒绝 `.env*`、`node_modules`、TS/TSX及source map。临时报告和临时wheel结束后自动清理，编译资产仍按原设计留在被Git忽略的目录，供本机服务使用。

这里只验收**编译资源随包完整提供**。开发演示仍由 `development_fixtures.py` 从源码项目的 `data/` 读取夹具，因此没有宣称 wheel 已能脱离源码运行整个产品，也未验收 sdist 或正式发行流程。

## 6. GitHub Actions 边界

- `push`、`pull_request` 和手动触发。
- 单一 `windows-latest` 任务，使用 PowerShell、Python 3.11和Node 24；pnpm版本读取前端 `packageManager`。
- 权限仅 `contents: read`，checkout不保留Git凭据；不使用 `pull_request_target` 或模型密钥。
- 任务20分钟上限，同分支新运行取消旧运行；任一验收步骤失败，该任务停止。
- 前端使用已提交的锁文件及 `--frozen-lockfile`，安装脚本继续只允许esbuild。

Python仍使用 `pyproject.toml` 的版本范围和隔离构建工具；本模块没有新增Python哈希锁或固定完整传递依赖，所以这是**统一、可重复执行的检查流程**，不是逐字节可复现的依赖环境。Windows CI目前验收最低支持版本3.11，不代表所有更高Python版本均已测试。需要精确环境复现或扩大Python版本支持时，应单独补锁文件和版本验收。

## 7. 结果与排错

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

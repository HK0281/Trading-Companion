# 真实证券目录：显式刷新与安全保存

更新：2026-10-28（America/Los_Angeles）

## 1. 这一步解决什么问题

[自然语言入口](natural-input-design.md) 已能提取用户原文中的名称，再由可信目录核实代码，但原演示只有4条人工记录。本步增加真实交易所列表读取和本地快照保存，不改模型理解、行情计算或下单权限。

刷新与查询分开：

```text
显式刷新 → 上海主板JSON＋科创板JSON＋深圳Excel导出
         → 校验全部必要分片 → 生成带获取时间的目录
         → 同目录临时文件 → 读回验证 → 原子替换本地快照

用户提问 → 加载本地快照 → 模型提取原文 → 目录身份匹配
         → 新鲜度/歧义/研究参数检查 → 原有研究流程
```

对话不会自动刷新目录。Windows `start.cmd` 会显式开启启动阶段的按需刷新：默认真实名单缺失、超过上海日期下 0–7 天范围或日期在未来时才刷新一次，名单可用时不联网。直接 `api_server` 默认不开启，须显式传入 `--refresh-stale-directory` 才有相同行为；`--demo`、`--data synthetic` 或任意显式 `--directory` 均跳过。格式无效、人工名单和读取失败直接报错，不自动覆盖修复。刷新不调用模型、不读凭据，也不请求个股日线；启动检查通过后才加载 `.env`。运行期间名单过期仍由既有研究检查拒绝，不在运行中刷新。

## 2. 文件职责

| 文件 | 职责 |
| --- | --- |
| [exchange_directory.py](../src/trading_companion/exchange_directory.py) | 固定交易所URL、限量HTTP读取、JSON/Excel解析和必要分片合并 |
| [security_directory.py](../src/trading_companion/security_directory.py) | 原有身份与时效规则；新增输出路径检查、快照序列化及原子保存 |
| [security_directory_refresh.py](../src/trading_companion/security_directory_refresh.py) | 独立刷新函数与显式 `--live` CLI |
| [api_server.py](../src/trading_companion/api_server.py) / [start.cmd](../start.cmd) | 默认关闭的启动刷新开关；Windows 脚本显式开启，只处理默认真实名单 |
| [test_security_directory_refresh.py](../tests/test_security_directory_refresh.py) | 模拟HTTP、内存Excel、失败时文件保留及自然入口衔接测试 |
| [test_startup_directory.py](../tests/test_startup_directory.py) | 启动刷新、模式绕过、旧文件保留、不提前读取配置及错误脱敏的离线测试 |

`data/security_directory_live.json` 是运行生成的本地快照，已加入 `.gitignore`，不替换人工夹具、不提交数千条易过期的数据。自定义输出路径需自行决定是否忽略。

## 3. 来源与真实接口发现

- 上海：`https://query.sse.com.cn/sseQuery/commonQuery.do`，固定查询 `COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L`。`STOCK_TYPE=1` 为主板A股，`8` 为科创板列表。
- 深圳：`https://www.szse.cn/api/report/ShowReport`，参数 `SHOWTYPE=xlsx`、`CATALOGID=1110`、`TABKEY=tab1`，读取A股列表导出。
- 路由和字段参考本地AKShare 1.19.1实现，再以真实HTTP返回核对；不直接调用无完整超时约束的全市场SDK函数。
- 深圳JSON候选接口实测未可用，最终实现固定使用Excel，不在运行中试探接口、重试或自动换源。

深圳Excel把工作表维度误写为 `A1:A1`。因此用 openpyxl 只读加载后调用 `reset_dimensions()`，再扫描实际行；不能把错误维度当作只有一条记录。`openpyxl>=3.1,<4` 明确列入 `data` 依赖，既有AKShare环境已包含该库。

科创板列表实际含 `689009.SH`（九号公司，CDR）；深圳列表实际含 `302132.SZ`（中航成飞）。目录保留这些有来源的身份，不把当前腾讯行情白名单当作证券存在性的定义。腾讯研究范围保持原样：这两类尚未支持的参数会在研究请求校验处阻断，不查询行情。目录能识别不等于行情能分析。

## 4. 数据校验与覆盖边界

共同规则：代码必须是六位ASCII字符串，不把整数、缺值或 `000001.0` 猜成代码；交易所和代码命名空间须一致；名称非空、可打印；记录非空、无重复代码，合并最多10,000条。ST、科创板和新上市名称不因风险标签或历史长度被过滤。不从名称推断历史风险或可交易状态。

上海检查查询标识、错误字段、分板类型、总条数和分页信息；只有第1页、共1页且实际条数等于声明总数才接收。虽然请求 `pageSize=10000`，服务端实测返回的有效页大小为2000，不能盲信请求参数。本步**不实现多页下载**：如果来源以后需要多页，刷新明确失败，必须先更新适配器，不能保存截断列表。

深圳按唯一列名读取 `A股代码`、`A股简称`，不固定列位置、不跳过坏行。导出不提供独立的总量元数据，所以只能验证取得的表格及记录，**不能证明上游没有遗漏证券**。即使三个分片都通过，也不声称全市场完整性已独立证实。当前不接入北交所，不包含历史退市目录和历史名称版本。

别名全部为空：本步未接入可验证别名源。“茅台”等部分名称依然遵守原有确认规则，不因模型熟悉这个简称就自动执行。目录的 `source` 和 `retrieved_at` 说明获取路径和完成时间，不是密码学证明，也不是交易所原始发布/版本时间。

## 5. 网络与保存的失败行为

- 固定HTTPS地址，不跟随重定向，不接受模型提供URL或保存路径。
- 单次HTTP解码后的响应最多10MB；Excel的ZIP条目最多1,000个、声明解压大小合计最多32MB；Excel扫描最多10,000条证券。它们是保守资源保护，不是完整的文件解析沙箱。
- 默认每次连接/读取等待15秒，可用 `--timeout` 调整；它不是完整刷新总截止时间。三个请求串行，没有自动重试或源回退。
- 任意分片失败，不进入保存阶段，不返回“部分成功”，也不为旧数据换上新时间。
- 启动入口传入检查基准日期，刷新函数先执行 `assert_usable(..., development=False)`，拒绝过期、未来日期和人工的新名单，再进入保存阶段。手动刷新 CLI 的既有默认行为不变。
- 保存只接受 `.json` 路径，拒绝符号链接、已有人工夹具及非目录JSON文件。已有目录可过期，因为刷新正是为了替换它。
- 完整结果先写目标同一文件夹的临时文件，`flush`/`fsync` 后用原加载器读回比较，最后 `os.replace()`。替换前失败保留旧文件；成功才替换。正常失败会清理临时文件，清理本身失败则明确报错。
- 原子替换避免读者看到半份文件，不提供旧版本备份、并发刷新锁或所有崩溃情况下的持久性保证。当前按单个刷新进程使用。

CLI失败返回非零码与错误类别，不把底层网络错误、凭据或本地错误路径直接输出。失败后不会自动把旧快照作为“本次成功”返回；应用若加载旧文件，仍必须通过上海日期下0–7天的 `assert_usable()` 检查，不能静默使用过期目录。

启动器先显示缺失或过期原因及刷新进度，成功后显示条数和日期，并从正式文件重新加载、再次检查；失败输出固定的 `directory_refresh_failed` 中文提示和手动刷新命令，不读取 `.env`，不启动研究服务。错误分类详见[本地配置与启动](local-environment.md#启动错误原因)。API 安装依赖包含深圳 Excel 解析器，腾讯模式无需为刷新安装完整 AKShare。

## 6. 如何运行

在项目根目录的PowerShell终端：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pip install -e ".[data]"
.\.venv\Scripts\python.exe -B -m trading_companion.security_directory_refresh
.\.venv\Scripts\python.exe -B -m trading_companion.security_directory_refresh --live --timeout 15
```

不加 `--live` 只返回 `live_refresh_not_enabled`，不联网、不写文件。加上后成功生成 `data/security_directory_live.json`；可通过可信应用参数 `--output` 指定另一个目录快照路径。

在Python中加载生成的文件即可交给既有自然语言入口，不需要重写Agent：

```python
from trading_companion.security_directory import load_security_directory

directory = load_security_directory("data/security_directory_live.json")
print(directory.lookup("平安银行"))
print(directory.lookup("平安"))
# Pass directory=directory to run_natural_research_turn(...).
```

`lookup()` 本身只查本地身份，不检查时效、不授权行情；自然入口会调用 `assert_usable()` 并校验研究来源。单独使用目录的应用也必须执行对应检查。

## 7. 验证记录

2026-10-20，Windows / Python 3.11.9：新增46项离线测试，全量455项通过。内存Excel测试包含错误维度、字段变化、代码类型及重复行；模拟HTTP检查超时、非200、流中断和后续分片失败；文件测试验证写入/替换失败、过期时间不续期及自然入口衔接。所有测试隔离真实网络和模型凭据。

另行真实刷新成功：UTC `2026-10-20T23:41:06.775810+00:00`（上海日期为2026-10-21），主板1,700条、科创板列表含CDR共618条、深圳2,906条，合计5,224条。来源返回量不等于独立认证的市场总量。名称查询中，“平安银行”唯一匹配 `000001.SZ`；“平安”出现平安银行、平安电工、中国平安三个候选，仍保留歧义。

未读取真实模型凭据，未请求模型或个股行情。本次不证明真实模型的自然语言质量，也不等于自然语言研究全链路真实验收。

后续实施更新：已另行完成有限[股票身份澄清续接](identity-clarification-design.md)，该阶段新增44项测试，全量499项通过；再增加[程序事实报告](safe-report-design.md)，当时全量539项通过；再补[受限指标关系检查](grounded-explanation-design.md)，当前全量593项通过。上文455项保留为目录刷新阶段的历史验证记录。通用参数修改/结果追问、任意自由文本语义检查与完整产品入口仍待开发。

# 有上限的资料与报价并发采集（3a、3b、3c）

2026-10-28：3a 同公司领域并发、3b 最多三家公司统一采集均已审核并提交；3c 腾讯报价固定批次并发已在工作区实现，待审核。公司资料、财务、新闻、公告和腾讯报价共用一个队列，全进程同时最多三个未完成任务；东方财富仍串行分页，技术计算、模型及市场背景请求的执行方式不变。实现位置是[采集执行模块](../src/trading_companion/collection_execution.py)、[公司采集器](../src/trading_companion/company_research.py)、[产品合并流程](../src/trading_companion/product_research.py)、[新浪新闻适配器](../src/trading_companion/company_news_sina.py)和[报价采集器](../src/trading_companion/screening_source.py)。

## 3c 腾讯报价批次（2026-10-28，工作区待审核）

腾讯的证券ID和批次边界在取数前已经确定，因此协调端一次生成任务表，按原五板块、板块内批次顺序编号，逐个交给共享执行器。完成顺序可以不同，全部成功后仍按原编号合并。standard保留代码前缀样本，expanded保留下标`floor(i × 板块目录数量 / 选中数量)`；不跨板块混批、不补样本。标准默认5批，扩大默认15批，50个ID一批时最多20批。

`QuoteCollectionScope`是报价单次采集的预算账本，最多20请求、总响应2,000,000字节；每个预定任务只请求一次。每个响应仍限200,000字节，实际响应块在保留前原子扣减总账，不按每任务预占200KB，失败不退款。共享生命周期只提取截止、关闭、检查点和结果接纳；公司`CollectionScope`仍最多12个任务描述、1～3家公司、每家公司最多5请求和4,048,000字节，不能借报价额度。

公司和报价、不同会话及旧迟到任务合计仍最多三个未完成Future（后台任务结果对象）。20个描述不等于排入20个后台任务，不新建报价池。腾讯来源窗口默认25秒、最多30秒，只在整次选样前起算一次；等共享名额和借用客户端锁都计入。单请求超时取原6秒上限、来源剩余和整轮剩余的最小值，可信请求配置仍最多10秒。

腾讯自有客户端或可信`session_factory`默认宽度3，每个批次创建并关闭自己的客户端。`session=`借用方式默认宽度1，同一实例跨调用也互斥，不代为关闭；单客户端与2/3宽度、客户端与工厂的冲突在取数前拒绝。服务端已有`--collection-workers`同时传给公司采集与报价配置工厂，独立`screening_cli`新增同名参数，在线默认3。CLI的`--demo`拒绝显式在线采集参数，不创建池或联网；服务端原`--demo`忽略该可信宽度，仍使用隔离人工服务。

任何批次失败、协议不合格、共享字节超限、来源截止或结果未完成，整次不构造快照、不发布部分名单。合法遗漏和`no-match`仍计为`missing_quote_records`，不混同传输失败。`TurnStopped`（整轮取消或期限信号）直接传播。停止后已运行请求可以迟到，直到真正退出才归还名额；其响应关闭，不能改写会话、候选缓存、归档或冻结追踪。发生错误时已发出的并发请求可能多于原串行的首错即停；协调端观察到错误后不再提交。成功固定响应对照的任务、请求、实际字节和覆盖完全一致。

追踪仍只有一次`screening_snapshot`逻辑来源事件，HTTP批次不冒充多个快照。每任务独立复制上下文变量，继承本轮控制器和父事件。东方财富保留原total驱动的分页、跨页总数/顺序/重复校验，所有宽度下均串行。公开源实际限流和耗时留到最终联网评估，不根据旧断连记录声称当前限流。

#77的小修已纳入：模型文字先单独核对6000字符及引用、公司覆盖，再追加程序生成的截止提示；6001字符仍拒绝，不增加重写或模型调用，保持六段上限和引用映射。

2MB精确边界验证还暴露旧腾讯协议正则搜索在大段尾部空白下反复扫描的慢路径。解析器现改为仅从已核验游标匹配，不再搜索后续位置；合法尾部空白和原严格协议反例均保持原处理。20个100KB响应分别用payload填充及尾部空白填充，两种输入都验证完整采集和精确总账，不通过延长期限回避问题。

### 3c 验证与离线计时

[并发测试](../tests/test_screening_collection_concurrency.py)使用真实采集器、独立假HTTP客户端和事件屏障验证1/2/3宽度、5/15/20批、逆序完成、缺板块、合法遗漏、共享池、迟到占槽、客户端互斥、请求/字节竞争和2MB精确边界。[API测试](../tests/test_screening_coverage_api.py)在已有名单后中途失败、取消、整轮/来源到期，先返回再启动新轮，最后放行旧请求，核对新会话、缓存、归档和追踪不变。[追踪测试](../tests/test_turn_trace_events.py)核对5次HTTP仍只有一次逻辑读取及正确父事件；原3a/3b及35场景105轮冻结路由保持原基线。

[计时脚本](../scripts/benchmark_screening_collection.py)用人工五板块目录、人工GB18030响应、固定墙钟/来源时钟及每请求100ms等待，封锁网络和本地配置读取。相同输入下串行/宽度3各五次，逐字段比较快照、筛选结果、覆盖、请求ID批次与字节，输出选样ID的SHA256摘要（确认同一组ID）。没有加速比通过门槛。

本轮完整`python`门禁通过2211项unittest、244项pytest函数用例、全部原固定流程与依赖检查；该总数包含工作区另有的浏览器启动测试。没有修改前端、冻结路由基线或观察器，也未运行真实行情/付费模型评估。

| 离线人工场景 | 请求/任务数 | 实际响应字节 | 理论等待批数：串行→宽度3 | 串行中位数 | 宽度3中位数 |
| --- | ---: | ---: | --- | ---: | ---: |
| standard，batch_size=80 | 5 | 36,695 | 5→2 | 555.995ms | 229.962ms |
| expanded，batch_size=80 | 15 | 147,710 | 15→5 | 1639.943ms | 550.966ms |
| expanded，batch_size=50 | 20 | 147,705 | 20→7 | 2185.807ms | 767.557ms |

这里只证明人工等待可以重叠，不能代表真实腾讯来源速度或覆盖。两个扩大档的ID相同但批次边界不同，行间分隔的字节数相差5；每个配置内串行/并发字节完全相同。下面保留三次命令的完整原始输出。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_screening_collection.py --coverage standard --batch-size 80 --repeat 5 --delay-ms 100
```

```json
{
  "benchmark": "Tencent_screening_fixed_delay",
  "data_mode": "offline_artificial_http_gb18030",
  "interpretation": "structural_overlap_not_live_source_performance",
  "coverage": "standard",
  "batch_size": 80,
  "repeat": 5,
  "delay_ms_per_request": 100,
  "task_descriptions": 5,
  "pool_unfinished_limit": 3,
  "selected_ids": {
    "sse_main": {
      "count": 50,
      "first": "600000.SH",
      "last": "600049.SH",
      "sha256": "46ad180525c63158c1264096c8cb3ceb6468b70660c94a0b1186e8e9168f6c3e"
    },
    "szse_main": {
      "count": 50,
      "first": "000000.SZ",
      "last": "000049.SZ",
      "sha256": "5dfcbd28150679ea19d298b0a56655b25911d561af2960fcf851a6f1fcdcb5ba"
    },
    "star": {
      "count": 50,
      "first": "688000.SH",
      "last": "688049.SH",
      "sha256": "d088375cd6489000b2d10b024c23ea1de753f1287633d686a52bf68b16cff61c"
    },
    "chinext": {
      "count": 50,
      "first": "300000.SZ",
      "last": "300049.SZ",
      "sha256": "edf42df55d5e46045dd2af488af32ad15af8a32f755af69e0de0b9f1fc1b2045"
    },
    "bse": {
      "count": 50,
      "first": "920000.BJ",
      "last": "920049.BJ",
      "sha256": "aaa3cc9cc1d13fdba1ef1765c72ba5151e6ad7c376beca7d9e7cbbfb4a2c40d6"
    }
  },
  "series": {
    "serial": {
      "workers": 1,
      "theoretical_wait_batches": 5,
      "elapsed_ms": [
        558.492,
        554.522,
        556.452,
        554.492,
        555.995
      ],
      "median_ms": 555.995,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        5,
        5,
        5,
        5,
        5
      ],
      "response_bytes": [
        36695,
        36695,
        36695,
        36695,
        36695
      ]
    },
    "parallel_3c": {
      "workers": 3,
      "theoretical_wait_batches": 2,
      "elapsed_ms": [
        216.061,
        229.962,
        230.579,
        230.676,
        227.882
      ],
      "median_ms": 229.962,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        5,
        5,
        5,
        5,
        5
      ],
      "response_bytes": [
        36695,
        36695,
        36695,
        36695,
        36695
      ]
    }
  },
  "snapshots_results_coverage_requests_bytes_equal": true
}
```

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_screening_collection.py --coverage expanded --batch-size 80 --repeat 5 --delay-ms 100
```

```json
{
  "benchmark": "Tencent_screening_fixed_delay",
  "data_mode": "offline_artificial_http_gb18030",
  "interpretation": "structural_overlap_not_live_source_performance",
  "coverage": "expanded",
  "batch_size": 80,
  "repeat": 5,
  "delay_ms_per_request": 100,
  "task_descriptions": 15,
  "pool_unfinished_limit": 3,
  "selected_ids": {
    "sse_main": {
      "count": 200,
      "first": "600000.SH",
      "last": "600398.SH",
      "sha256": "c5669106407aa5194d08b3dbb2998ba1c82de313b33bcb368cbdcbd465e92917"
    },
    "szse_main": {
      "count": 200,
      "first": "000000.SZ",
      "last": "000398.SZ",
      "sha256": "bf40125e61c405cca06b66ded696d1db8c5b392d1904282850489bbb35b303ab"
    },
    "star": {
      "count": 200,
      "first": "688000.SH",
      "last": "688398.SH",
      "sha256": "c98e034bda7c15df3b4aba287dda595f82b2e51ae9b6c7559509a8584ecde418"
    },
    "chinext": {
      "count": 200,
      "first": "300000.SZ",
      "last": "300398.SZ",
      "sha256": "90382a91b85c3e257048db306698b2cd688238eb49f4981de69ac2b464ec5935"
    },
    "bse": {
      "count": 200,
      "first": "920000.BJ",
      "last": "920398.BJ",
      "sha256": "87bacda0d3a757db23cb3de8d4cd10c7e93089b40dee877ab77ed667778ccf6a"
    }
  },
  "series": {
    "serial": {
      "workers": 1,
      "theoretical_wait_batches": 15,
      "elapsed_ms": [
        1647.95,
        1639.355,
        1639.943,
        1641.182,
        1639.789
      ],
      "median_ms": 1639.943,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        15,
        15,
        15,
        15,
        15
      ],
      "response_bytes": [
        147710,
        147710,
        147710,
        147710,
        147710
      ]
    },
    "parallel_3c": {
      "workers": 3,
      "theoretical_wait_batches": 5,
      "elapsed_ms": [
        548.74,
        550.966,
        551.966,
        547.073,
        551.799
      ],
      "median_ms": 550.966,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        15,
        15,
        15,
        15,
        15
      ],
      "response_bytes": [
        147710,
        147710,
        147710,
        147710,
        147710
      ]
    }
  },
  "snapshots_results_coverage_requests_bytes_equal": true
}
```

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_screening_collection.py --coverage expanded --batch-size 50 --repeat 5 --delay-ms 100
```

```json
{
  "benchmark": "Tencent_screening_fixed_delay",
  "data_mode": "offline_artificial_http_gb18030",
  "interpretation": "structural_overlap_not_live_source_performance",
  "coverage": "expanded",
  "batch_size": 50,
  "repeat": 5,
  "delay_ms_per_request": 100,
  "task_descriptions": 20,
  "pool_unfinished_limit": 3,
  "selected_ids": {
    "sse_main": {
      "count": 200,
      "first": "600000.SH",
      "last": "600398.SH",
      "sha256": "c5669106407aa5194d08b3dbb2998ba1c82de313b33bcb368cbdcbd465e92917"
    },
    "szse_main": {
      "count": 200,
      "first": "000000.SZ",
      "last": "000398.SZ",
      "sha256": "bf40125e61c405cca06b66ded696d1db8c5b392d1904282850489bbb35b303ab"
    },
    "star": {
      "count": 200,
      "first": "688000.SH",
      "last": "688398.SH",
      "sha256": "c98e034bda7c15df3b4aba287dda595f82b2e51ae9b6c7559509a8584ecde418"
    },
    "chinext": {
      "count": 200,
      "first": "300000.SZ",
      "last": "300398.SZ",
      "sha256": "90382a91b85c3e257048db306698b2cd688238eb49f4981de69ac2b464ec5935"
    },
    "bse": {
      "count": 200,
      "first": "920000.BJ",
      "last": "920398.BJ",
      "sha256": "87bacda0d3a757db23cb3de8d4cd10c7e93089b40dee877ab77ed667778ccf6a"
    }
  },
  "series": {
    "serial": {
      "workers": 1,
      "theoretical_wait_batches": 20,
      "elapsed_ms": [
        2180.279,
        2186.693,
        2187.445,
        2183.224,
        2185.807
      ],
      "median_ms": 2185.807,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        20,
        20,
        20,
        20,
        20
      ],
      "response_bytes": [
        147705,
        147705,
        147705,
        147705,
        147705
      ]
    },
    "parallel_3c": {
      "workers": 3,
      "theoretical_wait_batches": 7,
      "elapsed_ms": [
        767.557,
        769.416,
        765.848,
        764.277,
        769.935
      ],
      "median_ms": 767.557,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        20,
        20,
        20,
        20,
        20
      ],
      "response_bytes": [
        147705,
        147705,
        147705,
        147705,
        147705
      ]
    }
  },
  "snapshots_results_coverage_requests_bytes_equal": true
}
```

## 启动配置与串行对照

正常启动默认每批公司资料采集最多同时运行三个领域任务，三家公司共同使用这三个名额。可用服务端参数收紧为一个或两个：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --collection-workers 1
.\.venv\Scripts\python.exe -m trading_companion.api_server --collection-workers 2
.\.venv\Scripts\python.exe -m trading_companion.api_server --collection-workers 3
```

参数只接受1、2、3，默认3；非法值在加载配置、启动服务前被拒绝。设为1用于同一数据源的串行/并发真实评估对照，也可在来源对重叠请求敏感时手动退回逐领域获取。没有自动重试或换源。配置只来自服务端启动，聊天和HTTP请求没有修改它的字段。--demo和网页独立Dev Mode仍使用原人工夹具，不创建这些网络采集任务。

参数控制单批采集的宽度，所有默认公司和腾讯报价采集服务还共用进程级的三个在途名额。因此多个会话也不能各自放入三个任务使池膨胀；设为1是每批资料按公司、领域或报价批次依次执行，不把整个应用所有会话和所有HTTP路径改为全局串行。3b、3c只应用一次宽度，不能外层三家公司或五个板块各放入三个任务。东方财富在宽度1/2/3下均保持串行。

直接使用ResearchDataService时，内置客户端或可信session_factory默认宽度为3，可显式传max_workers=1对照。每个任务创建自己的客户端并负责关闭。旧session=注入默认宽度为1，且同一服务实例的借用客户端跨采集调用也互斥访问；客户端仍由注入者关闭。只给一个session却显式要求2/3，或同时提供session和工厂，均提前拒绝。工厂必须每次返回独立客户端；注入时钟和工厂是可信、可在线程间调用的服务端依赖。

## 生命周期与上下文

CollectionExecutor使用最多三个线程的ThreadPoolExecutor，即让现有同步网络调用在固定工作线程中重叠等待。默认实例在首次采集时创建，并由本进程复用。提交前还必须取得三个在途许可之一；Future，即后台任务的结果对象，真正完成或成功取消后才释放许可。这样线程池内部也不会无限堆积待执行任务。

协调端每隔至多20 ms的等待周期重新检查停止条件，空闲名额不足时只等待自己剩余的来源时间。超时或取消后不退出线程池的with，也不调用等待全部任务的shutdown；只关闭当前采集作用域、取消还未运行的任务。已运行的旧任务继续占名额，后续请求不能另开池补偿。测试注入专用执行器并在释放假来源后关闭；生产默认池由解释器退出机制回收。

Python无法强制结束一个已经阻塞的同步请求。生产传输保留连接/读取超时和流式检查点；无视超时的注入客户端会占住有限名额，可能影响后续采集和进程退出，但不会使线程持续增加。这个限制不能描述成硬中断或供应商侧撤销。

每次提交分别复制contextvars.copy_context()，即本调用链的上下文快照，再由任务自己的副本执行。所有副本指向同一个本轮TurnControl（单轮取消、期限和预算控制器）和追踪收集器，但不会同时进入同一个Context对象。父事件也从提交位置继承。不同轮次分别复制，不能把上一轮的控制器或事件带到下一轮。

## 同批资源账本

每批采集在协调端只建立一个CollectionScope，保存绝对截止时间、关闭标志、已接纳结果，以及请求和字节的公司分账、整批总账。账本用同一把锁保证公司和总量的检查、扣减是一个动作，锁内不做网络、磁盘、等待或外部回调。后台任务只持有所属公司的预算视图，共享关闭状态和期限，不能借用其他公司的余额。

| 资源 | 规则 |
| --- | --- |
| 来源时间 | 整批默认20秒，可信配置至多30秒；公司和领域的排队均计入时间，不能重新起算 |
| 单请求时间 | 单请求上限、来源剩余时间、整轮剩余时间取最小值；仍是协作式限制 |
| 请求次数 | 由各公司领域和来源推导，一般每领域一次，巨潮公告最多两次；每家公司完整四领域默认最多4次，巨潮配置最多5次；三家合计最多12次或15次 |
| 单响应大小 | 通常最多512,000字节，巨潮目录最多2,000,000字节 |
| 累计响应体 | 各公司所选端点上限之和；每家公司完整四领域默认最多2,048,000字节，巨潮配置最多4,048,000字节；三家最多6,144,000或12,144,000字节，子集相应收紧 |

禁止历史背景和未验证市场映射的领域不获得额外请求预算。每次尝试发送前原子预留一次；连接失败仍已消耗，不退款重用。巨潮的目录和公告各预留一次，复用该领域自己的客户端。新浪标题列表也使用同一作用域。

保留Content-Length声明校验和单响应限制，每块数据追加前再扣共享字节余额。已经处理的响应体即使解析失败也不退还；超出的块不会追加到缓存。这个账本限制应用保留/处理的响应体，不保证底层网络没有提前收到一个读取块。固定失败码为research_request_budget_exhausted、research_byte_budget_exhausted，已登记到追踪词表。

模型次数、日线计算次数和token阈值继续由原TurnControl管理，没有因为线程变多获得新额度。单次采集的截止时间也不能代替整轮截止时间。

## 多公司调度与共用窗口的取舍（3b）

内置ResearchDataService增加collect_many，即一次获取最多三家公司资料的可信内部入口。1～3个精确类型的请求、证券不重复、合法且不在未来的时间和非空无重复的领域全部在HTTP之前校验。collect委托单元素版本，原返回类型不变。

公司与领域组合压平成最多12个任务描述，由一个协调流程逐项提交；同时未完成的Future仍最多3个，不能把12个Future一次塞入线程池队列。并发时按领域轮转公司，例如A财务、B财务、C财务；宽度1保持公司优先、领域依次。没有外层“公司线程”在同一个池里等待领域子任务，因此不会出现三个外层任务占满池后无人执行子任务的死锁。

**与3a有一项明确的行为变化：** 3a外层逐公司调用，每家公司分别起算默认20秒；3家公司可能合计约60秒。3b整批只起算一次默认20秒，可信配置最多30秒，宽度1也遵守同一窗口。来源缓慢时，3b可能比原来更早只取得部分资料；没有扩大原90秒整轮期限，也没有把每家额度乘三。

资料先在主线程按请求顺序校验，任何证券、日期、领域或批次数量不匹配都在技术计算之前拒绝。随后仍按原公司、领域、证据顺序合并及分配E引用别名；技术分析在主线程逐家公司预留并执行，最多三次。仅精确的内置服务走多公司入口；不透明注入服务、子类和人工夹具保留原逐公司调用、技术计算的交替顺序，不根据属性猜测其并发能力。

来源窗口到期后，已接纳结果保留，其他领域在各公司coverage（资料覆盖状态）里标为research_data_deadline_exceeded。[回答模块](../src/trading_companion/product_answer.py)会在自然回答或兜底文字中追加简短的程序提示，指出哪个证券的哪些资料“未取得”，不增加模型调用或新界面；提示不伪造证据引用，六段上限及原引用绑定保持。普通来源失败继续仅影响对应公司/领域；缺项比较不会补零。

## 合并、停止和追踪

后台任务只返回完全读取、解析后的ResearchDataSection，即一份脱离连接的领域证据；响应和自有客户端先关闭。不返回可继续取数的生成器，不写研究会话或归档。

协调端接纳结果时再次核对作用域，最终按原kinds顺序组批。产品主流程保留原证券/领域/时间校验、公司顺序、证据顺序和E引用别名分配。正常领域失败只影响该领域。来源截止时保留已经接纳的按时结果，未接纳的领域标为research_data_deadline_exceeded；之后到达的数据不并入批次。

整轮取消或截止继续抛TurnStopped，即原上层能识别的停止信号；退出前关闭作用域。真正发布会话状态仍由既有控制器的最终发布锁决定，不由后台回调决定。回调只释放在途许可；新轮次可以开始，上一轮随后完成也不能追加到任何已返回结果或已保存追踪。

内置采集器仍按领域记录来源事件，不额外重复计整批调用，三家四领域最多12个领域事件。并发事件按实际开始先后记录，可以交错；它们的父事件和运行号必须正确。领域读取计数不等于HTTP请求数，例如巨潮一个领域可能两次请求。

来源窗口到期时，协调端将已进入但尚未完成的领域事件在逻辑上结束为stopped，代码research_data_deadline_exceeded；普通来源故障仍为failed。此操作不结束物理线程，也不释放它的在途许可。追踪事件登记与截止同时发生时也会结束到原运行号；已结束事件不会重复计数。合作式返回的截止状态同样归stopped。整轮取消仍保留未完成来源的unfinished语义，收尾冻结后迟到结束不重写文件；没有进入来源边界的排队项不虚构来源事件。格式继续使用trace-v2。

真实评估必须统计“因来源窗口到期而部分覆盖的轮数 / 实际尝试公司资料采集的轮数”，并分别报告单/多公司、领域、串行/并发配置、失败率及耗时分位数，不能只看成功。未取消、未整轮停止的公司研究轮中，sources.stopped提供来源截止信号，sources.failed提供普通来源失败计数；再按固定事件码和最终coverage核对每家公司缺项。若等待全局名额导致某领域从未进入，来源事件不会凭空补齐，因此精确的覆盖比例仍以coverage为准，详细事件截断也须报告；不要把所有stopped（包含整轮停止）直接当成来源超时比例。比例偏高时再依据同来源实测比较可信窗口20秒和30秒，当前不自动延长、不重试或换源。

## 验证与离线观测

[执行器测试](../tests/test_collection_execution.py)用事件、屏障和可控时钟验证最大在途数、各任务上下文、请求/字节原子预留、公司不能互借预算、取消/整轮截止、按时结果保留、超时后多轮不增加线程和许可恢复。[原单公司入口测试](../tests/test_company_collection_concurrency.py)保留巨潮两次预留、借用客户端互斥和3a回归；[多公司测试](../tests/test_multi_company_collection.py)运行真正的采集器，验证跨公司重叠、全批1/2/3宽度、逆序完成后证据/引用/比较一致、第三家公司部分超时、共用截止不重置、坏批次阻止后续技术取数，以及原技术额度。[API测试](../tests/test_product_api.py)覆盖真实多公司HTTP入口、自然回答和兜底的超时提示、取消后新轮继续、旧轮不发布不归档不重写追踪。[追踪事件测试](../tests/test_turn_trace_events.py)检查12来源的同父事件，以及stopped/failed/unfinished区分。

开启新默认实现后仍运行35场景、105轮冻结路由对照；旧夹具直接返回整批资料，因此该对照证明路由/状态契约，没有单独覆盖领域调度，不能替代上述真实采集器入口测试。旧观察器和两份基线文件均保持原文。

以下保留3a单公司固定延迟实验的历史记录，3b多公司对照在后文：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_company_collection.py --repeat 5 --delay-ms 100
```

以下是2026-10-27在Windows本地的原始输出。**离线固定延迟，结构性收益，不代表真实数据源**。每个请求的人工等待均为100 ms，四领域、同样的资料、每轮四次请求和489字节响应体；没有模型、网络、密钥或.env读取。串行四批等待，对应三线程两批等待，理想结构不是必然三倍；实际包含调度开销。结果完全一致，没有在单元测试中设置加速比门槛。

```json
{
  "benchmark": "company_sections_fixed_delay",
  "data_mode": "offline_artificial_http",
  "interpretation": "structural_overlap_not_live_source_performance",
  "repeat": 5,
  "delay_ms_per_request": 100,
  "sections": 4,
  "pool_unfinished_limit": 3,
  "series": {
    "serial": {
      "workers": 1,
      "elapsed_ms": [
        426.655,
        437.033,
        438.001,
        436.72,
        436.242
      ],
      "median_ms": 436.72,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        4,
        4,
        4,
        4,
        4
      ],
      "response_bytes": [
        489,
        489,
        489,
        489,
        489
      ]
    },
    "parallel": {
      "workers": 3,
      "elapsed_ms": [
        218.77,
        217.359,
        217.783,
        218.657,
        216.558
      ],
      "median_ms": 217.783,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        4,
        4,
        4,
        4,
        4
      ],
      "response_bytes": [
        489,
        489,
        489,
        489,
        489
      ]
    }
  },
  "serial_over_parallel": 2.0053,
  "results_equal": true
}
```

这只是等待重叠的本地证据。真实限流、延迟、失败率和模型成本要在后续真实评估中另测；可用上述启动参数在同来源、同模型和同问题下做对照，不能把本段比值写成线上提速承诺。


## 3b三种调度的离线固定延迟对照

2026-10-28：三种模式使用同一专用三槽执行器、每任务独立人工HTTP客户端、每请求100毫秒固定等待，各重复5次。资料按请求证券生成，全部结果逐字段完全一致；禁止网络、密钥和.env读取。没有模型或技术计算。**离线固定延迟，结构性收益，不代表真实数据源**。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_company_collection.py --companies 3 --kinds financial --repeat 5 --delay-ms 100
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_company_collection.py --companies 3 --kinds all --repeat 5 --delay-ms 100
.\.venv\Scripts\python.exe -X utf8 -B scripts/benchmark_company_collection.py --repeat 5 --delay-ms 100
```

| 公司/领域 | 模式 | 理论等待批数 | 中位耗时（ms） | 每轮请求/字节 | 最大在途 |
| --- | --- | --- | --- | --- | --- |
| 3家公司/财务 | serial | 3 | 325.402 | 3/792 | 1 |
| 3家公司/财务 | company_parallel_3a | 3 | 327.134 | 3/792 | 1 |
| 3家公司/财务 | batch_parallel_3b | 1 | 108.641 | 3/792 | 3 |
| 3家公司/四领域 | serial | 12 | 1304.569 | 12/1467 | 1 |
| 3家公司/四领域 | company_parallel_3a | 6 | 653.521 | 12/1467 | 3 |
| 3家公司/四领域 | batch_parallel_3b | 4 | 434.967 | 12/1467 | 3 |
| 1家公司/四领域 | serial | 4 | 435.174 | 4/489 | 1 |
| 1家公司/四领域 | company_parallel_3a | 2 | 217.523 | 4/489 | 3 |
| 1家公司/四领域 | batch_parallel_3b | 2 | 217.726 | 4/489 | 3 |

三公司单财务：3a与串行都要三批，3b一批，3a/3b中位比值3.0111；三公司四领域：串行十二批，3a六批，3b四批，3a/3b为1.5025，不能统一写成三倍。单公司3a与3b都为两批，中位比值0.9991，保留原结构。真实限流、来源失败、时间窗口及串行技术/模型都会改变整轮效果；后续真实评估同时观察覆盖率，不设置测试加速比门槛。

三份原始输出如下：

```json
{
  "benchmark": "company_candidates_fixed_delay",
  "data_mode": "offline_artificial_http",
  "interpretation": "structural_overlap_not_live_source_performance",
  "repeat": 5,
  "delay_ms_per_request": 100,
  "companies": 3,
  "kinds": [
    "financial"
  ],
  "task_descriptions": 3,
  "pool_unfinished_limit": 3,
  "series": {
    "serial": {
      "workers": 1,
      "theoretical_wait_batches": 3,
      "elapsed_ms": [
        321.573,
        324.388,
        325.402,
        325.64,
        329.312
      ],
      "median_ms": 325.402,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        3,
        3,
        3,
        3,
        3
      ],
      "response_bytes": [
        792,
        792,
        792,
        792,
        792
      ]
    },
    "company_parallel_3a": {
      "workers": 3,
      "theoretical_wait_batches": 3,
      "elapsed_ms": [
        327.134,
        327.72,
        326.212,
        328.06,
        325.826
      ],
      "median_ms": 327.134,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        3,
        3,
        3,
        3,
        3
      ],
      "response_bytes": [
        792,
        792,
        792,
        792,
        792
      ]
    },
    "batch_parallel_3b": {
      "workers": 3,
      "theoretical_wait_batches": 1,
      "elapsed_ms": [
        108.641,
        107.97,
        109.921,
        108.057,
        109.211
      ],
      "median_ms": 108.641,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        3,
        3,
        3,
        3,
        3
      ],
      "response_bytes": [
        792,
        792,
        792,
        792,
        792
      ]
    }
  },
  "serial_over_3b": 2.9952,
  "3a_over_3b": 3.0111,
  "results_equal": true
}
```

```json
{
  "benchmark": "company_candidates_fixed_delay",
  "data_mode": "offline_artificial_http",
  "interpretation": "structural_overlap_not_live_source_performance",
  "repeat": 5,
  "delay_ms_per_request": 100,
  "companies": 3,
  "kinds": [
    "profile",
    "financial",
    "news",
    "announcements"
  ],
  "task_descriptions": 12,
  "pool_unfinished_limit": 3,
  "series": {
    "serial": {
      "workers": 1,
      "theoretical_wait_batches": 12,
      "elapsed_ms": [
        1303.843,
        1306.651,
        1305.291,
        1304.335,
        1304.569
      ],
      "median_ms": 1304.569,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        12,
        12,
        12,
        12,
        12
      ],
      "response_bytes": [
        1467,
        1467,
        1467,
        1467,
        1467
      ]
    },
    "company_parallel_3a": {
      "workers": 3,
      "theoretical_wait_batches": 6,
      "elapsed_ms": [
        651.312,
        653.521,
        654.154,
        654.028,
        652.966
      ],
      "median_ms": 653.521,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        12,
        12,
        12,
        12,
        12
      ],
      "response_bytes": [
        1467,
        1467,
        1467,
        1467,
        1467
      ]
    },
    "batch_parallel_3b": {
      "workers": 3,
      "theoretical_wait_batches": 4,
      "elapsed_ms": [
        434.967,
        434.94,
        435.269,
        437.631,
        433.938
      ],
      "median_ms": 434.967,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        12,
        12,
        12,
        12,
        12
      ],
      "response_bytes": [
        1467,
        1467,
        1467,
        1467,
        1467
      ]
    }
  },
  "serial_over_3b": 2.9992,
  "3a_over_3b": 1.5025,
  "results_equal": true
}
```

```json
{
  "benchmark": "company_candidates_fixed_delay",
  "data_mode": "offline_artificial_http",
  "interpretation": "structural_overlap_not_live_source_performance",
  "repeat": 5,
  "delay_ms_per_request": 100,
  "companies": 1,
  "kinds": [
    "profile",
    "financial",
    "news",
    "announcements"
  ],
  "task_descriptions": 4,
  "pool_unfinished_limit": 3,
  "series": {
    "serial": {
      "workers": 1,
      "theoretical_wait_batches": 4,
      "elapsed_ms": [
        430.283,
        436.726,
        435.44,
        435.174,
        435.091
      ],
      "median_ms": 435.174,
      "maximum_inflight": [
        1,
        1,
        1,
        1,
        1
      ],
      "requests": [
        4,
        4,
        4,
        4,
        4
      ],
      "response_bytes": [
        489,
        489,
        489,
        489,
        489
      ]
    },
    "company_parallel_3a": {
      "workers": 3,
      "theoretical_wait_batches": 2,
      "elapsed_ms": [
        217.523,
        218.752,
        216.56,
        216.499,
        218.94
      ],
      "median_ms": 217.523,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        4,
        4,
        4,
        4,
        4
      ],
      "response_bytes": [
        489,
        489,
        489,
        489,
        489
      ]
    },
    "batch_parallel_3b": {
      "workers": 3,
      "theoretical_wait_batches": 2,
      "elapsed_ms": [
        217.95,
        217.726,
        217.421,
        217.911,
        216.993
      ],
      "median_ms": 217.726,
      "maximum_inflight": [
        3,
        3,
        3,
        3,
        3
      ],
      "requests": [
        4,
        4,
        4,
        4,
        4
      ],
      "response_bytes": [
        489,
        489,
        489,
        489,
        489
      ]
    }
  },
  "serial_over_3b": 1.9987,
  "3a_over_3b": 0.9991,
  "results_equal": true
}
```

# 有上限的公司资料并发采集（3a、3b）

2026-10-28：3a 已完成同公司领域并发，3b 已在工作区实现最多三家公司的统一采集，待审核。公司资料、财务、新闻和公告进入同一队列，同时最多三个领域任务；筛选批次、技术计算、模型及市场背景请求的执行方式不变。实现位置是[采集执行模块](../src/trading_companion/collection_execution.py)、[公司采集器](../src/trading_companion/company_research.py)、[产品合并流程](../src/trading_companion/product_research.py)和[新浪新闻适配器](../src/trading_companion/company_news_sina.py)。

## 启动配置与串行对照

正常启动默认每批公司资料采集最多同时运行三个领域任务，三家公司共同使用这三个名额。可用服务端参数收紧为一个或两个：

```powershell
.\.venv\Scripts\python.exe -m trading_companion.api_server --collection-workers 1
.\.venv\Scripts\python.exe -m trading_companion.api_server --collection-workers 2
.\.venv\Scripts\python.exe -m trading_companion.api_server --collection-workers 3
```

参数只接受1、2、3，默认3；非法值在加载配置、启动服务前被拒绝。设为1用于同一数据源的串行/并发真实评估对照，也可在来源对重叠请求敏感时手动退回逐领域获取。没有自动重试或换源。配置只来自服务端启动，聊天和HTTP请求没有修改它的字段。--demo和网页独立Dev Mode仍使用原人工夹具，不创建这些网络采集任务。

参数控制单批采集的宽度，所有默认公司采集服务还共用进程级的三个在途名额。因此多个会话也不能各自放入三个任务使池膨胀；设为1是每批资料按公司、领域依次执行，不把整个应用所有会话和所有HTTP路径改为全局串行。3b只应用一次宽度，不能外层三家公司各放入三个领域。筛选批次留到3c，届时也须共用执行器及整轮额度。

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

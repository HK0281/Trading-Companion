# RSI、BOLL、ATR 与量能：纯计算设计

日期：2026-10-16（America/Los_Angeles）

## 范围与接口

补齐 PRD M1a 的剩余四类指标，保留原 SMA/EMA/MACD、`analyze_daily_bars()` 与行情 CLI。计算函数不联网、不调用模型、不修改输入，不新增第三方依赖。多组 SMA 可对同一份已加载日线分别调用现有 `sma_close`；本轮不新增批量分析服务。

| 数值函数 | 日期结果点函数 | 默认参数与输出 |
| --- | --- | --- |
| `rsi_close` | `rsi_close_points` | `window=14`；标量或 `RsiPoint.value` |
| `boll_close` | `boll_close_points` | `window=20, stddev_multiplier=2.0`；`middle/upper/lower` |
| `atr` | `atr_points` | `window=14`；标量或 `AtrPoint.value`，单位元/股 |
| `volume_activity` | `volume_activity_points` | `window=20`；当日股数、含当日均量、此前均量、倍率及倍率状态 |

所有函数都接收同一股票、日期严格递增的 `DailyBar` 序列，输出与输入等长；日期点绑定股票、日期与参数。空输入返回空列表；不足记录处保留 `None`。不排序，不为日期间断补零或复制价格。截止日由调用方先用 `bars_as_of_close` 筛选。

即使历史不足，也检查整个输入的类型、股票和日期。非法输入抛 `ValueError`，不伪装成无数据或历史不足。

计算层延续 EMA/MACD 的正整数周期约定，拒绝布尔值，支持周期 1。BOLL 倍数接受有限正数。PRD 中 RSI/ATR/BOLL 周期 2–120、BOLL 倍数不超过 4 等产品限制留给未来请求/工具层；不把本轮纯函数直接当成模型工具。

## 固定算法口径

### RSI

1. 相邻收盘差分成非负上涨值与下跌绝对值；首条没有变化值。
2. 前 n 个上涨/下跌值各自取均值初始化，最早第 n+1 条日线可用。
3. 后续使用 Wilder 平滑：`average = previous + (current - previous) / n`。
4. RSI 等价于 `100 × average_gain / (average_gain + average_loss)`；实现先缩放以避免大数相加溢出。
5. 双均值为 0 时固定为 50；只有上涨时为 100；只有下跌时为 0。这些是项目算法约定。

### BOLL

- 中轨是最近 n 个收盘价的算术均值；上下轨为中轨 ± 倍数 × 总体标准差，固定 `ddof=0`。
- 使用标准库 `statistics.mean` 和 `statistics.pstdev`，逐个窗口计算，避免原始平方和相减导致的精度抵消。
- 第 n 条起可用；常量价格三轨重合。下轨可以为负，不能把指标结果重新套入正价格日线校验。
- 任何轨道无法表示为有限浮点数时抛出计算错误，不输出无穷大。

### ATR

- 从第二条开始，TR 取 `high-low`、`abs(high-previous_close)`、`abs(low-previous_close)` 的最大值。
- 首条没有前收盘，TR 不可用，不能用首条高低差代替。
- 前 n 个 TR 平均初始化，最早第 n+1 条日线可用；后续同样采用权重 `1/n` 的 Wilder 平滑。
- 单位是元/股；ATR=0 合法，不生成涨跌方向或止损价。

### 量能

| 字段 | 含义与首个可用位置 |
| --- | --- |
| `volume_shares` | 当日成交股数；每条可用 |
| `volume_ma` | 含当日在内的最近 n 条均量；第 n 条起 |
| `previous_volume_ma` | 严格在当日前的 n 条均量；第 n+1 条起 |
| `relative_volume` | 当日股数除以前述基准；基准非零时可用 |
| `relative_volume_status` | `available` / `insufficient_history` / `undefined` |

历史不足时，基准及倍率为 `None`，状态为 `insufficient_history`。此前均量为 0 时，基准保留合法的 0，倍率为 `None`，状态为 `undefined`。基准为正且当日零成交时，倍率是可用的 0。

滚动股数总和使用 Python 整数，避免先转换浮点后丢失小股数或产生滚动抵消。只在返回均量和倍率时做除法；不可表示的结果抛 `ValueError`。该倍率没有分钟级成交节奏信息，不称为盘中“量比”。

## 实现取舍与后续边界

- 日线验证提取为共享内部函数；收盘 EMA/MACD 及新指标复用校验，旧公开接口保持兼容。
- RSI 与 ATR 共用独立 Wilder 平滑，不将其误用成 EMA 的 `2/(n+1)` 权重。
- 以 m 条输入、周期 n 计，RSI/ATR/量能按记录线性处理；BOLL 逐窗为 O(m×n)，不足一个窗口时只作 O(m) 的输入检查和空值输出。当前日线规模优先选择清晰稳定的计算。
- 所有包装函数包含结果数组，空间至少 O(m)；不宣称整套函数是常数空间。
- 计算不舍入；展示格式化留上层。递推指标改变计算起点可能改变结果。
- 本轮量能倍率状态用于区分不足与零基准。后续M1b已在[独立多指标服务](multi-indicator-design.md)接入逐字段可用性、计算起始日/种子元数据、关系、交叉与证据。
- 原 `analyze_daily_bars` 和行情 CLI 仍只输出单 SMA；新增多指标服务不等于模型Agent已完成。

## 验证与试用

测试采用已知人工样本与独立 Decimal 参考，覆盖默认和短周期初始化、首次递推、恒定/单向价格、零成交、日期对齐、非法输入、不同起点及未来数据隔离。普通价格的绝对误差容限为 `1e-10`；极大数单独检查有限性或相对误差。

2026-10-16 验证记录：Windows / Python 3.12.10，新增 47 项指标测试通过；UserProxy 独立执行全量 193 项离线测试，0 失败、0 错误。README 四指标样例、旧 CSV 行情 CLI 与 Fake 模型 CLI 均退出 0。初次全量测试遇到沙箱临时文件权限限制，经自动审核批准在沙箱外原样运行后通过；未修改业务代码规避测试，也未联网或调用真实模型。

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -p test_technical_indicators.py -v
python -m unittest discover -s tests -v
```

启动已配置好环境的 Python 后，可用人工 CSV 缩短周期演示：

```python
from trading_companion.csv_loader import load_daily_bars
from trading_companion.indicators import (
    rsi_close_points, boll_close_points, atr_points, volume_activity_points,
)

bars = load_daily_bars("data/sample_daily_bars_synthetic.csv")
print(rsi_close_points(bars, window=3)[-1])
print(boll_close_points(bars, window=3)[-1])
print(atr_points(bars, window=3)[-1])
print(volume_activity_points(bars, window=3)[-1])
```

该文件只有五条人工记录，默认周期的指标字段不足；缩短周期仅用于展示算法接口，不是真实行情或推荐参数。测试不调用外部行情或真实模型。

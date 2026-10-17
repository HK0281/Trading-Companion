# Trading Companion

Trading Companion is a research and decision-support assistant for China's A-share market. All trading decisions and operations remain with the user.

## Current status

The foundation for the first product agent is implemented:

- Validated daily OHLCV records and local CSV loading.
- A Tencent adapter and an optional AKShare/Eastmoney adapter for historical, unadjusted daily bars.
- Trade-date filtering and deterministic closing-price simple moving averages (SMA).
- A standalone SMA-seeded closing-price EMA calculation with date-aligned result points; not yet integrated into the analysis CLI.
- A standalone MACD calculation with SMA-seeded DEA, aligned DIF/DEA/histogram fields, and date/period result points.
- A reusable analysis function with structured results, provenance, and explicit failure states.
- A CLI with table and JSON output, plus offline tests.
- A separate text-model interface, offline fake model, and DeepSeek HTTP adapter with configuration and response validation.

This is not yet a conversational agent. The model adapter is tested with simulated responses, and the developer reported one successful live `deepseek-flash` text request (`OK`, 29 prompt tokens and 1 completion token) on 2026-10-13. This verifies that one text request, not tool calling or market analysis. Conversation state, the remaining PRD indicators, multi-indicator integration, Jev, news, fundamentals, screening, DuckDB persistence, API/UI, and historical strategy evaluation are not implemented.

The intended product scope includes main-board, STAR Market, ST, and newly listed stocks. The current foundation reads price history; it does not yet identify historical ST status, enforce board/listing-specific trading rules, or distinguish suspensions from other missing-data causes.

## Safety boundaries

The application does not connect to brokerage accounts, place/modify/cancel orders, manage funds, or guarantee returns. A technical indicator is not a trading recommendation.

## Setup: Windows PowerShell / VS Code

Open the project folder in VS Code and open its PowerShell terminal. Commands below assume the terminal is in the project root.

Python 3.11 or newer is required. Create the virtual environment only if it does not already exist:

```powershell
py -3.11 -m venv .venv
```

You do not need to activate the environment or change PowerShell's execution policy. Use its Python executable directly.

For a source-tree session:

```powershell
$env:PYTHONPATH='src'
```

This setting applies to the current terminal. Alternatively, install the package in editable mode to avoid setting PYTHONPATH in later terminals:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

For online data sources and the full test suite, install the optional data dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[data]"
```

AKShare is pinned to 1.19.1. Tencent uses requests directly, with an explicit dependency of `requests>=2.31,<3`; it does not call the AKShare Tencent wrapper. The CSV runtime itself needs no third-party packages. In VS Code, use **Python: Select Interpreter** and select the project's `.venv\Scripts\python.exe`.

## Run with synthetic data

```powershell
.\.venv\Scripts\python.exe -m trading_companion data\sample_daily_bars_synthetic.csv --as-of 2026-09-23 --window 3
```

The sample is artificial and must not be used for investment decisions. This query uses only the first three sample records, with closing prices 10.20, 10.35, and 10.10. SMA3 is approximately 10.2167, displayed as 10.22 in the table. Earlier rows show N/A until enough observations are available.

For machine-readable output:

```powershell
.\.venv\Scripts\python.exe -m trading_companion data\sample_daily_bars_synthetic.csv --as-of 2026-09-25 --window 20 --format json
```

This returns `insufficient_history`: five bars are available, 15 more are needed, and SMA values are `null`.

Optional `--symbol` selects one stock from a multi-stock CSV. Optional `--start` excludes earlier records. Without `--symbol`, all selected records must belong to one stock.

## Try the standalone EMA calculation

The existing market CLI still outputs SMA only. To try the new pure calculation, set `PYTHONPATH` as above, start Python with `.\.venv\Scripts\python.exe`, then enter:

```python
from trading_companion.csv_loader import load_daily_bars
from trading_companion.indicators import ema_close
bars = load_daily_bars("data/sample_daily_bars_synthetic.csv")
print(ema_close(bars, window=3))
```

The values are approximately `[None, None, 10.216667, 10.108333, 10.254167]`. The function retains full calculation precision; these rounded values are for explanation only. This uses synthetic local data, not the model or a market request. Enter `exit()` to return to PowerShell.

EMA is initialized with the mean of the first `window` closes, followed by `alpha = 2 / (window + 1)` recursive updates. It returns one value per input record, leaving unavailable values as `None`. It rejects invalid periods, mixed instruments, and non-increasing dates rather than sorting silently. Its calculation start can affect the result; it does not fetch warm-up history automatically. See the [short EMA design](docs/ema-design.md).

## Try the standalone MACD calculation

MACD is also a pure calculation, not a model call or a trading signal. In the same Python session, use the five-row synthetic sample with deliberately shortened demonstration periods:

```python
from trading_companion.indicators import macd_close_points
points = macd_close_points(bars, fast=2, slow=3, signal=2)
print(points[-1])
```

Each `MacdPoint` records instrument, date, periods, DIF, DEA, and `histogram = 2 * (DIF - DEA)`. The default `(12, 26, 9)` needs 26 records for DIF and 34 for DEA/histogram; on this five-row sample, all default fields would be unavailable. Do not replace unavailable fields with zero or treat these demonstration periods as trading recommendations.

Both price EMAs reuse one validated closing-price list. An internal numeric EMA core also handles negative and zero DIF values, without disguising them as positive-price OHLC records. The original `ema_close` interface and SMA seed convention remain. The market CLI still outputs SMA only; multi-indicator integration, crossover facts, and initialization warnings are later work. See the [short MACD design](docs/macd-design.md).

## Run with historical A-share data

Tencent is the recommended first online source:

```powershell
.\.venv\Scripts\python.exe -m trading_companion --source tencent --symbol 000001.SZ --start 2025-09-01 --as-of 2025-09-05 --window 3 --format json --timeout 15
```

The existing Eastmoney command is still available:

```powershell
.\.venv\Scripts\python.exe -m trading_companion --source akshare --symbol 000001.SZ --start 2025-09-01 --as-of 2025-09-05 --window 3 --format json --timeout 15
```

- Both online modes require `--symbol` and `--start`; neither takes a CSV path. The default remains CSV for backward compatibility.
- Source selection is explicit: `tencent` uses Tencent, `akshare` uses Eastmoney via AKShare. No automatic fallback occurs. JSON records the actual source as `tencent` or `akshare_eastmoney`.
- Identifiers use a six-digit code and exchange suffix, such as `000001.SZ` or `688001.SH`. Syntax validation does not establish that a stock exists.
- Both dates are inclusive. The SMA window counts available bar records, not calendar days. No earlier warm-up records are fetched automatically.
- Both sources request unadjusted daily prices. Tencent responses must contain the `day` series; adjusted `qfqday`/`hfqday` are never substituted.
- Eastmoney volume is converted from hands to shares (100 shares per hand); its amount is already in CNY.
- The end date must be **before today in Shanghai**. Current-day bars are deliberately excluded until exchange-session handling exists.
- The default timeout is 15 seconds for each connection/read wait, not a total deadline for the full analysis. Multi-year queries involve multiple requests.
- Public endpoints may fail, return empty data, or change their schema. Empty data alone does not establish that a stock is suspended.

### Tencent mapping and coverage

The Tencent adapter reads the public JSON endpoint directly. This avoids the installed AKShare Tencent wrapper's `sz000` volume conversion issue and its extra listing-history request without a timeout. It also avoids SDK progress output contaminating CLI JSON.

- Supported equity prefixes: SH `600/601/603/605/688`, SZ `000/001/002/003/300/301`. BJ, `689` CDRs, funds, and indices are explicitly rejected until their field/unit contracts are verified.
- ST and newly listed stocks within those prefixes are not intentionally excluded. Historical ST status, listing rules, and complete provider coverage are not yet verified.
- Raw Tencent main-board and ChiNext volume is in hands and is multiplied by 100; STAR (`688`) volume is already in shares. Amount is in ten-thousand CNY and is multiplied by 10,000. Decimal arithmetic is used before validating whole share counts.
- Upstream rounding remains: conversion to shares/CNY does not recover precision lost at the source. An independent Sina comparison for `000001.SZ` on 2025-09-01 yielded 185,845,902 shares and CNY 2,214,306,332 versus Tencent's normalized 185,845,900 shares and CNY 2,214,306,300. This is a spot check, not a comprehensive data-quality certification.
- Tencent may ignore the requested start date and return up to 640 records ending at the requested cutoff. The adapter requests each calendar-year slice separately, then keeps only its requested dates. One year's possible daily records fit below the cap. This does not prove that no trading days are missing.
- Duplicate dates, bars later than a slice's cutoff, invalid values, malformed JSON, and unexpected adjusted data are rejected. Earlier extra rows are excluded; their date/shape is checked but their unused numerical fields are not analyzed.
- If any slice fails, the entire query fails; partial history is never returned as a successful result. There are no automatic retries or source changes.

Live smoke checks passed for `000001.SZ`, `600000.SH`, and `688001.SH` over 2025-09-01 through 2025-09-05, and for `000001.SZ` across 2024-12-30 through 2025-01-03. These checks do not guarantee future availability.

## Text-model layer: offline first

The model-layer demo is separate from the market-data CLI. It does not fetch prices or run the product agent. Start with the offline fake model; no API key or paid request is involved:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m trading_companion.model_cli "Reply with OK."
```

The JSON explicitly reports `provider: "fake"`, `is_mock: true`, a fixed mock answer, and unknown (`null`) token usage. This checks the application interface, not real model quality.

To prepare a future DeepSeek connectivity test, install the optional dependency if needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[model]"
```

`requests` is already included by `.[data]`; no OpenAI SDK or agent framework is required for this small HTTP adapter. Configure `DEEPSEEK_API_KEY` and `DEEPSEEK_MODEL` in your local environment. Use a model identifier available to your account; none is guessed by the application. Do not paste credentials into chat or commit them. `.env` files are not automatically loaded.

Create an API key in the [DeepSeek API platform](https://platform.deepseek.com/). The [official getting-started guide](https://api-docs.deepseek.com/zh-cn/) currently lists `deepseek-flash` (checked on 2026-10-13); use a model supported by your account. Set the values in the same PowerShell terminal that will run Python:

```powershell
$env:PYTHONPATH='src'
$env:DEEPSEEK_MODEL='deepseek-flash'
$deepSeekKeyInput = Read-Host -Prompt 'DeepSeek API key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $deepSeekKeyInput).Password
$deepSeekKeyInput.Dispose()
Remove-Variable deepSeekKeyInput
```

Paste the key only into the masked prompt, not as part of a shell command. This avoids recording the key in command history, but the resulting environment variable is plaintext in process memory, not a secret vault. It is inherited by Python launched from this terminal and is not saved permanently. Do not print it or share an environment dump.

**Only run the next command when you intend to send the test prompt and fixed system instruction to DeepSeek and incur any applicable API charges:**

```powershell
.\.venv\Scripts\python.exe -m trading_companion.model_cli "Reply with OK." --provider deepseek
```

Successful output has `status: "ok"`, `provider: "deepseek"`, `is_mock: false`, and a completed `text` answer. This verifies one text request, not trading analysis or tool execution. After testing, remove the key from the terminal environment with `Remove-Item Env:DEEPSEEK_API_KEY`; closing the terminal also ends this session configuration.

- The endpoint is fixed to `https://api.deepseek.com/chat/completions`. There is no custom URL from a prompt, no redirect, no automatic retry, and no provider fallback.
- The adapter implements only non-streaming text through a Chat Completions-compatible payload. Provider/model compatibility remains subject to a live test; OpenAI API examples do not establish DeepSeek account availability.
- Configuration is validated locally. Defaults are a 30-second connection/read wait and a 512-token output limit. This timeout is not the future agent's total wall-clock deadline.
- The adapter explicitly requests `thinking: {type: "disabled"}` for this small text-only layer. Current DeepSeek models enable thinking by default; thinking-mode budgeting and tool-message handling are separate future work. See the [official thinking-mode guide](https://api-docs.deepseek.com/guides/thinking_mode/).
- Normal completed text is returned with the requested and returned model identifiers. Missing usage stays unknown. Reasoning fields are not exposed as final answers.
- Authentication, rate limits, timeouts, invalid JSON/schema, refusals, unsupported tool calls, and truncated output have explicit outcomes. A truncated answer is not printed as successful output.
- Exit codes: 0 for a complete response, 1 for a model-call failure, 2 for local configuration/input errors. The CLI emits sanitized errors rather than raw provider error bodies.
- Nothing in this demo checks financial claims or makes it suitable for trading recommendations. Evidence checks and agent orchestration are later modules.

See [the short design](docs/model-layer-design.md) and [the Chinese learning notes](docs/agent-learning-notes.md). Compatible response semantics were checked against [OpenAI's Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create); transport retry behavior is controlled by our own HTTP implementation, not the OpenAI SDK.

## Analysis interface

The CLI and a future agent can share the same Python function:

```python
from datetime import date
from pathlib import Path

from trading_companion.analysis import analyze_daily_bars
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import InvalidRequestError

source = CsvDailyBarSource(Path("data/sample_daily_bars_synthetic.csv"))

try:
    result = analyze_daily_bars(
        source,
        instrument_id="000001.SZ",
        as_of_date=date(2026, 9, 23),
        window=3,
    )
except InvalidRequestError as error:
    print(f"Check the request: {error}")
else:
    payload = result.to_dict()
    print(payload["status"])
```

For a real source, import `TencentDailyBarSource` from `trading_companion.tencent_adapter`, or `AkshareDailyBarSource` from `trading_companion.akshare_adapter`. Construct either with `(instrument_id, start_date, end_date)` and pass it to the same analysis function. The configured range determines what is fetched; the analysis function filters those records and does not expand that range.

Result fields include requested dates, the actual latest trade date, source, price-adjustment convention, UTC retrieval time, OHLCV rows, SMA values, warnings, and missing observation count. JSON retains calculation precision and represents unavailable values as `null`.

| Status | Meaning | CLI exit code |
| --- | --- | --- |
| `ok` | The latest selected row has enough observations for SMA | 0 |
| `no_data` | Empty source or no records matching the request; see `reason` | 0 |
| `insufficient_history` | Valid records exist, but the SMA window is not filled | 0 |
| `source_error` | File access, missing optional dependency, or request failure | 1 |
| `invalid_data` | Invalid fields, malformed records, or invalid selected series | 1 |

Invalid requests raise `InvalidRequestError` in Python. The CLI reports them on stderr and exits with code 2, even in JSON mode. Unexpected programming errors are not swallowed. A future agent wrapper must handle both request exceptions and result statuses; exit code 0 alone does not mean an indicator is available.

## Data contract and limitations

CSV files must contain these unique columns:

```text
instrument_id,trade_date,open_price,high_price,low_price,close_price,volume_shares,amount_cny
```

Additional named columns are allowed. Rows must match the header width; malformed quoting and duplicate headers are rejected. Dates use ISO format. Prices must be finite, positive, and inside the daily high/low range; share volume must be a non-negative integer and amount must be finite and non-negative. The selected series must have unique, increasing trade dates.

CSV price adjustment and authenticity are unknown; the loader does not infer them. A header-only CSV is valid but empty. A missing/invalid header is a data error.

**Trade-date filtering is not strict point-in-time data availability.** `retrieved_at` records retrieval time, not original publication time. The pipeline does not yet track historical revisions or prove what was available on a past date. Unadjusted prices can jump around corporate actions, and neither SMA nor EMA alone accounts for those effects.

The requested cutoff and latest actual bar date are separate. An earlier last bar is reported explicitly; the application does not guess whether it reflects a holiday, suspension, stale data, or another cause.

## Tests

Install `.[data]` first: adapter tests use the requests exception classes included with the data dependency. All tests stub the external provider; the suite does not require live market access.

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover data validation, units, historical date filtering, SMA, SMA-seeded EMA, MACD/DEA initialization and causal prefixes, negative/zero values, independent Decimal references, source selection, structured output, insufficient/empty data, malformed CSV and provider responses, failed requests, and request validation.

A successful test run does not guarantee that a public endpoint is reachable. The historical Tencent and AKShare CLI commands above are separate live smoke tests.

## Module map

- `daily_bar.py`: canonical validated records.
- `csv_loader.py` / `data_source.py`: CSV loading and the source interface.
- `akshare_adapter.py`: provider-specific request, mapping, and validation.
- `tencent_adapter.py`: Tencent JSON requests, yearly slices, unit conversion, and validation.
- `history.py` / `indicators.py`: date selection and numerical calculations.
- `errors.py`: request, source, and validation error types.
- `analysis.py`: reusable analysis function and structured result.
- `__main__.py`: command-line input and output.
- `model.py` / `deepseek_model.py`: text model contracts, offline fake, and DeepSeek adapter.
- `model_cli.py`: explicit offline/live model-layer demonstration.
- `tests/`: offline regression tests.

## Next milestone

The model layer is an independent first step toward the product agent, not a finished agent. Complete the multi-indicator calculations and result contract requested in the revised PRD before integrating the intended multi-indicator analysis tool. Then add controlled tool execution, the single-turn agent flow, answer checks, and conversation state. Broader data, screening, point-in-time storage/replay, and the product UI remain later increments.

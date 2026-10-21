# Trading Companion

Trading Companion is a research and decision-support assistant for China's A-share market. All trading decisions and operations remain with the user.

## Documentation

- [Target architecture and implementation roadmap (Chinese)](docs/architecture.md): module responsibilities, data flow, safety boundaries, technology choices, and current-versus-planned capabilities.
- [Product agent PRD (Chinese)](docs/prd-agent-v0.1.md): first-agent scope, indicator conventions, and acceptance criteria.
- [Agent learning notes (Chinese)](docs/agent-learning-notes.md): project concepts and concise interview Q&A.
- [Multi-indicator service contract (Chinese)](docs/multi-indicator-design.md): request limits, field availability, initialization metadata, deterministic relations, and evidence identifiers.
- [Tool-model protocol and offline round trip (Chinese)](docs/tool-model-design.md): tool messages, call IDs, response states, scripted fake behavior, and validation boundaries.
- [Controlled research-tool executor (Chinese)](docs/research-tool-design.md): strict business validation, one execution per turn, trusted source configuration, and compact evidence results.
- [DeepSeek tool adapter (Chinese)](docs/deepseek-tool-model-design.md): provider message conversion, completion states, shared transport, and mocked HTTP verification.
- [Opt-in tool smoke test (Chinese)](docs/tool-smoke-design.md): a fixed synthetic task, offline default, explicit paid-provider selection, and stop conditions.
- [Bounded single-turn Agent (Chinese)](docs/single-turn-agent-design.md): explicit Chinese input, request-bound proposals, partial-result handling, and unchecked explanation drafts.
- [Natural-language research input (Chinese)](docs/natural-input-design.md): model-first interpretation, dated security snapshots, ambiguity gates, visible defaults, and an offline fixture demo.
- [Explicit exchange-directory refresh (Chinese)](docs/security-directory-refresh-design.md): real SSE/SZSE name lists, validation limits, atomic snapshots, and separate live verification.
- [Identity clarification continuation (Chinese)](docs/identity-clarification-design.md): per-session pending conditions, short stock-selection replies, expiry, cancellation, and replay protection.

## Current status

The foundation for the first product agent is implemented:

- Validated daily OHLCV records and local CSV loading.
- A Tencent adapter and an optional AKShare/Eastmoney adapter for historical, unadjusted daily bars.
- Trade-date filtering and deterministic closing-price simple moving averages (SMA).
- A standalone SMA-seeded closing-price EMA calculation with date-aligned result points; not yet integrated into the analysis CLI.
- A standalone MACD calculation with SMA-seeded DEA, aligned DIF/DEA/histogram fields, and date/period result points.
- Standalone Wilder RSI/ATR, population-standard-deviation Bollinger bands, and volume MA/prior-window activity ratios, all with date-aligned result points and explicit zero-baseline handling.
- A reusable analysis function with structured results, provenance, and explicit failure states.
- A separate multi-indicator research service with one source read, aligned field-level availability, initialization metadata, deterministic relations, and query-scoped evidence.
- A CLI with table and JSON output, plus offline tests.
- A separate text-model interface, offline fake model, and DeepSeek HTTP adapter with configuration and response validation.
- A separate tool-message model protocol and scripted offline fake with JSON/sequence validation and a fixed-result round-trip demonstration.
- A controlled daily-indicator tool executor connecting validated proposals to the research service, with one analysis per turn and a compact, traceable model view.
- A separate DeepSeek tool-capable adapter with strict response parsing and shared HTTP transport, tested offline with mocked provider responses and actual synthetic CSV calculations.
- An offline-by-default tool smoke-test CLI with explicit DeepSeek opt-in, a fixed synthetic task, at most two model calls, and no retries or market requests.
- A reusable bounded single-turn runner with conservative explicit-format Chinese input, clarification before any calls, request-bound tool proposals, and separate research facts and unchecked model drafts.
- A separate model-first natural-input runner, validating verbatim mentions against an injected security directory and applying visible date/indicator defaults before reusing the research flow.
- An opt-in SSE/SZSE directory-refresh CLI with bounded requests, required-fragment validation and atomic local snapshots; one live refresh returned 5,224 identities, without changing supported market-analysis prefixes.
- Optional per-conversation identity clarification: preserve validated dates and indicators while waiting for a stock selection, recheck the current directory and source configuration, and consume the pending request before execution.

This is an initial bounded research flow, with both an explicit-format entry and a model-first natural-input entry. The natural entry optionally supports cross-turn stock-identity clarification, not general parameter editing or chat/result memory. These paths are tested offline; real exchange-directory refresh and local lookup are separately verified, not an end-to-end live natural-language evaluation. General language-quality evaluation and a conversational product are not complete. The developer reported a successful live `deepseek-flash` text request on 2026-10-13 and subsequently one live tool round trip using synthetic CSV data (two model calls, one local analysis, 4,500 reported tokens; recorded 2026-10-20). That supports compatibility for the fixed task and model, not real-market research quality. Explanation checking, general conversation memory, Jev, news, fundamentals, screening, DuckDB persistence, API/UI, and historical strategy evaluation are not implemented.

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

Both price EMAs reuse one validated closing-price list. An internal numeric EMA core also handles negative and zero DIF values, without disguising them as positive-price OHLC records. The original `ema_close` interface and SMA seed convention remain. The market CLI still outputs SMA only; the separate multi-indicator service below now adds field states, crossover facts, and initialization metadata. See the [short MACD design](docs/macd-design.md).

## Try RSI, Bollinger bands, ATR, and volume activity

These are pure calculations over the same validated `bars`; the market CLI still outputs SMA only. Using the five-row synthetic sample from above, try deliberately shortened demonstration windows:

```python
from trading_companion.indicators import (
    rsi_close_points, boll_close_points, atr_points, volume_activity_points,
)
print(rsi_close_points(bars, window=3)[-1])
print(boll_close_points(bars, window=3)[-1])
print(atr_points(bars, window=3)[-1])
print(volume_activity_points(bars, window=3)[-1])
```

The scalar/value-list counterparts are `rsi_close`, `boll_close`, `atr`, and `volume_activity`. Defaults are RSI14, BOLL(20,2), ATR14, and a 20-record volume window. Default RSI/ATR first become available at record 15; BOLL and inclusive volume MA at record 20; the prior-20-records volume ratio at record 21. Those default indicator fields remain unavailable on the five-row sample.

RSI/ATR use Wilder smoothing with an arithmetic-mean seed; BOLL uses population standard deviation (`ddof=0`). RSI is fixed at 50 for flat prices, 100 for gains without losses, and 0 for losses without gains. Flat prices can produce ATR=0 and coincident BOLL bands.

Volume fields are `volume_shares`, `volume_ma` (including today), `previous_volume_ma` (excluding today), `relative_volume`, and `relative_volume_status`. The ratio status distinguishes `insufficient_history` from `undefined` when the prior mean is zero. Zero current volume with a positive baseline gives an available ratio of 0. This is a historical volume multiple, not an intraday volume-ratio measure.

All functions retain input positions and full calculation precision, validate the complete ordered single-stock sequence, and do not fetch warm-up data. Pure functions accept positive integer periods; product request limits will be enforced separately. See the [calculation design and offline test commands (Chinese)](docs/indicators-design.md).

## Run the multi-indicator service offline

The new service is independent of the existing single-SMA CLI and `analyze_daily_bars`. It validates product request limits before reading, then calculates all requested indicators from one selected history. Run this in a source-tree Python session with `PYTHONPATH` configured as above:

```python
from datetime import date
from pathlib import Path
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.multi_analysis import (
    IndicatorSpec, MultiIndicatorRequest, analyze_indicators,
)

source = CsvDailyBarSource(Path("data/sample_daily_bars_synthetic.csv"), is_synthetic=True)
request = MultiIndicatorRequest(
    instrument_id="000001.SZ",
    start_date=date(2026, 9, 21),
    as_of_date=date(2026, 9, 25),
    source="csv",
    indicators=(
        IndicatorSpec("sma", window=3),
        IndicatorSpec("ema", window=3),
        IndicatorSpec("macd", fast=2, slow=3, signal=2),
        IndicatorSpec("rsi", window=3),
        IndicatorSpec("boll", window=3),
        IndicatorSpec("atr", window=3),
        IndicatorSpec("volume", window=3),
    ),
)
result = analyze_indicators(source, request, allow_development_csv=True)
payload = result.to_dict()
print(payload["data_status"], payload["calculation_status"])
for indicator in result.indicators:
    print(indicator.indicator_id,
          {name: value.status for name, value in indicator.latest.items()})
```

This explicitly enables development CSV mode and uses artificial data with short demonstration periods. The result labels it as development/sample data and includes unknown authenticity/adjustment warnings; it is not real-market analysis. A production model tool must not expose this switch or a file path. No external market/model request is made by this example.

Omitting `indicators` uses eight instances: SMA20/60, EMA20, MACD(12,26,9), RSI14, BOLL(20,2), ATR14, and volume20. Explicit requests calculate only their selected instances and internal dependencies. Canonical duplicate instances are removed; more than 12 distinct instances or more than 366 inclusive calendar days are rejected.

Data status and calculation status are separate. Each numeric field and derived relation has an availability status and record requirements; the latest values always correspond to the actual latest selected date. Thirty records retain available indicators and MACD DIF while reporting SMA60 short by 30 records and MACD DEA/histogram short by 4. A ratio with a zero baseline is `undefined`, rather than a fabricated zero.

Results include seed/start information, per-component recursive-update counts, and project-defined initialization warnings. Evidence identifiers preserve the full indicator parameters; available relations reference the exact current and, where needed, previous-record facts. This supplies a basis for future answer checks; it does not yet validate model prose or call a model. See the [design and validation boundaries](docs/multi-indicator-design.md).

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
- The adapter explicitly requests `thinking: {type: "disabled"}` for this small text-only layer. Current DeepSeek models enable thinking by default; thinking-mode budgeting and reasoning-history handling remain future work. The separate tool adapter below also disables thinking. See the [official thinking-mode guide](https://api-docs.deepseek.com/guides/thinking_mode/).
- Normal completed text is returned with the requested and returned model identifiers. Missing usage stays unknown. Reasoning fields are not exposed as final answers.
- Authentication, rate limits, timeouts, invalid JSON/schema, refusals, unsupported tool calls, and truncated output have explicit outcomes. A truncated answer is not printed as successful output.
- Exit codes: 0 for a complete response, 1 for a model-call failure, 2 for local configuration/input errors. The CLI emits sanitized errors rather than raw provider error bodies.
- Nothing in this demo checks financial claims or makes it suitable for trading recommendations. Evidence checks and agent orchestration are later modules.

See [the short design](docs/model-layer-design.md) and [the Chinese learning notes](docs/agent-learning-notes.md). Compatible response semantics were checked against [OpenAI's Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create); transport retry behavior is controlled by our own HTTP implementation, not the OpenAI SDK.

## Tool-model protocol: offline round trip

The tool-message protocol is independent of the old text-only API. It represents tool definitions, assistant call proposals, caller-supplied results, and complete/refused/incomplete response states. Try the offline demonstration:

```powershell
$env:PYTHONPATH='src'
python -m trading_companion.tool_model_demo
```

The script uses a `FakeToolModel`: it proposes a declared tool, the caller explicitly supplies a fixed artificial result with the matching call ID, and the fake returns a scripted final answer. It does not execute the tool, fetch prices, read credentials, or request a real model. This verifies protocol handling; it is not yet the research Agent.

The public types are `ToolDefinition`, `ToolCall`, `ToolMessage`, `ToolModelResponse`, and `ToolCapableModel`, alongside `FakeToolModel`. Parameters/results are validated JSON objects. Message history must match every tool result to an outstanding call, and all results in a batch must arrive before proceeding. Unknown tool names, duplicate/orphan/missing results, invalid JSON, and size-limit violations produce explicit sanitized errors. Contract storage isolates nested mutable values; explicit `to_dict()` returns protocol content and must not be treated as a sanitized production log.

Tool parameter schemas describe structure; this is not a full JSON Schema validator or a business authorization layer. The separate research executor below validates stock/date/indicator parameters, permissions, and its one-analysis budget before invoking the existing research service. `DeepSeekTextModel` still accepts text only; the separate `DeepSeekToolModel` adapts the tool protocol, with one developer-reported live synthetic task now verified. See the [protocol design](docs/tool-model-design.md).

## DeepSeek tool adapter: offline verification

`DeepSeekToolModel.complete(messages, tools=...)` converts internal declarations, assistant proposals and tool results to the provider's function-tool payload, and parses complete/tool-call/refused/incomplete responses. It does not execute tools or run an Agent loop. Truncated or refused responses never forward actionable calls. Text and tool adapters share fixed-endpoint HTTP transport, timeouts and sanitized errors; the old text CLI remains unchanged.

Both adapters explicitly disable thinking. The current tool protocol does not store the reasoning history required by thinking-mode tool calls; enabling that mode needs a separate design. Tool adapter coverage includes 24 offline tests; including both research entries, directory refresh and identity clarification, all 499 regression tests pass as of 2026-10-20:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_deepseek_tool_model.py -v
```

All HTTP in these tests is mocked, including the round trip that actually computes indicators from trusted synthetic CSV. No real credentials, paid model request or market request is used. `is_mock=false` identifies the provider adapter path, not evidence that HTTP was live. See the [adapter design and verification limits](docs/deepseek-tool-model-design.md).

## Tool smoke test: offline default, explicit live opt-in

Start with the offline CLI; it does not read model credentials:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.tool_smoke
```

The fixed task requests SMA3, SMA20 and volume3 for `000001.SZ` on the trusted synthetic CSV. The program accepts exactly that task (indicator order may vary), actually calculates it once, returns the result under the original call ID, and requires a completed final response. It never repairs a proposal, uses live market data, resets execution allowance, or automatically retries. There are at most two model calls and one local analysis, not an unbounded Agent loop.

To test real provider compatibility, first configure the model and credentials in the same terminal using the text-model instructions above. **Only run this command when you intend to send the fixed instructions, tool definition and synthetic results to DeepSeek and incur up to two API requests:**

```powershell
.\.venv\Scripts\python.exe -B -m trading_companion.tool_smoke --provider deepseek
```

`status=ok` and `tool_round_trip_completed=true` mean the fixed protocol round trip completed. Inspect `provider`, `live_provider_selected`, per-response model metadata, `model_calls_attempted` and `analyses_used` as well. A successful default run is still offline Fake; `answer_validation=not_performed` explicitly means the model's claims have not been programmatically checked. The developer supplied one successful live `deepseek-flash` round trip with synthetic data; its manually reviewed explanation is not automated answer validation or a real-market test.

Exit codes are 0 for a completed round trip, 1 for nonverification/runtime failure, and 2 for configuration/argument errors. No proposal, mismatched arguments, unusable data, refusals, incomplete output or another proposal on the second call stops the test. See the [smoke-test design](docs/tool-smoke-design.md) for output fields, costs and limitations.

## Natural-language research: offline fixture demo

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.natural_agent_demo
```

The demo includes `看看平安银行最近的走势，关注3日均线和量能` and the ambiguous `看看平安最近的走势`. A scripted model extracts verbatim phrases; a four-record fixture resolves the first and returns two candidates for the second. The program computes missing date defaults and standard indicator parameters, retaining the exact applied conditions. No credentials, provider, directory or market network requests are used; this is not evidence of real model language quality.

`run_natural_research_turn()` permits at most three model calls and one analysis for an initial request; the explicit-format entry below remains limited to two. Directory snapshots carry source/time/fixture metadata, reject stale or future data, and cannot use fixtures to authorize online research. The demo remains intentionally synthetic, using a fixed reference date of 2026-09-26. A separate real-directory refresher is available below. See the [natural-input design](docs/natural-input-design.md) for supported phrases, default policies and remaining limits.

## Continue stock-identity clarification: offline demo

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.session_agent_demo
```

The first turn asks about the ambiguous `平安`, keeping the validated interval 2026-08-27 through 2026-09-25 and SMA3/RSI2. The reply `我指的是平安银行` selects its identity and reuses those conditions. The first turn uses one scripted model call and no analysis; selection uses two scripted calls and one real calculation on synthetic CSV data. Repeating the selection returns `no_pending_clarification` with no model or market calls. All dates, directory records and model replies in this demo are fixtures; this is not live research or a general interactive CLI.

To enable continuation, trusted application code must create a separate `ResearchSession()` per conversation and pass the same object as `session=` on each turn. Without it, the natural entry remains stateless. Pending conditions expire after 15 minutes; cancellation, explicit new questions, current-directory checks, source changes and same-session concurrent access are handled without implicitly executing a stale selection. Extra edits such as changing an indicator require a complete `新问题：...`; general parameter patching is not supported. See the [identity clarification design](docs/identity-clarification-design.md) for commands, failure behavior and limits.

## Refresh the real security-name directory

Install `.[data]`, then explicitly opt in to public exchange requests:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.security_directory_refresh --live --timeout 15
```

Without `--live`, this command makes no requests or writes. A successful refresh saves `data/security_directory_live.json` (Git-ignored); all three required fragments must validate before replacement. Failures preserve an existing snapshot and its timestamp, without automatically authorizing stale data.

Load it with `load_security_directory("data/security_directory_live.json")` and inject it as `directory=` into `run_natural_research_turn()`. Normal name lookup does not download lists. The offline demo still uses its four-record fixture.

One live refresh returned 1,700 SSE main-board entries, 618 STAR-list entries including CDRs, and 2,906 SZSE entries: 5,224 total. SSE declared totals are checked; SZSE export completeness cannot be independently proven. BSE and historical risk/name status are not covered. Some listed identities, including `689009.SH` and `302132.SZ`, remain unsupported by the current Tencent analysis contract; identity resolution does not broaden market-data authority. See the [refresh design](docs/security-directory-refresh-design.md).

## Bounded single-turn Agent: offline demo

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -B -m trading_companion.agent_demo
```

This default demo uses a scripted model and actual synthetic CSV calculations, with no credentials or network. It calls the reusable `run_research_turn()` rather than the fixed smoke-test runner. The current input grammar requires explicit stock, dates and indicator parameters, for example `分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3、SMA20、VOLUME3`. Ambiguous input returns clarification before calling any model or source; this is deliberately not arbitrary natural-language understanding.

The runner binds the model proposal to the parsed request, permits at most two model calls and one analysis, and never changes dates or sources to fill missing values. The demo returns `limited / partial_research`: SMA3 and volume3 are usable, while SMA20 lacks 15 records. Trusted research is retained if explanation fails. `model_draft` remains unchecked and `answer_validation=not_performed`, even when a fully available request returns `completed`. See the [single-turn design](docs/single-turn-agent-design.md) for state meanings and limits.

## Controlled research-tool execution

```powershell
$env:PYTHONPATH='src'
python -m trading_companion.research_tool_demo
```

This demonstration uses a scripted `FakeToolModel` and a trusted, fixed synthetic CSV. Unlike the fixed-result protocol demo, it actually runs the existing multi-indicator calculations and returns their evidence under the proposed call ID. It uses no network or credentials; the final model text is still scripted.

`ResearchToolExecutor` allows only `analyze_daily_indicators`. It validates explicit stock/date arguments and bounded indicator specifications before constructing a source. The model cannot select CSV files, paths, URLs, or request IDs. Online sources remain explicit Tencent/Eastmoney choices; development CSV is injected by trusted application code and stays labeled synthetic or unverified. Create one executor per user turn: its single analysis allowance is consumed before source construction, including attempts that fail. Multi-call batches are rejected before any analysis.

The full research result stays local. A versioned compact view carries latest fields, initialization metadata, relations and request-scoped evidence aliases, preserving unavailable states and calculation precision. Oversized results produce an explicit failure rather than truncated evidence. This local executor budget does not implement the future Agent's complete model/token/time budgets. See the [executor design](docs/research-tool-design.md).

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

Tests cover data validation, units, historical date filtering, SMA, SMA-seeded EMA, MACD/DEA, Wilder RSI/ATR, BOLL population variance, volume window alignment and zero baselines, initialization and causal prefixes, negative/zero values, independent Decimal references, source selection, structured output, insufficient/empty data, malformed CSV and provider responses, failed requests, and request validation. Multi-indicator tests additionally check one source read, partial availability, request limits, component metadata, full-precision relations, evidence dependencies, isolated calculation failures, and development-only CSV access.

Tool and Agent tests cover protocol pairing and limits, strict research arguments, trusted sources, atomic one-analysis execution, batch rejection, compact states and evidence, capacity fallback, adapter conversion, bounded smoke testing, input clarification, request binding, partial results and explanation failures. The complete offline suite currently has 499 tests, including 33 original runner tests, 38 natural-input/directory tests, 46 exchange-refresh/save tests and 44 identity-session tests. Directory cases check required fragments, declared pagination, real-export dimension errors, atomic replacement, unchanged old timestamps and identities outside the market-analysis scope. Session cases cover condition preservation, current-directory/source changes, expiry, cancellation, isolation, repeated replies and concurrent access. Details are in the [executor design](docs/research-tool-design.md), [adapter design](docs/deepseek-tool-model-design.md), [single-turn design](docs/single-turn-agent-design.md), [natural-input design](docs/natural-input-design.md), [refresh design](docs/security-directory-refresh-design.md) and [clarification design](docs/identity-clarification-design.md).

A successful test run does not guarantee that a public endpoint is reachable. The historical Tencent and AKShare CLI commands above are separate live smoke tests.

## Module map

- `daily_bar.py`: canonical validated records.
- `csv_loader.py` / `data_source.py`: CSV loading and the source interface.
- `akshare_adapter.py`: provider-specific request, mapping, and validation.
- `tencent_adapter.py`: Tencent JSON requests, yearly slices, unit conversion, and validation.
- `history.py` / `indicators.py`: date selection and numerical calculations.
- `errors.py`: request, source, and validation error types.
- `analysis.py`: reusable analysis function and structured result.
- `multi_contracts.py` / `multi_analysis.py`: validated multi-indicator requests, field states, initialization metadata, deterministic relations, and evidence.
- `__main__.py`: command-line input and output.
- `model.py` / `deepseek_model.py`: text model contracts, offline fake, and DeepSeek adapter.
- `model_cli.py`: explicit offline/live model-layer demonstration.
- `tool_model.py` / `tool_model_demo.py`: tool-message contracts, scripted fake, and an offline fixed-result round trip.
- `deepseek_tool_model.py`: DeepSeek function-tool message conversion and bounded response parsing; no tool execution.
- `tool_smoke.py`: fixed-task tool connectivity CLI; offline default, explicit DeepSeek opt-in, and bounded synthetic execution.
- `research_tool.py` / `research_tool_demo.py`: controlled analysis execution, compact evidence results, and an offline synthetic calculation round trip.
- `agent_input.py` / `agent.py` / `agent_demo.py`: conservative explicit-input parsing, shared bounded orchestration, optional identity-session routing, and an offline demo; no general chat/result memory or answer checker yet.
- `research_intent.py` / `security_directory.py` / `natural_agent_demo.py`: model-first intent candidates, grounded snapshot-based identity resolution, visible defaults and an offline fixture demo.
- `research_session.py` / `session_agent_demo.py`: per-conversation pending identity selection, revalidation, expiry/cancellation, and an offline two-turn demo; no persisted chat history.
- `exchange_directory.py` / `security_directory_refresh.py`: explicit SSE/SZSE reads and atomic snapshot refresh, separate from conversations and market data.
- `tests/`: offline regression tests.

## Next milestone

M2 now includes the protocol, executor, DeepSeek adapter, smoke-test CLI, bounded explicit/natural-input runners and a separately verified exchange-directory refresher. Limited stock-identity clarification is also implemented as an optional in-process session. This is not a completed full-market product. General parameter editing/result follow-ups, answer checking/safe presentation, real model evaluation and product entry remain separate increments. Broader conversation state and complete round-level budgets remain M3 work. Broader data, screening, point-in-time storage/replay, and the product UI remain later increments.

/** Numeric sample results are optional presentation; they never authorize new requests. */
import { formatNumber } from "./presentation";

const FIELDS = {
  price: ["最新价", "CNY/share", "元/股"], change_percent: ["涨跌幅", "percent", "%"],
  amount_cny: ["成交额", "CNY", "元"], turnover_percent: ["换手率", "percent", "%"],
  pe_dynamic: ["动态市盈率", "ratio", "倍"], pb: ["市净率", "ratio", "倍"],
  market_cap_cny: ["总市值", "CNY", "元"], float_market_cap_cny: ["流通市值", "CNY", "元"],
} as const;
const STRATA: Record<string, string> = { sse_main: "沪市主板", szse_main: "深市主板", star: "科创板", chinext: "创业板", bse: "北交所" };
const OPERATORS = { lt: "<", lte: "≤", eq: "=", gte: "≥", gt: ">" } as const;
type Field = keyof typeof FIELDS;
type Operator = keyof typeof OPERATORS;
type Condition = { field: Field; operator: Operator; value: number };
type Row = { instrument_id: string; name: string; quoted_at: string; quote_date: string; stratum: string;
  values: Record<Field, number | null>; sample_position: number; sort_value: number };
type Stratum = { stratum: string; total_basis: string; vendor_total: number | null; directory_records: number | null;
  scanned_records: number; identity_verified_records: number; unverified_records: number; missing_quote_records: number };
type ScreeningView = { status: "completed" | "empty" | "unavailable"; rows: Row[];
  request: { conditions: Condition[]; sort_by: Field; sort_direction: "asc" | "desc"; limit: number };
  conditions: (Condition & { passed: number; failed: number; unknown: number })[];
  coverage: { scanned_records: number; identity_verified_records: number; condition_matched_records: number;
    age_eligible_records: number; stale_records_not_evaluated: number; missing_quote_records: number;
    unverified_records: number; matched_missing_sort_records: number; strata: Stratum[] };
  source: { source: string; retrieved_at: string; data_authenticity: string } };
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max = 160): value is string => typeof value === "string" && value.trim().length > 0
  && value.length <= max && !/[\u0000-\u001f\u007f-\u009f\u200b]/.test(value);
const number = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value)
  && (!Number.isInteger(value) || Number.isSafeInteger(value));
const count = (value: unknown, max = 10000): value is number => number(value) && Number.isInteger(value) && value >= 0 && value <= max;
const field = (value: unknown): value is Field => typeof value === "string" && Object.hasOwn(FIELDS, value);
const operator = (value: unknown): value is Operator => typeof value === "string" && Object.hasOwn(OPERATORS, value);
const date = (value: unknown): value is string => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().startsWith(value);
const timestamp = (value: unknown): value is string => typeof value === "string" && value.length <= 40 && date(value.slice(0, 10))
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && Number.isFinite(Date.parse(value));
const condition = (value: unknown): value is Condition & Record<string, unknown> => object(value) && field(value.field) && operator(value.operator) && number(value.value);
const conditionKey = (item: Condition) => `${item.field}|${item.operator}|${item.value}`;
const stratumFor = (id: string) => id.endsWith(".BJ") ? "bse" : id.endsWith(".SH") ? /^(688|689)/.test(id) ? "star" : "sse_main"
  : id.startsWith("3") ? "chinext" : "szse_main";
const shanghaiDate = (value: string) => new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
}).format(new Date(value));

function validStratum(value: unknown): value is Stratum {
  if (!object(value) || typeof value.stratum !== "string" || !Object.hasOwn(STRATA, value.stratum)
    || !count(value.scanned_records, 1000) || !count(value.identity_verified_records, 1000)
    || !count(value.missing_quote_records, 1000) || !count(value.unverified_records, 1000)
    || value.identity_verified_records + value.unverified_records + value.missing_quote_records !== value.scanned_records
    || !count(value.pages_read, 2)) return false;
  if (value.total_basis === "vendor_declared") return count(value.vendor_total) && value.directory_records === null
    && value.vendor_total >= value.scanned_records && value.vendor_listing_exhausted === (value.vendor_total === value.scanned_records)
    && value.directory_stratum_exhausted === null;
  return value.total_basis === "configured_directory" && count(value.directory_records) && value.vendor_total === null
    && value.directory_records >= value.scanned_records && value.directory_stratum_exhausted === (value.directory_records === value.scanned_records)
    && value.vendor_listing_exhausted === null;
}

export function parseScreeningResult(value: unknown): ScreeningView | null {
  if (!object(value) || value.version !== "quote-screening-result-v1" || value.trading_enabled !== false
    || value.scope !== "numeric_conditions_within_actual_identity_verified_sample_not_full_market"
    || !["completed", "empty", "unavailable"].some(status => value.status === status)
    || !object(value.request) || !object(value.coverage) || !object(value.source) || !object(value.fields)
    || !Array.isArray(value.rows) || value.rows.length > 50 || !Array.isArray(value.conditions)) return null;
  const request = value.request, coverage = value.coverage, source = value.source;
  if (!Array.isArray(request.conditions) || request.conditions.length < 1 || request.conditions.length > 12
    || !request.conditions.every(condition) || new Set(request.conditions.map(conditionKey)).size !== request.conditions.length
    || !field(request.sort_by) || request.sort_direction !== "asc" && request.sort_direction !== "desc"
    || !count(request.limit, 50) || request.limit < 1 || !count(request.max_quote_age_days, 14)
    || value.rows.length > request.limit || value.conditions.length !== request.conditions.length
    || !text(source.source) || !timestamp(source.retrieved_at)
    || source.data_authenticity !== "synthetic" && source.data_authenticity !== "vendor_snapshot"
    || source.independent_full_market_completeness_proven !== false || !object(source.directory)
    || source.directory.is_fixture !== (source.data_authenticity === "synthetic")
    || coverage.independent_full_market_completeness_proven !== false || coverage.scope !== "actual_bounded_sample"
    || !count(coverage.scanned_records, 1000) || !count(coverage.identity_verified_records, 1000)
    || !count(coverage.unverified_records, 1000) || !count(coverage.missing_quote_records, 1000)
    || coverage.identity_verified_records + coverage.unverified_records + coverage.missing_quote_records !== coverage.scanned_records
    || !count(coverage.age_eligible_records, 1000) || !count(coverage.stale_records_not_evaluated, 1000)
    || coverage.age_eligible_records + coverage.stale_records_not_evaluated !== coverage.identity_verified_records
    || coverage.condition_evaluated_records !== coverage.age_eligible_records
    || !count(coverage.condition_matched_records, 1000) || coverage.condition_matched_records > coverage.age_eligible_records
    || !count(coverage.matched_missing_sort_records, 1000) || !count(coverage.sortable_matched_records, 1000)
    || coverage.matched_missing_sort_records + coverage.sortable_matched_records !== coverage.condition_matched_records
    || coverage.returned_records !== value.rows.length || value.rows.length > coverage.sortable_matched_records
    || coverage.result_truncated !== (value.rows.length < coverage.sortable_matched_records)
    || !Array.isArray(coverage.strata) || coverage.strata.length < 1 || coverage.strata.length > 5
    || !coverage.strata.every(validStratum) || new Set(coverage.strata.map(item => item.stratum)).size !== coverage.strata.length) return null;
  for (const key of Object.keys(FIELDS) as Field[]) {
    const definition = value.fields[key];
    if (!object(definition) || definition.label !== FIELDS[key][0] || definition.unit !== FIELDS[key][1]) return null;
  }
  for (const key of ["scanned_records", "identity_verified_records", "unverified_records", "missing_quote_records"] as const) {
    if (coverage.strata.reduce((sum, item) => sum + item[key], 0) !== coverage[key]) return null;
  }
  const expectedStatus = value.rows.length ? "completed" : coverage.age_eligible_records ? "empty" : "unavailable";
  if (value.status !== expectedStatus) return null;
  const summaries = value.conditions;
  for (let index = 0; index < summaries.length; index++) {
    const item = summaries[index], expected = request.conditions[index];
    if (!condition(item) || !object(item) || conditionKey(item) !== conditionKey(expected)
      || item.label !== FIELDS[item.field][0] || item.unit !== FIELDS[item.field][1]
      || !count(item.passed, 1000) || !count(item.failed, 1000) || !count(item.unknown, 1000)
      || item.passed + item.failed + item.unknown !== coverage.age_eligible_records) return null;
  }
  const seen = new Set<string>();
  for (let index = 0; index < value.rows.length; index++) {
    const row = value.rows[index];
    if (!object(row) || !text(row.instrument_id, 9) || !/^\d{6}\.(?:SH|SZ|BJ)$/.test(row.instrument_id)
      || seen.has(row.instrument_id) || !text(row.name, 80) || !timestamp(row.quoted_at) || !date(row.quote_date)
      || shanghaiDate(row.quoted_at) !== row.quote_date || Date.parse(row.quoted_at) > Date.parse(source.retrieved_at)
      || row.stratum !== stratumFor(row.instrument_id) || !coverage.strata.some(item => item.stratum === row.stratum)
      || row.risk_status_verified !== false || row.listing_date !== null
      || !object(row.values) || row.sample_position !== index + 1 || !number(row.sort_value)
      || !Array.isArray(row.condition_checks) || row.condition_checks.length !== request.conditions.length) return null;
    seen.add(row.instrument_id);
    for (const key of Object.keys(FIELDS) as Field[]) {
      const actual = row.values[key];
      if (actual !== null && !number(actual) || actual !== null
        && ["price", "amount_cny", "turnover_percent", "market_cap_cny", "float_market_cap_cny"].includes(key)
        && ((actual as number) < 0 || key === "price" && actual === 0)) return null;
    }
    if (row.values[request.sort_by] !== row.sort_value) return null;
    for (let position = 0; position < request.conditions.length; position++) {
      const check = row.condition_checks[position], expected = request.conditions[position], actual = row.values[expected.field];
      if (!condition(check) || !object(check) || conditionKey(check) !== conditionKey(expected)
        || check.label !== FIELDS[check.field][0] || check.unit !== FIELDS[check.field][1]
        || check.status !== "passed" || check.actual_value !== actual || !number(actual) || check.quoted_at !== row.quoted_at) return null;
      const passed = expected.operator === "lt" ? actual < expected.value : expected.operator === "lte" ? actual <= expected.value
        : expected.operator === "gt" ? actual > expected.value : expected.operator === "gte" ? actual >= expected.value : actual === expected.value;
      if (!passed) return null;
    }
    if (index > 0) {
      const previous = value.rows[index - 1] as Row;
      if (request.sort_direction === "desc" ? previous.sort_value < row.sort_value : previous.sort_value > row.sort_value) return null;
      if (previous.sort_value === row.sort_value && previous.instrument_id > row.instrument_id) return null;
    }
  }
  return value as unknown as ScreeningView;
}

const sourceNames: Record<string, string> = { eastmoney_stratified_quote_list_v1: "东方财富报价",
  tencent_directory_stratified_batch_quotes_v1: "腾讯报价", synthetic_quote_fixture_not_live_market: "人工样例" };

export function ScreeningResults({ data, activityDefault = false }: { data: ScreeningView; activityDefault?: boolean }) {
  const columns = [...new Set<Field>(["price", "change_percent", ...data.request.conditions.map(item => item.field), data.request.sort_by, "pe_dynamic", "pb"])];
  return <div className="conversation-response readable-text">
    {activityDefault && <p>未指定数值条件，本轮按成交额列出活跃研究候选，不代表潜力评分。</p>}
    <p>本次样本中有 {data.coverage.condition_matched_records} 只符合条件，展示 {data.rows.length} 只候选。
      按{FIELDS[data.request.sort_by][0]}{data.request.sort_direction === "desc" ? "从高到低" : "从低到高"}排列。</p>
    {data.source.data_authenticity === "synthetic" && <p className="data-note">人工样例 · 非真实行情</p>}
    {data.rows.length > 0 ? <div className="facts table-scroll"><table aria-label="筛选候选">
      <thead><tr><th scope="col">序号</th><th>候选 / 报价日</th>{columns.map(key => <th key={key}>{FIELDS[key][0]}<small>（{FIELDS[key][2]}）</small></th>)}</tr></thead>
      <tbody>{data.rows.map(row => <tr key={row.instrument_id}>
        <td className="number">{row.sample_position}</td>
        <td><strong>{row.name}</strong><small>{row.instrument_id} · {STRATA[row.stratum]}</small><small>{row.quote_date}</small></td>
        {columns.map(key => <td className="number" key={key}>{row.values[key] === null ? "未知" : formatNumber(row.values[key])}</td>)}
      </tr>)}</tbody>
    </table></div> : <p>{data.status === "unavailable" ? "本次样本没有满足时效要求的已核对报价。" : "本次覆盖样本没有可展示的匹配项。"}</p>}
    <small className="response-origin">{sourceNames[data.source.source] ?? "已配置报价源"} · 获取于 {data.source.retrieved_at.replace("T", " ")} · 有界样本筛选</small>
    <details className="disclosure facts" aria-label="筛选条件与覆盖">
      <summary>筛选条件与覆盖 <span>已核对 {data.coverage.identity_verified_records} 只</span></summary>
      <div className="table-scroll"><table aria-label="筛选条件检查"><thead><tr><th>条件</th><th>通过</th><th>未通过</th><th>未知</th></tr></thead>
        <tbody>{data.conditions.map((item, index) => <tr key={index}>
          <td>{FIELDS[item.field][0]} {OPERATORS[item.operator]} {formatNumber(item.value)} {FIELDS[item.field][2]}</td>
          <td>{item.passed}</td><td>{item.failed}</td><td>{item.unknown}</td>
        </tr>)}</tbody></table></div>
      <p className="workflow-note">扫描 {data.coverage.scanned_records} 条；未核对 {data.coverage.unverified_records} 条；缺失报价 {data.coverage.missing_quote_records} 只；
        报价超期 {data.coverage.stale_records_not_evaluated} 只；符合条件但缺排序值 {data.coverage.matched_missing_sort_records} 只。</p>
      <div className="table-scroll"><table aria-label="采样覆盖"><thead><tr><th>范围</th><th>参考名单</th><th>扫描</th><th>身份已核对</th></tr></thead>
        <tbody>{data.coverage.strata.map(item => <tr key={item.stratum}><td>{STRATA[item.stratum]}</td>
          <td>{item.total_basis === "configured_directory" ? `目录 ${item.directory_records}` : `供应商声明 ${item.vendor_total}`}</td>
          <td>{item.scanned_records}</td><td>{item.identity_verified_records}</td></tr>)}</tbody></table></div>
      <p className="workflow-note">未采样和缺失数据不计为条件失败。ST、科创板及新股未按类别排除；报价未核实风险与上市状态。结果仅描述实际样本。</p>
    </details>
  </div>;
}

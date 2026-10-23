import { marketContextNotice, parseMarketContext, type ContextSource, type MarketContextParameters } from "./marketContextData";
import "./marketContext.css";

const sourceLabels: Record<ContextSource, string> = { synthetic_fixture: "人工样例", sse: "上交所（快照标注）", szse: "深交所（快照标注）", bse: "北交所（快照标注）", curated_local: "本地整理" };
const boardLabels = { sse_main: "沪市主板", star: "科创板", szse_main: "深市主板", chinext: "创业板", bse: "北交所", unknown: "未知" };
const stLabels = { not_st: "非 ST（快照报告）", st: "ST", star_st: "*ST", unknown: "未知，不能视为非 ST" };
const suspensionLabels = { full_day_suspended: "报告全天停牌", not_full_day_suspended: "未报告全天停牌，不保证可交易", unknown: "未知，不能视为正常交易" };
const reasonLabels = {
  reported_state_at_analysis_date: "快照报告的分析日状态，未独立核实。",
  partial_state_unknown: "状态仅部分已知；未知字段仍保留为未知。",
  state_not_covered: "快照没有覆盖分析日的证券状态记录。",
  publication_time_unknown: "证券状态发布时间未知；未展示未确认的状态值。",
  published_after_analysis_cutoff: "证券状态晚于分析日截止才发布；未展示未确认的状态值。",
};

function Provenance({ source, reference, publishedAt }: { source: ContextSource | null; reference: string | null; publishedAt: string | null }) {
  return <p className="market-context-source">来源：{source === null ? "未提供" : sourceLabels[source]}；来源编号：{reference ?? "未提供"}；发布时间：{publishedAt ?? "未知"}。</p>;
}
function DateList({ title, dates }: { title: string; dates: string[] }) {
  return <details className="market-context-dates"><summary>{title}：{dates.length} 日 · 展开日期</summary>
    {dates.length ? <ul className="market-context-date-list">{dates.map((date) => <li key={date}>{date}</li>)}</ul> : <p>无对应日期。</p>}
  </details>;
}

/** Auxiliary context is read-only and never participates in request acknowledgement. */
export function MarketContext({ value, parameters, reportRequestId, historical = false }: {
  value: unknown; parameters: MarketContextParameters | null; reportRequestId: string | undefined; historical?: boolean;
}) {
  if (value === undefined || value === null) return null;
  const data = parseMarketContext(value, parameters, reportRequestId);
  if (!data) return <p className="market-context-unavailable" role="note">市场上下文暂不可用或未通过范围校验；不影响研究答案、指标证据或行情展示。未知状态没有被当作正常状态。</p>;
  const security = data.security, calendar = data.calendar;
  const listingAge = security.listing_date !== null && security.listing_date > data.as_of_date ? "尚未到所报上市日"
    : security.listing_age_calendar_days === null ? "未知"
      : `${new Intl.NumberFormat("zh-CN").format(security.listing_age_calendar_days)} 个自然日${security.listing_age_calendar_days === 0 ? "（上市当日）" : ""}`;
  return <section className="market-context" aria-label="证券状态与交易日历">
    <div className="market-context-header"><h4>证券状态与交易日历</h4>
      <span className="market-context-badge">{data.status === "available" ? "业务日期上下文已覆盖" : "部分未知或含来源冲突"}</span>
      {historical && <span className="market-context-badge">已保存上下文 · 非新查询</span>}
    </div>
    <div className="market-context-body">
      <div className="market-context-provenance">
        <strong>{data.data_authenticity === "synthetic" ? "人工样例 · 非真实市场事实" : "本地快照 · 内容与来源未独立核实"}</strong>
        <p>{historical ? "展示上次保存的上下文，没有重新查询或刷新证券状态。" : "使用已加载的本地快照，不是实时证券状态查询。"}</p>
        <p>股票：{data.instrument_id}；业务分析日：{data.as_of_date}；查询范围：{data.start_date} — {data.as_of_date}。</p>
        <p>快照编号：{data.snapshot_id}；采集时间：{data.retrieved_at}。</p>
        <p>这不是 point-in-time 数据：尚未证明信息在历史分析时点可获得；业务生效日期与采集时间不能替代历史可用性证明。</p>
      </div>
      <section className="market-context-section" aria-label="分析日证券状态"><h5>分析日证券状态</h5>
        <p className={security.status === "available" ? "" : "market-context-unknown"}>{reasonLabels[security.reason]}</p>
        <dl className="market-context-facts">
          <div><dt>上市板块</dt><dd>{boardLabels[security.board]}</dd></div>
          <div><dt>ST 状态</dt><dd>{stLabels[security.st_status]}</dd></div>
          <div><dt>全天停牌状态</dt><dd>{suspensionLabels[security.suspension_status]}</dd></div>
          <div><dt>所报上市日期</dt><dd>{security.listing_date ?? "未知"}</dd></div>
          <div><dt>上市自然日数</dt><dd>{listingAge}</dd></div>
        </dl>
        <p className="market-context-source">业务有效期：{security.effective_from === null ? "未覆盖" : `${security.effective_from} — ${security.effective_to}`}。上市自然日数不是交易日数，也不代表适用任何新股交易规则。</p>
        <Provenance source={security.source} reference={security.reference} publishedAt={security.published_at} />
      </section>
      <section className="market-context-section" aria-label="交易所日历"><h5>{calendar.exchange === "SH" ? "上交所" : calendar.exchange === "SZ" ? "深交所" : "北交所"}日历 · 查询范围内</h5>
        <dl className="market-context-facts"><div><dt>报告开市</dt><dd>{calendar.open_dates.length} 日</dd></div>
          <div><dt>报告休市</dt><dd>{calendar.closed_dates.length} 日</dd></div><div><dt>状态未知</dt><dd>{calendar.unknown_dates.length} 日</dd></div></dl>
        <p className="market-context-source">日历覆盖期：{calendar.coverage_start === null ? "未配置" : `${calendar.coverage_start} — ${calendar.coverage_end}`}；未覆盖或未确认的日期不按周末、节假日惯例自动推断。</p>
        <Provenance source={calendar.source} reference={calendar.reference} publishedAt={calendar.published_at} />
        <DateList title="报告开市日期" dates={calendar.open_dates} /><DateList title="报告休市日期" dates={calendar.closed_dates} />
        <DateList title="未知日期" dates={calendar.unknown_dates} />
        <DateList title="开市但未返回日线" dates={calendar.missing_bar_dates} /><DateList title="休市却返回日线" dates={calendar.unexpected_bar_dates} />
        <p className="market-context-limit">开市不等于该股票可交易；开市但缺少日线不等于停牌。冲突仅作提示，不改写、补齐或删除行情。</p>
      </section>
      {(!!data.conflicts.length || !!data.warnings.length) && <section className="market-context-notices" aria-label="上下文限制与冲突">
        <strong>来源限制{data.conflicts.length > 0 && `与 ${data.conflicts.length} 项冲突`}</strong>
        <ul>{data.conflicts.map((code) => <li key={`conflict-${code}`}>{marketContextNotice(code, "conflict")}</li>)}
          {data.warnings.map((code) => <li key={`warning-${code}`}>{marketContextNotice(code)}</li>)}</ul>
      </section>}
      <p className="market-context-limit">仅供研究阅读。未评估涨跌停、委托资格或完整交易规则，不承诺可交易，也不提供下单权限。</p>
    </div>
  </section>;
}

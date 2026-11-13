import { useEffect, useId, useState } from "react";
import type { PreferenceChanges, PreferenceFields, PreferenceRecord, SecuritySummary } from "./api";
import type { ResearchController, WorkspaceState } from "./controller";

type Props = { state: WorkspaceState; controller: ResearchController };
export type PreferenceDraftRequest = { instrumentId: string; kind: "exit" | "entry" };
const holdings = { unset: "未填写", unknown: "不确定", held: "持有", not_held: "未持有" };
const horizons = { unset: "未填写", short: "短线", medium: "中线", long: "长线" };
const risks = { unset: "未填写", low: "谨慎", medium: "均衡", high: "积极" };
export const preferenceDescription = (value: PreferenceFields) =>
  `持有状态：${holdings[value.holding_status ?? "unset"]}；期限偏好：${horizons[value.horizon ?? "unset"]}；风险偏好：${risks[value.risk ?? "unset"]}`;
const recordTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
const confirmed = (state: WorkspaceState) => !!state.preferences?.enabled && !state.preferencesLoading && !state.preferencesError;

function PreferenceForm({ state, controller, security, generation, initial }: Props & {
  security: SecuritySummary; generation: number; initial?: PreferenceRecord;
}) {
  // Keep the original revision and edits when a refresh reports another page's changes.
  const [basis] = useState(initial);
  const [fields, setFields] = useState<PreferenceFields>(() => ({ holding_status: initial?.holding_status ?? null,
    horizon: initial?.horizon ?? null, risk: initial?.risk ?? null }));
  const [formError, setFormError] = useState<string | null>(null);
  const busy = state.preferencesSaving || state.switching;
  const hasFields = Object.values(fields).some(value => value !== null);
  const changedElsewhere = initial?.revision !== basis?.revision;
  const save = () => {
    if (!hasFields) { setFormError("至少填写一项。要清除整份资料，请使用删除记录。"); return; }
    const changes: PreferenceChanges = {};
    if (fields.holding_status !== (basis?.holding_status ?? null)) changes.holding_status = fields.holding_status;
    if (fields.horizon !== (basis?.horizon ?? null)) changes.horizon = fields.horizon;
    if (fields.risk !== (basis?.risk ?? null)) changes.risk = fields.risk;
    if (!Object.keys(changes).length) { setFormError("资料未修改；原记录时间保持不变。"); return; }
    setFormError(null);
    void controller.savePreferences(security.instrument_id, basis?.revision ?? null, changes)
      .then(saved => { if (saved) controller.closePreferenceEditor(generation); });
  };
  return <form className="preference-editor" aria-label={`${security.name}的持仓与偏好`} onSubmit={event => { event.preventDefault(); save(); }}>
    <strong>{security.name} <small>{security.instrument_id}</small></strong>
    <p className="watchlist-note">请核对类别后保存。仅记录自报背景，不记录成本价或仓位比例，不自动提供给模型。</p>
    <label>持有状态<select autoFocus value={fields.holding_status ?? ""} disabled={busy} onChange={event =>
      setFields({ ...fields, holding_status: (event.target.value || null) as PreferenceFields["holding_status"] })}>
      <option value="">未填写</option><option value="unknown">不确定</option><option value="held">持有</option><option value="not_held">未持有</option>
    </select></label>
    <label>期限偏好<select value={fields.horizon ?? ""} disabled={busy} onChange={event =>
      setFields({ ...fields, horizon: (event.target.value || null) as PreferenceFields["horizon"] })}>
      <option value="">未填写</option><option value="short">短线</option><option value="medium">中线</option><option value="long">长线</option>
    </select></label>
    <label>风险偏好<select value={fields.risk ?? ""} disabled={busy} onChange={event =>
      setFields({ ...fields, risk: (event.target.value || null) as PreferenceFields["risk"] })}>
      <option value="">未填写</option><option value="low">谨慎</option><option value="medium">均衡</option><option value="high">积极</option>
    </select></label>
    {!hasFields && <p className="watchlist-note">至少填写一项；清除全部类别请删除整份资料。</p>}
    {changedElsewhere && <div className="preference-conflict" role="status"><p>已读取的资料版本发生变化，当前编辑仍保留。请重新核对后再保存。</p>
      <button type="button" disabled={busy || !confirmed(state)} onClick={() => controller.openPreferenceEditor(security)}>使用当前资料重新填表</button></div>}
    {formError && <p className="error-text" role="alert">{formError}</p>}
    <div className="preference-form-actions"><button type="submit" disabled={busy || !hasFields}>保存持仓与偏好</button>
      <button type="button" disabled={busy} onClick={() => controller.closePreferenceEditor(generation)}>关闭资料表单</button></div>
  </form>;
}

export function PersonalPreferencesPanel({ state, controller }: Props) {
  const [query, setQuery] = useState("");
  const [deleting, setDeleting] = useState<{ item: PreferenceRecord } | { collection: string } | null>(null);
  useEffect(() => { setQuery(""); setDeleting(null); }, [state.mode, state.switching]);
  const enabled = state.mode === "normal" && state.health?.personal_memory_enabled === true && state.preferences?.enabled !== false;
  const busy = state.preferencesSaving || state.switching;
  const items = state.preferences?.items ?? [];
  const editor = state.preferencesEditor;
  const remove = () => {
    if (!deleting) return;
    const task = "item" in deleting ? controller.deletePreference(deleting.item.instrument_id, deleting.item.revision)
      : controller.clearPreferences(deleting.collection);
    void task.then(saved => { if (saved) setDeleting(null); });
  };
  return <section className="watchlist-panel preferences-panel" aria-label="持仓与偏好">
    <div className="watchlist-heading"><span className="sidebar-caption">持仓与偏好 · 仅本机</span>
      {enabled && <span className="watchlist-count">{state.preferences ? `${items.length} / 50` : "— / 50"}</span>}</div>
    {state.switching ? <p className="empty-index">正在切换服务模式…</p>
      : state.mode !== "normal" ? <p className="empty-index">测试模式不读取或保存持仓与偏好。</p>
        : !state.health ? <p className="empty-index">连接本机服务后可查看资料。</p>
          : !enabled ? <p className="empty-index">持仓与偏好未开启。请用 --personal-memory-db 显式开启；--no-memory 会关闭全部记忆。</p>
            : <>
              <p className="watchlist-note">本机自报，可能已变化。不自动发给模型。修改资料不改变自选或聊天历史，不占会话问题额度。</p>
              <div className="watchlist-tools"><button type="button" disabled={busy || state.preferencesLoading} onClick={() => void controller.refreshPreferences()}>刷新资料</button>
                <button type="button" disabled={busy || !confirmed(state) || !items.length || !state.preferences?.collection_revision}
                  onClick={() => setDeleting({ collection: state.preferences!.collection_revision! })}>清空持仓与偏好</button></div>
              {state.preferencesLoading && <p className="watchlist-note" role="status">正在读取持仓与偏好…</p>}
              {state.preferencesError && <p className="error-text" role="alert">{state.preferencesError}{state.preferences && " 以下为上次确认的资料。"}</p>}
              {state.preferencesNotice && <p className="watchlist-note" role="status">{state.preferencesNotice}</p>}
              {editor && <PreferenceForm key={`${editor.security.instrument_id}:${editor.generation}`} state={state} controller={controller}
                security={editor.security} generation={editor.generation} initial={items.find(item => item.instrument_id === editor.security.instrument_id)} />}
              <form className="watchlist-search" aria-label="搜索持仓与偏好股票" onSubmit={event => { event.preventDefault(); void controller.searchPreferences(query); }}>
                <label className="sr-only" htmlFor="preferences-query">资料股票名称或完整代码</label>
                <input id="preferences-query" value={query} maxLength={80} placeholder="搜索股票后记录资料" disabled={busy}
                  onChange={event => { setQuery(event.target.value); controller.clearPreferencesSearch(); }} />
                <button type="submit" disabled={busy || state.preferencesSearchLoading || !query.trim()}>搜索资料股票</button>
              </form>
              {state.preferencesSearchLoading && <p className="watchlist-note" role="status">正在查询本机证券目录…</p>}
              {state.preferencesSearchError && <p className="error-text" role="alert">{state.preferencesSearchError}</p>}
              {state.preferencesSearch && <div className="watchlist-search-results" aria-label="资料搜索结果">
                {!state.preferencesSearch.candidates.length ? <p className="empty-index">没有匹配的证券。</p>
                  : <ul>{state.preferencesSearch.candidates.map(item => <li key={item.instrument_id}>
                    <span>{item.name}<small>{item.instrument_id}</small></span>
                    <button type="button" disabled={busy || !confirmed(state)} aria-label={`管理资料：${item.name}（${item.instrument_id}）`}
                      onClick={() => controller.openPreferenceEditor(item)}>管理资料</button></li>)}</ul>}
                {!confirmed(state) && <p className="watchlist-note">资料清单尚未确认，请先刷新资料。</p>}
                {state.preferencesSearch.truncated && <p className="watchlist-note">仅显示前 20 项，请缩小搜索范围。</p>}
              </div>}
              {state.preferences && (!items.length ? <p className="empty-index">尚未记录持仓与偏好；自选不代表持有。</p>
                : <ul className="preferences-items">{items.map(item => <li key={item.instrument_id}>
                  <div><strong>{item.name}</strong> <small>{item.instrument_id}</small>
                    <p>{preferenceDescription(item)}</p><small>记录最后修改：{recordTime(item.updated_at)}</small></div>
                  <div className="preference-row-actions"><button type="button" disabled={busy || !confirmed(state)} aria-label={`编辑资料：${item.name}`}
                    onClick={() => controller.openPreferenceEditor(item)}>编辑</button>
                    <button type="button" disabled={busy || !confirmed(state)} aria-label={`删除资料：${item.name}`} onClick={() => setDeleting({ item })}>删除</button></div>
                </li>)}</ul>)}
              {deleting && <div className="confirm-panel" role="group" aria-label="确认删除持仓与偏好">
                <p>{"item" in deleting ? `删除${deleting.item.name}（${deleting.item.instrument_id}）的持仓与偏好？` : "清空全部持仓与偏好？"}自选股和聊天历史不会删除。</p>
                <button type="button" disabled={busy} onClick={remove}>确认删除资料</button>
                <button type="button" disabled={busy} onClick={() => setDeleting(null)}>返回</button></div>}
            </>}
  </section>;
}

export function PersonalPreferenceHint({ securities, state, openEditor, onDraft }: {
  securities: SecuritySummary[]; state: WorkspaceState; openEditor: (security: SecuritySummary) => void; onDraft: (request: PreferenceDraftRequest) => void;
}) {
  const hintId = useId();
  if (state.mode !== "normal" || state.switching || state.health?.personal_memory_enabled !== true || !state.preferences?.enabled) return null;
  const ready = confirmed(state) && !state.preferencesSaving;
  const draftDisabledReason = !ready ? state.preferencesSaving ? "资料正在保存，请稍后核对。" : "资料待刷新核对，请先在面板刷新资料。"
    : state.loading || state.phase !== "idle" ? "研究正在处理，请结束后再放入草稿。" : null;
  return <div className="personal-preference-hints" aria-label="当前研究对象的本机资料">
    {securities.map(security => {
      const item = state.preferences!.items.find(value => value.instrument_id === security.instrument_id);
      return <section key={security.instrument_id} className="personal-preference-hint">
        <strong>{security.name} · 本机自报，可能已变化</strong>
        {item ? <><p>{item.holding_status === null ? "持有状态尚未填写。" : `你曾记录为${holdings[item.holding_status]}。`}</p>
          <p>{preferenceDescription(item)}</p><small>记录最后修改：{recordTime(item.updated_at)}；不是持仓核实时间。</small></>
          : <p>尚未记录持仓与偏好；自选不代表持有。</p>}
        <div className="preference-hint-actions"><button type="button" disabled={!ready} onClick={() => openEditor(security)}>{item ? "修改本机资料" : "记录持仓与偏好"}</button>
          {item && (item.holding_status === "held" || item.holding_status === "not_held") && <button type="button"
            aria-describedby={draftDisabledReason ? `${hintId}-${security.instrument_id}` : undefined}
            disabled={!ready || state.loading || state.phase !== "idle"} onClick={() => onDraft({ instrumentId: security.instrument_id,
              kind: item.holding_status === "held" ? "exit" : "entry" })}>
            {item.holding_status === "held" ? "放入退出条件研究草稿" : "放入买入条件研究草稿"}</button>}
        </div>{draftDisabledReason && <p className="watchlist-note" id={`${hintId}-${security.instrument_id}`}>{draftDisabledReason}</p>}
        <p className="watchlist-note">提示仅在本机显示；研究草稿只含股票代码和问题，不复制上述类别。按发送后才交给当前模型，并按普通聊天规则保存。</p>
      </section>;
    })}
  </div>;
}

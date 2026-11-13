import { useEffect, useState } from "react";
import type { SecuritySummary } from "./api";
import type { ResearchController, WorkspaceState } from "./controller";

type Props = { state: WorkspaceState; controller: ResearchController };
const disabledMessage = "自选股未开启。请用 --personal-memory-db 显式开启；--no-memory 会关闭全部记忆。";

export function WatchlistPanel({ state, controller }: Props) {
  const [query, setQuery] = useState("");
  const [confirmation, setConfirmation] = useState<SecuritySummary | "all" | null>(null);
  useEffect(() => { setQuery(""); setConfirmation(null); }, [state.mode, state.switching]);
  const enabled = state.mode === "normal" && state.health?.personal_memory_enabled === true
    && state.watchlist?.enabled !== false;
  const busy = state.watchlistSaving || state.switching;
  const items = state.watchlist?.items ?? [];
  const confirm = () => {
    if (!confirmation) return;
    const operation = confirmation === "all" ? controller.clearWatchlist() : controller.removeWatchlist(confirmation.instrument_id);
    void operation.then(saved => { if (saved) setConfirmation(null); });
  };
  return <section className="watchlist-panel" aria-label="自选股">
    <div className="watchlist-heading"><span className="sidebar-caption">自选股 · 保存在本机</span>
      {enabled && <span className="watchlist-count">{state.watchlist ? `${items.length} / 50` : "— / 50"}</span>}</div>
    {state.switching ? <p className="empty-index">正在切换服务模式…</p>
      : state.mode !== "normal" ? <p className="empty-index">测试模式不读取或保存个人自选。</p>
        : !state.health ? <p className="empty-index">连接本机服务后可查看自选。</p>
          : !enabled ? <p className="empty-index">{disabledMessage}</p>
            : <>
              <p className="watchlist-note">仅保存在本机。本面板操作不调用模型或行情，不占会话问题额度。</p>
              <form className="watchlist-search" aria-label="搜索自选股票" onSubmit={event => { event.preventDefault(); void controller.searchWatchlist(query); }}>
                <label className="sr-only" htmlFor="watchlist-query">股票名称或完整代码</label>
                <input id="watchlist-query" value={query} maxLength={80} placeholder="名称或完整代码"
                  disabled={busy} onChange={event => { setQuery(event.target.value); controller.clearWatchlistSearch(); }} />
                <button type="submit" disabled={busy || state.watchlistSearchLoading || !query.trim()}>搜索</button>
              </form>
              {state.watchlistSearchLoading && <p className="watchlist-note" role="status">正在查询本机证券目录…</p>}
              {state.watchlistSearchError && <p className="error-text" role="alert">{state.watchlistSearchError}</p>}
              {state.watchlistSearch && <div className="watchlist-search-results" aria-label="自选搜索结果">
                {!state.watchlistSearch.candidates.length ? <p className="empty-index">没有匹配的证券，请换个名称或输入完整代码。</p>
                  : <ul>{state.watchlistSearch.candidates.map(item => {
                    const saved = items.some(value => value.instrument_id === item.instrument_id);
                    return <li key={item.instrument_id}><span>{item.name}<small>{item.instrument_id}</small></span>
                      <button type="button" disabled={busy || saved} aria-label={`加入自选：${item.name}（${item.instrument_id}）`}
                        onClick={() => void controller.addWatchlist(item.instrument_id)}>{saved ? "已在自选" : "加入"}</button></li>;
                  })}</ul>}
                {state.watchlistSearch.truncated && <p className="watchlist-note">仅显示前 20 项，请缩小搜索范围。</p>}
              </div>}
              <div className="watchlist-tools"><button type="button" disabled={busy || state.watchlistLoading}
                onClick={() => void controller.refreshWatchlist()}>刷新自选</button>
                <button type="button" disabled={busy || !items.length} onClick={() => setConfirmation("all")}>清空自选</button></div>
              {state.watchlistLoading && <p className="watchlist-note" role="status">正在读取自选…</p>}
              {state.watchlistError && <p className="error-text" role="alert">{state.watchlistError}{state.watchlist && " 以下清单为上次确认的结果。"}</p>}
              {state.watchlistNotice && <p className="watchlist-note" role="status">{state.watchlistNotice}</p>}
              {state.watchlist && (!items.length ? <p className="empty-index">还没有自选股。搜索并选择股票后加入。</p>
                : <ul className="watchlist-items">{items.map(item => <li key={item.instrument_id}>
                  <span>{item.name}<small>{item.instrument_id}</small></span>
                  <button type="button" disabled={busy} aria-label={`移除自选：${item.name}`}
                    onClick={() => setConfirmation(item)}>移除</button>
                </li>)}</ul>)}
              {confirmation && <div className="confirm-panel" role="group" aria-label="确认自选操作">
                <p>{confirmation === "all" ? "清空全部自选股？聊天历史不会删除。" : `从自选移除${confirmation.name}（${confirmation.instrument_id}）？聊天历史不会删除。`}</p>
                <button type="button" disabled={busy} onClick={confirm}>确认{confirmation === "all" ? "清空自选" : "移除"}</button>
                <button type="button" disabled={busy} onClick={() => setConfirmation(null)}>返回</button>
              </div>}
            </>}
  </section>;
}

export function WatchlistActions({ securities, state, controller }: Props & { securities: SecuritySummary[] }) {
  const [removing, setRemoving] = useState<SecuritySummary | null>(null);
  useEffect(() => { setRemoving(null); }, [state.mode, state.switching]);
  if (state.mode !== "normal" || state.switching || state.health?.personal_memory_enabled !== true || !state.watchlist?.enabled) return null;
  return <div className="watchlist-result-actions" aria-label="研究对象自选操作">
    {securities.map(item => {
      const saved = state.watchlist!.items.some(value => value.instrument_id === item.instrument_id);
      return <button key={item.instrument_id} type="button" disabled={state.watchlistSaving || state.watchlistLoading}
        aria-label={`${saved ? "移除" : "加入"}自选：${item.name}（${item.instrument_id}）`}
        onClick={() => saved ? setRemoving(item) : void controller.addWatchlist(item.instrument_id)}>
        {item.name} · {saved ? "移除自选" : "加入自选"}</button>;
    })}
    {removing && <div className="confirm-panel" role="group" aria-label="确认移除研究对象自选">
      <p>从自选移除{removing.name}（{removing.instrument_id}）？聊天历史不会删除。</p>
      <button type="button" disabled={state.watchlistSaving} onClick={() => {
        void controller.removeWatchlist(removing.instrument_id).then(saved => { if (saved) setRemoving(null); });
      }}>确认移除</button>
      <button type="button" disabled={state.watchlistSaving} onClick={() => setRemoving(null)}>返回</button>
    </div>}
  </div>;
}

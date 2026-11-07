"""ウォッチリストの読み取りモデル。画面表示とDB取得を分離する。"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from batch_dates import jst_today
from config import db, WATCHLIST_CHART_YEARS


def load_workspace() -> dict:
    from watchlist_service import ensure_schema
    ensure_schema()
    with db() as cur:
        cur.execute("SELECT id, name FROM watchlist_lists ORDER BY sort_order, id")
        lists = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        # 最新価格の探索も保存銘柄に限定し、全銘柄の集計を避ける。
        cur.execute("""
            SELECT w.code, s.name, p.date, p.close, p.change_pct,
                   f.per, f.pbr, f.div_yield, w.added_at,
                   COALESCE(m.n, 0), m.last_at, sm.content, src.title
            FROM watchlist w LEFT JOIN stocks s ON s.code = w.code
            LEFT JOIN (SELECT d.code, MAX(d.date) latest_date FROM price_history d
                       JOIN watchlist saved ON saved.code = d.code
                       WHERE d.close IS NOT NULL GROUP BY d.code) latest ON latest.code = w.code
            LEFT JOIN price_history p ON p.code = w.code AND p.date = latest.latest_date
            LEFT JOIN stock_fundamentals f ON f.code = w.code
            LEFT JOIN (SELECT sm.code, COUNT(*) n, MAX(sm.id) last_id, MAX(sm.created_at) last_at
                       FROM stock_memos sm JOIN watchlist w ON w.code = sm.code
                       GROUP BY sm.code) m ON m.code = w.code
            LEFT JOIN stock_memos sm ON sm.id = m.last_id
            LEFT JOIN stock_memo_sources src ON src.memo_id = sm.id
            ORDER BY w.added_at DESC, w.code
        """)
        keys = ("code", "name", "price_date", "close", "change_pct", "per", "pbr",
                "div_yield", "added_at", "note_count", "last_note_at", "last_note", "last_title")
        stocks = [dict(zip(keys, row)) for row in cur.fetchall()]
        by_code = {s["code"]: s for s in stocks}
        for stock in stocks:
            stock["lists"], stock["alerts"] = [], []
            for key in ("close", "change_pct", "per", "pbr", "div_yield"):
                stock[key] = float(stock[key]) if stock[key] is not None else None
            for key in ("price_date", "added_at", "last_note_at"):
                stock[key] = str(stock[key]) if stock[key] is not None else ""
            stock["last_note"] = (stock["last_title"] or stock["last_note"] or "")[:200]
        cur.execute("""SELECT i.code, i.list_id FROM watchlist_list_items i
                       JOIN watchlist w ON w.code = i.code""")
        for code, list_id in cur.fetchall():
            by_code[code]["lists"].append(list_id)
        cur.execute("""
            SELECT a.code, a.direction, a.target_price, a.status, a.effective_from,
                   e.id, e.price_date, e.triggered_price
            FROM price_alerts a JOIN watchlist w ON w.code = a.code
            LEFT JOIN price_alert_events e ON e.alert_id = a.id AND e.alert_version = a.version
              AND e.cancelled_at IS NULL AND e.acknowledged_at IS NULL
        """)
        for code, direction, target, status, effective, eid, edate, price in cur.fetchall():
            by_code[code]["alerts"].append({"direction": direction, "target": float(target),
                "status": status, "effective": str(effective), "event_id": eid,
                "event_date": str(edate or ""), "price": float(price) if price is not None else None})
    return {"lists": lists, "stocks": stocks}


def load_memos(code: str) -> list[dict]:
    from watchlist_service import ensure_schema
    ensure_schema()
    with db() as cur:
        cur.execute("""SELECT m.id, m.content, m.created_at, s.title, s.source_url
                       FROM stock_memos m LEFT JOIN stock_memo_sources s ON s.memo_id = m.id
                       WHERE m.code = %s ORDER BY m.created_at DESC, m.id DESC""", (code,))
        rows = cur.fetchall()
    result = []
    for mid, content, created, title, url in rows:
        host = urlsplit(url or "").hostname or ""
        kind = "x" if host.lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} else "link" if url else "memo"
        result.append({"id": mid, "content": content or "", "created_at": str(created),
                       "title": title or "", "source_url": url or "", "kind": kind})
    return result


def load_chart(code: str) -> list[dict]:
    from price_store import load_candles
    return load_candles([code], jst_today() - timedelta(days=366 * WATCHLIST_CHART_YEARS)).get(code, [])

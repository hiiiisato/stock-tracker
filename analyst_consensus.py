#!/usr/bin/env python3
"""アナリスト業績コンセンサスの取得 — みんかぶ(minkabu.jp)から銘柄別に取得してDB化する。

【目的】
プロのアナリスト予想（目標株価・レーティング・今期/来期の業績コンセンサス）を取り込み、
「会社予想がコンセンサスをどれだけ下回っているか(=上方修正余地)」「コンセンサスの改善トレンド
(アナリストが強気化しているか)」を定量化して、上がる株の発掘に使う。

【データ源】みんかぶ /stock/<code>/analyst_consensus（無料・関連度付きテーマと同じ運営）。
IFIS株予想/Yahooも候補だったが、業績コンセンサスの数値表・予想推移まで無料で取れるのは
みんかぶのみ（2026-07調査）。UA判定あり → MINKABU_HEADERS(Safari系)を使う。

【取得項目】(analyst_consensus テーブル)
- target_price          : アナリスト平均目標株価（最新）
- target_price_1w/1m/3m : 目標株価の推移（上方/下方修正トレンド検知用）
- upside_pct            : 目標株価に対する現在株価の上昇余地(%)
- rating                : 総合レーティング（強気買い/買い/中立/売り/強気売り）
- n_strong_buy/buy/neutral/sell/strong_sell : レーティング内訳の人数
- fc_period             : 今期予想の決算期末（例 2027-03-31）
- cons_revenue/op/ordinary/net/eps : 今期のアナリスト予想（営業/経常は通期実績列から補完）
- company_revenue/op/ordinary/net/eps : 会社予想（同ページ掲載）
- cons_net_1w/1m/3m     : 純利益コンセンサスの推移（改善トレンド検知用）

【更新】日次で古い順に DAILY_LIMIT 件巡回（全体≒2週間で一巡）。misc_batch 夜間便に組込。
取得失敗時は既存データ維持（壊れない）。テーマ同期と同じくみんかぶ負荷に配慮。

CLI:
  python analyst_consensus.py                 # DAILY_LIMIT 件を古い順に更新
  python analyst_consensus.py --limit 50
  python analyst_consensus.py --codes 7203,6758
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from config import get_conn, bulk_upsert

BASE = "https://minkabu.jp"
# BOT対策: 実ブラウザ(Safari)を忠実に模したフルヘッダ。最小ヘッダより遮断されにくい。
MINKABU_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
    "Referer": "https://minkabu.jp/",
}

# BOT対策の要点:
# ① 件数を抑える(80/日) ② 間隔をランダム化(人間らしく) ③ セッションでCookieを引き継ぐ
# ④ 実ブラウザ相当のフルヘッダ ⑤ 遮断されたらRenderの別IP経由(プロキシ)へ自動切替
DAILY_LIMIT   = 80    # 1回の巡回件数（カバレッジ~700銘柄を約9日で一巡。量で弾かれない水準）
DELAY_MIN     = 2.0   # リクエスト間隔の下限(秒)
DELAY_MAX     = 4.5   # 上限。この範囲でランダム化して機械的アクセスに見せない
REFRESH_DAYS  = 12    # 取得済みをこの日数は再取得しない
MAX_403       = 3     # 直接取得で連続403がこの数に達したらプロキシへ切替
# GHA/ローカルのIPがみんかぶに遮断された時のフォールバック（Renderの別IP経由・kabutanと同方式）
PROXY_BASE = os.environ.get("MINKABU_PROXY_BASE", "https://stock-tracker-rfqn.onrender.com")


def _sleep():
    """人間らしいランダム間隔。時々長めの休止も入れて機械的パターンを避ける。"""
    d = random.uniform(DELAY_MIN, DELAY_MAX)
    if random.random() < 0.1:      # 10%の確率で長め(考えている風)
        d += random.uniform(3, 7)
    time.sleep(d)
RATING_MAP = {"強気買い": 5, "買い": 4, "中立": 3, "売り": 2, "強気売り": 1}


def ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analyst_consensus (
            code            VARCHAR(10) PRIMARY KEY,
            target_price    DECIMAL(12,2),
            target_price_1w DECIMAL(12,2),
            target_price_1m DECIMAL(12,2),
            target_price_3m DECIMAL(12,2),
            upside_pct      DECIMAL(8,2),
            rating          VARCHAR(8),
            rating_score    DECIMAL(4,2),        -- 内訳の加重平均(1-5)
            n_strong_buy    INT, n_buy INT, n_neutral INT, n_sell INT, n_strong_sell INT,
            n_analysts      INT,
            fc_period       DATE,                -- 今期の決算期末
            cons_revenue    BIGINT, cons_op BIGINT, cons_ordinary BIGINT,
            cons_net        BIGINT, cons_eps DECIMAL(12,2),
            cons_net_1w     BIGINT, cons_net_1m BIGINT, cons_net_3m BIGINT,
            company_revenue BIGINT, company_op BIGINT, company_ordinary BIGINT,
            company_net     BIGINT, company_eps DECIMAL(12,2),
            fetched_at      DATETIME,
            updated_at      DATETIME
        )
    """)


def _num(s: str) -> float | None:
    """'3,615' '298.18' '---' '52,894,495' → float / None。"""
    if not s:
        return None
    s = s.replace(",", "").replace("円", "").replace("人", "").strip()
    if s in ("---", "--", "", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _million_yen(s: str) -> int | None:
    """百万円単位の値を円(bigint)にする（みんかぶ業績は百万円表記）。"""
    v = _num(s)
    return int(v * 1_000_000) if v is not None else None


def parse_consensus(html: str) -> dict:
    """analyst_consensus ページHTML → 構造化dict。取れない項目は None。"""
    soup = BeautifulSoup(html, "html.parser")
    d: dict = {}

    # ① 上昇余地
    m = re.search(r"あと([0-9.\-]+)%上昇", html)
    d["upside_pct"] = _num(m.group(1)) if m else None
    # 総合レーティング（本文「アナリスト判断（コンセンサス）は、買い。」）
    m = re.search(r"アナリスト判断（コンセンサス）は、([^\s。]+)", html)
    d["rating"] = (m.group(1)[:8] if m else None)

    tables = soup.find_all("table")

    def _find(pred):
        for t in tables:
            if pred(t.get_text(" ", strip=True)):
                return t
        return None

    # ② レーティング内訳（強気買い/買い/中立/売り/強気売りの人数）
    t = None
    for tb in tables:
        ths = [th.get_text(strip=True) for th in tb.find_all("th")]
        if "強気買い" in ths and "強気売り" in ths:
            t = tb
            break
    if t:
        nums = None
        for tr in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cells) == 5 and any("人" in c for c in cells):
                nums = [int(_num(c) or 0) for c in cells]
                break
        if nums:
            d["n_strong_buy"], d["n_buy"], d["n_neutral"], d["n_sell"], d["n_strong_sell"] = nums
            total = sum(nums)
            d["n_analysts"] = total
            if total:
                d["rating_score"] = round(sum(n * s for n, s in zip(nums, (5, 4, 3, 2, 1))) / total, 2)

    # ③ 目標株価の推移（列: 3ヶ月前/1ヶ月前/1週間前/最新）
    t = _find(lambda x: "予想株価" in x and "1週間前" in x)
    if t:
        for tr in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells and cells[0] == "予想株価" and len(cells) >= 5:
                d["target_price_3m"] = _num(cells[1])
                d["target_price_1m"] = _num(cells[2])
                d["target_price_1w"] = _num(cells[3])
                d["target_price"] = _num(cells[4])

    # ④ 今期のアナリスト予想 vs 会社予想（売上高/当期利益/EPS＋純益推移）
    t = _find(lambda x: "証券アナリスト予想" in x and "会社予想" in x and "1株当り利益" in x)
    if t:
        rows = {}
        for tr in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells:
                rows[cells[0]] = cells
        # 各行は [ラベル, 3M前, 1M前, 1W前, 最新(アナリスト), 最新(会社)]
        def r(label):
            return rows.get(label, [])
        rev = r("売上高"); net = r("当期利益"); eps = r("1株当り利益")
        if len(rev) >= 6:
            d["cons_revenue"] = _million_yen(rev[4]); d["company_revenue"] = _million_yen(rev[5])
        if len(net) >= 6:
            d["cons_net"] = _million_yen(net[4]); d["company_net"] = _million_yen(net[5])
            d["cons_net_1w"] = _million_yen(net[3]); d["cons_net_1m"] = _million_yen(net[2])
            d["cons_net_3m"] = _million_yen(net[1])
        if len(eps) >= 6:
            d["cons_eps"] = _num(eps[4]); d["company_eps"] = _num(eps[5])

    # ⑤ 通期実績＋今期予想テーブル（営業利益/経常利益の今期会社予想を補完、fc_period推定）
    t = _find(lambda x: "アナリスト予想" in x and "会社予想" in x and "営業利益" in x and "年" in x)
    if t:
        header = None
        rows = {}
        for tr in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells and header is None and any("年" in c and "期" in c for c in cells):
                header = cells
            elif cells:
                rows[cells[0]] = cells
        # 今期(アナリスト予想)列・会社予想列の位置を特定
        if header:
            ai = next((i for i, h in enumerate(header) if "アナリスト予想" in h), None)
            ci = next((i for i, h in enumerate(header) if "会社予想" in h), None)
            # fc_period: 「2027年3月期」→ 2027-03末
            hm = re.search(r"(\d{4})年(\d{1,2})月期", header[ai] if ai else "")
            if hm:
                y, mth = int(hm.group(1)), int(hm.group(2))
                import calendar
                d["fc_period"] = date(y, mth, calendar.monthrange(y, mth)[1])
            for lbl, key in [("営業利益", "op"), ("経常利益", "ordinary")]:
                row = rows.get(lbl)
                if row:
                    if ai is not None and ai < len(row):
                        d[f"cons_{key}"] = _million_yen(row[ai])
                    if ci is not None and ci < len(row):
                        d[f"company_{key}"] = _million_yen(row[ci])
    return d


class Blocked(Exception):
    """みんかぶに403(アクセス制限)された。以降のリクエストは諦める。"""


def _proxy_token() -> str | None:
    pw = os.environ.get("TIDB_PASSWORD", "")
    return hashlib.sha256(pw.encode()).hexdigest()[:32] if pw else None


def _fetch_html(session: requests.Session, code: str, via_proxy: bool) -> tuple[int, str]:
    """コンセンサスページHTMLを取得。via_proxy=True なら Render 経由。(status, text)。"""
    path = f"stock/{code}/analyst_consensus"
    if via_proxy:
        token = _proxy_token()
        if not token:
            return 0, ""
        url = f"{PROXY_BASE}/internal/minkabu?token={token}&path={quote(path, safe='')}"
        r = session.get(url, timeout=25)
        return r.status_code, r.text
    r = session.get(f"{BASE}/{path}", headers=MINKABU_HEADERS, timeout=20)
    return r.status_code, r.text


def fetch_one(session: requests.Session, code: str, via_proxy: bool = False) -> dict | None:
    """1銘柄のコンセンサスを取得。ページ無し/未カバレッジは {} を返す。403は Blocked。"""
    status, text = _fetch_html(session, code, via_proxy)
    if status == 404:
        return {}
    if status in (403, 429):
        raise Blocked(f"HTTP {status}")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    d = parse_consensus(text)
    # 目標株価もレーティングも取れなければ未カバレッジ扱い
    if d.get("target_price") is None and d.get("n_analysts") is None:
        return {}
    return d


_COLS = ["target_price", "target_price_1w", "target_price_1m", "target_price_3m", "upside_pct",
         "rating", "rating_score", "n_strong_buy", "n_buy", "n_neutral", "n_sell",
         "n_strong_sell", "n_analysts", "fc_period", "cons_revenue", "cons_op", "cons_ordinary",
         "cons_net", "cons_eps", "cons_net_1w", "cons_net_1m", "cons_net_3m",
         "company_revenue", "company_op", "company_ordinary", "company_net", "company_eps"]


def _targets(cur, limit: int, only_codes: list[str] | None) -> list[str]:
    if only_codes:
        return only_codes
    # アナリストカバレッジは大型株に集中するため時価総額の大きい順に巡回
    # （主力株ほど新鮮に保つ。未取得優先→古い順→時価総額大の順）
    cur.execute("""
        SELECT s.code FROM stocks s
        LEFT JOIN analyst_consensus a ON a.code = s.code
        LEFT JOIN stock_fundamentals f ON f.code = s.code
        WHERE s.is_active = 1 AND s.market_id IN (2,3,4)
          AND (a.fetched_at IS NULL OR a.fetched_at < %s)
        ORDER BY a.fetched_at IS NULL DESC, a.fetched_at ASC, f.market_cap DESC
        LIMIT %s
    """, (datetime.now() - timedelta(days=REFRESH_DAYS), limit))
    return [r[0] for r in cur.fetchall()]


def run(limit: int = DAILY_LIMIT, only_codes: list[str] | None = None, verbose: bool = True) -> dict:
    stats = {"fetched": 0, "saved": 0, "no_data": 0, "failed": 0, "blocked": False, "via_proxy": False}
    session = requests.Session()
    conn = get_conn()
    cur = conn.cursor()
    ensure_table(cur)
    conn.commit()
    now = datetime.now().replace(microsecond=0)
    consec_403 = 0
    via_proxy = False   # 直接取得で連続ブロックされたらプロキシ(Render別IP)へ切替
    # セッションウォームアップ: 先にトップページを踏んでCookieを得る（実ブラウザ相当）
    try:
        session.get(f"{BASE}/", headers=MINKABU_HEADERS, timeout=15)
        _sleep()
    except Exception:  # noqa: BLE001
        pass

    for code in _targets(cur, limit, only_codes):
        try:
            d = fetch_one(session, code, via_proxy)
            consec_403 = 0
        except Blocked as e:
            consec_403 += 1
            if consec_403 >= MAX_403:
                if not via_proxy and _proxy_token():
                    via_proxy = True
                    consec_403 = 0
                    stats["via_proxy"] = True
                    if verbose:
                        print(f"  直接取得が連続ブロック({e})→Renderプロキシ経由に切替")
                    _sleep()
                    continue
                stats["blocked"] = True
                if verbose:
                    print(f"  プロキシでもブロック({e})→本日は打ち切り（翌日再開）")
                break
            time.sleep(random.uniform(15, 30))   # ブロック時は大きく間隔を空けて様子見
            continue
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            if verbose:
                print(f"  [{code}] 取得失敗（既存維持）: {str(e)[:50]}")
            _sleep()
            continue
        stats["fetched"] += 1
        if not d:
            stats["no_data"] += 1
            # 未カバレッジでも fetched_at は更新して巡回を進める（毎回叩かない）
            cur.execute("""
                INSERT INTO analyst_consensus (code, fetched_at, updated_at) VALUES (%s,%s,%s)
                ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
            """, (code, now, now))
            conn.commit()
            _sleep()
            continue
        row = [code] + [d.get(c) for c in _COLS] + [now, now]
        bulk_upsert(cur, "analyst_consensus", ["code"] + _COLS + ["fetched_at", "updated_at"],
                    [row], update_cols=_COLS + ["fetched_at", "updated_at"])
        conn.commit()
        stats["saved"] += 1
        if verbose:
            tp = d.get("target_price")
            print(f"  [{code}] 目標{tp}円 {d.get('rating') or '-'} "
                  f"上昇余地{d.get('upside_pct')}% (アナリスト{d.get('n_analysts') or 0}人)")
        _sleep()

    cur.close()
    conn.close()
    if verbose:
        print(f"完了: 取得{stats['fetched']} 保存{stats['saved']} "
              f"未カバレッジ{stats['no_data']} 失敗{stats['failed']}"
              f"{' / プロキシ経由' if stats['via_proxy'] else ''}"
              f"{' / ブロックで打ち切り' if stats['blocked'] else ''}")
    return stats


def run_daily() -> int:
    return run(verbose=False)["saved"]


def main() -> None:
    ap = argparse.ArgumentParser(description="みんかぶからアナリスト業績コンセンサスを取得")
    ap.add_argument("--limit", type=int, default=DAILY_LIMIT)
    ap.add_argument("--codes", type=str, default="")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    run(limit=args.limit, only_codes=codes)


if __name__ == "__main__":
    main()

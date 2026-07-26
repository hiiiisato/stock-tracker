#!/usr/bin/env python3
"""四半期アナリストコンセンサス（経常利益）の取得 — IFIS株予報(kabuyoho.ifis.co.jp)から取得。

【目的】
証券会社アナリストの「四半期毎の経常利益コンセンサス（累積）」を取り込み、SBI証券アプリ等が
表示する「四半期毎の目安（赤三角）」を再現する。会社予想に対する進捗が、前年ペースだけでなく
アナリスト平均の四半期見通しに対してどうかを、実データで判定できるようにする。

【データ源】IFIS株予報 業績ページ index.php?action=tp1&sa=report&bcode=<code>。
  「経常利益四半期進ちょく」テーブルに、今期のコンセンサス予想が 第1Q/中間(2Q累計)/第3Q/通期
  の累積値（百万円）で載る。売上/営業/純利益の四半期コンセンサスは無料では非提供のため経常のみ。
  ※四半期毎のアナリストコンセンサスを無料で出しているのはIFISのみ（2026-07調査。みんかぶ/Yahoo/
    株探/バフェットコードは全て通期のみ）。

【取得項目】(quarterly_consensus テーブル、metric='ordinary' 経常利益)
- fiscal_year_end : 今期の決算期末（例 2026-12-31）
- cons_q1/q2/q3/full : 四半期累積のコンセンサス予想（百万円）。中間=2Q累計、第3Q=3Q累計。
- company_full : 同ページ掲載の通期会社予想（百万円、補助）
- prev_full    : 前期通期実績（百万円、補助）

【BOT対策】① 件数を抑え日次巡回 ② 間隔ランダム化 ③ セッションでCookie引継ぎ
  ④ 実ブラウザ相当フルヘッダ ⑤ robots.txt 尊重（/img/ /js/ /m* のみDisallow、index.phpは許可）
  ⑥ 遮断(403)されたら即中断（翌日再開）。IFIS負荷に配慮。

【更新】日次で古い順に DAILY_LIMIT 件巡回。取得失敗時は既存データ維持（壊れない）。

CLI:
  python quarterly_consensus.py                 # DAILY_LIMIT 件を古い順に更新
  python quarterly_consensus.py --limit 30
  python quarterly_consensus.py --codes 1911,7203
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from config import get_conn, bulk_upsert

BASE = "https://kabuyoho.ifis.co.jp"
IFIS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1", "Connection": "keep-alive",
    "Referer": f"{BASE}/",
}

DAILY_LIMIT = 60     # 1回の巡回件数（カバレッジ~1500銘柄を約1か月で一巡。量で弾かれない水準）
DELAY_MIN   = 2.5    # リクエスト間隔の下限(秒)
DELAY_MAX   = 5.0    # 上限。ランダム化して機械的アクセスに見せない
REFRESH_DAYS = 14    # 取得済みをこの日数は再取得しない
MAX_403      = 2     # 連続403がこの数に達したら本日打ち切り


def _sleep() -> None:
    d = random.uniform(DELAY_MIN, DELAY_MAX)
    if random.random() < 0.1:
        d += random.uniform(3, 7)
    time.sleep(d)


class Blocked(Exception):
    """IFISにアクセス制限(403/429)された。以降のリクエストは諦める。"""


def ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_consensus (
            code            VARCHAR(12),
            fiscal_year_end DATE,                   -- 今期の決算期末
            metric          VARCHAR(16) DEFAULT 'ordinary',  -- 経常利益(将来拡張用)
            cons_q1         BIGINT,                 -- 1Q累計コンセンサス(百万円)
            cons_q2         BIGINT,                 -- 2Q累計(中間)
            cons_q3         BIGINT,                 -- 3Q累計
            cons_full       BIGINT,                 -- 通期
            company_full    BIGINT,                 -- 通期会社予想(補助)
            prev_full       BIGINT,                 -- 前期通期実績(補助)
            fetched_at      DATETIME,
            updated_at      DATETIME,
            PRIMARY KEY (code, fiscal_year_end, metric)
        )
    """)


def _num(s: str) -> int | None:
    """'33,300' / '△1,200' / '(05/07)160,000' → int(百万円)。空/記号/'--' は None。
    セル先頭に付く発表日 '(MM/DD)' や末尾の進捗率 '13.6 %' は金額ではないため除外する。"""
    if not s:
        return None
    s = re.sub(r"\(\d{1,2}/\d{1,2}\)", "", s)          # 発表日プレフィックス除去
    s = s.replace(",", "").replace("△", "-").replace("▲", "-").strip()
    m = re.search(r"-?\d{2,}", s)                       # 金額（2桁以上）。日付の1桁月日は拾わない
    return int(m.group(0)) if m else None


def parse_quarterly_consensus(html: str) -> dict:
    """IFIS業績ページから経常利益の四半期コンセンサスを抽出。取れなければ {}。"""
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for t in soup.find_all("table"):
        tt = t.get_text(" ", strip=True)
        if "コンセンサス予想" in tt and ("第３四半期" in tt or "第3四半期" in tt):
            target = t
            break
    if target is None:
        return {}

    rows: dict[str, list[str]] = {}
    header: list[str] = []
    for tr in target.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        # ヘッダ行（第1四半期/中間/第3四半期/通期）は先頭セルが空
        if not header and any("四半期" in c or "通期" in c for c in cells) \
                and "コンセンサス" not in "".join(cells):
            header = cells
            continue
        rows[cells[0]] = cells[1:]

    cons = rows.get("コンセンサス予想")
    if not cons or len(cons) < 4:
        return {}
    q1, q2, q3, full = (_num(cons[i]) for i in range(4))
    if all(v is None for v in (q1, q2, q3, full)):
        return {}

    d: dict = {"cons_q1": q1, "cons_q2": q2, "cons_q3": q3, "cons_full": full}

    # 今期の決算期末: 「今期 202612進ちょく率」などの行キーから YYYYMM を拾う
    fye = None
    for k in rows:
        m = re.search(r"今期\s*(\d{4})(\d{2})", k)
        if m:
            y, mth = int(m.group(1)), int(m.group(2))
            import calendar
            fye = date(y, mth, calendar.monthrange(y, mth)[1])
            break
    d["fiscal_year_end"] = fye

    # 通期会社予想（「会社予想」行の通期列＝末尾の数値）
    comp = rows.get("会社予想")
    if comp:
        d["company_full"] = _num(comp[-1])
    # 前期通期実績（「前期 …進ちょく結果」行の通期列）
    for k, v in rows.items():
        if k.startswith("前期") and v:
            d["prev_full"] = _num(v[-1])
            break
    return d


def _fetch_html(session: requests.Session, code: str) -> tuple[int, str]:
    url = f"{BASE}/index.php?action=tp1&sa=report&bcode={code}"
    r = session.get(url, headers=IFIS_HEADERS, timeout=20)
    return r.status_code, r.text


def fetch_one(session: requests.Session, code: str) -> dict | None:
    """1銘柄の四半期コンセンサスを取得。ページ無し/未カバレッジは {}。403/429は Blocked。"""
    status, text = _fetch_html(session, code)
    if status == 404:
        return {}
    if status in (403, 429):
        raise Blocked(f"HTTP {status}")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    d = parse_quarterly_consensus(text)
    # 決算期末が取れないコンセンサスは信頼できないため未カバレッジ扱い
    if not d or d.get("fiscal_year_end") is None:
        return {}
    return d


_COLS = ["fiscal_year_end", "metric", "cons_q1", "cons_q2", "cons_q3",
         "cons_full", "company_full", "prev_full"]


def _targets(cur, limit: int, only_codes: list[str] | None) -> list[str]:
    if only_codes:
        return only_codes
    # アナリストカバレッジは大型株中心。時価総額の大きい順に、未取得優先→古い順で巡回。
    cur.execute("""
        SELECT s.code FROM stocks s
        LEFT JOIN quarterly_consensus q ON q.code = s.code
        LEFT JOIN stock_fundamentals f ON f.code = s.code
        WHERE s.is_active = 1 AND s.market_id IN (2,3,4)
          AND (q.fetched_at IS NULL OR q.fetched_at < %s)
        ORDER BY q.fetched_at IS NULL DESC, q.fetched_at ASC, f.market_cap DESC
        LIMIT %s
    """, (datetime.now() - timedelta(days=REFRESH_DAYS), limit))
    return [r[0] for r in cur.fetchall()]


def run(limit: int = DAILY_LIMIT, only_codes: list[str] | None = None, verbose: bool = True) -> dict:
    stats = {"fetched": 0, "saved": 0, "no_data": 0, "failed": 0, "blocked": False}
    session = requests.Session()
    conn = get_conn()
    cur = conn.cursor()
    ensure_table(cur)
    conn.commit()
    now = datetime.now().replace(microsecond=0)
    consec_403 = 0
    # ウォームアップ: トップページを踏んでCookieを得る
    try:
        session.get(f"{BASE}/", headers=IFIS_HEADERS, timeout=15)
        _sleep()
    except Exception:  # noqa: BLE001
        pass

    for code in _targets(cur, limit, only_codes):
        try:
            d = fetch_one(session, code)
            consec_403 = 0
        except Blocked as e:
            consec_403 += 1
            if consec_403 >= MAX_403:
                stats["blocked"] = True
                if verbose:
                    print(f"  連続ブロック({e})→本日は打ち切り（翌日再開）")
                break
            time.sleep(random.uniform(20, 40))
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
            # 未カバレッジでも fetched_at は進める（毎回叩かない）。ダミー期末で1行だけ持つ。
            cur.execute("""
                INSERT INTO quarterly_consensus (code, fiscal_year_end, metric, fetched_at, updated_at)
                VALUES (%s, '1900-01-01', 'ordinary', %s, %s)
                ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
            """, (code, now, now))
            conn.commit()
            _sleep()
            continue
        d.setdefault("metric", "ordinary")
        row = [code] + [d.get(c) for c in _COLS] + [now, now]
        bulk_upsert(cur, "quarterly_consensus", ["code"] + _COLS + ["fetched_at", "updated_at"],
                    [row], update_cols=_COLS + ["fetched_at", "updated_at"])
        conn.commit()
        stats["saved"] += 1
        if verbose:
            print(f"  [{code}] {d.get('fiscal_year_end')} 経常コンセンサス "
                  f"1Q={d.get('cons_q1')} 2Q={d.get('cons_q2')} "
                  f"3Q={d.get('cons_q3')} 通期={d.get('cons_full')}")
        _sleep()

    cur.close()
    conn.close()
    if verbose:
        print(f"完了: 取得{stats['fetched']} 保存{stats['saved']} "
              f"未カバレッジ{stats['no_data']} 失敗{stats['failed']}"
              f"{' / ブロックで打ち切り' if stats['blocked'] else ''}")
    return stats


def run_daily() -> int:
    return run(verbose=False)["saved"]


def main() -> None:
    ap = argparse.ArgumentParser(description="IFIS株予報から四半期経常コンセンサスを取得")
    ap.add_argument("--limit", type=int, default=DAILY_LIMIT)
    ap.add_argument("--codes", type=str, default="")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    run(limit=args.limit, only_codes=codes)


if __name__ == "__main__":
    main()

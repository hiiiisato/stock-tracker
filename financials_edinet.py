"""EDINET(edinetdb.jp)の有価証券報告書XBRLから過去業績を取得し、financials の欠損を穴埋めする。

背景・設計方針:
- kabutan/Yahoo経由で埋まらなかった過去の営業利益(operating_income)等の欠損を、
  公式EDINET(有報)ベースの構造化データで埋める。値はLLM非使用のXBRL抽出で信頼できる。
- **穴埋め(fill-only)専用**。既存の値(TDnet短信=権威データ)は絶対に上書きしない。
  config.bulk_upsert(fill_only_cols=...) の `COALESCE(col, VALUES(col))` を使い、NULLの列だけ埋める。
- EDINETの生値は円単位。TDnet由来の既存データが百万円単位(端数ゼロ)で入っているため、
  取得値も百万円に丸めて格納し、表示上の齟齬(桁の見え方の違い)を出さない。
- fiscal_year は「決算期末の年」。既存 financials 行(period_type='A')の YEAR(period_end) と突き合わせ、
  **既存のNULL行だけを埋める**(新しい期を勝手に作らない)。
- レート制限: edinetdb.jp 無料枠 100コール/日・3100コール/月。1銘柄1コールで全年度取得できる。
  レスポンスヘッダの残数を見て、上限手前で自動停止する(daily_run から残枠を消費して段階的に完了)。

メンテナンス(データ最新化)方針:
- 直近の期はTDnet(financials_tdnet.py)が短信ベースで自動更新するため、本モジュールは
  「過去の穴埋め」が主目的。daily_run に組込み、op がNULLの銘柄を優先度順(直近欠損DESC)に
  少しずつ取得する。有報は年1回更新なので、一度取得した銘柄は REFRESH_DAYS 再取得しない。

CLI:
    python financials_edinet.py                 # daily_limit までバックフィル
    python financials_edinet.py --limit 50      # 最大50銘柄
    python financials_edinet.py --codes 9221,7203
    python financials_edinet.py --force         # meta の取得済みスキップを無視
"""
from __future__ import annotations

import argparse
import datetime as _dt
import time

import requests

import config
from config import bulk_upsert

BASE = "https://edinetdb.jp/v1"
KEY = config.EDINETDB_API_KEY
HEADERS = {"X-API-Key": KEY, "Accept": "application/json"}

DELAY = 0.4          # API間隔(秒)
DAILY_LIMIT = 90     # 1回の実行で取得する最大銘柄数(無料枠100/日に対する既定上限)
DAILY_FLOOR = 4      # 日次残数がこれ未満になったら停止
MONTHLY_FLOOR = 10   # 月次残数がこれ未満になったら停止
REFRESH_DAYS = 90    # 取得済み銘柄を再取得しない期間(有報は年1回)

# EDINETレスポンス(snake_case) → financials 列。すべて穴埋め対象。
FIELD_MAP = {
    "revenue": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "ordinary_income": "ordinary_income",
    "net_income": "net_income",
    "total_assets": "total_assets",
    "cf_operating": "cf_operating",
}
_FIN_COLS = list(FIELD_MAP.values())


class QuotaExhausted(Exception):
    """日次/月次のAPI残枠が尽きたことを示す。"""


def _to_million(v) -> int | None:
    """円単位の生値を百万円単位に丸めた円値(百万の倍数)にする。既存TDnetデータと桁を揃える。"""
    if v is None:
        return None
    try:
        return int(round(float(v) / 1_000_000)) * 1_000_000
    except (TypeError, ValueError):
        return None


def normalize_zero_artifacts(cur) -> int:
    """Yahoo由来の op=0 疑似欠損を NULL に戻し、穴埋め対象に含める。

    Yahoo Finance は日本株の営業利益を欠損時に "0" で返す(financials.py の _zero_to_none で
    新規は防いでいるが、過去に書き込まれた 0 が残存)。売上>0 で営業利益が「正確に0円」は
    実務上まず有り得ないため、これは誤値。0 のままだと fill-only(COALESCE)では埋まらないので、
    一度 NULL に戻して EDINET の精密値で補完できるようにする。冪等(2回目以降は0件)。
    """
    cur.execute(
        "UPDATE financials SET operating_income=NULL "
        "WHERE operating_income=0 AND revenue>0 AND period_type='A'"
    )
    return cur.rowcount


def _ensure_meta(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS financials_edinet_meta (
            code         VARCHAR(10) PRIMARY KEY,
            edinet_code  VARCHAR(8),
            status       VARCHAR(16),
            filled_cells INT DEFAULT 0,
            last_fetched DATETIME
        )
        """
    )


def _fetch(edinet_code: str) -> list[dict]:
    """1銘柄の全年度財務を取得。残枠が尽きていれば QuotaExhausted を送出。"""
    r = requests.get(
        f"{BASE}/companies/{edinet_code}/financials",
        headers=HEADERS, timeout=20,
    )
    # レート制限ヘッダで事前・事後に判定
    daily_rem = r.headers.get("X-Ratelimit-Remaining")
    month_rem = r.headers.get("X-Ratelimit-Monthly-Remaining")
    if r.status_code == 429:
        raise QuotaExhausted("HTTP 429")
    r.raise_for_status()
    data = r.json().get("data", []) or []
    # 残枠が閾値未満なら、この結果を最後に次回以降を止める
    try:
        if daily_rem is not None and int(daily_rem) < DAILY_FLOOR:
            _fetch._stop = True
        if month_rem is not None and int(month_rem) < MONTHLY_FLOOR:
            _fetch._stop = True
    except ValueError:
        pass
    return data


def _targets(cur, limit: int, only_codes: list[str] | None, force: bool) -> list[tuple[str, str]]:
    """op がNULLの銘柄を (code, edinet_code) で返す。優先度=直近の欠損期がより新しい順。"""
    params: list = []
    where = [
        "f.period_type = 'A'",
        "f.operating_income IS NULL",
        "s.edinet_code IS NOT NULL",
        "s.edinet_code <> ''",
    ]
    if only_codes:
        ph = ",".join(["%s"] * len(only_codes))
        where.append(f"f.code IN ({ph})")
        params.extend(only_codes)
    if not force:
        # REFRESH_DAYS 以内に取得済みの銘柄は除外
        where.append(
            "(m.last_fetched IS NULL OR m.last_fetched < %s)"
        )
        params.append(_dt.datetime.now() - _dt.timedelta(days=REFRESH_DAYS))
    sql = f"""
        SELECT f.code, s.edinet_code, MAX(f.period_end) AS mx
        FROM financials f
        JOIN stocks s ON s.code = f.code
        LEFT JOIN financials_edinet_meta m ON m.code = f.code
        WHERE {' AND '.join(where)}
        GROUP BY f.code, s.edinet_code
        ORDER BY mx DESC
        LIMIT %s
    """
    params.append(limit)
    cur.execute(sql, params)
    return [(r[0], r[1]) for r in cur.fetchall()]


def _null_periods(cur, code: str) -> dict[int, _dt.date]:
    """code の op がNULLな年次(A)期末を {年: period_end} で返す。"""
    cur.execute(
        "SELECT period_end FROM financials "
        "WHERE code=%s AND period_type='A' AND operating_income IS NULL",
        (code,),
    )
    return {r[0].year: r[0] for r in cur.fetchall()}


def backfill(daily_limit: int = DAILY_LIMIT, only_codes: list[str] | None = None,
             force: bool = False, verbose: bool = True) -> dict:
    """op欠損銘柄をEDINETで穴埋めする。戻り値に処理件数などのサマリを返す。"""
    if not KEY:
        raise RuntimeError("EDINETDB_API_KEY が未設定です(https://edinetdb.jp/developers で無料発行)")

    _fetch._stop = False
    conn = config.get_conn()
    cur = conn.cursor()
    _ensure_meta(cur)
    normalized = normalize_zero_artifacts(cur)
    conn.commit()
    if verbose and normalized:
        print(f"op=0 疑似欠損を {normalized} 行 NULL化(穴埋め対象に追加)")

    targets = _targets(cur, daily_limit, only_codes, force)
    if verbose:
        print(f"対象 {len(targets)} 銘柄(op欠損・EDINETコード有・優先度=直近欠損順)")

    stats = {"fetched": 0, "filled_codes": 0, "filled_cells": 0, "no_data": 0, "stopped": False}
    now = _dt.datetime.now()

    for code, ec in targets:
        try:
            recs = _fetch(ec)
        except QuotaExhausted:
            stats["stopped"] = True
            if verbose:
                print("  API残枠が尽きたため停止")
            break
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  [{code}/{ec}] 取得失敗: {str(e)[:80]}")
            time.sleep(DELAY)
            continue

        stats["fetched"] += 1
        by_year = {r.get("fiscal_year"): r for r in recs if r.get("fiscal_year")}
        null_map = _null_periods(cur, code)

        rows: list[list] = []
        for year, pend in null_map.items():
            rec = by_year.get(year)
            if not rec:
                continue
            vals = {col: _to_million(rec.get(src)) for src, col in FIELD_MAP.items()}
            if all(v is None for v in vals.values()):
                continue
            rows.append([code, pend, "A"] + [vals[c] for c in _FIN_COLS])

        filled_cells = 0
        if rows:
            cols = ["code", "period_end", "period_type"] + _FIN_COLS
            bulk_upsert(
                cur, "financials", cols, rows,
                update_cols=_FIN_COLS,        # キー列は更新しない
                fill_only_cols=_FIN_COLS,     # 全列 COALESCE(既存NULLのみ埋める)
            )
            filled_cells = sum(1 for r in rows for v in r[3:] if v is not None)
            stats["filled_codes"] += 1
            stats["filled_cells"] += filled_cells
        else:
            stats["no_data"] += 1

        cur.execute(
            """
            INSERT INTO financials_edinet_meta (code, edinet_code, status, filled_cells, last_fetched)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              edinet_code=VALUES(edinet_code), status=VALUES(status),
              filled_cells=VALUES(filled_cells), last_fetched=VALUES(last_fetched)
            """,
            (code, ec, "filled" if rows else "no_data", filled_cells, now),
        )
        conn.commit()
        if verbose:
            print(f"  [{code}] {ec}: {'埋め ' + str(filled_cells) + 'セル' if rows else 'EDINETに該当年度なし'}")

        if _fetch._stop:
            stats["stopped"] = True
            if verbose:
                print("  日次/月次残枠が閾値未満のため停止")
            break
        time.sleep(DELAY)

    cur.close()
    conn.close()
    if verbose:
        print(f"完了: 取得{stats['fetched']} / 穴埋め{stats['filled_codes']}銘柄"
              f" {stats['filled_cells']}セル / 該当なし{stats['no_data']}"
              f"{' / 残枠切れ停止' if stats['stopped'] else ''}")
    return stats


def _remaining_targets(cur) -> int:
    cur.execute(
        """
        SELECT COUNT(DISTINCT f.code)
        FROM financials f JOIN stocks s ON s.code=f.code
        LEFT JOIN financials_edinet_meta m ON m.code=f.code
        WHERE f.period_type='A' AND f.operating_income IS NULL
          AND s.edinet_code IS NOT NULL AND s.edinet_code<>''
          AND (m.last_fetched IS NULL OR m.last_fetched < %s)
        """,
        (_dt.datetime.now() - _dt.timedelta(days=REFRESH_DAYS),),
    )
    return cur.fetchone()[0]


def run_incremental(daily_limit: int = DAILY_LIMIT) -> int:
    """daily_run から呼ぶ用。残枠内で穴埋めし、埋めたセル数を返す。"""
    stats = backfill(daily_limit=daily_limit, verbose=False)
    return stats["filled_cells"]


def main() -> None:
    ap = argparse.ArgumentParser(description="EDINET有報で financials の過去業績欠損を穴埋め")
    ap.add_argument("--limit", type=int, default=DAILY_LIMIT, help="最大取得銘柄数")
    ap.add_argument("--codes", type=str, default="", help="対象コード(カンマ区切り)")
    ap.add_argument("--force", action="store_true", help="取得済みスキップを無視")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    backfill(daily_limit=args.limit, only_codes=codes, force=args.force)


if __name__ == "__main__":
    main()

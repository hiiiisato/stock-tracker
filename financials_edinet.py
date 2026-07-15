"""EDINET(edinetdb.jp)の有価証券報告書XBRLから過去業績を取得する。2つの役割を1コールで担う:
  (1) financials の欠損(operating_income等)を穴埋め(fill-only・百万丸め)
  (2) 同じレスポンスで返る豊富な有報データ(損益内訳・CF・資本配分・従業員・ガバナンス等)を
      financials_edinet_annual にそのまま保存(将来の分析・スクリーニング用・生値精密)

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
import calendar
import datetime as _dt
import time

import requests

import config
from config import bulk_upsert

BASE = "https://edinetdb.jp/v1"
KEY = config.EDINETDB_API_KEY
HEADERS = {"X-API-Key": KEY, "Accept": "application/json"}

DELAY = 0.4          # API間隔(秒)
DAILY_LIMIT = 95     # 1回の実行で取得する最大銘柄数(edinet_texts/segmentsを停止し枠を集中)
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

# ── 付随データの保存用 ──────────────────────────────────────────────
# 同じ1コールで返る有報の豊富なデータ(損益内訳・CF・資本配分・従業員・ガバナンス等)を
# 権威データとしてそのままDBに残す(financials_edinet_annual)。将来の分析・スクリーニング用。
# こちらは百万丸めせずEDINETの生値(精密)を格納する(この表単体で一貫)。
# (col_name, source_key, sql_type, kind)  kind: yen=金額(bigint) / num=比率等(double) / int / str
EXTRA_COLS: list[tuple[str, str, str, str]] = [
    # 損益計算書
    ("revenue", "revenue", "BIGINT", "yen"),
    ("cost_of_sales", "cost_of_sales", "BIGINT", "yen"),
    ("gross_profit", "gross_profit", "BIGINT", "yen"),
    ("sga", "sga", "BIGINT", "yen"),
    ("operating_income", "operating_income", "BIGINT", "yen"),
    ("non_operating_income", "non_operating_income", "BIGINT", "yen"),
    ("non_operating_expenses", "non_operating_expenses", "BIGINT", "yen"),
    ("ordinary_income", "ordinary_income", "BIGINT", "yen"),
    ("extraordinary_income", "extraordinary_income", "BIGINT", "yen"),
    ("extraordinary_loss", "extraordinary_loss", "BIGINT", "yen"),
    ("profit_before_tax", "profit_before_tax", "BIGINT", "yen"),
    ("income_taxes", "income_taxes", "BIGINT", "yen"),
    ("net_income", "net_income", "BIGINT", "yen"),
    ("comprehensive_income", "comprehensive_income", "BIGINT", "yen"),
    # 貸借対照表
    ("total_assets", "total_assets", "BIGINT", "yen"),
    ("current_assets", "current_assets", "BIGINT", "yen"),
    ("noncurrent_assets", "noncurrent_assets", "BIGINT", "yen"),
    ("ppe", "ppe", "BIGINT", "yen"),
    ("intangible_assets", "intangible_assets", "BIGINT", "yen"),
    ("total_liabilities", "total_liabilities", "BIGINT", "yen"),
    ("current_liabilities", "current_liabilities", "BIGINT", "yen"),
    ("noncurrent_liabilities", "noncurrent_liabilities", "BIGINT", "yen"),
    ("net_assets", "net_assets", "BIGINT", "yen"),
    ("shareholders_equity", "shareholders_equity", "BIGINT", "yen"),
    ("retained_earnings", "retained_earnings", "BIGINT", "yen"),
    ("cash", "cash", "BIGINT", "yen"),
    ("inventories", "inventories", "BIGINT", "yen"),
    ("trade_receivables", "trade_receivables", "BIGINT", "yen"),
    ("trade_payables", "trade_payables", "BIGINT", "yen"),
    ("short_term_loans", "short_term_loans", "BIGINT", "yen"),
    ("long_term_loans", "long_term_loans", "BIGINT", "yen"),
    ("current_portion_lt_loans", "current_portion_lt_loans", "BIGINT", "yen"),
    # キャッシュフロー・資本配分
    ("cf_operating", "cf_operating", "BIGINT", "yen"),
    ("cf_investing", "cf_investing", "BIGINT", "yen"),
    ("cf_financing", "cf_financing", "BIGINT", "yen"),
    ("capex", "capex", "BIGINT", "yen"),
    ("depreciation", "depreciation", "BIGINT", "yen"),
    ("rnd_expenses", "rnd_expenses", "BIGINT", "yen"),
    # 1株・利回り・効率(公式値)
    ("eps", "eps", "DOUBLE", "num"),
    ("bps", "bps", "DOUBLE", "num"),
    ("per", "per", "DOUBLE", "num"),
    ("roe_official", "roe_official", "DOUBLE", "num"),
    ("equity_ratio_official", "equity_ratio_official", "DOUBLE", "num"),
    ("effective_tax_rate", "effective_tax_rate", "DOUBLE", "num"),
    ("dividend_per_share", "dividend_per_share", "DOUBLE", "num"),
    ("payout_ratio", "payout_ratio", "DOUBLE", "num"),
    # 従業員・ガバナンス
    ("num_employees", "num_employees", "INT", "int"),
    ("avg_annual_salary", "avg_annual_salary", "BIGINT", "yen"),
    ("avg_age", "avg_age", "DOUBLE", "num"),
    ("avg_tenure_years", "avg_tenure_years", "DOUBLE", "num"),
    ("female_director_ratio", "female_director_ratio", "DOUBLE", "num"),
    ("directors_ownership_ratio", "directors_ownership_ratio", "DOUBLE", "num"),
    ("cross_shareholding_book_value", "cross_shareholding_total_book_value", "BIGINT", "yen"),
    ("total_shareholder_return", "total_shareholder_return", "DOUBLE", "num"),
    # 株式
    ("shares_issued", "shares_issued", "BIGINT", "yen"),
    ("float_shares", "float_shares", "BIGINT", "yen"),
    ("treasury_shares_count", "treasury_shares_count", "BIGINT", "yen"),
    ("split_adjustment_factor", "split_adjustment_factor", "DOUBLE", "num"),
    # トレーサビリティ
    ("accounting_standard", "accounting_standard", "VARCHAR(8)", "str"),
    ("doc_id", "doc_id", "VARCHAR(128)", "str"),  # 四半期はdocIDが複数連結され長い
    ("submit_date", "submit_date", "VARCHAR(32)", "str"),
    ("edinet_filing_url", "edinet_filing_url", "VARCHAR(512)", "str"),
]


def _cast(kind: str, v):
    if v is None:
        return None
    try:
        if kind == "yen" or kind == "int":
            return int(round(float(v)))
        if kind == "num":
            return float(v)
        return str(v)[:255]
    except (TypeError, ValueError):
        return None


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
        "WHERE operating_income=0 AND revenue>0 AND period_type IN ('A','Q')"
    )
    return cur.rowcount


# 取得済み管理は期種別に分ける(年次=financials_edinet_meta / 四半期=financials_edinet_qmeta)。
META_TABLE = {"A": "financials_edinet_meta", "Q": "financials_edinet_qmeta"}


def _ensure_meta(cur) -> None:
    for tbl in META_TABLE.values():
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
                code         VARCHAR(10) PRIMARY KEY,
                edinet_code  VARCHAR(8),
                status       VARCHAR(16),
                filled_cells INT DEFAULT 0,
                last_fetched DATETIME
            )
            """
        )


def _ensure_extra_table(cur) -> None:
    """有報の豊富なデータを丸ごと残す表(年次 financials_edinet_annual / 四半期 _quarterly)。"""
    col_defs = ",\n            ".join(f"`{c}` {t}" for c, _s, t, _k in EXTRA_COLS)
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS financials_edinet_annual (
            code        VARCHAR(10) NOT NULL,
            fiscal_year INT NOT NULL,
            period_end  DATE,
            {col_defs},
            updated_at  DATETIME,
            UNIQUE KEY uq_code_fy (code, fiscal_year)
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS financials_edinet_quarterly (
            code        VARCHAR(10) NOT NULL,
            fiscal_year INT NOT NULL,
            quarter     TINYINT NOT NULL,
            period_end  DATE,
            {col_defs},
            updated_at  DATETIME,
            UNIQUE KEY uq_code_fy_q (code, fiscal_year, quarter)
        )
        """
    )


def _q_end(fiscal_year: int, quarter: int, fye_month: int) -> _dt.date | None:
    """(会計年度, 四半期, 決算月) から四半期末日を導出。Q4=決算月末、Q3=−3ヶ月…と遡る。"""
    if not fye_month or quarter not in (1, 2, 3, 4):
        return None
    m = fye_month - 3 * (4 - quarter)
    y = fiscal_year
    while m <= 0:
        m += 12
        y -= 1
    return _dt.date(y, m, calendar.monthrange(y, m)[1])


def _fye(cur, code: str) -> tuple[int, int] | None:
    """既存 financials 年次行から決算期末の (月, 日) を推定する。無ければ None。"""
    cur.execute(
        "SELECT period_end FROM financials WHERE code=%s AND period_type='A' "
        "ORDER BY period_end DESC LIMIT 1",
        (code,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return (row[0].month, row[0].day)


def _period_end(fiscal_year: int, fye: tuple[int, int] | None) -> _dt.date | None:
    if not fye:
        return None
    m, d = fye
    try:
        return _dt.date(fiscal_year, m, d)
    except ValueError:  # 2/29 等
        try:
            return _dt.date(fiscal_year, m, 28)
        except ValueError:
            return None


def _fetch(edinet_code: str, period: str = "annual") -> list[dict]:
    """1銘柄の財務を取得(period='annual' or 'quarterly_standalone')。残枠切れは QuotaExhausted。"""
    r = requests.get(
        f"{BASE}/companies/{edinet_code}/financials",
        headers=HEADERS, params={"period": period}, timeout=20,
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


def _targets(cur, limit: int, only_codes: list[str] | None, force: bool) -> list[tuple[str, str, str]]:
    """取得タスクを (code, edinet_code, kind) で優先度順に返す。kind: 'A'=年次 / 'Q'=四半期。
    優先度:
      ① 年次のop欠損(穴埋め最優先・直近欠損が新しい順)
      ② 四半期のop欠損/ゼロ(穴埋め)
      ③ 年次の未取得銘柄(付随データの全体カバレッジ)
    各期種の meta で REFRESH_DAYS 以内取得済みは除外。上限まで①→②→③の順で詰める。"""
    stale = _dt.datetime.now() - _dt.timedelta(days=REFRESH_DAYS)

    if only_codes:
        ph = ",".join(["%s"] * len(only_codes))
        cur.execute(
            f"SELECT s.code, s.edinet_code FROM stocks s "
            f"WHERE s.code IN ({ph}) AND s.edinet_code IS NOT NULL AND s.edinet_code<>''",
            only_codes,
        )
        base = [(r[0], r[1]) for r in cur.fetchall()]
        # 指定コードは年次・四半期の両方を対象にする
        return ([(c, e, "A") for c, e in base] + [(c, e, "Q") for c, e in base])[:limit]

    tasks: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(rows, kind):
        for c, ec in rows:
            if (c, kind) in seen:
                continue
            seen.add((c, kind))
            tasks.append((c, ec, kind))
            if len(tasks) >= limit:
                return True
        return False

    fresh = "" if force else "AND (m.last_fetched IS NULL OR m.last_fetched < %s)"
    sp = [] if force else [stale]

    # ① 年次op欠損（穴埋め最優先）
    cur.execute(f"""
        SELECT f.code, s.edinet_code, MAX(f.period_end) AS mx
        FROM financials f JOIN stocks s ON s.code=f.code
        LEFT JOIN financials_edinet_meta m ON m.code=f.code
        WHERE f.period_type='A' AND f.operating_income IS NULL
          AND s.edinet_code IS NOT NULL AND s.edinet_code<>'' {fresh}
        GROUP BY f.code, s.edinet_code ORDER BY mx DESC LIMIT %s
    """, sp + [limit])
    if _add([(r[0], r[1]) for r in cur.fetchall()], "A"):
        return tasks

    # ② 四半期op欠損（穴埋め）
    cur.execute(f"""
        SELECT f.code, s.edinet_code, MAX(f.period_end) AS mx
        FROM financials f JOIN stocks s ON s.code=f.code
        LEFT JOIN financials_edinet_qmeta m ON m.code=f.code
        WHERE f.period_type='Q' AND f.operating_income IS NULL
          AND s.edinet_code IS NOT NULL AND s.edinet_code<>'' {fresh}
        GROUP BY f.code, s.edinet_code ORDER BY mx DESC LIMIT %s
    """, sp + [limit])
    if _add([(r[0], r[1]) for r in cur.fetchall()], "Q"):
        return tasks

    # ③ 年次の未取得（付随データの全体カバレッジ）
    cur.execute(f"""
        SELECT s.code, s.edinet_code
        FROM stocks s LEFT JOIN financials_edinet_meta m ON m.code=s.code
        WHERE s.edinet_code IS NOT NULL AND s.edinet_code<>'' {fresh}
        ORDER BY s.code LIMIT %s
    """, sp + [limit])
    _add([(r[0], r[1]) for r in cur.fetchall()], "A")
    return tasks


def _null_periods(cur, code: str) -> dict[int, _dt.date]:
    """code の op がNULLな年次(A)期末を {年: period_end} で返す。"""
    cur.execute(
        "SELECT period_end FROM financials "
        "WHERE code=%s AND period_type='A' AND operating_income IS NULL",
        (code,),
    )
    return {r[0].year: r[0] for r in cur.fetchall()}


def _null_q_periods(cur, code: str) -> set[_dt.date]:
    """code の op がNULLな四半期(Q)期末の集合を返す。"""
    cur.execute(
        "SELECT period_end FROM financials "
        "WHERE code=%s AND period_type='Q' AND operating_income IS NULL",
        (code,),
    )
    return {r[0] for r in cur.fetchall()}


_EXTRA_NAMES = [c for c, _s, _t, _k in EXTRA_COLS]


def _process_annual(cur, code: str, recs: list[dict], now) -> tuple[int, int]:
    """年次: financials のop欠損を穴埋め + financials_edinet_annual に全保存。(穴埋めセル, 保存件数)。"""
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
    filled = 0
    if rows:
        cols = ["code", "period_end", "period_type"] + _FIN_COLS
        bulk_upsert(cur, "financials", cols, rows, update_cols=_FIN_COLS, fill_only_cols=_FIN_COLS)
        filled = sum(1 for r in rows for v in r[3:] if v is not None)

    fye = _fye(cur, code)
    extra = [[code, y, _period_end(y, fye)]
             + [_cast(k, rec.get(s)) for _c, s, _t, k in EXTRA_COLS] + [now]
             for y, rec in by_year.items()]
    if extra:
        ecols = ["code", "fiscal_year", "period_end"] + _EXTRA_NAMES + ["updated_at"]
        bulk_upsert(cur, "financials_edinet_annual", ecols, extra,
                    update_cols=["period_end"] + _EXTRA_NAMES + ["updated_at"])
    return filled, len(extra)


def _process_quarterly(cur, code: str, recs: list[dict], now) -> tuple[int, int]:
    """四半期: op欠損を穴埋め + financials_edinet_quarterly に全保存。(穴埋めセル, 保存件数)。"""
    fye = _fye(cur, code)
    fye_m = fye[0] if fye else None
    null_q = _null_q_periods(cur, code)

    rows: list[list] = []
    extra: list[list] = []
    for rec in recs:
        fy, q = rec.get("fiscal_year"), rec.get("quarter")
        if not fy or not q:
            continue
        pe = _q_end(fy, q, fye_m)
        if pe and pe in null_q:
            vals = {col: _to_million(rec.get(src)) for src, col in FIELD_MAP.items()}
            if not all(v is None for v in vals.values()):
                rows.append([code, pe, "Q"] + [vals[c] for c in _FIN_COLS])
        extra.append([code, fy, q, pe]
                     + [_cast(k, rec.get(s)) for _c, s, _t, k in EXTRA_COLS] + [now])
    filled = 0
    if rows:
        cols = ["code", "period_end", "period_type"] + _FIN_COLS
        bulk_upsert(cur, "financials", cols, rows, update_cols=_FIN_COLS, fill_only_cols=_FIN_COLS)
        filled = sum(1 for r in rows for v in r[3:] if v is not None)
    if extra:
        ecols = ["code", "fiscal_year", "quarter", "period_end"] + _EXTRA_NAMES + ["updated_at"]
        bulk_upsert(cur, "financials_edinet_quarterly", ecols, extra,
                    update_cols=["period_end"] + _EXTRA_NAMES + ["updated_at"])
    return filled, len(extra)


def backfill(daily_limit: int = DAILY_LIMIT, only_codes: list[str] | None = None,
             force: bool = False, verbose: bool = True) -> dict:
    """op欠損銘柄をEDINETで穴埋めする。戻り値に処理件数などのサマリを返す。"""
    if not KEY:
        raise RuntimeError("EDINETDB_API_KEY が未設定です(https://edinetdb.jp/developers で無料発行)")

    _fetch._stop = False
    conn = config.get_conn()
    cur = conn.cursor()
    _ensure_meta(cur)
    _ensure_extra_table(cur)
    normalized = normalize_zero_artifacts(cur)
    conn.commit()
    if verbose and normalized:
        print(f"op=0 疑似欠損を {normalized} 行 NULL化(穴埋め対象に追加)")

    targets = _targets(cur, daily_limit, only_codes, force)
    if verbose:
        na = sum(1 for _c, _e, k in targets if k == "A")
        print(f"対象 {len(targets)} タスク(年次{na}/四半期{len(targets)-na})"
              f" 優先: ①年次op欠損 →②四半期op欠損 →③未取得の付随データ")

    stats = {"fetched": 0, "filled_codes": 0, "filled_cells": 0,
             "rich_codes": 0, "rich_rows": 0, "no_data": 0, "stopped": False}
    now = _dt.datetime.now()

    for code, ec, kind in targets:
        period = "annual" if kind == "A" else "quarterly_standalone"
        try:
            recs = _fetch(ec, period)
        except QuotaExhausted:
            stats["stopped"] = True
            if verbose:
                print("  API残枠が尽きたため停止")
            break
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  [{code}/{ec}/{kind}] 取得失敗: {str(e)[:80]}")
            time.sleep(DELAY)
            continue

        stats["fetched"] += 1
        if kind == "A":
            filled, rich = _process_annual(cur, code, recs, now)
        else:
            filled, rich = _process_quarterly(cur, code, recs, now)

        if filled:
            stats["filled_codes"] += 1
            stats["filled_cells"] += filled
        if rich:
            stats["rich_codes"] += 1
            stats["rich_rows"] += rich
        else:
            stats["no_data"] += 1

        status = "filled" if filled else ("rich" if rich else "no_data")
        cur.execute(
            f"""
            INSERT INTO {META_TABLE[kind]} (code, edinet_code, status, filled_cells, last_fetched)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              edinet_code=VALUES(edinet_code), status=VALUES(status),
              filled_cells=VALUES(filled_cells), last_fetched=VALUES(last_fetched)
            """,
            (code, ec, status, filled, now),
        )
        conn.commit()
        if verbose:
            print(f"  [{code}] {ec} {kind}: 穴埋め{filled}セル / {'年次' if kind=='A' else '四半期'}{rich}件保存")

        if _fetch._stop:
            stats["stopped"] = True
            if verbose:
                print("  日次/月次残枠が閾値未満のため停止")
            break
        time.sleep(DELAY)

    cur.close()
    conn.close()
    if verbose:
        print(f"完了: 取得{stats['fetched']} / 穴埋め{stats['filled_codes']}銘柄{stats['filled_cells']}セル"
              f" / 年次保存{stats['rich_codes']}銘柄{stats['rich_rows']}件 / 該当なし{stats['no_data']}"
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

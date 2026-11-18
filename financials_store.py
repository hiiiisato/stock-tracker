"""財務実績の取得元保存と canonical `financials` への統合。

`financials` は既存の計算・表示モジュールが読む互換テーブルとして残す。
ただし取得処理はこれを直接「最後に書いた元が勝つ」形で更新しない。

優先順位（実績値の各セル単位）:
  TDnet決算短信 > 整合性検証済みEDINET有報の欠損補完 > Yahoo補助

EDINETの原文は公式だが、現在の取得APIの概念選択には連結/単体の取り違えが
実在する。そのため既存の非NULL値を一律上書きせず、同一年度の既存項目と
整合する行の欠損セルだけに使う。

初回マイグレーション:
  `python financials.py` または `python financials_tdnet.py` の次回実行時に
  CREATE TABLE IF NOT EXISTS で取得元テーブルを追加する。既存テーブルは変更しない。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from config import bulk_upsert


KEY_COLUMNS = ("code", "period_end", "period_type")
VALUE_COLUMNS = (
    "revenue", "gross_profit", "operating_income", "ordinary_income",
    "net_income", "total_assets", "total_equity", "total_debt", "cf_operating",
)
SOURCE_TABLES = {"financials_yahoo_source", "financials_tdnet_source"}


def ensure_source_tables(cur) -> None:
    """取得元別の原本テーブルを追加する。Webリクエストからは呼ばない。"""
    value_defs = ",\n            ".join(f"`{c}` BIGINT" for c in VALUE_COLUMNS)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS financials_yahoo_source (
            code        VARCHAR(10) NOT NULL,
            period_end  DATE NOT NULL,
            period_type VARCHAR(5) NOT NULL,
            {value_defs},
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_yahoo_fin (code, period_end, period_type)
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS financials_tdnet_source (
            code         VARCHAR(10) NOT NULL,
            period_end   DATE NOT NULL,
            period_type  VARCHAR(5) NOT NULL,
            announced_at DATETIME NOT NULL,
            {value_defs},
            ingested_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_tdnet_fin (code, period_end, period_type, announced_at)
        )
    """)


def save_source_rows(
    cur,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
) -> None:
    """取得元の値を原本テーブルへ保存する。"""
    if table not in SOURCE_TABLES:
        raise ValueError(f"unsupported financial source table: {table}")
    if not rows:
        return
    allowed = set(KEY_COLUMNS) | set(VALUE_COLUMNS) | {"announced_at"}
    if not set(columns) <= allowed or not set(KEY_COLUMNS) <= set(columns):
        raise ValueError("invalid financial source columns")
    update_cols = [c for c in columns if c not in (*KEY_COLUMNS, "announced_at")]
    bulk_upsert(cur, table, list(columns), rows, update_cols=update_cols)


def merge_canonical_rows(
    cur,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    *,
    prefer_incoming: bool,
    batch_size: int = 500,
) -> None:
    """
    `financials` へNULLを破壊的に書かずに統合する。

    prefer_incoming=False: Yahoo用。既存値を残し、NULLだけ埋める。
    prefer_incoming=True:  TDnet/EDINET用。取得値が非NULLのセルだけ更新する。
    """
    if not rows:
        return
    cols = list(columns)
    if not set(KEY_COLUMNS) <= set(cols):
        raise ValueError("canonical rows require code/period_end/period_type")
    value_cols = [c for c in cols if c not in KEY_COLUMNS]
    allowed = set(KEY_COLUMNS) | set(VALUE_COLUMNS)
    if not set(cols) <= allowed:
        raise ValueError("invalid canonical financial columns")

    col_sql = ",".join(f"`{c}`" for c in cols)
    row_ph = "(" + ",".join(["%s"] * len(cols)) + ")"
    if prefer_incoming:
        updates = ",".join(f"`{c}`=COALESCE(VALUES(`{c}`),`{c}`)" for c in value_cols)
    else:
        updates = ",".join(f"`{c}`=COALESCE(`{c}`,VALUES(`{c}`))" for c in value_cols)

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        values_sql = ",".join([row_ph] * len(batch))
        cur.execute(
            f"INSERT INTO financials ({col_sql}) VALUES {values_sql} "
            f"ON DUPLICATE KEY UPDATE {updates}",
            [v for row in batch for v in row],
        )


def _round_million(value):
    if value is None:
        return None
    return int(round(float(value) / 1_000_000)) * 1_000_000


def financial_rows_consistent(existing: Sequence, candidate: Sequence) -> bool:
    """同一年度の重複項目が同じ財務諸表を指すかを判定する。"""
    overlaps = [(float(old), float(new)) for old, new in zip(existing, candidate)
                if old is not None and new is not None]
    if not overlaps:
        return False
    relative_diffs = [
        abs(old - new) / max(abs(old), abs(new), 1_000_000.0)
        for old, new in overlaps
    ]
    # 丸め・軽微な分類差は許容するが、連結/単体の取り違えは通さない。
    return min(relative_diffs) <= 0.05 and max(relative_diffs) <= 0.25


def _reconcile_table(
    cur,
    table: str,
    period_type: str,
    codes: Iterable[str] | None,
    *,
    dry_run: bool,
) -> int:
    if table not in {"financials_edinet_annual", "financials_edinet_quarterly"}:
        raise ValueError("invalid EDINET table")
    params: list = []
    code_filter = ""
    code_list = list(dict.fromkeys(codes or ()))
    if code_list:
        ph = ",".join(["%s"] * len(code_list))
        code_filter = f" AND f.code IN ({ph})"
        params.extend(code_list)

    cur.execute(f"""
        SELECT f.code, f.period_end, f.period_type,
               f.revenue, f.gross_profit, f.operating_income, f.ordinary_income,
               f.net_income, f.total_assets, f.total_equity, f.cf_operating,
               e.revenue, e.gross_profit, e.operating_income, e.ordinary_income,
               e.net_income, e.total_assets,
               COALESCE(e.shareholders_equity, e.net_assets), e.cf_operating
        FROM financials f
        JOIN {table} e ON e.code=f.code AND e.period_end=f.period_end
        WHERE f.period_type=%s {code_filter}
    """, [period_type, *params])

    columns = [*KEY_COLUMNS, "revenue", "gross_profit", "operating_income",
               "ordinary_income", "net_income", "total_assets", "total_equity",
               "cf_operating"]
    rows: list[list] = []
    changed_cells = 0
    for record in cur.fetchall():
        existing = record[3:11]
        official = tuple(_round_million(v) for v in record[11:19])
        if not financial_rows_consistent(existing, official):
            continue
        changes = sum(1 for old, new in zip(existing, official)
                      if old is None and new is not None)
        if not changes:
            continue
        changed_cells += changes
        rows.append([record[0], record[1], record[2], *official])

    if not dry_run:
        merge_canonical_rows(cur, columns, rows, prefer_incoming=False)
    return changed_cells


def reconcile_edinet_priority(
    cur,
    codes: Iterable[str] | None = None,
    *,
    dry_run: bool = False,
) -> int:
    """既存項目と整合するEDINET年次原本から、canonicalの欠損だけを補完する。

    EDINET四半期は「単独3か月」と累計の概念対応を全銘柄で検証できていないため、
    この年次修復には混ぜない。
    """
    return _reconcile_table(
        cur, "financials_edinet_annual", "A", codes, dry_run=dry_run
    )


def edinet_gap_count(cur) -> int:
    """EDINETに値があるのにcanonicalがNULLのセル数。"""
    cur.execute("""
        SELECT
          SUM(f.revenue IS NULL AND e.revenue IS NOT NULL)
        + SUM(f.gross_profit IS NULL AND e.gross_profit IS NOT NULL)
        + SUM(f.operating_income IS NULL AND e.operating_income IS NOT NULL)
        + SUM(f.ordinary_income IS NULL AND e.ordinary_income IS NOT NULL)
        + SUM(f.net_income IS NULL AND e.net_income IS NOT NULL)
        + SUM(f.total_assets IS NULL AND e.total_assets IS NOT NULL)
        + SUM(f.total_equity IS NULL AND COALESCE(e.shareholders_equity,e.net_assets) IS NOT NULL)
        + SUM(f.cf_operating IS NULL AND e.cf_operating IS NOT NULL)
        FROM financials f
        JOIN financials_edinet_annual e
          ON e.code=f.code AND e.period_end=f.period_end
        WHERE f.period_type='A'
    """)
    row = cur.fetchone()
    return int((row and row[0]) or 0)

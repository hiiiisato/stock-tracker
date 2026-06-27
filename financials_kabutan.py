"""
kabutan.jp の決算ページから財務データを補完する。

取得内容:
- 年次(A): 売上高・営業益・純利益・総資産・自己資本・営業CF (直近4期)
- 四半期(Q): 売上高・営業益・純利益 (直近4期)

実行方法:
  python3 financials_kabutan.py               # 全銘柄(is_active)
  python3 financials_kabutan.py 7203 6758     # 指定銘柄のみ
  python3 financials_kabutan.py --force       # 取得済みも強制再取得
"""

import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from calendar import monthrange
from config import get_conn, bulk_upsert

UA    = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DELAY = 0.5   # リクエスト間隔（秒）
MAN   = 1_000_000  # 百万円 → 円


# ─── ユーティリティ ──────────────────────────────────────────────────────────────

def _to_yen(text: str):
    """百万円テキスト → 整数円。取得不能な場合は None。"""
    t = text.replace(",", "").replace("－", "").replace("—", "").strip()
    if not t or t == "-":
        return None
    try:
        return int(float(t)) * MAN
    except (ValueError, TypeError):
        return None


def _parse_period(raw: str):
    """
    "I2026.03"  → ("A", "2026-03-31")
    "I24.07-09" → ("Q", "2024-09-30")
    予測・前期比等 → (None, None)
    """
    s = re.sub(r'\s+', '', raw.replace('\xa0', ' ').strip())
    if '予' in s or '前期' in s or '前年' in s or not s:
        return None, None
    nums = re.sub(r'[^0-9.\-]', '', s)

    # 年次: 2026.03
    m = re.match(r'^(\d{4})\.(\d{1,2})$', nums)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        last_day = monthrange(year, month)[1]
        return 'A', f'{year:04d}-{month:02d}-{last_day:02d}'

    # 四半期: 24.07-09（3ヶ月のみ。6ヶ月の半期 04-09 等はスキップ）
    m = re.match(r'^(\d{2})\.(\d{2})-(\d{2})$', nums)
    if m:
        year       = 2000 + int(m.group(1))
        start_mon  = int(m.group(2))
        end_month  = int(m.group(3))
        span = (end_month - start_mon + 1) if end_month >= start_mon else (12 - start_mon + end_month + 1)
        if span != 3:  # 3ヶ月以外（半期など）はスキップ
            return None, None
        last_day = monthrange(year, end_month)[1]
        return 'Q', f'{year:04d}-{end_month:02d}-{last_day:02d}'

    return None, None


def _cells(row) -> list[str]:
    return [c.get_text(strip=True) for c in row.find_all(['th', 'td'])]


def _find_col(headers: list[str], *names: str, exclude: tuple = ()):
    """ヘッダーリストから名前に部分一致する列インデックスを返す。
    exact match を優先し、exclude に含まれる文字列を含む列は無視する。"""
    for name in names:
        # 完全一致を優先
        for i, h in enumerate(headers):
            if h == name and not any(ex in h for ex in exclude):
                return i
        # 部分一致
        for i, h in enumerate(headers):
            if name in h and not any(ex in h for ex in exclude):
                return i
    return None


# ─── テーブルパーサー ─────────────────────────────────────────────────────────────

def _parse_income_table(table, code: str) -> list[tuple]:
    rows = table.find_all('tr')
    if len(rows) < 2:
        return []
    headers = _cells(rows[0])

    col_period = _find_col(headers, '決算期')
    col_rev    = _find_col(headers, '売上高')
    col_oi     = _find_col(headers, '営業益')
    col_ni     = _find_col(headers, '最終益')
    if col_period is None or col_rev is None:
        return []

    max_col = max(c for c in [col_period, col_rev, col_oi, col_ni] if c is not None)
    results = []
    for row in rows[1:]:
        cells = _cells(row)
        if len(cells) <= max_col:
            continue
        ptype, period_end = _parse_period(cells[col_period])
        if not period_end:
            continue
        rev = _to_yen(cells[col_rev]) if col_rev is not None else None
        oi  = _to_yen(cells[col_oi])  if col_oi  is not None else None
        ni  = _to_yen(cells[col_ni])  if col_ni  is not None else None
        results.append((code, period_end, ptype, rev, oi, ni))
    return results


def _parse_bs_table(table) -> dict:
    """期末日 → (total_assets, total_equity)"""
    rows = table.find_all('tr')
    if len(rows) < 2:
        return {}
    headers = _cells(rows[0])

    col_period = _find_col(headers, '決算期')
    col_assets = _find_col(headers, '総資産', exclude=('回転',))
    col_equity = _find_col(headers, '自己資本', exclude=('比率', '回転'))
    if col_period is None:
        return {}

    result = {}
    for row in rows[1:]:
        cells = _cells(row)
        if not cells:
            continue
        ptype, period_end = _parse_period(cells[col_period])
        if not period_end or ptype != 'A':
            continue
        assets = _to_yen(cells[col_assets]) if col_assets is not None and len(cells) > col_assets else None
        equity = _to_yen(cells[col_equity]) if col_equity is not None and len(cells) > col_equity else None
        result[period_end] = (assets, equity)
    return result


def _parse_cf_table(table) -> dict:
    """期末日 → cf_operating"""
    rows = table.find_all('tr')
    if len(rows) < 2:
        return {}
    headers = _cells(rows[0])

    col_period = _find_col(headers, '決算期')
    col_cf     = _find_col(headers, '営業CF', '営業キャッシュ')
    if col_period is None or col_cf is None:
        return {}

    result = {}
    for row in rows[1:]:
        cells = _cells(row)
        if not cells or len(cells) <= col_cf:
            continue
        ptype, period_end = _parse_period(cells[col_period])
        if not period_end or ptype != 'A':
            continue
        result[period_end] = _to_yen(cells[col_cf])
    return result


# ─── メインスクレイパー ───────────────────────────────────────────────────────────

def scrape_one(code4: str) -> list[tuple]:
    """1銘柄 → UPSERT用rowsのリスト。失敗時は空リスト。"""
    try:
        time.sleep(DELAY)
        r = requests.get(
            f"https://kabutan.jp/stock/finance?code={code4}",
            headers={"User-Agent": UA},
            timeout=15,
        )
        if r.status_code != 200:
            return []

        soup   = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table')

        income_rows: dict = {}  # (period_end, ptype) → (rev, oi, ni)
        bs_data:     dict = {}  # period_end → (assets, equity)
        cf_data:     dict = {}  # period_end → cf

        for t in tables:
            header_text = ' '.join(_cells(t.find('tr') or t))

            if '総資産' in header_text and '自己資本' in header_text:
                bs_data.update(_parse_bs_table(t))
            elif '営業CF' in header_text or '営業キャッシュ' in header_text:
                cf_data.update(_parse_cf_table(t))
            elif '売上高' in header_text and '営業益' in header_text and '最終益' in header_text:
                for _, period_end, ptype, rev, oi, ni in _parse_income_table(t, code4):
                    key = (period_end, ptype)
                    if key not in income_rows:  # 先に出たテーブルを優先
                        income_rows[key] = (rev, oi, ni)

        rows = []
        for (period_end, ptype), (rev, oi, ni) in income_rows.items():
            assets, equity = bs_data.get(period_end, (None, None)) if ptype == 'A' else (None, None)
            cf = cf_data.get(period_end) if ptype == 'A' else None
            rows.append((
                code4, period_end, ptype,
                rev,   # revenue
                None,  # gross_profit (kabutan は非掲載)
                oi,    # operating_income
                ni,    # net_income
                assets,  # total_assets
                equity,  # total_equity
                None,  # total_debt
                cf,    # cf_operating
            ))
        return rows

    except Exception as e:
        print(f"    [ERROR] {code4}: {e}")
        return []


# ─── バルク保存 ──────────────────────────────────────────────────────────────────

def _save(rows: list[tuple]):
    conn = get_conn()
    cur  = conn.cursor()
    bulk_upsert(cur, 'financials',
        ['code', 'period_end', 'period_type',
         'revenue', 'gross_profit', 'operating_income', 'net_income',
         'total_assets', 'total_equity', 'total_debt', 'cf_operating'],
        rows,
        update_cols=['revenue', 'gross_profit', 'operating_income', 'net_income',
                     'total_assets', 'total_equity', 'total_debt', 'cf_operating'])
    conn.commit()
    cur.close()
    conn.close()


# ─── エントリーポイント ───────────────────────────────────────────────────────────

def run(target_codes=None, force=False):
    conn = get_conn()
    cur  = conn.cursor()

    if target_codes:
        ph = ','.join(['%s'] * len(target_codes))
        cur.execute(f"SELECT code FROM stocks WHERE code IN ({ph})", target_codes)
    else:
        if force:
            cur.execute("SELECT code FROM stocks WHERE is_active=TRUE ORDER BY code")
        else:
            # operating_income が NULL（未取得）の銘柄 or 全く records がない銘柄を優先
            cur.execute("""
                SELECT DISTINCT s.code
                FROM stocks s
                LEFT JOIN (
                    SELECT code, MAX(CASE WHEN operating_income IS NOT NULL THEN 1 ELSE 0 END) as has_oi
                    FROM financials GROUP BY code
                ) f ON f.code = s.code
                WHERE s.is_active = TRUE
                  AND (f.code IS NULL OR f.has_oi = 0)
                ORDER BY s.code
            """)
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    total = len(codes)
    print(f"=== kabutan 財務データ補完 ===")
    print(f"対象: {total} 銘柄")

    ok = fail = 0
    batch = []
    start = time.time()

    for i, code in enumerate(codes, 1):
        rows = scrape_one(code)
        if rows:
            batch.extend(rows)
            ok += 1
        else:
            fail += 1

        if len(batch) >= 500 or i == total:
            if batch:
                _save(batch)
                batch = []

        if i % 50 == 0 or i == total or i == 1:
            elapsed = time.time() - start
            remain  = (elapsed / i * (total - i)) / 60 if i < total else 0
            print(f"  [{i:>4}/{total}]  OK:{ok}  失敗:{fail}  残り約{remain:.0f}分")

    print(f"\n完了: {ok}/{total} 銘柄取得成功")


if __name__ == '__main__':
    args         = sys.argv[1:]
    force        = '--force' in args
    target_codes = [a for a in args if not a.startswith('--')] or None
    run(target_codes=target_codes, force=force)

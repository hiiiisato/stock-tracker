"""
kabutan.jp の決算ページから財務データを補完する。

取得内容:
- 年次(A): 売上高・営業益・経常益・純利益・総資産・自己資本・営業CF (直近4期)
- 四半期(Q): 売上高・営業益・経常益・純利益 (直近4期)
- 通期予想(financials_forecast): 売上・営業益・経常益・最終益・配当
- 上期予想(financials_forecast): 売上・営業益・経常益・最終益・配当

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
    if not t or t in ("-", "－", "—"):
        return None
    try:
        return int(float(t)) * MAN
    except (ValueError, TypeError):
        return None


def _to_yen_raw(text: str):
    """百万円テキスト → 整数円（単純変換、－は None）。"""
    return _to_yen(text)


def _to_date(text: str):
    """'26/02/13' → '2026-02-13'。失敗時は None。"""
    t = text.strip()
    m = re.match(r'^(\d{2})/(\d{2})/(\d{2})$', t)
    if m:
        return f'20{m.group(1)}-{m.group(2)}-{m.group(3)}'
    return None


def _parse_period(raw: str):
    """
    "I2026.03"  → ("A", "2026-03-31")
    "I24.07-09" → ("Q", "2024-09-30")
    "予2026.12" → ("forecast_A", "2026-12-31")
    "予26.01-06"→ ("forecast_H", "2026-06-30")
    予測・前期比等 → (None, None)
    """
    s = re.sub(r'\s+', '', raw.replace('\xa0', ' ').strip())
    if '前期' in s or '前年' in s or '前年同期' in s or not s:
        return None, None

    is_forecast = s.startswith('予')
    s_clean = s.lstrip('予')

    nums = re.sub(r'[^0-9.\-]', '', s_clean)

    # 年次: 2026.03 or 2026.12
    m = re.match(r'^(\d{4})\.(\d{1,2})$', nums)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        last_day = monthrange(year, month)[1]
        period_end = f'{year:04d}-{month:02d}-{last_day:02d}'
        ptype = 'forecast_A' if is_forecast else 'A'
        return ptype, period_end

    # 四半期/上期: 24.07-09（3ヶ月 or 6ヶ月）
    m = re.match(r'^(\d{2})\.(\d{2})-(\d{2})$', nums)
    if m:
        year       = 2000 + int(m.group(1))
        start_mon  = int(m.group(2))
        end_month  = int(m.group(3))
        span = (end_month - start_mon + 1) if end_month >= start_mon else (12 - start_mon + end_month + 1)
        last_day = monthrange(year, end_month)[1]
        period_end = f'{year:04d}-{end_month:02d}-{last_day:02d}'
        if is_forecast:
            ptype = 'forecast_H' if span == 6 else 'forecast_Q'
            return ptype, period_end
        else:
            if span != 3:  # 実績は3ヶ月のみ
                return None, None
            return 'Q', period_end

    return None, None


def _cells(row) -> list[str]:
    return [c.get_text(strip=True) for c in row.find_all(['th', 'td'])]


def _find_col(headers: list[str], *names: str, exclude: tuple = ()):
    """ヘッダーリストから名前に部分一致する列インデックスを返す。"""
    for name in names:
        for i, h in enumerate(headers):
            if h == name and not any(ex in h for ex in exclude):
                return i
        for i, h in enumerate(headers):
            if name in h and not any(ex in h for ex in exclude):
                return i
    return None


# ─── テーブルパーサー ─────────────────────────────────────────────────────────────

def _parse_income_table(table, code: str):
    """
    年次・四半期の実績行と予想行を両方パースして返す。
    returns:
      actuals  : list of (code, period_end, period_type, rev, oi, ordinary_i, ni)
      forecasts: list of (code, fiscal_year_end, period_type, rev, oi, ordinary_i, ni, div, announced_at)
    """
    rows = table.find_all('tr')
    if len(rows) < 2:
        return [], []
    headers = _cells(rows[0])

    col_period  = _find_col(headers, '決算期')
    col_rev     = _find_col(headers, '売上高')
    col_oi      = _find_col(headers, '営業益')
    col_ordinary= _find_col(headers, '経常益')
    col_ni      = _find_col(headers, '最終益')
    col_div     = _find_col(headers, '修正1株配')
    col_date    = _find_col(headers, '発表日')
    if col_period is None or col_rev is None:
        return [], []

    max_col = max(c for c in [col_period, col_rev, col_oi, col_ordinary, col_ni, col_div, col_date] if c is not None)
    actuals  = []
    forecasts= []

    for row in rows[1:]:
        cells = _cells(row)
        if len(cells) <= max_col:
            continue
        ptype, period_end = _parse_period(cells[col_period])
        if not period_end:
            continue

        rev      = _to_yen(cells[col_rev])       if col_rev      is not None else None
        oi       = _to_yen(cells[col_oi])        if col_oi       is not None else None
        ordinary = _to_yen(cells[col_ordinary])  if col_ordinary is not None else None
        ni       = _to_yen(cells[col_ni])        if col_ni       is not None else None
        div_raw  = cells[col_div].replace(',','').strip() if col_div is not None else None
        try:
            div = float(div_raw) if div_raw and div_raw not in ('-','－','—','') else None
        except (ValueError, TypeError):
            div = None
        announced = _to_date(cells[col_date]) if col_date is not None else None

        if ptype in ('forecast_A', 'forecast_H', 'forecast_Q'):
            # 通期・上期・四半期予想 → financials_forecast へ
            forecasts.append((code, period_end, ptype.replace('forecast_',''),
                               rev, oi, ordinary, ni, div, announced))
        elif ptype in ('A', 'Q'):
            actuals.append((code, period_end, ptype, rev, oi, ordinary, ni))

    return actuals, forecasts


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

def scrape_one(code4: str):
    """1銘柄 → (actuals_rows, forecast_rows)。失敗時は ([], [])。"""
    try:
        time.sleep(DELAY)
        r = requests.get(
            f"https://kabutan.jp/stock/finance?code={code4}",
            headers={"User-Agent": UA},
            timeout=15,
        )
        if r.status_code != 200:
            return [], []

        soup   = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table')

        income_actuals: dict  = {}  # (period_end, ptype) → (rev, oi, ordinary, ni)
        income_forecasts: dict= {}  # (fiscal_year_end, ptype) → (rev, oi, ordinary, ni, div, announced)
        bs_data:   dict = {}
        cf_data:   dict = {}

        for t in tables:
            header_text = ' '.join(_cells(t.find('tr') or t))

            if '総資産' in header_text and '自己資本' in header_text and '営業益' not in header_text:
                bs_data.update(_parse_bs_table(t))
            elif '営業CF' in header_text or '営業キャッシュ' in header_text:
                cf_data.update(_parse_cf_table(t))
            elif '売上高' in header_text and ('営業益' in header_text or '最終益' in header_text):
                acts, fcs = _parse_income_table(t, code4)
                for code, period_end, ptype, rev, oi, ordinary, ni in acts:
                    key = (period_end, ptype)
                    if key not in income_actuals:
                        income_actuals[key] = (rev, oi, ordinary, ni)
                for code, fiscal_end, ptype, rev, oi, ordinary, ni, div, announced in fcs:
                    key = (fiscal_end, ptype)
                    if key not in income_forecasts:
                        income_forecasts[key] = (rev, oi, ordinary, ni, div, announced)

        # 実績行を組み立て
        actual_rows = []
        for (period_end, ptype), (rev, oi, ordinary, ni) in income_actuals.items():
            assets, equity = bs_data.get(period_end, (None, None)) if ptype == 'A' else (None, None)
            cf = cf_data.get(period_end) if ptype == 'A' else None
            actual_rows.append((
                code4, period_end, ptype,
                rev, None,  # gross_profit は kabutan 非掲載
                oi, ordinary, ni,
                assets, equity, None, cf,  # total_debt は非掲載
            ))

        # 予想行を組み立て
        forecast_rows = []
        for (fiscal_end, ptype), (rev, oi, ordinary, ni, div, announced) in income_forecasts.items():
            forecast_rows.append((
                code4, fiscal_end, ptype,
                rev, oi, ordinary, ni, div, announced,
            ))

        return actual_rows, forecast_rows

    except Exception as e:
        print(f"    [ERROR] {code4}: {e}")
        return [], []


# ─── バルク保存 ──────────────────────────────────────────────────────────────────

def _save_actuals(rows: list):
    if not rows:
        return
    conn = get_conn()
    cur  = conn.cursor()
    bulk_upsert(cur, 'financials',
        ['code', 'period_end', 'period_type',
         'revenue', 'gross_profit', 'operating_income', 'ordinary_income', 'net_income',
         'total_assets', 'total_equity', 'total_debt', 'cf_operating'],
        rows,
        update_cols=['revenue', 'gross_profit', 'operating_income', 'ordinary_income', 'net_income',
                     'total_assets', 'total_equity', 'total_debt', 'cf_operating'])
    conn.commit()
    cur.close()
    conn.close()


def _save_forecasts(rows: list):
    """予想値は INSERT IGNORE で追加のみ。announced_at が変わった修正は新レコードとして保持。"""
    if not rows:
        return
    from datetime import date as _date
    today = str(_date.today())
    # announced_at が None の場合はスクレイプ日で代替
    fixed = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8] if r[8] else today)
             for r in rows]
    conn = get_conn()
    cur  = conn.cursor()
    sql = """
        INSERT IGNORE INTO financials_forecast
            (code, fiscal_year_end, period_type,
             revenue, operating_income, ordinary_income, net_income,
             div_per_share, announced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    cur.executemany(sql, fixed)
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
    batch_act = []
    batch_fc  = []
    start = time.time()

    for i, code in enumerate(codes, 1):
        acts, fcs = scrape_one(code)
        if acts or fcs:
            batch_act.extend(acts)
            batch_fc.extend(fcs)
            ok += 1
        else:
            fail += 1

        if len(batch_act) >= 500 or i == total:
            _save_actuals(batch_act)
            _save_forecasts(batch_fc)
            batch_act = []
            batch_fc  = []

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

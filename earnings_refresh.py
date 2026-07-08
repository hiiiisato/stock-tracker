"""
決算・業績修正のタイムリー反映 — 適時開示を検知した銘柄だけ当日中に業績データを更新する。

背景:
  業績（financials / financials_forecast）の全銘柄更新は週次（月曜）のため、
  決算発表・業績修正がDBに反映されるまで最大1週間かかっていた。
  一方、適時開示（disclosures）は毎日16時台+20:30に蓄積されており、
  kabutan は発表当日中に新しい予想値を掲載する（発表日カラム付き）。

仕組み:
  1. 当日の適時開示から決算・業績修正・配当修正の銘柄コードを抽出
  2. その銘柄だけ financials_kabutan.scrape_one で再取得
     → financials（実績・四半期）更新、financials_forecast に新announced_atの行が追加される
     （financials_forecast は UNIQUE(code, fiscal_year_end, period_type, announced_at) で
       修正履歴がそのまま蓄積される既存設計を利用）
  3. 同一決算期に対する直近2つの予想を比較し、修正幅を forecast_revisions に保存
     → 日次レポート・銘柄ページで「上方修正 営業益+20%」のように定量表示できる

実行: daily_run.py のメイン便・イブニング便から自動実行。
  python3 earnings_refresh.py               # 当日の開示銘柄を反映
  python3 earnings_refresh.py 2026-07-08    # 日付指定（過去日の取りこぼし補完）
  python3 earnings_refresh.py --detect-only # 再取得せず修正検知だけやり直す
"""
import sys
import time
from datetime import date, datetime, timedelta
from config import get_conn

# 業績・配当に関わる開示カテゴリ（disclosures.classify_title のカテゴリ体系）
EARNINGS_CATEGORIES = (
    "earnings_report",   # 決算短信
    "earnings_up", "earnings_down", "earnings_rev",   # 業績修正
    "div_up", "div_down", "dividend_rev",             # 配当修正
    "guidance",          # 業績見通し
)
MAX_CODES_PER_RUN = 600   # 決算集中日でも1回の実行が長くなりすぎないよう上限（残りは翌日/週次が拾う）


def ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS forecast_revisions (
            code            VARCHAR(10) NOT NULL,
            fiscal_year_end DATE        NOT NULL,
            period_type     VARCHAR(2)  NOT NULL,   -- A=通期 / H=上期
            announced_at    DATE        NOT NULL,   -- 新予想の発表日
            prev_announced_at DATE,
            revenue_old BIGINT, revenue_new BIGINT, revenue_chg_pct DOUBLE,
            op_old      BIGINT, op_new      BIGINT, op_chg_pct      DOUBLE,
            ord_old     BIGINT, ord_new     BIGINT, ord_chg_pct     DOUBLE,
            net_old     BIGINT, net_new     BIGINT, net_chg_pct     DOUBLE,
            dps_old DECIMAL(10,2), dps_new DECIMAL(10,2),
            direction   TINYINT,       -- 1=上方 / -1=下方 / 0=中立・混在
            is_turnaround TINYINT,     -- 1=黒字転換（利益が赤字→黒字）
            created_at  DATETIME,
            PRIMARY KEY (code, fiscal_year_end, period_type, announced_at)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def _disclosed_codes(cur, target_date: date) -> list[str]:
    """指定日に業績関連の開示を出した銘柄コード。"""
    ph = ",".join(["%s"] * len(EARNINGS_CATEGORIES))
    cur.execute(f"""
        SELECT DISTINCT d.code
        FROM disclosures d
        JOIN stocks s ON s.code = d.code AND s.is_active = 1
        WHERE DATE(d.disclosed_at) = %s AND d.category IN ({ph})
    """, (target_date, *EARNINGS_CATEGORIES))
    return [r[0] for r in cur.fetchall()]


def _chg_pct(old, new):
    """予想値の変化率(%)。旧値がゼロ以下（赤字・ゼロ）の場合は率が無意味なのでNone。"""
    if old is None or new is None or old <= 0:
        return None
    return round((float(new) / float(old) - 1) * 100, 1)


def detect_revisions(days_back: int = 7) -> int:
    """
    financials_forecast の同一(code, 決算期, 期区分)の直近2予想を比較し、
    直近days_back日以内に発表された修正を forecast_revisions に保存する。
    冪等（再実行しても同じ結果）。戻り値=保存件数。
    """
    ensure_table()
    conn = get_conn()
    cur  = conn.cursor()
    since = date.today() - timedelta(days=days_back)

    # 直近days_backに新しい予想が入った (code, fiscal_year_end, period_type) を対象に、
    # 新旧2行を取得して比較する
    cur.execute("""
        SELECT f.code, f.fiscal_year_end, f.period_type, f.announced_at,
               f.revenue, f.operating_income, f.ordinary_income, f.net_income, f.div_per_share
        FROM financials_forecast f
        JOIN (
            SELECT code, fiscal_year_end, period_type
            FROM financials_forecast
            WHERE announced_at >= %s
            GROUP BY code, fiscal_year_end, period_type
        ) t ON t.code = f.code AND t.fiscal_year_end = f.fiscal_year_end
           AND t.period_type = f.period_type
        ORDER BY f.code, f.fiscal_year_end, f.period_type, f.announced_at DESC
    """, (since,))
    rows = cur.fetchall()

    # (code, fy, pt) ごとに announced_at 降順で並んでいる → 先頭2つが新旧
    from collections import defaultdict
    grouped: dict = defaultdict(list)
    for r in rows:
        grouped[(r[0], str(r[1]), r[2])].append(r)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    upserts = []
    for (code, fy, pt), hist in grouped.items():
        if len(hist) < 2:
            continue   # 初出の予想（新年度ガイダンス等）は「修正」ではない
        new, old = hist[0], hist[1]
        if new[3] < since:
            continue   # 最新予想が対象期間外
        n_rev, n_op, n_ord, n_net, n_dps = new[4], new[5], new[6], new[7], new[8]
        o_rev, o_op, o_ord, o_net, o_dps = old[4], old[5], old[6], old[7], old[8]

        # 方向判定: 営業益 → 経常益 → 純利益 の優先順で新旧比較
        direction = 0
        is_turnaround = 0
        for n_v, o_v in ((n_op, o_op), (n_ord, o_ord), (n_net, o_net)):
            if n_v is not None and o_v is not None and n_v != o_v:
                direction = 1 if n_v > o_v else -1
                if o_v < 0 <= n_v:
                    is_turnaround = 1
                break
        if direction == 0 and n_rev is not None and o_rev is not None and n_rev != o_rev:
            direction = 1 if n_rev > o_rev else -1
        if direction == 0 and n_dps is not None and o_dps is not None and float(n_dps) != float(o_dps):
            direction = 1 if float(n_dps) > float(o_dps) else -1
        if direction == 0:
            continue   # 数値の変化なし（同日再掲等）

        upserts.append((
            code, fy, pt, new[3], old[3],
            o_rev, n_rev, _chg_pct(o_rev, n_rev),
            o_op,  n_op,  _chg_pct(o_op,  n_op),
            o_ord, n_ord, _chg_pct(o_ord, n_ord),
            o_net, n_net, _chg_pct(o_net, n_net),
            o_dps, n_dps,
            direction, is_turnaround, now,
        ))

    if upserts:
        cur.executemany("""
            INSERT INTO forecast_revisions
                (code, fiscal_year_end, period_type, announced_at, prev_announced_at,
                 revenue_old, revenue_new, revenue_chg_pct,
                 op_old, op_new, op_chg_pct,
                 ord_old, ord_new, ord_chg_pct,
                 net_old, net_new, net_chg_pct,
                 dps_old, dps_new, direction, is_turnaround, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                prev_announced_at=VALUES(prev_announced_at),
                revenue_old=VALUES(revenue_old), revenue_new=VALUES(revenue_new), revenue_chg_pct=VALUES(revenue_chg_pct),
                op_old=VALUES(op_old), op_new=VALUES(op_new), op_chg_pct=VALUES(op_chg_pct),
                ord_old=VALUES(ord_old), ord_new=VALUES(ord_new), ord_chg_pct=VALUES(ord_chg_pct),
                net_old=VALUES(net_old), net_new=VALUES(net_new), net_chg_pct=VALUES(net_chg_pct),
                dps_old=VALUES(dps_old), dps_new=VALUES(dps_new),
                direction=VALUES(direction), is_turnaround=VALUES(is_turnaround)
        """, upserts)
        conn.commit()
    cur.close()
    conn.close()
    print(f"  業績修正検知: {len(upserts)} 件を forecast_revisions に保存")
    return len(upserts)


def refresh_from_disclosures(target_date: date | None = None) -> dict:
    """当日の業績関連開示の銘柄だけ kabutan から再取得し、修正を検知する。"""
    ensure_table()
    target = target_date or date.today()
    conn = get_conn()
    cur  = conn.cursor()
    codes = _disclosed_codes(cur, target)
    cur.close()
    conn.close()

    if not codes:
        print(f"  {target} の業績関連開示なし")
        return {"codes": 0, "revisions": 0}
    if len(codes) > MAX_CODES_PER_RUN:
        print(f"  対象 {len(codes)} 銘柄 → 上限 {MAX_CODES_PER_RUN} 件に制限（残りは翌日・週次で反映）")
        codes = codes[:MAX_CODES_PER_RUN]

    print(f"  {target} の業績関連開示: {len(codes)} 銘柄を再取得...")
    from financials_kabutan import run as kabutan_run
    kabutan_run(target_codes=codes)

    n_rev = detect_revisions(days_back=7)
    return {"codes": len(codes), "revisions": n_rev}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--detect-only" in args:
        detect_revisions(days_back=7)
    else:
        d = None
        for a in args:
            if not a.startswith("--"):
                d = datetime.strptime(a, "%Y-%m-%d").date()
        refresh_from_disclosures(d)

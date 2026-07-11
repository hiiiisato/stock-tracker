"""
株式分割・併合の公式対応（J-Quants ベース）
============================================
価格の急変ヒューリスティックを廃止し、JPX公式データ（J-Quants）で対応する。

背景（重要）:
  本番の生データ源 Yahoo は、daily_prices.close に「生値」を入れる銘柄と
  「分割調整済み値」を入れる銘柄が混在しており信頼できない（例: 9984 の
  2025-12-26 は Yahoo close=4450 だが真の生値は 17800）。
  一方 J-Quants は C(真の生値) と AdjC(公式の分割調整済み) を正確に返す。

方針:
  1. adj_close は J-Quants の AdjC をそのまま採用（公式・分割のみ調整・配当は含めない）。
     Yahoo の壊れた close には一切依存しない。
  2. 分割イベント（AdjFactor≠1.0＝除権日）を stock_splits に公式ソースとして記録。
  3. J-Quants 無料枠は約12週遅延のため、その先（直近窓）は:
       - 分割が無い銘柄 → adj_close = 生 close（Yahoo, 分割のみなので生値でよい）
       - 直近窓で新規分割があった銘柄 → Yahoo の splits で暫定検知し、除権日より前の
         全 adj_close に価格乗数を掛ける（J-Quants が追いつけば公式値で上書き）。

実行:
  python splits.py --backfill      # 初回移行（stock_splits再構築 + 全 adj_close 再計算）
  python splits.py --daily         # 日次: 直近窓の新規分割をYahoo検知し該当銘柄のみ再計算
"""

from __future__ import annotations
import sys
import time
import requests
from datetime import date, datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import get_conn, bulk_upsert, JQUANTS_BASE_URL, JQUANTS_HEADERS

YAHOO_API = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}
BACKFILL_FROM = "20240101"
# 実在する分割/併合の価格乗数はこの範囲に収まる（1:100分割〜100:1併合を余裕を持ってカバー）。
# Yahoo の splits イベントには "1:658353" のような明らかなデータ異常が混入することが
# 確認されたため、この範囲外は分割イベントとして採用しない。
RATIO_MIN, RATIO_MAX = 0.005, 200.0


# ─────────────────────────────────────────────────────────────────────────────
# テーブル
# ─────────────────────────────────────────────────────────────────────────────

def ensure_splits_table():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_splits (
            code         VARCHAR(10)   NOT NULL,
            ex_date      DATE          NOT NULL,
            split_ratio  DECIMAL(12,6) NOT NULL COMMENT '価格乗数(除権日前の価格に掛ける). 1:4分割=0.25, 10:1併合=10.0',
            detected_at  DATETIME      DEFAULT CURRENT_TIMESTAMP,
            source       VARCHAR(20)   DEFAULT 'jquants',
            PRIMARY KEY (code, ex_date)
        )
    """)
    conn.commit(); cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# J-Quants 取得
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_jquants_bars(code5: str, date_from: str, date_to: str) -> list[dict]:
    for attempt in range(3):
        try:
            r = requests.get(f"{JQUANTS_BASE_URL}/equities/bars/daily",
                headers=JQUANTS_HEADERS,
                params={"code": code5, "date_from": date_from, "date_to": date_to},
                timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            if r.status_code != 200:
                return []
            return r.json().get("data", [])
        except Exception:
            time.sleep(1)
    return []


def _fetch_jquants_one(code4: str, date_from: str, date_to: str):
    """1銘柄の (adj_rows, split_rows, max_date) を返す。
    adj_rows: [(code, date, AdjC)]、split_rows: [(code, ex_date, AdjFactor)]。"""
    data = _fetch_jquants_bars(code4 + "0", date_from, date_to)
    adj_rows, split_rows = [], []
    max_date = None
    for d in data:
        dt = d.get("Date"); adjc = d.get("AdjC"); af = d.get("AdjFactor")
        if dt is None:
            continue
        if max_date is None or dt > max_date:
            max_date = dt
        if adjc is not None:
            adj_rows.append((code4, dt, round(float(adjc), 4)))
        if af is not None and abs(float(af) - 1.0) > 1e-6:
            split_rows.append((code4, dt, round(float(af), 6)))
    return adj_rows, split_rows, max_date


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo 暫定（直近窓の新規分割のみ）
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_yahoo_splits(code4: str, since: date) -> list[tuple[str, float]]:
    """Yahoo splits を (ex_date, 価格乗数=denominator/numerator) で返す。"""
    p1 = int(datetime.combine(since, datetime.min.time()).timestamp())
    p2 = int(datetime.now().timestamp()) + 86400
    for attempt in range(3):
        try:
            r = requests.get(YAHOO_API.format(ticker=f"{code4}.T"),
                params={"interval": "1d", "period1": p1, "period2": p2, "events": "split"},
                headers=YAHOO_HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** attempt + 2); continue
            if r.status_code != 200:
                return []
            result = r.json().get("chart", {}).get("result")
            if not result:
                return []
            out = []
            for _, s in result[0].get("events", {}).get("splits", {}).items():
                num = s.get("numerator", 1) or 1
                den = s.get("denominator", 1) or 1
                ex_dt = datetime.fromtimestamp(s["date"]).date()
                if ex_dt < since or num <= 0 or den <= 0:
                    continue
                ratio = den / num
                if not (RATIO_MIN <= ratio <= RATIO_MAX):
                    print(f"    [警告] {code4}: 異常な分割比率を除外 ex_date={ex_dt} "
                          f"numerator={num} denominator={den} ratio={ratio}")
                    continue
                out.append((str(ex_dt), round(ratio, 6)))
            return out
        except Exception:
            time.sleep(1)
    return []


def sync_yahoo_provisional_splits(codes4: list[str], since: date,
                                  existing: set, max_workers: int = 6) -> list[tuple]:
    """直近窓の新規分割を Yahoo から取得。既存(existing)に無いものだけ返す。"""
    def fetch(code4):
        time.sleep(0.03)
        return code4, _fetch_yahoo_splits(code4, since)
    new_rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(fetch, c): c for c in codes4}):
            code4, splits = fut.result()
            for ex_date, ratio in splits:
                if (code4, ex_date) not in existing:
                    new_rows.append((code4, ex_date, ratio, "yahoo"))
    return new_rows


# ─────────────────────────────────────────────────────────────────────────────
# adj_close 再構築（J-Quants AdjC 主軸）
# ─────────────────────────────────────────────────────────────────────────────

def _bulk_upsert_adj(rows: list[tuple]) -> int:
    """rows=[(code,date,adj_close,adj_factor)] を daily_prices へ一括UPSERT。"""
    if not rows:
        return 0
    conn = get_conn(); cur = conn.cursor()
    BATCH = 500; RECONNECT_AT = 200; batches = 0; done = 0
    for start in range(0, len(rows), BATCH):
        if batches and batches % RECONNECT_AT == 0:
            conn.commit(); cur.close(); conn.close()
            conn = get_conn(); cur = conn.cursor()
        batch = rows[start:start + BATCH]
        ph = ",".join(["(%s,%s,%s,%s)"] * len(batch))
        cur.execute(f"""INSERT INTO daily_prices (code, date, adj_close, adj_factor)
                        VALUES {ph}
                        ON DUPLICATE KEY UPDATE adj_close=VALUES(adj_close), adj_factor=VALUES(adj_factor)""",
                    [v for row in batch for v in row])
        conn.commit(); batches += 1; done += len(batch)
        if done % 100000 < BATCH:
            print(f"    adj_close 書込: {done}/{len(rows)}件")
    cur.close(); conn.close()
    return done


def _verify_split_reflected(series: list[tuple], ex_date: str, ratio: float) -> bool:
    """Yahoo の close 系列(dateでソート済み, [(date_str, close), ...])を見て、
    ex_date の除権が「生 close にそのまま反映されている(=段差がある)」か検証する。

    Yahoo の close は銘柄によって既に分割調整済みの値を返すことがあり
    （例: 9984 は分割前後で close が連続していた）、その場合に比率を追加で
    掛けると二重調整になる。実際の価格変化と期待比率(ratio)を突き合わせ、
    段差が確認できた場合のみ True（＝この分割は close に未反映＝要調整）を返す。
    """
    before = after = None
    for dt, close in series:
        if dt < ex_date:
            before = close
        elif dt >= ex_date and after is None:
            after = close
            break
    if before is None or after is None or before <= 0:
        return False
    actual = after / before
    expected = ratio
    if expected <= 0:
        return False
    # 比率が1に近いイベント（±25%程度）は通常の値動きと区別できず検証不能のため
    # 採用しない（誤適用の影響も小さい。公式ソースの反映を待つ）。
    if 0.75 < expected < 1.333:
        return False
    rel = actual / expected
    # 実際の変化率が期待比率の 0.7〜1.4 倍に収まる場合のみ「反映されている」とみなす。
    # 上限1.4が重要: 1:2分割(ratio=0.5)が close に未反映だと rel≈2.0 になるため、
    # 旧上限2.0では素通りして二重調整を起こしていた（8031三井物産などで実害）。
    # 下限0.7も同様に 2:1併合(ratio=2.0) の未反映(rel≈0.5)を弾く。
    return 0.7 <= rel <= 1.4


def rebuild(codes4: list[str] | None = None, full: bool = True, use_jquants: bool = True):
    """adj_close を再構築し、stock_splits を更新する。

    - use_jquants=True: J-Quants の AdjC を主軸に使う（公式・最も正確）。
      J-Quants 無料枠は約12週遅延のため、その先は Yahoo の splits で暫定補完。
    - use_jquants=False: J-Quants API が利用できない時の代替経路。
      Yahoo の splits イベント（妥当性チェック済み）を全期間から集め、
      各イベントについて実際に close に段差が出ているか検証してから
      adj_close = close × Π(検証OKだった分割比率) を計算する。
      Yahoo の close は銘柄によって既に調整済みのことがあるため、
      検証をスキップして機械的に掛けると二重調整になり危険。
    """
    ensure_splits_table()

    explicit_codes = codes4 is not None  # 呼び出し元が明示的に対象銘柄を指定したか
    if codes4 is None:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
        codes4 = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()

    date_to = date.today().strftime("%Y%m%d")
    jq_adjc: dict = {}
    all_jq_splits: list = []
    jq_max_global = ""

    if use_jquants:
        print(f"  [J-Quants] {len(codes4)}銘柄取得中...")

        def fetch(code4):
            time.sleep(0.03)
            return _fetch_jquants_one(code4, BACKFILL_FROM, date_to)

        with ThreadPoolExecutor(max_workers=8) as ex:
            done = 0
            for fut in as_completed({ex.submit(fetch, c): c for c in codes4}):
                adj_rows, split_rows, mx = fut.result()
                for code, dt, adjc in adj_rows:
                    jq_adjc[(code, dt)] = adjc
                for code, dt, af in split_rows:
                    all_jq_splits.append((code, dt, af, "jquants"))
                if mx and mx > jq_max_global:
                    jq_max_global = mx
                done += 1
                if done % 1000 == 0:
                    print(f"    {done}/{len(codes4)} 銘柄")
        print(f"  [J-Quants] 公式分割イベント {len(all_jq_splits)}件、最新日 {jq_max_global}")
    else:
        print("  [J-Quants] スキップ（利用不可のため Yahoo のみで構築）")

    # stock_splits 更新（full時は旧データ破棄。ただし use_jquants=False の代替経路では
    # J-Quants由来の既存データ（例: 1306）を壊さないため破棄しない）
    conn = get_conn(); cur = conn.cursor()
    if full and use_jquants:
        cur.execute("DELETE FROM stock_splits")
    if all_jq_splits:
        bulk_upsert(cur, "stock_splits", ["code", "ex_date", "split_ratio", "source"],
                    all_jq_splits, update_cols=["split_ratio", "source"])
    conn.commit(); cur.close(); conn.close()

    # 既存の分割イベント（今回のJ-Quants取得分・過去に登録済みの全ソースを含む）を取得。
    # 1306 のように Yahoo では検出できない銘柄の既存データも、ここで拾って
    # adj_close 計算に反映させる（さもないと Yahoo 経路で上書き消去されてしまう）。
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT code, ex_date, split_ratio FROM stock_splits")
    existing_rows = cur.fetchall()
    cur.close(); conn.close()
    existing = {(r[0], str(r[1])) for r in existing_rows}

    # Yahoo 暫定分割の対象期間: J-Quants が使えるならその先の窓のみ、
    # 使えないなら全期間（BACKFILL_FROM〜）を対象にする。
    if use_jquants:
        since = datetime.strptime(jq_max_global, "%Y-%m-%d").date() if jq_max_global else (date.today() - timedelta(weeks=16))
    else:
        since = datetime.strptime(BACKFILL_FROM, "%Y%m%d").date()
    print(f"  [Yahoo] {since} 以降の分割候補を確認中（妥当性チェック込み）...")
    yahoo_new = sync_yahoo_provisional_splits(codes4, since, existing)
    print(f"  [Yahoo] 新規分割候補 {len(yahoo_new)}件")

    # 生 close 取得
    print("  生 close 取得中...")
    conn = get_conn(); cur = conn.cursor()
    fmt = ",".join(["%s"] * len(codes4))
    cur.execute(f"SELECT code, date, close FROM daily_prices WHERE code IN ({fmt}) AND close IS NOT NULL ORDER BY code, date", codes4)
    by_code = defaultdict(list)
    for code, dt, close in cur.fetchall():
        by_code[code].append((str(dt), float(close)))
    cur.close(); conn.close()

    # Yahoo新規分割イベントごとに「close に反映済みか」を検証し、OKのものだけ採用してDB登録。
    # 注意: J-Quants経路でも検証は必須（かつては直近窓を無検証で信頼していたが、
    # DB構築時に Yahoo が「調整済み close」を返した銘柄は段差が存在せず、
    # 係数を掛けると二重調整でチャートが壊れる。2026-07 フジクラ5803等51銘柄の障害の根本原因）。
    accepted_new = []
    rejected = 0
    for code, ex_date, ratio, src in yahoo_new:
        ok = _verify_split_reflected(by_code.get(code, []), ex_date, ratio)
        if ok:
            accepted_new.append((code, ex_date, ratio, src))
        else:
            rejected += 1
            print(f"    [除外] {code} {ex_date}: close に段差が確認できず(既に調整済みの疑い) ratio={ratio}")
    if rejected:
        print(f"  [検証] {rejected}件は close 未反映のため adj_close 計算から除外")
    if accepted_new:
        conn = get_conn(); cur = conn.cursor()
        for c, dt, r, src in accepted_new:
            cur.execute("INSERT IGNORE INTO stock_splits (code,ex_date,split_ratio,source) VALUES (%s,%s,%s,%s)", (c, dt, r, src))
        conn.commit(); cur.close(); conn.close()

    # adj_close 計算用の分割マップ = 既存(DB登録済み全件) + 今回検証OKの新規分
    splits_by_code = defaultdict(list)
    for code, ex_date, ratio in existing_rows:
        splits_by_code[code].append((str(ex_date), float(ratio)))
    for code, ex_date, ratio, _ in accepted_new:
        splits_by_code[code].append((ex_date, ratio))

    # 分割イベントが存在する銘柄、または J-Quants の AdjC が取れている銘柄のみ
    # adj_close を再計算する。それ以外（分割の無い大多数の銘柄）は既存値を変更しない
    # ＝ Yahoo close の未確認な調整状態を推測で書き換えるリスクを避ける。
    targets = set(splits_by_code.keys()) | {c for c, _ in jq_adjc.keys()}
    if full and use_jquants:
        # J-Quants全量経路のみ: 分割の無い銘柄も対象（真の生closeで factor=1.0 に統一）
        targets = set(codes4)
    elif explicit_codes:
        # 呼び出し元が特定銘柄を明示指定した場合は、その銘柄「だけ」を対象にする。
        # 分割イベントの有無に関わらず必ず含める（イベント削除後の再計算漏れ防止）一方、
        # close系列を読み込んでいない指定外の銘柄が splits_by_code 経由で混ざるのを防ぐ。
        targets = set(codes4)

    upd = []
    for code in targets:
        series = by_code.get(code, [])
        # 適用時検証: 登録済みイベントもソース（jquants/yahoo）を問わず、
        # close 系列に実際に段差が確認できたものだけを係数適用の対象にする。
        # close が調整済みスケールで連続している銘柄（DB構築時期による）に
        # 過去イベントを機械的に掛けると二重調整になるため、ここが最後の防波堤。
        events = [(ex, r) for ex, r in splits_by_code.get(code, [])
                  if _verify_split_reflected(series, ex, r)]
        for ds, close in series:
            has_adjc = (code, ds) in jq_adjc
            # J-Quants の AdjC は現在判明している全分割（価格データ窓より未来の
            # 除権日を含む）を反映済みの公式調整値。イベントを重ねると二重調整に
            # なるため、AdjC を採用する日付には係数を一切掛けない（1436で実害確認）。
            base = jq_adjc[(code, ds)] if has_adjc else close
            factor = 1.0
            if not has_adjc:
                for ex_date, ratio in events:
                    if ex_date > ds:
                        factor *= ratio
            adj = round(base * factor, 4)
            af = round(adj / close, 6) if close else 1.0
            # 安全弁: DECIMAL(12,6)の範囲外になる異常値はDBエラーの元になるため除外
            if not (0 < af < 999999):
                print(f"    [警告] {code} {ds}: adj_factor異常値のためスキップ af={af}")
                continue
            upd.append((code, ds, adj, af))

    print(f"  adj_close 対象 {len(targets)}銘柄 / 書込 {len(upd)}行...")
    _bulk_upsert_adj(upd)

    # change_pct 再計算（adj_close を実際に変更した銘柄のみ。全銘柄再計算は重いため）
    if targets:
        from split_backfill import recompute_change_pct
        recompute_change_pct(sorted(targets))
    print(f"  完了: 対象{len(codes4)}銘柄中 {len(targets)}銘柄のadj_closeを更新（adj {len(upd)}行）")
    return len(upd)


# ─────────────────────────────────────────────────────────────────────────────
# 日次: 直近窓の新規分割のみ Yahoo 検知し該当銘柄を再計算
# ─────────────────────────────────────────────────────────────────────────────

def run_daily(weeks: int = 16) -> int:
    ensure_splits_table()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
    codes4 = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT code, ex_date FROM stock_splits")
    existing = {(r[0], str(r[1])) for r in cur.fetchall()}
    cur.close(); conn.close()

    since = date.today() - timedelta(weeks=weeks)
    yahoo_new = sync_yahoo_provisional_splits(codes4, since, existing)
    if not yahoo_new:
        print("  [分割] 直近窓の新規分割なし")
        return 0

    # 登録前に「close に段差が実在するか」を必ず検証する。
    # Yahoo は偽の分割イベントを返すことがあり、また close が調整済みスケールの
    # 銘柄では実分割でも段差が無い。未検証のまま登録→係数適用すると二重調整で
    # チャートが壊れる（2026-07 の51銘柄障害の再発防止）。
    cand_codes = sorted({c for c, *_ in yahoo_new})
    conn = get_conn(); cur = conn.cursor()
    fmt = ",".join(["%s"] * len(cand_codes))
    cur.execute(f"SELECT code, date, close FROM daily_prices WHERE code IN ({fmt}) AND close IS NOT NULL ORDER BY code, date", cand_codes)
    series_by_code = defaultdict(list)
    for c, dt, close in cur.fetchall():
        series_by_code[c].append((str(dt), float(close)))
    cur.close(); conn.close()

    verified = []
    for c, dt, r, src in yahoo_new:
        if _verify_split_reflected(series_by_code.get(c, []), dt, r):
            verified.append((c, dt, r, src))
        else:
            print(f"  [分割] 除外 {c} {dt}: close に段差なし（偽イベント/調整済みcloseの疑い） ratio={r}")
    if not verified:
        print("  [分割] 検証を通過した新規分割なし")
        return 0

    conn = get_conn(); cur = conn.cursor()
    changed = set()
    for c, dt, r, src in verified:
        cur.execute("INSERT IGNORE INTO stock_splits (code,ex_date,split_ratio,source) VALUES (%s,%s,%s,%s)", (c, dt, r, src))
        changed.add(c)
    conn.commit(); cur.close(); conn.close()
    print(f"  [分割] 新規分割 {len(verified)}件 / {len(changed)}銘柄 → adj_close 再計算")
    # 該当銘柄のみ J-Quants 主軸で再構築（fullではない）
    rebuild(sorted(changed), full=False)
    return len(changed)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args:
        rebuild(full=True, use_jquants="--no-jquants" not in args)
    elif "--daily" in args:
        run_daily()
    else:
        print("使い方: python splits.py [--backfill [--no-jquants] | --daily]")

"""
資金フロー分析 — 「どこに資金が流入しているか」を週次で多角的に集計する。

グループ軸（4種類）:
  theme  : kabutanテーマタグ（kabutan_themes、銘柄ごとに多数付与 → AI株・防衛・半導体など細かい切り口）
  sector : 東証33業種（sectors）
  size   : 時価総額帯（大型≥1兆 / 準大型3000億-1兆 / 中型1000-3000億 / 小型300-1000億 / 超小型<300億）
  style  : 投資スタイル（高配当・バリュー・グロース・好業績・高ROE・低位株）

週次メトリクス（グループ×週）:
  turnover       : 週間売買代金合計（億円）
  turnover_share : 全市場に占めるシェア（%）
  flow_ratio     : シェアの対13週平均比（>1 = 資金がこのグループに移動している）★資金流入度
  ret_median     : 構成銘柄の週間騰落率の中央値（%）
  breadth        : 上昇銘柄比率（%）
  excess_topix   : 中央値騰落率 − TOPIX(1306)週間騰落率（%pt）
  top_stocks     : 売買代金上位の構成銘柄（JSON、ドリルダウン表示用）

設計メモ:
  - 週キーは ISO週の金曜日（week_end）。進行中の週は日次で上書き更新され、確定後は不変。
  - 毎回直近 LOOKBACK_WEEKS 週分をまとめて再計算して upsert（自己修復・履歴自動蓄積）。
  - 銘柄属性（時価総額・配当利回り等）は「現在値」を過去週にも適用する近似。
    資金フローの方向感を掴む用途では十分（厳密なPITはバックテスト系に任せる）。
  - 対象は プライム/スタンダード/グロース の現物株のみ（ETF/REIT/PRO Market除外）。

実行例:
  python3 money_flow.py            # 直近26週を再計算して保存
  python3 money_flow.py --weeks 8  # 直近8週のみ
"""
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import median, pstdev
from config import get_conn

LOOKBACK_WEEKS = 26      # 計算対象の週数（13週ベースライン + 表示13週）
BASELINE_WEEKS = 13      # flow_ratio のベースライン週数
MIN_STOCKS     = 5       # グループ最小銘柄数（これ未満は保存しない）
TOPIX_ETF      = "1306"  # ベンチマーク
# 銘柄あたりのkabutanテーマ付与数の上限。これを超える銘柄（パナソニック162・NTT103・
# セブン&アイ57 等の巨大コングロマリット）は、多数テーマに広く付与されタグがノイズ化し、
# 巨大な売買代金でニッチテーマの指標・代表銘柄を汚染するため、テーマ集計から除外する。
# ※業種/規模/スタイルのグループには影響しない（テーマ集計のみ）。中央値11・75%ile16 に対し
#   25 は上位約8%（明確に拡散したコングロ）だけを外す水準。
THEME_MAX_PER_STOCK = 25

SIZE_BUCKETS = [
    ("mega",  "大型（1兆円以上）",        lambda mc: mc >= 1e12),
    ("large", "準大型（3000億〜1兆円）",  lambda mc: 3e11 <= mc < 1e12),
    ("mid",   "中型（1000億〜3000億円）", lambda mc: 1e11 <= mc < 3e11),
    ("small", "小型（300億〜1000億円）",  lambda mc: 3e10 <= mc < 1e11),
    ("micro", "超小型（300億円未満）",    lambda mc: mc < 3e10),
]

GROUP_TYPE_LABELS = {"theme": "テーマ", "sector": "業種", "size": "規模", "style": "スタイル"}


def ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS money_flow_weekly (
            week_end     DATE        NOT NULL,   -- ISO週の金曜日
            group_type   VARCHAR(16) NOT NULL,   -- theme / sector / size / style
            group_key    VARCHAR(80) NOT NULL,
            group_label  VARCHAR(80),
            n_stocks     INT,
            turnover     DOUBLE,                 -- 億円
            turnover_share DOUBLE,               -- %
            flow_ratio   DOUBLE,                 -- 対13週平均シェア比
            zscore       DOUBLE,                 -- 今週シェアの対過去13週 Zスコア（母数非依存の流入強度）
            ret_median   DOUBLE,                 -- %
            ret_mean     DOUBLE,                 -- %
            breadth      DOUBLE,                 -- %
            excess_topix DOUBLE,                 -- %pt
            flow_class   VARCHAR(10),            -- inflow=流入 / dump=投げ売り / outflow=流出 / neutral
            last_trade_date DATE,                -- 週内の最終取引日
            top_stocks   TEXT,                   -- JSON [{code,name,ret,tv}]
            updated_at   DATETIME,
            PRIMARY KEY (week_end, group_type, group_key)
        )
    """)
    # 既存テーブルへのカラム追加（初回マイグレーション）
    for col, typedef in [
        ("zscore", "DOUBLE"),
        ("flow_class", "VARCHAR(10)"),
    ]:
        try:
            cur.execute(f"ALTER TABLE money_flow_weekly ADD COLUMN {col} {typedef}")
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()


def _iso_friday(d: date) -> date:
    """dが属するISO週の金曜日（週キー）。"""
    return d + timedelta(days=4 - d.isoweekday())


def _load_groups(cur) -> tuple[dict, dict]:
    """
    銘柄→所属グループ一覧 と グループ→ラベル を構築する。
    戻り値: (groups[code] = [(group_type, group_key), ...], labels[(type,key)] = label)
    """
    groups: dict[str, list] = defaultdict(list)
    labels: dict[tuple, str] = {}

    # 対象銘柄: プライム/スタンダード/グロースの現役銘柄 + 属性
    cur.execute("""
        SELECT s.code, sec.name,
               f.market_cap, f.div_yield, f.per, f.pbr, f.roe,
               ps.rev_growth, ps.op_growth, ps.close
        FROM stocks s
        LEFT JOIN sectors sec ON s.sector_id = sec.id
        LEFT JOIN stock_fundamentals f ON f.code = s.code
        LEFT JOIN price_stats ps ON ps.code = s.code
        WHERE s.is_active = 1 AND s.market_id IN (2, 3, 4)
    """)
    for code, sec_name, mc, dy, per, pbr, roe, revg, opg, close in cur.fetchall():
        mc  = float(mc)  if mc  else None
        dy  = float(dy)  if dy  else None
        per = float(per) if per else None
        pbr = float(pbr) if pbr else None
        roe = float(roe) if roe else None
        revg = float(revg) if revg is not None else None
        opg  = float(opg)  if opg  is not None else None
        close = float(close) if close else None

        if sec_name:
            groups[code].append(("sector", sec_name))
            labels[("sector", sec_name)] = sec_name
        if mc:
            for key, label, cond in SIZE_BUCKETS:
                if cond(mc):
                    groups[code].append(("size", key))
                    labels[("size", key)] = label
                    break
        # スタイル（重複所属OK）
        styles = []
        if dy and dy >= 3.5:
            styles.append(("hi_div", "高配当（利回り3.5%+）"))
        if pbr and pbr < 1.0:
            styles.append(("value", "バリュー（PBR1倍割れ）"))
        if revg is not None and revg >= 15:
            styles.append(("growth", "グロース（増収15%+）"))
        if revg is not None and opg is not None and revg >= 5 and opg >= 10:
            styles.append(("earnings", "好業績（増収増益）"))
        if roe and roe >= 15:
            styles.append(("hi_roe", "高ROE（15%+）"))
        if close and close < 500:
            styles.append(("low_price", "低位株（500円未満）"))
        for key, label in styles:
            groups[code].append(("style", key))
            labels[("style", key)] = label

    # テーマ: 統一テーママスタ(theme_master.py)の active テーマ × tier>=2(コア/関連)のみ。
    # 旧: kabutanタグ直接参照（コングロ汚染のため2026-07に廃止。タグは証拠データとして
    # theme_master のスコアリングに使われる）。フォールバック: マスタ未構築なら旧方式。
    try:
        from theme_master import load_theme_groups
        tg, tl = load_theme_groups(cur, min_tier=2)
        if not tl:
            raise RuntimeError("テーママスタが空")
        for code, glist in tg.items():
            groups[code].extend(glist)
        labels.update(tl)
    except Exception as e:  # noqa: BLE001  (テーブル未作成等)
        print(f"  [theme_master] 読込失敗・kabutanタグに fallback: {str(e)[:60]}")
        cur.execute("""
            SELECT kt.theme, kt.code
            FROM kabutan_themes kt
            JOIN stocks s ON s.code = kt.code
            WHERE s.is_active = 1 AND s.market_id IN (2, 3, 4)
              AND kt.theme IN (
                SELECT theme FROM kabutan_themes GROUP BY theme HAVING COUNT(*) >= %s
              )
              AND kt.code NOT IN (
                SELECT code FROM kabutan_themes GROUP BY code HAVING COUNT(*) > %s
              )
        """, (MIN_STOCKS, THEME_MAX_PER_STOCK))
        for theme, code in cur.fetchall():
            groups[code].append(("theme", theme))
            labels[("theme", theme)] = theme
    return groups, labels


def get_group_members(group_type: str, group_key: str) -> tuple[list[str], str]:
    """指定グループの所属銘柄コード一覧と表示ラベルを返す（ドリルダウンページ用）。
    グループ定義は _load_groups と完全に同一（定義の二重管理を避けるため流用）。"""
    conn = get_conn()
    cur  = conn.cursor()
    groups, labels = _load_groups(cur)
    cur.close()
    conn.close()
    target = (group_type, group_key)
    codes = [c for c, gl in groups.items() if target in gl]
    return codes, labels.get(target, group_key)


def compute(weeks: int = LOOKBACK_WEEKS) -> int:
    """直近weeks週の資金フローを再計算してmoney_flow_weeklyにupsertする。"""
    ensure_table()
    conn = get_conn()
    cur  = conn.cursor()

    from_dt = date.today() - timedelta(weeks=weeks + 1)
    groups, labels = _load_groups(cur)
    print(f"  対象銘柄: {len(groups)}  グループ: {len(labels)}")

    # 銘柄名（top_stocks表示用）
    cur.execute("SELECT code, name FROM stocks WHERE is_active = 1")
    names = dict(cur.fetchall())

    # 代表銘柄の「テーマ特化度」重み用: 銘柄あたりの全kabutanテーマ数。
    # テーマの代表銘柄は「売買代金 ÷ テーマ数」で選び、多数テーマに緩く付いた大型株より
    # そのテーマに特化した銘柄が前に出るようにする（表示上の代表性を高める。指標は実額のまま）。
    cur.execute("SELECT code, COUNT(*) FROM kabutan_themes GROUP BY code")
    theme_cnt = {c: int(n) for c, n in cur.fetchall()}

    # 週次の銘柄別集計: 週間売買代金・週末終値（分割調整済み）
    # ※ 26週×約3700銘柄 ≈ 45万行を1クエリで集計してPython側でグループ展開する
    #   （週→金曜日の変換はDB方言差を避けてPython側で行う）
    cur.execute("""
        SELECT code,
               YEARWEEK(date, 3)                                               AS yw,
               MAX(date)                                                       AS last_dt,
               SUM(COALESCE(turnover, volume * close))                         AS tv,
               SUBSTRING_INDEX(GROUP_CONCAT(
                   COALESCE(adj_close, close) ORDER BY date DESC), ',', 1)     AS wk_close
        FROM daily_prices
        WHERE date >= %s AND close IS NOT NULL AND close > 0
        GROUP BY code, YEARWEEK(date, 3)
    """, (from_dt,))
    rows = cur.fetchall()

    from datetime import datetime as _dt
    _fri_cache: dict[int, date] = {}
    def _yw_to_friday(yw: int) -> date:
        if yw not in _fri_cache:
            _fri_cache[yw] = _dt.strptime(f"{yw // 100}-W{yw % 100:02d}-5", "%G-W%V-%u").date()
        return _fri_cache[yw]

    # code → {week_friday: (last_dt, tv, close)}
    by_stock: dict[str, dict] = defaultdict(dict)
    week_set: set = set()
    week_last_dt: dict = {}   # 週→市場全体の最終取引日
    for code, yw, last_dt, tv, wk_close in rows:
        if yw is None:
            continue
        wk_fri = _yw_to_friday(int(yw))
        by_stock[code][wk_fri] = (last_dt, float(tv or 0), float(wk_close or 0))
        week_set.add(wk_fri)
        if wk_fri not in week_last_dt or last_dt > week_last_dt[wk_fri]:
            week_last_dt[wk_fri] = last_dt
    week_list = sorted(week_set)
    if len(week_list) < 3:
        print("  データ不足のためスキップ")
        cur.close(); conn.close()
        return 0

    # TOPIX(1306)の週間騰落率
    topix_ret: dict = {}
    t = by_stock.get(TOPIX_ETF, {})
    for i, wk in enumerate(week_list):
        if i == 0 or wk not in t or week_list[i-1] not in t:
            continue
        prev_c, cur_c = t[week_list[i-1]][2], t[wk][2]
        if prev_c > 0:
            topix_ret[wk] = (cur_c / prev_c - 1) * 100

    # 週×グループで集計
    # agg[(wk, gtype, gkey)] = {"tv":…, "rets":[…], "tops": 上位5銘柄のみ保持（min-heap）}
    import heapq
    agg: dict[tuple, dict] = defaultdict(lambda: {"tv": 0.0, "rets": [], "tops": []})
    market_tv: dict = defaultdict(float)   # 週次の全市場売買代金（対象銘柄合計）

    for code, wk_map in by_stock.items():
        glist = groups.get(code)
        if not glist:
            continue
        for i, wk in enumerate(week_list):
            if wk not in wk_map:
                continue
            last_dt, tv, close = wk_map[wk]
            ret = None
            if i > 0 and week_list[i-1] in wk_map:
                prev_close = wk_map[week_list[i-1]][2]
                if prev_close > 0 and close > 0:
                    ret = (close / prev_close - 1) * 100
            market_tv[wk] += tv
            for gtype, gkey in glist:
                a = agg[(wk, gtype, gkey)]
                a["tv"] += tv
                if ret is not None:
                    a["rets"].append(ret)
                    # テーマは特化度(=売買代金÷テーマ数)で代表銘柄を選ぶ。
                    # 業種/規模/スタイルは該当しないので売買代金そのもの。
                    score = tv / theme_cnt.get(code, 1) if gtype == "theme" else tv
                    if len(a["tops"]) < 5:
                        heapq.heappush(a["tops"], (score, tv, code, ret))
                    elif score > a["tops"][0][0]:
                        heapq.heapreplace(a["tops"], (score, tv, code, ret))

    # 保存（最初の週はリターン計算不能なので除外。flow_ratioは過去シェアが
    # BASELINE_WEEKS分無くても、最低4週あれば計算する）
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    saved = 0
    # グループ×週のシェア履歴を先に構築
    share: dict[tuple, dict] = defaultdict(dict)   # (gtype,gkey) -> {wk: share%}
    for (wk, gtype, gkey), a in agg.items():
        mtv = market_tv.get(wk, 0)
        if mtv > 0:
            share[(gtype, gkey)][wk] = a["tv"] / mtv * 100

    upserts = []
    for (wk, gtype, gkey), a in agg.items():
        rets = a["rets"]
        if len(rets) < MIN_STOCKS:
            continue
        idx = week_list.index(wk)
        if idx == 0:
            continue
        sh_map = share[(gtype, gkey)]
        cur_share = sh_map.get(wk)
        past = [sh_map[w] for w in week_list[max(0, idx - BASELINE_WEEKS):idx] if w in sh_map]
        flow_ratio = (cur_share / (sum(past) / len(past))) if (cur_share and len(past) >= 4 and sum(past) > 0) else None

        # Zスコア: 今週シェアが過去の変動幅の何σ分か。母数(銘柄数・売買代金)の大小に
        # 依存せず「そのグループにとって異常な流入か」を測る。過去も普段からブレていた
        # 小グループ（紳士靴等）は普段のσが大きいため、多少のシェア増ではZが上がらない＝ノイズ抑制。
        zscore = None
        if cur_share is not None and len(past) >= 4:
            mean = sum(past) / len(past)
            sd = pstdev(past)
            if sd > 1e-9:
                zscore = (cur_share - mean) / sd

        ret_med  = median(rets)
        ret_mean = sum(rets) / len(rets)
        breadth  = sum(1 for r in rets if r > 0) / len(rets) * 100
        excess   = (ret_med - topix_ret[wk]) if wk in topix_ret else None

        # 資金フロー分類（表示・スコアリング用の一次判定）:
        #   inflow  = 資金流入（買い優勢）: 流入が有意(Z高 or 流入度高) かつ 上昇・広がりあり
        #   dump    = 投げ売り警戒（売り優勢の大商い）: 流入が有意 かつ 明確に下落 ← 貴重な逆張り/警戒情報
        #   outflow = 流出（冷却）: シェア低下
        #   neutral = それ以外
        flow_class = "neutral"
        strong = (zscore is not None and zscore >= 1.0) or (flow_ratio is not None and flow_ratio >= 1.15)
        if strong and ret_med > 0 and breadth >= 50:
            flow_class = "inflow"
        elif strong and ret_med <= -3:
            flow_class = "dump"
        elif flow_ratio is not None and flow_ratio < 0.9:
            flow_class = "outflow"

        tops = sorted(a["tops"], reverse=True)
        top_json = json.dumps([
            {"code": c, "name": names.get(c, c), "ret": round(r, 1), "tv": round(tv_ / 1e8, 1)}
            for _score, tv_, c, r in tops
        ], ensure_ascii=False)
        last_dt = week_last_dt.get(wk)
        upserts.append((
            wk, gtype, gkey, labels.get((gtype, gkey), gkey), len(rets),
            round(a["tv"] / 1e8, 1), round(cur_share, 3) if cur_share else None,
            round(flow_ratio, 3) if flow_ratio else None,
            round(zscore, 2) if zscore is not None else None,
            round(ret_med, 2), round(ret_mean, 2), round(breadth, 1),
            round(excess, 2) if excess is not None else None,
            flow_class, last_dt, top_json, now,
        ))

    # 再計算対象週の既存行を先に削除してから入れ直す。これにより、テーマ除外や
    # 銘柄異動で MIN_STOCKS 未満に落ちたグループの「古い行」が残留するのを防ぐ
    # （残ると、もう集計対象でないコングロマリットが表示され続けてしまう）。
    refresh_weeks = sorted(set(week_list[1:]))
    if refresh_weeks:
        ph = ",".join(["%s"] * len(refresh_weeks))
        cur.execute(f"DELETE FROM money_flow_weekly WHERE week_end IN ({ph})", refresh_weeks)

    cur.executemany("""
        INSERT INTO money_flow_weekly
            (week_end, group_type, group_key, group_label, n_stocks,
             turnover, turnover_share, flow_ratio, zscore,
             ret_median, ret_mean, breadth, excess_topix,
             flow_class, last_trade_date, top_stocks, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            group_label=VALUES(group_label), n_stocks=VALUES(n_stocks),
            turnover=VALUES(turnover), turnover_share=VALUES(turnover_share),
            flow_ratio=VALUES(flow_ratio), zscore=VALUES(zscore),
            ret_median=VALUES(ret_median),
            ret_mean=VALUES(ret_mean), breadth=VALUES(breadth),
            excess_topix=VALUES(excess_topix), flow_class=VALUES(flow_class),
            last_trade_date=VALUES(last_trade_date),
            top_stocks=VALUES(top_stocks), updated_at=VALUES(updated_at)
    """, upserts)
    saved = len(upserts)
    conn.commit()
    cur.close()
    conn.close()
    print(f"  資金フロー保存: {saved} 行（{len(week_list)}週 × グループ）")
    return saved


if __name__ == "__main__":
    weeks = LOOKBACK_WEEKS
    if "--weeks" in sys.argv:
        weeks = int(sys.argv[sys.argv.index("--weeks") + 1])
    compute(weeks=weeks)

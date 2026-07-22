"""
AIファンドマネージャー
======================
AIが銘柄を発掘・保有・売却する模擬運用ファンド。サイトの /aifund タブに表示する。

運用ルール（オーナー指定）:
  - 資金1000万円スタート。常に8銘柄を保有（市況を問わず）
  - 投資期間の目安は数日〜半年。キャピタルゲインの最大化が目的
  - 売買には必ず理由を付け、購入理由〜売却理由まで通しで記録する
  - **意思決定した当日の株価では売買しない**（先読み防止）。
    夜に意思決定 → 翌営業日の寄付（始値）で約定
  - 100株単位。銘柄ごとに予算の強弱をつけてよい
  - 取引コスト: 約定代金の0.1%/片道（手数料+スリッページの模擬）

アーキテクチャ（ハイブリッド）:
  1. 定量スクリーニングで候補を数十銘柄に絞る（モメンタム/押し目/ブレイク/
     業績イベント/割安成長 の5観点。price_stats・theoretical_values・
     forecast_revisions を利用）
  2. Gemini が現ポジションと候補を見て売り/買いを決定し、理由とシナリオ
     （どうなったら売るか）を日本語で書く
  3. コード側ガードレールが強制執行:
     常時8銘柄・予算60万〜250万/銘柄・1日の入替最大3銘柄（初回除く）・
     売却後5営業日は同一銘柄の再購入禁止・含み損-20%で強制ロスカット

日次フロー（daily_run.py に組込み）:
  - メイン便（夕方）: execute_orders() 当日寄付で約定 → record_nav() 終値で評価
  - イブニング便（20:30・開示回収後）: decide() 意思決定 → 翌日分の注文登録

実行例:
  python3 ai_fund.py --execute   # pending注文を当日寄付で約定
  python3 ai_fund.py --nav       # 当日NAVを記録
  python3 ai_fund.py --decide    # 意思決定（Gemini・注文登録）
  python3 ai_fund.py --status    # 現状表示
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from config import get_conn

load_dotenv()

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

INITIAL_CASH   = 10_000_000
N_POSITIONS    = 8            # 常時保有する銘柄数
COST_RATE      = 0.001        # 片道0.1%（往復0.2%）
BUDGET_MIN     = 600_000      # 1銘柄の予算下限
BUDGET_MAX     = 2_500_000    # 1銘柄の予算上限
MAX_SWAPS      = 2            # 1日の入替上限（初回構築を除く。過回転＝目先の上下での振り落とし・
                             # コスト・whipsawを抑え、良い銘柄を握り続けるため3→2に抑制）
REBUY_COOLDOWN = 7            # 売却後の再購入禁止（カレンダー日）
LOSSCUT_PCT    = -20.0        # 強制ロスカット閾値（含み損%）
PROFIT_ARM_PCT   = 25.0       # 利益保全: 取得後高値がこの含み益%を超えたらトレールに移行
PROFIT_TRAIL_PCT = 15.0       # 移行後は高値からこの%下落で機械売り（-10%→-15%: テーマ相場の捕捉率53%→59%と実測）
# 段階的トレール: 大きな勝者ほど押し目が深くなるため、勝ち幅に応じて許容下落を広げて振り落としを防ぐ
# （「本当に良い銘柄を目先の上下に惑わされず握り続ける」ための構造。基本-15%の実測を土台に拡張）。
# (取得後高値の含み益%がキー以上, 高値からの許容下落%)。上から順に最初に該当したものを使う。
PROFIT_TRAIL_TIERS = [(100.0, 25.0), (50.0, 20.0), (PROFIT_ARM_PCT, PROFIT_TRAIL_PCT)]


def _trail_pct(hi_gain_pct: float) -> float:
    """取得後高値の含み益%に応じた許容トレール下落%を返す（大勝ち銘柄ほど広い余地）。"""
    for arm, trail in PROFIT_TRAIL_TIERS:
        if hi_gain_pct >= arm:
            return trail
    return PROFIT_TRAIL_PCT
STOP_LOCK_PCT  = 8.0         # ストップ高/安の一本値張り付き判定（高値==安値かつ前日比がこの%以上）
BENCH_CODE     = "1306"       # ベンチマーク: TOPIX連動ETF
N_BENCH        = 8            # 控え（次点候補）銘柄数
MAX_ANTICIPATE = 2            # 「先回り（予測）」スタイルの保有上限（ポートフォリオの一部に留める）

# ファンドの運営方針（憲章）。固定ルールの一元記述。サイトの /aifund に常時表示する。
# 変更時はこの定数を更新する（コードのガードレールと必ず一致させること）。
CHARTER = f"""1. 目的: キャピタルゲインの最大化。投資期間の目安は数日〜数ヶ月・最長9ヶ月（デイトレはしない）
2. 常時{N_POSITIONS}銘柄を保有し、市況を問わずフルポジションを維持する
3. 全ての買いに「カタリスト」を明文化する: 自分が買った後に、誰が・いつ・なぜ買い上げてくるのか
   （例: 決算がコンセンサスを上回る見込み／統計的エッジ／業界拡大に必須のパーツだが未注目）。
   カタリストは毎晩再検証し、崩れたら売却する
4. 意思決定した当日の価格では売買しない。夜に判断し、翌営業日に約定（先読みの排除）。
   約定方法は成行（翌営業日の寄付）か指値（その価格に到達すれば約定・届かなければ失効し翌日再判断）を選べる
5. 売買単位は100株。1銘柄の予算は{BUDGET_MIN//10000}万〜{BUDGET_MAX//10000}万円で確信度に応じて強弱
6. 取引コスト{COST_RATE*100:.1f}%/片道を控除。無駄な回転を抑えるため入替は1日最大{MAX_SWAPS}銘柄、
   売却後{REBUY_COOLDOWN}日間は同一銘柄を再購入しない
7. 損切りは-12%目安（AIの規律）、含み損{LOSSCUT_PCT:.0f}%で強制ロスカット（システムが機械的に執行）
8. 「先回り（予測）」スタイルは最大{MAX_ANTICIPATE}銘柄まで。予測であることを明示する
9. 保有に次ぐ控え{N_BENCH}銘柄を常に選定・計測し、機動的に昇格させる
10. 投資基準は毎晩、相場環境と自らの成績を検証して明文化・更新し、履歴を蓄積する
11. 決算発表日を必ず把握し、無自覚に決算を跨がない（跨ぐならそれ自体をカタリストとして明文化する）
12. 分散を守る: 同一業種・同一テーマは最大3銘柄。カタリストの種類（イベント/需給(自社株買い)/トレンド/割安見直し/先回り）も分散
13. 検証済みプレイブック（docs/trade_strategy_research.md）に従う。特に「深押しからの逆張り」
    「出来高急増のみを根拠にした初動買い」「RSI85以上の過熱圏での新規買い」は実測で期待値マイナスのため取らない
14. 利益保全（段階的トレール）: 取得後の高値が+{PROFIT_ARM_PCT:.0f}%を超えたらトレールに移行し、
    高値から一定%下落で機械売却。許容下落は勝ち幅で拡大（+{PROFIT_ARM_PCT:.0f}%〜:-{PROFIT_TRAIL_PCT:.0f}% / +50%〜:-20% / +100%〜:-25%）。
    大勝ち銘柄ほど押し目が深くなるため余地を広げ、目先の上下で優良銘柄を振り落とさない
    （規律を判断より優先。トレンドとカタリストが継続する限り含み益を返しても保有を続ける）
19. 【保有規律】カタリストとシナリオが生きている銘柄は、目先の下落や一時的なアンダーパフォーム、
    あるいは「他にもっと良さそうな候補が現れた」程度の理由では売らない。売ってよいのは
    (a)機械規律(損切り/トレール) (b)カタリストが実現・出尽くした (c)シナリオが崩れた具体的証拠がある
    (d)候補が明確かつ大幅に優れ、かつ保有銘柄のエッジが薄れている——のいずれか。
    リターンの大半は少数の大勝ち銘柄を数ヶ月握って生まれる。過回転はコストとwhipsawで期待値を毀損する
15. モメンタム系スタイル（相対強度リーダー・テーマ主役）の新規買いはTOPIXが200日線より上のときのみ
    （200日線割れ局面では勝率24%まで劣化と実測。地合い悪化時は押し目反発・イベント型を優先する）
16. 全ての買いに投資スタイル（A:52週ブレイク/R:相対強度リーダー/E:好業績ドリフト/T:テーマ主役/
    C:押し目反発/V:割安×成長/L:先回り）を明示し、保有中もスタイル別に成績を検証・表示する
17. 買い注文は寄付が決定日終値の+15%を超えてギャップした場合は自動失効
    （引け後開示の翌朝プレイ等での高値掴み防止。「+15%超の初動に乗らない」実測ルールの機械執行）
18. ストップ高/安で一本値に張り付いた（気配・比例配分）銘柄は約定不能として見送る。
    TOB等でストップ高に張り付くと出来高があっても全員には割り当たらないため、成行でも
    約定を仮定しない（買いはS高気配で入れない／売りはS安気配で逃げられず翌日再判断）"""

# 検証済みプレイブック（docs/trade_strategy_research.md の要約。毎晩のプロンプトに注入）
# 2024-01〜2026-07の全銘柄日次データ・全シグナル機械エントリー・コスト0.2%控除で実測。
PLAYBOOK = """【主力】A:52週高値ブレイク型 — 出来高(5日/25日比)1.3倍以上を伴い52週高値を更新・更新間近の銘柄。
  実測: 3345取引 平均+10.2% 勝率54%（下記2段階エグジット併用時）。半導体相場の勝者(1年+50%超)204銘柄の
  98%はこの型を通過しており、初回シグナルは上昇の2割消化時点＝テーマ相場に乗る入口として機能する
【主力】R:相対強度リーダー型 — 6ヶ月騰落率が市場上位10%かつ52週高値-10%圏・MA25上（テーマ相場の主役に乗る型）。
  実測: 1155取引(RSI<85) 平均+12.0% 中央値+5.5% 勝率55%。TOPIXが200日線より上のときのみ有効（下では勝率24%）。
  なお同じ上位10%でも極端な最上位(97パーセンタイル超)は中央値ゼロ近辺＝「一番過激な銘柄」より「高値圏の2番手集団」
【副力】T:テーマ主役型 — テーマ平均25日騰落+10%超の強テーマ内でRS上位1/3の銘柄。
  実測: 722取引 平均+8.4%だが中央値-2%（当たり外れの大きい宝くじ型）。カタリスト必須・分散前提で少数に留める。
  強テーマ内の「出遅れ」買いは主役の半分のリターン（主役+6.4% vs 出遅れ+3.7%/20日）→ 先回りは最小限に
【副力】C:トレンド押し目反発型 — 200日線が上向きで株価がその上、RSI42未満まで調整→5日で+3%以上の反発を確認。
  実測: 2554取引 平均+6.4%。平常時は+3%程度だが、市場急落直後のリバウンド局面では平均+20%超（地合い悪化時の主戦術）
【RSIの使い方（全数実測）】上昇トレンド中の高RSI(65-85)は売り理由にならない（60営業日後リターンはむしろ最高帯。
  「過熱だから売る」は誤り）。ただしRSI85以上での新規買いは全スタイルで期待値マイナス（勝率31〜44%）のため禁止。
  押し目買いはRSI45未満→反発確認後のみ
【禁止】52週高値から-40%超の深押し逆張り／出来高急増のみを根拠にした初動買い／RSI85以上の新規買い
  （全シグナル検証で期待値マイナス。勝った例だけ見ると魅力的に見える選択バイアスに注意）
【エグジット規律・2段階＋段階的トレール】買値-12%で損切り。+25%到達後は高値からのトレールに切替えて
  利を伸ばす。許容下落は勝ち幅で拡大（+25%〜:-15% / +50%〜:-20% / +100%〜:-25%）——大勝ち銘柄ほど
  押し目が深くなるため余地を広げ、大相場の途中で振り落とされないようにする。トレール-10%→-15%で
  長期テーマ相場の捕捉率が53%→59%に改善と実測。利が乗る前のMA25割れ売りやRSI過熱での利食いは
  whipsawで期待値を毀損する（実測: 平均+2.0%まで劣化）。大勝ち銘柄を握り続けることがリターンの源泉
【主力級】E:好業績ドリフト型 — 決算短信・上方修正などの発表後、市場の初動反応が+7〜15%で株価が52週高値-15%圏
  にある銘柄を翌日に買う。実測(決算シーズン窓プロキシ・496取引): 平均+11.8% 勝率57%。
  初動+15%超への飛び乗り（勝率42%）と深いベース(52週高値-15%超下・勝率40%)は期待値ゼロ〜マイナスのため見送り。
  決算跨ぎは進捗率の上振れ等の根拠を明文化できるときのみ（発表後の初動からでも統計的に十分間に合う）
【E派生・引け後プレイと跨ぎ】fresh_event=本日引け後の上方修正等・株価は未反応→明朝の寄付で入る先行プレイ。
  自前検証は蓄積待ち（PEAD研究では発表翌日からのドリフトが60〜90日続く）。寄付が決定日終値+15%超なら
  システムが約定を自動失効（ギャップ高値掴み防止）。preview=発表7日以内×進捗率が按分+20pt超の上振れ候補。
  いずれも合計2銘柄程度に留め、通常のE（反応確認済みドリフト）を優先する
【天井の平均像】起点から60〜70日・RSI75前後。天井後9〜10日でMA25割れ（そこで売ると6〜7割捕捉）
【時間】想定保有60〜130営業日。トレンドとカタリストが生きている限り最長9ヶ月まで延長可。
  60日で+10%に届かなければ入替を検討"""


# 投資スタイル分類（全数バックテストで検証済み。買い注文ごとに strategy コードを付与し、
# サイト /aifund に常時表示する。定義を変えたら PLAYBOOK・CHARTER と必ず同期させること）
STRATEGIES = {
    "A": {"name": "52週ブレイク",     "entry": "52週高値の更新・更新間近×出来高1.3倍以上",
          "edge": "平均+10.2%／勝率54%（n=3,345）", "role": "主力。テーマ相場の入口"},
    "R": {"name": "相対強度リーダー", "entry": "6ヶ月騰落率が市場上位10%×52週高値-10%圏×MA25上",
          "edge": "平均+12.0%／勝率55%（n=1,155）", "role": "主力。テーマ相場の主役に乗る（TOPIX200日線上のみ）"},
    "E": {"name": "好業績ドリフト",   "entry": "決算・上方修正の発表後、初動+7〜15%×52週高値-15%圏を翌日に買う",
          "edge": "平均+11.8%／勝率57%（n=496・プロキシ検証）", "role": "主力級。決算シーズンの主戦術"},
    "T": {"name": "テーマ主役",       "entry": "テーマ平均25日+10%超の強テーマ内でテーマ内RS上位1/3",
          "edge": "平均+8.4%／中央値-2%（n=722）", "role": "副力。当たり外れ大・カタリスト必須・少数保有"},
    "C": {"name": "押し目反発",       "entry": "200日線上向き×株価がその上×RSI<45→5日+3%超の反発確認",
          "edge": "平均+6.4%・市場急落局面では+20%超（n=2,554）", "role": "副力。地合い悪化時の主戦術"},
    "V": {"name": "割安×成長",       "entry": "理論株価比1.25倍以上×営業増益×財務健全性(F-score≥5)×需給が崩れていない",
          "edge": "（理論株価・スクリーニング由来。クオリティ足切りでバリュートラップを除外）", "role": "中期の柱。モメンタム消失時の受け皿"},
    "L": {"name": "先回り",           "entry": "資金流入テーマ内でまだ上がっていない銘柄を予測で先回り",
          "edge": "+3.4%/20日（テーマ主役の約半分）", "role": "予測枠。最大2銘柄まで・予測であることを明示"},
}
# 候補タグ→スタイルの既定対応（AIがstrategy未指定のときのフォールバック。優先順に判定）
_STRAT_TAG_ORDER = [("event", "E"), ("fresh_event", "E"), ("preview", "E"), ("buyback", "E"),
                    ("breakout", "A"), ("rs_leader", "R"),
                    ("theme_leader", "T"), ("dip", "C"), ("value_growth", "V"), ("laggard", "L"),
                    ("momentum", "R")]
STRATEGIES_FORBIDDEN = ("禁止: 深押し逆張り（52週高値-40%超）／出来高急増のみを根拠にした初動買い／"
                        "RSI85以上の新規買い（いずれも全シグナル検証で期待値マイナス）")


def _strategy_from_tags(tags) -> str:
    for tag, code in _STRAT_TAG_ORDER:
        if tag in (tags or []):
            return code
    return "R"


def _parse_order(o: dict, ref_close) -> tuple[str, float | None]:
    """AI出力の order_type/limit を検証して (order_type, limit_price) を返す。
    指値が現値から±50%超乖離、または数値不正なら誤り/暴走とみなし成行に落とす。"""
    ot = str(o.get("order_type", "")).strip()
    if ot not in ("指値", "limit"):
        return "market", None
    try:
        lp = float(o.get("limit"))
    except (TypeError, ValueError):
        return "market", None
    if lp <= 0 or not ref_close or float(ref_close) <= 0:
        return "market", None
    if not (float(ref_close) * 0.5 <= lp <= float(ref_close) * 1.5):
        return "market", None
    return "limit", round(lp, 1)


# ─────────────────────────────────────────────────────────────────────────────
# テーブル
# ─────────────────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_state (
            id             TINYINT PRIMARY KEY,
            cash           DOUBLE NOT NULL,
            inception_date DATE,
            last_decided   DATE,
            updated_at     DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_positions (
            code       VARCHAR(10) PRIMARY KEY,
            shares     INT NOT NULL,
            avg_cost   DOUBLE NOT NULL COMMENT '取得単価（片道コスト込み）',
            buy_date   DATE COMMENT '約定日',
            buy_reason TEXT,
            thesis     TEXT COMMENT '想定シナリオ・売却条件',
            created_at DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_orders (
            id           BIGINT AUTO_INCREMENT PRIMARY KEY,
            code         VARCHAR(10) NOT NULL,
            side         VARCHAR(4)  NOT NULL COMMENT 'buy/sell',
            budget       DOUBLE COMMENT 'buy: 予算円（株数は約定時の寄付値で決定）',
            shares       INT    COMMENT 'sell: 株数',
            reason       TEXT,
            thesis       TEXT,
            decided_date DATE NOT NULL COMMENT '意思決定日（この日の価格は使わない）',
            status       VARCHAR(10) DEFAULT 'pending' COMMENT 'pending/filled/expired',
            note         VARCHAR(200),
            created_at   DATETIME,
            INDEX idx_status (status)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_trades (
            id           BIGINT AUTO_INCREMENT PRIMARY KEY,
            code         VARCHAR(10) NOT NULL,
            side         VARCHAR(4)  NOT NULL,
            shares       INT NOT NULL,
            price        DOUBLE NOT NULL COMMENT '約定値（当日寄付）',
            fee          DOUBLE NOT NULL,
            trade_date   DATE NOT NULL,
            decided_date DATE,
            reason       TEXT,
            buy_reason   TEXT COMMENT 'sell時: 対応する購入理由（通しで読めるように）',
            pnl          DOUBLE COMMENT 'sell時: 実現損益（コスト込み）',
            pnl_pct      DOUBLE,
            hold_days    INT,
            created_at   DATETIME,
            INDEX idx_code (code), INDEX idx_date (trade_date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_nav (
            date       DATE PRIMARY KEY,
            nav        DOUBLE NOT NULL COMMENT '現金+時価評価',
            cash       DOUBLE NOT NULL,
            n_pos      INT,
            bench      DOUBLE COMMENT 'ベンチマーク(1306 adj_close)',
            market_view TEXT COMMENT 'AIの市況見解（decide時に更新）',
            created_at DATETIME
        )
    """)
    # 投資基準（毎晩AIが相場環境・成績を踏まえて更新。日次で蓄積し最新をサイト表示）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_policy (
            policy_date DATE PRIMARY KEY,
            statement   TEXT COMMENT 'その日時点の投資基準（明文）',
            created_at  DATETIME
        )
    """)
    # 控え（ベンチ）銘柄: 保有8に次ぐ候補8。日次で入替・蓄積
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_bench (
            bench_date DATE NOT NULL,
            rank_no    TINYINT NOT NULL,
            code       VARCHAR(10) NOT NULL,
            style      VARCHAR(10) DEFAULT '通常' COMMENT '通常/先回り',
            reason     TEXT,
            close_at   DOUBLE COMMENT '選定日の終値（以後のパフォーマンス計測用）',
            created_at DATETIME,
            PRIMARY KEY (bench_date, rank_no)
        )
    """)
    # 観点タグ別の実測エッジ（週次スナップショットで20営業日後リターンを実測。週1再計算）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_edge (
            view_tag     VARCHAR(20) PRIMARY KEY,
            horizon_days INT,
            n            INT,
            avg_ret      DOUBLE,
            med_ret      DOUBLE,
            win_rate     DOUBLE,
            computed_at  DATETIME
        )
    """)
    # 決算発表予定日（kabutanのfinanceページから取得・キャッシュ。決算跨ぎのリスク管理用）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS earnings_schedule (
            code          VARCHAR(10) PRIMARY KEY,
            announce_date DATE,
            fetched_at    DATETIME
        )
    """)
    # 既存テーブルへのカラム追加（初回マイグレーション）:
    #   style    = 投資スタイル（通常/先回り）
    #   catalyst = カタリスト（誰が・いつ・なぜ後から買ってくるかの明文化。買いの必須項目）
    #   strategy = スタイル分類コード（STRATEGIES のキー: A/R/E/T/C/V/L）
    for tbl in ("ai_fund_orders", "ai_fund_positions", "ai_fund_trades"):
        for coldef in ("style VARCHAR(10) DEFAULT '通常'", "catalyst TEXT", "strategy VARCHAR(4)"):
            try:
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {coldef}")
            except Exception:
                pass
    #   order_type = 'market'(翌営業日の寄付成行) / 'limit'(翌営業日の指値・1日限り)
    #   limit_price = 指値価格。trades にも約定種別として残す
    for tbl in ("ai_fund_orders", "ai_fund_trades"):
        for coldef in ("order_type VARCHAR(8) DEFAULT 'market'", "limit_price DOUBLE"):
            try:
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {coldef}")
            except Exception:
                pass
    conn.commit(); cur.close(); conn.close()


def _get_state(cur) -> dict | None:
    cur.execute("SELECT cash, inception_date, last_decided FROM ai_fund_state WHERE id = 1")
    r = cur.fetchone()
    return {"cash": float(r[0]), "inception": r[1], "last_decided": r[2]} if r else None


def _init_state(cur):
    cur.execute("""
        INSERT IGNORE INTO ai_fund_state (id, cash, inception_date, updated_at)
        VALUES (1, %s, CURDATE(), NOW())
    """, (INITIAL_CASH,))


def _latest_trading_date(cur) -> date | None:
    cur.execute("SELECT MAX(date) FROM daily_prices")
    return cur.fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# 約定処理（当日の寄付＝始値で pending 注文を執行）
# ─────────────────────────────────────────────────────────────────────────────

def execute_orders() -> int:
    """pending注文を「最新取引日」に約定する。決定日より後の取引日にのみ約定（先読み防止の要）。
    - order_type='market': 始値（寄付・成行）で約定。
    - order_type='limit' : 指値。買いは当日安値<=指値、売りは当日高値>=指値のときのみ約定
      （その価格に到達しなければ約定しない＝失効。1日限り＝翌営業日に再判断）。
    始値が無い銘柄（売買停止等）は pending のまま持ち越し。"""
    ensure_tables()
    conn = get_conn(); cur = conn.cursor()
    today = _latest_trading_date(cur)
    if today is None:
        cur.close(); conn.close(); return 0
    state = _get_state(cur)
    if state is None:
        cur.close(); conn.close(); return 0

    cur.execute("""
        SELECT id, code, side, budget, shares, reason, thesis, decided_date, style, catalyst, strategy,
               order_type, limit_price
        FROM ai_fund_orders WHERE status = 'pending' ORDER BY side DESC, id
    """)  # side DESC → sell を先に処理して現金を作ってから buy
    orders = cur.fetchall()
    filled = 0

    for oid, code, side, budget, shares, reason, thesis, decided, style, catalyst, strategy, order_type, limit_price in orders:
        if decided >= today:
            continue  # 決定日当日の価格では絶対に約定させない
        cur.execute("SELECT open, high, low, close, change_pct FROM daily_prices WHERE code = %s AND date = %s", (code, today))
        row = cur.fetchone()
        if not row or not row[0] or float(row[0]) <= 0:
            continue  # 当日値なし（売買停止等）→ 持ち越し
        open_px = float(row[0])
        high_px = float(row[1]) if row[1] else open_px
        low_px  = float(row[2]) if row[2] else open_px
        chg     = float(row[4]) if row[4] is not None else None

        # ── ストップ高/安の一本値張り付き（気配・比例配分）は約定不能として見送り ──
        # 高値==安値（日中レンジがゼロ＝一本値で張り付き）かつ大幅変動のとき、市場は
        # 一方通行で全員には割り当たらない。出来高があっても約定を仮定しない（成行でも失効）:
        #   ストップ高気配 → 買いは入れない（売りは需要に応じて売れる）
        #   ストップ安気配 → 売りは逃げられない（買いは供給に応じて買える）
        locked = (row[1] is not None and row[2] is not None
                  and high_px == low_px and chg is not None)
        if locked and side == "buy" and chg >= STOP_LOCK_PCT:
            cur.execute("UPDATE ai_fund_orders SET status='expired', note=%s WHERE id=%s",
                        (f"ストップ高気配で一本値張り付き（前日比{chg:+.0f}%）＝比例配分/気配のため買えず見送り", oid))
            print(f"  [失効] 買 {code}: ストップ高気配（前日比{chg:+.0f}%）で約定不能")
            continue
        if locked and side == "sell" and chg <= -STOP_LOCK_PCT:
            cur.execute("UPDATE ai_fund_orders SET status='expired', note=%s WHERE id=%s",
                        (f"ストップ安気配で一本値張り付き（前日比{chg:+.0f}%）＝売れず見送り（翌日再判断）", oid))
            print(f"  [失効] 売 {code}: ストップ安気配（前日比{chg:+.0f}%）で約定不能")
            continue

        # ── 約定価格の決定（指値は到達判定。未達なら失効） ──
        is_limit = (order_type == "limit") and limit_price and float(limit_price) > 0
        if is_limit:
            lp = float(limit_price)
            if side == "buy":
                # 寄付が指値以下＝より有利な寄付で約定 / 日中に指値まで下落＝指値で約定 / 届かず＝失効
                fill_px = open_px if open_px <= lp else (lp if low_px <= lp else None)
                reached = f"当日安値{low_px:,.0f}円"
            else:  # sell
                fill_px = open_px if open_px >= lp else (lp if high_px >= lp else None)
                reached = f"当日高値{high_px:,.0f}円"
            if fill_px is None:
                cur.execute("UPDATE ai_fund_orders SET status='expired', note=%s WHERE id=%s",
                            (f"指値{lp:,.0f}円に未達（{reached}）で失効", oid))
                print(f"  [失効] {'買' if side=='buy' else '売'} {code}: 指値{lp:,.0f}円未達（{reached}）")
                continue
        else:
            fill_px = open_px

        if side == "sell":
            cur.execute("SELECT shares, avg_cost, buy_date, buy_reason, style, strategy FROM ai_fund_positions WHERE code = %s", (code,))
            pos = cur.fetchone()
            if not pos:
                cur.execute("UPDATE ai_fund_orders SET status='expired', note='ポジションなし' WHERE id=%s", (oid,))
                continue
            p_shares, avg_cost, buy_date, buy_reason, p_style, p_strat = int(pos[0]), float(pos[1]), pos[2], pos[3], pos[4], pos[5]
            proceeds = p_shares * fill_px
            fee = proceeds * COST_RATE
            pnl = proceeds - fee - p_shares * avg_cost
            pnl_pct = (fill_px * (1 - COST_RATE) / avg_cost - 1) * 100
            hold_days = (today - buy_date).days if buy_date else None
            cur.execute("""
                INSERT INTO ai_fund_trades (code, side, shares, price, fee, trade_date, decided_date,
                                            reason, buy_reason, pnl, pnl_pct, hold_days, style, strategy,
                                            order_type, limit_price, created_at)
                VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (code, p_shares, fill_px, round(fee), today, decided, reason, buy_reason,
                  round(pnl), round(pnl_pct, 2), hold_days, p_style or "通常", p_strat,
                  order_type or "market", limit_price))
            cur.execute("DELETE FROM ai_fund_positions WHERE code = %s", (code,))
            state["cash"] += proceeds - fee
            cur.execute("UPDATE ai_fund_orders SET status='filled' WHERE id=%s", (oid,))
            filled += 1
            _ot = f"指値{float(limit_price):,.0f}" if is_limit else "成行"
            print(f"  [約定] 売 {code} {p_shares}株 @{fill_px:,.0f}({_ot}) 損益{pnl:+,.0f}円 ({pnl_pct:+.1f}%)")

        else:  # buy
            # ギャップガード（運営方針17）は成行買いのみ: 寄付が決定日終値の+15%超なら見送り。
            # 指値買いは fill_px が指値以下に抑えられ高値掴みが構造的に起きないため対象外。
            if not is_limit:
                cur.execute("SELECT close FROM daily_prices WHERE code = %s AND date = %s", (code, decided))
                rb = cur.fetchone()
                if rb and rb[0] and float(rb[0]) > 0 and open_px > float(rb[0]) * 1.15:
                    gap = (open_px / float(rb[0]) - 1) * 100
                    cur.execute("UPDATE ai_fund_orders SET status='expired', note=%s WHERE id=%s",
                                (f"寄付+{gap:.0f}%のギャップ（決定日終値比+15%超）で自動失効・高値掴み回避", oid))
                    print(f"  [失効] 買 {code}: 寄付{open_px:,.0f}円が決定日終値比+{gap:.0f}%（ギャップガード）")
                    continue
            budget = float(budget or 0)
            n = int(budget // (fill_px * 100)) * 100
            max_afford = int((state["cash"] / (1 + COST_RATE)) // (fill_px * 100)) * 100
            n = min(n, max_afford)
            if n <= 0:
                cur.execute("UPDATE ai_fund_orders SET status='expired', note='予算/現金内で100株単位が買えず' WHERE id=%s", (oid,))
                print(f"  [失効] 買 {code}: 約定値{fill_px:,.0f}円が予算内で買えず")
                continue
            amount = n * fill_px
            fee = amount * COST_RATE
            avg_cost = (amount + fee) / n
            cur.execute("""
                INSERT INTO ai_fund_positions (code, shares, avg_cost, buy_date, buy_reason, thesis, style, catalyst, strategy, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON DUPLICATE KEY UPDATE
                    avg_cost = (avg_cost*shares + VALUES(avg_cost)*VALUES(shares)) / (shares+VALUES(shares)),
                    shares = shares + VALUES(shares)
            """, (code, n, round(avg_cost, 2), today, reason, thesis, style or "通常", catalyst, strategy))
            cur.execute("""
                INSERT INTO ai_fund_trades (code, side, shares, price, fee, trade_date, decided_date, reason,
                                            style, catalyst, strategy, order_type, limit_price, created_at)
                VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (code, n, fill_px, round(fee), today, decided, reason, style or "通常", catalyst, strategy,
                  order_type or "market", limit_price))
            state["cash"] -= amount + fee
            cur.execute("UPDATE ai_fund_orders SET status='filled' WHERE id=%s", (oid,))
            filled += 1
            _ot = f"指値{float(limit_price):,.0f}" if is_limit else "成行"
            print(f"  [約定] 買 {code} {n}株 @{fill_px:,.0f}({_ot}) ({amount/1e4:,.0f}万円)")

    cur.execute("UPDATE ai_fund_state SET cash=%s, updated_at=NOW() WHERE id=1", (state["cash"],))
    conn.commit(); cur.close(); conn.close()
    if filled:
        print(f"  [AIファンド] {filled}件約定 / 現金残 {state['cash']/1e4:,.0f}万円")
    return filled


# ─────────────────────────────────────────────────────────────────────────────
# NAV記録（終値で時価評価）
# ─────────────────────────────────────────────────────────────────────────────

def record_nav() -> float | None:
    ensure_tables()
    conn = get_conn(); cur = conn.cursor()
    today = _latest_trading_date(cur)
    state = _get_state(cur)
    if today is None or state is None:
        cur.close(); conn.close(); return None
    cur.execute("SELECT code, shares FROM ai_fund_positions")
    pos = cur.fetchall()
    mv = 0.0
    for code, shares in pos:
        cur.execute("SELECT close FROM daily_prices WHERE code=%s AND date=%s", (code, today))
        r = cur.fetchone()
        if r and r[0]:
            mv += int(shares) * float(r[0])
        else:  # 当日値なし→直近値
            cur.execute("SELECT close FROM daily_prices WHERE code=%s AND date<=%s ORDER BY date DESC LIMIT 1", (code, today))
            r2 = cur.fetchone()
            if r2 and r2[0]:
                mv += int(shares) * float(r2[0])
    nav = state["cash"] + mv
    cur.execute("SELECT COALESCE(adj_close, close) FROM daily_prices WHERE code=%s AND date=%s", (BENCH_CODE, today))
    b = cur.fetchone()
    bench = float(b[0]) if b and b[0] else None
    cur.execute("""
        INSERT INTO ai_fund_nav (date, nav, cash, n_pos, bench, created_at)
        VALUES (%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE nav=VALUES(nav), cash=VALUES(cash), n_pos=VALUES(n_pos), bench=VALUES(bench)
    """, (today, round(nav), round(state["cash"]), len(pos), bench))
    conn.commit(); cur.close(); conn.close()
    print(f"  [AIファンド] NAV {nav/1e4:,.0f}万円（現金{state['cash']/1e4:,.0f}万・{len(pos)}銘柄）")
    return nav


# ─────────────────────────────────────────────────────────────────────────────
# 候補の定量スクリーニング（5観点）
# ─────────────────────────────────────────────────────────────────────────────

_CAND_FROM = """
    FROM price_stats p
    JOIN stocks s ON s.code = p.code AND s.is_active = 1
    LEFT JOIN sectors sec ON sec.id = s.sector_id
    LEFT JOIN stock_fundamentals f ON f.code = p.code
    LEFT JOIN theoretical_values t ON t.code = p.code
    WHERE p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
      AND f.market_cap >= 15e9
"""
_CAND_SEL = """
    SELECT p.code, s.name, p.close, p.chg5d, p.chg25d, p.chg75d, p.rsi14,
           p.dev_ma25, p.dev_high52w, p.vol20_ratio, p.turnover_20d,
           p.ma200_slope, p.break_65d, f.per, f.roe, f.market_cap,
           t.theo_ratio, t.upside_3y_pct, p.rev_growth, p.op_growth, sec.name,
           p.fscore
"""
_CAND_COLS = ["code", "name", "close", "chg5d", "chg25d", "chg75d", "rsi14", "dev_ma25",
              "dev_high52w", "vol20_ratio", "turnover_20d", "ma200_slope", "break_65d",
              "per", "roe", "market_cap", "theo_ratio", "upside_3y_pct", "rev_growth", "op_growth",
              "sector", "fscore"]


def _cconv(x):
    from decimal import Decimal
    return float(x) if isinstance(x, Decimal) else x


def _candidates_one(cur, code: str) -> dict | None:
    """1銘柄分の候補メトリクスを取得（控え銘柄の合流用）。流動性等の足切りも適用される。"""
    cur.execute(_CAND_SEL + _CAND_FROM.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip(_CAND_COLS, [_cconv(x) for x in r]))
    d["event"] = None
    return d


def _candidates(cur, exclude: set[str], regime_up: bool = True) -> list[dict]:
    """観点別に候補を集めて重複排除。exclude（保有中・再購入クールダウン中）は除外。
    regime_up=False（TOPIXが200日線割れ）のときはモメンタム系ビュー(rs_leader/theme_leader)を出さない
    （実測: 地合い下ではRS戦略の勝率24%・平均マイナス）。"""
    base_from = _CAND_FROM
    sel = _CAND_SEL
    # RSI上限は全ビュー85で統一（実測: RSI78-85帯もプラス、85以上は全スタイルで期待値マイナス）
    views = [
        ("momentum", sel + base_from + """
            AND p.chg25d >= 10 AND p.rsi14 < 85 AND p.close > p.ma25 AND p.chg5d > -4
            ORDER BY p.chg25d DESC LIMIT 8"""),
        # dip: 検証済みプレイブックC — RSI調整後の「反発確認」(chg5d>2)を必須にする
        ("dip", sel + base_from + """
            AND p.ma200_slope > 0 AND p.close > p.ma200 AND p.rsi14 < 45
            AND p.chg5d > 2 AND p.chg25d > -12
            ORDER BY p.rsi14 ASC LIMIT 6"""),
        # breakout: 検証済みプレイブックA — 52週高値圏×出来高1.3倍（65日高値より52週が有効と実測）
        ("breakout", sel + base_from + """
            AND p.dev_high52w >= -1 AND p.vol20_ratio >= 1.3 AND p.rsi14 < 85
            ORDER BY p.vol20_ratio DESC LIMIT 8"""),
        # value_growth: 割安×成長に「クオリティ足切り(F-score>=5)」を追加。
        # 財務健全性の低い"万年割安株(バリュートラップ)"を除外する（F-score 8-9で年率+13%超過の実証）。
        # fscore が NULL（算出不能）の銘柄は従来どおり通す（データ欠損で機会を潰さない）。
        ("value_growth", sel + base_from + """
            AND t.theo_ratio >= 1.25 AND p.op_growth > 5 AND p.chg25d > -5 AND p.rsi14 < 70
            AND (p.fscore IS NULL OR p.fscore >= 5)
            ORDER BY t.upside_3y_pct DESC LIMIT 6"""),
    ]
    # rs_leader: 検証済みプレイブックR — 6ヶ月騰落率の市場上位10%×52週高値-10%圏×MA25上。
    # 閾値（上位10%ライン）は毎回その日の市場分布から実測する（固定値は分布変動で崩れるため）。
    # 並び順はdev_high52w（高値への近さ）: chg126d順だと極端な最上位(97pct超・中央値ゼロと実測)に偏るのを避ける
    if regime_up:
        cur.execute("SELECT COUNT(*)" + base_from + " AND p.chg126d IS NOT NULL")
        n_univ = int(cur.fetchone()[0])
        if n_univ >= 300:
            cur.execute("SELECT p.chg126d" + base_from +
                        " AND p.chg126d IS NOT NULL ORDER BY p.chg126d DESC LIMIT 1 OFFSET %s",
                        (n_univ // 10,))
            r = cur.fetchone()
            if r and r[0] is not None:
                views.append(("rs_leader", sel + base_from + f"""
                    AND p.chg126d >= {float(r[0]):.2f} AND p.dev_high52w >= -10
                    AND p.close > p.ma25 AND p.chg5d > -4 AND p.rsi14 < 85
                    ORDER BY p.dev_high52w DESC LIMIT 8"""))
    _conv = _cconv
    cands: dict[str, dict] = {}
    cols = _CAND_COLS
    for tag, q in views:
        cur.execute(q)
        for r in cur.fetchall():
            d = dict(zip(cols, [_conv(x) for x in r]))
            code = d["code"]
            if code in exclude:
                continue
            if code in cands:
                if tag not in cands[code]["tags"]:
                    cands[code]["tags"].append(tag)
            else:
                d["tags"] = [tag]
                d["event"] = None
                cands[code] = d

    # 観点5: 直近の上方修正・増配イベント（発表済み・reaction_date以降のみ＝先読みなし）
    cur.execute("""
        SELECT r.code, s.name, r.direction, r.op_chg_pct, r.dps_old, r.dps_new, r.announced_at
        FROM forecast_revisions r
        JOIN stocks s ON s.code = r.code AND s.is_active = 1
        JOIN price_stats p ON p.code = r.code
        LEFT JOIN stock_fundamentals f ON f.code = r.code
        WHERE r.announced_at >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
          AND r.reaction_date <= (SELECT MAX(date) FROM daily_prices)
          AND (r.direction = 'up' OR (r.dps_new > r.dps_old))
          AND p.chg5d > -3  -- 初動が崩れた銘柄は除外（プレイブックE: 発表後のポジティブな流れに乗る）
          AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND COALESCE(f.market_cap, 0) >= 15e9
        ORDER BY r.announced_at DESC LIMIT 8
    """)
    ev_rows = cur.fetchall()
    for code, name, direction, op_chg, dps_old, dps_new, ann in ev_rows:
        if code in exclude:
            continue
        label = []
        if direction == "up":
            label.append(f"上方修正(営業益{f'{float(op_chg):+.0f}%' if op_chg is not None else ''})")
        if dps_new and dps_old and float(dps_new) > float(dps_old):
            label.append(f"増配{float(dps_old):.0f}→{float(dps_new):.0f}円")
        ev = f"{str(ann)[:10]} {'・'.join(label)}"
        if code in cands:
            if "event" not in cands[code]["tags"]:
                cands[code]["tags"].append("event")
            cands[code]["event"] = ev
        else:
            cur.execute(sel + base_from.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
            r = cur.fetchone()
            if r:
                d = dict(zip(cols, [_conv(x) for x in r]))
                d["tags"] = ["event"]; d["event"] = ev
                cands[code] = d

    # 観点5b: 好業績ドリフト（検証済みプレイブックE） — TDnet開示（決算短信・業績/配当修正等）の後、
    # 市場の初動反応がポジティブ（5日+6〜18%）かつ52週高値-15%圏の銘柄。
    # DATE(disclosed_at) < 最新取引日 の条件で「反応日の終値が出てから」に限定（先読みなし）。
    # 実測(プロキシ): 初動+7〜15%×高値圏で平均+11.8%/勝率57%。+15%超の飛び乗りは期待値劣化のため上限18%
    cur.execute("""
        SELECT d.code, MAX(DATE(d.disclosed_at)) AS ddate,
               SUBSTRING_INDEX(GROUP_CONCAT(d.title ORDER BY d.disclosed_at DESC SEPARATOR '||'), '||', 1)
        FROM disclosures d
        JOIN stocks s ON s.code = d.code AND s.is_active = 1
        JOIN price_stats p ON p.code = d.code
        LEFT JOIN stock_fundamentals f ON f.code = d.code
        WHERE d.category IN ('earnings_report','earnings_rev','earnings_up','guidance','div_up','dividend_rev')
          AND d.disclosed_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
          AND DATE(d.disclosed_at) < (SELECT MAX(date) FROM daily_prices)
          AND p.chg5d BETWEEN 6 AND 18 AND p.dev_high52w >= -15 AND p.rsi14 < 85
          AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND COALESCE(f.market_cap, 0) >= 15e9
        GROUP BY d.code
        ORDER BY MAX(d.disclosed_at) DESC LIMIT 8
    """)
    for code, ddate, title in cur.fetchall():
        if code in exclude:
            continue
        note = f"{ddate} 開示「{(title or '')[:36]}」後の初動ポジティブ（好業績ドリフト候補）"
        if code in cands:
            if "event" not in cands[code]["tags"]:
                cands[code]["tags"].append("event")
            cands[code]["event"] = cands[code].get("event") or note
        else:
            cur.execute(sel + base_from.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
            r = cur.fetchone()
            if r:
                d = dict(zip(cols, [_conv(x) for x in r]))
                d["tags"] = ["event"]; d["event"] = note
                cands[code] = d

    # 観点5c: 自社株買い（バイバック＝需給改善カタリスト・提案D）。
    # 「取得の決定／立会外買付（ToSTNeT-3）」＝新規発表のみを拾い、「取得状況／終了」の途中経過は除外。
    # 発行済株式を市場から吸収する需給改善で、発表後に見直し買いが入りやすい（需給アノマリー）。
    # 反応日の終値確定後（先読みなし）・下落継続銘柄は除外。
    cur.execute("""
        SELECT d.code, MAX(DATE(d.disclosed_at)) AS ddate,
               SUBSTRING_INDEX(GROUP_CONCAT(d.title ORDER BY d.disclosed_at DESC SEPARATOR '||'), '||', 1)
        FROM disclosures d
        JOIN stocks s ON s.code = d.code AND s.is_active = 1
        JOIN price_stats p ON p.code = d.code
        LEFT JOIN stock_fundamentals f ON f.code = d.code
        WHERE d.category = 'buyback'
          AND (d.title LIKE '%取得に係る事項の決定%' OR d.title LIKE '%買付%')
          AND d.title NOT LIKE '%状況%' AND d.title NOT LIKE '%終了%'
          AND d.disclosed_at >= DATE_SUB(CURDATE(), INTERVAL 20 DAY)
          AND DATE(d.disclosed_at) < (SELECT MAX(date) FROM daily_prices)
          -- バイバックは需給フロア型（初動スパイク前提のEと異なる）。明確な下落トレンドのみ除外し過熱は避ける
          AND p.chg25d > -12 AND p.rsi14 < 78
          AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND COALESCE(f.market_cap, 0) >= 15e9
        GROUP BY d.code
        ORDER BY MAX(d.disclosed_at) DESC LIMIT 6
    """)
    for code, ddate, title in cur.fetchall():
        if code in exclude:
            continue
        note = f"{ddate} 自社株買い発表（需給改善カタリスト）"
        if code in cands:
            if "buyback" not in cands[code]["tags"]:
                cands[code]["tags"].append("buyback")
            cands[code]["event"] = cands[code].get("event") or note
        else:
            cur.execute(sel + base_from.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
            r = cur.fetchone()
            if r:
                d = dict(zip(cols, [_conv(x) for x in r]))
                d["tags"] = ["buyback"]; d["event"] = note
                cands[code] = d

    # 観点5d: 本日引け後の好開示（株価未反応・翌朝の寄付で入る「引け後プレイ」）。
    # decide() はイブニング便(20:30)で disclosures/earnings_refresh が当日の引け後開示を
    # 取り込んだ後に走るため、この時点で市場はまだ反応していない（反応は明日の寄付から）。
    # 一次情報は定量化済みの上方修正(forecast_revisions)。ギャップ高値掴みは
    # execute_orders のギャップガード（寄付が決定日終値+15%超なら自動失効）が防ぐ。
    cur.execute("""
        SELECT r.code, r.op_chg_pct, r.dps_old, r.dps_new
        FROM forecast_revisions r
        JOIN stocks s ON s.code = r.code AND s.is_active = 1
        JOIN price_stats p ON p.code = r.code
        LEFT JOIN stock_fundamentals f ON f.code = r.code
        WHERE r.announced_at = (SELECT MAX(date) FROM daily_prices)
          AND r.reaction_date > (SELECT MAX(date) FROM daily_prices)
          AND r.direction = 1
          AND p.rsi14 < 85 AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND COALESCE(f.market_cap, 0) >= 15e9
        ORDER BY r.op_chg_pct DESC LIMIT 5
    """)
    for code, op_chg, dps_old, dps_new in cur.fetchall():
        if code in exclude:
            continue
        label = []
        if op_chg is not None:
            label.append(f"営業益{float(op_chg):+.0f}%")
        if dps_new and dps_old and float(dps_new) > float(dps_old):
            label.append(f"増配{float(dps_old):.0f}→{float(dps_new):.0f}円")
        note = (f"本日引け後に上方修正({'・'.join(label) or '内容は開示参照'})。"
                "株価は未反応＝明朝ギャップの可能性（寄付が+15%超なら自動見送り）")
        if code in cands:
            if "fresh_event" not in cands[code]["tags"]:
                cands[code]["tags"].append("fresh_event")
            cands[code]["event"] = note
        else:
            cur.execute(sel + base_from.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
            r = cur.fetchone()
            if r:
                d = dict(zip(cols, [_conv(x) for x in r]))
                d["tags"] = ["fresh_event"]; d["event"] = note
                cands[code] = d

    # 観点5e: 決算プレビュー — 発表7日以内×進捗率が単純按分+20pt超の「上振れ候補」（決算跨ぎ用）。
    # 跨ぎはプレイブックで「根拠の明文化」が条件。進捗率超過がそのカタリストになる。
    cur.execute("""
        SELECT es.code, es.announce_date
        FROM earnings_schedule es
        JOIN stocks s ON s.code = es.code AND s.is_active = 1
        JOIN price_stats p ON p.code = es.code
        LEFT JOIN stock_fundamentals f ON f.code = es.code
        WHERE es.announce_date BETWEEN %s AND %s
          AND p.rsi14 < 85 AND p.chg25d > -10
          AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND COALESCE(f.market_cap, 0) >= 15e9
    """, (date.today() + timedelta(days=1), date.today() + timedelta(days=7)))
    sched = {r[0]: r[1] for r in cur.fetchall()}
    if sched:
        prog_all = _progress_batch(cur, list(sched.keys()))
        ups = sorted((c for c, pg in prog_all.items() if pg["excess"] >= 20),
                     key=lambda c: -prog_all[c]["excess"])
        for code in ups[:5]:
            if code in exclude:
                continue
            pg = prog_all[code]; ann = sched[code]
            note = (f"決算発表{ann.month}/{ann.day}({(ann - date.today()).days}日後)・"
                    f"進捗率{pg['progress']:.0f}%(按分{pg['prorata']}%・+{pg['excess']:.0f}pt)＝上振れ候補。"
                    "跨ぐならこの進捗率をカタリストとして明文化すること")
            if code in cands:
                if "preview" not in cands[code]["tags"]:
                    cands[code]["tags"].append("preview")
                cands[code]["event"] = cands[code].get("event") or note
            else:
                cur.execute(sel + base_from.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
                r = cur.fetchone()
                if r:
                    d = dict(zip(cols, [_conv(x) for x in r]))
                    d["tags"] = ["preview"]; d["event"] = note
                    cands[code] = d

    # 観点6: 資金流入テーマ内の「出遅れ」（先回り投資の材料）。
    # 「テーマ内のAが買われた→出遅れているBに資金が波及する」という一歩先の予測用。
    # テーマは規模足切り（売買代金100億+・8銘柄+）でノイズを除外（/flowsと同じ基準）
    cur.execute("""
        SELECT group_key, zscore FROM money_flow_weekly
        WHERE group_type = 'theme' AND flow_class = 'inflow'
          AND week_end = (SELECT MAX(week_end) FROM money_flow_weekly)
          AND turnover >= 100 AND n_stocks >= 8
        ORDER BY zscore DESC LIMIT 5
    """)
    for theme, z in cur.fetchall():
        # テーマ所属は統一マスタ(みんかぶ・tier>=2)。kabutan_themesは2026-07廃止
        cur.execute(
            sel + base_from.replace(
                "WHERE",
                "WHERE p.code IN (SELECT tm.code FROM theme_members tm "
                "JOIN themes t ON t.id = tm.theme_id WHERE t.name = %s AND tm.tier >= 2) AND", 1) + """
            AND p.chg25d < 12 AND p.chg5d > -5 AND p.ma200_slope >= 0 AND p.rsi14 < 65
            ORDER BY p.turnover_20d DESC LIMIT 3
        """, (theme,))
        for r in cur.fetchall():
            d = dict(zip(cols, [_conv(x) for x in r]))
            code = d["code"]
            if code in exclude:
                continue
            note = f"資金流入テーマ「{theme}」(Z={float(z):.1f})内でまだ上がっていない出遅れ"
            if code in cands:
                if "laggard" not in cands[code]["tags"]:
                    cands[code]["tags"].append("laggard")
                cands[code]["event"] = cands[code].get("event") or note
            else:
                d["tags"] = ["laggard"]; d["event"] = note
                cands[code] = d

    # 観点7: 強いテーマの「主役」（検証済みプレイブックT）。テーマ内RS上位のfwd20は+6.4%と
    # 出遅れ(+3.7%)の約2倍と実測。テーマ強度は株価ベース（平均25日騰落+10%以上・メンバー8以上）。
    # レジームゲート対象（モメンタム系のため）
    if regime_up:
        # テーマ所属は統一マスタ(みんかぶ・tier>=2)。kabutan_themesは2026-07廃止
        cur.execute("""
            SELECT t.name, AVG(p.chg25d) AS t_mom, COUNT(*) AS n
            FROM theme_members tm
            JOIN themes t ON t.id = tm.theme_id AND t.status = 'active'
            JOIN stocks s ON s.code = tm.code AND s.is_active = 1
            JOIN price_stats p ON p.code = tm.code
            WHERE tm.tier >= 2 AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
            GROUP BY t.id, t.name HAVING n >= 8 AND t_mom >= 10
            ORDER BY t_mom DESC LIMIT 5
        """)
        for theme, t_mom, _n in cur.fetchall():
            cur.execute(
                sel + base_from.replace(
                    "WHERE",
                    "WHERE p.code IN (SELECT tm.code FROM theme_members tm "
                    "JOIN themes t ON t.id = tm.theme_id WHERE t.name = %s AND tm.tier >= 2) AND", 1) + """
                AND p.close > p.ma25 AND p.chg5d > -4 AND p.rsi14 < 85
                ORDER BY p.chg25d DESC LIMIT 3
            """, (theme,))
            for r in cur.fetchall():
                d = dict(zip(cols, [_conv(x) for x in r]))
                code = d["code"]
                if code in exclude:
                    continue
                note = f"強テーマ「{theme}」(平均25日{float(t_mom):+.0f}%)のテーマ内RS上位＝主役"
                if code in cands:
                    if "theme_leader" not in cands[code]["tags"]:
                        cands[code]["tags"].append("theme_leader")
                    cands[code]["event"] = cands[code].get("event") or note
                else:
                    d["tags"] = ["theme_leader"]; d["event"] = note
                    cands[code] = d
    return list(cands.values())[:44]


# 観点タグ→過去スナップショットでの再現条件（price_stats_historyの列で表現できるもののみ。
# 「AIの語るエッジ」を実測値で裏付ける／反証するための検証基盤）
EDGE_VIEWS = {
    "momentum": "chg25d >= 10 AND rsi14 < 85 AND close > ma25 AND chg5d > -4",
    "dip":      "ma200_slope > 0 AND close > ma200 AND rsi14 < 45 AND chg5d > 2 AND chg25d > -12",
    "breakout": "dev_high52w >= -1 AND vol20_ratio >= 1.3 AND rsi14 < 85",
    # rs_leaderの本来の定義は「chg126dが市場上位10%」。スナップショットSQLでは百分位が引けないため
    # 固定値で近似（上位10%ラインの実測平均≈56%・最小29%。下限側に寄せて40%）
    "rs_leader": "chg126d >= 40 AND dev_high52w >= -10 AND close > ma25 AND chg5d > -4 AND rsi14 < 85",
    # quality: 財務健全性F-score上位。自前実測でF7は+3.4%/20日・勝率60%（F0-2は+0.5%）と単調なエッジ
    "quality": "fscore >= 6",
    "baseline": "1 = 1",  # 全銘柄平均（比較基準）
}
EDGE_HORIZON = 20  # 営業日


def _refresh_edge_stats(cur, conn, force: bool = False) -> None:
    """観点タグ別の実測エッジを週次スナップショット×20営業日後リターンで計測し
    ai_fund_edge に保存する（週1回再計算）。流動性・価格帯は候補生成と同じ足切り。"""
    cur.execute("SELECT MAX(computed_at) FROM ai_fund_edge")
    r = cur.fetchone()
    if not force and r and r[0] and (datetime.now() - r[0]).days < 7:
        return

    print("  [AIファンド] 観点タグ別エッジを再計測中（週次）...")
    cur.execute("SELECT DISTINCT date FROM daily_prices WHERE date >= '2024-10-01' ORDER BY date")
    tdays = [row[0] for row in cur.fetchall()]
    tidx = {d: i for i, d in enumerate(tdays)}
    cur.execute("SELECT DISTINCT snapshot_date FROM price_stats_history ORDER BY snapshot_date")
    snaps = [row[0] for row in cur.fetchall()]

    for tag, cond in EDGE_VIEWS.items():
        rets: list[float] = []
        for d0 in snaps:
            i = tidx.get(d0)
            if i is None or i + EDGE_HORIZON >= len(tdays):
                continue
            d2 = tdays[i + EDGE_HORIZON]
            cur.execute(f"""
                SELECT h.code, h.close FROM price_stats_history h
                WHERE h.snapshot_date = %s AND h.turnover_20d >= 3
                  AND h.close BETWEEN 300 AND 24000 AND {cond}
            """, (d0,))
            rows = cur.fetchall()
            if not rows:
                continue
            codes = [row[0] for row in rows]
            px0 = {row[0]: float(row[1]) for row in rows}
            fmt = ",".join(["%s"] * len(codes))
            cur.execute(f"""
                SELECT code, COALESCE(adj_close, close) FROM daily_prices
                WHERE date = %s AND code IN ({fmt})
            """, [d2] + codes)
            for code, px2 in cur.fetchall():
                if px2 and px0.get(code, 0) > 0:
                    rets.append((float(px2) / px0[code] - 1) * 100)
        if not rets:
            continue
        rets.sort()
        n = len(rets)
        cur.execute("""
            INSERT INTO ai_fund_edge (view_tag, horizon_days, n, avg_ret, med_ret, win_rate, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON DUPLICATE KEY UPDATE horizon_days=VALUES(horizon_days), n=VALUES(n),
                avg_ret=VALUES(avg_ret), med_ret=VALUES(med_ret),
                win_rate=VALUES(win_rate), computed_at=NOW()
        """, (tag, EDGE_HORIZON, n, round(sum(rets) / n, 2), round(rets[n // 2], 2),
              round(sum(1 for x in rets if x > 0) / n * 100, 1)))
        print(f"    {tag}: 平均{sum(rets)/n:+.2f}% 中央値{rets[n//2]:+.2f}% 勝率{sum(1 for x in rets if x>0)/n*100:.0f}% (n={n})")
    conn.commit()


def _edge_summary(cur) -> str:
    """プロンプト注入用のエッジ実測サマリー。"""
    cur.execute("SELECT view_tag, horizon_days, n, avg_ret, med_ret, win_rate FROM ai_fund_edge")
    rows = cur.fetchall()
    if not rows:
        return ""
    parts = []
    for tag, hz, n, avg, med, wr in sorted(rows, key=lambda r: r[0] != "baseline"):
        label = "全銘柄平均" if tag == "baseline" else tag
        parts.append(f"{label}: 平均{float(avg):+.1f}%/中央値{float(med):+.1f}%/勝率{float(wr):.0f}%(n={n})")
    return f"当サイトの週次スナップショットで実測した{rows[0][1]}営業日後リターン（過去約1年半） — " + " ／ ".join(parts)


def _progress_note(cur, code: str) -> str:
    """通期会社予想に対する営業益の進捗率（コンセンサス不在の代替指標）。
    例: ' 進捗率:営業益54%(Q2終了・単純按分50%)' — 按分超なら上振れ気配。"""
    try:
        cur.execute("""
            SELECT period_end, operating_income FROM financials
            WHERE code = %s AND period_type = 'A' AND period_end > CURDATE()
            ORDER BY period_end LIMIT 1
        """, (code,))
        fc = cur.fetchone()
        if not fc or not fc[1] or float(fc[1]) <= 0:
            return ""
        fy_end, op_fc = fc[0], float(fc[1])
        cur.execute("""
            SELECT COUNT(*), SUM(operating_income) FROM financials
            WHERE code = %s AND period_type = 'Q'
              AND period_end > DATE_SUB(%s, INTERVAL 1 YEAR) AND period_end <= CURDATE()
              AND operating_income IS NOT NULL
        """, (code, fy_end))
        n_q, op_sum = cur.fetchone()
        if not n_q or n_q == 0 or n_q >= 4 or op_sum is None:
            return ""
        progress = float(op_sum) / op_fc * 100
        return f" 進捗率:営業益{progress:.0f}%(Q{n_q}終了・単純按分{n_q*25}%)"
    except Exception:
        return ""


def _progress_batch(cur, codes: list[str]) -> dict:
    """複数銘柄の通期営業益予想に対する進捗率をまとめて計算する（_progress_noteのバッチ版）。
    /earnings ページ(app.py)と観点5e（決算上振れ跨ぎ候補）で共用。
    返り値: {code: {"progress": %, "prorata": %, "n_q": int, "excess": pt}}"""
    if not codes:
        return {}
    today = date.today()
    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT code, period_end, operating_income FROM financials
        WHERE period_type='A' AND period_end > %s AND code IN ({ph})
          AND operating_income IS NOT NULL
        ORDER BY code, period_end
    """, (today, *codes))
    fc = {}
    for c, pe, oi in cur.fetchall():
        if c not in fc and float(oi) > 0:
            fc[c] = (pe, float(oi))
    if not fc:
        return {}
    cur.execute(f"""
        SELECT code, period_end, operating_income FROM financials
        WHERE period_type='Q' AND period_end <= %s AND code IN ({ph})
          AND operating_income IS NOT NULL
        ORDER BY code, period_end
    """, (today, *codes))
    qs: dict = {}
    for c, pe, oi in cur.fetchall():
        qs.setdefault(c, []).append((pe, float(oi)))
    out = {}
    for c, (fy_end, op_fc) in fc.items():
        try:
            fy_start = fy_end.replace(year=fy_end.year - 1)
        except ValueError:   # 2/29期末のうるう日
            fy_start = fy_end.replace(year=fy_end.year - 1, day=28)
        rows = [(pe, oi) for pe, oi in qs.get(c, []) if fy_start < pe <= today]
        n_q = len(rows)
        if n_q == 0 or n_q >= 4:
            continue
        progress = sum(oi for _pe, oi in rows) / op_fc * 100
        out[c] = {"progress": progress, "prorata": n_q * 25, "n_q": n_q,
                  "excess": progress - n_q * 25}
    return out


def _earnings_dates(cur, conn, codes: list[str]) -> dict:
    """各銘柄の次回決算発表予定日を earnings_schedule から返す。
    earnings_schedule は JPX公式の決算発表予定日Excel（earnings_calendar_jpx）で
    日次更新される。決算跨ぎはイベントリスク/カタリストの両面で意思決定に必須。
    （旧実装はkabutanスクレイプだったが、データセンターIP遮断のためJPX公式に置換）"""
    if not codes:
        return {}
    fmt = ",".join(["%s"] * len(codes))
    # 日付比較はJST(Python)側。DBのCURDATE()はUTCで日本と最大9時間ズレるため使わない
    cur.execute(f"""
        SELECT code, announce_date FROM earnings_schedule
        WHERE code IN ({fmt}) AND announce_date >= %s
    """, (*codes, date.today()))
    return {r[0]: r[1] for r in cur.fetchall()}


def _earn_note(earn_map: dict, code: str) -> str:
    """プロンプト用の決算予定表記（45日以内のみ）。例: ' 決算発表:07/15(3日後)'"""
    d = earn_map.get(code)
    if not d:
        return ""
    days = (d - date.today()).days
    if days < 0 or days > 45:
        return ""
    return f" 決算発表:{d.month:02d}/{d.day:02d}({days}日後)"


def _cooldown_codes(cur) -> set[str]:
    cur.execute("""
        SELECT DISTINCT code FROM ai_fund_trades
        WHERE side='sell' AND trade_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
    """, (REBUY_COOLDOWN,))
    return {r[0] for r in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────
# 意思決定（Gemini + ガードレール）
# ─────────────────────────────────────────────────────────────────────────────

def _fnum(v, nd=1):
    return "-" if v is None else f"{float(v):.{nd}f}"


def _build_prompt(state, positions, cands, market_ctx, n_slots, est_cash,
                  policy_prev, feedback, inflow_themes, movers, earn_map=None,
                  edge_line="", prog_map=None) -> str:
    earn_map = earn_map or {}
    prog_map = prog_map or {}
    pos_lines = []
    for p in positions:
        style_tag = f"[{p.get('style') or '通常'}] " if p.get("style") == "先回り" else ""
        strat = p.get("strategy")
        if strat and strat in STRATEGIES:
            style_tag = f"[{strat}:{STRATEGIES[strat]['name']}] " + style_tag
        pos_lines.append(
            f"- {style_tag}{p['code']} {p['name']}: 取得{p['avg_cost']:,.0f}円({str(p['buy_date'])}) 現在{p['close']:,.0f}円 "
            f"損益{p['pnl_pct']:+.1f}% 保有{p['hold_days']}日 RSI{_fnum(p['rsi14'],0)} 5日{_fnum(p['chg5d'])}% 25日{_fnum(p['chg25d'])}%"
            f"{_earn_note(earn_map, p['code'])}\n"
            f"  購入理由: {p['buy_reason']}\n  カタリスト: {p.get('catalyst') or '（未記録）'}\n  シナリオ: {p['thesis']}"
        )
    cand_lines = []
    for c in cands:
        extra = f" 補足:{c['event']}" if c.get("event") else ""
        sec = f" 業種:{c['sector']}" if c.get("sector") else ""
        cand_lines.append(
            f"- {c['code']} {c['name']} [{'/'.join(c['tags'])}]{sec} 株価{c['close']:,.0f}円 "
            f"5日{_fnum(c['chg5d'])}% 25日{_fnum(c['chg25d'])}% 75日{_fnum(c['chg75d'])}% RSI{_fnum(c['rsi14'],0)} "
            f"52週高値比{_fnum(c['dev_high52w'])}% 出来高比{_fnum(c['vol20_ratio'])}x 売買代金{_fnum(c['turnover_20d'],0)}億 "
            f"PER{_fnum(c['per'])} ROE{_fnum(c['roe'])}% 理論株価比{_fnum(c['theo_ratio'],2)} 営業益成長{_fnum(c['op_growth'],0)}%"
            f"{(' F-score' + str(c['fscore']) + '/7') if c.get('fscore') is not None else ''}"
            f"{_earn_note(earn_map, c['code'])}{prog_map.get(c['code'], '')}{extra}"
        )
    return f"""あなたは日本株のファンドマネージャーです。模擬ファンドを運用しています。

# 運用ルール（厳守）
- 目的: キャピタルゲインの最大化。投資期間の目安は数日〜数ヶ月（トレンドとカタリストが生きていれば最長9ヶ月）
- 常に{N_POSITIONS}銘柄を保有する。今回は売りと買いをセットで考え、決定後の保有数が{N_POSITIONS}になるようにする
- 今回の買い枠: {n_slots}銘柄（売却を指示すればその分増える。1日の入替は最大{MAX_SWAPS}銘柄まで）
- 予算: 1銘柄 {BUDGET_MIN//10000}万〜{BUDGET_MAX//10000}万円。買い予算の合計は約{est_cash/10000:,.0f}万円以内
- 約定方法は各売買で選べる: order_type="成行"（翌営業日の寄付で確実に約定）か "指値"（limitに価格）。
  指値は「翌営業日にその価格へ到達すれば約定・届かなければその注文は失効し翌日また判断」の1日限り。
  買い指値=押し目を待って安く拾う（現値よりやや下）／売り指値=戻り・目標値まで待って売る（現値よりやや上）。
  確実に建てたい・急ぐときは成行、価格に固執したいときは指値。指値の数値は現値から乖離しすぎないこと
- 売買理由は具体的に（何を根拠に・何を期待して・どうなったら降りるか）。理由の水増しや創作は禁止
- **全ての買いに catalyst（カタリスト）を必ず書く**: 自分が買った後に「誰が・いつ・なぜ買い上げてくるのか」。
  例: 「直近の上方修正で機関投資家の見直し買いが入る局面」「65日高値ブレイク銘柄はトレンド追随の買いを
  呼びやすい」「テーマXに資金流入中だが本銘柄はそのXに不可欠な部材でまだ物色が及んでいない」。
  提供データから言えることだけを書き、無いイベントを創作しない。カタリストが書けない銘柄は買わない
- 各買いに style を付ける: "通常" または "先回り"（=まだ上がっていないが、資金波及・技術トレンドの読みで
  次に買われると**予測**する銘柄。laggardタグ等）。先回りは予測であることを理由に明記し、保有は最大{MAX_ANTICIPATE}銘柄まで
- **各買いに strategy（投資スタイル分類）を必ず1つ付ける**: {" / ".join(f'{k}={v["name"]}' for k, v in STRATEGIES.items())}。
  購入理由の主軸に最も合うものを選ぶ（サイトに常時表示され、スタイル別に成績検証される）
- **決算発表日（表記がある銘柄）を必ず考慮する**: 決算を跨ぐなら「決算が上振れするとみて跨ぐ（＝それがカタリスト）」か
  「決算前に手仕舞う/買わない」かを理由・シナリオに明記。無自覚に決算を跨ぐことを禁止する
- 分散: 同一業種・同一テーマの保有は最大3銘柄まで。カタリストの種類（イベント/トレンド/割安見直し/先回り）も分散させる

# 前回までの投資基準（あなた自身が書いたもの。継続性を保ちつつ、環境変化の根拠があれば更新する）
{policy_prev or '（初回のためまだ無い。今日の環境から初版を書くこと）'}

# 直近の成績フィードバック（何が効いて何が外れたか。基準の更新材料にする）
{feedback or '（まだ売買実績なし）'}

# 今週の相場環境
{market_ctx}
- 資金流入テーマ: {inflow_themes or '—'}
- 直近1週間の上昇上位: {movers or '—'}

# 検証済みプレイブック（過去2.5年・全シグナル機械検証。売買判断とエグジット設計はこれに沿わせる）
{PLAYBOOK}

# 観点タグ別の実測エッジ（週次で自動再計測される監視値。カタリストや投資基準で統計を語るときは、
# この実測値かプレイブックの数値を引用する。数値の創作は厳禁）
{edge_line or '（計測データなし）'}
※候補行の「進捗率」= 通期会社予想の営業益に対する四半期累計の消化率。単純按分を大きく超えていれば上方修正の素地
※候補行の「F-score」= 財務健全性クオリティ(0〜7点。当サイト版Piotroski)。収益性・利益の質・財務改善を採点。
  6〜7=優良/0〜2=要注意。実証ではF-score上位は年率+13%超過。割安株はF-scoreが高いほどバリュートラップを回避しやすい

# 現在のポートフォリオ（現金 {state['cash']/10000:,.0f}万円）
{chr(10).join(pos_lines) if pos_lines else '（なし・初回構築）'}

# 買い候補（定量スクリーニング済み。この中からのみ選ぶこと）
[タグ] momentum=上昇モメンタム / dip=上昇トレンド中の押し目 / breakout=52週高値ブレイク(主力A) / rs_leader=6ヶ月相対強度上位×高値圏(主力R・実測平均+12%) / theme_leader=強テーマの主役(副力T・平均は高いが当たり外れ大) / value_growth=割安×成長×財務健全 / event=上方修正・増配・好業績ドリフト(反応確認済み) / fresh_event=本日引け後の好開示・株価未反応(明朝の寄付で入る先行プレイ・ギャップリスクあり) / preview=決算発表7日以内×進捗率上振れ(跨ぐなら進捗率をカタリスト化) / buyback=自社株買い発表(需給改善カタリスト) / laggard=資金流入テーマ内の出遅れ(先回り向き・リターンは主役の半分と実測) / bench=昨日までの控え銘柄
{chr(10).join(cand_lines)}

# 指示
1. 保有銘柄それぞれについて**カタリストとシナリオが生きているか**を点検する。**保有継続がデフォルト**であり、
   売るのは次のいずれかに該当するときだけ: (a)カタリストが実現・出尽くした (b)シナリオが崩れた具体的証拠がある
   (c)候補が**明確かつ大幅に**優れ、かつ保有銘柄のエッジが薄れている。
   ⚠ 目先の下落・一時的なアンダーパフォーム・RSIの過熱・「他に良さそうな候補がある」程度では売らないこと
   （リターンの大半は少数の大勝ち銘柄を数ヶ月握って生まれる。過回転はコストとwhipsawで損なう）。
   含み益が乗ってトレンドとカタリストが続く銘柄は、目先の上下に関わらず握り続けるのが正解
2. 買いは候補から選ぶ。分散（同一業種・同一テーマに偏らない）と、観点の組み合わせを意識する。確信度に応じて予算に強弱。
   ただし入替は「保有中の最弱 vs 候補の最強」を比べ、明確に優位なときだけ行う（無理に{MAX_SWAPS}枠を使い切らない）
3. 保有8銘柄に次ぐ「控え」を{N_BENCH}銘柄選ぶ（保有・買い予定と重複しないこと。次に昇格させたい順）
4. 投資基準(policy)を更新する: 今の相場で「何が効いているか」を踏まえ、銘柄選定・利確/損切り・保有期間の方針を
   箇条書き4〜7行で明文化。前回から変えた点があれば末尾に「【更新】…」として1行で理由を書く

以下のJSONのみを出力:
{{"market_view": "市況の見立て(2文以内)",
 "policy": "今日時点の投資基準（箇条書き。改行は\\n）",
 "sells": [{{"code": "XXXX", "order_type": "成行", "limit": null, "reason": "売却理由(カタリスト・シナリオとの照合を含め具体的に)"}}],
 "buys": [{{"code": "XXXX", "budget": 1200000, "order_type": "指値", "limit": 1450, "style": "通常", "strategy": "A",
           "reason": "購入理由", "catalyst": "誰が・いつ・なぜ買い上げてくるか（必須）",
           "thesis": "想定シナリオと売却条件"}}],
 "bench": [{{"code": "XXXX", "style": "通常", "reason": "控えに置く理由(1文)"}}]}}"""


def _call_gemini(prompt: str) -> dict | None:
    from google import genai
    client = genai.Client(api_key=GEMINI_KEY)
    # JSONモード（構造化出力）で呼ぶ。理由文に引用符等が入ってもJSONが壊れない
    try:
        from google.genai import types
        config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4)
    except Exception:
        config = None
    for attempt in range(3):
        try:
            kwargs = {"model": GEMINI_MODEL, "contents": prompt}
            if config is not None:
                kwargs["config"] = config
            resp = client.models.generate_content(**kwargs)
            raw = (resp.text or "").strip()
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError as e:
                    print(f"  [AIファンド] JSONパース失敗({attempt+1}回目): {str(e)[:80]} → リトライ")
                    continue
        except Exception as e:
            if ("429" in str(e) or "503" in str(e)) and attempt < 2:
                print("  [AIファンド] レート制限 → 65秒待機してリトライ")
                time.sleep(65)
            else:
                print(f"  [AIファンド] Geminiエラー: {str(e)[:120]}")
                break
    return None


def decide() -> int:
    """夜間の意思決定。売り/買いを決めて ai_fund_orders に登録する（執行は翌営業日の寄付）。
    戻り値: 登録した注文数。"""
    ensure_tables()
    if not GEMINI_KEY:
        print("  [AIファンド] GEMINI_API_KEY未設定のためスキップ")
        return 0
    conn = get_conn(); cur = conn.cursor()
    _init_state(cur); conn.commit()
    state = _get_state(cur)
    today = date.today()

    if state["last_decided"] == today:
        print("  [AIファンド] 本日の意思決定は完了済み")
        cur.close(); conn.close(); return 0
    # 未約定注文が残っている間は新たな決定をしない（二重注文防止・休場明けに自然と解消）
    cur.execute("SELECT COUNT(*) FROM ai_fund_orders WHERE status='pending'")
    if cur.fetchone()[0] > 0:
        print("  [AIファンド] 未約定注文が残っているため今日は見送り")
        cur.close(); conn.close(); return 0

    latest = _latest_trading_date(cur)

    # ── 現ポジション状況 ──
    cur.execute("SELECT code, shares, avg_cost, buy_date, buy_reason, thesis, style, catalyst, strategy FROM ai_fund_positions")
    positions = []
    for code, shares, avg_cost, buy_date, buy_reason, thesis, style, catalyst, strategy in cur.fetchall():
        cur.execute("""
            SELECT s.name, d.close, p.rsi14, p.chg5d, p.chg25d
            FROM stocks s
            LEFT JOIN daily_prices d ON d.code = s.code AND d.date = %s
            LEFT JOIN price_stats p ON p.code = s.code
            WHERE s.code = %s
        """, (latest, code))
        r = cur.fetchone()
        close = float(r[1]) if r and r[1] else float(avg_cost)
        positions.append({
            "code": code, "name": r[0] if r else code, "shares": int(shares),
            "avg_cost": float(avg_cost), "buy_date": buy_date, "buy_reason": buy_reason,
            "thesis": thesis, "style": style or "通常", "catalyst": catalyst,
            "strategy": strategy, "close": close,
            "pnl_pct": (close / float(avg_cost) - 1) * 100,
            "hold_days": (today - buy_date).days if buy_date else 0,
            "rsi14": r[2] if r else None, "chg5d": r[3] if r else None, "chg25d": r[4] if r else None,
        })

    # ── 強制規律（AI判断に先立つ機械執行）: ロスカット＋利益保全（検証済み2段階エグジット） ──
    forced_sells = []
    for p in positions:
        if p["pnl_pct"] <= LOSSCUT_PCT:
            p["_force_reason"] = (f"[ロスカット規律] 含み損{p['pnl_pct']:.1f}%が閾値{LOSSCUT_PCT}%に到達。"
                                  "ルールに従い機械的に撤退。")
            forced_sells.append(p)
            continue
        # 利益保全: 取得後高値が+PROFIT_ARM_PCT%超 → 高値-PROFIT_TRAIL_PCT%まで陥落したら売り
        # （プレイブックのトレール切替。-15%はテーマ相場を長く乗るための実測値）
        cur.execute("""
            SELECT MAX(COALESCE(adj_close, close)) FROM daily_prices
            WHERE code = %s AND date >= %s
        """, (p["code"], p["buy_date"]))
        r = cur.fetchone()
        hi = float(r[0]) if r and r[0] else None
        if hi and hi >= p["avg_cost"] * (1 + PROFIT_ARM_PCT / 100):
            hi_pct = (hi / p["avg_cost"] - 1) * 100
            trail = _trail_pct(hi_pct)   # 大勝ち銘柄ほど広い余地（段階的トレール）
            if p["close"] <= hi * (1 - trail / 100):
                p["_force_reason"] = (f"[利益保全規律] 取得後高値+{hi_pct:.0f}%到達後、高値から-{trail:.0f}%まで陥落"
                                      f"（現在{p['pnl_pct']:+.1f}%）。段階的トレール利確（大勝ち銘柄ほど余地を広げ長く保有）。")
                forced_sells.append(p)

    # ── TOPIXレジーム（モメンタム系ビューのゲート。実測: 200日線割れではRS戦略の勝率24%） ──
    cur.execute("""
        SELECT COALESCE(adj_close, close) FROM daily_prices
        WHERE code = %s ORDER BY date DESC LIMIT 200
    """, (BENCH_CODE,))
    bench_px = [float(r2[0]) for r2 in cur.fetchall()]
    regime_up, regime_line = True, ""
    if len(bench_px) >= 200:
        bench_ma200 = sum(bench_px) / len(bench_px)
        bench_chg25 = (bench_px[0] / bench_px[25] - 1) * 100 if len(bench_px) > 25 else 0
        regime_up = bench_px[0] > bench_ma200
        regime_line = (f"\n- TOPIXレジーム: 200日線より{'上' if regime_up else '下'}"
                       f"（乖離{(bench_px[0]/bench_ma200-1)*100:+.1f}%）・直近25日{bench_chg25:+.1f}%")
        if not regime_up:
            regime_line += ("\n- ⚠ 地合い悪化局面: モメンタム系（rs_leader/theme_leader）は候補から自動除外済み。"
                            "押し目反発（急落時こそ平均+20%超と実測）・イベント型を優先する")

    # ── 候補 ──
    held = {p["code"] for p in positions}
    exclude = held | _cooldown_codes(cur)
    cands = _candidates(cur, exclude, regime_up=regime_up)
    if not cands:
        print("  [AIファンド] 候補なし（データ未整備？）→ 見送り")
        cur.close(); conn.close(); return 0

    # ── 昨日までの控え銘柄を候補プールに合流（昇格の道を確保） ──
    cur.execute("""
        SELECT code FROM ai_fund_bench
        WHERE bench_date = (SELECT MAX(bench_date) FROM ai_fund_bench)
    """)
    prev_bench = [r[0] for r in cur.fetchall()]
    cand_codes_now = {c["code"] for c in cands}
    for bcode in prev_bench:
        if bcode in exclude or bcode in cand_codes_now:
            if bcode in cand_codes_now and "bench" not in {t for c in cands if c["code"] == bcode for t in c["tags"]}:
                next(c for c in cands if c["code"] == bcode)["tags"].append("bench")
            continue
        cands_extra = _candidates_one(cur, bcode)
        if cands_extra:
            cands_extra["tags"] = ["bench"]
            cands.append(cands_extra)

    # ── 決算発表予定日（保有＋候補。決算跨ぎの判断材料） ──
    all_codes = sorted(held | {c["code"] for c in cands})
    earn_map = _earnings_dates(cur, conn, all_codes)

    # ── 実測エッジ（週1再計測）と通期予想への進捗率 ──
    _refresh_edge_stats(cur, conn)
    edge_line = _edge_summary(cur)
    prog_map = {c2: _progress_note(cur, c2) for c2 in all_codes}

    # ── 市況コンテキスト（AI考察 + TOPIXレジーム。レジームは候補生成前に計算済み） ──
    cur.execute("SELECT ai_commentary FROM market_summary ORDER BY summary_date DESC LIMIT 1")
    r = cur.fetchone()
    market_ctx = (r[0][:400] if r and r[0] else "（市況コメントなし）") + regime_line

    # ── 投資基準（前回分）・成績フィードバック・環境データ ──
    cur.execute("SELECT statement FROM ai_fund_policy ORDER BY policy_date DESC LIMIT 1")
    r = cur.fetchone()
    policy_prev = r[0] if r else None

    cur.execute("""
        SELECT t.code, s.name, t.pnl_pct, t.hold_days, t.style, t.strategy
        FROM ai_fund_trades t JOIN stocks s ON s.code = t.code
        WHERE t.side = 'sell' ORDER BY t.trade_date DESC LIMIT 12
    """)
    fb_rows = cur.fetchall()
    feedback = None
    if fb_rows:
        wins = sum(1 for r2 in fb_rows if float(r2[2] or 0) > 0)
        lines = [f"- {r2[0]} {r2[1]}: {float(r2[2]):+.1f}% ({r2[3]}日保有・{r2[4] or '通常'}"
                 f"{'・' + r2[5] + ':' + STRATEGIES[r2[5]]['name'] if r2[5] in STRATEGIES else ''})"
                 for r2 in fb_rows]
        feedback = f"直近{len(fb_rows)}トレードの勝率 {wins}/{len(fb_rows)}\n" + "\n".join(lines)

    cur.execute("""
        SELECT group_label, zscore FROM money_flow_weekly
        WHERE group_type='theme' AND flow_class='inflow'
          AND week_end = (SELECT MAX(week_end) FROM money_flow_weekly)
          AND turnover >= 100 AND n_stocks >= 8
        ORDER BY zscore DESC LIMIT 6
    """)
    inflow_themes = "、".join(f"{r2[0]}(Z{float(r2[1]):.1f})" for r2 in cur.fetchall())

    cur.execute("""
        SELECT p.code, s.name, p.chg5d FROM price_stats p
        JOIN stocks s ON s.code = p.code AND s.is_active = 1
        WHERE p.turnover_20d >= 5 ORDER BY p.chg5d DESC LIMIT 10
    """)
    movers = "、".join(f"{r2[1]}({float(r2[2]):+.0f}%)" for r2 in cur.fetchall())

    n_slots = N_POSITIONS - len(positions) + len(forced_sells)
    # 買い予算の目安: 現金 + 売り見込み（強制ロスカット分は今日終値の98%で概算）
    est_cash = state["cash"] + sum(p["close"] * p["shares"] * 0.98 for p in forced_sells)

    prompt = _build_prompt(state, positions, cands, market_ctx, n_slots, est_cash,
                           policy_prev, feedback, inflow_themes, movers, earn_map,
                           edge_line, prog_map)
    out = _call_gemini(prompt)

    sells, buys, market_view, policy_new, bench_out = [], [], "", "", []
    if out:
        market_view = str(out.get("market_view", ""))[:500]
        pol = out.get("policy", "")
        if isinstance(pol, list):  # 箇条書きを配列で返してくるケースに対応
            pol = "\n".join(str(x) for x in pol)
        policy_new = str(pol)[:3000]
        sells = [s0 for s0 in (out.get("sells") or []) if isinstance(s0, dict)]
        buys = [b0 for b0 in (out.get("buys") or []) if isinstance(b0, dict)]
        bench_out = [b0 for b0 in (out.get("bench") or []) if isinstance(b0, dict)]

    # ── ガードレール ──
    cand_codes = {c["code"] for c in cands}
    cand_by_code = {c["code"]: c for c in cands}
    forced_codes = {p["code"] for p in forced_sells}

    pos_by_code = {p["code"]: p for p in positions}
    valid_sells = []
    for s0 in sells:
        c = str(s0.get("code", "")).strip()
        if c in held and c not in forced_codes and len(valid_sells) < MAX_SWAPS:
            ot, lp = _parse_order(s0, pos_by_code.get(c, {}).get("close"))
            valid_sells.append({"code": c, "reason": str(s0.get("reason", ""))[:1000],
                                "order_type": ot, "limit_price": lp})
    is_initial = len(positions) == 0
    if not is_initial:
        valid_sells = valid_sells[:MAX_SWAPS]

    n_buy_slots = N_POSITIONS - len(positions) + len(valid_sells) + len(forced_sells)
    est_cash = state["cash"] + sum(p["close"] * p["shares"] * 0.98
                                   for p in positions
                                   if p["code"] in forced_codes | {s["code"] for s in valid_sells})

    # 先回り（予測）スタイルはポートフォリオの一部に留める:
    # 売却されずに残る先回り保有 + 新規の先回り買い ≤ MAX_ANTICIPATE
    leaving = forced_codes | {s["code"] for s in valid_sells}
    n_antic = sum(1 for p in positions if p["style"] == "先回り" and p["code"] not in leaving)

    valid_buys, budget_sum, seen = [], 0.0, set()
    for b0 in buys:
        c = str(b0.get("code", "")).strip()
        if c not in cand_codes or c in seen or len(valid_buys) >= n_buy_slots:
            continue
        # プレイブック禁止事項: RSI85以上の新規買い（全スタイルで期待値マイナスと実測）。
        # 候補ビューは85未満に絞ってあるが、bench合流分はRSIフィルタを通らないためここで機械的に弾く
        rsi_c = cand_by_code.get(c, {}).get("rsi14")
        if rsi_c is not None and float(rsi_c) >= 85:
            print(f"    [ガード] {c}: RSI{float(rsi_c):.0f}が過熱圏(85以上)のため見送り")
            continue
        style = "先回り" if str(b0.get("style", "")).strip() == "先回り" else "通常"
        if style == "先回り":
            if n_antic >= MAX_ANTICIPATE:
                print(f"    [ガード] {c}: 先回り枠({MAX_ANTICIPATE})超過のため見送り")
                continue
            n_antic += 1
        try:
            budget = float(b0.get("budget", 0))
        except (TypeError, ValueError):
            budget = 0
        budget = max(BUDGET_MIN, min(BUDGET_MAX, budget or BUDGET_MIN))
        if budget_sum + budget > est_cash:
            budget = est_cash - budget_sum
            if budget < BUDGET_MIN * 0.8:
                continue
        catalyst = str(b0.get("catalyst", "")).strip()[:800]
        if not catalyst:
            # 運営方針3: カタリストが書けない銘柄は買わない
            print(f"    [ガード] {c}: カタリスト未記載のため見送り")
            if style == "先回り":
                n_antic -= 1
            continue
        # スタイル分類（運営方針16）: AI指定が不正・欠落ならタグから既定対応で補完
        strategy = str(b0.get("strategy", "")).strip().upper()
        if strategy not in STRATEGIES:
            strategy = "L" if style == "先回り" else _strategy_from_tags(cand_by_code.get(c, {}).get("tags"))
        ot, lp = _parse_order(b0, cand_by_code.get(c, {}).get("close"))
        valid_buys.append({"code": c, "budget": round(budget), "style": style,
                           "strategy": strategy,
                           "reason": str(b0.get("reason", ""))[:1000],
                           "catalyst": catalyst,
                           "thesis": str(b0.get("thesis", ""))[:600],
                           "order_type": ot, "limit_price": lp})
        budget_sum += budget
        seen.add(c)

    # 8銘柄維持の充足: AI出力が足りなければ定量上位（タグ数→25日騰落順）で補完。RSI85以上は除外
    if len(valid_buys) < n_buy_slots and est_cash - budget_sum >= BUDGET_MIN:
        ranked = sorted((c for c in cands if c["code"] not in seen
                         and not (c.get("rsi14") is not None and float(c["rsi14"]) >= 85)),
                        key=lambda c: (-len(c["tags"]), -(c["chg25d"] or 0)))
        for c in ranked:
            if len(valid_buys) >= n_buy_slots or est_cash - budget_sum < BUDGET_MIN:
                break
            budget = min(BUDGET_MAX, max(BUDGET_MIN, (est_cash - budget_sum) / max(1, n_buy_slots - len(valid_buys))))
            tag_jp = {"momentum": "上昇モメンタム", "dip": "押し目", "breakout": "52週高値ブレイク",
                      "value_growth": "割安×成長", "event": "好業績イベント", "buyback": "自社株買い",
                      "fresh_event": "引け後好開示", "preview": "決算上振れ候補",
                      "rs_leader": "相対強度リーダー", "theme_leader": "テーマ主役",
                      "laggard": "テーマ出遅れ", "bench": "控え継続"}
            reasons = "・".join(tag_jp.get(t, t) for t in c["tags"])
            valid_buys.append({"code": c["code"], "budget": round(budget), "style": "通常",
                               "strategy": _strategy_from_tags(c["tags"]),
                               "reason": f"[定量補完] {reasons}の条件に合致（25日騰落{_fnum(c['chg25d'])}%・RSI{_fnum(c['rsi14'],0)}）。AI出力不足分を規律的に補充。",
                               "catalyst": f"{reasons}の統計的エッジ（強いトレンド・出来高を伴う銘柄は追随買いを集めやすい）。",
                               "thesis": f"購入根拠のトレンドが崩れたら（-12%損切り）撤退。+{PROFIT_ARM_PCT:.0f}%到達後は高値-{PROFIT_TRAIL_PCT:.0f}%トレールで利を伸ばす。"})
            budget_sum += budget
            seen.add(c["code"])

    # ── 注文登録 ──（強制売り・定量補完は成行固定。AI指定の売買のみ指値可）
    n_orders = 0
    for p in forced_sells:
        cur.execute("""
            INSERT INTO ai_fund_orders (code, side, shares, reason, order_type, decided_date, created_at)
            VALUES (%s,'sell',%s,%s,'market',%s,NOW())
        """, (p["code"], p["shares"], p.get("_force_reason", "強制規律による売却"), today))
        n_orders += 1
    for s0 in valid_sells:
        pos = next(p for p in positions if p["code"] == s0["code"])
        cur.execute("""
            INSERT INTO ai_fund_orders (code, side, shares, reason, order_type, limit_price, decided_date, created_at)
            VALUES (%s,'sell',%s,%s,%s,%s,%s,NOW())
        """, (s0["code"], pos["shares"], s0["reason"],
              s0.get("order_type", "market"), s0.get("limit_price"), today))
        n_orders += 1
    for b0 in valid_buys:
        cur.execute("""
            INSERT INTO ai_fund_orders (code, side, budget, reason, catalyst, thesis, style, strategy,
                                        order_type, limit_price, decided_date, created_at)
            VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, (b0["code"], b0["budget"], b0["reason"], b0.get("catalyst", ""), b0["thesis"],
              b0.get("style", "通常"), b0.get("strategy"),
              b0.get("order_type", "market"), b0.get("limit_price"), today))
        n_orders += 1

    # ── 投資基準の保存（日次で蓄積。最新をサイト表示） ──
    if policy_new:
        cur.execute("""
            INSERT INTO ai_fund_policy (policy_date, statement, created_at)
            VALUES (%s, %s, NOW())
            ON DUPLICATE KEY UPDATE statement = VALUES(statement)
        """, (today, policy_new))

    # ── 控え（ベンチ）銘柄の保存: 保有・買い予定と重複しない8銘柄。不足は定量上位で補完 ──
    held_after = (held - {s["code"] for s in valid_sells} - forced_codes) | {b["code"] for b in valid_buys}
    bench_rows, bseen = [], set()
    for b0 in bench_out:
        c = str(b0.get("code", "")).strip()
        if len(bench_rows) >= N_BENCH:
            break
        if c in cand_codes and c not in held_after and c not in bseen:
            style = "先回り" if str(b0.get("style", "")).strip() == "先回り" else "通常"
            bench_rows.append((c, style, str(b0.get("reason", ""))[:500]))
            bseen.add(c)
    if len(bench_rows) < N_BENCH:
        ranked = sorted((c for c in cands if c["code"] not in bseen and c["code"] not in held_after),
                        key=lambda c: (-len(c["tags"]), -(c["chg25d"] or 0)))
        for c in ranked:
            if len(bench_rows) >= N_BENCH:
                break
            bench_rows.append((c["code"], "通常", f"[定量補完] {'/'.join(c['tags'])}の上位候補"))
            bseen.add(c["code"])
    cur.execute("DELETE FROM ai_fund_bench WHERE bench_date = %s", (today,))
    for i, (c, style, reason) in enumerate(bench_rows, 1):
        cur.execute("SELECT close FROM daily_prices WHERE code=%s AND date=%s", (c, latest))
        r = cur.fetchone()
        close_at = float(r[0]) if r and r[0] else None
        cur.execute("""
            INSERT INTO ai_fund_bench (bench_date, rank_no, code, style, reason, close_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
        """, (today, i, c, style, reason, close_at))

    cur.execute("UPDATE ai_fund_state SET last_decided=%s, updated_at=NOW() WHERE id=1", (today,))
    if market_view and latest:
        # NAV行が既にある日だけ市況見解を付記する（無ければ捨てる。nav=0のゴミ行を作らない）
        cur.execute("UPDATE ai_fund_nav SET market_view=%s WHERE date=%s", (market_view, latest))
    conn.commit(); cur.close(); conn.close()

    print(f"  [AIファンド] 意思決定: 売{len(forced_sells)+len(valid_sells)} 買{len(valid_buys)}（翌営業日の寄付で約定）")
    for b0 in valid_buys:
        print(f"    買 {b0['code']} {b0['budget']/1e4:.0f}万円: {b0['reason'][:60]}…")
    return n_orders


def status():
    conn = get_conn(); cur = conn.cursor()
    st = _get_state(cur)
    if not st:
        print("未初期化"); return
    print(f"現金: {st['cash']/1e4:,.1f}万円 / 設定日: {st['inception']} / 最終判断: {st['last_decided']}")
    cur.execute("SELECT code, shares, avg_cost, buy_date FROM ai_fund_positions ORDER BY code")
    for r in cur.fetchall():
        print(f"  保有 {r[0]} {r[1]}株 @{float(r[2]):,.1f} ({r[3]})")
    cur.execute("SELECT code, side, budget, shares, status, decided_date FROM ai_fund_orders WHERE status='pending'")
    for r in cur.fetchall():
        print(f"  注文 {r[1]} {r[0]} {'予算' + format(r[2]/1e4, '.0f') + '万' if r[2] else str(r[3]) + '株'} 決定{r[5]}")
    cur.close(); conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--execute" in args:
        execute_orders()
    elif "--nav" in args:
        record_nav()
    elif "--decide" in args:
        decide()
    elif "--status" in args:
        status()
    else:
        print("使い方: python3 ai_fund.py [--execute | --nav | --decide | --status]")

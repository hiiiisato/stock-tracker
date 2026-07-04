import os
import pymysql
from dotenv import load_dotenv

load_dotenv()

JQUANTS_API_KEY = os.environ["JQUANTS_API_KEY"]
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
EDINET_API_KEY   = os.environ.get("EDINET_API_KEY", "")
EDINETDB_API_KEY = os.environ.get("EDINETDB_API_KEY", "")
JQUANTS_BASE_URL = "https://api.jquants.com/v2"
JQUANTS_HEADERS = {"x-api-key": JQUANTS_API_KEY}


def _resolve_ssl_ca() -> str:
    """SSL証明書パスをクロスプラットフォームで解決する。
    優先順位: 環境変数 TIDB_SSL_CA → certifi → システム既定
    """
    env_val = os.environ.get("TIDB_SSL_CA", "")
    if env_val:
        return env_val
    try:
        import certifi
        return certifi.where()
    except ImportError:
        pass
    import ssl
    return ssl.get_default_verify_paths().cafile or ""


_ssl_ca = _resolve_ssl_ca()

DB_CONFIG = dict(
    host=os.environ["TIDB_HOST"],
    port=int(os.environ.get("TIDB_PORT", 4000)),
    database=os.environ.get("TIDB_DATABASE", "stock_tracker"),
    user=os.environ["TIDB_USER"],
    password=os.environ["TIDB_PASSWORD"],
    ssl={"ca": _ssl_ca} if _ssl_ca else {},
    charset="utf8mb4",
    autocommit=False,
)


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def bulk_upsert(cur, table, columns, rows, update_cols=None, batch_size=500):
    """MySQL/TiDB向けバルクUPSERT (INSERT ... ON DUPLICATE KEY UPDATE)"""
    if not rows:
        return
    col_list = ",".join(f"`{c}`" for c in columns)
    row_ph = "(" + ",".join(["%s"] * len(columns)) + ")"
    if update_cols is None:
        update_cols = columns
    updates = ",".join(f"`{c}`=VALUES(`{c}`)" for c in update_cols)
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        values_ph = ",".join([row_ph] * len(batch))
        sql = f"INSERT INTO `{table}` ({col_list}) VALUES {values_ph} ON DUPLICATE KEY UPDATE {updates}"
        cur.execute(sql, [v for row in batch for v in row])


# ─── 理論株価モデル（はっしゃん式）の係数テーブル ───────────────────────────
# 参考: 素材/投資判断ツール（株plus版）.xlsx / kabuka.biz 系
# 各テーブルは (閾値, 係数) の昇順リスト。lookup は「閾値 <= 値」の最大行を返す
# （Excel の VLOOKUP( , TRUE) と同じ近似一致=ステップ関数）。

# 自己資本比率(小数 0.0〜1.0) → 資産価値の割引率
# 財務が厚い企業ほど資産価値の評価を高くする
THEO_DISCOUNT_TABLE = [
    (0.00, 0.50),
    (0.10, 0.60),
    (0.33, 0.65),
    (0.50, 0.70),
    (0.67, 0.75),
    (0.80, 0.80),
]

# PBR(株価/BPS) → リスク評価率（理論株価全体への掛け目）
# 極端に低PBR（解散価値を大きく下回る）銘柄は「安いなりの理由」を織り込み減点。
# PBR>=0.5 は 1.0（減点なし）。実在銘柄はほぼ全て 1.0 になる。
THEO_RISK_TABLE = [
    (0.00, 0.33),
    (0.21, 0.33),
    (0.25, 0.50),
    (0.34, 0.66),
    (0.41, 0.80),
    (0.50, 1.00),
]

# 事業価値の係数
THEO_BUSINESS_MULT = 150     # EPS × min(ROA,上限) × 150 × Lev補正
THEO_ROA_CAP       = 0.20    # ROA の上限（20%でクリップ）
THEO_RETAIN_RATIO  = 0.70    # 最終利益の純資産への組入比率（BPS成長に使用）
THEO_SIM_YEARS     = 5       # 何年先までシミュレーションするか
# 経常増益率の許容レンジ（±/年, 小数）。低ベースからの急回復などで増益率が
# 異常値になる銘柄が5年複利で非現実的に爆発するのを防ぐためクランプする。
THEO_GROWTH_CAP    = 0.50
# 予想PERの下限。会社予想EPSがこれ未満のPERを示す場合はデータ異常（Yahooの
# eps_forward が桁違いに大きい等）とみなし、実績EPSにフォールバックする。
# 事業価値の計算でも EPS <= 現在株価/この値 にキャップし異常爆発を防ぐ。
THEO_MIN_PER       = 3.0

# 投資判断○×の基準（Excel由来）
THEO_MKTCAP_MIN = 3_000_000_000      # 時価総額 下限 30億円
THEO_MKTCAP_MAX = 200_000_000_000    # 時価総額 上限 2000億円
THEO_BIZ_RATIO_MAX = 0.90            # 事業価値比率の上限
THEO_JUDGE_MULT = 1.70               # 投資判断倍率の目安（これ以上で有望）


def theo_lookup(table, value):
    """(閾値, 係数) 昇順リストに対し、閾値<=value の最大行の係数を返す（ステップ関数）。
    value が最小閾値未満なら先頭の係数を返す。"""
    result = table[0][1]
    for threshold, coef in table:
        if value >= threshold:
            result = coef
        else:
            break
    return result


def theo_leverage(equity_ratio_frac):
    """財務レバレッジ補正 = median(1, 1.5, (1/自己資本比率)/3 + 1/2)。
    自己資本比率が小さい（レバレッジ高い）ほど 1.5 に寄る。"""
    if not equity_ratio_frac or equity_ratio_frac <= 0:
        return 1.0
    x = (1.0 / equity_ratio_frac) / 3.0 + 0.5
    return sorted([1.0, 1.5, x])[1]

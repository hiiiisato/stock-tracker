"""
調査戦略 (Research Strategy)
=============================
銘柄価格変動の要因を調査する方法を一元管理するファイル。
手法の追加・変更・切り替えはこのファイルだけを編集すればよい。

◆ 戦略を切り替えるには
  ACTIVE_STRATEGY の値を変更する。

◆ 調査対象フィルターを変えるには
  RESEARCH_THRESHOLD_PCT や RESEARCH_MAX_PER_DIRECTION を変更する。

◆ 新しい調査手法を追加するには
  1. STRATEGIES 辞書に設定を追加する
  2. 同名の _fetch_<name> 関数を実装する
  3. ACTIVE_STRATEGY をそのキーに変更する

利用可能な戦略
--------------
  kabutan  : kabutan.jp ニュースをスクレイピング（材料・開示を優先）← 現在
  yfinance : Yahoo Finance ニュース（英語、将来用）
"""

import time
import requests
from bs4 import BeautifulSoup
from datetime import date
from typing import Optional

# ══════════════════════════════════════════════════════════════
#  【メイン設定】ここを変更して調査方法・条件をカスタマイズする
# ══════════════════════════════════════════════════════════════

# 使用する調査戦略のキー
ACTIVE_STRATEGY = "kabutan"

# 調査対象とする最低変動率（絶対値）。これ未満は調査しない
RESEARCH_THRESHOLD_PCT = 10.0

# 上昇・下落それぞれの最大調査件数
RESEARCH_MAX_PER_DIRECTION = 20

# ══════════════════════════════════════════════════════════════
#  戦略別パラメーター定義
# ══════════════════════════════════════════════════════════════

STRATEGIES = {
    "kabutan": {
        "description": "kabutan.jp 銘柄別ニュースページをスクレイピング（材料・開示を優先）",
        "delay_seconds": 0.8,          # リクエスト間隔（サーバー負荷対策）
        "max_items": 8,                # 1銘柄あたりの最大ニュース取得件数
        "date_window_days": 1,         # target_date ±N日以内のニュースを優先
        "priority_categories": {"材料", "開示", "業績", "決算", "注目"},
    },
    "yfinance": {
        "description": "Yahoo Finance ニュース API（英語ヘッドライン）",
        "delay_seconds": 0.3,
        "max_items": 5,
        "date_window_days": 1,
        "priority_categories": set(),
    },
}


# ══════════════════════════════════════════════════════════════
#  公開インターフェース（event_researcher.py から呼ぶ）
# ══════════════════════════════════════════════════════════════

def fetch_news(code: str, target_date: date) -> list:
    """
    指定銘柄のニュースを調査して返す。
    戻り値: [{"dt": datetime, "date": date, "category": str, "title": str}, ...]
    """
    cfg = STRATEGIES.get(ACTIVE_STRATEGY, STRATEGIES["kabutan"])
    time.sleep(cfg["delay_seconds"])

    if ACTIVE_STRATEGY == "kabutan":
        return _fetch_kabutan(code, target_date, cfg)
    elif ACTIVE_STRATEGY == "yfinance":
        return _fetch_yfinance(code, target_date, cfg)
    else:
        return []


def get_delay() -> float:
    """現在の戦略のリクエスト間隔を返す（ループ制御用）。"""
    return STRATEGIES.get(ACTIVE_STRATEGY, {}).get("delay_seconds", 1.0)


def get_strategy_description() -> str:
    """現在の戦略の説明文を返す（ログ・UIへの表示用）。"""
    cfg = STRATEGIES.get(ACTIVE_STRATEGY, {})
    return f"[{ACTIVE_STRATEGY}] {cfg.get('description', '')}"


# ══════════════════════════════════════════════════════════════
#  戦略実装
# ══════════════════════════════════════════════════════════════

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _fetch_kabutan(code: str, target_date: date, cfg: dict) -> list:
    """kabutan.jp の銘柄別ニュースをスクレイピングして返す。"""
    from datetime import datetime
    url = f"https://kabutan.jp/stock/news?code={code}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return []
    except Exception as e:
        print(f"    [kabutan] {code}: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    tbl = soup.find("table", class_="s_news_list")
    if not tbl:
        return []

    items = []
    priority_cats = cfg.get("priority_categories", set())
    window = cfg.get("date_window_days", 1)
    max_items = cfg.get("max_items", 8)

    for row in tbl.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 3:
            continue
        time_el = tds[0].find("time")
        if not time_el:
            continue
        dt_str = time_el.get("datetime", "")
        try:
            news_dt = datetime.fromisoformat(dt_str[:16])
        except ValueError:
            try:
                news_dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
            except ValueError:
                continue

        category = tds[1].text.strip()
        link = tds[2].find("a")
        title = link.text.strip() if link else tds[2].text.strip()

        items.append({
            "dt":       news_dt,
            "date":     news_dt.date(),
            "category": category,
            "title":    title,
        })

    if not items:
        return []

    # target_date 周辺の優先カテゴリ → target_date 周辺の全記事 → その他
    near_priority = [it for it in items
                     if abs((it["date"] - target_date).days) <= window
                     and it["category"] in priority_cats]
    near_other    = [it for it in items
                     if abs((it["date"] - target_date).days) <= window
                     and it not in near_priority]
    rest          = [it for it in items if it not in near_priority and it not in near_other]

    return (near_priority + near_other + rest)[:max_items]


def _fetch_yfinance(code: str, target_date: date, cfg: dict) -> list:
    """
    Yahoo Finance ニュースを取得する（英語）。
    将来: APIキー不要で使えるが英語のみ。
    """
    # yfinance の news プロパティは現時点で不安定なため未実装
    # 利用可能になり次第ここに実装する
    print(f"    [yfinance] 未実装: {code}")
    return []

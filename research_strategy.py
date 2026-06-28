"""
調査戦略 (Research Strategy)
=============================
銘柄価格変動の要因を調査する方法を一元管理するファイル。
手法の追加・変更・切り替えはこのファイルだけを編集すればよい。

◆ 調査対象フィルターを変えるには
  RESEARCH_THRESHOLD_PCT や RESEARCH_MAX_PER_DIRECTION を変更する。

◆ ソース設定を変えるには
  SOURCE_CONFIGS の各パラメーターを編集する。

◆ AI 要約を有効にするには
  環境変数 GEMINI_API_KEY を設定する（Google AI Studio で無料取得）。
  AI_SUMMARY_CONFIG の enabled を True にする。

現在の仕組み
--------------
  2ソースを並行して取得し、日付フィルタ後にマージする。

  1. kabutan.jp  : 銘柄別ニュースページをスクレイピング（材料・開示を優先）
  2. Google News RSS : 複数媒体（日経・株探・みんかぶ・Yahoo・四季報）を横断検索
  3. Gemini AI   : 収集した記事タイトルをもとに【変動理由】【背景】【参考ソース】に要約

  将来追加候補:
  4. TDnet（東証適時開示）: 公式IR情報
"""

import time
import warnings
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════
#  【メイン設定】ここを変更して調査方法・条件をカスタマイズする
# ══════════════════════════════════════════════════════════════

# 調査対象とする最低変動率（絶対値）。これ未満は調査しない
RESEARCH_THRESHOLD_PCT = 10.0

# 上昇・下落それぞれの最大調査件数
RESEARCH_MAX_PER_DIRECTION = 20

# ──────────────────────────────────────────────────────────────
#  AI 要約設定（Gemini 1.5 Flash 無料枠）
# ──────────────────────────────────────────────────────────────
AI_SUMMARY_CONFIG = {
    "enabled":           True,
    "model":             "gemini-2.5-flash",
    "rate_limit_delay":  4.1,   # 無料枠 15 RPM → 4秒以上空ける
}

# ──────────────────────────────────────────────────────────────
#  ソース別設定
# ──────────────────────────────────────────────────────────────
SOURCE_CONFIGS = {
    "kabutan": {
        "enabled":            True,
        "description":        "kabutan.jp 銘柄別ニュース（材料・開示を優先）",
        "delay_seconds":      0.5,
        "max_items":          5,       # kabutan から最大5件
        "date_window_days":   2,       # target_date ±2日以内を優先
        "priority_categories": {"材料", "開示", "業績", "決算", "注目"},
    },
    "google_news": {
        "enabled":            True,
        "description":        "Google News RSS（日経・株探・みんかぶ・Yahoo・四季報など横断）",
        "delay_seconds":      0.5,
        "max_items":          5,       # Google News から最大5件
        "date_window_days":   2,       # target_date ±2日以内の記事のみ採用
        "search_keywords":    ["急騰", "急落", "上昇", "下落", "材料", "理由"],
    },
}

# ══════════════════════════════════════════════════════════════
#  公開インターフェース（event_researcher.py から呼ぶ）
# ══════════════════════════════════════════════════════════════

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_news(code: str, target_date: date, company_name: str = "",
               direction: str = "") -> list:
    """
    指定銘柄のニュースを全有効ソースから取得してマージして返す。
    Google News の記事は対象銘柄に言及しているものだけを残す。
    戻り値: [{"dt": datetime, "date": date, "source": str, "category": str, "title": str}, ...]
    """
    all_items = []

    if SOURCE_CONFIGS["kabutan"]["enabled"]:
        cfg = SOURCE_CONFIGS["kabutan"]
        time.sleep(cfg["delay_seconds"])
        kabutan_items = _fetch_kabutan(code, target_date, cfg)
        all_items.extend(kabutan_items)

    if SOURCE_CONFIGS["google_news"]["enabled"]:
        cfg = SOURCE_CONFIGS["google_news"]
        time.sleep(cfg["delay_seconds"])
        gnews_raw = _fetch_google_news(code, company_name, target_date, cfg,
                                       direction=direction)
        # 対象銘柄に言及していない集合記事などを除外
        gnews_filtered = _filter_relevant_gnews(gnews_raw, code, company_name)
        # kabutan と重複するタイトルを除去
        existing_titles = {_normalize_title(it["title"]) for it in all_items}
        for it in gnews_filtered:
            if _normalize_title(it["title"]) not in existing_titles:
                all_items.append(it)
                existing_titles.add(_normalize_title(it["title"]))

    # 日付の近い順にソート（target_date に近い→遠い）
    all_items.sort(key=lambda x: (abs((x["date"] - target_date).days), x["dt"]), reverse=False)

    return all_items


def summarize_news(items: list, code: str, company_name: str,
                   target_date: date, direction: str = "", change_pct: float = None):
    """
    Gemini API でニュース記事タイトルを構造化要約する。
    GEMINI_API_KEY 未設定 / AI_SUMMARY_CONFIG 無効時は None を返す。
    戻り値: "【変動理由】\n...\n【背景・詳細】\n...\n【参考ソース】\n..." | None
    """
    if not AI_SUMMARY_CONFIG.get("enabled") or not items:
        return None

    try:
        from google import genai as _genai
        from config import GEMINI_API_KEY
        if not GEMINI_API_KEY:
            return None
        _client = _genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        return None

    name_label = company_name or code
    articles_text = "\n".join(
        f"- [{it['dt'].strftime('%m/%d %H:%M')}][{it['category']}] {it['title']}"
        for it in items
    )

    if direction == "up":
        dir_label = f"値上がり（当日騰落率: +{abs(change_pct):.2f}%）" if change_pct is not None else "値上がり"
        dir_instruction = "この銘柄は当日大きく値上がりしています。値上がりの理由を分析してください。"
    elif direction == "down":
        dir_label = f"値下がり（当日騰落率: -{abs(change_pct):.2f}%）" if change_pct is not None else "値下がり"
        dir_instruction = "この銘柄は当日大きく値下がりしています。値下がりの理由を分析してください。"
    else:
        dir_label = "株価変動"
        dir_instruction = "この銘柄の株価変動理由を分析してください。"

    prompt = f"""あなたは株式市場の専門アナリストです。
以下は{name_label}（証券コード: {code}）の{target_date.strftime('%Y年%m月%d日')}の{dir_label}に関するニュース記事の情報です。

{dir_instruction}

【収集した記事情報】
{articles_text}

上記の情報をもとに、{name_label}の株価変動理由を詳しくまとめてください。

## 必須ルール
- {name_label}に直接関係する情報のみ使用すること（他銘柄の情報は除外）
- タイトルに含まれる数値（金額・利回り・倍率・比率・パーセンテージ等）は必ず明記すること
  例：「純利益が増加」→「純利益が◯億円から◯億円（前期比◯倍）に増加」
- 数値が不明な場合のみ「詳細は記事本文参照」と書くこと
- 株価の方向性（{dir_label}）に合致した理由のみを述べること。逆方向の情報は無視すること
- 「急騰」「上昇」等の事実ではなく、なぜその材料が株価を動かしたかの"理由"を書くこと
- フォーマット以外の余分な文章・前置き・後書きは不要

## 出力フォーマット

【変動理由】
（1〜2文。株価変動の直接的な引き金となった出来事を具体的に）

【背景・詳細】
（3〜5文。発表内容の具体的な数値・業績の前後比較・市場背景・投資家心理の変化など、できるだけ詳しく）

【参考ソース】
（使用した主な媒体名を箇条書き。同じ媒体は1回のみ）"""

    try:
        time.sleep(AI_SUMMARY_CONFIG.get("rate_limit_delay", 4.1))
        resp = _client.models.generate_content(
            model=AI_SUMMARY_CONFIG["model"],
            contents=prompt,
        )
        return resp.text.strip()
    except Exception as e:
        print(f"    [AI要約] {code}: {e}")
        return None


def get_strategy_description() -> str:
    """現在の有効ソース一覧を返す（ログ表示用）。"""
    active = [cfg["description"] for cfg in SOURCE_CONFIGS.values() if cfg["enabled"]]
    if AI_SUMMARY_CONFIG.get("enabled"):
        active.append("Gemini AI 要約")
    return " + ".join(active)


def get_delay() -> float:
    """全ソースの合計遅延時間を返す。"""
    return sum(cfg["delay_seconds"] for cfg in SOURCE_CONFIGS.values() if cfg["enabled"])


# ══════════════════════════════════════════════════════════════
#  ソース実装 1: kabutan.jp
# ══════════════════════════════════════════════════════════════

def _fetch_kabutan(code: str, target_date: date, cfg: dict) -> list:
    """kabutan.jp の銘柄別ニュースページをスクレイピングして返す。"""
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
    window = cfg.get("date_window_days", 2)
    max_items = cfg.get("max_items", 5)

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
            "source":   "kabutan",
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
    rest          = [it for it in items
                     if it not in near_priority and it not in near_other]

    return (near_priority + near_other + rest)[:max_items]


# ══════════════════════════════════════════════════════════════
#  ソース実装 2: Google News RSS
# ══════════════════════════════════════════════════════════════

# 除外する媒体（ノイズになりやすいもの）
_EXCLUDE_SOURCES = {"掲示板", "Yahoo!ファイナンス 掲示板"}

# 優先する媒体（金融専門メディア）
_PRIORITY_SOURCES = {
    "株探", "日本経済新聞", "会社四季報オンライン", "みんかぶ",
    "ダイヤモンド・オンライン", "東洋経済オンライン", "Bloomberg",
    "Reuters", "トウシル", "マネックス証券", "SBI証券",
}


def _fetch_google_news(code: str, company_name: str,
                       target_date: date, cfg: dict,
                       direction: str = "") -> list:
    """
    Google News RSS から銘柄関連ニュースを取得する。
    company_name が空の場合は証券コードのみで検索する。
    """
    window    = cfg.get("date_window_days", 2)
    max_items = cfg.get("max_items", 5)
    from_date = target_date - timedelta(days=window)
    to_date   = target_date + timedelta(days=window)

    # 方向性に応じてクエリを絞る（上昇/下落のニュースを混在させない）
    name_part = company_name if company_name else ""
    if direction == "up":
        kw = "(急騰 OR 上昇 OR 好業績 OR 増益 OR 材料 OR 理由)"
    elif direction == "down":
        kw = "(急落 OR 下落 OR 悪材料 OR 減益 OR 売られ OR 材料 OR 理由)"
    else:
        kw = "(急騰 OR 上昇 OR 材料 OR 急落 OR 下落 OR 理由)"
    query = f"{code} {name_part} {kw}"
    url = (f"https://news.google.com/rss/search"
           f"?q={quote(query)}&hl=ja&gl=JP&ceid=JP:ja")

    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return []
    except Exception as e:
        print(f"    [google_news] {code}: {e}")
        return []

    soup = BeautifulSoup(r.content, "xml")
    raw_items = soup.find_all("item")
    items = []

    for it in raw_items:
        title_el = it.find("title")
        pub_el   = it.find("pubDate")
        src_el   = it.find("source")

        if not title_el or not pub_el:
            continue

        title  = title_el.text.strip()
        source = src_el.text.strip() if src_el else "不明"

        # 掲示板など除外
        if source in _EXCLUDE_SOURCES:
            continue
        # タイトルが掲示板っぽいものを除外
        if "掲示板" in title or "BBS" in title.upper():
            continue

        # 公開日をパース
        try:
            pub_dt = parsedate_to_datetime(pub_el.text).replace(tzinfo=None)
        except Exception:
            continue

        pub_date = pub_dt.date()

        # 日付フィルタ
        if not (from_date <= pub_date <= to_date):
            continue

        items.append({
            "dt":       pub_dt,
            "date":     pub_date,
            "source":   "google_news",
            "category": source,   # 媒体名をカテゴリとして使用
            "title":    title,
            "_priority": source in _PRIORITY_SOURCES,
        })

    # 優先媒体を前に
    priority = [it for it in items if it.get("_priority")]
    others   = [it for it in items if not it.get("_priority")]
    merged   = priority + others

    # _priority フラグを削除して返す
    for it in merged:
        it.pop("_priority", None)

    return merged[:max_items]


# ══════════════════════════════════════════════════════════════
#  ユーティリティ
# ══════════════════════════════════════════════════════════════

def _normalize_title(title: str) -> str:
    """タイトルの正規化（重複除去用）。"""
    import re
    t = re.sub(r"\s+", " ", title).strip()
    # 媒体名サフィックス "- 株探" 等を除去
    t = re.sub(r"\s+-\s+\S+$", "", t)
    return t[:50]


def _filter_relevant_gnews(items: list, code: str, company_name: str) -> list:
    """
    Google News 記事から対象銘柄に言及していないものを除外する。
    証券コードか社名（先頭4文字も許容）がタイトルに含まれる記事のみ残す。
    """
    keywords = [code]
    if company_name:
        keywords.append(company_name)
        if len(company_name) > 4:
            keywords.append(company_name[:4])
    return [it for it in items if any(kw in it["title"] for kw in keywords)]

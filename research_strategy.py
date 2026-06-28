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
  Phase 1: kabutan.jp + Google News RSS を並行取得（ThreadPoolExecutor）
  Phase 2: 複数銘柄を1回の Gemini 呼び出しにまとめて要約（バッチサイズ: AI_BATCH_SIZE）
  Phase 3: 結果を DB に保存

  ソース:
  1. kabutan.jp  : 銘柄別ニュースページをスクレイピング（材料・開示を優先）
  2. Google News RSS : 複数媒体（日経・株探・みんかぶ・Yahoo・四季報）を横断検索
  3. Gemini AI   : 収集した記事タイトルをもとに【変動理由】【背景】【参考ソース】に要約
"""

import re
import time
import warnings
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════
#  【メイン設定】ここを変更して調査方法・条件をカスタマイズする
# ══════════════════════════════════════════════════════════════

# 調査対象とする最低変動率（絶対値）。これ未満は調査しない
RESEARCH_THRESHOLD_PCT = 10.0

# 上昇・下落それぞれの最大調査件数（変動率 10%以上 かつ TOP15）
RESEARCH_MAX_PER_DIRECTION = 15

# ──────────────────────────────────────────────────────────────
#  AI 要約設定（Gemini Flash 無料枠）
# ──────────────────────────────────────────────────────────────
AI_SUMMARY_CONFIG = {
    "enabled":           True,
    "model":             "gemini-2.5-flash",
    "rate_limit_delay":  5.0,   # バッチ間のウェイト（秒）
    "batch_size":        5,     # 1回の Gemini 呼び出しにまとめる銘柄数
    "max_retries":       3,     # 429 エラー時のリトライ回数
}

# ──────────────────────────────────────────────────────────────
#  ニュース取得設定
# ──────────────────────────────────────────────────────────────
SOURCE_CONFIGS = {
    "kabutan": {
        "enabled":            True,
        "description":        "kabutan.jp 銘柄別ニュース（材料・開示を優先）",
        "delay_seconds":      0.3,
        "max_items":          5,
        "date_window_days":   2,
        "priority_categories": {"材料", "開示", "業績", "決算", "注目"},
    },
    "google_news": {
        "enabled":            True,
        "description":        "Google News RSS（日経・株探・みんかぶ・Yahoo・四季報など横断）",
        "delay_seconds":      0.3,
        "max_items":          5,
        "date_window_days":   2,
        "search_keywords":    ["急騰", "急落", "上昇", "下落", "材料", "理由"],
    },
}

# 並行ニュース取得時の同時リクエスト数（多すぎるとブロックされる）
NEWS_FETCH_WORKERS = 4

# ══════════════════════════════════════════════════════════════
#  公開インターフェース
# ══════════════════════════════════════════════════════════════

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_news(code: str, target_date: date, company_name: str = "",
               direction: str = "") -> list:
    """
    1銘柄のニュースを全有効ソースから取得してマージして返す。
    """
    all_items = []

    if SOURCE_CONFIGS["kabutan"]["enabled"]:
        cfg = SOURCE_CONFIGS["kabutan"]
        time.sleep(cfg["delay_seconds"])
        all_items.extend(_fetch_kabutan(code, target_date, cfg))

    if SOURCE_CONFIGS["google_news"]["enabled"]:
        cfg = SOURCE_CONFIGS["google_news"]
        time.sleep(cfg["delay_seconds"])
        gnews_raw = _fetch_google_news(code, company_name, target_date, cfg, direction=direction)
        gnews_filtered = _filter_relevant_gnews(gnews_raw, code, company_name)
        existing_titles = {_normalize_title(it["title"]) for it in all_items}
        for it in gnews_filtered:
            if _normalize_title(it["title"]) not in existing_titles:
                all_items.append(it)
                existing_titles.add(_normalize_title(it["title"]))

    all_items.sort(key=lambda x: (abs((x["date"] - target_date).days), x["dt"]))
    return all_items


def fetch_news_batch(stocks: list) -> dict:
    """
    複数銘柄のニュースを並行取得する（Phase 1）。
    stocks: [{"code": str, "name": str, "date": date, "direction": str}, ...]
    returns: {code: [news_items], ...}
    """
    results = {}

    def _fetch_one(s):
        return s["code"], fetch_news(s["code"], s["date"], s.get("name", ""), s.get("direction", ""))

    with ThreadPoolExecutor(max_workers=NEWS_FETCH_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, s): s["code"] for s in stocks}
        done = 0
        for future in as_completed(futures):
            try:
                code, news = future.result()
                results[code] = news
            except Exception as e:
                code = futures[future]
                results[code] = []
                print(f"    [ニュース取得失敗] {code}: {e}")
            done += 1
            if done % 10 == 0:
                print(f"    ニュース取得: {done}/{len(stocks)} 完了")

    return results


def summarize_news(items: list, code: str, company_name: str,
                   target_date: date, direction: str = "", change_pct: float = None):
    """
    1銘柄の Gemini 要約（個別コード指定時の後方互換用）。
    まとめて処理する場合は summarize_news_batch() を使うこと。
    """
    if not AI_SUMMARY_CONFIG.get("enabled") or not items:
        return None
    result = summarize_news_batch([{
        "code": code, "name": company_name, "date": target_date,
        "direction": direction, "change_pct": change_pct or 0.0, "news": items,
    }])
    return result.get(code)


def summarize_news_batch(stock_data: list) -> dict:
    """
    複数銘柄のニュースを AI_BATCH_SIZE 単位でまとめて Gemini に送信（Phase 2）。
    stock_data: [{"code", "name", "date", "direction", "change_pct", "news"}, ...]
    returns: {code: summary_text | None, ...}
    """
    if not AI_SUMMARY_CONFIG.get("enabled"):
        return {s["code"]: None for s in stock_data}

    try:
        from google import genai as _genai
        from config import GEMINI_API_KEY
        if not GEMINI_API_KEY:
            return {s["code"]: None for s in stock_data}
        client = _genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"    [AI要約] Gemini 初期化失敗: {e}")
        return {s["code"]: None for s in stock_data}

    batch_size = AI_SUMMARY_CONFIG.get("batch_size", 5)
    results = {}

    for i in range(0, len(stock_data), batch_size):
        batch = stock_data[i:i + batch_size]
        print(f"    [AI要約] バッチ {i//batch_size + 1}/{-(-len(stock_data)//batch_size)}"
              f" ({[s['code'] for s in batch]})")
        batch_result = _call_gemini_batch(client, batch)
        results.update(batch_result)

    return results


def get_strategy_description() -> str:
    """現在の有効ソース一覧を返す（ログ表示用）。"""
    active = [cfg["description"] for cfg in SOURCE_CONFIGS.values() if cfg["enabled"]]
    if AI_SUMMARY_CONFIG.get("enabled"):
        batch_size = AI_SUMMARY_CONFIG.get("batch_size", 5)
        active.append(f"Gemini AI 要約（{batch_size}銘柄/バッチ）")
    return " + ".join(active)


# ══════════════════════════════════════════════════════════════
#  Gemini バッチ呼び出し（内部）
# ══════════════════════════════════════════════════════════════

def _call_gemini_batch(client, batch: list) -> dict:
    """
    batch（最大 batch_size 銘柄）を1回の Gemini 呼び出しで要約する。
    429 エラー時は指数バックオフでリトライ。
    """
    codes = [s["code"] for s in batch]

    # プロンプト構築
    sections = []
    for s in batch:
        if s["direction"] == "up":
            dir_label = f"値上がり（+{abs(s['change_pct']):.2f}%）"
        elif s["direction"] == "down":
            dir_label = f"値下がり（-{abs(s['change_pct']):.2f}%）"
        else:
            dir_label = "株価変動"

        name_label = s.get("name") or s["code"]
        date_str   = s["date"].strftime("%Y年%m月%d日")

        if s.get("news"):
            articles_text = "\n".join(
                f"  [{it['dt'].strftime('%m/%d %H:%M')}][{it['category']}] {it['title']}"
                for it in s["news"]
            )
        else:
            articles_text = "  （関連ニュースなし）"

        # 財務データが提供されている場合は【直近業績】セクションを追加
        fin_ctx = s.get("financials")
        if fin_ctx:
            body = f"  【直近業績（参考数値）】\n{fin_ctx}\n  【関連ニュース】\n{articles_text}"
        else:
            body = articles_text

        sections.append(f"▼ STOCK:{s['code']} {name_label} {date_str} {dir_label}\n{body}")

    output_template = "\n\n".join(
        f"STOCK:{c}\n【変動理由】\n（1〜2文）\n【背景・詳細】\n（3〜5文）\n【参考ソース】\n（媒体名を箇条書き）"
        for c in codes
    )

    prompt = f"""あなたは株式市場の専門アナリストです。
以下{len(batch)}銘柄それぞれの株価変動理由を分析してください。

{"=" * 60}
{chr(10).join(sections)}
{"=" * 60}

## 必須ルール
- 各銘柄の情報のみ使用すること（他銘柄の情報と混在禁止）
- 【直近業績】が提供されている場合、金額は「前期: XX億円 → 今期: YY億円（前期比+ZZ%）」の形式で必ず明記すること
- ニュースタイトルに数値が含まれる場合も同様に具体的な数値を明記すること
- 「増加した」「改善した」などの曖昧表現だけで数値を省略しないこと
- 「STOCK:証券コード」の行を各銘柄の先頭に必ず付けること
- 前置き・後書き・余分な説明は不要

## 出力フォーマット（全銘柄分を続けて出力）

{output_template}"""

    max_retries = AI_SUMMARY_CONFIG.get("max_retries", 3)
    delay = AI_SUMMARY_CONFIG.get("rate_limit_delay", 5.0)

    for attempt in range(max_retries):
        try:
            time.sleep(delay)
            resp = client.models.generate_content(
                model=AI_SUMMARY_CONFIG["model"],
                contents=prompt,
            )
            return _parse_batch_response(resp.text, codes)

        except Exception as e:
            err_str = str(e)
            if any(s in err_str for s in ("429", "503", "quota", "rate", "UNAVAILABLE", "overloaded")):
                wait = 30 * (attempt + 1)
                print(f"    [Gemini] 一時エラー (attempt {attempt+1}/{max_retries}): {wait}秒待機... ({err_str[:60]})")
                time.sleep(wait)
            else:
                print(f"    [Gemini] エラー {codes}: {e}")
                break

    return {code: None for code in codes}


def _parse_batch_response(text: str, codes: list) -> dict:
    """
    "STOCK:XXXX\n内容..." 形式の Gemini レスポンスを銘柄コードごとに分割する。
    """
    result = {}
    # STOCK:4文字英数字（4桁数字 + 285A のような英数混じりコード対応）の位置で分割
    parts = re.split(r"(?=STOCK:[A-Z0-9]{4})", text.strip())
    for part in parts:
        m = re.match(r"STOCK:([A-Z0-9]{4})\s*\n?([\s\S]*)", part.strip())
        if m:
            code    = m.group(1)
            content = m.group(2).strip()
            if code in codes and content:
                result[code] = content
                print(f"    [AI要約完了] {code}")

    # 取得できなかった銘柄は None
    for code in codes:
        if code not in result:
            result[code] = None

    return result


# ══════════════════════════════════════════════════════════════
#  ソース実装 1: kabutan.jp
# ══════════════════════════════════════════════════════════════

def _fetch_kabutan(code: str, target_date: date, cfg: dict) -> list:
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
    window    = cfg.get("date_window_days", 2)
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
        link  = tds[2].find("a")
        title = link.text.strip() if link else tds[2].text.strip()
        items.append({
            "dt": news_dt, "date": news_dt.date(),
            "source": "kabutan", "category": category, "title": title,
        })

    if not items:
        return []

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

_EXCLUDE_SOURCES  = {"掲示板", "Yahoo!ファイナンス 掲示板"}
_PRIORITY_SOURCES = {
    "株探", "日本経済新聞", "会社四季報オンライン", "みんかぶ",
    "ダイヤモンド・オンライン", "東洋経済オンライン", "Bloomberg",
    "Reuters", "トウシル", "マネックス証券", "SBI証券",
}


def _fetch_google_news(code: str, company_name: str, target_date: date,
                       cfg: dict, direction: str = "") -> list:
    window    = cfg.get("date_window_days", 2)
    max_items = cfg.get("max_items", 5)
    from_date = target_date - timedelta(days=window)
    to_date   = target_date + timedelta(days=window)

    name_part = company_name if company_name else ""
    if direction == "up":
        kw = "(急騰 OR 上昇 OR 好業績 OR 増益 OR 材料 OR 理由)"
    elif direction == "down":
        kw = "(急落 OR 下落 OR 悪材料 OR 減益 OR 売られ OR 材料 OR 理由)"
    else:
        kw = "(急騰 OR 上昇 OR 材料 OR 急落 OR 下落 OR 理由)"

    query = f"{code} {name_part} {kw}"
    url   = f"https://news.google.com/rss/search?q={quote(query)}&hl=ja&gl=JP&ceid=JP:ja"

    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return []
    except Exception as e:
        print(f"    [google_news] {code}: {e}")
        return []

    soup     = BeautifulSoup(r.content, "xml")
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

        if source in _EXCLUDE_SOURCES or "掲示板" in title or "BBS" in title.upper():
            continue

        try:
            pub_dt = parsedate_to_datetime(pub_el.text).replace(tzinfo=None)
        except Exception:
            continue

        pub_date = pub_dt.date()
        if not (from_date <= pub_date <= to_date):
            continue

        items.append({
            "dt": pub_dt, "date": pub_date,
            "source": "google_news", "category": source, "title": title,
            "_priority": source in _PRIORITY_SOURCES,
        })

    priority = [it for it in items if it.get("_priority")]
    others   = [it for it in items if not it.get("_priority")]
    merged   = priority + others
    for it in merged:
        it.pop("_priority", None)

    return merged[:max_items]


# ══════════════════════════════════════════════════════════════
#  ユーティリティ
# ══════════════════════════════════════════════════════════════

def _normalize_title(title: str) -> str:
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"\s+-\s+\S+$", "", t)
    return t[:50]


def _filter_relevant_gnews(items: list, code: str, company_name: str) -> list:
    keywords = [code]
    if company_name:
        keywords.append(company_name)
        if len(company_name) > 4:
            keywords.append(company_name[:4])
    return [it for it in items if any(kw in it["title"] for kw in keywords)]

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
    "rate_limit_delay":  8.0,   # バッチ間のウェイト（秒）。無料枠 10 RPM → 8秒で安全マージン確保
    "batch_size":        5,     # 1回の Gemini 呼び出しにまとめる銘柄数
    "max_retries":       3,     # 429 エラー時のリトライ回数
}

# ──────────────────────────────────────────────────────────────
#  ニュース取得設定
# ──────────────────────────────────────────────────────────────
SOURCE_CONFIGS = {
    "tdnet": {
        "enabled":            True,
        "description":        "TDnet適時開示（東証公式・PDFリンク付き）",
        "delay_seconds":      0.5,
        "max_items":          6,
        "date_window_days":   2,
    },
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
               direction: str = "", window_days: int = None) -> list:
    """
    1銘柄のニュースを全有効ソースから取得してマージして返す。
    window_days を指定するとソース既定の取得窓を上書きする（週次調査では7日等）。
    """
    all_items = []

    if SOURCE_CONFIGS["tdnet"]["enabled"]:
        cfg = dict(SOURCE_CONFIGS["tdnet"])
        if window_days:
            cfg["date_window_days"] = window_days
        all_items.extend(_fetch_tdnet(code, target_date, cfg))

    if SOURCE_CONFIGS["kabutan"]["enabled"]:
        cfg = dict(SOURCE_CONFIGS["kabutan"])
        if window_days:
            cfg["date_window_days"] = window_days
        time.sleep(cfg["delay_seconds"])
        kab = _fetch_kabutan(code, target_date, cfg)
        existing_titles = {_normalize_title(it["title"]) for it in all_items}
        for it in kab:
            if _normalize_title(it["title"]) not in existing_titles:
                all_items.append(it)
                existing_titles.add(_normalize_title(it["title"]))

    if SOURCE_CONFIGS["google_news"]["enabled"]:
        cfg = dict(SOURCE_CONFIGS["google_news"])
        if window_days:
            cfg["date_window_days"] = window_days
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
    stocks: [{"code": str, "name": str, "date": date, "direction": str,
              "window_days": int(省略可)}, ...]
    returns: {code: [news_items], ...}
    """
    results = {}

    # TDnetの日付別インデックスを先に順次取得（並行アクセスでの重複取得を防ぐ）
    if SOURCE_CONFIGS["tdnet"]["enabled"] and stocks:
        try:
            window = max(s.get("window_days") or SOURCE_CONFIGS["tdnet"]["date_window_days"]
                         for s in stocks)
            target = max(s["date"] for s in stocks)
            _prefetch_tdnet_index(target, window)
        except Exception as e:
            print(f"    [tdnet] インデックス取得失敗（スキップして続行）: {e}")

    def _fetch_one(s):
        return s["code"], fetch_news(s["code"], s["date"], s.get("name", ""),
                                     s.get("direction", ""), s.get("window_days"))

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

        parts = []

        # 銘柄プロフィール（時価総額・テーマ）: 小型株の投機/テーマ物色の文脈判断に必要
        profile_bits = []
        if s.get("market_cap"):
            profile_bits.append(f"時価総額 約{s['market_cap']/1e8:,.0f}億円")
        if s.get("themes"):
            profile_bits.append(f"所属テーマ: {s['themes']}")
        if profile_bits:
            parts.append(f"  【銘柄プロフィール】{ ' / '.join(profile_bits) }")

        # 直近の値動きと過去イベント: 連続ストップ高・暴落後リバウンド等の文脈を読むために必須
        if s.get("price_history"):
            parts.append(f"  【直近の値動き（日次騰落率）】\n  {s['price_history']}")
        if s.get("recent_events"):
            parts.append(f"  【この銘柄の直近の変動イベント（当システムの過去分析）】\n{s['recent_events']}")

        fin_ctx = s.get("financials")
        if fin_ctx:
            parts.append(f"  【直近業績（参考数値）】\n{fin_ctx}")

        if s.get("news"):
            tdnet_items = [it for it in s["news"] if it.get("source") == "tdnet"]
            other_items = [it for it in s["news"] if it.get("source") != "tdnet"]
            if tdnet_items:
                parts.append("  【適時開示（TDnet・公式）】\n" + "\n".join(
                    f"  [{it['dt'].strftime('%m/%d %H:%M')}] {it['title']}" for it in tdnet_items))
            if other_items:
                parts.append("  【関連ニュース】\n" + "\n".join(
                    f"  [{it['dt'].strftime('%m/%d %H:%M')}][{it['category']}] {it['title']}"
                    for it in other_items))
        else:
            parts.append("  【関連ニュース】\n  （関連ニュースなし）")

        sections.append(f"▼ STOCK:{s['code']} {name_label} {date_str} {dir_label}\n" + "\n".join(parts))

    from event_classifier import REASON_CATEGORIES
    cat_guide = " / ".join(f"{k}={v[1]}" for k, v in REASON_CATEGORIES.items())

    output_template = "\n\n".join(
        f"STOCK:{c}\n【分類】（下記カテゴリから1つ選ぶ）\n【変動理由】\n（1〜2文）\n【背景・詳細】\n（3〜5文）\n【参考ソース】\n（根拠にした開示・記事名を箇条書き）"
        for c in codes
    )

    prompt = f"""あなたは株式市場の専門アナリストです。
以下{len(batch)}銘柄それぞれの株価変動理由を分析してください。

{"=" * 60}
{chr(10).join(sections)}
{"=" * 60}

## 最重要ルール（変動方向との整合）
- 記述する理由は必ず変動方向と整合させること。値上がりには「上がった理由」、値下がりには「下がった理由」を書く。
  方向と逆の材料（例: 上昇日に悪材料）しか見つからない場合、それをそのまま理由にしてはならない。
- 当日の新規材料が確認できない場合は、冒頭に「新規の明確な材料は確認できず。」と正直に書き、その上で
  【直近の値動き】【直近の変動イベント】から読み取れる文脈（急落後の自律反発・連日のストップ高の継続・
  材料出尽くし・テーマ物色・小型株特有の需給/投機的売買など）を「〜の可能性（推測）」と明示して記述すること。
- 適時開示（TDnet）は公式情報であり、ニュース記事より優先して根拠にすること。

## 必須ルール
- 各銘柄の情報のみ使用すること（他銘柄の情報と混在禁止）
- 【直近業績】が提供されている場合、金額は「前期: XX億円 → 今期: YY億円（前期比+ZZ%）」の形式で必ず明記すること
- ニュースタイトルに数値が含まれる場合も同様に具体的な数値を明記すること
- 「増加した」「改善した」などの曖昧表現だけで数値を省略しないこと
- 「STOCK:証券コード」の行を各銘柄の先頭に必ず付けること
- 前置き・後書き・余分な説明は不要
- 「AIが解説」「値動きの背景をAIが解説」等のYahoo Finance自動生成ページは情報源として使用しないこと
- 【参考ソース】には実際に根拠として使った開示・記事のタイトルを書くこと（使っていない媒体名を並べない）

## 分類ルール（【分類】に書く理由カテゴリ・下記から必ず1つのキーを選ぶ）
{cat_guide}
- 決算短信への反応は earnings_beat（好感で上昇）/ earnings_miss（失望で下落）。業績予想の修正は guidance_up/guidance_down。
- 明確な個別材料が無く、所属テーマ全体やTOPICの連れ高/連れ安なら theme（テーマ物色）か market（地合い連動）。
- 前日からの急騰/急落が続いているだけなら continuation。仕手・需給的な急変なら supply_demand。
- どれにも当てはまらない・材料不明なら unknown。【分類】にはキー（例: earnings_beat）だけを書く。

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
    "STOCK:XXXX\n内容..." 形式の Gemini レスポンスを銘柄コードごとに分割し、
    {code: {"summary": 変動理由本文, "category": 分類キー|None}} を返す。
    """
    from event_classifier import REASON_CATEGORIES
    result = {}
    parts = re.split(r"(?=STOCK:[A-Z0-9]{4})", text.strip())
    for part in parts:
        m = re.match(r"STOCK:([A-Z0-9]{4})\s*\n?([\s\S]*)", part.strip())
        if not m:
            continue
        code, content = m.group(1), m.group(2).strip()
        if code not in codes or not content:
            continue
        # 【分類】キーを抽出し、本文からは分類行を除いて要約として保存
        cat = None
        cm = re.search(r"【分類】\s*[:：]?\s*([A-Za-z_]+)", content)
        if cm and cm.group(1) in REASON_CATEGORIES:
            cat = cm.group(1)
        summary = re.sub(r"【分類】.*?(?=\n【|$)", "", content, count=1, flags=re.S).strip()
        result[code] = {"summary": summary or content, "category": cat}
        print(f"    [AI要約完了] {code}{f'（{cat}）' if cat else ''}")

    for code in codes:
        if code not in result:
            result[code] = None
    return result


# ══════════════════════════════════════════════════════════════
#  ソース実装 0: TDnet 適時開示（東証公式）
# ══════════════════════════════════════════════════════════════
# 日付ごとの開示一覧ページ（I_list_{page}_{YYYYMMDD}.html）を一度だけ取得して
# インデックス化し、銘柄コードで引く。公式情報のためニュースより確実で、
# PDFへの直接リンクを提供できる。TDnetの保持期間は約1ヶ月。

TDNET_BASE = "https://www.release.tdnet.info/inbs/"

# {date_str: {code4: [item, ...]}} のモジュールキャッシュ
_TDNET_CACHE: dict = {}


def _fetch_tdnet_day(d: date) -> dict:
    """1日分のTDnet開示一覧を全ページ取得し {code4: [items]} を返す。"""
    ds = d.strftime("%Y%m%d")
    if ds in _TDNET_CACHE:
        return _TDNET_CACHE[ds]

    by_code: dict = {}
    page = 1
    while page <= 30:  # 安全上限（通常は数ページ）
        url = f"{TDNET_BASE}I_list_{page:03d}_{ds}.html"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
        except Exception:
            break
        if r.status_code != 200 or not r.content:
            break
        soup = BeautifulSoup(r.content, "html.parser")
        rows = soup.find_all("tr")
        found = 0
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            t_str = tds[0].get_text(strip=True)          # "19:20"
            code5 = tds[1].get_text(strip=True)          # "39940"
            title = tds[3].get_text(strip=True)
            if not re.fullmatch(r"[0-9A-Z]{5}", code5) or not re.fullmatch(r"\d{2}:\d{2}", t_str):
                continue
            a = tds[3].find("a")
            pdf = a.get("href", "") if a else ""
            pdf_url = TDNET_BASE + pdf if pdf and not pdf.startswith("http") else pdf
            try:
                hh, mm = int(t_str[:2]), int(t_str[3:])
                dt = datetime(d.year, d.month, d.day, hh, mm)
            except ValueError:
                dt = datetime(d.year, d.month, d.day)
            code4 = code5[:4]
            by_code.setdefault(code4, []).append({
                "dt": dt, "date": d,
                "source": "tdnet", "category": "適時開示",
                "title": title, "url": pdf_url,
            })
            found += 1
        if found == 0:
            break
        # 総件数からページ数を判断（"1～100件 / 全201件"）
        sum_el = soup.find("div", class_="kaijiSum")
        if sum_el:
            m = re.search(r"全\s*([\d,]+)\s*件", sum_el.get_text())
            if m and page * 100 >= int(m.group(1).replace(",", "")):
                break
        page += 1
        time.sleep(SOURCE_CONFIGS["tdnet"]["delay_seconds"])

    _TDNET_CACHE[ds] = by_code
    return by_code


def _prefetch_tdnet_index(target_date: date, window_days: int):
    """調査窓内の全日付のTDnetインデックスを事前取得する（土日はスキップ）。"""
    for i in range(window_days + 1):
        d = target_date - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        n = len(_fetch_tdnet_day(d))
        print(f"    [tdnet] {d}: {n}社の開示を取得")


def _fetch_tdnet(code: str, target_date: date, cfg: dict) -> list:
    """事前取得済みインデックスから該当銘柄の開示を返す。"""
    window    = cfg.get("date_window_days", 2)
    max_items = cfg.get("max_items", 6)
    items = []
    for i in range(window + 1):
        d = target_date - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        day_map = _fetch_tdnet_day(d)
        items.extend(day_map.get(code, []))
    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[:max_items]


# ══════════════════════════════════════════════════════════════
#  ソース実装 1: kabutan.jp
# ══════════════════════════════════════════════════════════════

def _fetch_kabutan(code: str, target_date: date, cfg: dict) -> list:
    try:
        # GHAランナーIPの遮断(405)時はRenderプロキシへ自動フォールバックする共通クライアント
        from kabutan_client import get as kabutan_get
        status, text = kabutan_get(f"stock/news?code={code}", timeout=15)
        if status != 200:
            return []
    except Exception as e:
        print(f"    [kabutan] {code}: {e}")
        return []

    soup = BeautifulSoup(text, "html.parser")
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
        href  = link.get("href", "") if link else ""
        if href and href.startswith("/"):
            href = "https://kabutan.jp" + href
        items.append({
            "dt": news_dt, "date": news_dt.date(),
            "source": "kabutan", "category": category, "title": title,
            "url": href,
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

# 全銘柄に自動生成される汎用テンプレートページのタイトルパターン（実質ゴミ情報）
# 例: Yahoo Finance「今の株価の理由は？値動きの背景をAIが解説」
_EXCLUDE_TITLE_PATTERNS = (
    "AIが解説",
    "値動きの背景をAIが解説",
    "今の株価の理由は？",
    "株価の理由は？値動きの背景",
)


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
        link_el  = it.find("link")
        if not title_el or not pub_el:
            continue

        title  = title_el.text.strip()
        source = src_el.text.strip() if src_el else "不明"
        link   = link_el.text.strip() if link_el and link_el.text else ""

        if source in _EXCLUDE_SOURCES or "掲示板" in title or "BBS" in title.upper():
            continue
        if any(pat in title for pat in _EXCLUDE_TITLE_PATTERNS):
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
            "url": link,
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

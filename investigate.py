#!/usr/bin/env python3
from __future__ import annotations
"""
銘柄の株価変動理由を調査するツール（試作版）

使い方:
  python investigate.py トヨタ
  python investigate.py 7203
  python investigate.py "ソフトバンクグループ"
"""

import sys
import os
import urllib.parse
from datetime import datetime

from config import get_conn
import feedparser
from duckduckgo_search import DDGS
import anthropic


# ───────────────────────────── DB ──────────────────────────────

def find_stock_by_name(query: str) -> list[dict]:
    """DBから銘柄名または証券コードで検索"""
    conn = get_conn()
    cur = conn.cursor()

    if query.isdigit():
        cur.execute(
            "SELECT code, name FROM stocks WHERE code = %s AND is_active = TRUE",
            (query,),
        )
    else:
        cur.execute(
            """SELECT code, name FROM stocks
               WHERE name LIKE %s AND is_active = TRUE
               ORDER BY LENGTH(name) LIMIT 10""",
            (f"%{query}%",),
        )

    results = [{"code": row[0], "name": row[1]} for row in cur.fetchall()]
    cur.close()
    conn.close()
    return results


def get_price_data(code: str):
    """DBから直近10営業日の価格データを取得"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT date, close, change_pct, volume
           FROM daily_prices
           WHERE code = %s
           ORDER BY date DESC LIMIT 10""",
        (code,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return None

    latest = rows[0]
    week_ago = rows[min(4, len(rows) - 1)]  # 5営業日前(インデックス4)

    price_now = float(latest[1])
    price_prev = float(week_ago[1])
    change_1w = (price_now - price_prev) / price_prev * 100

    return {
        "date_latest": str(latest[0]),
        "date_week_ago": str(week_ago[0]),
        "price_latest": price_now,
        "price_week_ago": price_prev,
        "change_1w_pct": change_1w,
        "change_today_pct": float(latest[2]) if latest[2] else 0.0,
        "recent": [
            {
                "date": str(r[0]),
                "close": float(r[1]),
                "change_pct": float(r[2]) if r[2] else 0.0,
            }
            for r in rows[:5]
        ],
    }


# ─────────────────────────── 情報収集 ───────────────────────────

def search_google_news(query: str, max_items: int = 8) -> list[dict]:
    """Google News RSSからニュースを取得（無料・APIキー不要）"""
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
    try:
        feed = feedparser.parse(url)
        return [
            {
                "title": e.get("title", ""),
                "published": e.get("published", "")[:16],
                "summary": e.get("summary", "")[:300],
                "source": e.get("source", {}).get("title", ""),
            }
            for e in feed.entries[:max_items]
        ]
    except Exception as e:
        print(f"  [Google News エラー] {e}")
        return []


def search_web(query: str, max_results: int = 6) -> list[dict]:
    """DuckDuckGoでWeb検索（無料・APIキー不要）"""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print(f"  [DuckDuckGo エラー] {e}")
        return []


def collect_all_info(code: str, stock_name: str, price_data: dict) -> dict:
    """複数ソースから情報を一括収集"""
    direction = "上昇" if price_data["change_1w_pct"] > 0 else "下落"
    month_str = datetime.now().strftime("%Y年%m月")

    news_queries = [
        f"{stock_name} 株価 {direction}",
        f"{code} {stock_name} 決算 ニュース",
        f"{stock_name} 株 材料 話題",
    ]
    web_queries = [
        f"{stock_name} 株価 {direction} 理由 {month_str}",
        f"{stock_name} 株 最新情報",
    ]

    all_news, seen = [], set()
    for q in news_queries:
        for a in search_google_news(q, max_items=5):
            if a["title"] not in seen:
                seen.add(a["title"])
                all_news.append(a)

    all_web = []
    for q in web_queries:
        all_web.extend(search_web(q, max_results=5))

    return {"news": all_news[:15], "web": all_web[:10]}


# ─────────────────────────── Claude 分析 ───────────────────────

def analyze_with_claude(code: str, stock_name: str, price_data: dict, info: dict) -> str:
    """Claude Haiku APIで変動理由を分析"""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を自動参照

    direction = "上昇" if price_data["change_1w_pct"] > 0 else "下落"

    price_history = "\n".join(
        f"  {r['date']}: {r['close']:.0f}円 ({r['change_pct']:+.2f}%)"
        for r in price_data["recent"]
    )
    news_text = "\n".join(
        f"- [{a.get('source', '不明')}] {a['title']} ({a.get('published', '')})"
        for a in info["news"][:12]
    ) or "（ニュースなし）"
    web_text = "\n".join(
        f"- {r.get('title', '')}: {r.get('body', '')[:200]}"
        for r in info["web"][:6]
    ) or "（Web情報なし）"

    prompt = f"""あなたは日本株の株式アナリストです。以下の情報をもとに、{stock_name}（証券コード：{code}）の株価変動理由を分析してください。

【株価データ】
- 直近1週間の変動: {price_data['change_1w_pct']:+.1f}% ({direction})
- {price_data['date_week_ago']} 終値: {price_data['price_week_ago']:.0f}円
- {price_data['date_latest']} 終値: {price_data['price_latest']:.0f}円

直近5営業日の推移:
{price_history}

【最新ニュース（Google News）】
{news_text}

【Web検索結果（DuckDuckGo）】
{web_text}

---
以下の形式で回答してください：

## {stock_name}（{code}）株価{direction}の主な理由

（重要度の高い順に3〜5点、箇条書きで）

## 補足・注意点

（情報が不十分な場合や確認が必要な点を記載。情報がない場合は「詳細不明」と明記）
"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ──────────────────────────── main ─────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    stock_input = " ".join(sys.argv[1:]).strip()

    print(f"\n{'='*55}")
    print(f"  株価変動理由 調査ツール（試作版）")
    print(f"{'='*55}")
    print(f"  入力   : {stock_input}")
    print(f"  調査日 : {datetime.now().strftime('%Y年%m月%d日 %H:%M')}")
    print(f"{'='*55}\n")

    # ① 銘柄特定
    print("銘柄を検索中...")
    candidates = find_stock_by_name(stock_input)

    if not candidates:
        print(f"銘柄が見つかりません: {stock_input}")
        sys.exit(1)

    if len(candidates) == 1:
        stock = candidates[0]
    else:
        print("複数の銘柄が見つかりました:")
        for i, c in enumerate(candidates[:5], 1):
            print(f"  {i}. {c['code']}  {c['name']}")
        try:
            choice = int(input(f"番号を選択 (1-{min(5, len(candidates))}): ").strip())
            stock = candidates[choice - 1]
        except (ValueError, IndexError):
            stock = candidates[0]

    code, stock_name = stock["code"], stock["name"]
    print(f"  → {code}  {stock_name}\n")

    # ② 株価データ
    print("株価データを取得中...")
    price_data = get_price_data(code)
    if not price_data:
        print(f"株価データがありません: {code}")
        sys.exit(1)

    arrow = "↑" if price_data["change_1w_pct"] > 0 else "↓"
    print(f"  1週間変動: {price_data['change_1w_pct']:+.1f}% {arrow}")
    print(f"  株価: {price_data['price_week_ago']:.0f}円 → {price_data['price_latest']:.0f}円\n")

    # ③ 情報収集
    print("ニュース・Web情報を収集中...")
    info = collect_all_info(code, stock_name, price_data)
    print(f"  ニュース: {len(info['news'])}件  /  Web: {len(info['web'])}件\n")

    # ④ Claude 分析
    print("Claude で分析中...")
    analysis = analyze_with_claude(code, stock_name, price_data, info)

    # ⑤ 結果出力
    print(f"\n{'='*55}")
    print(analysis)

    if info["news"]:
        print(f"\n{'='*55}")
        print("  参照ニュース（上位5件）")
        print(f"{'='*55}")
        for i, a in enumerate(info["news"][:5], 1):
            print(f"{i}. {a['title']}")
            print(f"   [{a.get('source', '不明')}] {a.get('published', '')}")

    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    main()

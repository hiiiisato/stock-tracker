"""
ファンド月次レポートの定期取込・示唆抽出（ファンドウォッチ機能）
================================================================
中小型・割安成長株ファンド等の月次レポートPDFを毎月取得し、Geminiで
「保有銘柄とその理由」「マクロ環境」「今後の投資戦略」を構造化抽出する。

設計:
  - fund_master  : 監視対象ファンドのマスタ（PDF URLパターンを保持）
  - fund_reports : 取り込んだレポート1件（月1本）。要約結果をJSONで保存
  - PDF取得は先頭数ページ（組入銘柄・市場動向・銘柄紹介）のみをGeminiに渡す。
    末尾の申込メモ・費用・販売会社一覧はどのファンドも定型文でノイズなので除外。
  - 著作権配慮: レポート全文は保存・表示しない（内部の抽出処理にのみ使用）。
    表示するのはGeminiによる要約・抽出結果のみ。

実行:
  python3 fund_watch.py               # 全ファンドの最新レポートを確認・取込
  python3 fund_watch.py --fund jnext  # 指定ファンドのみ
"""

from __future__ import annotations
import os
import re
import sys
import json
import time
from datetime import date, datetime

import requests
from dotenv import load_dotenv

from config import get_conn, bulk_upsert

load_dotenv()

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
PDF_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
PAGES_TO_USE = 4  # レポート冒頭何ページ分をGeminiに渡すか（組入銘柄〜銘柄紹介まで）


# ─────────────────────────────────────────────────────────────────────────────
# ファンドマスタ（Phase1: SBI系2ファンドのみ。追加は fund_master に INSERT するだけ）
# ─────────────────────────────────────────────────────────────────────────────
# url_template は {yymm} (例: 2607) を埋め込んで月次PDFのURLを組み立てる。
# 運用会社ごとにサイト構造が異なるため、ファンド追加時はこのパターン調査が必要。
FUND_DEFS = [
    {
        "fund_key": "jnext",
        "fund_name": "SBI中小型成長株ファンド ジェイネクスト",
        "company": "SBIアセットマネジメント",
        "report_page_url": "https://www.sbiam.co.jp/fund/report/sa_2005020103.html",
        "url_template": "https://www.sbiam.co.jp/fund/pdf/89311052_jnext_mr_{yymm}.pdf",
    },
    {
        "fund_key": "jrevive",
        "fund_name": "SBI中小型割安成長株ファンド ジェイリバイブ",
        "company": "SBIアセットマネジメント",
        "report_page_url": "https://www.sbiam.co.jp/fund/report/sa_2006073104.html",
        "url_template": "https://www.sbiam.co.jp/fund/pdf/89311067_jrevive_mr_{yymm}.pdf",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# テーブル
# ─────────────────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fund_master (
            fund_key         VARCHAR(30) PRIMARY KEY,
            fund_name        VARCHAR(120) NOT NULL,
            company          VARCHAR(80),
            report_page_url  VARCHAR(255),
            url_template     VARCHAR(255),
            is_active        TINYINT DEFAULT 1,
            created_at       DATETIME DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fund_reports (
            id                 INT AUTO_INCREMENT PRIMARY KEY,
            fund_key           VARCHAR(30) NOT NULL,
            report_date        DATE NOT NULL,
            pdf_url            VARCHAR(255),
            holdings_json      MEDIUMTEXT,
            extra_stocks_json  MEDIUMTEXT,
            macro_view         TEXT,
            strategy           TEXT,
            created_at         DATETIME DEFAULT NOW(),
            UNIQUE KEY uq_fund_date (fund_key, report_date)
        )
    """)
    conn.commit()
    cur.close(); conn.close()


def sync_fund_master():
    """FUND_DEFS を fund_master に反映（新規追加・URL更新）。"""
    ensure_tables()
    conn = get_conn(); cur = conn.cursor()
    rows = [(f["fund_key"], f["fund_name"], f["company"], f["report_page_url"], f["url_template"])
            for f in FUND_DEFS]
    bulk_upsert(cur, "fund_master",
                ["fund_key", "fund_name", "company", "report_page_url", "url_template"],
                rows, update_cols=["fund_name", "company", "report_page_url", "url_template"])
    conn.commit()
    cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PDF取得（URLパターンから直近数ヶ月を試行）
# ─────────────────────────────────────────────────────────────────────────────

def _try_fetch_pdf(url_template: str, yymm: str) -> bytes | None:
    url = url_template.format(yymm=yymm)
    try:
        r = requests.get(url, headers=PDF_HEADERS, timeout=20)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content
    except Exception:
        pass
    return None


def find_latest_report_pdf(url_template: str, months_back: int = 3) -> tuple[bytes, str, date] | None:
    """直近 months_back ヶ月分を新しい順に試し、最初に見つかったPDFを返す。
    戻り値: (pdf_bytes, url, report_date(月末近似)) または None。"""
    today = date.today()
    y, m = today.year, today.month
    for i in range(months_back):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        yymm = f"{yy % 100:02d}{mm:02d}"
        content = _try_fetch_pdf(url_template, yymm)
        if content:
            url = url_template.format(yymm=yymm)
            # report_date は月末近似（正確な基準日はPDF本文からGeminiが抽出する）
            rd = date(yy, mm, 1)
            return content, url, rd
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PDFテキスト抽出
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes, max_pages: int = PAGES_TO_USE) -> str:
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = min(max_pages, len(pdf.pages))
        texts = [pdf.pages[i].extract_text() or "" for i in range(n)]
    return "\n\n".join(texts)


# ─────────────────────────────────────────────────────────────────────────────
# Gemini 構造化抽出
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(fund_name: str, report_text: str) -> str:
    return f"""以下は投資信託「{fund_name}」の月次レポートの抜粋です。
このテキストから、個人投資家向けの示唆を以下のJSON形式で抽出してください。

重要な注意点:
- 「組入銘柄のご紹介」等の見出しで個別銘柄が紹介されている場合、そこに明記されている銘柄名・証券コードを一字一句そのまま転記すること。他の銘柄と混同しないこと。紹介されている銘柄が複数あれば全て拾うこと。
- その銘柄が「組入上位10銘柄」の一覧にも含まれる場合は、その銘柄のreasonフィールドに紹介文の内容を要約して入れる。含まれない場合(圏外の銘柄)はholdings配列には追加せず、別途extra_mentioned_stocks配列に入れる。
- 数値や固有名詞を創作しないこと。テキストに書かれていないことは書かない。

出力形式(JSON):
{{
  "report_date": "レポートの基準日(YYYY-MM-DD形式)",
  "holdings": [
    {{"code": "証券コード", "name": "銘柄名", "weight_pct": 比率(数値), "reason": "紹介コーナーでの言及があればその要約。無ければnull"}}
  ],
  "extra_mentioned_stocks": [
    {{"code": "証券コード", "name": "銘柄名", "reason": "要約"}}
  ],
  "macro_view": "当月の市場動向・マクロ環境の要約(200字程度)",
  "strategy": "今後の投資戦略・銘柄選別方針の要約(150字程度)"
}}

組入銘柄は上位10銘柄をすべて含めてください。extra_mentioned_stocksは無ければ空配列[]にしてください。
JSON以外の文字は一切出力しないでください。

--- レポート本文 ---
{report_text}
"""


def _call_gemini(prompt: str) -> dict | None:
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_KEY)
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw = resp.text or ""
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
        json_str = m.group(1) if m else raw
        return json.loads(json_str)
    except Exception as e:
        print(f"    [Gemini] エラー: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 1ファンド処理
# ─────────────────────────────────────────────────────────────────────────────

def process_fund(fund_key: str, fund_name: str, url_template: str, force: bool = False) -> str:
    """最新レポートを取得・解析・保存する。戻り値は状態文字列。"""
    found = find_latest_report_pdf(url_template)
    if not found:
        return "not_found"
    pdf_bytes, pdf_url, _approx_date = found

    text = _extract_pdf_text(pdf_bytes)
    if not text.strip():
        return "no_text"

    prompt = _build_prompt(fund_name, text)
    data = _call_gemini(prompt)
    if not data:
        return "gemini_error"

    try:
        report_date = datetime.strptime(data["report_date"], "%Y-%m-%d").date()
    except Exception:
        report_date = _approx_date

    conn = get_conn(); cur = conn.cursor()
    if not force:
        cur.execute("SELECT 1 FROM fund_reports WHERE fund_key=%s AND report_date=%s",
                     (fund_key, report_date))
        if cur.fetchone():
            cur.close(); conn.close()
            return "already_exists"

    cur.execute("""
        INSERT INTO fund_reports
            (fund_key, report_date, pdf_url, holdings_json, extra_stocks_json, macro_view, strategy)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            pdf_url=VALUES(pdf_url), holdings_json=VALUES(holdings_json),
            extra_stocks_json=VALUES(extra_stocks_json),
            macro_view=VALUES(macro_view), strategy=VALUES(strategy)
    """, (
        fund_key, report_date, pdf_url,
        json.dumps(data.get("holdings", []), ensure_ascii=False),
        json.dumps(data.get("extra_mentioned_stocks", []), ensure_ascii=False),
        data.get("macro_view", ""), data.get("strategy", ""),
    ))
    conn.commit()
    cur.close(); conn.close()
    return "ok"


# ─────────────────────────────────────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────────────────────────────────────

def run(target_key: str | None = None, force: bool = False):
    sync_fund_master()
    targets = [f for f in FUND_DEFS if (target_key is None or f["fund_key"] == target_key)]
    print(f"=== ファンドウォッチ: {len(targets)}ファンド処理 ===")
    results = {}
    for f in targets:
        print(f"  [{f['fund_key']}] {f['fund_name']} を確認中...")
        status = process_fund(f["fund_key"], f["fund_name"], f["url_template"], force=force)
        results[f["fund_key"]] = status
        print(f"    → {status}")
        time.sleep(5)  # Gemini無料枠 RPM対策
    print("完了:", results)
    return results


if __name__ == "__main__":
    args = sys.argv[1:]
    key = None
    if "--fund" in args:
        key = args[args.index("--fund") + 1]
    force = "--force" in args
    run(target_key=key, force=force)

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
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

from config import get_conn, bulk_upsert

load_dotenv()

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
PDF_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
PAGES_TO_USE = 4  # レポート冒頭何ページ分をGeminiに渡すか（url_mode=template のデフォルト。page_range指定時は無視）


# ─────────────────────────────────────────────────────────────────────────────
# ファンドマスタ（追加は fund_master に INSERT するだけ、ではなく FUND_DEFS に追記）
# ─────────────────────────────────────────────────────────────────────────────
# url_mode:
#   "template" : url_template の {yymm} (例: 2607) を埋め込んで直近数ヶ月のURLを順に試行（SBI系）
#   "scrape"   : report_page_url を取得し、pdf_pattern に一致する最新リンクを抽出（結い2101等、
#                PDFファイル名が規則的でなくURL予測できないファンド向け）
# page_range: Geminiに渡すページ範囲 (start, end) 0-indexで [start, end)。
#             未指定ならtemplateモードは先頭 PAGES_TO_USE ページを使う。
#             レイアウトが崩れて文字化けする表紙ページ等はここで除外する。
FUND_DEFS = [
    {
        "fund_key": "jnext",
        "fund_name": "SBI中小型成長株ファンド ジェイネクスト",
        "company": "SBIアセットマネジメント",
        "url_mode": "template",
        "report_page_url": "https://www.sbiam.co.jp/fund/report/sa_2005020103.html",
        "url_template": "https://www.sbiam.co.jp/fund/pdf/89311052_jnext_mr_{yymm}.pdf",
    },
    {
        "fund_key": "jrevive",
        "fund_name": "SBI中小型割安成長株ファンド ジェイリバイブ",
        "company": "SBIアセットマネジメント",
        "url_mode": "template",
        "report_page_url": "https://www.sbiam.co.jp/fund/report/sa_2006073104.html",
        "url_template": "https://www.sbiam.co.jp/fund/pdf/89311067_jrevive_mr_{yymm}.pdf",
    },
    {
        "fund_key": "yui2101",
        "fund_name": "結い2101",
        "company": "鎌倉投信",
        "url_mode": "scrape",
        "report_page_url": "https://www.kamakuraim.jp/about-yui2101/monthly-report/",
        "pdf_pattern": r"_files/[\w\-]+/yuidayori\d{6}\.pdf",
        "page_range": (1, 6),  # 表紙(縦書きで文字化け)を除いた2〜6ページ目。組入上位10銘柄・市況解説を含む
    },
    {
        "fund_key": "obune_japan",
        "fund_name": "農林中金〈パートナーズ〉おおぶねJAPAN",
        "company": "農林中金バリューインベストメンツ",
        "url_mode": "template",
        "report_page_url": "https://www.nvic.co.jp/fund/obune_japan/",
        # {yymm}=対象期間、{uy4}/{um2}=公開月(期間の翌月)。例: 2026年5月分は2026/06にuploadされる。
        "url_template": "https://www.nvic.co.jp/wp/wp-content/uploads/{uy4}/{um2}/id200001_report1_{yymm}.pdf",
        "page_range": (3, 7),  # 運用実績・組入資産の状況(組入上位10銘柄)・CIOコメント・当月の運用コメント(市況動向)
    },
    {
        "fund_key": "senko_japan",
        "fund_name": "厳選ジャパン",
        "company": "アセットマネジメントOne",
        "url_mode": "direct",  # 固定URLが常に最新月のPDFを指す（日付を含まない）
        "report_page_url": "https://www.am-one.co.jp/fund/summary/118591/",
        "pdf_url": "https://www.am-one.co.jp/fund/pdf/118591/118591_mr.pdf",
        "page_range": (0, 3),  # 運用実績・組入上位10銘柄・マーケット動向とファンドの動き/今後の見通し
    },
    {
        "fund_key": "saikou",
        "fund_name": "fundnoteTOB企業価値ジャッジファンド（匠のファンド さいこう）",
        "company": "fundnote",
        "url_mode": "template",
        "report_page_url": "https://www.fundnote.co.jp/fund/saikou/",
        "url_template": "https://www.fundnote.co.jp/wp-content/uploads/fund-reports/fund_saikou_report_{y4}_{m2}.pdf",
        "page_range": (0, 2),  # 市場動向・運用状況・見通し(p1)、組入上位10銘柄+個別コメント(p2)
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
    rows = [(f["fund_key"], f["fund_name"], f["company"], f["report_page_url"], f.get("url_template", ""))
            for f in FUND_DEFS]
    bulk_upsert(cur, "fund_master",
                ["fund_key", "fund_name", "company", "report_page_url", "url_template"],
                rows, update_cols=["fund_name", "company", "report_page_url", "url_template"])
    conn.commit()
    cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PDF取得
# ─────────────────────────────────────────────────────────────────────────────

def _try_fetch_pdf(url_template: str, yy: int, mm: int) -> bytes | None:
    # yymm = 対象期間の2桁年月(例: 2605)。y4/m2 = 対象期間の4桁年/2桁月(例: 2026/05、fundnote等)。
    # uy4/um2 = 公開月(期間の翌月、例: 2026/06、NVIC等)。テンプレートに無いプレースホルダは無視される。
    uy, um = (yy, mm + 1) if mm < 12 else (yy + 1, 1)
    yymm = f"{yy % 100:02d}{mm:02d}"
    url = url_template.format(yymm=yymm, y4=f"{yy:04d}", m2=f"{mm:02d}", uy4=f"{uy:04d}", um2=f"{um:02d}")
    try:
        r = requests.get(url, headers=PDF_HEADERS, timeout=20)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content, url
    except Exception:
        pass
    return None


def _find_latest_report_pdf_by_template(url_template: str, months_back: int = 3) -> tuple[bytes, str, date] | None:
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
        found = _try_fetch_pdf(url_template, yy, mm)
        if found:
            content, url = found
            # report_date は月末近似（正確な基準日はPDF本文からGeminiが抽出する）
            rd = date(yy, mm, 1)
            return content, url, rd
    return None


def _find_latest_report_pdf_by_scrape(page_url: str, pdf_pattern: str) -> tuple[bytes, str, date] | None:
    """一覧ページのHTMLを取得し、pdf_pattern に一致する最初の(最新の)リンクを辿る。
    <base href> があればそれを起点に相対パスを解決する（無ければ page_url 自体を起点にする）。"""
    try:
        r = requests.get(page_url, headers=PDF_HEADERS, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception:
        return None

    m = re.search(pdf_pattern, html)
    if not m:
        return None

    base_m = re.search(r'<base\s+href=["\']([^"\']+)["\']', html, re.I)
    base = base_m.group(1) if base_m else page_url
    pdf_url = urljoin(base, m.group(0))

    try:
        rp = requests.get(pdf_url, headers=PDF_HEADERS, timeout=20)
        if rp.status_code == 200 and rp.content[:4] == b"%PDF":
            return rp.content, pdf_url, date.today().replace(day=1)
    except Exception:
        pass
    return None


def _find_latest_report_pdf_direct(pdf_url: str) -> tuple[bytes, str, date] | None:
    """固定URLが常に最新の月次レポートを指すサイト向け（URL自体に日付が入らない）。"""
    try:
        r = requests.get(pdf_url, headers=PDF_HEADERS, timeout=20)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content, pdf_url, date.today().replace(day=1)
    except Exception:
        pass
    return None


def find_latest_report_pdf(fund: dict, months_back: int = 3) -> tuple[bytes, str, date] | None:
    mode = fund.get("url_mode", "template")
    if mode == "scrape":
        return _find_latest_report_pdf_by_scrape(fund["report_page_url"], fund["pdf_pattern"])
    if mode == "direct":
        return _find_latest_report_pdf_direct(fund["pdf_url"])
    return _find_latest_report_pdf_by_template(fund["url_template"], months_back)


# ─────────────────────────────────────────────────────────────────────────────
# PDFテキスト抽出
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes, page_range: tuple[int, int] | None = None,
                       max_pages: int = PAGES_TO_USE) -> str:
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n_total = len(pdf.pages)
        if page_range:
            start, end = page_range
            idxs = range(max(0, start), min(end, n_total))
        else:
            idxs = range(min(max_pages, n_total))
        texts = [pdf.pages[i].extract_text() or "" for i in idxs]
    return "\n\n".join(texts)


# ─────────────────────────────────────────────────────────────────────────────
# Gemini 構造化抽出
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(fund_name: str, report_text: str) -> str:
    return f"""以下は投資信託「{fund_name}」の月次レポートの抜粋です。
このテキストから、個人投資家向けの示唆を以下のJSON形式で抽出してください。

重要な注意点:
- 「組入上位10銘柄」等の一覧表がある場合、そこに明記された証券コード・銘柄名・比率(weight_pct)を一字一句正確に転記すること。
- 「組入銘柄のご紹介」「投資先の「いい会社」紹介」等の特集記事コーナーで個別銘柄が深く紹介されている場合、その銘柄名・証券コード(記載があれば)を正確に拾うこと。複数あれば全て拾うこと。
- 特集記事で紹介された銘柄が「組入上位10銘柄」の一覧にも含まれる場合、その銘柄のreasonフィールドには一覧表の短い説明ではなく特集記事本文の要約を優先して入れる。一覧に含まれない場合(圏外の銘柄)はholdings配列には追加せず、別途extra_mentioned_stocks配列に入れる。証券コードが本文になければcodeはnullにする。
- 数値や固有名詞を創作しないこと。テキストに書かれていないことは書かない。
- ファンドによっては「当月の市場動向・マクロ環境」を独立した見出しで解説しない場合がある。その場合でも基準価額の増減理由や個別銘柄の株価変動理由などから市場環境に関する記述が読み取れればそれを要約する。全く読み取れない場合のみmacro_viewを空文字列にする。

出力形式(JSON):
{{
  "report_date": "レポートの基準日(YYYY-MM-DD形式)",
  "holdings": [
    {{"code": "証券コード", "name": "銘柄名", "weight_pct": 比率(数値、無ければnull), "reason": "紹介コーナーでの言及があればその要約。無ければnull"}}
  ],
  "extra_mentioned_stocks": [
    {{"code": "証券コードまたはnull", "name": "銘柄名", "reason": "要約"}}
  ],
  "macro_view": "当月の市場動向・マクロ環境の要約(200字程度、無ければ空文字列)",
  "strategy": "今後の投資戦略・銘柄選別方針の要約(150字程度)"
}}

組入銘柄の一覧表がある場合はそこに載っている銘柄をすべて含めてください。extra_mentioned_stocksは無ければ空配列[]にしてください。
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

def process_fund(fund: dict, force: bool = False) -> str:
    """最新レポートを取得・解析・保存する。戻り値は状態文字列。"""
    fund_key, fund_name = fund["fund_key"], fund["fund_name"]
    found = find_latest_report_pdf(fund)
    if not found:
        return "not_found"
    pdf_bytes, pdf_url, _approx_date = found

    text = _extract_pdf_text(pdf_bytes, page_range=fund.get("page_range"))
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
        status = process_fund(f, force=force)
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

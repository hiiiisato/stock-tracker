"""東証上場日を公式JPXデータから履歴として保存する。

初回はJPXデータポータル「企業・銘柄」の全件CSVを取り込む。以後はJPXの
新規上場会社情報を日次で確認し、新しい銘柄を自動追記する。銘柄マスタの現在値へ
上場日を埋め込まず、再上場や組織再編を将来扱えるよう履歴表を正本にする。

初回一括取込:
    python stock_listings.py --csv /path/to/Issues_YYYYMMDDHHMMSS.csv

日次差分:
    python stock_listings.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from config import get_conn


JPX_ISSUES_URL = "https://clientportal.jpx.co.jp/ClientPortal/s/Issue?language=ja"
JPX_NEW_LISTINGS_URL = "https://www.jpx.co.jp/listing/stocks/new/index.html"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0; +https://www.jpx.co.jp/)"
}
_CODE_RE = re.compile(r"^[0-9]{3}[0-9A-Z]$")
_DATE_RE = re.compile(r"((?:19|20)\d{2})[/-](\d{1,2})[/-](\d{1,2})")


def ensure_schema(cur) -> None:
    """上場履歴表を追加する。既存stocksの列は変更しない。"""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_listing_history (
            code                 VARCHAR(10) NOT NULL,
            exchange             VARCHAR(16) NOT NULL,
            listing_date         DATE NOT NULL,
            delisting_date       DATE,
            company_name         VARCHAR(255),
            product_type         VARCHAR(32),
            market_segment       VARCHAR(64),
            source               VARCHAR(32) NOT NULL,
            source_url           VARCHAR(2048) NOT NULL,
            source_observed_at   DATE NOT NULL,
            source_payload_hash  CHAR(64) NOT NULL,
            updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (code, exchange, listing_date),
            KEY idx_listing_current (code, delisting_date, listing_date)
        )
        """
    )


def normalize_jpx_code(value) -> str | None:
    """JPX CSVの5文字コード（30030/130A0）をアプリの4文字へそろえる。"""
    if value is None:
        return None
    raw = str(value).strip().upper()
    if len(raw) == 5 and raw.endswith("0"):
        raw = raw[:4]
    return raw if _CODE_RE.fullmatch(raw) else None


def _parse_date(value) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("JPX CSVの文字コードを判定できません")


def parse_issues_csv(content: bytes) -> list[tuple]:
    """JPX全件CSVから国内株式だけを上場履歴行へ変換する。"""
    payload_hash = hashlib.sha256(content).hexdigest()
    reader = csv.DictReader(io.StringIO(_decode_csv(content)))
    required = {"更新日", "銘柄コード", "銘柄名称", "商品分類", "東証上場日"}
    if not reader.fieldnames or not required <= set(reader.fieldnames):
        raise ValueError("JPX全件CSVの必須列が見つかりません")

    rows = []
    for item in reader:
        if (item.get("商品分類") or "").strip() != "株式":
            continue
        if (item.get("国内外区分") or "国内").strip() not in ("", "国内"):
            continue
        code = normalize_jpx_code(item.get("銘柄コード"))
        listing_date = _parse_date(item.get("東証上場日"))
        observed_at = _parse_date(item.get("更新日"))
        if not code or not listing_date or not observed_at:
            continue
        rows.append(
            (
                code,
                "TSE",
                listing_date,
                _parse_date(item.get("東証上場廃止日")),
                (item.get("銘柄名称") or "").strip() or None,
                "株式",
                (item.get("市場区分") or "").strip() or None,
                "JPX_ISSUES_CSV",
                JPX_ISSUES_URL,
                observed_at,
                payload_hash,
            )
        )
    return rows


def parse_new_listings_html(
    content: bytes,
    *,
    as_of: date | None = None,
) -> list[tuple]:
    """JPX新規上場一覧から、上場日到来済みの普通株を抽出する。"""
    basis = as_of or date.today()
    payload_hash = hashlib.sha256(content).hexdigest()
    soup = BeautifulSoup(content, "html.parser")
    listing_table = None
    for table in soup.find_all("table"):
        heading = table.get_text(" ", strip=True)
        if "上場日" in heading and "コード" in heading and "会社名" in heading:
            listing_table = table
            break
    if listing_table is None:
        raise ValueError("JPX新規上場一覧の表が見つかりません")

    rows = []
    for tr in listing_table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        listing_date = _parse_date(cells[0].get_text(" ", strip=True))
        code_match = re.search(r"(?<![0-9A-Z])([0-9]{3}[0-9A-Z])(?![0-9A-Z])",
                               cells[2].get_text(" ", strip=True).upper())
        code = normalize_jpx_code(code_match.group(1)) if code_match else None
        if not code or not listing_date or listing_date > basis:
            continue
        rows.append(
            (
                code,
                "TSE",
                listing_date,
                None,
                cells[1].get_text(" ", strip=True) or None,
                "株式",
                None,
                "JPX_NEW_LISTINGS",
                JPX_NEW_LISTINGS_URL,
                basis,
                payload_hash,
            )
        )
    return rows


def _upsert_rows(cur, rows: Iterable[tuple]) -> int:
    values = list(rows)
    if not values:
        return 0
    cur.executemany(
        """
        INSERT INTO stock_listing_history (
            code, exchange, listing_date, delisting_date, company_name,
            product_type, market_segment, source, source_url,
            source_observed_at, source_payload_hash
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            delisting_date=COALESCE(VALUES(delisting_date),delisting_date),
            company_name=COALESCE(VALUES(company_name),company_name),
            product_type=COALESCE(VALUES(product_type),product_type),
            market_segment=COALESCE(VALUES(market_segment),market_segment),
            source=IF(VALUES(source)='JPX_ISSUES_CSV',VALUES(source),source),
            source_url=IF(VALUES(source)='JPX_ISSUES_CSV',VALUES(source_url),source_url),
            source_observed_at=IF(
                VALUES(source)='JPX_ISSUES_CSV',VALUES(source_observed_at),source_observed_at
            ),
            source_payload_hash=IF(
                VALUES(source)='JPX_ISSUES_CSV',VALUES(source_payload_hash),source_payload_hash
            )
        """,
        values,
    )
    return len(values)


def import_issues_csv(path: str | Path) -> int:
    """手動取得したJPX全件CSVを初回バックフィルとして取り込む。"""
    content = Path(path).read_bytes()
    rows = parse_issues_csv(content)
    if not rows:
        raise RuntimeError("JPX全件CSVから国内株式を1件も解析できませんでした")
    conn = get_conn()
    cur = conn.cursor()
    try:
        ensure_schema(cur)
        count = _upsert_rows(cur, rows)
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def refresh_new_listings() -> int:
    """JPXの公開新規上場一覧を取得し、上場日が来た銘柄を追記する。"""
    response = requests.get(JPX_NEW_LISTINGS_URL, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    rows = parse_new_listings_html(response.content)
    if not rows:
        raise RuntimeError("JPX新規上場一覧から上場済み銘柄を1件も解析できませんでした")
    conn = get_conn()
    cur = conn.cursor()
    try:
        ensure_schema(cur)
        count = _upsert_rows(cur, rows)
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def listing_date_for(cur, code: str, as_of: date | None = None) -> date | None:
    """採点基準日時点で有効な東証上場日のうち最古を返す。"""
    basis = as_of or date.today()
    cur.execute(
        """
        SELECT MIN(listing_date)
        FROM stock_listing_history
        WHERE code=%s AND exchange='TSE' AND listing_date<=%s
          AND (delisting_date IS NULL OR delisting_date>=%s)
        """,
        (code, basis, basis),
    )
    row = cur.fetchone()
    return row[0] if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="JPX公式の東証上場日を取り込む")
    parser.add_argument("--csv", help="JPXデータポータルの全件CSV（初回用）")
    args = parser.parse_args()
    if args.csv:
        count = import_issues_csv(args.csv)
        print(f"[上場履歴] JPX全件CSVから国内株式 {count:,}件を更新")
    else:
        count = refresh_new_listings()
        print(f"[上場履歴] JPX新規上場一覧から {count:,}件を確認・更新")


if __name__ == "__main__":
    main()

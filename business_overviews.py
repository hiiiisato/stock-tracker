"""
Codexが整理した事業概要を business_overviews に保存・確認するための補助モジュール。

通常バッチからは呼ばない。ユーザーがCodexに要約を依頼したタイミングで、
未処理銘柄の抽出や保存先テーブルの作成に使う。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from config import get_conn


def ensure_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS business_overviews (
            code              VARCHAR(10) PRIMARY KEY,
            overview          TEXT,
            points_json       TEXT,
            source_hash       CHAR(64),
            source_updated_at DATETIME,
            generated_by      VARCHAR(20),
            updated_at        DATETIME
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def list_pending(limit: int = 10) -> list[dict]:
    ensure_table()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.code, s.name, s.biz_updated_at, CHAR_LENGTH(s.business_description) AS desc_len
        FROM stocks s
        LEFT JOIN business_overviews b ON b.code = s.code
        LEFT JOIN stock_fundamentals f ON f.code = s.code
        LEFT JOIN watchlist w          ON w.code = s.code
        WHERE s.is_active = 1
          AND s.business_description IS NOT NULL
          AND (b.code IS NULL OR b.source_hash != SHA2(s.business_description, 256))
        ORDER BY (w.code IS NOT NULL) DESC, COALESCE(f.market_cap, 0) DESC
        LIMIT %s
    """, (int(limit),))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"code": r[0], "name": r[1], "biz_updated_at": str(r[2]) if r[2] else None, "desc_len": r[3]}
        for r in rows
    ]


def save_overview(code: str, overview: str, points: dict, generated_by: str = "codex") -> None:
    ensure_table()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT business_description, biz_updated_at, SHA2(business_description, 256)
        FROM stocks
        WHERE code = %s
    """, (code,))
    row = cur.fetchone()
    if not row or not row[0]:
        cur.close()
        conn.close()
        raise ValueError(f"{code}: business_description is empty")
    _desc, source_updated_at, source_hash = row
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO business_overviews
            (code, overview, points_json, source_hash, source_updated_at, generated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            overview          = VALUES(overview),
            points_json       = VALUES(points_json),
            source_hash       = VALUES(source_hash),
            source_updated_at = VALUES(source_updated_at),
            generated_by      = VALUES(generated_by),
            updated_at        = VALUES(updated_at)
    """, (
        code,
        overview,
        json.dumps(points, ensure_ascii=False, separators=(",", ":")),
        source_hash,
        source_updated_at,
        generated_by,
        now,
    ))
    conn.commit()
    cur.close()
    conn.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description="business_overviews operation helper")
    parser.add_argument("--ensure", action="store_true", help="create business_overviews table")
    parser.add_argument("--pending", type=int, help="show pending/stale stock codes")
    parser.add_argument("--save-json", type=Path, help="save one overview from a JSON file")
    args = parser.parse_args()

    if args.ensure:
        ensure_table()
        print("business_overviews: ensured")

    if args.pending is not None:
        for row in list_pending(args.pending):
            print(json.dumps(row, ensure_ascii=False))

    if args.save_json:
        data = json.loads(args.save_json.read_text(encoding="utf-8"))
        save_overview(data["code"], data["overview"], data.get("points", {}), data.get("generated_by", "codex"))
        print(f"business_overviews: saved {data['code']}")


if __name__ == "__main__":
    _main()

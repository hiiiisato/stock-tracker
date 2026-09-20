"""終値ベースの価格リマインダー判定と翌朝LINE送信。"""
from __future__ import annotations

import argparse
import os
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from batch_dates import JST, jst_now
from config import get_conn
from watchlist_service import ensure_schema, migrate_legacy_memos


SEND_TIME = time(7, 30)


def reached(direction: str, close: Decimal, target: Decimal) -> bool:
    return close >= target if direction == "upper" else close <= target


def check_alerts(price_date: date | None = None) -> int:
    """対象日終値を評価し、初回到達イベントを永続化する。再実行は冪等。"""
    ensure_schema()
    conn = get_conn(); cur = conn.cursor()
    try:
        if price_date is None:
            cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
            row = cur.fetchone()
            price_date = row[0] if row else None
        if price_date is None:
            return 0
        cur.execute("""
            SELECT a.id, a.code, a.direction, a.target_price, a.version, dp.close,
                   EXISTS(
                     SELECT 1 FROM stock_splits sp
                     WHERE sp.code = a.code
                       AND sp.ex_date >= a.effective_from
                       AND sp.ex_date <= %s
                   ) AS has_split
            FROM price_alerts a
            JOIN daily_prices dp ON dp.code = a.code AND dp.date = %s
            WHERE a.status = 'active' AND a.effective_from <= %s AND dp.close IS NOT NULL
        """, (price_date, price_date, price_date))
        triggered = 0
        for alert_id, code, direction, target, version, close, has_split in cur.fetchall():
            if has_split:
                cur.execute("UPDATE price_alerts SET status = 'split_review' WHERE id = %s", (alert_id,))
                continue
            if not reached(direction, Decimal(str(close)), Decimal(str(target))):
                continue
            cur.execute("""
                INSERT IGNORE INTO price_alert_events
                  (alert_id, alert_version, code, direction, target_price,
                   triggered_price, price_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (alert_id, version, code, direction, target, close, price_date))
            if cur.rowcount:
                triggered += 1
            cur.execute("""
                UPDATE price_alerts SET status = 'triggered'
                WHERE id = %s AND version = %s AND status = 'active'
            """, (alert_id, version))
        conn.commit()
        print(f"  [価格リマインダー] {price_date}: {triggered}件到達")
        return triggered
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def _format_price(value: object) -> str:
    number = Decimal(str(value))
    return f"{number:,.0f}" if number == number.to_integral() else f"{number:,.2f}"


def build_message(rows: list[tuple], base_url: str) -> str:
    heading = f"📣 価格リマインダー（{len(rows)}件）"
    lines = [heading]
    for _event_id, code, name, direction, target, close, price_date in rows:
        arrow = "↑" if direction == "upper" else "↓"
        condition = "以上" if direction == "upper" else "以下"
        line = (f"{arrow} {name or code}（{code}）\n"
                f"終値 {_format_price(close)}円｜設定 {_format_price(target)}円{condition}｜{price_date}")
        candidate = "\n\n".join(lines + [line, f"一覧: {base_url}/watchlist"])
        if len(candidate) > 4800:
            remaining = len(rows) - (len(lines) - 1)
            lines.append(f"ほか{remaining}件は一覧で確認してください")
            break
        lines.append(line)
    lines.append(f"一覧: {base_url}/watchlist")
    return "\n\n".join(lines)


def send_due_alerts(*, now: datetime | None = None, force: bool = False, dry_run: bool = False) -> bool:
    """翌暦日7:30以降の未送信イベントを1通にまとめる。"""
    ensure_schema()
    current = (now or jst_now()).astimezone(JST)
    if not force and current.time() < SEND_TIME:
        print("  [価格リマインダー] 7:30 JST前のため送信しません")
        return True

    conn = get_conn(); cur = conn.cursor()
    batch_id = None
    retry_key = None
    body = None
    try:
        cur.execute("""
            SELECT id, retry_key, body, TIMESTAMPDIFF(SECOND, created_at, NOW())
            FROM price_alert_notification_batches
            WHERE status IN ('pending', 'failed')
            ORDER BY created_at LIMIT 1
            FOR UPDATE
        """)
        batch = cur.fetchone()
        if batch:
            batch_id, retry_key, body, age_seconds = batch
            if int(age_seconds or 0) >= 24 * 60 * 60:
                cur.execute("""
                    UPDATE price_alert_notification_batches
                    SET status = 'uncertain', last_error = 'retry key expired; manual review required'
                    WHERE id = %s
                """, (batch_id,))
                conn.commit()
                print("  [価格リマインダー] 再送期限超過。画面で確認してください")
                return False
        else:
            cur.execute("""
                SELECT e.id, e.code, s.name, e.direction, e.target_price,
                       e.triggered_price, e.price_date
                FROM price_alert_events e
                LEFT JOIN stocks s ON s.code = e.code
                WHERE e.notified_at IS NULL AND e.cancelled_at IS NULL
                  AND e.notification_batch_id IS NULL AND e.price_date < %s
                ORDER BY e.price_date, e.id
                FOR UPDATE
            """, (current.date(),))
            rows = cur.fetchall()
            if not rows:
                conn.commit()
                print("  [価格リマインダー] 送信対象なし")
                return True
            base_url = os.environ.get("REPORT_BASE_URL", "https://stock-tracker-rfqn.onrender.com").rstrip("/")
            body = build_message(rows, base_url)
            if dry_run:
                conn.rollback()
                print(body)
                return True
            batch_id = str(uuid.uuid4())
            retry_key = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO price_alert_notification_batches (id, retry_key, body)
                VALUES (%s, %s, %s)
            """, (batch_id, retry_key, body))
            ids = [r[0] for r in rows]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"UPDATE price_alert_events SET notification_batch_id = %s WHERE id IN ({placeholders})",
                (batch_id, *ids),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()

    if dry_run:  # 作成済みバッチも状態を変えず本文だけ確認する。
        print(body)
        return True

    from line_notify import push_text
    sent = push_text(body, label="価格リマインダー", retry_key=retry_key)
    conn = get_conn(); cur = conn.cursor()
    try:
        if sent:
            cur.execute("""
                UPDATE price_alert_notification_batches
                SET status = 'sent', attempts = attempts + 1, last_error = NULL, sent_at = NOW()
                WHERE id = %s
            """, (batch_id,))
            cur.execute("""
                UPDATE price_alert_events SET notified_at = NOW()
                WHERE notification_batch_id = %s AND notified_at IS NULL
            """, (batch_id,))
        else:
            cur.execute("""
                UPDATE price_alert_notification_batches
                SET status = 'failed', attempts = attempts + 1, last_error = 'LINE push failed'
                WHERE id = %s
            """, (batch_id,))
        conn.commit()
        return sent
    finally:
        cur.close(); conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    ensure_schema()
    if args.migrate:
        print(f"旧メモ移行: {migrate_legacy_memos()}件")
    if args.check:
        check_alerts()
    if args.send:
        return 0 if send_due_alerts(force=args.force, dry_run=args.dry_run) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""サーバー定期処理で使う日本時間・対象日の共通ルール。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")

# 平日の最初の定期処理は16:17 JST。これより前に起動した処理は、
# GitHub Actionsで前日分が遅延して到着したものとして扱える。
NEXT_BATCH_START = time(16, 0)
MARKET_DATA_READY = time(15, 45)


def jst_now() -> datetime:
    """タイムゾーン情報を保持した現在の日本時刻を返す。"""
    return datetime.now(JST)


def jst_today(now: datetime | None = None) -> date:
    """実行ホストのTZ設定に依存しない日本の日付を返す。"""
    current = now or jst_now()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(JST).date()


def latest_sunday(day: date) -> date:
    """指定日以前の直近日曜日（指定日が日曜なら当日）。"""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def price_fetch_end_date(now: datetime | None = None) -> date:
    """Yahoo日足の取得上限日。大引けデータ確定前は前日までに限定する。"""
    current = now or jst_now()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(JST)
    if current.time() < MARKET_DATA_READY:
        return current.date() - timedelta(days=1)
    return current.date()


def resolve_evening_business_date(
    latest_price_date: date | None,
    now: datetime | None = None,
) -> date | None:
    """イブニング便が確定させる営業日を返す。

    同日中なら最新価格日を使う。日をまたいだ遅延実行は、次の日次バッチが
    始まる16:00 JSTより前に限ってDB上の最新営業日を引き継ぐ。これにより
    休場日の通常スケジュールが古い営業日を再処理することはない。
    """
    if latest_price_date is None:
        return None
    current = now or jst_now()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(JST)
    if latest_price_date > current.date():
        return None
    if latest_price_date == current.date():
        return latest_price_date
    # 日跨ぎ救済は「直前日の便が翌朝まで遅延した」場合だけに限定する。
    # 何日も価格更新が止まった状態で古い営業日を成功扱いにすると、停止が連鎖する。
    if (
        current.time() < NEXT_BATCH_START
        and latest_price_date == current.date() - timedelta(days=1)
    ):
        return latest_price_date
    return None

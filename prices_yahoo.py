"""
Yahoo Finance APIで日本株の日次価格を取得してTiDBに保存。

取得値は提供元の観測値。原本を保存してprice_pipelineが当時値・調整済み値へ正規化する。
Yahooのadjclose（配当込み）は使わない。

取得タイミング: 東京市場終了後 (15:30 JST) 以降に実行すること
"""
import time
import requests
from datetime import date, datetime, timedelta, timezone
from typing import List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
from config import get_conn, PRICE_REPAIR_DAYS

YAHOO_API = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}

# 最新日だけを差分取得すると、途中失敗で「一部銘柄だけ最新日まで保存」された場合に
# DB全体のMAX(date)が進み、残りの銘柄が永久に補完されない。毎回この期間を全銘柄
# 再取得してUPSERTすることで、PC停止・API一時障害・途中終了を次回実行時に自己修復する。
ROLLING_REPAIR_DAYS = PRICE_REPAIR_DAYS
MIN_PRICE_COVERAGE_RATIO = 0.90


class QuoteBatch(list):
    """同時に取得した分割イベントと価格観測。原本の取得基準を失わない。"""
    def __init__(self, rows=(), *, split_events=()):
        super().__init__(rows)
        self.split_events = list(split_events)
        self.observed_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _get_missing_date_range(
    conn,
    date_to: date | None = None,
    repair_days: int = ROLLING_REPAIR_DAYS,
) -> Tuple[date, date]:
    """DBの未取得期間と直近の自己修復期間を合わせた取得範囲を返す。"""
    if repair_days < 1:
        raise ValueError("repair_days must be at least 1")

    cur = conn.cursor()
    cur.execute("SELECT MAX(date) FROM daily_prices")
    last_date = cur.fetchone()[0]
    cur.close()

    target_end = date_to or date.today()

    if last_date is None:
        return date(2024, 3, 30), target_end

    repair_start = target_end - timedelta(days=repair_days - 1)
    first_missing = last_date + timedelta(days=1)
    return min(first_missing, repair_start), target_end


def _validate_recent_price_coverage(
    date_from: date,
    date_to: date,
    min_ratio: float = MIN_PRICE_COVERAGE_RATIO,
) -> list[tuple[date, int, int]]:
    """公式営業日の価格件数が有効銘柄数の閾値を満たすことを確認する。

    売買停止・上場直後・Yahoo未対応銘柄を考慮して100%一致は要求しない。一方、
    API障害や途中終了で数百〜数千銘柄が欠けた日を成功扱いにはしない。
    """
    if not 0 < min_ratio <= 1:
        raise ValueError("min_ratio must be in (0, 1]")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM stocks WHERE is_active = TRUE")
        active_count = int(cur.fetchone()[0] or 0)
        if active_count == 0:
            raise RuntimeError("有効銘柄が0件のため価格充足率を検証できません")

        cur.execute(
            """
            SELECT tc.date, COUNT(DISTINCT s.code) AS price_count
            FROM trading_calendar tc
            LEFT JOIN price_history dp
              ON dp.date = tc.date AND dp.close IS NOT NULL
            LEFT JOIN stocks s
              ON s.code = dp.code AND s.is_active = TRUE
            WHERE tc.date BETWEEN %s AND %s
              AND tc.is_holiday = FALSE
            GROUP BY tc.date
            ORDER BY tc.date
            """,
            (date_from, date_to),
        )
        coverage = [(row[0], int(row[1] or 0), active_count) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    minimum = int(active_count * min_ratio)
    if not coverage:
        raise RuntimeError(
            f"{date_from}〜{date_to}に検証可能な営業日がありません。取引カレンダーを確認してください"
        )
    insufficient = [(day, count) for day, count, _ in coverage if count < minimum]
    if insufficient:
        details = ", ".join(f"{day}={count:,}件" for day, count in insufficient)
        raise RuntimeError(
            f"価格データ不足: {details}（有効銘柄{active_count:,}件、最低{minimum:,}件）"
        )
    return coverage


def _fetch_yahoo(code4: str, date_from: date, date_to: date) -> List[dict]:
    """Yahooのquote（配当調整なし）とsplitイベントを同じ応答から取得。"""
    ticker = f"{code4}.T"
    url = YAHOO_API.format(ticker=ticker)

    jst = ZoneInfo("Asia/Tokyo")
    import_from = int(datetime.combine(date_from, datetime.min.time(), tzinfo=jst).timestamp())
    import_to = int(datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=jst).timestamp())

    for attempt in range(3):
        try:
            r = requests.get(url,
                params={"interval": "1d", "period1": import_from, "period2": import_to, "events": "splits"},
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                return []

            result = r.json().get("chart", {}).get("result")
            if not result:
                return []

            res        = result[0]
            timestamps = res.get("timestamp", [])
            quotes     = res.get("indicators", {}).get("quote", [{}])[0]
            opens      = quotes.get("open",   [])
            highs      = quotes.get("high",   [])
            lows       = quotes.get("low",    [])
            closes     = quotes.get("close",  [])
            volumes    = quotes.get("volume", [])

            rows = []
            for i, ts in enumerate(timestamps):
                close = closes[i] if i < len(closes) else None
                if close is None:
                    continue
                dt = datetime.fromtimestamp(ts, jst).date()
                # Yahoo chart APIはperiod2境界の次日（当日進行中の足を含む）を
                # 返すことがある。要求範囲外はレスポンス受領後にも必ず除外する。
                if not date_from <= dt <= date_to:
                    continue

                rows.append({
                    "code":       code4,
                    "date":       str(dt),
                    "open":       opens[i] if i < len(opens) else None,
                    "high":       highs[i] if i < len(highs) else None,
                    "low":        lows[i] if i < len(lows) else None,
                    "close":      close,
                    "volume":     volumes[i] if i < len(volumes) else None,
                })
            events = [{"date": str(datetime.fromtimestamp(s["date"], jst).date()),
                       "numerator": s["numerator"], "denominator": s["denominator"]}
                      for s in res.get("events", {}).get("splits", {}).values()]
            return QuoteBatch(rows, split_events=events)
        except Exception:
            time.sleep(1)
    return []


def fetch_and_store_yahoo(max_workers: int = 10, date_to: date | None = None) -> int:
    """Yahoo Financeの未取得期間＋直近14日を全銘柄再取得してUPSERT。"""
    conn = get_conn()
    date_from, date_to = _get_missing_date_range(conn, date_to)
    conn.close()

    print(f"  Yahoo取得範囲: {date_from} 〜 {date_to}（直近{ROLLING_REPAIR_DAYS}日を自己修復）")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"  対象銘柄: {len(codes)} 件 (並列{max_workers}本)")

    batches: dict[str, list[dict]] = {}
    failed: List[str] = []

    def fetch(code4):
        time.sleep(0.02)
        return code4, _fetch_yahoo(code4, date_from, date_to)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            code4, rows = future.result()
            done += 1
            if not rows:
                failed.append(code4)
            else:
                batches[code4] = rows
            if done % 1000 == 0:
                print(f"    進捗: {done}/{len(codes)} 銘柄完了")

    print(f"  取得完了: {sum(map(len,batches.values()))} 件, データなし: {len(failed)} 銘柄")

    # 現在の有効銘柄数を基準にする充足率は、新規上場前まで遡る長期初期取得には
    # 適用できない。自己修復対象の直近窓だけを厳格検証する。
    coverage_from = max(date_from, date_to - timedelta(days=ROLLING_REPAIR_DAYS - 1))

    if not batches:
        raise RuntimeError("Yahoo価格が全銘柄0件でした。取得成功にはしません")

    # 原本→検証→当時値と調整済み値の同時公開。adj_close=closeの直接上書きは禁止。
    from price_pipeline import store_yahoo_batches
    from price_store import PricePublicationHeld
    held = None
    try:
        written = store_yahoo_batches(batches, date_to)
    except PricePublicationHeld as exc:
        held, written = exc, exc.published_rows

    coverage = _validate_recent_price_coverage(coverage_from, date_to)
    if coverage:
        summary = ", ".join(f"{day}:{count:,}" for day, count, _ in coverage)
        print(f"  価格充足確認: {summary}")
    if held:
        raise held
    return written


if __name__ == "__main__":
    print("=== Yahoo Finance 価格データ更新 ===")
    n = fetch_and_store_yahoo()
    print(f"  合計 {n} 件を保存")

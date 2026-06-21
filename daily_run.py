"""
日次実行スクリプト — 毎営業日の市場終了後（16:00以降）に実行する
実行順: 銘柄マスタ → カレンダー → 価格データ → ランキング

使い方:
  python daily_run.py              # 通常の日次更新
  python daily_run.py --init       # 初回: 全期間の価格データを一括取得
  python daily_run.py --rankings   # ランキングのみ再計算
"""
import sys
import traceback
from datetime import datetime
from config import get_conn
from master import update_stock_master, update_trading_calendar
from prices import fetch_and_store_prices
from prices_yahoo import fetch_and_store_yahoo
from rankings import compute_daily_rankings, compute_weekly_rankings, print_rankings


def _log(conn, fetch_type: str, status: str, rows: int = 0, error: str = None):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO fetch_logs (fetch_type, status, rows_upserted, finished_at, error_msg)
        VALUES (%s, %s, %s, NOW(), %s)
    """, (fetch_type, status, rows, error))
    conn.commit()
    cur.close()


def run(init: bool = False, rankings_only: bool = False):
    start = datetime.now()
    print(f"\n{'='*50}")
    print(f"日次更新開始: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    conn = get_conn()

    if not rankings_only:
        # 1. 銘柄マスタ更新
        print("\n[1/4] 銘柄マスタ更新...")
        try:
            n = update_stock_master()
            print(f"  完了: {n} 銘柄")
            _log(conn, "master", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log(conn, "master", "failed", error=str(e))

        # 2. 取引カレンダー更新
        print("\n[2/4] 取引カレンダー更新...")
        try:
            n = update_trading_calendar()
            print(f"  完了: {n} 日分")
            _log(conn, "calendar", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log(conn, "calendar", "failed", error=str(e))

        # 3. 価格データ取得（J-Quants → Yahoo Finance の順で差分補完）
        print(f"\n[3/4] 価格データ取得 ({'初回一括' if init else '差分更新'})...")
        try:
            # J-Quants（フリープラン範囲内）
            n1 = fetch_and_store_prices(max_workers=4 if init else 8)
            print(f"  J-Quants: {n1} 件")
            # Yahoo Finance（J-Quantsカバー外の直近データ）
            n2 = fetch_and_store_yahoo(max_workers=10)
            print(f"  Yahoo Finance: {n2} 件")
            _log(conn, "prices", "done", n1 + n2)
        except Exception as e:
            print(f"  エラー: {e}")
            traceback.print_exc()
            _log(conn, "prices", "failed", error=str(e))

    # 4. ランキング計算
    print("\n[4/4] ランキング計算...")
    try:
        n_daily  = compute_daily_rankings()
        n_weekly = compute_weekly_rankings()
        total = n_daily + n_weekly
        print(f"  完了: 日次{n_daily}件 / 週次{n_weekly}件")
        _log(conn, "rankings", "done", total)
    except Exception as e:
        print(f"  エラー: {e}")
        _log(conn, "rankings", "failed", error=str(e))

    conn.close()

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n{'='*50}")
    print(f"完了: {elapsed:.1f}秒")

    # 結果表示
    print_rankings("daily",  "change_pct")
    print_rankings("weekly", "change_pct")


if __name__ == "__main__":
    init          = "--init" in sys.argv
    rankings_only = "--rankings" in sys.argv
    run(init=init, rankings_only=rankings_only)

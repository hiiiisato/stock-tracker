"""
日次実行スクリプト — 毎営業日の市場終了後（16:00以降）に実行する
実行順: 銘柄マスタ → カレンダー → 価格データ → テーマスコア → ランキング

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
from prices_yahoo import fetch_and_store_yahoo
from dividends import fetch_all_dividends
from financials import fetch_all_financials
from rankings import compute_daily_rankings, compute_weekly_rankings, print_rankings
from theme_score import compute_day as compute_theme_day
from fundamentals import fetch_all_known as update_fundamentals


def _log(fetch_type: str, status: str, rows: int = 0, error: str = None):
    """ログを記録する。毎回新しい接続を使い、長時間処理後の接続切れを防ぐ。"""
    try:
        c = get_conn()
        cur = c.cursor()
        cur.execute("""
            INSERT INTO fetch_logs (fetch_type, status, rows_upserted, finished_at, error_msg)
            VALUES (%s, %s, %s, NOW(), %s)
        """, (fetch_type, status, rows, error))
        c.commit()
        cur.close()
        c.close()
    except Exception as log_err:
        print(f"  [ログ記録失敗] {log_err}")


def run(init: bool = False, rankings_only: bool = False):
    start = datetime.now()
    print(f"\n{'='*50}")
    print(f"日次更新開始: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    if not rankings_only:
        # 1. 銘柄マスタ更新
        print("\n[1/4] 銘柄マスタ更新...")
        try:
            n = update_stock_master()
            print(f"  完了: {n} 銘柄")
            _log("master", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("master", "failed", error=str(e))

        # 2. 取引カレンダー更新
        print("\n[2/4] 取引カレンダー更新...")
        try:
            n = update_trading_calendar()
            print(f"  完了: {n} 日分")
            _log("calendar", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("calendar", "failed", error=str(e))

        # 3. 価格データ取得（Yahoo Finance のみ、差分更新）
        print(f"\n[3/4] 価格データ取得...")
        try:
            n2 = fetch_and_store_yahoo(max_workers=10)
            print(f"  Yahoo Finance: {n2} 件")
            # 価格データの日付を取引カレンダーに反映
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trading_calendar (date, is_holiday)
                SELECT DISTINCT date, FALSE FROM daily_prices
                ON DUPLICATE KEY UPDATE is_holiday=FALSE
            """)
            conn.commit()
            cur.close()
            conn.close()
            _log("prices", "done", n2)
        except Exception as e:
            print(f"  エラー: {e}")
            traceback.print_exc()
            _log("prices", "failed", error=str(e))

    # 4. 配当・財務・ファンダメンタルズ更新（毎週月曜のみ）
    if datetime.now().weekday() == 0 and not rankings_only:
        print("\n[4/6] 配当データ更新（週次）...")
        try:
            n = fetch_all_dividends()
            _log("dividends", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("dividends", "failed", error=str(e))

        print("\n[5/6] 財務諸表更新（週次）...")
        try:
            n = fetch_all_financials()
            _log("financials", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("financials", "failed", error=str(e))

        print("\n[6/6] ファンダメンタルズ更新（週次・テーマ銘柄）...")
        try:
            n = update_fundamentals()
            _log("fundamentals", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("fundamentals", "failed", error=str(e))

    # テーマスコア計算（価格更新後）
    if not rankings_only:
        print("\n[テーマスコア] テーマ別過熱スコアを計算中...")
        try:
            compute_theme_day()
            _log("theme_score", "done")
        except Exception as e:
            print(f"  エラー: {e}")
            _log("theme_score", "failed", error=str(e))

    # ランキング計算
    step = "5/5" if datetime.now().weekday() == 0 else "4/4"
    print(f"\n[{step}] ランキング計算...")
    try:
        n_daily  = compute_daily_rankings()
        n_weekly = compute_weekly_rankings()
        total = n_daily + n_weekly
        print(f"  完了: 日次{n_daily}件 / 週次{n_weekly}件")
        _log("rankings", "done", total)
    except Exception as e:
        print(f"  エラー: {e}")
        _log("rankings", "failed", error=str(e))

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n{'='*50}")
    print(f"完了: {elapsed:.1f}秒")

    print_rankings("daily",  "change_pct")
    print_rankings("weekly", "change_pct")


if __name__ == "__main__":
    init          = "--init" in sys.argv
    rankings_only = "--rankings" in sys.argv
    run(init=init, rankings_only=rankings_only)

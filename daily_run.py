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
from datetime import datetime, date
from config import get_conn
from master import update_stock_master, update_trading_calendar
from prices_yahoo import fetch_and_store_yahoo
from dividends import fetch_all_dividends
from financials import fetch_all_financials
from rankings import compute_daily_rankings, compute_weekly_rankings, print_rankings
from theme_score import compute_day as compute_theme_day
from fundamentals import fetch_all_known as update_fundamentals, recompute_price_metrics
from compute_price_stats import run as compute_price_stats
from financials_kabutan import run as fetch_kabutan_financials
from event_researcher import research_top_movers
from market_indices import fetch_and_store as update_market_indices, ensure_table as ensure_indices_table
from research_strategy import RESEARCH_THRESHOLD_PCT


def _detect_split_candidates(target_date: date) -> list[str]:
    """
    当日の前日比 -30% 未満（生close比較）の銘柄を株式分割候補として返す。
    split_backfill.run_for_codes() のインプットになる。
    """
    conn = get_conn()
    cur  = conn.cursor()
    # 前日の最新日（取引日）を取得して、今日との生close比を計算
    cur.execute("""
        SELECT today.code
        FROM daily_prices today
        JOIN daily_prices prev
          ON today.code = prev.code
         AND prev.date = (
               SELECT MAX(d.date) FROM daily_prices d
               WHERE d.code = today.code AND d.date < %s
             )
        WHERE today.date = %s
          AND today.close IS NOT NULL
          AND prev.close IS NOT NULL
          AND prev.close > 0
          AND today.close / prev.close < 0.70
    """, (target_date, target_date))
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return codes


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

    # 0. 主要指数データ更新（毎日・差分）
    if not rankings_only:
        print("\n[0/4] 主要指数データ更新...")
        try:
            ensure_indices_table()
            n = update_market_indices(init=False)
            print(f"  完了: {n} 件")
            _log("market_indices", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("market_indices", "failed", error=str(e))

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

    # 3.5. 株式分割 自動検知・バックフィル
    if not rankings_only:
        print("\n[分割検知] 前日比 -30% 未満の銘柄を確認中...")
        try:
            from split_backfill import run_for_codes as split_backfill_codes
            target_date = date.today()
            split_candidates = _detect_split_candidates(target_date)
            if split_candidates:
                print(f"  分割候補: {split_candidates}")
                n_split = split_backfill_codes(split_candidates)
                _log("split_auto_backfill", "done", len(split_candidates))
            else:
                print("  分割候補なし。")
        except Exception as e:
            print(f"  エラー: {e}")
            traceback.print_exc()
            _log("split_auto_backfill", "failed", error=str(e))

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

        print("\n[週次] kabutan 財務実績・業績予想更新...")
        try:
            fetch_kabutan_financials(force=True)
            _log("kabutan_financials", "done")
        except Exception as e:
            print(f"  エラー: {e}")
            _log("kabutan_financials", "failed", error=str(e))

    # テーマスコア計算（価格更新後）
    if not rankings_only:
        print("\n[テーマスコア] テーマ別過熱スコアを計算中...")
        try:
            compute_theme_day()
            _log("theme_score", "done")
        except Exception as e:
            print(f"  エラー: {e}")
            _log("theme_score", "failed", error=str(e))

    # PER/PBR/時価総額/配当利回りを最新株価で再計算（毎日）
    if not rankings_only:
        print("\n[指標再計算] PER/PBR/時価総額/配当利回りを更新中...")
        try:
            n = recompute_price_metrics()
            _log("metrics_recompute", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("metrics_recompute", "failed", error=str(e))

    # 株価テクニカル指標（MA・騰落率・乖離率・RSI など）を毎日計算
    if not rankings_only:
        print("\n[テクニカル指標] MA・騰落率・RSI・出来高比率を計算中...")
        try:
            n = compute_price_stats()
            _log("price_stats", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("price_stats", "failed", error=str(e))

    # スイングトレード候補のスコアリング → DB 保存 → LINE 通知
    if not rankings_only:
        print("\n[スイング] 候補スコアリング & DB 保存 & LINE 通知...")
        try:
            from swing_scorer import score_all, save_scores as save_swing_scores
            from swing_notifier import send as send_swing_line
            candidates = score_all()
            n_saved = save_swing_scores(candidates)
            print(f"  候補: {len(candidates)} 銘柄 → DB 保存: {n_saved} 件")
            send_swing_line(candidates)
            _log("swing_score", "done", len(candidates))
        except Exception as e:
            print(f"  エラー: {e}")
            _log("swing_score", "failed", error=str(e))

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

    # ニュース収集（日次＋週次 TOP15）
    if not rankings_only:
        print(f"\n[ニュース] 上昇/下落 ±{RESEARCH_THRESHOLD_PCT}%超えの材料を収集中...")
        try:
            n_d = research_top_movers(period="daily")
            n_w = research_top_movers(period="weekly")
            _log("events", "done", n_d + n_w)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("events", "failed", error=str(e))

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n{'='*50}")
    print(f"完了: {elapsed:.1f}秒")

    print_rankings("daily",  "change_pct")
    print_rankings("weekly", "change_pct")


if __name__ == "__main__":
    init          = "--init" in sys.argv
    rankings_only = "--rankings" in sys.argv
    run(init=init, rankings_only=rankings_only)

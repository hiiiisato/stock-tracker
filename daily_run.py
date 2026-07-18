"""
日次実行スクリプト — 毎営業日の市場終了後（16:00以降）に実行する
実行順: 銘柄マスタ → カレンダー → 価格データ → テーマスコア → ランキング

スケジューラは GitHub Actions（.github/workflows/daily.yml）:
  16:00 JST メイン / 17:00 JST リトライ（完了済みならスキップ） / 20:30 JST イブニング便

使い方:
  python daily_run.py              # 通常の日次更新（当日完了済みならスキップ）
  python daily_run.py --force     # 完了済みでも再実行
  python daily_run.py --evening   # イブニング便: 夜間開示の回収+市況考察+日次レポート確定版
  python daily_run.py --init       # 初回: 全期間の価格データを一括取得
  python daily_run.py --rankings   # ランキングのみ再計算
"""
import sys
import traceback
from datetime import datetime, timedelta, timezone
from config import get_conn


def _jst_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=9)


def _already_done_today(fetch_type: str = "daily_report") -> bool:
    """今日(JST)に指定ステップが完了済みか。リトライ実行の重複ガードに使う。"""
    jst_day_start_utc = datetime.combine(_jst_now().date(), datetime.min.time()) - timedelta(hours=9)
    try:
        c = get_conn(); cur = c.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM fetch_logs
            WHERE fetch_type = %s AND status = 'done' AND finished_at >= %s
        """, (fetch_type, jst_day_start_utc.strftime("%Y-%m-%d %H:%M:%S")))
        n = cur.fetchone()[0]
        cur.close(); c.close()
        return n > 0
    except Exception:
        return False


def _has_prices_today() -> bool:
    """今日(JST)の価格データが存在するか（=営業日か）。休場日はYahooが当日行を返さない。"""
    try:
        c = get_conn(); cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM daily_prices WHERE date = %s LIMIT 1",
                    (_jst_now().date(),))
        n = cur.fetchone()[0]
        cur.close(); c.close()
        return n > 0
    except Exception:
        return True   # 判定不能時は続行（止める方がリスク）


def run_evening():
    """イブニング便（20:30 JST）: 夜間に出た適時開示を回収し、市況考察と日次レポートを確定版に更新する。"""
    print(f"\n{'='*50}\nイブニング便開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*50}")
    if not _has_prices_today():
        print("本日は休場（当日価格データなし）のためスキップ")
        return

    print("\n[適時開示] 夜間分を含めて再取得・市況考察を更新...")
    try:
        from disclosures import run_daily as disclosures_run_daily
        result = disclosures_run_daily(with_ai=True)
        _log("disclosures_evening", "done", result.get("stored", 0))
    except Exception as e:
        print(f"  エラー: {e}")
        _log("disclosures_evening", "failed", error=str(e))

    # 夕方〜夜に出た決算・業績修正も当日中に反映（15時以降の開示が大半のためここが本命）
    print("\n[業績反映] 当日開示銘柄の業績・予想を更新...")
    try:
        from earnings_refresh import refresh_from_disclosures
        result = refresh_from_disclosures()
        _log("earnings_refresh", "done", result.get("revisions", 0))
    except Exception as e:
        print(f"  エラー: {e}")
        _log("earnings_refresh", "failed", error=str(e))

    print("\n[日次レポート] 確定版を保存...")
    try:
        from daily_report import save_report, notify_report_ready
        saved = save_report()
        _log("daily_report", "done", 1)
        # 確定版が保存できたら LINE に「完成」通知（リンクのみ）。通知失敗はレポート保存の成否に影響させない
        if saved:
            try:
                notify_report_ready(saved)
            except Exception as e:
                print(f"  [日次レポートLINE] エラー: {e}")
    except Exception as e:
        print(f"  エラー: {e}")
        _log("daily_report", "failed", error=str(e))

    # JPX公式の決算発表予定日を更新（AIファンドの決算跨ぎ管理に必須。JPXは17時頃更新）
    print("\n[決算予定日] JPX公式の決算発表予定日を取込...")
    try:
        from earnings_calendar_jpx import import_schedule as jpx_earnings
        res = jpx_earnings()
        _log("jpx_earnings_schedule", "done", res.get("codes", 0))
    except Exception as e:
        print(f"  エラー: {e}")
        _log("jpx_earnings_schedule", "failed", error=str(e))

    # AIファンドの意思決定（当日の全情報が揃った最後に実施。約定は翌営業日の寄付＝先読みなし）
    print("\n[AIファンド] 意思決定...")
    try:
        from ai_fund import decide as ai_fund_decide
        n = ai_fund_decide()
        _log("ai_fund_decide", "done", n)
    except Exception as e:
        print(f"  エラー: {e}")
        traceback.print_exc()
        _log("ai_fund_decide", "failed", error=str(e))
    print("\nイブニング便 完了")
from master import update_stock_master, update_trading_calendar
from prices_yahoo import fetch_and_store_yahoo
from dividends import fetch_all_dividends
from financials import fetch_all_financials
from rankings import compute_daily_rankings, compute_weekly_rankings, print_rankings
from theme_score import compute_day as compute_theme_day
from fundamentals import fetch_all_known as update_fundamentals, recompute_price_metrics
from compute_price_stats import run as compute_price_stats
from event_researcher import research_top_movers
from market_indices import fetch_and_store as update_market_indices, ensure_table as ensure_indices_table
from research_strategy import RESEARCH_THRESHOLD_PCT


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


def run(init: bool = False, rankings_only: bool = False, force: bool = False):
    start = datetime.now()
    print(f"\n{'='*50}")
    print(f"日次更新開始: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    # 重複実行ガード: 16:00メインが完走済みなら17:00リトライは何もしない
    if not (force or init or rankings_only) and _already_done_today("daily_report"):
        print("本日の日次更新は完了済み（リトライ実行をスキップ）。再実行は --force")
        return

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
                WHERE date >= '2000-01-01'  -- 零値日付等の毒データでINSERT全体が失敗するのを防ぐ
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

    # 休場日ガード: 当日の価格が1件も来ていない＝休場（祝日等）。
    # 元データが更新されていないのに後段（指標再計算・AI調査・レポート保存）が走るのを防ぐ。
    if not (rankings_only or init) and not _has_prices_today():
        print("\n本日は休場（当日価格データなし）。後段処理をスキップして終了します。")
        return

    # 3.5. 株式分割・併合 対応（JPX公式 J-Quants ベース）
    #   - 直近窓の新規分割を Yahoo splits で暫定検知し、該当銘柄の adj_close を再計算。
    #   - 週次で J-Quants 公式(AdjC)に同期して確定・上書き（splits.run_daily 内は日次の暫定のみ）。
    #   価格急変ヒューリスティックは値幅制限撤廃等で誤判定するため廃止。
    if not rankings_only:
        print("\n[分割対応] 直近窓の新規分割を確認中（J-Quants/Yahoo・TDnet裏取り）...")
        try:
            from splits import run_daily as splits_run_daily
            n_changed = splits_run_daily()
            _log("splits_daily", "done", n_changed)
        except Exception as e:
            print(f"  エラー: {e}")
            traceback.print_exc()
            _log("splits_daily", "failed", error=str(e))

        # 調整アーティファクト監視（closeは正常なのにadjだけ跳ねる箇所を検出→自動修復）
        try:
            from splits import run_integrity_check
            n_fixed = run_integrity_check()
            _log("splits_integrity", "done", n_fixed)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("splits_integrity", "failed", error=str(e))

    # AIファンド: 前夜に決定した注文を当日寄付で約定 → 終値でNAV記録
    # （価格・分割処理の直後＝当日の open/close が確定してから）
    if not rankings_only:
        print("\n[AIファンド] 約定・NAV記録...")
        try:
            from ai_fund import execute_orders, record_nav
            n_fill = execute_orders()
            record_nav()
            _log("ai_fund_execute", "done", n_fill)
        except Exception as e:
            print(f"  エラー: {e}")
            traceback.print_exc()
            _log("ai_fund_execute", "failed", error=str(e))

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

        print("\n[週次] TDnet決算短信XBRL 財務実績・業績予想の広域取込（過去30日・取りこぼし補完）...")
        try:
            from financials_tdnet import import_recent as tdnet_import
            res = tdnet_import(days=30)
            _log("tdnet_financials_weekly", "done", res.get("financials", 0))
        except Exception as e:
            print(f"  エラー: {e}")
            _log("tdnet_financials_weekly", "failed", error=str(e))

    # ※会社概要・kabutanテーマタグの定期メンテは夜間バッチ（misc_batch.yml 23:45）に移動。
    #   市場時間と無関係なデータのため、夕方のクリティカルパスから外して日次レポートを早める。

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

    # 理論株価（はっしゃん式）を計算 → theoretical_values に保存
    # price_stats と stock_fundamentals の最新値に依存するため、その後に実行する
    if not rankings_only:
        print("\n[理論株価] 資産価値・事業価値・理論株価・投資判断を計算中...")
        try:
            from compute_theoretical import run as compute_theoretical
            n = compute_theoretical()
            _log("theoretical_values", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("theoretical_values", "failed", error=str(e))

    # バックテスト用の週次スナップショット（直近週）を追記
    # price_stats_history に当週の全指標(PIT補正済)を upsert。過去分は
    # compute_stats_history.py --backfill で一度だけ生成済み。
    if not rankings_only:
        print("\n[週次スナップショット] バックテスト用 price_stats_history を追記中...")
        try:
            from compute_stats_history import run as compute_stats_history
            n = compute_stats_history(latest_only=True)
            _log("price_stats_history", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("price_stats_history", "failed", error=str(e))

    # スイングトレード候補のスコアリング → DB 保存（/swing ページで表示）
    if not rankings_only:
        print("\n[スイング] 候補スコアリング & DB 保存...")
        try:
            from swing_scorer import score_all, save_scores as save_swing_scores
            candidates = score_all()
            n_saved = save_swing_scores(candidates)
            print(f"  候補: {len(candidates)} 銘柄 → DB 保存: {n_saved} 件")
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

    # 資金フロー週次集計（テーマ/業種/規模/スタイル別。直近26週を再計算=自己修復）
    # テーママスタ(theme_master)の同期は misc_batch.yml の夜間便で日次実行される
    # （みんかぶを正とし120テーマ/日巡回）。money_flow はDB上のマスタを参照するだけ。
    if not rankings_only:
        print("\n[資金フロー] グループ別の週次売買代金シェアを集計中...")
        try:
            from money_flow import compute as compute_money_flow
            n = compute_money_flow()
            _log("money_flow", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("money_flow", "failed", error=str(e))

    # 日次レポート【速報版】を先に保存（数値系セクションはこの時点で全部揃っている。
    # AI変動理由・当日市況考察・当日開示は後段完了後の再保存とイブニング便で追記される）
    if not rankings_only:
        print("\n[日次レポート] 速報版を保存...")
        try:
            from daily_report import save_report
            save_report()
            _log("daily_report_interim", "done", 1)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("daily_report_interim", "failed", error=str(e))

    # 適時開示の蓄積・好材料AI付加・市況考察
    # （TDnetは約1ヶ月で消えるため毎日蓄積が必須。時間のかかるイベント調査より先に実行し、
    #   タイムアウト等で途中終了しても当日の開示データは確保する。夜間分は20:30のイブニング便が回収）
    if not rankings_only:
        print("\n[適時開示] TDnet蓄積・好材料分析・市況考察...")
        try:
            from disclosures import run_daily as disclosures_run_daily
            result = disclosures_run_daily(with_ai=True)
            _log("disclosures", "done", result.get("stored", 0))
        except Exception as e:
            print(f"  エラー: {e}")
            _log("disclosures", "failed", error=str(e))

    # 決算・業績修正のタイムリー反映（当日開示の銘柄だけkabutan再取得→修正幅を算出・蓄積）
    if not rankings_only:
        print("\n[業績反映] 当日開示銘柄の業績・予想を更新中...")
        try:
            from earnings_refresh import refresh_from_disclosures
            result = refresh_from_disclosures()
            _log("earnings_refresh", "done", result.get("revisions", 0))
        except Exception as e:
            print(f"  エラー: {e}")
            _log("earnings_refresh", "failed", error=str(e))

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

    # EDINET有報の「事業の内容」増分更新（直近7日に提出された有報のみ・年1回自動更新される）
    if not rankings_only:
        print("\n[事業内容] EDINET有報の増分を確認中...")
        try:
            from edinet_business import run_incremental as edinet_biz_incremental
            n = edinet_biz_incremental()
            _log("edinet_business", "done", n)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("edinet_business", "failed", error=str(e))

    # 日次レポートを生成してDBに蓄積（全ステップの後 = 開示・市況考察・資金フローが揃った状態）
    if not rankings_only:
        print("\n[日次レポート] 生成・保存中...")
        try:
            from daily_report import save_report
            save_report()
            _log("daily_report", "done", 1)
        except Exception as e:
            print(f"  エラー: {e}")
            _log("daily_report", "failed", error=str(e))

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n{'='*50}")
    print(f"完了: {elapsed:.1f}秒")

    print_rankings("daily",  "change_pct")
    print_rankings("weekly", "change_pct")


if __name__ == "__main__":
    if "--evening" in sys.argv:
        run_evening()
    else:
        init          = "--init" in sys.argv
        rankings_only = "--rankings" in sys.argv
        force         = "--force" in sys.argv
        run(init=init, rankings_only=rankings_only, force=force)

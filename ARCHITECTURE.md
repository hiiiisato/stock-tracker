# ARCHITECTURE.md — システム全体マップ

AI・開発者がこのリポジトリを読み解くための地図。**変更時はこのファイルも更新すること。**

## 一言でいうと

日本株の分析Webアプリ。TiDB Cloud (MySQL互換) にデータを持ち、Render.com で
Flask アプリ + cron バッチが動く。データ取得は無料ソース（Yahoo Finance・kabutan・
J-Quants 無料枠・EDINET）のみで構成。

```
[Yahoo/kabutan/J-Quants/EDINET/ファンド各社PDF]
        │  (cronバッチが毎日/毎週/毎月取得)
        ▼
   TiDB Cloud (stock_tracker DB, 27テーブル)
        │
        ▼
   app.py (Flask・単一ファイル) ── Render.com Web Service
```

## エントリーポイント（render.yaml に対応）

| サービス | 起動コマンド | スケジュール | 役割 |
|---|---|---|---|
| kabushiki-tracker | `gunicorn app:app` | 常駐(Web) | 全ページ・API |
| daily-stock-update | `python daily_run.py` | 平日17:00 JST | 日次バッチ本体 |
| edinet-text-daily | `python edinet_texts.py --all` | 毎日23:45 JST | EDINET本文の取得(1日80件) |
| fund-watch-monthly | `python fund_watch.py` | 毎月5/15/25日 6:00 JST | ファンド月次レポート取込 |

GitHub Actions (`.github/workflows/daily.yml`) にも daily_run.py の予備実行がある。

## 日次バッチのパイプライン順序（daily_run.py）

依存関係があるので順序を崩さないこと:

1. 主要指数更新 (`market_indices`)
2. 銘柄マスタ・取引カレンダー (`master`)
3. 価格取得 Yahoo差分 (`prices_yahoo`) — adj_close含む
4. **分割対応** (`splits`) — J-Quants公式AdjFactor/AdjCが正。価格急変ヒューリスティックは誤判定するため廃止済み
5. 【月曜のみ】配当 (`dividends`)・財務 (`financials`)・ファンダ (`fundamentals`)・kabutan業績 (`financials_kabutan`)
6. テーマスコア (`theme_score`)
7. PER/PBR等の最新値再計算 (`fundamentals.recompute_price_metrics`)
8. テクニカル指標 (`compute_price_stats`) → `price_stats`
9. **理論株価** (`compute_theoretical`) — price_stats と stock_fundamentals に依存するため必ずその後
10. 週次スナップショット追記 (`compute_stats_history`) → `price_stats_history`（バックテスト用）
11. スイング候補スコア＆LINE通知 (`swing_scorer` → `swing_notifier`)
12. ランキング (`rankings`)・変動要因AI調査 (`event_researcher`)

## モジュール一覧（リポジトリルート＝すべて現役）

### 基盤
- `config.py` — DB接続・bulk_upsert・APIキー・理論株価の係数テーブル。**設定値は必ずここに置く**
- `render.yaml` — Render.com のサービス定義（本番のスケジューラ）

### データ取得
- `master.py` — 銘柄マスタ・取引カレンダー（J-Quants）
- `prices_yahoo.py` — 日次価格（Yahoo Finance chart API・並列・差分）
- `splits.py` — 株式分割・併合。J-Quants公式(AdjFactor/AdjC)を正とし、日次はYahoo splitsで暫定検知。`_verify_split_reflected()` が実際の価格変化を検証してから調整適用（二重調整防止）
- `split_backfill.py` — splits.py から `recompute_change_pct` が使われる（他は初期バックフィルの名残）
- `dividends.py` / `financials.py` — J-Quants 配当・財務
- `financials_kabutan.py` — kabutan スクレイピングで業績実績+会社予想（financialsに未来日付期=予想として入る）
- `fundamentals.py` — PER/PBR/時価総額等 → `stock_fundamentals`
- `market_indices.py` — 海外・国内指数 → `market_index_prices`
- `edinet_texts.py` — EDINET有報の定性テキスト → `edinet_text_blocks`
- `fund_watch.py` — ファンド月次レポートPDF取込＋Gemini構造化抽出 → `fund_master`/`fund_reports`。FUND_DEFSの `url_mode` (template/scrape/direct) と `page_range` で各運用会社の方式差を吸収

### 計算・分析
- `compute_price_stats.py` — テクニカル指標一式 → `price_stats`（現在値スナップショット）
- `compute_stats_history.py` — 週次スナップショット → `price_stats_history`（バックテスト用・PIT補正=期末+45日）
- `compute_theoretical.py` — はっしゃん式理論株価 → `theoretical_values`。係数は config.py
- `theme_score.py` — テーマ別過熱スコア → `theme_daily_stats`
- `rankings.py` — 日次/週次ランキング → `rankings`
- `swing_scorer.py` / `swing_notifier.py` — スイング候補スコアとLINE通知
- `event_researcher.py` — 急騰急落銘柄の要因をニュース検索+Geminiで要約 → `price_events`
- `research_strategy.py` — event_researcher の調査対象選定ロジック・閾値
- `theme_report.py` — テーマレポートHTML生成（app.py の /report から利用）
- `seed_themes.py` — テーママスタ・銘柄×テーマ関連の投入（テーマ追加時に再実行する保守スクリプト）

### Web (app.py — 単一ファイル約6200行)
セクション見出し（`# ═══` コメント）で区切られている。構成:
- インメモリキャッシュ `_get`/`_set`/`_bust_prefix`（TTL 1時間）
- `_BASE_CSS` / `_nav()` / `_page_html()` — 共通レイアウト（ダークテーマ）
- ページビルダー `_build_*_page()` 群 + ページ固有CSS/JS定数
- ルート定義はファイル末尾にまとまっている

| ルート | 内容 |
|---|---|
| `/` | ホーム（指数・注目銘柄） |
| `/stock/<code>` | 銘柄詳細（チャート・業績・理論株価・メモ） |
| `/screen` + `/api/screen` | スクリーニング（条件stateはDOM非依存のJSオブジェクト `_cmin/_cmax/_cflg`） |
| `/api/screen_asof` `/api/backtest` `/api/backtest_dates` | バックテスト（週次スナップショット、TOPIX ETF 1306がベンチマーク） |
| `/valuation` + `/api/theoretical/<code>` | 理論株価ランキング・個別シミュレーション |
| `/funds` | ファンドウォッチ（複数ファンド共通銘柄ハイライト） |
| `/rankings` `/events` `/theme/<id>` `/swing` `/watchlist` `/report/<date>` | 各分析ページ |
| `/api/chart_grid` `/api/search` `/health` | 補助API |

## DBテーブル（stock_tracker・27テーブル）

| 分類 | テーブル | 内容・更新元 |
|---|---|---|
| マスタ | `stocks` `markets` `sectors` `trading_calendar` | master.py |
| 価格 | `daily_prices` | prices_yahoo.py（adj_close=分割調整済。splits.pyが再計算） |
| 価格 | `stock_splits` | splits.py（分割イベント。J-Quants公式が正） |
| 価格 | `market_index_prices` | market_indices.py |
| 財務 | `financials` | financials.py + financials_kabutan.py（**未来日付の期=会社予想**） |
| 財務 | `financials_forecast` `dividends` `stock_fundamentals` | 各取得モジュール |
| 指標 | `price_stats` | compute_price_stats.py（最新値のみ） |
| 指標 | `price_stats_history` | compute_stats_history.py（週次・バックテスト用） |
| 指標 | `stock_metrics_history` | 旧方式（archive/compute_metrics_history.py）。現役コードから参照なし・更新停止 |
| 分析 | `theoretical_values` | compute_theoretical.py |
| 分析 | `rankings` `swing_scores` `price_events` | 各計算モジュール |
| テーマ | `theme_categories` `stock_themes` `theme_daily_stats` | seed_themes.py / theme_score.py |
| テキスト | `edinet_text_blocks` | edinet_texts.py |
| ファンド | `fund_master` `fund_reports` | fund_watch.py |
| アプリ | `watchlist` `stock_memos` `fetch_logs` | app.py / daily_run.py |

## 落とし穴（過去に踏んだもの）

- **daily_prices.close は生値とは限らない**。Yahooのcloseは銘柄により生値/調整済みが混在。
  分割処理は必ず「実際の価格変化と期待比率の照合」(`splits._verify_split_reflected`)を通す
- **financialsに未来日付の期が入っている**（会社予想）。実績として使う時は `period_end <= CURDATE()` で除外
- **バックテストの先読み防止**: ファンダは期末+45日を公開日とみなす（PIT補正）。price_stats_history生成時に適用済み
- **フォーム値をJSの状態として使わない**: bfcacheが復元して壊れる。純粋JSオブジェクトで管理
- J-Quants無料枠はレート制限が厳しい。大量アクセス後は`Rate limit exceeded`が数時間続く
- `archive/` は本番不使用の旧コード。詳細は `archive/README.md`

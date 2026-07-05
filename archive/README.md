# archive/ — 役目を終えたスクリプト置き場

**このフォルダのコードは本番では使われていない。** 参照用に残しているだけで、
`daily_run.py`・`app.py`・`render.yaml` のどこからも import / 実行されない。

再実行したい場合はリポジトリルートから `PYTHONPATH=. python archive/xxx.py` とする
（`from config import ...` の解決のため）。

| ファイル | 元の役割 | 現在の代替 |
|---|---|---|
| `prices.py` | J-Quants日次価格取得 | `prices_yahoo.py`（J-Quants無料枠は12週のみのため移行） |
| `backfill_yahoo.py` | Yahoo過去2年分の一括バックフィル | 完了済みの一回きり作業 |
| `fetch_historical_prices.py` | J-Quants過去分の一括取得 | 同上 |
| `compute_metrics_history.py` | 決算期末ごとのPER/PBR履歴 | `compute_stats_history.py`（週次スナップショット）に統合 |
| `edinet_biz.py` | 「事業の内容」一括取得 | 完了済みの一回きり作業 |
| `edinet_summarizer.py` | EDINETテキストのGemini要約（実験） | 未採用（summary列はアプリ未使用） |
| `init_db.py` | 初回スキーマ作成 | 各モジュールの `ensure_table(s)` で管理 |
| `chart.py` | ローカルでチャートHTML生成 | Webアプリ `/stock/<code>` のチャート |
| `theme_chart.py` | テーマ過熱チャートHTML生成 | Webアプリ `/theme/<id>` ページ |
| `investigate.py` | 変動要因調査の試作 | `event_researcher.py` |
| `debug_html/` | 上記ツールが生成したデバッグ出力（gitignore対象） | 削除して問題ない |

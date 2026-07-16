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

## エントリーポイント

**Webサイト**: Render.com（`render.yaml`）の `kabushiki-tracker`（`gunicorn app:app`・常駐）のみ。

**定期バッチはすべて GitHub Actions**（Render無料プランはcron非対応のため）:

| ワークフロー | スケジュール(JST) | コマンド | 役割 |
|---|---|---|---|
| daily.yml | 平日16:00 メイン | `python daily_run.py` | 日次バッチ本体（価格→指標→ランキング→開示→AI調査→レポート保存） |
| daily.yml | 平日17:00 リトライ | 同上 | GHA cron遅延・失敗への保険。daily_run側の重複ガードで完了済みならスキップ |
| daily.yml | 平日20:30 イブニング便 | `python daily_run.py --evening` | 夜間の適時開示回収→市況考察→日次レポート確定版を上書き保存→**確定版をLINE通知**→AIファンド意思決定 |
| misc_batch.yml | ~~毎日23:45~~ **一時停止中** | `python edinet_texts.py --all` | EDINET本文ドリップ（`if:false`。財務キャッチアップ中は枠譲渡） |
| misc_batch.yml | ~~毎日23:45~~ **一時停止中** | `python edinet_segments.py --all` | 事業セグメント時系列（`if:false`。同上） |
| misc_batch.yml | 毎日**00:30 JST** | `python financials_edinet.py` | 過去業績(op等)欠損の穴埋め＋有報年次/四半期データ蓄積（EDINET）。**枠リセット(00:00 JST)直後**に走らせフレッシュな100/日を使い切る。自動停止 |
| misc_batch.yml | 5/15/25日 6:00 | `python fund_watch.py` | ファンド月次レポート取込 |

**GitHub Actions cron の注意**: 発火は数十分〜数時間遅延することがある（実測で2時間超）。
このため「メイン+リトライ」の2本立て+`timeout-minutes: 60`+イブニング便で確定、という設計にしている。
daily_run.py には (a)重複実行ガード（当日daily_report完了済みならスキップ）と
(b)休場日ガード（当日価格が0件なら後段をスキップ）が入っている。

## 日次バッチのパイプライン順序（daily_run.py）

依存関係があるので順序を崩さないこと:

1. 主要指数更新 (`market_indices`)
2. 銘柄マスタ・取引カレンダー (`master`)
3. 価格取得 Yahoo差分 (`prices_yahoo`) — adj_close含む
4. **分割対応** (`splits`) — J-Quants公式AdjFactor/AdjCが正。価格急変ヒューリスティックは誤判定するため廃止済み
5. 【月曜のみ】配当 (`dividends`)・財務 (`financials`)・ファンダ (`fundamentals`)・TDnet短信の広域取込 (`financials_tdnet` 過去30日)
6. テーマスコア (`theme_score`)
7. PER/PBR等の最新値再計算 (`fundamentals.recompute_price_metrics`)
8. テクニカル指標 (`compute_price_stats`) → `price_stats`
9. **理論株価** (`compute_theoretical`) — price_stats と stock_fundamentals に依存するため必ずその後
10. 週次スナップショット追記 (`compute_stats_history`) → `price_stats_history`（バックテスト用）
11. スイング候補スコア → `swing_scores` テーブル保存 (`swing_scorer`。/swing ページで表示)
12. ランキング (`rankings`)・資金フロー週次集計 (`money_flow`)
13. 適時開示 (`disclosures`) — 重いAI調査より先に実行（timeout時も当日開示を確保）
14. 変動要因AI調査 (`event_researcher`)
15. EDINET有報の事業内容 増分 (`edinet_business`)
16. 日次レポート生成・保存 (`daily_report.save_report`) — 全データが揃った最後
※ 20:30のイブニング便（`daily_run.py --evening`）が 13 と 16 を再実行し夜間開示を反映した確定版にする

## モジュール一覧（リポジトリルート＝すべて現役）

### 基盤
- `config.py` — DB接続・bulk_upsert・APIキー・理論株価の係数テーブル。**設定値は必ずここに置く**
  - `with db() as cur:` — 例外時も必ずrollback+closeする接続。**app.py（常駐プロセス）の新規コードはこちらを使う**。`get_conn()`は自前でclose管理する既存コード・バッチ用
- `render.yaml` — Render.com のサービス定義（本番のスケジューラ）

### データ取得
- `master.py` — 銘柄マスタ・取引カレンダー（J-Quants）
- `prices_yahoo.py` — 日次価格（Yahoo Finance chart API・並列・差分）
- `splits.py` — 株式分割・併合。CRSP等の業界標準（生値＋調整係数分離・イベントは公式コーポレートアクション由来）に倣った多層防御:
  1. **J-Quants公式**(AdjFactor/AdjC)が正（12週遅延）。AdjC採用日には係数を重ねない
  2. 直近窓はYahoo splitsで暫定検知 → **TDnet適時開示（disclosures・事前公表）で裏取り**。
     裏取りあり=通常帯(0.7-1.4)/なし=厳格帯(0.85-1.18)で `_verify_split_reflected()` の段差検証
  3. 係数の**適用時にも毎回検証**（登録済みイベントでもcloseに段差が無ければ適用しない＝最後の防波堤）
  4. `run_integrity_check()` — 「closeは正常なのにadjだけ跳ねる」箇所を毎晩スキャン→自動修復（daily_runから呼出）
- `split_backfill.py` — splits.py から `recompute_change_pct` が使われる（他は初期バックフィルの名残）
- `dividends.py` — J-Quants 配当
- `financials.py` — Yahoo Finance の損益/BS（crumb方式）。**穴埋め専用フォールバック**:
  権威データは TDnet(financials_tdnet)。Yahooは日本株の売上/粗利/営業益を欠損時に "0" で返すため
  （例: 9221は営業益・粗利が常時0）、`_zero_to_none` で 0→None にし、bulk_upsert の `fill_only_cols` で
  損益・BS列は既存値を上書きしない（COALESCE。既にTDnet/公式値があればYahooの0/欠損で壊さない）。
  total_debt だけは TDnet が持たないため Yahoo が通常更新する
- `financials_tdnet.py` — TDnet決算短信XBRL(サマリー)から実績+会社予想を取得（financialsに未来日付期=予想として入る）。
  kabutanがデータセンターIP遮断のため公式一次データに置換(2026-07)。`import_recent(days)` で直近短信を取込。
  earnings_refresh から呼ばれ、detect_revisions が上方/下方修正を検知。旧 `financials_kabutan.py` は archive/ へ
- `financials_edinet.py` — **EDINET公式有報XBRL（edinetdb.jp経由）を1コールで2役**:
  **(1) 過去業績の欠損を穴埋め**: Yahoo/kabutanで埋まらなかった `operating_income` 等を公式有報の精密値で補完。
  **fill-only**（`bulk_upsert` の `fill_only_cols` で全列COALESCE＝既存TDnet値は上書きせずNULLだけ埋める）。
  EDINETの円単位生値を百万円に丸めてTDnet由来データと桁を揃える。`fiscal_year` を既存NULL行の
  `YEAR(period_end)` に対応させ既存行のみ更新（新しい期は作らない）。`normalize_zero_artifacts` で
  Yahoo由来の `op=0` 疑似欠損(rev>0)をNULLに戻して補完対象化。
  **(2) 有報の豊富な年次データを蓄積**: 同じレスポンスで返る損益内訳・CF・capex・R&D・従業員・給与・
  ガバナンス・持合い・TSR等（約60項目）を `financials_edinet_annual`（生値精密・EDINET権威データで全更新）へ。
  **年次・四半期の両対応**（`period=annual`/`quarterly_standalone`）。四半期は period_end 列が無いため
  `(会計年度, 四半期, 決算月)` から四半期末日を導出（`_q_end`）して既存Q行に対応。
  取得タスクの優先度: **①年次op欠損 →②四半期op欠損 →③未取得銘柄の付随データ**。
  edinetdb.jp無料枠100件/日の残数ヘッダで自動停止 → 期種別メタ（`financials_edinet_meta`/`_qmeta`）に取得済記録（90日周期で再取得）。
  **枠集中のため edinet_texts/segments は一時停止中**（misc_batch.yml の `if: false`。キャッチアップ後に再開）
- `earnings_calendar_jpx.py` — JPX公式「決算発表予定日」Excelを取込 → `earnings_schedule`（決算跨ぎ管理）
- `fundamentals.py` — PER/PBR/時価総額等 → `stock_fundamentals`
- `market_indices.py` — 海外・国内指数 → `market_index_prices`
- `edinet_texts.py` — EDINET有報の定性テキスト全17セクション → `edinet_text_blocks`（edinetdb.jp経由・10件/日制限）
  ＋取得試行 `edinet_text_meta`（edinet_code を解決できない銘柄=新形式コード/非EDINET等は`no_edinet_code`記録で30日間再スキャン防止。毎回755件をフルスキャンして28分浪費するのを回避）。
- `edinet_segments.py` — 事業セグメント別の売上・利益・構成比の時系列（約7年分）→ `company_segments`
  ＋取得状態 `company_segments_meta`（単一セグメント企業は404→`no_data`記録で再取得ループ防止）。
  80件/日ドリップ（未取得は時価総額の大きい順→90日周期で巡回更新）。edinetdb.jp無料枠100コール/日をtextsと分け合う。
  銘柄ページ「会社概要」内にセグメント構成バーとして表示（app.py `_build_stock_page`）
- `edinet_business.py` — **EDINET公式API直接**で有報「事業の内容」→ `stocks.business_description`（詳細な事業内容）。
  日次は増分（直近7日の提出分のみ→各社年1回自動更新）。初回は `--backfill` で過去380日を一括。要 `EDINET_API_KEY`（無料）
- `company_profile.py` — kabutan銘柄トップページから会社概要（簡単な事業内容）・kabutanテーマタグ・会社サイト
  → `stocks.business_summary`/`stocks.website`/`kabutan_themes`。日次150件で未取得優先→古い順（約1ヶ月で全銘柄一巡）
- `fund_watch.py` — ファンド月次レポートPDF取込＋Gemini構造化抽出 → `fund_master`/`fund_reports`。FUND_DEFSの `url_mode` (template/scrape/direct) と `page_range` で各運用会社の方式差を吸収
- `disclosures.py` — TDnet適時開示の蓄積・分析 → `disclosures`/`market_summary`。タイトルからカテゴリ・ポジネガをルール分類（APIコストゼロ）。好材料（上方修正・増配等）はPDF本文をGeminiで読み修正理由＋関連テーマを抽出、テーマ経由で関連銘柄をサジェスト。業種・テーマ・開示動向から日次市況コメントも生成。**TDnetは約1ヶ月で消えるため毎日蓄積が必須**

### 計算・分析
- `compute_price_stats.py` — テクニカル指標一式＋財務指標 → `price_stats`（現在値スナップショット）。
  財務は `_load_financials`（年次実績のみ=period_end<=今日で会社予想を除外）。
  `fscore` = 当サイト版 Piotroski F-score（`_fscore_7`・7点。標準9項目のうち流動比率/希薄化はデータ未保持で除外し、
  総負債→自己資本比率改善・粗利率→営業利益率改善で代替。クオリティ・ファクター。詳細は docs/capital_gains_strategy_survey.md）
- `compute_stats_history.py` — 週次スナップショット → `price_stats_history`（バックテスト用・PIT補正=期末+45日）。
  fscore も PIT（`_pit_fm`）で再現・全履歴バックフィル済み
- `compute_theoretical.py` — はっしゃん式理論株価 → `theoretical_values`。係数は config.py
- `theme_score.py` — テーマ別過熱スコア → `theme_daily_stats`
- `rankings.py` — 日次/週次ランキング → `rankings`
- `line_notify.py` — LINE Messaging API への汎用テキストpush（`push_text`/`is_configured`）。
  日次レポート完成通知のトランスポート。環境変数 `LINE_CHANNEL_ACCESS_TOKEN`/`LINE_USER_ID`、
  未設定なら送信スキップ（例外を出さない）。LINE初回設定手順は line_notify.py の docstring
- `swing_scorer.py` — スイング候補スコア → `swing_scores`（/swing ページで表示）
  ※旧 `swing_notifier.py`（スイングのLINE通知）はテスト用途のみで廃止し archive/ へ移動（2026-07）
- `event_researcher.py` — 急騰急落銘柄の要因をニュース検索+Geminiで要約 → `price_events`。
  Phase1.5で `event_classifier` が理由を機械分類（`reason_category`）してからGeminiが要約＋残りの分類を補完
- `event_classifier.py` — 株価変動の「理由」を機械分類 → `price_events.reason_category`。
  自由文だけでは集計/持続日数分析/戦略検証ができないため14カテゴリのタクソノミーに分類。
  一次データ優先: ①開示由来(disclosures/forecast_revisions)で決算好感/失望・上方/下方修正・自社株買い・
  TOB・提携等を機械確定(信頼度high) ②テーマ物色/地合い連動/継続を theme_daily_stats/TOPIX/直近イベントで判定(med)
  ③残りはGeminiが構造化出力(【分類】)で補完。REASON_CATEGORIES が表示バッジ・集計の単一の真実
- `research_strategy.py` — event_researcher の調査対象選定・閾値＋ニュース取得(TDnet/kabutan/Google)＋Gemini要約
- `theme_report.py` — テーマレポートHTML生成（app.py の /report から利用）
- `daily_report.py` — 日次相場レポートHTML生成（app.py の /daily から利用）。結論→数字→資金フロー変化→
  騰落TOP5+理由+スパークライン→トリガー銘柄(基準は TRIGGER_DEFS)→好材料開示→ウォッチリスト。
  自己完結HTML（外部JS/CSSなし）なのでメール・LINE転送にも流用可能。
  `notify_report_ready(date)` = 確定版のLINE通知（リンクのみの最小通知。全文URLは `REPORT_BASE_URL` 環境変数、
  既定は `DEFAULT_REPORT_BASE_URL`）。イブニング便の `save_report()` 直後に呼ばれる。
  手動テスト: `python daily_report.py --save --notify` / 保存済みを再通知 `python daily_report.py --notify`
- `money_flow.py` — 資金フロー週次集計 → `money_flow_weekly`。テーマ(kabutan_themes)/業種/時価総額帯/スタイル別に
  週間売買代金シェアの対13週平均比（flow_ratio）・**Zスコア**（母数非依存の流入強度＝今週シェアが過去13週の
  変動幅の何σ分か。小グループの偶然のブレを排除）・騰落率中央値・上昇銘柄比率・対TOPIXを算出。
  **flow_class**で分類: inflow=買い優勢の流入 / dump=大商い×下落の投げ売り / outflow=流出 / neutral。
  毎回直近26週を再計算してupsert（自己修復）。週キーはISO週の金曜日。
  ※売買代金だけでは「買われた」と「売られた」を区別できない → 表示(/flows・日次レポート)は
    「買い優勢の流入」と「投げ売り警戒」を分けて出す。テーマは規模足切り(100億+・8銘柄+)でノイズ除去
- `seed_themes.py` — テーママスタ・銘柄×テーマ関連の投入（テーマ追加時に再実行する保守スクリプト）
- `ai_fund.py` — AIファンドマネージャー（模擬運用・/aifundタブ）。元本1000万・常時8銘柄・100株単位・
  コスト0.1%/片道。**先読み防止が最重要**: イブニング便で意思決定(`decide`)→翌営業日の寄付で約定
  (`execute_orders`は決定日 >= 最新取引日の注文を約定させない)。ハイブリッド判断=定量6観点
  （モメンタム/押し目/ブレイク/割安成長/業績イベント/資金流入テーマ内の出遅れ=先回り材料）で候補約30銘柄
  →Geminiが売買と理由・シナリオを決定→ガードレール（8銘柄維持・予算60万〜250万・入替3/日・
  再購入7日禁止・-20%強制ロスカット・**先回り(予測)スタイルは最大2銘柄**・AI出力不足時は定量補完）。
  さらに毎晩: **投資基準を明文化して`ai_fund_policy`に日次蓄積**（前回基準+成績フィードバック+
  資金流入テーマを見て更新→次回プロンプトに注入する学習ループ）、**控え8銘柄を`ai_fund_bench`で別管理**
  （選定日終値からの騰落を計測・候補プールに合流して昇格可能）。NAVは終値評価で日次記録（ベンチ=1306）

### Web (app.py — 単一ファイル約6200行)
セクション見出し（`# ═══` コメント）で区切られている。構成:
- インメモリキャッシュ `_get`/`_set`/`_bust_prefix`（TTL 1時間）
- `_BASE_CSS` / `_nav()` / `_page_html()` — 共通レイアウト（ダークテーマ）
- ページビルダー `_build_*_page()` 群 + ページ固有CSS/JS定数
- ルート定義はファイル末尾にまとまっている
- `/earnings` 決算プレビュー: 今後14日の発表予定（JPX）×進捗率の上振れ候補ピックアップ×想定インパクト
  （過去の決算級イベント日=|前日比|4%超×出来高3倍の反応統計によるシミュレーション。`_earnings_impact_stats`）。
  銘柄ページにも同素材の「決算プレビュー」カード（次回発表日・進捗率・想定インパクト）を表示。
  進捗率は ai_fund `_progress_batch` を共用。日付比較はJST(Python)側で行う（DBのCURDATE()はUTC）

| ルート | 内容 |
|---|---|
| `/` | ホーム（指数・注目銘柄） |
| `/stock/<code>` | 銘柄詳細（チャート・業績・理論株価・メモ） |
| `/screen` + `/api/screen` | スクリーニング（条件stateはDOM非依存のJSオブジェクト `_cmin/_cmax/_cflg`） |
| `/api/period_stats` | 任意期間（days=N or from=日付）の騰落率・高安レンジ幅。スクリーニングの期間騰落条件📅で使用。バックテストでは期間条件は無視される |
| `/api/screen_asof` `/api/backtest` `/api/backtest_dates` | バックテスト（週次スナップショット、TOPIX ETF 1306がベンチマーク） |
| `/valuation` + `/api/theoretical/<code>` | 理論株価ランキング・個別シミュレーション |
| `/disclosures` | 適時開示（市況考察・好材料ハイライト・関連銘柄サジェスト・全開示一覧） |
| `/flows` | 資金フロー（テーマ/業種/規模/スタイル別の週次資金流入度ランキング・週切替可） |
| `/daily` `/daily/<date>` | 日次相場レポート（daily_report.py が生成する自己完結HTML・逆ピラミッド構成） |
| `/funds` | ファンドウォッチ（複数ファンド共通銘柄ハイライト） |
| `/aifund` | AIファンド（模擬運用: 保有8銘柄・次の売買予定と理由・NAV vs TOPIX・売買履歴） |
| `/rankings` `/events` `/theme/<id>` `/swing` `/watchlist` `/report/<date>` | 各分析ページ |
| `/api/chart_grid` `/api/search` `/health` | 補助API |

## DBテーブル（stock_tracker・27テーブル）

| 分類 | テーブル | 内容・更新元 |
|---|---|---|
| マスタ | `stocks` `markets` `sectors` `trading_calendar` | master.py |
| 価格 | `daily_prices` | prices_yahoo.py（adj_close=分割調整済。splits.pyが再計算） |
| 価格 | `stock_splits` | splits.py（分割イベント。J-Quants公式が正） |
| 価格 | `market_index_prices` | market_indices.py |
| 財務 | `financials` | financials.py + financials_tdnet.py（TDnet短信XBRL・**未来日付の期=会社予想**）+ financials_edinet.py（有報で欠損穴埋め） |
| 財務 | `financials_edinet_annual` `financials_edinet_quarterly` | financials_edinet.py（有報の豊富な年次/四半期データ約60項目・生値精密） |
| 財務 | `financials_edinet_meta` `financials_edinet_qmeta` | financials_edinet.py（年次/四半期の取得済メタ・90日周期で再取得） |
| 財務 | `financials_forecast` `dividends` `stock_fundamentals` | 各取得モジュール |
| 指標 | `price_stats` | compute_price_stats.py（最新値のみ） |
| 指標 | `price_stats_history` | compute_stats_history.py（週次・バックテスト用） |
| 指標 | `stock_metrics_history` | 旧方式（archive/compute_metrics_history.py）。現役コードから参照なし・更新停止 |
| 分析 | `theoretical_values` | compute_theoretical.py |
| 分析 | `rankings` `swing_scores` `price_events` | 各計算モジュール |
| テーマ | `theme_categories` `stock_themes` `theme_daily_stats` | seed_themes.py / theme_score.py（自前キュレーション・テーマ分析ハブ用） |
| テーマ | `kabutan_themes` | company_profile.py（kabutan付与タグ・1500超テーマ。資金フロー分析用） |
| 分析 | `money_flow_weekly` | money_flow.py（グループ別の週次資金フロー） |
| テキスト | `edinet_text_blocks` `edinet_text_meta` | edinet_texts.py（metaは解決失敗銘柄のnegative cache・再スキャン防止） |
| 会社情報 | `company_segments` `company_segments_meta` | edinet_segments.py（セグメント別売上・利益の時系列） |
| 開示 | `disclosures` `market_summary` | disclosures.py（TDnet全開示の蓄積＋日次市況考察） |
| 会社情報 | `stocks.business_summary/website`（カラム） | company_profile.py（簡単な事業内容=kabutan概要） |
| 会社情報 | `stocks.business_description`（カラム） | edinet_business.py / edinet_texts.py（詳細=有報「事業の内容」） |
| ファンド | `fund_master` `fund_reports` | fund_watch.py |
| アプリ | `watchlist` `stock_memos` `fetch_logs` | app.py / daily_run.py |
| AIファンド | `ai_fund_state` `ai_fund_positions` `ai_fund_orders` `ai_fund_trades` `ai_fund_nav` `ai_fund_policy` `ai_fund_bench` | ai_fund.py（模擬運用・全売買に理由を記録・投資基準と控え銘柄を日次蓄積） |
| 決算予定 | `earnings_schedule` | earnings_calendar_jpx.py（JPX公式の決算発表予定日Excel・日次更新）。ai_fund.py `_earnings_dates` が読み取り。kabutan/ J-Quants無料枠(12週遅延)は使用不可のため公式JPXに置換 |

**トレード戦略の実証研究**: `docs/trade_strategy_research.md`（2024-2026全銘柄・全シグナル機械検証。
第1弾: 52週高値ブレイク×2段階エグジットが主力、深押し逆張りは期待値マイナスで禁止等。
第2弾(2026-07): 相対強度リーダー=平均+12%/勝率55%を主力追加、テーマ主役>出遅れ、RSI>=85新規買い禁止、
利益保全トレール-15%化、TOPIX200日線割れでモメンタム系ビューを自動停止（レジームゲート）。
第3弾(2026-07): E:好業績ドリフト=発表後初動+7〜15%×高値圏で平均+11.8%/勝率57%（プロキシ検証）を追加、
投資スタイル分類 STRATEGIES(A/R/E/T/C/V/L) を新設し買い注文ごとに strategy 列へ永続化・/aifund に常時表示、
保有期間を最長9ヶ月に延長。
結論は ai_fund.py の PLAYBOOK 定数として毎晩の意思決定に注入され、EDGE_VIEWS が週次で前提を再検証する）

## 落とし穴（過去に踏んだもの）

- **daily_prices.close は生値とは限らない**。Yahooのcloseは銘柄により生値/調整済みが混在。
  分割処理は必ず「実際の価格変化と期待比率の照合」(`splits._verify_split_reflected`)を通す。
  **検証はどの経路でも省略禁止**（2026-07: J-Quants経路と日次検知が無検証で係数を掛け、
  フジクラ5803等178イベント・約130銘柄のadj_closeが二重調整で汚染された）。教訓3点:
  (1) Yahooは偽の分割イベントを返す（3/30に51件の偽クラスタ等）
  (2) 検証の許容帯は狭く。旧0.5〜2.0倍では「1:2分割が未反映(rel≈2.0)」が素通りした（8031で実害）
  (3) J-QuantsのAdjCは「価格データ窓より未来の除権日」まで反映済み。AdjCの上に係数を重ねない
- **Yahooのadjcloseは配当・分配金まで調整する**ため採用しない（方針は分割のみ調整）。
  prices_yahoo は adj_close=close で書き、調整は splits.run_daily が一元管理
  （大型分配のインフラファンド9282でadjがズレた実害から修正）
- **financialsに未来日付の期が入っている**（会社予想）。実績として使う時は `period_end <= CURDATE()` で除外
- **バックテストの先読み防止**: ファンダは期末+45日を公開日とみなす（PIT補正）。price_stats_history生成時に適用済み
- **業績修正・増配のバックテストは必ず `forecast_revisions.reaction_date` を基準にする**。
  発表時刻(disclosed_at)から場中/引け後(session)を判定済みで、reaction_date=「実際に売買できる最初の営業日」。
  場中発表は当日株価に既に織り込まれ、引け後発表は翌営業日に反応する。announced_at（発表日）で
  エントリーを測ると引け後発表分を「発表日に買えた」と誤計上する先読みバイアスになる。
  reaction_dateの始値エントリーで測れば場中/引け後を問わず先読みしない
- **フォーム値をJSの状態として使わない**: bfcacheが復元して壊れる。純粋JSオブジェクトで管理
- J-Quants無料枠はレート制限が厳しい。大量アクセス後は`Rate limit exceeded`が数時間続く
- **kabutan の `/stock/info?code=` は廃止(404)**。会社概要はトップページ `/stock/?code=` の
  `.company_block` から取得する（company_profile.py）。ライブ取得はせずDB保存値を表示する
- **GitHub ActionsランナーIP→kabutanはHTTP 405で一律遮断される**（2026-07-09実測。RenderのAWS IPは通る）。
  kabutan取得は必ず `kabutan_client.get()` を使うこと（直接→遮断検知でRenderの
  `/internal/kabutan` プロキシへ自動切替。認証はTIDB_PASSWORDのSHA256先頭32桁）。
  requests直叩きで新規コードを書くとGHA上で沈黙失敗する
- **edinetdb.jp 無料枠は300件/月（実質10件/日）**で全銘柄カバー不能。事業内容の一括取得は
  EDINET公式API（edinet_business.py・無料キー）を使う。名証など東証外単独上場はマスタ対象外
- **Gemini無料枠はモデルごとに独立したRPD枠で、gemini-2.5-flashは20回/日しかない**。
  用途でモデルを分離している: イベント調査=gemini-2.5-flash / 適時開示・市況=gemini-2.5-flash-lite。
  新しいAI機能を足す時は既存の枠を食い潰さないようモデル配分を確認すること
- `archive/` は本番不使用の旧コード。詳細は `archive/README.md`

### 株価データ品質の知見（2026-07-06のETF/REITスパイク大規模修復で判明）

- **J-Quantsの `AdjC` は取得時点の全分割を織り込んだ累積調整済み値**。データ窓（無料枠は
  約12週遅延・過去2年）の外で起きた分割も反映される。ただし窓外の分割は `AdjFactor` 行と
  しては現れないため、`AdjC/C` 比（=その日以降の全分割の累積乗数）から導出する
- **Yahoo chart APIの `close` は銘柄により生値/分割調整済みが混在**し、しかも取得時点の
  基準で歴史が書き換わる。DBに過去に書き込んだcloseと今取得するcloseは基準が違うことがある。
  基準判定は「C≠AdjCの判別日」だけで比較すること（C=AdjCの日は判別力ゼロ）
- **幽霊行**: J-Quantsにバーが無い日（=公式には取引なし）でもYahooはstale値の行を返すことが
  あり、これがDBに入ると比率系指標が全滅する。窓内でJQに無い日の行は削除してよい
- **薄商いのマイクロETF**（326A等の業界改革厳選シリーズ、486A/487A等）はYahooデータの品質が
  根本的に低く（停止期間のstale値・分割の反映漏れ）、無料ソースでは完全な修復が不可能。
  スクリーニングでは時価総額・流動性フィルタで自然に除外される前提で許容する
- **本物の急変動の例**: 2553（中国A株ETF）の2024年10月の数倍化→暴落（国慶節休場中の
  東証プレミアム）、7691の1円株化。スパイク≠必ずデータ異常。公式データと突合してから直すこと

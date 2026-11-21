# 分割・併合補正の全体実装計画（2026-11-21）

## 目的と決定事項

株価、EPS、BPS、DPS、PER、PBR、配当利回りを、現在画面では可能な限り同じ株数基準で扱う。
分割・併合後は、過去の一株指標も現在の一株単位へ直し、分割による見かけの段差を業績変化として
表示・採点しない。

次を確定事項とする。

1. `corporate_actions` / `corporate_action_history` を唯一の公式権利台帳とする。新しい分割台帳は作らない。
2. 分割係数の計算実装は `price_adjustments.py` の1か所だけに置く。財務・配当・画面に独自式を書かない。
3. 原本値は変更しない。補正値は原本と権利台帳から再生成し、補正値へ再び係数を掛けない。
4. 現在画面の株価と一株指標は、公開価格が使用した `target_basis_date` と `action_digest` にそろえる。
5. 出典がすでに遡及修正した一株値には係数を掛けない。基準を決められない値は
   `needs_review` とし、画面の正式値、理論株価、採点へ渡さない。
6. 売上高、純利益、自己資本、総資産、ROEなど株数単位ではない値は分割補正しない。
7. ライツイシュー、株式交換、合併、持株会社移行は単純な分割係数で自動処理しない。
8. 一株指標の株数基準は年度行全体ではなく、EPS・BPS・DPSの事実ごとに判定する。同じ開示・年度でも
   指標ごとに遡及修正の有無が異なり得るため、一つの `source_basis_status` を共有しない。
9. 権利台帳の履歴範囲は価格の公開期間だけで決めない。EPS/BPS/DPSを表示する最古年度まで公式権利を
   遡って整備し、価格が2024年以降しかなくても2015年等の分割を一株指標の正規化に使用する。

## 現状監査

### 既に正しい部分

- `price_pipeline.py` が価格公開の唯一の経路になっている。
- `daily_prices` は当時値と補正値を別列で持ち、`price_history` は補正済みOHLCVだけを公開する。
- `price_adjustment_state.action_digest` と `published_version` により、古い台帳で作った価格を判別できる。
- 価格補正確認中の銘柄は `price_history` から隠れ、ウォッチリストのチャートAPIは503を返す。
- チャートキャッシュは価格公開世代と価格版で失効する。
- `ai_fund_adjustments.py` は同じ `Action` を使用し、適用履歴で二重処理を防いでいる。
- `stock_splits` と `splits.py` は互換用であり、別の価格補正ロジックではない。

### 実装時に直す部分

- `forecast_metrics` は現在、実績期後の権利をフラグにするだけで補正していない。
- 銘柄詳細画面は年度ごとのEPS/BPSを「各年の純利益・自己資本÷現在株数」で再計算している。
  これは分割には追随するが、増資・自己株式・期中加重平均株式数を無視するため正式値にはできない。
- 銘柄詳細、業績グリッド、`stock_fundamentals`、理論株価で一株指標の取得経路が分かれており、
  同じ銘柄に異なるEPSが表示され得る。
- `fundamentals.recompute_price_metrics` は補正済み株価と、株数基準未確認のEPS/BPS/DPSを結合し得る。
- 配当チャートと連続増配判定は、配当イベントを同一株数基準へ直す前の値を使用している。
- SBI約定履歴からの実現損益は、分割前購入・分割後売却を再現できない。
- 20:30のイブニング便はTDnet開示を再取得するが、その後に権利台帳を再同期していない。
- 現行 `corporate_actions` の主キーは `(code, ex_date)` であり、同じ日に別の権利がある場合を保持できない。
- `daily_prices.adj_factor` は `DECIMAL(12,6)` のため、繰返し小数の係数を最大約5e-7丸める。
- J-Quants価格対象外のTOKYO PRO MARKETが通常の公式照合失敗・修復キューへ入り、通常市場の修復を遅らせる。

## 株数基準の契約

### 現在表示

現在画面の正本は「公開価格と同じ現在株数基準」とする。

- 価格: `price_history.open/high/low/close/volume`
- 実績EPS/BPS: 同じ `action_digest` へ正規化した値
- 予想EPS: 同じ株数基準へ直した平均株式数で会社予想純利益を割った値
- 実績・予想DPS: 配当通知ごとに同じ株数基準へ直した後の年度値
- PER/PBR/配当利回り/理論株価: 上記の価格と一株指標だけから計算した値

`price_adjustment_state` にNULL許容の `target_basis_date` を追加する。`price_date` は最新取引日、
`target_basis_date` はどの日までの権利を織り込んだ株数単位かであり、意味を分ける。

### バックテスト

バックテストでは `target_basis_date=as_of` とし、`announced_at <= as_of` かつ
権利実施日が `as_of` 以前の公式イベントだけを使う。現在の補正結果を過去へ流用しない。
そのため `corporate_actions` へ公式発表日 `announced_at` を追加する。

### 共通係数

分割前の基準日から対象基準日までの確定済み分割・併合について、次を一つの純粋関数で計算する。

```text
share_factor    = Π(new_shares / old_shares)
per_share_factor = 1 / share_factor
```

1株を2株に分割した場合、株数と出来高は2倍、株価・EPS・BPS・DPSは1/2とする。
価格は `ex_date`、財務は確認済みの `source_basis_date`、配当は通知の基準日と公式仕様を境界にする。
異なる境界規則を呼出側で再実装せず、共通関数へ用途を渡して判定する。

`action_digest` は価格だけでなく一株指標・配当・バックテストの無効化にも使うため、`action_id`、
`announced_at`、`ex_date`、`record_date`、`effective_date`、旧株数・新株数、種類、価格係数、状態を含める。
提供元が予定表から月報へ変わっただけで経済条件が同じ場合は値を変えず、経済条件・境界日・発表日の訂正時に変える。

### 複数回分割の適用契約

同じ銘柄で何回分割が起きても、最新値へ上書きしながら補正しない。対象基準に含まれる公式イベント集合から、
原本値へすでに反映済みと確認できたイベント集合を引き、残ったイベントの係数を一度だけ掛ける。

```text
target_actions  = target_basis_date までに有効な action_id の集合
source_actions  = 原本値へ反映済みと検証できた action_id の集合
applied_actions = target_actions - source_actions
per_share_factor = Π(1 / share_factor(action)) for action in applied_actions
```

イベントは `ex_date, action_id` 順の正規形で保存・計算し、`Decimal` で積を求める。同じ年に2回実施されても
年単位でまとめず、権利落ち日ごとの別イベントとして扱う。乗算結果だけでは監査できないため、原本事実には
`source_basis_action_ids`、派生値には `applied_action_ids` を安定した `action_id` のJSON配列で保存する。
配列は補助的な第二の台帳ではなく、正本の `corporate_actions` のどの行を使用したかを示す計算証跡であり、
比率・状態・日付は保持しない。係数は必ず正本を再読込して計算する。

例えば1:2分割後に公表されたEPSへ、その後1:3分割が起きた場合は、1:2と1:3の累積1/6を掛けず、
未反映の1:3だけを掛ける。さらに1:5分割が起きた場合は、新しい原本へ上書きせず同じ原本から
1/15を再生成する。いずれかのイベントが訂正・取消になれば `action_digest` が変わり、そのイベント以後の
価格・一株指標・配当・valuationをすべて無効化して原本から再生成する。同じdigestの再実行では
`applied_action_ids`、係数、値が完全に同じになることを必須とする。

## 権利の検知と反映時点

### 検知経路

| 段階 | 情報源 | 役割 | 採用条件 |
|---|---|---|---|
| 発表時 | TDnet本文 | 比率、基準日、効力日、訂正・中止を早期取得 | 本文の基準日と比率が一意に確定 |
| 予定表掲載時 | JPX権利落ち予定表 | 権利落ち日・基準日を公式確認 | 公式取引カレンダーと整合 |
| 月報掲載時 | JPX月報 | 実施済み比率を確定・訂正 | 公式PDFを保存して採用 |
| 約12週後 | J-Quants公式日足 | OHLCVと調整係数を事後監査 | 台帳の比率・日付と一致 |

Yahooの分割イベントや価格段差は候補検知にだけ使い、正本にはしない。

### 定期処理の確定順序

1. **16:05日次便**
   - 当日TDnet開示を保存する。
   - JPX予定表・月報・TDnet本文を `corporate_actions` へ反映する。
   - 公式情報の全体取得に失敗した場合、価格公開を止める。
   - 実施日到来または台帳訂正でdigestが変わった銘柄は、過去価格を原本から再生成する。
   - 価格公開後、同じdigestで一株指標、配当、バリュエーションを再生成する。
2. **20:30イブニング便**
   - 夜間TDnet開示保存後、分割・併合・無償割当て・訂正・中止だけを再解決する。
   - 将来イベントは台帳へ保存するだけで現在値を変えない。
   - 実施済みイベントの訂正は価格と派生値を即時 `pending/held` にし、古い値を公開しない。
3. **権利落ち日00:30便**
   - 日付が変わった時点で `actions_digest` にその日のイベントが入る。
   - 市場開始前に価格履歴を現在株数基準へ再生成する。
   - その後、EPS/BPS/DPSとバリュエーションを同じdigestへ更新する。
   - 00:30便に失敗しても、16:05便の価格公開前に同じ判定を再実行する。
4. **約12週後の監査**
   - J-Quantsの公式当時値・調整値と全OHLCVを照合する。
   - 不一致なら公開を止め、原本から再生成する。値動きからイベントを作らない。

20:30便で新規台帳を検知しても、実施前なら画面値は変えない。実施済み訂正なら、誤ったチャートを
残すより一時的に非表示にすることを優先する。

## データ構成

### 既存のまま正本にするもの

- `corporate_actions`: 現在有効な公式イベント
- `corporate_action_history`: 訂正を含む改訂履歴
- `price_source_snapshots`: 価格・公式文書原本
- `daily_prices`: 当時値と補正値
- `price_adjustment_state`: 価格の公開状態

`corporate_actions` には安定した `action_id` を追加し、`(code, ex_date)` だけをイベント識別子にしない。
同じ日に複数種類の権利がある場合を扱えるようにする。`corporate_action_history` は訂正だけでなく、TDnet、
JPX予定表、JPX月報、J-Quants監査の全公式観測を追記保存する。現在行は採用結果、historyは判断根拠とする。
`action_id` は訂正で変えず、最初の公式開示IDまたは永続的な内部IDから割り当てる。取得元ごとに別IDを作らず、
同じ経済イベントのJPX・TDnet・J-Quants観測は一つの `action_id` へ解決する。一方、同じ銘柄・同じ権利落ち日でも
対象証券、基準日、効力日、イベント種別が異なる別権利は別IDにする。観測のsource keyはhistory側で一意にする。

`daily_prices.adj_factor` は `DECIMAL(28,18)` へ後方互換ALTERし、既存raw行は正本台帳から再計算する。
価格表示はすでに保存済みOHLCVを読むため、この列の移行途中に画面側で係数を掛け直さない。

### `per_share_fact_versions`

EPS/BPS/DPSの公表版を開示・指標単位で保存する。同じ年度行でも、EPS/BPSだけが遡及修正済みで
DPSは分割前基準という実例があるため、wideな年度行へ共通の基準状態を置かない。原本は後続開示で上書きしない。

主な列:

- `fact_id`, `code`, `metric` (`eps` / `bps` / `dps`)、`source`, `source_ref`, `payload_hash`
- `period_type` (`annual` / `quarterly_cumulative` / `quarterly_standalone` / `ttm`)
- `period_start`, `period_end`, `announced_at`, `is_comparative`
- `consolidation_scope`, `accounting_standard`
- `raw_value`, `net_income_raw`, `equity_raw`
- `reported_average_shares`, `reported_end_shares`, `xbrl_concept`
- `source_basis_date`, `source_basis_status`, `source_basis_action_ids`
- `quality_status`, `quality_reason`, `ingested_at`

`source_basis_status` は `explicit_pre_action / explicit_post_action / derived_pre_action /
derived_post_action / ambiguous` とする。

判定優先順位:

1. XBRLの加重平均株式数、期末株式数、分割遡及注記など明示情報
2. 同一期の分割前開示と分割後開示のEPS/BPS比率
3. 純利益÷EPS、自己資本÷BPSからの逆算株式数と公式分割比率の整合
4. どちらにも一意に合わない場合は `ambiguous/needs_review`

### `per_share_fact_projections`

`per_share_fact_versions` の原本へ公式台帳の係数を一度だけ適用した、再生成可能な派生表とする。
原本表の列を更新せず、補正済み値から再補正しない。

主な列:

- `fact_id`, `action_digest`, `target_basis_date`, `calculation_version`
- `applied_action_ids`, `share_adjustment_factor`, `normalized_value`
- `quality_status`, `quality_reason`, `computed_at`

同じdigestのまま日付だけ進むたびに履歴行を増やさない。現在用は `(fact_id, action_digest,
calculation_version)` で再利用し、バックテストは当時判明済みの原本・権利から都度PIT計算する。
`source_basis_action_ids` または `applied_action_ids` が正本にない、重複する、対象基準日より未来、
または集合差から再計算した係数と一致しない場合は `needs_review` とし、正式値へ公開しない。

### 配当

- `dividend_notices`: 配当通知・訂正・削除の原本
- `dividend_events`: 訂正を解決した現在有効な中間・期末・特別・記念配当イベント
- `dividend_annual_normalized`: 通知ごとに正規化してから会計年度合計した現在基準DPS

年度DPSへ一括で係数を掛けない。中間・期末・特別・記念配当をイベント単位で補正する。

### `valuation_metrics_current`

画面、スクリーニング、理論株価、AIファンドが読む現在値を1行へ集約する。
`stock_fundamentals` はYahoo等の取得値・補助情報として残し、一株指標の正式な読取先にはしない。

主な列:

- `code`, `price_date`, `target_basis_date`, `price_version`, `action_digest`
- `eps_latest_annual`, `eps_ttm`, `eps_company_forecast`, `bps_latest`
- `dps_last_fiscal_year`, `dps_company_forecast`, `shares_outstanding`
- `shares_basis_date`, `shares_source_observed_at`, `shares_basis_status`
- `per_latest_annual`, `per_ttm`, `per_forecast`, `pbr`, `div_yield`, `market_cap`
- 各値の `fact_id` / `forecast_metric_id` / `source_ref`
- `quality_status`, `quality_reason`, `calculation_version`, `calculated_at`

公開条件は次のすべてを満たすこととする。

- 価格状態が `published` または `audit_due`
- `price_version` が現在の価格版と一致
- `action_digest` が現在の台帳digestと一致
- 使用した一株指標が `verified`

時価総額の株数は、出典の観測日と株数基準を確認する。最新分割後も取得元の株数更新が確認できない場合、
価格だけ新基準にして時価総額を計算せず、一時的にNULLと理由を出す。分割以外の増資・自己株式変動が
あり得るため、古い株数へ分割係数を掛けた値を無条件に正式値にはしない。

### `corporate_action_projection_state`

台帳変更後の再計算漏れと部分失敗を管理する小さな状態表を追加する。

```text
PRIMARY KEY (code, consumer)
consumer: price / per_share / dividend / valuation / ai_fund / alert
required_digest
completed_digest
status: scheduled / pending / complete / needs_review / error
not_before
last_error
updated_at
```

`put_action` と同じトランザクションで影響consumerを更新する。長時間の全体トランザクションは作らず、
各consumerが自分の出力へ同じdigestを保存してから `complete` にする。

### `data_coverage_state`

データが存在しない状態と取得未実行・取得失敗を区別し、新規上場銘柄を自動追跡する。

```text
PRIMARY KEY (code, dataset)
dataset: corporate_action / per_share / company_forecast / dividend
status: pending / available / no_data / not_applicable / error
last_checked_at
last_success_at
source_as_of
row_count
last_error
```

マスタへ新規上場銘柄が入った時点で各datasetを `pending` とし、取得後に必ず状態を更新する。
`forecast_metrics` に行がないだけで「会社予想なし」と判断しない。

価格監査・採点の対象集合も同じ考えで明示する。TOKYO PRO MARKETなど公式価格監査の対象外は
`not_applicable` とし、`held/error` の再試行キューへ入れない。通常市場の修復キューは、ウォッチリスト、
対象普通株、未試行、最終試行時刻の順に処理し、恒久的な対象外銘柄で枠が埋まらないようにする。

### 過去権利の初回バックフィル

- 対象開始日は銘柄ごとに `MIN(価格日, 一株指標の最古period_end, 配当イベントの最古日)` とする。
- JPX月報の現行取得範囲外は、EDINET有報の株式数推移・分割注記、保存済みTDnet、会社公式IRを公式根拠にする。
- `financials_edinet_annual.split_adjustment_factor` やYahooイベントは候補・整合性確認にだけ使い、
  それ単独で公式イベントを創作しない。
- 公式根拠を特定できない候補は `needs_review` とし、一株値を正式値へ混ぜない。

## 画面とチャートの統一

### 株価チャート

- UIで係数を掛けない。すべて `price_history` または `price_store.load_candles` を読む。
- `price_history.close` が補正済みの正本なので、`COALESCE(adj_close, close)` を段階的に廃止する。
- 銘柄詳細、ウォッチリスト、チャートグリッド、ポートフォリオの価格系列を共通ローダーへ寄せる。
- `held/pending` の銘柄は古い系列を表示せず、確認中メッセージを出す。
- 公開版変更時はサーバーキャッシュとブラウザー側チャートキャッシュを失効する。
- 旧行に `adj_close` しかなく `price_basis` / raw OHLCV がない場合、`price_history.close=NULL` のまま
  正常データとして返さない。原本から再構築するか、検証済み期間だけを返して `available_from` と理由を画面に出す。
- 公式台帳でイベントを確定済みの場合、Yahoo等のイベントメタデータは「提供系列が既に分割調整済みか」の
  基準判定にだけ使用できる。イベントの存在・比率・日付を値動きやYahooだけから決めない。公式当時値がない期間は
  提供元メタデータ、公式イベントとの完全一致、全期間を一括取得した同一スナップショット、連続性監査の全条件を
  満たす場合だけ `split_adjusted` とし、根拠をスナップショットへ保存する。

### 業績チャート

- 売上・各利益・資産は原本額を表示する。
- EPS/BPS/DPSだけを `per_share_fact_versions` / `dividend_annual_normalized` の補正値へ置換する。
- 予想EPS/ROEは `forecast_metrics` の単一式だけを使う。
- 銘柄詳細内の「純利益÷現在株数」計算とYahoo予想EPSへの画面内フォールバックを削除する。
- 見出しに「一株指標は分割調整済み・基準日」を表示する。
- 未確認年度は線をつながずNULLで表示し、未補正値を補正済み系列へ混ぜない。

## 消費側の切替対象

| 消費側 | 切替内容 |
|---|---|
| `financials_view.py` | EPS/DPSを補正済み読取モデルへ変更 |
| `forecast_metrics.py` | 補正済み実績EPS/BPSから予想EPS/ROEを再生成 |
| `fundamentals.py` | PER/PBR/利回りの正式値を `valuation_metrics_current` へ移す |
| `app.py` | 銘柄詳細・業績グリッド・スクリーナーを同じ正式値へ統一 |
| `compute_theoretical.py` | 正規化済みEPS/BPS以外を理論株価へ渡さない |
| `fundamentals_ext.py` | 連続増配を `dividend_annual_normalized` で計算 |
| `portfolio_analytics.py` | 配当見込みを正規化済み予想DPSで計算 |
| `portfolio_analytics.trade_summary` | 権利落ち日に未決済ロットの株数・単価を変換 |
| `compute_price_stats.py` | 価格は現行の `price_history` を維持し、一株指標の独自補正は追加しない |
| `ai_fund_adjustments.py` | 現行の適用履歴方式を維持し、共通係数APIへ接続 |
| `daily_run.py` | 権利同期→価格→一株指標・配当→valuation→各分析の順を固定 |
| `.github/workflows/misc_batch.yml` | 00:30便で権利到来の価格再生成を予想指標再計算より先に実行 |

SBIの現在保有明細は証券会社側で分割反映済みなので追加補正しない。約定原本も変更せず、実現損益の
再現時だけ未決済ロットを権利日順に変換する。

## 実装単位

### 1. 共通係数と状態管理

- `price_adjustments.py` に `share_factor`、`per_share_factor`、用途別境界判定を追加
- `price_store.py` で `price_adjustment_state.target_basis_date` を追加
- `corporate_action_projection_state` と無効化処理を追加
- `daily_run.run_evening` で夜間開示後の権利訂正を再解決
- `.github/workflows/misc_batch.yml` の00:30処理順を権利・価格→財務→予想指標へ変更
- 既存価格APIを変えず回帰テストを追加

### 2. 公表版一株指標

- TDnet/EDINETの開示・指標単位で `per_share_fact_versions` を保存
- `per_share_fact_projections` へ株数基準判定と補正結果を保存
- `forecast_metrics` を同じ基準へ接続
- 現行v2との並行比較レポートを出す

### 3. 配当

- `dividend_notices` と訂正解決を実装
- `dividend_annual_normalized` を再生成
- 連続増配、配当見込み、配当利回りを切り替える

### 4. 正式な現在値

- `valuation_metrics_current` を構築
- 銘柄詳細、スクリーナー、理論株価、AIファンドを順に移行
- 移行中は旧値と新値を並べて監査し、画面には一方だけを出す

### 5. 売買履歴・画面

- 実現損益へロット単位の分割処理を追加
- 価格・業績の全チャートを共通の公開状態へ接続
- PC・スマホで表示確認

## 必須テスト

### 純粋関数

- 分割なし、1:2、1:25、複数回、10:1併合、1:0.1無償割当て
- 価格、一株値、株数、出来高の逆数関係
- ライツ、端株、未確定、競合、取消を自動処理しないこと
- 出典がすでに遡及修正済みの場合に係数1となること

### DB・再実行

- 同じイベント・同じジョブを複数回実行して値が変わらないこと
- 1回目の分割反映後に2回目・3回目を追加しても、補正済み値ではなく原本から累積係数で再生成されること
- 同一年の複数イベントを別々に適用し、`applied_action_ids` に欠落・重複がないこと
- 1回目または途中のイベントを訂正・取消した場合、後続を含むdigestと全派生値が再生成されること
- 同じdigestで再実行した場合、`applied_action_ids`、係数、補正値、公開版が変わらないこと
- 台帳訂正で全consumerがpendingになり、旧digestの値が公開されないこと
- 途中失敗後の再実行で原本が増殖せず、派生値だけ復旧すること
- 価格版と一株指標版が異なる間、PER/PBR等が表示されないこと

### チャート

- 1:2分割で分割前2,000円、分割後1,000円の系列が同一基準で連続すること
- OHLCの大小関係、出来高の逆補正、前日比、移動平均が同じ基準であること
- 銘柄詳細、ウォッチリスト、チャートグリッドが同じOHLCを返すこと
- 補正保留時は古いチャートを返さず、復旧後にキャッシュが更新されること
- EPS/BPS/DPSチャートに分割だけを原因とする段差がなく、未確認値を線で結ばないこと
- PCとスマホのPlotly表示、期間切替、日週月足、拡大・復帰を確認すること

### 経済的な不変条件

1:2分割の前後で、次が丸め誤差の範囲で不変であることを確認する。

- `株価 × 株数`
- `取得単価 × 保有株数`
- `EPS ÷ BPS`（ROE相当）
- `株価 ÷ EPS`（PER）
- `株価 ÷ BPS`（PBR）
- `DPS ÷ 株価`（配当利回り）

### 実データ

- 既存テストの285A（複数倍分割）、2814（無償割当て）を回帰対象にする。
- 3064（MonotaRO）を2015年分割の回帰対象にする。2015年開示のEPS/BPS/DPSは2015年分割を
  遡及反映済みだが、2018年・2021年の各1:2分割は未反映なので、現在基準へは追加で1/4を適用する。
- 2801（キッコーマン）を2024年1:5分割の回帰対象にする。2024年3月期開示はEPS/BPSが分割後基準、
  年間DPSが分割前基準という混在を扱い、EPS/BPSへ再度1/5を掛けず、DPSはイベント単位で正規化する。
- 2801の旧価格期間は安全な再構築または理由付き期間制限を行い、3年指定でNULL系列・無言の短縮を返さない。
- 分割なしの対照銘柄として3003を使い、予想EPS 165.85617577円、予想ROE 13.78963183%が
  不要に変化しないことを確認する。
- 保存期間内の複数回イベントについて、2026-11-21時点の実DBで次を回帰対象にする。
  - 8136（サンリオ）: 2024年1:3、2026年1:5。累積株数係数15、過去一株値・株価係数1/15。
  - 4062（イビデン）: 2025年1:2、2026年1:2。累積株数係数4、過去一株値・株価係数1/4。
  - 5535（ミガロHD）: 2024年1:2、2025年2月1:2、同年5月1:2。累積株数係数8、
    過去一株値・株価係数1/8。同一年の2回をまとめたり片方を落としたりしない。
  - 2814（佐藤食品工業）: 2025年1:1.1無償割当て、2026年1:2分割。累積株数係数2.2、
    過去一株値・株価係数1/2.2。整数丸めをしない。
- 上記4銘柄の公開可能なraw日足全行を、各日より後の確定イベント積から再計算した結果、現状は
  価格係数、OHLC、出来高とも不一致0件である。この状態を実装後も全行監査する。
- 4062の2026年3月期原本は最初の1:2後、2回目の1:2前に公表されているため、現在基準への追加係数は
  2回目だけの1/2とする。2814も2026年3月期原本に2025年の無償割当てが反映済みなら、2026年分割の
  1/2だけを適用する。8136・5535を含め、単に全イベント積を各年度へ再適用しないことを確認する。
- 現行 `forecast_metrics` で4062と2814は `corporate_action_since_basis` により `needs_review` となる。
  実装後は、上記イベント集合差でEPS/BPSを現在基準へ正規化してから予想EPS/ROEを再生成し、
  8136・5535の現在 `verified` の値も二重補正がないことを計算証跡で再検証する。
- 2026-11-21の全体監査では、確定権利725件に過去の未確定・不正比率は0件、将来権利は6件で、
  公開4,160銘柄のdigest不一致は0件だった。権利対象のraw日足418,057行はOHLC・出来高不一致0件。
  `adj_factor` のみ60,371行で正確な係数との差があったが、全件が現行`DECIMAL(12,6)`の丸め範囲内
  （最大4.7846e-7）で、OHLCVには影響していない。実装後は18桁保存で係数差も0件にする。
- 同日の種類差が履歴にある1357と2814は、別の経済イベントではなく、同一イベントを取得元が
  split/consolidation、split/bonusと異なる語で表した例だった。sourceごとに別actionを増殖させず、
  経済条件を比較して一つの `action_id` へ解決する回帰例にする。
- 保留282銘柄の内訳はTOKYO PRO 188、プライム3、スタンダード47、グロース10、その他34。
  TOKYO PROは対象外状態へ分離し、その他市場はETF等の商品区分を判定する。プライム・スタンダード・
  グロース60件は修復または理由付き状態へ解決し、対象普通株の`held`を0件にしてから切り替える。
- 実DB履歴に取消済みイベントは0件だったため、取消は実データだけに依存せず、1回目・途中・最新イベントを
  それぞれ取り消す合成DBテストを本番切替の必須条件にする。
- 実DBの確定済みイベントから、分割、併合、複数回分割、訂正ありを各1銘柄以上自動抽出して監査する。

## 本番切替条件

- 全単体テストと `python -m unittest discover -s tests -v` が成功
- 実DBで全銘柄の `required_digest = completed_digest`、または理由付き `needs_review`
- 価格監査対象外市場は `not_applicable` に分離され、対象普通株の `price_adjustment_state.status='held'` が0件
- `daily_prices.adj_factor` を正本から全行再計算し、保存精度を含む係数不一致が0件
- 旧値と新値の差分を、分割あり・なしに分けて件数と上位差異で確認
- `/stock/<code>`、`/watchlist`、`/api/watchlist/chart/<code>`、`/api/chart_grid`、
  `/portfolio`、`/screen`、`/valuation` をPC・スマホで確認
- 本番反映後に `/health`、対象銘柄、価格・EPS・PER/PBR・配当利回り、チャートを確認
- 問題時は旧表を戻すのではなく、読取先の切替だけを戻せること

## 非対象

- 分割履歴を値動きから創作すること
- Yahoo `adjclose` の採用
- 分割と増資・自己株式取得を同じ係数で扱うこと
- 不明な基準を別ソースの推測値で自動補完すること
- 原本のEPS/BPS/DPSや約定履歴を上書きすること

この計画を分割補正実装の正本とし、以後の実装は上記5単位の順に進める。

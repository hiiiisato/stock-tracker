# 事業内容の整理（AI要約）— Codex引き継ぎ文書

最終更新: 2026-07-11
前提知識: リポジトリ直下の `ARCHITECTURE.md`（DB接続・テーブル一覧・開発指針）を先に読むこと。

## 目的

銘柄ページの「事業内容」が有価証券報告書の記載そのままで長く読みにくい
（`stocks.business_description`・平均約6,000字・最大63,000字）。
AI（Codex）がDB内の有報由来テキストを読み、投資家が理解しやすい形に整理した文章を
**別テーブルに**保存し、サイト表示に反映する。

## 基本方針

- 有報原文（`stocks.business_description`）は根拠データとしてそのまま残す。**上書きしない。**
- 表示用の「整理済み事業概要」は新テーブル `business_overviews` に保存する。
- Gemini等の外部AI APIは使わない（課金回避）。要約はユーザーがCodexに指示した
  タイミングで、CodexがDBから未処理銘柄を読んで作成・保存する運用。
- サーバー側の日次バッチには**一切手を入れない**（下記ハッシュ照合方式のため不要）。

## 設計の要点: ステータスフラグは使わない（ハッシュ照合で導出）

`business_description` の書き込み経路は `edinet_business.py` と `edinet_texts.py` の
**2つ**あり、書き込み側に「要約未処理フラグを立てる」処理を入れると経路追加のたびに
更新漏れバグを生む。代わりに、要約テーブル側に**要約時点の原文ハッシュ**を持ち、
未処理・stale をSQLで導出する。書き込み側の変更ゼロ・自己修復的。

```sql
-- 未処理（未要約 or 有報更新で古くなった）銘柄の取得。優先順:
--   1. ウォッチリスト銘柄  2. 時価総額の大きい順
SELECT s.code, s.name
FROM stocks s
LEFT JOIN business_overviews b ON b.code = s.code
LEFT JOIN price_stats p        ON p.code = s.code
LEFT JOIN watchlist w          ON w.code = s.code
WHERE s.is_active = 1
  AND s.business_description IS NOT NULL
  AND (b.code IS NULL OR b.source_hash != SHA2(s.business_description, 256))
ORDER BY (w.code IS NOT NULL) DESC, p.market_cap DESC
LIMIT 10;
```

## DBテーブル（新規作成）

`stocks` へのカラム追加はしない（コアテーブルの肥大化を避ける）。

```sql
CREATE TABLE IF NOT EXISTS business_overviews (
    code              VARCHAR(10) PRIMARY KEY,
    overview          TEXT,         -- 整理文（である調・3〜5文・250〜400字）
    points_json       TEXT,         -- 構造化データ（JSON文字列。既存テーブル群に合わせTEXT型）
    source_hash       CHAR(64),     -- 要約時点の business_description の SHA256（照合キー）
    source_updated_at DATETIME,     -- 要約時点の stocks.biz_updated_at（表示の鮮度注記用）
    generated_by      VARCHAR(20),  -- 'codex'（将来 'claude' 等が増えても対応）
    updated_at        DATETIME
);
```

保存時の `source_hash` は必ず **読み込んだ時点の原文** から計算する
（`SHA2(business_description, 256)` をSELECT時に一緒に取得すると安全）。

### points_json の構造

```json
{
  "main_businesses":      ["主な事業（文で簡潔に）"],
  "products_services":    ["主な商材・サービス"],
  "customers_or_channels":["顧客層・販売チャネル"],
  "features":             ["特徴（控えめな表現で）"],
  "keywords":             ["検索・分類用キーワード"],
  "notes":                ["補足。本文から判断できない事項の明記など"]
}
```

**注意: `segments` キーは持たない。** セグメント情報は公式数値テーブル
`company_segments`（edinet_segments.py が毎晩自動更新・売上構成比・利益率・約7年分）
が唯一の正であり、テキスト由来のセグメント名を二重保存すると将来必ず食い違う。

## 要約時の材料（2つ渡す）

1. **有報原文**: `stocks.business_description`
2. **セグメント公式数値**（あれば）: 整理文の規模感の裏付けに使う

```sql
SELECT segment_name, revenue_share, oi_margin, revenue_yoy
FROM company_segments
WHERE code = %s AND segment_type IN ('reportable', 'other')
  AND fiscal_year = (SELECT MAX(fiscal_year) FROM company_segments WHERE code = %s)
ORDER BY revenue DESC;
```

## 要約プロンプト（テンプレート）

```text
有価証券報告書の「事業の内容」と、セグメント別売上構成比（公式数値）をもとに、
企業の事業内容を3〜5文・全体250〜400字で整理してください。

- 文体は「である調」
- 冗長な表現、IR文書特有の長い修飾、関係会社名の羅列は避ける
- 主な事業、商材・サービス、顧客・販売先、特徴を読み取れる範囲で整理する
- セグメント別売上構成比が提供されている場合は整合させ、主力事業には
  「売上の約◯割」の形で規模感を添える
- 有報本文・提供数値から直接読み取れない強み・弱み・競合比較は断定しない
  （「特徴である」「主力である」程度の表現に留める）
- 有報原文に無い固有名詞・数値を創作しない
- 仕入れ先・販売先が具体名で書かれていない場合は、顧客層・販売チャネルとして整理する
```

### 模範例（1911 住友林業）— この文体・粒度に合わせる

```text
住友林業は、山林事業を原点に、木材・建材、住宅、建築・不動産、資源環境を展開する
総合住生活企業である。木材の調達・製造・加工・販売から、戸建住宅や集合住宅の建築、
リフォーム、分譲住宅、不動産開発・管理までを国内外で手がける。海外では住宅販売、
戸建住宅建築、集合住宅・商業複合施設の開発を進めている。木材資源、住宅、不動産開発、
森林・再生可能エネルギーを横断して展開する点が特徴である。
```

## 表示側（app.py `_build_stock_page`）の方針

会社概要ボックス内の表示優先順位を以下に変更する。

```text
1. business_overviews.overview があれば表示（整理済み事業概要）
   → 「AI整理（有報 YYYY-MM-DD 時点）」の鮮度注記を必ず付ける
2. なければ stocks.business_summary（kabutan概要・約100字）を表示
3. なければ business_description の冒頭を表示
4. business_description 原文は従来どおり折りたたみ（details）で常に残す
```

`points_json` の項目（主な事業・商材・顧客・特徴）は、overview本文の下に
小さな見出し付きリストで表示する。データが無い・パース失敗時は非表示に
フォールバック（既存方針: 0件・NULLでもエラーにしない）。

なお会社概要ボックスには既にセグメント構成の表示（`company_segments` 由来）が
あるため、レイアウトの重複・冗長に注意する。

## 実装順

```text
1. business_overviews テーブル作成
2. app.py に優先表示＋鮮度注記＋points_json リスト表示（無ければ非表示）
3. 1911 住友林業を手動で保存して表示確認（スマホ表示も確認）
4. Codex運用開始: 上記の未処理取得SQLで10〜20件ずつ要約・保存
```

## Codexへの運用指示例

```text
docs/business_overview_handoff.md の手順に従い、未処理銘柄を10件読み、
事業内容をである調で整理して business_overviews に保存してください。
セグメント公式数値がある銘柄は売上構成比と整合させてください。
弱みや競合比較は有報本文から断定できる場合だけ notes に書いてください。
```

## 制約・注意

- TiDB Cloud無料枠のため、TEXT系カラムの実効上限は約63KB（overview/points_jsonは余裕）。
- SQLはすべてプレースホルダー（%s）を使用。HTMLへの埋め込みは必ずエスケープ。
- 弱み・リスク分析は初期スコープ外。扱う場合は有報「事業等のリスク」
  （`edinet_text_blocks` の同名セクション）等を別途読む必要があるが、
  現状カバレッジが約108件しかない点に注意。

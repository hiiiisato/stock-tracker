#!/usr/bin/env python3
"""YouTube株関連動画の週次巡回 — マーケット状況・テーマ・注目銘柄をAIで構造化して蓄積する。

【目的】
最新の市場ニュース・注目テーマ・個別銘柄の見方をいち早くキャッチし、投資戦略に役立てる。
複数の発信者が同時に言及する銘柄・テーマ＝市場の注目が集まっている先を機械的に抽出する。

【仕組み（すべて無料・サーバー側完結）】
1. チャンネルリスト(CHANNELS)のハンドルを channel_id に解決（初回のみ・DBキャッシュ）
2. YouTube公式RSS（キー不要）で直近 LOOKBACK_DAYS 日の新着動画を取得
3. Gemini の YouTube動画理解（URLを直接渡すと動画内容を解析できる）で各動画を構造化:
   マーケット状況 / 言及テーマ / 言及銘柄(コード・強気弱気・理由) / 発信者の相場観
4. 銘柄名・コードは stocks テーブルと突合して検証（存在しないコードは名前で再解決）
5. 全動画を横断して週次サマリーを生成（共通見解・複数動画で言及された銘柄・新出テーマ）
6. /youtube ページで表示（週次サマリー→動画別カード→言及銘柄ランキング）

【制約・設計判断】
- Gemini無料枠は YouTube動画処理 1日8時間まで → 1回の実行で MAX_ANALYZE 本・
  ライブ配信アーカイブ（長時間）はタイトルで除外
- 分析失敗（非公開化・年齢制限等）は failed 記録して再試行しない
- 週1実行（GHA・土曜朝）。手動実行: python youtube_insights.py [--limit N] [--video VIDEO_ID]

【チャンネルの増減】CHANNELS を編集するだけ（ハンドルは youtube.com/@xxx のxxx部分）。
"""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import requests

from config import get_conn, GEMINI_API_KEY

# ── 巡回チャンネル（ハンドルは実在検証済み。追加はここに1行足すだけ）──────────
CHANNELS: list[dict] = [
    {"handle": "DanTakahashi1",    "name": "高橋ダン"},
    {"handle": "kabunokaidoki",    "name": "株の買い時を考えるチャンネル"},
    {"handle": "gototatsuya",      "name": "後藤達也・経済チャンネル"},
    {"handle": "MatsuiSecurities", "name": "松井証券"},
]

LOOKBACK_DAYS   = 8      # 何日前までの動画を対象にするか（週次+バッファ）
MAX_PER_CHANNEL = 3      # 1チャンネルあたり最大何本
MAX_ANALYZE     = 10     # 1回の実行でGemini分析する最大本数（無料枠の動画8時間/日に配慮）
GEMINI_MODEL    = "gemini-2.5-flash"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Accept-Language": "ja"}

# ライブ配信アーカイブ等の長尺・分析対象外をタイトルで除外
SKIP_TITLE = re.compile(r"(生配信|ライブ|LIVE|ラジオ|雑談|切り抜き|shorts?)", re.I)

ANALYZE_PROMPT = """この投資系YouTube動画を分析し、以下のJSONのみを出力してください（コードブロック不要）。
{
  "market": "マーケット状況・地合いの要約（2文以内）",
  "themes": ["言及された投資テーマ（例: 半導体, データセンター。最大5つ）"],
  "stocks": [
    {"name": "銘柄名", "code": "証券コード4桁(日本株のみ・不明なら空)", "view": "強気|弱気|中立",
     "reason": "そう見る理由（具体的な数字・事実を含めて1-2文）"}
  ],
  "stance": "発信者の相場観・スタンス（1文）",
  "actionable": "視聴者への具体的な示唆があれば（1文・なければ空）",
  "key_points": ["動画中の重要な具体的情報を5-8個。数字・固有名詞・根拠をそのまま保持する。
    例: '日経平均は週間で-4.0%、3月以来の下落幅'
    例: '安川電機の1Q営業益はコンセンサスを約30%下回った'
    例: '銀行株は金利上昇期待で年初来高値圏、三菱UFJは新高値'"]
}
注意: 動画内で実際に言及された内容のみ。銘柄は個別に語られたもののみ（指数・ETFはstocksに含めず themes/market へ）。
key_pointsは抽象的な要約ではなく、後から読んで役立つ具体的なファクト・数字・見解を優先。"""

WEEKLY_PROMPT = """あなたは投資情報誌の編集者です。以下は今週の株式投資系YouTube動画{n}本の分析結果（各動画の
要点・具体的ファクト・銘柄への見方）です。これらを**全て集約・整理して1本の週次レポート**に仕上げてください。

素材:
{videos}

以下のJSONのみを出力してください（コードブロック不要）。

{{
  "summary": "今週のマーケット状況と発信者たちの共通見解（3文以内・結論ファースト）",
  "consensus": "強気/弱気の全体トーン（1文）",
  "hot_themes": [{{"theme": "複数動画または強い確信で言及されたテーマ", "note": "何が言われているか（1文）"}}],
  "hot_stocks": [{{"name": "銘柄名", "code": "コード", "note": "どう言及されたか（1文）"}}],
  "divergence": "発信者間で見方が分かれている論点があれば（1文・なければ空）",
  "report_md": "Markdown形式の週次レポート本文（後述の構成・800〜1500字）"
}}

report_md の構成（Markdown・## 見出し）:
## 今週の結論
3行以内。今週何が起き、来週に向けて何を見るべきか。
## マーケット概況
各発信者の見立てを統合。指数の具体的な動き・数字は保持する（例: 日経平均は週間-4.0%）。
## 注目テーマ
テーマごとに小見出し(###)を立て、「何が起きたか→発信者の見方→関連銘柄」。具体的な数字・事実を残す。
## 個別銘柄の見方
言及された銘柄ごとに1行〜2行: **銘柄名(コード)** — 強気/弱気と具体的な理由・数字。
複数の発信者が言及した銘柄は「複数言及」と明示（注目度が高いシグナル）。
## 見方が分かれる点
あれば。なければこのセクションごと省略。
## 来週の注目点
動画で言及された決算・イベント・指標があれば具体的に。

厳守事項:
- 抽象論に逃げず、素材のkey_pointsにある具体的な数字・固有名詞・根拠を積極的に本文へ残す
- 素材に無い情報を創作しない。銘柄コードは素材にあるもののみ使用
- どの発信者の見方かが分かるように適宜「（高橋ダン）」等と出典を添える"""


# ═══════════════════════════════════════════════════════════
#  スキーマ
# ═══════════════════════════════════════════════════════════

def ensure_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS youtube_channels (
            handle      VARCHAR(60) PRIMARY KEY,
            channel_id  VARCHAR(30),
            name        VARCHAR(80),
            resolved_at DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS youtube_videos (
            video_id    VARCHAR(16) PRIMARY KEY,
            handle      VARCHAR(60),
            channel     VARCHAR(80),
            title       VARCHAR(300),
            published   DATETIME,
            status      VARCHAR(12),      -- analyzed / failed / skipped
            market      VARCHAR(500),
            stance      VARCHAR(300),
            actionable  VARCHAR(300),
            themes_json TEXT,             -- ["半導体", ...]
            stocks_json TEXT,             -- [{name, code, view, reason}, ...]
            analyzed_at DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS youtube_weekly (
            week_end    DATE PRIMARY KEY,
            n_videos    INT,
            summary     VARCHAR(1000),
            consensus   VARCHAR(300),
            divergence  VARCHAR(300),
            themes_json TEXT,             -- [{theme, note}]
            stocks_json TEXT,             -- [{name, code, note}]
            report_md   TEXT,             -- 週次レポート本文(Markdown・全動画を集約した1本のレポート)
            created_at  DATETIME
        )
    """)
    # 旧スキーマからの移行（列が無ければ追加）
    for tbl, ddl in [("youtube_videos", "ADD COLUMN notes_json TEXT"),
                     ("youtube_weekly", "ADD COLUMN report_md TEXT")]:
        try:
            cur.execute(f"ALTER TABLE {tbl} {ddl}")
        except Exception:  # noqa: BLE001  既に存在
            pass


# ═══════════════════════════════════════════════════════════
#  取得
# ═══════════════════════════════════════════════════════════

def _resolve_channel(cur, handle: str, name: str) -> str | None:
    """ハンドル→channel_id（DBキャッシュ・初回のみHTMLから解決）。"""
    cur.execute("SELECT channel_id FROM youtube_channels WHERE handle=%s", (handle,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    try:
        r = requests.get(f"https://www.youtube.com/@{handle}", headers=UA, timeout=15)
        m = re.search(r'"externalId":"(UC[\w-]+)"', r.text)
        cid = m.group(1) if m else None
    except Exception as e:  # noqa: BLE001
        print(f"  [{handle}] 解決失敗: {str(e)[:60]}")
        cid = None
    if cid:
        cur.execute("""
            INSERT INTO youtube_channels (handle, channel_id, name, resolved_at)
            VALUES (%s,%s,%s,NOW())
            ON DUPLICATE KEY UPDATE channel_id=VALUES(channel_id), name=VALUES(name),
                                    resolved_at=NOW()
        """, (handle, cid, name))
    return cid


def _fetch_recent(channel_id: str) -> list[dict]:
    """公式RSSから直近動画（新しい順）。"""
    r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                     headers=UA, timeout=15)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", ns):
        out.append({
            "video_id":  e.find("yt:videoId", ns).text,
            "title":     e.find("a:title", ns).text or "",
            "published": datetime.fromisoformat(e.find("a:published", ns).text).replace(tzinfo=None),
        })
    return out


# ═══════════════════════════════════════════════════════════
#  Gemini 分析
# ═══════════════════════════════════════════════════════════

def _gemini():
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def _verify_codes(cur, stocks: list[dict]) -> list[dict]:
    """Geminiが出した銘柄コードを stocks テーブルで検証。不正なら名前で再解決、それでも
    無ければ code を空にする（誤リンク防止）。"""
    out = []
    for s in stocks or []:
        code = str(s.get("code") or "").strip()
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        ok = False
        if re.match(r"^[0-9][0-9A-Z]{3}$", code):
            cur.execute("SELECT name FROM stocks WHERE code=%s", (code,))
            ok = cur.fetchone() is not None
        if not ok:
            # 名前の部分一致で解決（全角対応のためLIKE両側）
            cur.execute("SELECT code FROM stocks WHERE is_active=1 AND name LIKE %s LIMIT 1",
                        (f"%{name[:10]}%",))
            row = cur.fetchone()
            code = row[0] if row else ""
        out.append({"name": name[:40], "code": code,
                    "view": str(s.get("view") or "中立")[:4],
                    "reason": str(s.get("reason") or "")[:150]})
    return out


def _gen_with_retry(client, contents, retries: int = 2):
    """Gemini呼び出し。429(無料枠の分間トークン制限)は毎分リセットされるため70秒待ちで再試行。"""
    from google.genai import types
    cfg = types.GenerateContentConfig(
        # 動画トークンを約1/4に削減（内容・音声の分析品質には影響小。無料枠のTPM対策）
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    )
    for attempt in range(retries + 1):
        try:
            return client.models.generate_content(model=GEMINI_MODEL, contents=contents, config=cfg)
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) and attempt < retries:
                print(f"  レート制限(429)・70秒待って再試行 ({attempt + 1}/{retries})")
                time.sleep(70)
                continue
            raise


def analyze_video(cur, client, video: dict) -> bool:
    """1本をGeminiで分析してDB保存。成功=True。
    429等の一時的エラーは記録しない（次回実行で自動再試行）。恒久エラーのみ failed 記録。"""
    from google.genai import types
    url = f"https://www.youtube.com/watch?v={video['video_id']}"
    transient = False
    try:
        resp = _gen_with_retry(client, types.Content(parts=[
            types.Part(file_data=types.FileData(file_uri=url)),
            types.Part(text=ANALYZE_PROMPT),
        ]))
        d = _parse_json(resp.text)
    except Exception as e:  # noqa: BLE001
        transient = any(k in str(e) for k in ("429", "503", "500", "timeout", "Timeout"))
        print(f"  [{video['video_id']}] 分析失敗{'(一時的・次回再試行)' if transient else ''}: {str(e)[:80]}")
        d = None
    if not d:
        if not transient:
            cur.execute("""
                INSERT INTO youtube_videos (video_id, handle, channel, title, published, status, analyzed_at)
                VALUES (%s,%s,%s,%s,%s,'failed',NOW())
                ON DUPLICATE KEY UPDATE status='failed', analyzed_at=NOW()
            """, (video["video_id"], video["handle"], video["channel"],
                  video["title"][:300], video["published"]))
        return False

    stocks = _verify_codes(cur, d.get("stocks") or [])
    cur.execute("""
        INSERT INTO youtube_videos
            (video_id, handle, channel, title, published, status,
             market, stance, actionable, themes_json, stocks_json, notes_json, analyzed_at)
        VALUES (%s,%s,%s,%s,%s,'analyzed',%s,%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
            status='analyzed', market=VALUES(market), stance=VALUES(stance),
            actionable=VALUES(actionable), themes_json=VALUES(themes_json),
            stocks_json=VALUES(stocks_json), notes_json=VALUES(notes_json), analyzed_at=NOW()
    """, (video["video_id"], video["handle"], video["channel"], video["title"][:300],
          video["published"],
          str(d.get("market") or "")[:500], str(d.get("stance") or "")[:300],
          str(d.get("actionable") or "")[:300],
          json.dumps((d.get("themes") or [])[:6], ensure_ascii=False),
          json.dumps(stocks, ensure_ascii=False),
          json.dumps([str(k)[:200] for k in (d.get("key_points") or [])[:10]], ensure_ascii=False)))
    return True


def aggregate_weekly(cur, client, week_end: date) -> bool:
    """直近 LOOKBACK_DAYS 日の分析済み動画を横断して週次サマリーを生成・保存。"""
    cur.execute("""
        SELECT channel, title, market, stance, themes_json, stocks_json, notes_json
        FROM youtube_videos
        WHERE status='analyzed' AND published >= %s
        ORDER BY published DESC
    """, (datetime.now() - timedelta(days=LOOKBACK_DAYS),))
    rows = cur.fetchall()
    if not rows:
        print("  集約対象なし")
        return False
    digest = []
    for ch, title, market, stance, tj, sj, nj in rows:
        digest.append({"channel": ch, "title": title, "market": market, "stance": stance,
                       "themes": json.loads(tj or "[]"), "stocks": json.loads(sj or "[]"),
                       "key_points": json.loads(nj or "[]")})
    prompt = WEEKLY_PROMPT.replace("{n}", str(len(digest))).replace(
        "{videos}", json.dumps(digest, ensure_ascii=False))
    try:
        resp = _gen_with_retry(client, prompt)
        d = _parse_json(resp.text)
    except Exception as e:  # noqa: BLE001
        print(f"  週次集約失敗: {str(e)[:80]}")
        return False
    if not d:
        return False
    hot_stocks = _verify_codes(cur, [
        {"name": s.get("name"), "code": s.get("code"), "view": "", "reason": s.get("note")}
        for s in (d.get("hot_stocks") or [])])
    # _verify_codesの出力を hot_stocks 形式に戻す
    hot_stocks = [{"name": s["name"], "code": s["code"], "note": s["reason"]} for s in hot_stocks]
    cur.execute("""
        INSERT INTO youtube_weekly
            (week_end, n_videos, summary, consensus, divergence, themes_json, stocks_json,
             report_md, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
            n_videos=VALUES(n_videos), summary=VALUES(summary), consensus=VALUES(consensus),
            divergence=VALUES(divergence), themes_json=VALUES(themes_json),
            stocks_json=VALUES(stocks_json), report_md=VALUES(report_md), created_at=NOW()
    """, (week_end, len(digest),
          str(d.get("summary") or "")[:1000], str(d.get("consensus") or "")[:300],
          str(d.get("divergence") or "")[:300],
          json.dumps((d.get("hot_themes") or [])[:8], ensure_ascii=False),
          json.dumps(hot_stocks[:10], ensure_ascii=False),
          str(d.get("report_md") or "")[:20000]))
    return True


def notify_weekly(cur, week_end: date) -> bool:
    """週次サマリーをLINEに通知（日次レポートと同じトランスポート・要点＋リンク）。
    LINE未設定なら送信スキップ（例外を出さない）。"""
    from line_notify import is_configured, push_text
    if not is_configured():
        print("  [YouTube週報LINE] LINE未設定のためスキップ")
        return False
    cur.execute("""
        SELECT n_videos, summary, consensus, themes_json, stocks_json
        FROM youtube_weekly WHERE week_end = %s
    """, (week_end,))
    row = cur.fetchone()
    if not row:
        return False
    n_videos, summary, consensus, tj, sj = row
    themes = [t.get("theme") for t in json.loads(tj or "[]") if t.get("theme")][:5]
    stocks = [s.get("name") for s in json.loads(sj or "[]") if s.get("name")][:6]
    from daily_report import _report_base_url
    lines = [f"📺 YouTube週報（{week_end.strftime('%m/%d')}・{n_videos}本を巡回）",
             "", (summary or "")[:300]]
    if consensus:
        lines += ["", f"🧭 {consensus[:120]}"]
    if themes:
        lines += [f"🔥 テーマ: {'、'.join(themes)}"]
    if stocks:
        lines += [f"👀 銘柄: {'、'.join(stocks)}"]
    lines += ["", f"▶ 詳細\n{_report_base_url()}/youtube"]
    return push_text("\n".join(lines), label="YouTube週報LINE")


# ═══════════════════════════════════════════════════════════
#  実行
# ═══════════════════════════════════════════════════════════

def run_weekly(max_analyze: int = MAX_ANALYZE, verbose: bool = True) -> dict:
    stats = {"videos_found": 0, "analyzed": 0, "failed": 0, "skipped": 0}
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY未設定のためスキップ")
        return stats
    conn = get_conn()
    cur = conn.cursor()
    ensure_tables(cur)
    conn.commit()
    client = _gemini()
    since = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    # 対象動画の収集
    targets = []
    for ch in CHANNELS:
        cid = _resolve_channel(cur, ch["handle"], ch["name"])
        conn.commit()
        if not cid:
            continue
        try:
            vids = _fetch_recent(cid)
        except Exception as e:  # noqa: BLE001
            print(f"  [{ch['handle']}] RSS失敗: {str(e)[:60]}")
            continue
        picked = 0
        for v in vids:
            if v["published"] < since or picked >= MAX_PER_CHANNEL:
                continue
            if SKIP_TITLE.search(v["title"]):
                stats["skipped"] += 1
                continue
            v["handle"], v["channel"] = ch["handle"], ch["name"]
            targets.append(v)
            picked += 1
        time.sleep(0.5)
    stats["videos_found"] = len(targets)
    if verbose:
        print(f"  対象動画: {len(targets)}本（直近{LOOKBACK_DAYS}日・ライブ等除外後）")

    # 未分析のみGeminiへ（新しい順・上限あり）
    targets.sort(key=lambda v: v["published"], reverse=True)
    n = 0
    for v in targets:
        cur.execute("SELECT status FROM youtube_videos WHERE video_id=%s", (v["video_id"],))
        row = cur.fetchone()
        if row and row[0] in ("analyzed", "failed"):
            continue
        if n >= max_analyze:
            break
        ok = analyze_video(cur, client, v)
        conn.commit()
        stats["analyzed" if ok else "failed"] += 1
        n += 1
        if verbose and ok:
            print(f"  ✓ [{v['channel']}] {v['title'][:40]}")
        time.sleep(20)   # 動画1本=数十万トークン。無料枠の分間制限(TPM)に配慮した間隔

    # 週次集約 → LINE通知（集約成功時のみ）
    if aggregate_weekly(cur, client, date.today()):
        conn.commit()
        try:
            notify_weekly(cur, date.today())
        except Exception as e:  # noqa: BLE001  通知失敗で本体を落とさない
            print(f"  [YouTube週報LINE] 送信失敗: {str(e)[:60]}")
    conn.commit()
    cur.close()
    conn.close()
    if verbose:
        print(f"完了: 発見{stats['videos_found']} 分析{stats['analyzed']}"
              f" 失敗{stats['failed']} 除外{stats['skipped']}")
    return stats


if __name__ == "__main__":
    limit = MAX_ANALYZE
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    run_weekly(max_analyze=limit)

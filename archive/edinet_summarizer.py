"""
edinet_text_blocks のテキストを Gemini で要約し、summary 列に保存する。

設計:
  1銘柄の全セクションを1コールでまとめて要約（Gemini コール = 銘柄数のみ）。
  Gemini 無料枠: 1500 req/日、15 RPM → 5秒インターバル遵守。

実行例:
  python3 edinet_summarizer.py               # 未要約銘柄から順に処理
  python3 edinet_summarizer.py 1911 7203     # 指定銘柄のみ
  python3 edinet_summarizer.py --force       # 未要約銘柄を強制再要約
  python3 edinet_summarizer.py --all --force # 全銘柄を再要約
"""

import os
import sys
import json
import re
import time
from dotenv import load_dotenv
from config import get_conn

load_dotenv()

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
MODEL        = "gemini-2.5-flash"
LIMIT        = 80       # 1日あたり処理上限（銘柄数）
RPM_INTERVAL = 5.0      # 15 RPM 以内 = 4秒間隔（余裕を持って5秒）
SECTION_CHARS = 1500    # セクションごとの入力文字数上限（Gemini コスト削減）
SUMMARY_LEN  = 200      # 要約の目安文字数


# ─── テーブル更新 ─────────────────────────────────────────────────────────────
def _ensure_column():
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute("ALTER TABLE edinet_text_blocks ADD COLUMN summary MEDIUMTEXT")
        conn.commit()
    except Exception:
        pass  # 既存カラムは無視
    cur.close()
    conn.close()


# ─── データ取得 ───────────────────────────────────────────────────────────────
def _fetch_targets(target_codes: list[str] | None, force: bool) -> list[tuple]:
    """(code, name) のリストを返す。"""
    conn = get_conn()
    cur  = conn.cursor()

    if target_codes:
        fmt = ",".join(["%s"] * len(target_codes))
        cur.execute(f"""
            SELECT DISTINCT e.code, COALESCE(s.name, e.code)
            FROM edinet_text_blocks e
            LEFT JOIN stocks s ON s.code = e.code
            WHERE e.code IN ({fmt})
            ORDER BY e.code
        """, target_codes)
    elif force:
        cur.execute("""
            SELECT DISTINCT e.code, COALESCE(s.name, e.code)
            FROM edinet_text_blocks e
            LEFT JOIN stocks s ON s.code = e.code
            ORDER BY e.code
        """)
    else:
        # summary が NULL のセクションを持つ銘柄（コード順）
        cur.execute("""
            SELECT DISTINCT e.code, COALESCE(s.name, e.code)
            FROM edinet_text_blocks e
            LEFT JOIN stocks s ON s.code = e.code
            WHERE e.summary IS NULL
            ORDER BY e.code
        """)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _fetch_sections(code: str) -> list[dict]:
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT section, text FROM edinet_text_blocks WHERE code = %s AND text IS NOT NULL ORDER BY section",
        (code,)
    )
    rows = [{"section": r[0], "text": r[1] or ""} for r in cur.fetchall() if r[1]]
    cur.close()
    conn.close()
    return rows


# ─── Gemini 呼び出し ──────────────────────────────────────────────────────────
def _build_prompt(code: str, name: str, sections: list[dict]) -> str:
    header = (
        f"{name}（証券コード: {code}）の有価証券報告書の各セクションを"
        f"{SUMMARY_LEN}文字以内の日本語で簡潔に要約してください。\n"
        "結果は以下の JSON 形式のみで返してください（前後の説明文は不要）:\n"
        '{"セクション名": "要約テキスト", ...}\n'
    )
    body_parts = []
    for s in sections:
        preview = s["text"][:SECTION_CHARS].replace("\n", " ")
        body_parts.append(f"## {s['section']}\n{preview}")
    return header + "\n" + "\n\n".join(body_parts)


def _call_gemini(prompt: str) -> dict | None:
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_KEY)
        resp   = client.models.generate_content(model=MODEL, contents=prompt)
        raw    = resp.text or ""

        # ```json ... ``` ブロックまたは裸の {...} を抽出
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
        json_str = m.group(1) if m else re.search(r"\{[\s\S]+\}", raw)
        if not json_str:
            return None
        if hasattr(json_str, "group"):
            json_str = json_str.group()
        return json.loads(json_str)
    except Exception as e:
        print(f"    [Gemini] {e}")
        return None


# ─── DB 保存 ──────────────────────────────────────────────────────────────────
def _save_summaries(code: str, summaries: dict) -> int:
    if not summaries:
        return 0
    conn = get_conn()
    cur  = conn.cursor()
    saved = 0
    for section, summary in summaries.items():
        if not isinstance(summary, str):
            continue
        cur.execute(
            "UPDATE edinet_text_blocks SET summary = %s WHERE code = %s AND section = %s",
            (summary.strip(), code, section)
        )
        if cur.rowcount:
            saved += 1
    conn.commit()
    cur.close()
    conn.close()
    return saved


# ─── 1銘柄処理 ────────────────────────────────────────────────────────────────
def summarize_one(code: str, name: str, force: bool = False) -> dict:
    if not force:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM edinet_text_blocks WHERE code = %s AND summary IS NULL",
            (code,)
        )
        missing = cur.fetchone()[0]
        cur.close()
        conn.close()
        if missing == 0:
            return {"code": code, "status": "skip"}

    sections = _fetch_sections(code)
    if not sections:
        return {"code": code, "status": "no_text"}

    prompt    = _build_prompt(code, name, sections)
    summaries = _call_gemini(prompt)
    if summaries is None:
        return {"code": code, "status": "gemini_error"}

    saved = _save_summaries(code, summaries)
    return {"code": code, "status": "ok", "saved": saved}


# ─── バッチ実行 ───────────────────────────────────────────────────────────────
def run(target_codes: list[str] | None = None, force: bool = False):
    if not GEMINI_KEY:
        print("ERROR: GEMINI_API_KEY が未設定です。")
        return

    _ensure_column()

    targets = _fetch_targets(target_codes, force)
    total   = len(targets)
    done = skip = err = 0

    print(f"対象: {total} 銘柄  上限: {LIMIT} 件/日")
    print()

    for i, (code, name) in enumerate(targets, 1):
        if done >= LIMIT:
            print(f"\n[!] 上限 {LIMIT} 件に到達。残り {total - i + 1} 銘柄は明日以降。")
            break

        result = summarize_one(code, name, force=force)

        if result["status"] == "ok":
            done += 1
            label = f"OK ({result.get('saved', 0)} セクション保存)"
            time.sleep(RPM_INTERVAL)
        elif result["status"] == "skip":
            skip += 1
            label = "SKIP（要約済み）"
        else:
            err += 1
            label = f"ERROR: {result['status']}"

        print(f"  [{i:>4}/{total}] {code} {name[:12]:12s}  {label}")

    print()
    print(f"完了: 要約OK={done}  スキップ={skip}  エラー={err}")


if __name__ == "__main__":
    args  = sys.argv[1:]
    force = "--force" in args
    all_  = "--all"   in args
    codes = [a for a in args if not a.startswith("--")] or None

    if all_ or codes:
        run(target_codes=codes, force=force)
    else:
        run(force=force)

"""
スイング候補を LINE に通知する

LINE Messaging API の push メッセージを使用する（LINE Notify は2025年3月廃止済み）。

【初回設定手順】
1. https://developers.line.biz にアクセスし「コンソール」にログイン
2. プロバイダーを作成（まだなければ）
3. 「Messaging API チャネル」を新規作成
4. 「チャネル設定」>「Messaging API」タブ > チャネルアクセストークン（長期）を発行
   → LINE_CHANNEL_ACCESS_TOKEN に設定
5. LINE アプリで Bot を友だち追加（チャネルページの QR コードから）
6. LINE アプリ > ホーム > 設定 > プロフィール > 「自分のプロフィール」内のユーザーID を確認
   → LINE_USER_ID に設定（U から始まる文字列）
7. render.yaml の daily-stock-update 環境変数に両キーを追加

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャネルアクセストークン
  LINE_USER_ID               通知先ユーザー ID（U から始まる）

単体実行: python3 swing_notifier.py
"""

import os
import json
import urllib.request
import urllib.error
from datetime import date
from swing_scorer import score_all, MIN_SCORE

LINE_PUSH_URL  = "https://api.line.me/v2/bot/message/push"
MAX_CANDIDATES = 10   # 1通のメッセージに載せる最大件数


def _format_message(candidates: list[dict]) -> str:
    today = date.today().strftime("%Y/%m/%d")
    if not candidates:
        return f"📊 スイング候補なし [{today}]\n本日は条件を満たす銘柄がありませんでした。"

    lines = [f"📈 スイング候補 [{today}]  {len(candidates)}銘柄", "─" * 28]

    for i, s in enumerate(candidates[:MAX_CANDIDATES], 1):
        f     = s["flags"]
        close = s.get("close") or 0
        name  = (s.get("name") or s["code"])[:10]
        rs    = s.get("rs") or 0
        rsi_v = s.get("rsi14") or 0
        dev_h = s.get("dev_high52w") or 0

        badges = [
            "Stage2✓" if f["stage2"] else "Stage2✗",
            f"RS✓{rs:.2f}" if f["rs"] else f"RS✗{rs:.2f}",
            "出来高✓" if f["volume"] else "出来高✗",
            f"RSI✓{rsi_v:.0f}" if f["rsi"] == "good" else
            f"RSI熱{rsi_v:.0f}" if f["rsi"] == "hot" else f"RSI{rsi_v:.0f}",
            f"高値圏✓{dev_h:.1f}%" if f["near_high"] else "高値圏✗",
        ]

        target = round(close * 1.06)
        stop   = round(close * 0.96)

        lines.append(
            f"{i}. {name}({s['code']}) ¥{close:,.0f}\n"
            f"   スコア:{s['score']} | {' '.join(badges)}\n"
            f"   目標:¥{target:,}(+6%) 損切:¥{stop:,}(-4%)"
        )

    if len(candidates) > MAX_CANDIDATES:
        lines.append(f"…他 {len(candidates) - MAX_CANDIDATES} 銘柄")

    return "\n".join(lines)


def send(candidates: list[dict] | None = None) -> bool:
    """LINE へ通知を送信する。candidates が None の場合はスコアリングから実行する。"""
    token   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()

    if not token or not user_id:
        print("  [LINE通知] LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が未設定のためスキップ")
        return False

    if candidates is None:
        candidates = score_all(min_score=MIN_SCORE)

    message = _format_message(candidates)
    payload = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
    }).encode("utf-8")

    req = urllib.request.Request(
        LINE_PUSH_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [LINE通知] 送信完了（{len(candidates)}銘柄）HTTP {resp.status}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [LINE通知] HTTP エラー {e.code}: {body}")
        return False
    except Exception as e:
        print(f"  [LINE通知] エラー: {e}")
        return False


if __name__ == "__main__":
    candidates = score_all()
    print(f"候補: {len(candidates)} 銘柄")
    print(_format_message(candidates))
    print()
    send(candidates)

"""
LINE Messaging API への汎用プッシュ送信ヘルパー。

LINE Notify は2025年3月に廃止されたため、Messaging API の push メッセージを使う。
チャネル作成・トークン発行・ユーザーID確認の手順は swing_notifier.py の docstring を参照。

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャネルアクセストークン（長期）
  LINE_USER_ID               通知先ユーザーID（U から始まる文字列）

未設定でも例外は出さず、送信をスキップして False を返す（ローカル/未設定環境でも安全）。
"""

import json
import os
import urllib.error
import urllib.request

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
MAX_TEXT_LEN = 5000   # LINE テキストメッセージの上限


def is_configured() -> bool:
    """LINE 送信に必要な環境変数が両方そろっているか。"""
    return bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
                and os.environ.get("LINE_USER_ID", "").strip())


def push_text(text: str, *, label: str = "LINE") -> bool:
    """LINE にテキストメッセージを1通送る。未設定ならスキップして False を返す。

    label は複数の通知種別（スイング/日次レポート等）をログで区別するためのタグ。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user_id:
        print(f"  [{label}] LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が未設定のためスキップ")
        return False

    payload = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": text[:MAX_TEXT_LEN]}],
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
            print(f"  [{label}] 送信完了 HTTP {resp.status}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [{label}] HTTP エラー {e.code}: {body}")
        return False
    except Exception as e:
        print(f"  [{label}] エラー: {e}")
        return False

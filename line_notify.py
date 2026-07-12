"""
LINE Messaging API への汎用プッシュ送信ヘルパー。

LINE Notify は2025年3月に廃止されたため、Messaging API の push メッセージを使う。
日次レポート完成通知（daily_report.notify_report_ready）の送信トランスポート。

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャネルアクセストークン（長期）
  LINE_USER_ID               通知先ユーザーID（U から始まる文字列）

未設定でも例外は出さず、送信をスキップして False を返す（ローカル/未設定環境でも安全）。

【初回設定手順】
1. https://developers.line.biz にアクセスし「コンソール」にログイン
2. プロバイダーを作成（まだなければ）
3. 「Messaging API チャネル」を新規作成
4. 「チャネル設定」>「Messaging API」タブ > チャネルアクセストークン（長期）を発行
   → LINE_CHANNEL_ACCESS_TOKEN に設定
5. LINE アプリで Bot を友だち追加（チャネルページの QR コードから）
6. LINE アプリ > ホーム > 設定 > プロフィール > 「自分のプロフィール」内のユーザーID を確認
   → LINE_USER_ID に設定（U から始まる文字列）
7. GitHub Secrets（daily.yml が参照）に両キー＋レポートURL(REPORT_BASE_URL)を登録
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

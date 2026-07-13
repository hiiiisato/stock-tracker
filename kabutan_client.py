"""
kabutan ページ取得の共通クライアント。

GitHub Actions のランナーIPは kabutan に HTTP 405 で遮断される（2026-07-09 実測）。
そのためまず直接取得を試み、遮断を検知したら以後は Render の内部プロキシ
（app.py /internal/kabutan）経由に自動フォールバックする。
ローカル・Render上では常に直接取得（プロキシ不使用）で従来と同じ動きになる。

利用側: company_profile.py / financials_kabutan.py（earnings_refresh経由含む）
"""
import hashlib
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

# BOT遮断を避けるため実ブラウザに近いヘッダ一式を送る（UAのみだと簡易ボット判定に掛かりやすい）
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://kabutan.jp/",
}
PROXY_BASE = os.environ.get("KABUTAN_PROXY_BASE", "https://stock-tracker-rfqn.onrender.com")
_BLOCKED_STATUSES = (403, 405, 429)

# 一度遮断を検知したらプロセス内では以後プロキシ直行（毎回2往復しない）
_use_proxy = False

# サーキットブレーカー: kabutanがデータセンターIP（GitHub Actions / Renderプロキシ）を
# 遮断し、直接もプロキシも取得不能になった場合、多数銘柄で各~185秒ハングして
# バッチ全体が60分タイムアウトで打ち切られる事態を防ぐ。
# 連続で完全失敗（status 0 または 403/405/429）が閾値に達したら、
# 以後プロセス内の取得を即座に (0, "") で諦める（後続の日次レポート等を巻き添えにしない）。
_consec_fails = 0
_MAX_CONSEC_FAILS = 5
_dead = False


def is_dead() -> bool:
    """サーキットブレーカーが作動済み（以後の取得を諦めている）か。"""
    return _dead


def _record(status: int) -> None:
    global _consec_fails, _dead
    if status == 0 or status in _BLOCKED_STATUSES:
        _consec_fails += 1
        if _consec_fails >= _MAX_CONSEC_FAILS and not _dead:
            _dead = True
            print(f"  [kabutan_client] 連続{_consec_fails}回の取得失敗によりサーキットブレーカー作動"
                  "（kabutanがこのIPを遮断中）。以後の取得は即座にスキップします")
    else:
        _consec_fails = 0


def _proxy_token() -> str | None:
    pw = os.environ.get("TIDB_PASSWORD", "")
    return hashlib.sha256(pw.encode()).hexdigest()[:32] if pw else None


def _via_proxy(path: str, timeout: int) -> tuple[int, str]:
    token = _proxy_token()
    if not token:
        return 0, ""
    from urllib.parse import quote
    url = f"{PROXY_BASE}/internal/kabutan?token={token}&path={quote(path, safe='')}"
    # Render無料プランはスリープからの復帰に~60秒かかることがある → 長めのタイムアウト+1回リトライ
    for attempt in (1, 2):
        try:
            r = requests.get(url, timeout=max(timeout, 90))
            if r.status_code == 502 and attempt == 1:
                time.sleep(5)
                continue
            return r.status_code, r.text
        except Exception as e:
            if attempt == 2:
                print(f"  [kabutan_client] プロキシ失敗: {e}")
                return 0, ""
            time.sleep(5)
    return 0, ""


def get(path: str, timeout: int = 10) -> tuple[int, str]:
    """kabutan.jp/{path} を取得して (status_code, text) を返す。失敗時は (0, "")。
    path例: "stock/?code=7203", "stock/finance?code=7203"

    サーキットブレーカー作動後は即座に (0, "") を返す（バッチのハング防止）。
    """
    global _use_proxy
    if _dead:
        return 0, ""
    status, text = 0, ""
    if not _use_proxy:
        try:
            r = requests.get(f"https://kabutan.jp/{path}", headers=UA, timeout=timeout)
            if r.status_code in _BLOCKED_STATUSES:
                print(f"  [kabutan_client] 直接取得が遮断されました(HTTP {r.status_code}) → 以後プロキシ経由")
                _use_proxy = True
                status, text = _via_proxy(path, timeout)
            else:
                status, text = r.status_code, r.text
        except Exception:
            # ネットワークエラーは遮断とは限らないため、その1回だけプロキシで救済
            status, text = _via_proxy(path, timeout)
    else:
        status, text = _via_proxy(path, timeout)
    _record(status)
    return status, text

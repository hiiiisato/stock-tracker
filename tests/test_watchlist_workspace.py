import hashlib
import os
import sqlite3
import unittest
from urllib.parse import parse_qs, urlsplit
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import app as stock_app
from watchlist_service import delete_memo, save_memo, validate_memo
from watchlist_workspace import adjusted_candles, load_memos


class SQLiteCursor:
    """検証専用の隔離DB。MySQLのロック/UPSERT構文だけSQLiteへ翻訳。"""
    def __init__(self, conn):
        self.cur = conn.cursor()

    def execute(self, sql, args=()):
        sql = sql.replace("%s", "?").replace(" FOR UPDATE", "").replace("INSERT IGNORE", "INSERT OR IGNORE")
        sql = sql.replace("ON DUPLICATE KEY UPDATE title = VALUES(title), source_url = VALUES(source_url)",
                          "ON CONFLICT(memo_id) DO UPDATE SET title=excluded.title, source_url=excluded.source_url")
        self.cur.execute(sql, args)

    def executemany(self, sql, args):
        self.cur.executemany(sql.replace("%s", "?"), args)

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    @property
    def lastrowid(self):
        return self.cur.lastrowid


class StockResearchRecordsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE stocks (code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1);
            INSERT INTO stocks (code) VALUES ('7203'), ('6758');
            CREATE TABLE watchlist (code TEXT PRIMARY KEY);
            CREATE TABLE watchlist_lists (id INTEGER PRIMARY KEY);
            INSERT INTO watchlist_lists VALUES (1);
            CREATE TABLE watchlist_list_items (list_id INTEGER, code TEXT, PRIMARY KEY (list_id, code));
            CREATE TABLE stock_memos (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT,
                content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE stock_memo_sources (memo_id INTEGER PRIMARY KEY, title TEXT, source_url TEXT);
        """)
        @contextmanager
        def db():
            with self.conn:
                yield SQLiteCursor(self.conn)
        self.patches = [patch("watchlist_service.ensure_schema"), patch("watchlist_service.db", db), patch("app.db", db),
                        patch("watchlist_workspace.db", db), patch.dict(os.environ, {"WATCHLIST_PASSCODE": "test-pass"})]
        for p in self.patches:
            p.start()
        self.client = stock_app.app.test_client()
        token = stock_app._watchlist_token()
        self.client.set_cookie("wl_auth", token)
        self.csrf = hashlib.sha256(("wl-csrf:" + token).encode()).hexdigest()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.conn.close()

    def test_three_posts_accumulate_with_independent_urls_and_full_text(self):
        for i in range(3):
            response = self.client.post("/memo/add", headers={"Accept": "application/json"}, data={
                "code": "7203", "csrf": self.csrf, "content": (f"考察{i}\n" * 1000),
                "title": f"投稿{i}", "source_url": f"https://x.com/example/status/{i}"})
            self.assertEqual(response.status_code, 200)
        rows = self.client.get("/api/watchlist/memos/7203").json
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["source_url"] for r in rows}), 3)
        self.assertTrue(all(len(r["content"]) > 2000 and r["kind"] == "x" for r in rows))
        self.assertEqual(self.client.get("/api/watchlist/memos/7203").headers["Cache-Control"], "no-store")

    def test_new_stock_can_save_url_and_memo_together(self):
        response = self.client.post("/watchlist/add", headers={"Accept": "application/json"}, data={
            "code": "7203", "csrf": self.csrf, "memo": "投稿の考察\n最後まで保存", "title": "成長への期待",
            "source_url": "https://x.com/example/status/123", "memberships_present": "1", "list_ids": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM watchlist WHERE code='7203'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT list_id FROM watchlist_list_items WHERE code='7203'").fetchone()[0], 1)
        rows = load_memos("7203")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_url"], "https://x.com/example/status/123")
        self.assertEqual(rows[0]["title"], "成長への期待")
        self.assertEqual(rows[0]["content"], "投稿の考察\n最後まで保存")

    def test_first_registration_continues_after_inline_login(self):
        client = stock_app.app.test_client()
        values = {"code": "7203", "memo": "入力した考察", "source_url": "https://x.com/example/status/1"}
        response = client.post("/watchlist/add", headers={"Accept": "application/json"}, data=values)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(load_memos("7203"), [])
        login = client.post("/watchlist/login", headers={"Accept": "application/json"}, data={"passcode": "test-pass"})
        self.assertEqual(login.status_code, 200)
        response = client.post("/watchlist/add", headers={"Accept": "application/json"}, data={**values, "csrf": login.json["csrf"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(load_memos("7203")[0]["source_url"], values["source_url"])

    def test_new_stock_accepts_url_only_and_no_information(self):
        for code, url in (("7203", "https://example.com/ir"), ("6758", "")):
            response = self.client.post("/watchlist/add", headers={"Accept": "application/json"}, data={
                "code": code, "csrf": self.csrf, "source_url": url})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(load_memos("7203")[0]["source_url"], "https://example.com/ir")
        self.assertEqual(load_memos("6758"), [])

    def test_failed_first_source_rolls_back_stock_and_memo(self):
        self.conn.execute("DROP TABLE stock_memo_sources")
        response = self.client.post("/watchlist/add", headers={"Accept": "application/json"}, data={
            "code": "7203", "csrf": self.csrf, "memo": "全体を取り消す", "source_url": "https://example.com"})
        self.assertEqual(response.status_code, 503)
        for table in ("watchlist", "stock_memos", "watchlist_list_items"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_new_stock_rejects_invalid_url_without_saving(self):
        response = self.client.post("/watchlist/add", headers={"Accept": "application/json"}, data={
            "code": "7203", "csrf": self.csrf, "memo": "保存しない", "source_url": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0], 0)

    def test_url_only_and_legacy_notes_share_the_same_history_without_twenty_item_limit(self):
        self.conn.executemany("INSERT INTO stock_memos (code,content) VALUES ('7203', ?)", [(f"旧メモ{i}",) for i in range(25)])
        self.conn.commit()
        save_memo("7203", "", "決算資料", "https://example.com/ir.pdf")
        rows = load_memos("7203")
        self.assertEqual(len(rows), 26)
        self.assertEqual(rows[0]["source_url"], "https://example.com/ir.pdf")
        self.assertEqual(rows[-1]["kind"], "memo")

    def test_edit_keeps_other_posts_and_cannot_modify_a_different_stock(self):
        first = save_memo("7203", "first", source_url="https://x.com/a/status/1")
        second = save_memo("7203", "second", source_url="https://x.com/a/status/2")
        save_memo("7203", "edited", "変更", "https://example.com", first)
        self.assertEqual({r["id"]: r["content"] for r in load_memos("7203")}, {first: "edited", second: "second"})
        with self.assertRaises(ValueError):
            save_memo("6758", "wrong stock", memo_id=first)
        with self.assertRaises(ValueError):
            delete_memo("6758", first)
        delete_memo("7203", first)
        self.assertEqual([r["id"] for r in load_memos("7203")], [second])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stock_memo_sources WHERE memo_id=?", (first,)).fetchone()[0], 0)

    def test_source_write_failure_rolls_back_note_creation(self):
        self.conn.execute("DROP TABLE stock_memo_sources")
        with self.assertRaises(sqlite3.OperationalError):
            save_memo("7203", "must roll back", source_url="https://example.com")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stock_memos").fetchone()[0], 0)

    def test_invalid_url_and_oversize_content_are_rejected_without_truncation(self):
        for url in ("javascript:alert(1)", "data:text/html,evil", "https://", "https://user:pass@example.com", "https://a.com\\evil", "https://a.com:bad", "https://a.com/\nfoo"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_memo("memo", source_url=url)
        response = self.client.post("/memo/add", headers={"Accept": "application/json"}, data={
            "code": "7203", "csrf": self.csrf, "content": "x" * 20001})
        self.assertEqual(response.status_code, 400)
        self.assertIn("20,000", response.json["error"])
        self.assertEqual(load_memos("7203"), [])
        with self.assertRaises(ValueError):
            validate_memo("😀" * 20000)

    def test_memo_writes_still_require_authentication_and_csrf(self):
        self.client.delete_cookie("wl_auth")
        self.assertEqual(self.client.post("/memo/add", data={"code": "7203", "content": "no"}).status_code, 302)
        self.client.set_cookie("wl_auth", stock_app._watchlist_token())
        self.assertEqual(self.client.post("/memo/add", data={"code": "7203", "content": "no"}).status_code, 403)

    def test_html_error_retains_input_and_escapes_markup(self):
        response = self.client.post("/memo/add", data={"code": "7203", "csrf": self.csrf,
            "content": '<script>alert("x")</script>', "source_url": "javascript:bad"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn('<script>alert("x")</script>', response.text)

    def test_alert_validation_failure_returns_an_actionable_error(self):
        with patch("watchlist_service.set_price_alerts", side_effect=ValueError("下値は上値より小さく設定してください")):
            response = self.client.post("/watchlist/alert", headers={"Accept": "application/json"},
                data={"code": "7203", "upper": "1000", "lower": "2000", "csrf": self.csrf})
        self.assertEqual(response.status_code, 400)
        self.assertIn("下値", response.json["error"])

    def test_form_error_keeps_list_and_fragment_in_the_return_url(self):
        for target in ("/watchlist", "/watchlist?list=3#wl-7203"):
            with self.subTest(target=target), patch("watchlist_service.set_price_alerts", side_effect=ValueError("価格を確認してください")):
                response = self.client.post("/watchlist/alert", data={"code": "7203", "csrf": self.csrf, "next": target})
            redirect_to = urlsplit(response.headers["Location"])
            self.assertEqual(redirect_to.path, "/watchlist")
            self.assertIn("msg", parse_qs(redirect_to.query))
            self.assertEqual(redirect_to.fragment, urlsplit(target).fragment)

    def test_workspace_template_escapes_saved_names_and_script_payloads(self):
        payload = {"lists": [{"id": 1, "name": '<script>alert("list")</script>'}],
                   "stocks": [{"code": "7203", "name": "</script><script>alert('stock')</script>"}]}
        with patch("watchlist_workspace.load_workspace", return_value=payload):
            response = self.client.get("/watchlist")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script>alert("list")</script>', response.text)
        self.assertNotIn("</script><script>alert('stock')</script>", response.text)


class WatchlistCandlesTest(unittest.TestCase):
    def test_ohlc_uses_existing_split_adjustment_and_volume_keeps_actual_share_count(self):
        rows = [("2026-01-05", Decimal("2000"), Decimal("2200"), Decimal("1800"), Decimal("2100"), Decimal("1050"), 1234),
                ("2026-01-06", None, None, None, Decimal("1050"), None, None),
                ("2026-01-07", None, None, None, None, None, None)]
        candles = adjusted_candles(rows)
        self.assertEqual(candles[0], {"date": "2026-01-05", "open": 1000., "high": 1100., "low": 900., "close": 1050., "volume": 1234})
        self.assertEqual(candles[1]["open"], None)
        self.assertEqual(len(candles), 2)


if __name__ == "__main__":
    unittest.main()

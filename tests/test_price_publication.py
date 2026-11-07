"""隔離DBで公開・ロールバック・参照経路を通し、取得層の継続処理も検証する。"""
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal as D
import re
import sqlite3
import unittest
from unittest.mock import Mock, patch

import app
import price_store as store
import price_verification as verification
from price_pipeline import _with_official_values
from corporate_actions import parse_monthly_pdf, parse_disclosure_ratio
from price_adjustments import Action, PriceBasisError, StalePublication, normalize


sqlite3.register_adapter(D, str)
sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_adapter(datetime, lambda d: d.isoformat(" "))
sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()))
sqlite3.register_converter("DECIMAL", lambda b: D(b.decode()))
sqlite3.register_converter("DATETIME", lambda b: datetime.fromisoformat(b.decode()))


class Cursor:
    def __init__(self, connection):
        self.cur = connection.cursor()

    def execute(self, query, args=()):
        query = query.replace("%s", "?").replace(" FOR UPDATE", "").replace("INSERT IGNORE", "INSERT OR IGNORE")
        query = re.sub(r"ON DUPLICATE KEY UPDATE (.*)", lambda m: "ON CONFLICT DO UPDATE SET " + re.sub(r"VALUES\(`?(\w+)`?\)", r"excluded.\1", m[1]), query, flags=re.S)
        self.cur.execute(query, args)

    def fetchone(self): return self.cur.fetchone()
    def fetchall(self): return self.cur.fetchall()


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.conn.executescript("""
          CREATE TABLE daily_prices (id INTEGER PRIMARY KEY, code TEXT, date DATE, open DECIMAL, high DECIMAL,
            low DECIMAL, close DECIMAL, volume INT, turnover INT, adj_open DECIMAL, adj_high DECIMAL,
            adj_low DECIMAL, adj_close DECIMAL, adj_volume DECIMAL, adj_factor DECIMAL, change_pct DECIMAL,
            price_basis TEXT, price_version TEXT, UNIQUE(code,date));
          CREATE TABLE corporate_actions (code TEXT,ex_date DATE,old_shares DECIMAL,new_shares DECIMAL,
            kind TEXT,status TEXT,source TEXT,source_url TEXT,record_date DATE,effective_date DATE,price_ratio DECIMAL);
          CREATE TABLE price_adjustment_state (code TEXT PRIMARY KEY,status TEXT,action_digest TEXT,
            published_version TEXT,price_date DATE,basis TEXT,error TEXT,updated_at DATETIME,source_observed_at DATETIME);
          CREATE TABLE price_official_checks (code TEXT,date DATE,open DECIMAL,high DECIMAL,low DECIMAL,close DECIMAL,volume INT);
          CREATE TABLE price_sync_state (name TEXT PRIMARY KEY,value TEXT);
          CREATE VIEW price_history AS SELECT p.id,p.code,p.date,p.adj_open AS open,p.adj_high AS high,
            p.adj_low AS low,p.adj_close AS close,p.adj_volume AS volume FROM daily_prices p
            JOIN price_adjustment_state s ON s.code=p.code WHERE s.status='published';
          INSERT INTO corporate_actions (code,ex_date,old_shares,new_shares,kind,status,source,source_url)
            VALUES ('285A','2026-09-29',1,3,'split','confirmed','jpx_monthly','https://www.jpx.co.jp/');
        """)
        @contextmanager
        def db():
            with self.conn:
                yield Cursor(self.conn)
        self.db = db
        self.patches = [patch.object(store, "db", db), patch.object(store, "publication_generation", return_value="test")]
        for p in self.patches: p.start()
        self.action = Action("285A", date(2026,9,29), D(1), D(3))
        self.basis = date(2026,10,30)
        source = [dict(code="285A",date="2026-09-28",open=6000,high=6300,low=5700,close=6000,volume=100),
                  dict(code="285A",date="2026-09-29",open=2000,high=2200,low=1900,close=2100,volume=330)]
        self.rows = normalize(source,[self.action],self.basis,"raw")

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        app._cache.clear()
        self.conn.close()

    def publish(self, sid="original", observed=datetime(2026,10,30,8)):
        with self.db() as cur:
            store.publish(cur,"285A",self.rows,[self.action],self.basis,"raw",sid,observed)

    def test_shared_chart_api_publishes_same_adjusted_ohlcv_and_replay_is_idempotent(self):
        self.publish()
        first = store.load_candles(["285A"],date(2026,9,1))
        self.publish()
        self.assertEqual(first,store.load_candles(["285A"],date(2026,9,1)))
        self.assertEqual(first["285A"][0],dict(date="2026-09-28",open=2000.,high=2100.,low=1900.,close=2000.,volume=300.))
        with patch("watchlist_workspace.jst_today",return_value=self.basis):
            response=app.app.test_client().get("/api/watchlist/chart/285A")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json,first["285A"])
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    def test_failure_after_price_write_rolls_back_price_and_publication_together(self):
        self.publish()
        original = store.bulk_upsert
        def fail(cur,table,*args,**kwargs):
            if table == "price_adjustment_state": raise RuntimeError("interrupted")
            return original(cur,table,*args,**kwargs)
        self.rows[0]["adj_close"]=D(1)
        with patch.object(store,"bulk_upsert",side_effect=fail), self.assertRaises(RuntimeError):
            self.publish("broken")
        self.assertEqual(store.load_candles(["285A"],date(2026,9,1))["285A"][0]["close"],2000.)

    def test_stale_result_cannot_overwrite_newer_snapshot_and_hold_hides_chart(self):
        self.publish()
        with self.assertRaises(StalePublication): self.publish("late",datetime(2026,10,29,8))
        store.hold("285A","official mismatch")
        self.assertEqual(store.load_candles(["285A"],date(2026,9,1)),{})
        self.assertEqual(app.app.test_client().get("/api/watchlist/chart/285A").status_code,503)

    def test_action_change_during_fetch_rejects_old_series(self):
        self.conn.execute("UPDATE corporate_actions SET new_shares=5"); self.conn.commit()
        with self.assertRaises(PriceBasisError): self.publish()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0],0)

    def test_official_ohlcv_replaces_bad_provider_values_after_basis_is_confirmed(self):
        self.conn.execute("INSERT INTO price_official_checks VALUES ('285A','2026-09-28',6000,6300,5700,6000,100)")
        self.conn.commit()
        source=[dict(code="285A",date="2026-09-28",open=2000,high=10,low=1900,close=2000,volume=999)]
        with patch("price_pipeline.db",self.db):
            candidate=_with_official_values("285A",source,[self.action],self.basis,"split_adjusted","raw")
        self.assertEqual(source[0]["high"],10)  # 取得原本は加工しない。
        result=normalize(candidate,[self.action],self.basis,"split_adjusted",volume_basis="raw")
        self.assertEqual(result[0]["high"],D(6300))
        self.assertEqual(result[0]["adj_high"],D(2100))
        self.assertEqual(result[0]["adj_volume"],D(300))


class OfficialFetchTest(unittest.TestCase):
    def test_expired_free_window_dates_do_not_block_the_new_verification_queue(self):
        cur=Mock()
        cur.fetchall.side_effect=[[(date(2026,8,13),),(date(2026,8,14),)],
                                   [("jquants_date:2024-01-01",)],[("jquants_date:2024-02-01",)]]
        @contextmanager
        def db(): yield cur
        def audited(target): return {"date":str(target),"checked":1,"mismatches":[]}
        with patch.object(verification,"db",db), patch.object(verification,"ensure_schema"), \
             patch.object(verification,"jst_today",return_value=date(2026,11,7)), \
             patch.object(verification,"audit_date",side_effect=audited) as audit:
            verification.run_audit()
        self.assertEqual([c.args[0] for c in audit.call_args_list],[date(2026,8,14),date(2026,8,13)])

    def test_pagination_uses_documented_from_to_and_preserves_all_pages(self):
        replies = [Mock(status_code=200),Mock(status_code=200)]
        replies[0].json.return_value={"data":[{"Date":"2026-01-05"}],"pagination_key":"page2"}
        replies[1].json.return_value={"data":[{"Date":"2026-01-06"}]}
        with patch.object(verification.requests,"get",side_effect=replies) as get, patch.object(verification,"reserve_request_slot"):
            result=verification.fetch_bars(code="285A",since=date(2026,1,1),until=date(2026,1,31))
        self.assertEqual(len(result),2)
        params=get.call_args.kwargs["params"]
        self.assertEqual(params,{"code":"285A","from":"2026-01-01","to":"2026-01-31","pagination_key":"page2"})

    def test_empty_official_day_is_not_recorded_as_verified(self):
        with patch.object(verification,"fetch_bars",return_value=[]), patch.object(verification,"remember_bars") as remember:
            with self.assertRaises(RuntimeError):verification.audit_date(date(2025,1,6))
        remember.assert_not_called()

    def test_jpx_etf_row_and_multi_issue_disclosure_select_the_right_code(self):
        line="ETF 1306 NEXT FUNDS TOPIX Exchange Traded Fund 2026.03.30 2026.03.31 1:10 受益権分割"
        pdf=Mock();pdf.pages=[Mock()];pdf.pages[0].extract_text.return_value=line
        with patch("pdfplumber.open") as open_pdf:
            open_pdf.return_value.__enter__.return_value=pdf
            result=parse_monthly_pdf(b"fixture","https://www.jpx.co.jp/")
        self.assertEqual(result[0].price_factor,D("0.1"))
        action=Action("1546",date(2026,11,4),D(1),D(1),status="needs_ratio",record_date=date(2026,11,5))
        result=parse_disclosure_ratio("2026年11月5日 （1328）1口を50口に分割 （1546）1口を400口に分割",action)
        self.assertEqual(result.after,D(400))


if __name__ == "__main__": unittest.main()

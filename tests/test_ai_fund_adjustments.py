from datetime import date
from decimal import Decimal as D
import unittest

import test_price_publication as fixtures
from ai_fund_adjustments import apply_actions, adjusted_units
from price_adjustments import Action, PriceBasisError


class FundUnitsTest(unittest.TestCase):
    def setUp(self):
        fixtures.PublicationTest.setUp(self)
        self.conn.executescript("""
          CREATE TABLE ai_fund_state (id INT PRIMARY KEY,cash DECIMAL);
          INSERT INTO ai_fund_state VALUES (1,1000000);
          CREATE TABLE ai_fund_positions (code TEXT PRIMARY KEY,shares INT,avg_cost DECIMAL,buy_date DATE);
          INSERT INTO ai_fund_positions VALUES ('285A',100,6000,'2026-09-28');
          CREATE TABLE ai_fund_orders (id INT PRIMARY KEY,code TEXT,shares INT,limit_price DECIMAL,decided_date DATE,status TEXT);
          INSERT INTO ai_fund_orders VALUES (1,'285A',100,6150,'2026-09-28','pending');
          CREATE TABLE ai_fund_action_applications (object_key TEXT,ex_date DATE,revision TEXT,details TEXT,PRIMARY KEY(object_key,ex_date));
          INSERT INTO price_adjustment_state (code,status) VALUES ('285A','published');
        """)

    def tearDown(self): fixtures.PublicationTest.tearDown(self)

    def test_split_keeps_book_value_cash_and_pending_limit_then_does_not_apply_twice(self):
        with self.db() as cur:
            self.assertEqual(apply_actions(cur,date(2026,9,29)),2)
        qty,cost=self.conn.execute("SELECT shares,avg_cost FROM ai_fund_positions").fetchone()
        self.assertEqual((qty,cost),(300,D(2000)))
        self.assertEqual(qty*cost,D(600000))
        self.assertEqual(self.conn.execute("SELECT cash FROM ai_fund_state").fetchone()[0],D(1000000))
        self.assertEqual(self.conn.execute("SELECT shares,limit_price FROM ai_fund_orders").fetchone(),(300,D(2050)))
        with self.db() as cur:
            self.assertEqual(apply_actions(cur,date(2026,9,30)),0)
        self.assertEqual(self.conn.execute("SELECT shares FROM ai_fund_positions").fetchone()[0],300)

    def test_unpublished_prices_block_units_and_later_cancellation_is_not_silently_reversed(self):
        self.conn.execute("UPDATE price_adjustment_state SET status='held'"); self.conn.commit()
        with self.assertRaises(PriceBasisError), self.db() as cur:
            apply_actions(cur,date(2026,9,29))
        self.assertEqual(self.conn.execute("SELECT shares FROM ai_fund_positions").fetchone()[0],100)
        self.conn.execute("UPDATE price_adjustment_state SET status='published'"); self.conn.commit()
        with self.db() as cur: apply_actions(cur,date(2026,9,29))
        self.conn.execute("UPDATE corporate_actions SET status='cancelled'"); self.conn.commit()
        with self.assertRaises(PriceBasisError), self.db() as cur:
            apply_actions(cur,date(2026,9,30))

    def test_ex_date_purchase_and_future_split_are_not_adjusted_and_fractional_shares_need_review(self):
        with self.db() as cur: self.assertEqual(apply_actions(cur,date(2026,9,28)),0)
        self.conn.execute("UPDATE ai_fund_positions SET buy_date='2026-09-29'"); self.conn.commit()
        with self.db() as cur: apply_actions(cur,date(2026,9,29))
        self.assertEqual(self.conn.execute("SELECT shares FROM ai_fund_positions").fetchone()[0],100)
        consolidation=Action("285A",date(2026,9,29),D(10),D(1),"consolidation")
        with self.assertRaises(PriceBasisError): adjusted_units(101,D(6000),consolidation)

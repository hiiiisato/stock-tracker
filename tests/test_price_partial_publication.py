import unittest
from unittest.mock import patch

import daily_run
from price_store import PricePublicationHeld


class PartialPublicationTest(unittest.TestCase):
    def test_quarantined_unwatched_symbol_does_not_stop_other_reports(self):
        error=PricePublicationHeld(40000,{"1234":"公式基準が不足"})
        with patch.object(daily_run,"watched_codes",return_value=set()),patch.object(daily_run,"_log") as log:
            self.assertEqual(daily_run._record_price_hold(error),[])
        self.assertEqual(log.call_args.args[:3],("prices","partial",40000))

    def test_watched_symbol_or_no_publication_keeps_the_report_incomplete(self):
        for count,watched in ((40000,{"285A"}),(0,set())):
            with self.subTest(count=count),patch.object(daily_run,"watched_codes",return_value=watched),patch.object(daily_run,"_log"):
                self.assertTrue(daily_run._record_price_hold(PricePublicationHeld(count,{"285A":"公式基準が不足"})))

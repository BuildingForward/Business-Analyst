"""Sources: parsing the messy shapes real listing data arrives in."""

import json
import tempfile
import unittest
from pathlib import Path

from business_analyst.models import SellerFinancing
from business_analyst.sources import CsvSource, JsonSource, row_to_listing
from business_analyst.sources.base import (
    SourceRegistry,
    parse_financing,
    parse_int,
    parse_money,
    parse_pct,
)

CSV_BODY = """Business Name,Industry,Asking Price,Cash Flow,Established,Seller Financing
Acme Cleaning,commercial cleaning,"$450,000",185000,2005,Yes
Beta HVAC,hvac,1.2M,410k,1998,Will consider
Gamma Cafe,restaurant,n/a,,2019,No
"""


class TestParsers(unittest.TestCase):
    def test_money_formats(self):
        self.assertEqual(parse_money("$1.2M"), 1_200_000)
        self.assertEqual(parse_money("450,000"), 450_000)
        self.assertEqual(parse_money("250k"), 250_000)
        self.assertEqual(parse_money("$3.5MM"), 3_500_000)
        self.assertEqual(parse_money(185000), 185_000)

    def test_money_absences(self):
        for blank in (None, "", "n/a", "undisclosed", "-"):
            self.assertIsNone(parse_money(blank), f"{blank!r} should parse as unknown")

    def test_money_ignores_unparseable(self):
        self.assertIsNone(parse_money("call for details"))

    def test_int_parsing(self):
        self.assertEqual(parse_int("2005"), 2005)
        self.assertIsNone(parse_int(""))

    def test_pct_formats(self):
        for value in ("70%", 0.7, "0.7", 70):
            self.assertAlmostEqual(parse_pct(value), 0.7, places=6)
        self.assertIsNone(parse_pct(None))
        self.assertIsNone(parse_pct("lots"))

    def test_pct_is_clamped(self):
        self.assertEqual(parse_pct("150%"), 1.0)

    def test_financing_words(self):
        self.assertEqual(parse_financing("Yes"), SellerFinancing.OFFERED)
        self.assertEqual(parse_financing("cash only"), SellerFinancing.REFUSED)
        self.assertEqual(parse_financing("Will consider"), SellerFinancing.NEGOTIABLE)
        self.assertEqual(parse_financing(None), SellerFinancing.UNKNOWN)


class TestRowMapping(unittest.TestCase):
    def test_flexible_column_names(self):
        row = {"Title": "Acme", "SDE": "$120,000", "Ask": "400k", "Year Established": "2010"}
        l = row_to_listing(row, "test")
        self.assertEqual(l.name, "Acme")
        self.assertEqual(l.sde, 120_000)
        self.assertEqual(l.asking_price, 400_000)
        self.assertEqual(l.established_year, 2010)

    def test_missing_name_gets_a_placeholder(self):
        self.assertIn("unnamed", row_to_listing({"Ask": "100k"}, "test", 3).name)

    def test_real_estate_flag(self):
        self.assertTrue(row_to_listing({"name": "x", "real estate": "Yes"}, "t").real_estate_included)
        self.assertFalse(row_to_listing({"name": "x", "real estate": "no"}, "t").real_estate_included)

    def test_raw_is_preserved(self):
        l = row_to_listing({"Name": "Acme", "Weird Field": "keep me"}, "t")
        self.assertEqual(l.raw.get("weird field"), "keep me")


class TestFileSources(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_csv_source(self):
        csv_path = self.path / "listings.csv"
        csv_path.write_text(CSV_BODY)
        listings = list(CsvSource(csv_path).fetch())
        self.assertEqual(len(listings), 3)
        self.assertEqual(listings[0].asking_price, 450_000)
        self.assertEqual(listings[1].cash_flow, 410_000)
        self.assertEqual(listings[1].seller_financing, SellerFinancing.NEGOTIABLE)
        self.assertIsNone(listings[2].asking_price)

    def test_csv_respects_limit(self):
        csv_path = self.path / "listings.csv"
        csv_path.write_text(CSV_BODY)
        self.assertEqual(len(list(CsvSource(csv_path).fetch(limit=2))), 2)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            list(CsvSource(self.path / "nope.csv").fetch())

    def test_json_source_array_and_object(self):
        rows = [{"name": "A", "ask": "100k"}, {"name": "B", "ask": "200k"}]
        flat = self.path / "a.json"
        flat.write_text(json.dumps(rows))
        self.assertEqual(len(list(JsonSource(flat).fetch())), 2)

        wrapped = self.path / "b.json"
        wrapped.write_text(json.dumps({"listings": rows}))
        self.assertEqual(len(list(JsonSource(wrapped).fetch())), 2)


class TestRegistry(unittest.TestCase):
    def test_a_failing_source_does_not_stop_the_others(self):
        class Boom:
            name = "boom"

            def fetch(self, limit=50):
                raise RuntimeError("upstream is down")

        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "listings.csv"
            csv_path.write_text(CSV_BODY)
            registry = SourceRegistry()
            registry.register(Boom())
            registry.register(CsvSource(csv_path, name="good"))
            with self.assertLogs("business_analyst.sources.base", level="ERROR"):
                listings = registry.fetch()
        self.assertEqual(len(listings), 3, "the healthy source still delivers")
        self.assertEqual(registry.names(), ["boom", "good"])


if __name__ == "__main__":
    unittest.main()

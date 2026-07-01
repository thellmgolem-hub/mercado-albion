# -*- coding: utf-8 -*-
"""Testes do consultor de flips por orçamento (albion/advisor.py)."""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from albion import advisor


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class _FakeDB:
    """ItemDB mínimo: só get() (metadados) — o universo é passado explícito."""

    def __init__(self, metas):
        self._m = metas

    def get(self, iid):
        return self._m.get(iid)

    def filter(self, **kw):
        return []


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            """
            CREATE TABLE prices (
              server TEXT, item_id TEXT, city TEXT, quality INTEGER,
              sell_price_min INTEGER, sell_price_min_date TEXT,
              sell_price_max INTEGER, sell_price_max_date TEXT,
              buy_price_min INTEGER, buy_price_min_date TEXT,
              buy_price_max INTEGER, buy_price_max_date TEXT, fetched_at REAL);
            CREATE TABLE history (
              server TEXT, item_id TEXT, city TEXT, quality INTEGER,
              time_scale INTEGER, ts TEXT, item_count INTEGER,
              avg_price REAL, fetched_at REAL);
            """
        )
        now = datetime.now(timezone.utc)
        niso, ts = _iso(now), now.timestamp()
        # comprar (instant) em Fort Sterling a 10000; vender (ordem) em Martlock
        # a 20000 -> receita 20000*(1-0.04-0.025)=18700, lucro 8700/un.
        self.con.execute(
            "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["Americas", "T5_BAG", "Fort Sterling", 1, 10000, niso,
             0, None, 0, None, 9000, niso, ts])
        self.con.execute(
            "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["Americas", "T5_BAG", "Martlock", 1, 20000, niso,
             0, None, 0, None, 15000, niso, ts])
        # liquidez: 5 dias com 100/dia em Fort Sterling -> mediana 100 -> cap 20
        for d in range(1, 6):
            day = _iso((now - timedelta(days=d)).replace(hour=12))
            self.con.execute(
                "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)",
                ["Americas", "T5_BAG", "Fort Sterling", 1, 24, day, 100,
                 10000.0, ts])
        self.con.commit()
        self.db = _FakeDB({"T5_BAG": {"pt": "Bolsa", "en": "Bag", "tier": 5,
                                      "ench": 0, "cat": "bags", "w": 1.5}})
        self.flip = {"T5_BAG"}

    def tearDown(self):
        self.con.close()

    def test_liquidity_caps_units(self):
        res = advisor.advise(self.con, "Americas", self.db, budget=10_000_000,
                             city="Fort Sterling", flipable=self.flip)
        self.assertEqual(len(res["shopping_list"]), 1)
        line = res["shopping_list"][0]
        self.assertEqual(line["buy_city"], "Fort Sterling")
        self.assertEqual(line["sell_city"], "Martlock")
        self.assertEqual(line["units"], 20)                 # 100 * 0.20
        self.assertEqual(line["units_cap_reason"], "liquidity")
        self.assertEqual(line["unit_cost"], 10000)          # compra instant: sem taxa
        self.assertAlmostEqual(line["unit_profit"], 8700, delta=1)  # taxas do motor
        self.assertLessEqual(res["summary"]["orcamento_usado"], 10_000_000)
        self.assertTrue(res["summary"]["anti_isca_aplicada"])

    def test_budget_caps_units(self):
        # orçamento compra só 5 unidades (custo 10000) -> teto por orçamento
        res = advisor.advise(self.con, "Americas", self.db, budget=55_000,
                             city="Fort Sterling", flipable=self.flip)
        line = res["shopping_list"][0]
        self.assertEqual(line["units"], 5)
        self.assertEqual(line["units_cap_reason"], "budget")
        self.assertLessEqual(res["summary"]["orcamento_usado"], 55_000)

    def test_con_none_is_empty(self):
        res = advisor.advise(None, "Americas", self.db, budget=1000,
                             city="Martlock")
        self.assertEqual(res["shopping_list"], [])
        self.assertEqual(res["summary"]["linhas"], 0)

    def test_universe_restriction(self):
        # item fora do universo flipável não é considerado
        res = advisor.advise(self.con, "Americas", self.db, budget=10_000_000,
                             city="Fort Sterling", flipable=set())
        self.assertEqual(res["shopping_list"], [])

    def test_budget_too_small_no_line(self):
        # nem 1 unidade cabe (custo 10000)
        res = advisor.advise(self.con, "Americas", self.db, budget=5000,
                             city="Fort Sterling", flipable=self.flip)
        self.assertEqual(res["shopping_list"], [])

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            advisor.advise(self.con, "Americas", self.db, budget=1000,
                           city="Martlock", buy_mode="x-invalid")


if __name__ == "__main__":
    unittest.main()

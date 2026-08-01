# -*- coding: utf-8 -*-
"""Testes do Estúdio de Refino (albion/refining.py).

Cobrem as três coisas que o motor faz e que uma calculadora comum não faz:
o retorno de recursos criando operações EXTRAS (que gastam foco), o foco como
estoque que ACABA no meio do plano, e a cascata de tiers do coletor.
"""
import unittest

from albion import refining as rf


# preços de laboratório (madeira; Fort Sterling é a cidade-bônus da família)
PRICES = {
    "T5_WOOD": 400, "T4_WOOD": 200, "T3_WOOD": 100, "T2_WOOD": 60,
    "T5_PLANKS": 2000, "T4_PLANKS": 700, "T3_PLANKS": 300, "T2_PLANKS": 120,
}
CITY = "Fort Sterling"


def price_of(item_id, city):
    return PRICES.get(item_id)


class RefineBasicsTests(unittest.TestCase):
    def test_ids_and_bonus_city(self):
        self.assertEqual(rf.refined_id("wood", 5), "T5_PLANKS")
        self.assertEqual(rf.raw_id("ore", 4), "T4_ORE")
        self.assertEqual(rf.refined_id("ore", 4, 2), "T4_METALBAR_LEVEL2@2")
        self.assertEqual(rf.bonus_city("wood"), "Fort Sterling")
        self.assertEqual(rf.bonus_city("ore"), "Thetford")

    def test_station_fee_from_itemvalue(self):
        # T5_PLANKS: itemvalue 32 -> nutrição 3,6 -> a 500/100nutri = 18 prata
        self.assertAlmostEqual(rf.station_fee("T5_PLANKS", 500), 18.0, places=2)
        self.assertEqual(rf.station_fee("T5_PLANKS", 0), 0.0)
        # o itemvalue DOBRA por tier: T5=32 -> T8=256, taxa 8x maior
        self.assertAlmostEqual(rf.station_fee("T8_PLANKS", 500),
                               8 * rf.station_fee("T5_PLANKS", 500), places=2)

    def test_focus_cost_with_specialization(self):
        base = rf.focus_per_op("T5_PLANKS", 0)
        self.assertEqual(base, 94)                    # foco do dump
        # cada 10.000 de Focus Cost Efficiency corta o foco pela metade
        self.assertAlmostEqual(rf.focus_per_op("T5_PLANKS", 10000), 47.0, places=1)

    def test_unit_economics_matches_hand_math(self):
        u = rf.unit_economics("T5_PLANKS", CITY, price_of, focus=True, fee_per_100=500)
        # insumos: 3x T5_WOOD (400) + 1x T4_PLANKS (700) = 1900
        self.assertEqual(u["gross_cost"], 1900)
        self.assertAlmostEqual(u["rrr_pct"], 53.9, places=1)
        # custo efetivo = 1900 * (1 - 0,539) = 875,6
        self.assertAlmostEqual(u["eff_cost"], 875.6, delta=0.5)
        # venda: 2000 - 4% imposto - 2,5% anúncio = 1870
        self.assertAlmostEqual(u["sell_net"], 1870.0, delta=0.5)
        self.assertAlmostEqual(u["margin"], 1870 - 875.6 - 18, delta=1.0)


class ReturnCascadeTests(unittest.TestCase):
    """O retorno de recursos vira PRODUÇÃO EXTRA — e foco extra."""

    def test_stock_yields_geometric_series(self):
        # 100 troncos T5, 3 por operação, RRR 53,9% -> 100/(3*0,461) = 72,3
        u = rf.unit_economics("T5_PLANKS", CITY, price_of, focus=True)
        self.assertAlmostEqual(u["raw_per_unit"], 3 * (1 - 0.539), places=2)
        p = rf.plan("T5_PLANKS", CITY, price_of, focus_available=10 ** 9,
                    raw_stock=100, prev_stock=10 ** 9)
        self.assertEqual(p["total"]["qty"], 72)       # e não 33 (100/3)

    def test_focus_scales_with_extra_operations(self):
        """Foco cobra por OPERAÇÃO: o retorno aumenta as operações e o foco."""
        p = rf.plan("T5_PLANKS", CITY, price_of, focus_available=10 ** 9,
                    raw_stock=100, prev_stock=10 ** 9)
        # 72 operações x 94 de foco — não 33 x 94 (o erro comum)
        self.assertAlmostEqual(p["total"]["focus_used"], 72 * 94, delta=100)


class FocusCliffTests(unittest.TestCase):
    """O foco acaba no meio: é onde o jogador perde prata sem perceber."""

    def setUp(self):
        self._wood = PRICES["T5_WOOD"]

    def tearDown(self):
        PRICES["T5_WOOD"] = self._wood

    def test_plan_splits_in_two_phases(self):
        p = rf.plan("T5_PLANKS", CITY, price_of, focus_available=10000,
                    budget=5_000_000, fee_per_100=500)
        fases = [ph["phase"] for ph in p["phases"]]
        self.assertEqual(fases, ["com foco", "sem foco"])
        com = p["phases"][0]
        self.assertLessEqual(com["focus_used"], 10000)
        self.assertGreater(com["margin_unit"], p["phases"][1]["margin_unit"])

    def test_stops_when_no_focus_loses_money(self):
        PRICES["T5_WOOD"] = 900                  # bruto caro: sem foco dá prejuízo
        p = rf.plan("T5_PLANKS", CITY, price_of, focus_available=10000,
                    budget=5_000_000, fee_per_100=500)
        self.assertLess(p["unit_plain"]["margin"], 0)
        self.assertEqual([ph["phase"] for ph in p["phases"]], ["com foco"])
        self.assertGreater(p["total"]["profit"], 0)         # não afunda no automático
        self.assertIn("SÓ é lucrativo COM foco", p["advice"])
        self.assertIn("foco", p["limiter"])
        # forçar (allow_no_focus=True) mostra o estrago que seria feito
        forced = rf.plan("T5_PLANKS", CITY, price_of, focus_available=10000,
                         budget=5_000_000, fee_per_100=500, allow_no_focus=True)
        self.assertLess(forced["total"]["profit"], 0)

    def test_break_even_raw_price(self):
        be = rf.break_even_raw_price("T5_PLANKS", CITY, price_of, fee_per_100=500)
        self.assertGreater(be["com_foco"], be["sem_foco"])
        self.assertGreater(be["com_foco"], PRICES["T5_WOOD"])   # hoje compensa
        # no break-even exato a margem é ~0
        PRICES["T5_WOOD"] = be["sem_foco"]
        u = rf.unit_economics("T5_PLANKS", CITY, price_of, focus=False, fee_per_100=500)
        self.assertAlmostEqual(u["margin"], 0, delta=1.0)

    def test_budget_limits_before_focus(self):
        p = rf.plan("T5_PLANKS", CITY, price_of, focus_available=10 ** 9,
                    budget=100_000, fee_per_100=500)
        self.assertLessEqual(p["total"]["invested"], 100_000)
        self.assertEqual(p["limiter"], "prata")


class CascadeTests(unittest.TestCase):
    """Refinar T5 exige refinado T4, que exige T3... (a dúvida do coletor)."""

    def test_cascade_walks_down_to_t2(self):
        c = rf.cascade("wood", 5, 100, CITY, focus=True)
        tiers = [s["tier"] for s in c["steps"]]
        self.assertEqual(tiers, [5, 4, 3, 2])
        top = c["steps"][0]
        self.assertEqual(top["raw_id"], "T5_WOOD")
        # 100 tábuas T5 = 100 operações; cada uma consome 3 troncos x (1-RRR)
        self.assertAlmostEqual(top["raw_needed"], 100 * 3 * (1 - 0.539), delta=1)
        # e exige 46 tábuas T4 (que também são refinadas no passo seguinte)
        self.assertAlmostEqual(top["prev_needed"], 100 * (1 - 0.539), delta=1)
        self.assertEqual(c["steps"][1]["ops"], top["prev_needed"])
        self.assertGreater(c["focus_total"], 100 * 94)   # a cascata custa mais foco

    def test_buy_from_tier_stops_the_cascade(self):
        c = rf.cascade("wood", 5, 100, CITY, focus=True, buy_from_tier=4)
        self.assertEqual([s["tier"] for s in c["steps"]], [5, 4])


class CollectorModeTests(unittest.TestCase):
    """Modo coletor: estoque bruto -> o que sai, onde trava, quanto de foco."""

    def setUp(self):
        self._wood5 = PRICES["T5_WOOD"]

    def tearDown(self):
        PRICES["T5_WOOD"] = self._wood5

    def test_missing_prev_is_bought_not_a_dead_end(self):
        """Sem o refinado do tier de baixo o coletor travava em zero."""
        s = rf.from_stock("wood", {4: 1000}, CITY, price_of, focus_available=0)
        self.assertTrue(s["rows"])
        row = s["rows"][0]
        self.assertEqual(row["tier"], 4)
        self.assertGreater(row["made"], 0)
        self.assertGreater(row["prev_bought"], 0)      # comprou a tábua T3
        self.assertGreater(row["prev_cost"], 0)
        # sem poder comprar, o degrau realmente não sai
        travado = rf.from_stock("wood", {4: 1000}, CITY, price_of,
                                buy_missing_prev=False)
        self.assertFalse(travado["rows"])

    def test_output_feeds_next_tier(self):
        s = rf.from_stock("wood", {3: 500, 4: 1200, 5: 300}, CITY, price_of,
                          focus_available=10000)
        tiers = [r["tier"] for r in s["rows"]]
        self.assertEqual(tiers, sorted(tiers))
        by = {r["tier"]: r for r in s["rows"]}
        # o T4 usa as tábuas T3 produzidas antes de comprar
        self.assertLess(by[4]["prev_bought"], by[4]["prev_used"])

    def test_focus_goes_where_it_pays_most(self):
        s = rf.from_stock("wood", {3: 500, 4: 1200, 5: 300}, CITY, price_of,
                          focus_available=10000)
        gastos = {r["tier"]: r["focus_used"] for r in s["rows"]}
        melhor = max(s["focus_order"][:1]) if s["focus_order"] else None
        self.assertIsNotNone(melhor)
        self.assertGreater(gastos.get(melhor, 0), 0)   # o 1º da fila recebeu foco
        self.assertLessEqual(s["focus_used"], 10000)

    def test_does_not_refine_at_a_loss_without_focus(self):
        PRICES["T5_WOOD"] = 900        # T5 sem foco é prejuízo
        s = rf.from_stock("wood", {5: 300}, CITY, price_of, focus_available=0)
        self.assertFalse([r for r in s["rows"] if r["tier"] == 5])
        # com foco suficiente, volta a valer
        s2 = rf.from_stock("wood", {5: 300}, CITY, price_of, focus_available=30000)
        self.assertTrue([r for r in s2["rows"] if r["tier"] == 5])


class RankingTests(unittest.TestCase):
    def test_ranking_respects_limits_and_orders_by_profit(self):
        rows = rf.ranking(price_of, cities=[CITY], families=["wood"],
                          tiers=[3, 4, 5], focus_available=10000,
                          budget=1_000_000, limit=10)
        self.assertTrue(rows)
        lucros = [r["profit"] for r in rows]
        self.assertEqual(lucros, sorted(lucros, reverse=True))
        for r in rows:
            self.assertLessEqual(r["focus_used"], 10000)
            self.assertLessEqual(r["invested"], 1_000_000)

    def test_ranking_by_silver_per_focus(self):
        rows = rf.ranking(price_of, cities=[CITY], families=["wood"],
                          tiers=[3, 4, 5], focus_available=10000,
                          budget=1_000_000, sort="focus", limit=10)
        vals = [r["silver_per_focus"] or 0 for r in rows]
        self.assertEqual(vals, sorted(vals, reverse=True))


if __name__ == "__main__":
    unittest.main()

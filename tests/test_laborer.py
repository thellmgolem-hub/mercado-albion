# -*- coding: utf-8 -*-
"""Modelo completo do trabalhador de FABRICAÇÃO (albion/island.py).

Cobre crafting_laborer_economy: retorno do trabalhador (lootlist ponderada por
peso) e o custo REAL de encher. Encher NÃO consome o item craftado — você crafta,
ganha a fama e VENDE o item; logo o custo de encher é a MARGEM DE CRAFT (venda
líq − material net RRR), não o material bruto. Fixture sintética + preços fake,
sem tocar em data/island_data.json nem no cache — monkeypatch em island._load /
craft.*
"""
import os
import unittest

os.environ.setdefault("ALBION_AUTH_DISABLED", "1")

from albion import island, config
from albion.flips import sell_revenue


def net(price):
    """Venda líquida por ordem, premium (imposto 4% + anúncio 2,5%)."""
    return sell_revenue(price, "order", True)


class CraftingLaborerTests(unittest.TestCase):
    # 1 diário sintético de FABRICAÇÃO. Números redondos p/ conferir na mão.
    LAB = {
        "SYN_JOURNAL_TESTER_EMPTY": {
            "full": "SYN_JOURNAL_TESTER_FULL",
            "family": "TESTER",
            "tier": 6,
            "resource_family": None,
            "max_fame": 12000.0,
            "base_loot_amount": 4.0,
            "fill": {
                "fame_value": 3000.0,   # 12000/3000 = 4 crafts p/ encher
                "min_tier": 6,
                "items": ["SYN_CHEAP", "SYN_PRICEY"],
            },
            "loot": [
                {"item": "SYN_BAR", "ench": 0, "amount": 1.0, "weight": 75.0},
                {"item": "SYN_BAR", "ench": 1, "amount": 1.0, "weight": 25.0},
            ],
        },
        # diário de COLETA (sem fill) — deve ser IGNORADO por este motor
        "SYN_JOURNAL_MINER_EMPTY": {
            "full": "SYN_JOURNAL_MINER_FULL", "family": "ORE", "tier": 6,
            "resource_family": "ORE", "max_fame": 7200.0,
            "base_loot_amount": 32.0, "fill": None,
            "loot": [{"item": "SYN_ORE", "ench": 0, "amount": 1.0, "weight": 1.0}],
        },
    }

    # Dois validitem, AMBOS vendáveis. SYN_PRICEY tem MAIOR margem de craft
    # (deve ser o escolhido), embora custe mais material — a jogada real vende o
    # item, então o critério é margem, não material.
    RECIPES = {
        "SYN_CHEAP": {"inputs": [{"id": "SYN_BAR", "count": 2}],   # mat 200
                      "focus": 0, "output": 1, "category": "tools"},
        "SYN_PRICEY": {"inputs": [{"id": "SYN_BAR", "count": 10}],  # mat 1000
                       "focus": 0, "output": 1, "category": "tools"},
    }

    PRICES = {
        "SYN_BAR": 100,          # insumo (ench 0)
        "SYN_BAR@1": 500,        # loot encantado (ench 1)
        "SYN_CHEAP": 300,        # net 280,5 -> margem 80,5
        "SYN_PRICEY": 2000,      # net 1870  -> margem 870  (MAIOR)
        "SYN_JOURNAL_TESTER_EMPTY": 400,
        "SYN_JOURNAL_TESTER_FULL": 9000,
    }

    def _q1(self):
        q = {}
        for iid, p in self.PRICES.items():
            for c in config.CITIES:
                q[(iid, c)] = p
        return q

    def setUp(self):
        # injeta a fixture no lugar do island_data.json e neutraliza o RRR
        self._orig_load = island._load
        self._orig_recipe = island.craft.recipe_for
        self._orig_rrr = island.craft.unified_rrr
        self._orig_bcity = island.craft.unified_bonus_city
        island._load = lambda: {"laborers": self.LAB}
        island.craft.recipe_for = lambda iid: self.RECIPES.get(iid)
        island.craft.unified_rrr = lambda *a, **k: 0.0   # sem RRR no teste
        island.craft.unified_bonus_city = lambda *a, **k: "Martlock"

    def tearDown(self):
        island._load = self._orig_load
        island.craft.recipe_for = self._orig_recipe
        island.craft.unified_rrr = self._orig_rrr
        island.craft.unified_bonus_city = self._orig_bcity

    def _row(self):
        res = island.crafting_laborer_economy(self._q1(), premium=True,
                                              sell_mode="order")
        self.assertTrue(res["available"])
        # coleta (sem fill) fica fora; só o de fabricação entra
        self.assertEqual(res["priced"], 1)
        return res["rows"][0]

    def test_return_is_baseloot_times_weighted_average(self):
        r = self._row()
        # média ponderada por peso (75/100 e 25/100), amount=1, base_loot=4
        weighted = 0.75 * 1 * net(100) + 0.25 * 1 * net(500)
        expected = 4.0 * weighted
        self.assertEqual(r["retorno_por_diario"], round(expected))
        self.assertEqual(r["loot_priced_weight_pct"], 100.0)  # tudo precificado
        self.assertEqual(r["loot_top"], "SYN_BAR")

    def test_fill_choice_maximizes_craft_margin(self):
        r = self._row()
        # margem SYN_CHEAP  = net(300)  - 200  = 80,5
        # margem SYN_PRICEY = net(2000) - 1000 = 870   -> escolhido
        self.assertEqual(r["fill_item"], "SYN_PRICEY")
        self.assertEqual(r["crafts_to_fill"], 4.0)
        self.assertEqual(r["margem_craft_un"], round(net(2000) - 1000))
        self.assertEqual(r["craft_venda_liquida"], round(net(2000)))
        self.assertEqual(r["material_un"], 1000)
        self.assertTrue(r["fill_cost_is_proxy"])     # fama/craft é proxy
        self.assertFalse(r["fill_liquidez_baixa"])   # item de fill é cotado

    def test_selling_beats_discarding_when_margin_positive(self):
        r = self._row()
        crafts, margin = 4.0, net(2000) - 1000       # margem positiva
        mat_unit = 1000
        # encher VENDENDO: custo = -(crafts*margem) (crédito, pois margem>0)
        self.assertEqual(r["custo_encher_vendendo"], round(-crafts * margin))
        # encher DESCARTANDO: custo = crafts * material (pessimista)
        self.assertEqual(r["custo_encher_descartando"], round(crafts * mat_unit))
        # com margem >= 0, vender NUNCA é pior que descartar (correção do furo)
        self.assertGreater(r["lucro_alimentar_vendendo"],
                           r["lucro_alimentar_descartando"])
        # e as duas definições batem com retorno - custo - vazio
        self.assertEqual(
            r["lucro_alimentar_vendendo"],
            round(r["retorno_por_diario"] - r["custo_encher_vendendo"]
                  - r["vazio"]))
        self.assertEqual(
            r["lucro_alimentar_descartando"],
            round(r["retorno_por_diario"] - r["custo_encher_descartando"]
                  - r["vazio"]))

    def test_flip_play_and_prices(self):
        r = self._row()
        self.assertEqual(r["vazio"], 400)
        self.assertEqual(r["cheio"], round(net(9000)))
        self.assertEqual(r["lucro_flip"], round(r["cheio"] - r["vazio"]))

    def test_unsellable_validitem_is_skipped(self):
        # sem cotação de venda p/ nenhum validitem -> não dá pra encher (fill None)
        orig = dict(self.PRICES)
        try:
            self.PRICES.pop("SYN_CHEAP")
            self.PRICES.pop("SYN_PRICEY")
            res = island.crafting_laborer_economy(self._q1(), premium=True,
                                                  sell_mode="order")
            self.assertEqual(res["priced"], 0)   # sem validitem vendável, sai fora
        finally:
            self.PRICES.clear()
            self.PRICES.update(orig)

    def test_partial_loot_pricing_is_conservative(self):
        # sem preço p/ a variante encantada: ela contribui 0 e a cobertura cai
        orig = dict(self.PRICES)
        try:
            self.PRICES.pop("SYN_BAR@1")
            r = self._row()
            expected = 4.0 * (0.75 * 1 * net(100))   # só a base (peso 75/100)
            self.assertEqual(r["retorno_por_diario"], round(expected))
            self.assertEqual(r["loot_priced_weight_pct"], 75.0)
        finally:
            self.PRICES.clear()
            self.PRICES.update(orig)


if __name__ == "__main__":
    unittest.main()

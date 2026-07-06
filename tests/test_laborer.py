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

    # Dois validitem, AMBOS vendáveis e feitos SÓ de refinado básico
    # (T6_METALBAR — casa no regex de insumo limpo, refined_only default).
    # SYN_PRICEY tem MAIOR margem de craft (deve ser o escolhido), embora custe
    # mais material — a jogada real vende o item, então o critério é margem.
    RECIPES = {
        "SYN_CHEAP": {"inputs": [{"id": "T6_METALBAR", "count": 2}],   # mat 200
                      "focus": 0, "output": 1, "category": "tools"},
        "SYN_PRICEY": {"inputs": [{"id": "T6_METALBAR", "count": 10}],  # mat 1000
                       "focus": 0, "output": 1, "category": "tools"},
    }

    PRICES = {
        "T6_METALBAR": 100,      # insumo refinado (ench 0)
        "SYN_BAR": 100,          # item de loot (ench 0)
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

    def test_bait_price_fill_item_is_rejected(self):
        # SYN_PRICEY vira ISCA: preço absurdo (margem gigante). Um validador de
        # VWAP (fill_sell_ok) o rejeita; o motor deve escolher o SÃO (SYN_CHEAP),
        # que tem margem menor mas passa na banda — e o escolhido NÃO é isca.
        orig = dict(self.PRICES)
        try:
            self.PRICES["SYN_PRICEY"] = 9_000_000   # isca: 9M
            sell_ok = lambda iid, price: iid != "SYN_PRICEY"   # reprova a isca
            res = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                fill_sell_ok=sell_ok)
            r = res["rows"][0]
            self.assertEqual(r["fill_item"], "SYN_CHEAP")   # o são venceu
            self.assertFalse(r["fill_isca"])                # o escolhido é elegível
            # e o lucro NÃO explodiu por causa da isca (margem do SYN_CHEAP ~80)
            self.assertLess(r["margem_craft_un"], 1000)
        finally:
            self.PRICES.clear()
            self.PRICES.update(orig)

    def test_bait_flagged_when_no_sane_alternative(self):
        # se o ÚNICO validitem cotado é isca, o motor usa-o mas marca fill_isca.
        orig = dict(self.PRICES)
        try:
            self.PRICES.pop("SYN_CHEAP")            # sobra só o SYN_PRICEY (isca)
            self.PRICES["SYN_PRICEY"] = 9_000_000
            sell_ok = lambda iid, price: price <= 5000   # reprova a isca
            res = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                fill_sell_ok=sell_ok)
            r = res["rows"][0]
            self.assertEqual(r["fill_item"], "SYN_PRICEY")
            self.assertTrue(r["fill_isca"])          # avisa: escolhido fora da banda
        finally:
            self.PRICES.clear()
            self.PRICES.update(orig)

    def test_station_fee_reduces_profit_by_ncrafts_times_fee(self):
        # station_fee (prata por craft) abate n_crafts × fee do lucro alimentar.
        base = island.crafting_laborer_economy(
            self._q1(), premium=True, sell_mode="order", station_fee=0)["rows"][0]
        fee = 300
        withfee = island.crafting_laborer_economy(
            self._q1(), premium=True, sell_mode="order",
            station_fee=fee)["rows"][0]
        n = base["crafts_to_fill"]               # 4.0 na fixture
        self.assertEqual(withfee["custo_taxa_estacao"], round(n * fee))
        self.assertEqual(withfee["station_fee"], fee)
        # vendendo E descartando caem exatamente n×fee
        self.assertEqual(
            base["lucro_alimentar_vendendo"] - withfee["lucro_alimentar_vendendo"],
            round(n * fee))
        self.assertEqual(
            base["lucro_alimentar_descartando"]
            - withfee["lucro_alimentar_descartando"], round(n * fee))
        # default é 0 (não inventamos taxa)
        self.assertEqual(base["station_fee"], 0)
        self.assertEqual(base["custo_taxa_estacao"], 0)

    def test_special_input_fill_item_excluded_for_refined_one(self):
        # SYN_PRICEY passa a usar um COMPONENTE ESPECIAL (skillbook) — mesmo com
        # margem maior, deve ser EXCLUÍDO (refined_only). Vence o 100% refinado
        # (SYN_CHEAP, só T6_METALBAR), e fill_item_inputs comprova a decisão.
        orig_recipes = dict(self.RECIPES)
        orig_prices = dict(self.PRICES)
        try:
            self.RECIPES["SYN_PRICEY"] = {
                "inputs": [{"id": "T6_METALBAR", "count": 10},
                           {"id": "T6_SKILLBOOK_STANDARD", "count": 1}],
                "focus": 0, "output": 1, "category": "tools"}
            self.PRICES["T6_SKILLBOOK_STANDARD"] = 500
            r = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                refined_only=True)["rows"][0]
            self.assertEqual(r["fill_item"], "SYN_CHEAP")     # o refinado venceu
            self.assertFalse(r["fill_impuro"])                # escolhido é limpo
            ins = {i["id"] for i in r["fill_item_inputs"]}
            self.assertEqual(ins, {"T6_METALBAR"})            # só refinado básico
            # com --no-fill-refined-only o especial volta a ser elegível (maior margem)
            r2 = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                refined_only=False)["rows"][0]
            self.assertEqual(r2["fill_item"], "SYN_PRICEY")
        finally:
            self.RECIPES.clear(); self.RECIPES.update(orig_recipes)
            self.PRICES.clear(); self.PRICES.update(orig_prices)

    def test_all_special_flags_impuro(self):
        # se o ÚNICO cotado tem insumo especial, o motor usa-o mas marca fill_impuro.
        orig_recipes = dict(self.RECIPES)
        orig_prices = dict(self.PRICES)
        try:
            self.PRICES.pop("SYN_CHEAP")   # sobra só o SYN_PRICEY
            self.RECIPES["SYN_PRICEY"] = {
                "inputs": [{"id": "T6_METALBAR", "count": 10},
                           {"id": "T6_ARTEFACT_FOO", "count": 1}],
                "focus": 0, "output": 1, "category": "tools"}
            self.PRICES["T6_ARTEFACT_FOO"] = 500
            r = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                refined_only=True)["rows"][0]
            self.assertEqual(r["fill_item"], "SYN_PRICEY")
            self.assertTrue(r["fill_impuro"])      # avisa: insumo especial
            self.assertFalse(r["fill_eligivel"])
        finally:
            self.RECIPES.clear(); self.RECIPES.update(orig_recipes)
            self.PRICES.clear(); self.PRICES.update(orig_prices)

    def test_farm_input_accepted_when_allow_farm(self):
        # insumo FARMÁVEL (cultura) é aceito só com allow_farm; sem ele, exclui.
        orig_recipes = dict(self.RECIPES)
        orig_prices = dict(self.PRICES)
        orig_load = island._load
        try:
            # island_data com uma cultura farmável (T6_POTATO)
            island._load = lambda: {"laborers": self.LAB,
                                    "crops": {"T6_FARM_POTATO_SEED":
                                              {"crop": "T6_POTATO"}}}
            self.RECIPES["SYN_CHEAP"] = {
                "inputs": [{"id": "T6_METALBAR", "count": 2},
                           {"id": "T6_POTATO", "count": 1}],
                "focus": 0, "output": 1, "category": "tools"}
            self.PRICES["T6_POTATO"] = 50
            self.PRICES.pop("SYN_PRICEY")   # sobra o SYN_CHEAP (metalbar + batata)
            r_yes = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                refined_only=True, allow_farm=True)["rows"][0]
            self.assertEqual(r_yes["fill_item"], "SYN_CHEAP")
            self.assertFalse(r_yes["fill_impuro"])   # batata é farmável => limpo
            # sem allow_farm, a batata deixa de ser limpa => impuro
            r_no = island.crafting_laborer_economy(
                self._q1(), premium=True, sell_mode="order",
                refined_only=True, allow_farm=False)["rows"][0]
            self.assertTrue(r_no["fill_impuro"])
        finally:
            island._load = orig_load
            self.RECIPES.clear(); self.RECIPES.update(orig_recipes)
            self.PRICES.clear(); self.PRICES.update(orig_prices)

    def test_yield_pct_multiplies_worker_return(self):
        # yield_pct (felicidade, journal_yield) multiplica SÓ o retorno do
        # trabalhador; default None = comportamento atual (loot pleno = 100%).
        base = self._row()
        r150 = island.crafting_laborer_economy(
            self._q1(), premium=True, sell_mode="order",
            yield_pct=150)["rows"][0]
        self.assertEqual(r150["retorno_por_diario"],
                         round(base["retorno_por_diario"] * 1.5))
        # custo de encher e vazio NÃO mudam — só o retorno
        self.assertEqual(r150["custo_encher_vendendo"],
                         base["custo_encher_vendendo"])
        self.assertEqual(r150["vazio"], base["vazio"])
        r100 = island.crafting_laborer_economy(
            self._q1(), premium=True, sell_mode="order",
            yield_pct=100)["rows"][0]
        self.assertEqual(r100["retorno_por_diario"],
                         base["retorno_por_diario"])

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


class LaborerPlanTests(unittest.TestCase):
    """Otimizador de diversificação + produção (island.laborer_plan)."""
    # a chave DEVE bater com T{tier}_JOURNAL_{family}_EMPTY (o que laborer_plan monta)
    LAB = {
        "T6_JOURNAL_TESTER_EMPTY": {
            "full": "T6_JOURNAL_TESTER_FULL", "family": "TESTER", "tier": 6,
            "resource_family": None, "max_fame": 12000.0, "base_loot_amount": 4.0,
            "fill": {"fame_value": 3000.0, "min_tier": 6,
                     "items": ["SYN_CHEAP", "SYN_PRICEY"]},
            "loot": [{"item": "SYN_BAR", "ench": 0, "amount": 1.0, "weight": 100.0}],
        },
    }
    RECIPES = {  # ambos 100% refinado (T6_METALBAR), ambos elegíveis
        "SYN_CHEAP": {"inputs": [{"id": "T6_METALBAR", "count": 2}],   # mat 200
                      "focus": 0, "output": 1, "category": "tools"},
        "SYN_PRICEY": {"inputs": [{"id": "T6_METALBAR", "count": 10}],  # mat 1000
                       "focus": 0, "output": 1, "category": "tools"},
    }
    PRICES = {
        "T6_METALBAR": 100,
        "SYN_BAR": 100,
        "SYN_CHEAP": 300,        # margem ~80,5
        "SYN_PRICEY": 2000,      # margem ~870 (MAIOR -> guloso pega 1º)
        "T6_JOURNAL_TESTER_EMPTY": 400,
        "T6_JOURNAL_TESTER_FULL": 9000,
    }

    def _q1(self):
        return {(iid, c): p for iid, p in self.PRICES.items()
                for c in config.CITIES}

    def setUp(self):
        self._o = (island._load, island.craft.recipe_for,
                   island.craft.unified_rrr, island.craft.unified_bonus_city)
        island._load = lambda: {"laborers": self.LAB}
        island.craft.recipe_for = lambda iid: self.RECIPES.get(iid)
        island.craft.unified_rrr = lambda *a, **k: 0.0
        island.craft.unified_bonus_city = lambda *a, **k: "Martlock"

    def tearDown(self):
        (island._load, island.craft.recipe_for, island.craft.unified_rrr,
         island.craft.unified_bonus_city) = self._o

    def _plan(self, volumes, **kw):
        return island.laborer_plan(
            self._q1(), family="TESTER", tier=6, n_laborers=kw.pop("n", 3),
            item_volumes=volumes, **kw)

    def test_allocation_respects_per_item_cap(self):
        # SYN_PRICEY vol 20 -> cap 4 ; SYN_CHEAP vol 50 -> cap 10. needed=9.
        # guloso: pega 4 do PRICEY (maior margem) + 5 do CHEAP = 9. Nenhum passa do cap.
        r = self._plan({"SYN_PRICEY": 20, "SYN_CHEAP": 50}, n=3)
        self.assertFalse(r["market_limited"])
        self.assertEqual(r["crafts_allocated"], 9)
        by = {b["item"]: b for b in r["basket"]}
        self.assertEqual(by["SYN_PRICEY"]["crafts_dia"], 4)   # limitado pelo cap
        self.assertEqual(by["SYN_PRICEY"]["cap"], 4)
        self.assertEqual(by["SYN_CHEAP"]["crafts_dia"], 5)    # completa o restante
        for b in r["basket"]:
            self.assertLessEqual(b["crafts_dia"], b["cap"])   # nunca passa do cap
        # material refinado net RRR: 4×10 (PRICEY) + 5×2 (CHEAP) = 50 T6_METALBAR
        self.assertEqual(r["refined_per_day"]["METALBAR"], 50)
        self.assertEqual(r["raw_gather_per_day"]["ORE"], 50)  # ~1 bruto/refinado

    def test_market_limited_when_caps_below_need(self):
        # volumes baixos: cap 2 + 2 = 4 < needed 9 -> market_limited, feedable=1
        r = self._plan({"SYN_PRICEY": 10, "SYN_CHEAP": 10}, n=3)
        self.assertTrue(r["market_limited"])
        self.assertEqual(r["market_capacity"], 4)     # 2 + 2
        self.assertEqual(r["crafts_allocated"], 4)
        self.assertEqual(r["laborers_feedable"], 1)   # 4 // 3
        # item sem volume é EXCLUÍDO (não dá pra vender)
        r2 = self._plan({"SYN_PRICEY": 10}, n=3)      # SYN_CHEAP sem volume
        self.assertEqual({b["item"] for b in r2["basket"]}, {"SYN_PRICEY"})

    def test_profit_day_is_sum_of_parts(self):
        r = self._plan({"SYN_PRICEY": 20, "SYN_CHEAP": 50}, n=3, station_fee=50)
        # lucro/dia = retorno_total + margem_total − vazio_total − taxa_total
        self.assertEqual(
            r["profit_day"],
            r["worker_return_total"] + r["craft_margin_total"]
            - r["empty_cost_total"] - r["station_fee_total"])
        n_journals = r["n_laborers"] * r["journals_per_day"]
        self.assertEqual(r["worker_return_total"],
                         n_journals * r["return_per_journal"])
        self.assertEqual(r["empty_cost_total"], n_journals * r["empty_price"])
        self.assertEqual(r["station_fee_total"], r["crafts_allocated"] * 50)
        # margem total bate com a soma dos itens da cesta
        self.assertEqual(r["craft_margin_total"],
                         sum(b["lucro_item_dia"] for b in r["basket"]))

    def test_yield_pct_multiplies_return_per_journal(self):
        # yield_pct multiplica o retorno/diário do plano (default None = pleno)
        vols = {"SYN_PRICEY": 20, "SYN_CHEAP": 50}
        base = self._plan(vols, n=3)
        r = self._plan(vols, n=3, yield_pct=150)
        self.assertEqual(r["return_per_journal"],
                         round(base["return_per_journal"] * 1.5))
        # e o lucro/dia continua sendo a soma das partes (com o retorno novo)
        self.assertEqual(
            r["profit_day"],
            r["worker_return_total"] + r["craft_margin_total"]
            - r["empty_cost_total"] - r["station_fee_total"])
        # a cesta/margens não mudam — só o retorno do trabalhador
        self.assertEqual(r["craft_margin_total"], base["craft_margin_total"])


class HappinessTests(unittest.TestCase):
    """Painel de felicidade — modelo REAL do jogo (calibrado 2026-07-05 com
    screenshot ao vivo: Ferreiro T6 em GH T7 → Camas 200/200 · Mesas 200/200 ·
    Troféus 25/100 · Total 425/500; diários T2=150%, T3=150%, T4=112,5%).
    Cálculo puro, sem dump nem cache."""

    # o cenário do dono (verdade-base do screenshot), como kwargs reutilizáveis
    OWNER = dict(building_tier=7, n_laborers=15, beds=15, bed_tier=7,
                 tables=3, table_tier=7, family="WARRIOR")

    def test_a_screenshot_panel_425(self):
        # (a) caso do print: gerais 2..7 MENOS um tier (5 cobertos) → 425/500
        p = island.happiness_panel(6, general_tiers=(2, 3, 4, 5, 7),
                                   **self.OWNER)
        self.assertEqual(p["camas"], {"score": 200, "cap": 200,
                                      "estimado": False})
        self.assertEqual(p["mesas"], {"score": 200, "cap": 200,
                                      "estimado": False})
        self.assertEqual(p["trofeus"]["score"], 25)
        self.assertEqual(p["trofeus"]["cap"], 100)
        self.assertEqual(p["total"], 425)
        self.assertEqual(p["total_max"], 500)
        self.assertFalse(p["estimado"])            # painel maxado = calibrado
        # rendimento por tier de diário — EXATAMENTE o painel do jogo
        self.assertEqual(island.journal_yield(425, 2), 150.0)
        self.assertEqual(island.journal_yield(425, 3), 150.0)
        self.assertEqual(island.journal_yield(425, 4), 112.5)
        # o tier que falta aparece como hint alcançável, citando o ponto
        adv = island.happiness_advice(6, general_tiers=(2, 3, 4, 5, 7),
                                      **self.OWNER)
        self.assertTrue(any("troféu geral T6 sem cobertura: +5" in h
                            for h in adv["hints"]))

    def test_b_owner_full_generals_430(self):
        # (b) gerais 2..7 completos → 430/500 e diário T4 = 115%
        adv = island.happiness_advice(6, general_tiers=(2, 3, 4, 5, 6, 7),
                                      **self.OWNER)
        self.assertEqual(adv["total"], 430)
        ys = {y["diario_tier"]: y["yield_pct"] for y in adv["yield_por_diario"]}
        self.assertEqual(ys[2], 150.0)
        self.assertEqual(ys[3], 150.0)
        self.assertEqual(ys[4], 115.0)
        self.assertEqual(ys[5], 100.0)             # piso (calibrado-parcial)
        self.assertEqual(ys[6], 100.0)

    def test_c_t4_laborer_caps_100_100_100(self):
        # (c) confirmação independente do fórum: T4 = "300/300", troféu 100 fixo
        p = island.happiness_panel(4, building_tier=7, n_laborers=1, beds=1,
                                   bed_tier=7, tables=1, table_tier=7)
        self.assertEqual(p["camas"]["cap"], 100)
        self.assertEqual(p["mesas"]["cap"], 100)
        self.assertEqual(p["camas"]["score"], 100)
        self.assertEqual(p["mesas"]["score"], 100)
        self.assertEqual(p["trofeus"]["cap"], 100)
        self.assertEqual(p["total_max"], 300)

    def test_d_gathering_typed_saturates_trophy_100(self):
        # (d) coleta ORE em prédio T8: gerais 2..8 (35) + tipo 2..8 (70) + tubarão
        # (5) = 110 bruto → satura no cap 100
        p = island.happiness_panel(6, building_tier=8, n_laborers=3, beds=3,
                                   bed_tier=8, tables=1, table_tier=8,
                                   general_tiers=range(2, 9),
                                   typed_tiers=range(2, 9), family="ORE",
                                   shark=True)
        self.assertEqual(p["trofeus"]["bruto"], 110)
        self.assertEqual(p["trofeus"]["score"], 100)
        self.assertEqual(p["total"], 500)          # 200+200+100 = teto do T6

    def test_e_crafting_trophy_ceiling_and_best_yield(self):
        # (e) fabricação NÃO tem troféu de tipo: mesmo com TUDO (prédio T8,
        # gerais 2..8, tubarão) o troféu para em 40; +5 do Spyglass inobtenível
        # → teto ABSOLUTO 45. Nunca chega perto dos 100.
        base = dict(building_tier=8, n_laborers=1, beds=1, bed_tier=8,
                    tables=1, table_tier=8, general_tiers=range(2, 9),
                    family="WARRIOR", shark=True)
        p = island.happiness_panel(8, **base)
        self.assertEqual(p["trofeus"]["score"], 40)          # sem spyglass
        p_abs = island.happiness_panel(8, spyglass=True, **base)
        self.assertEqual(p_abs["trofeus"]["score"], 45)      # teto absoluto
        self.assertLess(p_abs["trofeus"]["score"], 100)      # nunca fecha 100
        # teto do painel p/ L=8: mobília 600 + troféu 45 = 645 → o MELHOR diário
        # acima do piso é o T6 (=L−2): 100 + 0,5×(645−600) = 122,5%; o T7 (=L−1)
        # já cai no piso de 100%
        self.assertEqual(p_abs["total"], 645)
        self.assertEqual(island.journal_yield(645, 6), 122.5)
        self.assertEqual(island.journal_yield(645, 7), 100.0)
        # advice deixa o trancado explícito
        adv = island.happiness_advice(8, **base)
        self.assertEqual(adv["alcancavel"]["trofeus"], 40)
        self.assertTrue(any("FABRICAÇÃO" in h for h in adv["trancados"]))
        self.assertTrue(any("Spyglass" in h for h in adv["trancados"]))

    def test_f_validations(self):
        # (f1) mobília acima do tier do prédio → erro
        with self.assertRaises(ValueError):
            island.happiness_panel(6, building_tier=7, n_laborers=1, beds=1,
                                   bed_tier=8, tables=0, table_tier=None)
        # troféu geral acima do prédio → erro; tubarão em prédio < T8 → erro
        with self.assertRaises(ValueError):
            island.happiness_panel(6, building_tier=7, n_laborers=1, beds=1,
                                   bed_tier=7, tables=0, table_tier=None,
                                   general_tiers=(8,))
        with self.assertRaises(ValueError):
            island.happiness_panel(6, building_tier=7, n_laborers=1, beds=1,
                                   bed_tier=7, tables=0, table_tier=None,
                                   shark=True)
        # (f2) typed em família de FABRICAÇÃO é IGNORADO (não soma, não erra)
        kw = dict(building_tier=7, n_laborers=1, beds=1, bed_tier=7,
                  tables=1, table_tier=7, general_tiers=(2, 3),
                  family="WARRIOR")
        with_typed = island.happiness_panel(6, typed_tiers=(2, 3, 4), **kw)
        without = island.happiness_panel(6, **kw)
        self.assertEqual(with_typed["trofeus"]["score"],
                         without["trofeus"]["score"])
        self.assertEqual(with_typed["trofeus"]["tipo"], 0)

    def test_g_yield_floor_100(self):
        # (g) piso: total 430 num diário T5 → 100% (nunca abaixo do piso)
        self.assertEqual(island.journal_yield(430, 5), 100.0)
        self.assertEqual(island.journal_yield(0, 8), 100.0)
        # e o teto: nunca acima de 150%
        self.assertEqual(island.journal_yield(10_000, 2), 150.0)

    def test_partial_furniture_is_proportional_estimate(self):
        # abaixo do maxado a pontuação cai (média/cobertura) e vira ESTIMATIVA
        p = island.happiness_panel(6, building_tier=7, n_laborers=15, beds=5,
                                   bed_tier=7, tables=3, table_tier=3)
        self.assertAlmostEqual(p["camas"]["score"], 200 * (5 / 15), places=1)
        self.assertTrue(p["camas"]["estimado"])
        # mesa T3 p/ trabalhador T6: qualidade 3/6 = metade do cap
        self.assertEqual(p["mesas"]["score"], 100)
        self.assertTrue(p["mesas"]["estimado"])
        self.assertTrue(p["estimado"])


if __name__ == "__main__":
    unittest.main()

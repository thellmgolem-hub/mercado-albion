# -*- coding: utf-8 -*-
"""Testes dos helpers de builds do bot (data/builds.json + apresentação PT-BR).

Só funções puras — não sobe o bot nem precisa do discord.py conectado.
"""
import asyncio
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import discord_bot as bot  # noqa: E402


class BuildsDataTests(unittest.TestCase):
    def test_builds_json_loads_all(self):
        self.assertGreaterEqual(len(bot.load_builds()), 40)

    def test_all_items_resolved_to_official_pt(self):
        # cada slot preenchido tem id + nome PT oficial (tradução completa)
        for b in bot.load_builds():
            for slot, it in (b.get("items") or {}).items():
                if it is None:
                    continue
                self.assertTrue(it.get("id"), f"{b['buildName']}/{slot} sem id")
                self.assertTrue(it.get("pt"), f"{b['buildName']}/{slot} sem pt")
                self.assertTrue(it.get("icon", "").startswith("/icon/"))

    def test_find_by_tree_and_content(self):
        fire = bot.find_builds("Cajados de Fogo")
        self.assertTrue(fire)
        self.assertTrue(all(b["tree"] == "Cajados de Fogo" for b in fire))
        dawn = bot.find_builds("Cajados de Fogo", "Faccao/ZvZ")
        self.assertTrue(any(b["buildName"] == "Dawnsong" for b in dawn))

    def test_find_unknown_is_empty(self):
        self.assertEqual(bot.find_builds("Arvore Inexistente"), [])

    def test_card_text_is_portuguese_only(self):
        dawn = next(b for b in bot.load_builds() if b["buildName"] == "Dawnsong")
        txt = bot.build_card_text(dawn)
        self.assertIn("Canção da Alvorada", txt)   # arma em PT oficial
        self.assertIn("Habilidades", txt)
        self.assertNotIn("Knight Helmet", txt)      # nada de EN cru vazando

    def test_title_and_icon_url(self):
        dawn = next(b for b in bot.load_builds() if b["buildName"] == "Dawnsong")
        title = bot.build_title(dawn)
        self.assertIn("Canção da Alvorada", title)   # título em PT (buildName_pt)
        self.assertNotIn("Dawnsong", title)          # nada de nome EN no título
        self.assertIn("Fogo", title)
        url = bot.weapon_icon_url(dawn)
        self.assertTrue(url.startswith("https://render.albiononline.com/v1/item/"))
        self.assertIn("size=128", url)

    def test_no_english_build_names_in_titles(self):
        # regressão: NENHUM título de build deve exibir o buildName em inglês
        for b in bot.load_builds():
            title = bot.build_title(b)
            self.assertNotIn(b["buildName"], title,
                             f"título com nome EN: {b['buildName']}")
            self.assertTrue(b.get("buildName_pt"),
                            f"{b['buildName']} sem buildName_pt")

    def test_no_english_game_terms_in_card(self):
        # regressão: título + ficha (itens/skills/execução/nota) sem termo de jogo
        # em inglês (build_notes_pt.py + build_names_pt.py traduzem tudo).
        EN = re.compile(
            r"\b(Wildfire|Quiver|Meteor|Cataclysm|Wall of Flames|Fire Wave|Frost Nova|"
            r"Frost Lance|Frost Shot|Cleric Robe|Hellion Jacket|Hellion Hood|Mercenary "
            r"Hood|Mercenary Shoes|Mercenary Jacket|Assassin Hood|Knight Armor|Demon "
            r"Armor|Feyscale Robe|Graveguard Helmet|Enfeeble Blades|Forceful Swing|Ray "
            r"of Light|Magic Arrow|Living Armor|Death Curse|Haunting Screams|Area of "
            r"Decay|Mystic Rocks|Soul Shaker|Acid Potion|Bear Paws|Great Frost)\b")
        for b in bot.load_builds():
            txt = bot.build_title(b) + " " + bot.build_card_text(b)
            m = EN.search(txt)
            self.assertIsNone(m, f"{b['buildName']}: termo EN "
                                 f"'{m.group(0) if m else ''}' na ficha")

    def test_every_tree_has_builds_and_clean_label(self):
        for label, value in bot.BUILD_TREES:
            self.assertTrue(bot.find_builds(value), f"árvore vazia: {value}")
            self.assertNotIn("(", label)            # rótulo limpo p/ o usuário


class _FakeApi:
    def __init__(self, search_res, prices_res):
        self._search, self._prices = search_res, prices_res

    async def get(self, path, params=None):
        if path == "/api/search":
            return self._search
        if path == "/api/prices":
            return self._prices
        return []


class HandlerTests(unittest.TestCase):
    def test_resolve_item_prefers_exact_id(self):
        search = [{"id": "T4_BAG", "pt": "Bolsa", "tier": 4, "ench": 0},
                  {"id": "T8_2H_FIRESTAFF", "pt": "Cajado", "tier": 8, "ench": 0}]
        it = asyncio.run(bot._resolve_item(_FakeApi(search, []),
                                           "T8_2H_FIRESTAFF"))
        self.assertEqual(it["id"], "T8_2H_FIRESTAFF")

    def test_comparar_table_and_flip_route(self):
        # /comparar agora resolve o item LOCAL e busca preço direto na AODP:
        # injeta a via local (o id exato resolve pelo items_db do disco).
        prices = [
            {"city": "Caerleon", "sell_price_min": 1000, "buy_price_max": 800},
            {"city": "Martlock", "sell_price_min": 1500, "buy_price_max": 1200},
            {"city": "Lymhurst", "sell_price_min": 900, "buy_price_max": 700},
        ]

        async def _f(ids, cities=None, qualities=None):
            return prices
        orig = bot.local_prices_rows
        try:
            bot.local_prices_rows = _f
            out = asyncio.run(bot.handle_comparar(None, "T4_BAG"))
        finally:
            bot.local_prices_rows = orig
        self.assertIn("Bolsa", out)
        self.assertIn("Caerleon", out)
        # rota: comprar mais barato (Lymhurst) -> vender maior ordem (Martlock)
        self.assertIn("comprar em Lymhurst", out)
        self.assertIn("Martlock", out)

    def test_comparar_no_item(self):
        out = asyncio.run(bot.handle_comparar(_FakeApi([], []), "xyz"))
        self.assertIn("Nenhum item", out)

    def test_bar_scales(self):
        self.assertEqual(bot._bar(0, 100), "")
        self.assertTrue(len(bot._bar(100, 100)) >= len(bot._bar(50, 100)))

    @staticmethod
    def _fake_gold(pts):
        """/ouro agora bate DIRETO na AODP (sem plataforma): injeta a via local
        pra o teste seguir hermético (sem rede)."""
        async def _f(count=48):
            return pts
        return _f

    def test_ouro_usa_mais_novo_como_atual(self):
        # a via local normaliza p/ o mais NOVO primeiro: pts[0]=agora.
        orig = bot.local_gold_pts
        try:
            bot.local_gold_pts = self._fake_gold(
                [{"price": 4000, "ts": "2026-07-14T00:00:00"},    # atual
                 {"price": 3000, "ts": "2026-07-12T00:00:00"}])   # ~48h atrás
            out = asyncio.run(bot.handle_ouro(None))
            self.assertIn("4.000", out)    # cotação atual = ponto mais novo
            self.assertIn("subindo", out)  # 4000 vs 3000 -> subiu
        finally:
            bot.local_gold_pts = orig

    def test_ouro_sem_cotacao_nao_quebra(self):
        orig = bot.local_gold_pts
        try:
            bot.local_gold_pts = self._fake_gold([])   # AODP fora / sem dado
            out = asyncio.run(bot.handle_ouro(None))   # sem ZeroDivisionError
            self.assertIn("cotação do ouro", out)
        finally:
            bot.local_gold_pts = orig

    def test_historico_grafico_e_estatisticas(self):
        def mk(base, step, cnt):
            return [{"ts": f"2026-07-{d:02d}T00:00:00",
                     "avg_price": base + d * step, "item_count": cnt}
                    for d in range(1, 31)]
        series = [{"item_id": "T4_BAG", "city": "Martlock", "quality": 1,
                   "data": mk(900, 5, 10)},
                  {"item_id": "T4_BAG", "city": "Caerleon", "quality": 1,
                   "data": mk(1000, 10, 50)}]

        async def _f(ids, cities=None, quality=None, time_scale=24, days=30):
            return series
        orig = bot.local_history_series
        try:
            bot.local_history_series = _f
            res = asyncio.run(bot.handle_historico(None, "T4_BAG", 30))
        finally:
            bot.local_history_series = orig
        # Caerleon tem mais volume -> vem primeiro no texto e no gráfico
        self.assertTrue(res["text"].startswith("**Caerleon**"))
        self.assertIn("Martlock", res["text"])
        self.assertIn("+", res["text"])            # variação com sinal
        url = res["chart_url"]
        self.assertTrue(url.startswith("https://quickchart.io/chart"))
        self.assertLessEqual(len(url), 2000)       # cabe no limite de URL

    def test_historico_sem_dados(self):
        async def _f(ids, cities=None, quality=None, time_scale=24, days=30):
            return []
        orig = bot.local_history_series
        try:
            bot.local_history_series = _f
            res = asyncio.run(bot.handle_historico(None, "T4_BAG"))
        finally:
            bot.local_history_series = orig
        self.assertIsNone(res["chart_url"])
        self.assertIn("sem histórico", res["text"])

    def test_sample_preserva_extremos(self):
        seq = list(range(100))
        out = bot._sample(seq, 20)
        self.assertEqual(len(out), 20)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[-1], 99)
        self.assertEqual(bot._sample([1, 2], 20), [1, 2])


class HelpTests(unittest.TestCase):
    def test_help_has_intro_and_sections(self):
        self.assertTrue(bot.HELP_INTRO)
        fields = bot.help_fields()
        self.assertGreaterEqual(len(fields), 5)
        for name, body in fields:
            self.assertTrue(name and body)
            self.assertLessEqual(len(body), 1024)   # limite de campo do embed

    def test_help_covers_every_registered_command(self):
        import os
        os.environ.setdefault("ALBION_PUBLIC_URL", "https://x")
        from tools.discord_bot import build_bot, ApiClient
        b = build_bot(ApiClient("http://x", "svc"))
        help_text = "\n".join(body for _n, body in bot.help_fields())
        for cmd in b.tree.get_commands():
            if cmd.name == "ajuda":
                continue
            self.assertIn(f"/{cmd.name}", help_text,
                          f"/{cmd.name} não está no /ajuda")


class BuildImageFileTests(unittest.TestCase):
    """build_image_file anexa o PNG do loadout. O import de discord é lazy no
    módulo (só dentro de função), então a helper precisa reimportar — senão dá
    NameError, que quebrou o /builds em produção. Estes testes exercem o caminho
    de verdade (os testes antigos nunca chamavam a helper)."""

    def test_retorna_discord_file_do_png(self):
        import json
        from tools import discord_bot as bot
        b = json.load(open("data/builds.json", encoding="utf-8"))["builds"][0]
        f = bot.build_image_file(b, 0)
        self.assertIsNotNone(f)                 # PNG existe em web/builds/
        self.assertEqual(f.filename, "build0.png")

    def test_sem_imagem_retorna_none(self):
        from tools import discord_bot as bot
        self.assertIsNone(bot.build_image_file({}, 0))


class CraftRefinarHandlerTests(unittest.TestCase):
    """/craftar e /refinar: formatação da resposta sem tocar campo inexistente.

    Ambos são LOCAIS (rodam no processo do bot, sem plataforma), então o teste
    injeta a via LOCAL — injetar só uma API falsa faria o handler bater na REDE
    de verdade (armadilha documentada no CLAUDE.md: a suíte pulou p/ 71s)."""

    class _Morta:
        async def get(self, *a, **k):
            raise AssertionError("handler chamou a plataforma (era p/ ser LOCAL)")

    def _com_craft(self, payload, termo="barra de aço"):
        async def _f(item_id, premium=True, focus=False):
            return payload
        orig = bot.local_craft_data
        try:
            bot.local_craft_data = _f
            return asyncio.run(bot.handle_craftar(self._Morta(), termo))
        finally:
            bot.local_craft_data = orig

    def _com_refine(self, rows, tier=None):
        async def _f(t=None, premium=True, limit=40):
            return [r for r in rows
                    if not t or str(r.get("item_id", "")).startswith(f"T{t}_")]
        orig = bot.local_refine_rows
        try:
            bot.local_refine_rows = _f
            return asyncio.run(bot.handle_refinar(self._Morta(), tier))
        finally:
            bot.local_refine_rows = orig

    def test_craftar_veredito_e_lista_de_compras(self):
        out = self._com_craft({
            "item": {"id": "T4_METALBAR", "name_pt": "Barra de Aço",
                     "tier": 4, "ench": 0},
            "rows": [{
                "craft_city": "Thetford", "is_bonus_city": True,
                "rrr_pct": 36.7, "materials": 1000, "revenue": 1500,
                "sell_city": "Caerleon", "margin": 500, "margin_pct": 45.0,
                "sourcing": [{"id": "T4_ORE", "count": 2,
                              "buy_city": "Thetford", "unit_price": 200,
                              "name_pt": "Minério de Estanho"}],
            }],
        })
        self.assertIn("Barra de Aço", out)
        self.assertIn("VALE A PENA", out)          # margem > 0
        self.assertIn("cidade-bônus", out)
        self.assertIn("Minério de Estanho", out)   # lista de compras em PT

    def test_craftar_margem_negativa(self):
        out = self._com_craft(
            {"item": {"id": "T4_X", "name_pt": "Item X", "tier": 4, "ench": 0},
             "rows": [{"craft_city": "Lymhurst", "rrr_pct": 20.0,
                       "materials": 900, "revenue": 800,
                       "sell_city": "Lymhurst", "margin": -100,
                       "margin_pct": -11.0, "sourcing": []}]})
        self.assertIn("NÃO compensa", out)

    def test_craftar_sem_rows(self):
        out = self._com_craft(
            {"item": {"id": "T4_X", "name_pt": "Item X"}, "rows": []})
        self.assertIn("Item X", out)
        self.assertIn("sem cotação", out)

    def test_refinar_ranking_e_filtro_tier(self):
        out = self._com_refine([
            {"item_id": "T4_METALBAR", "city": "Thetford", "margin": 300,
             "premium_pct": 20.0, "rrr_pct": 36.7, "name_pt": "Barra de Aço"},
            {"item_id": "T5_METALBAR", "city": "Thetford", "margin": -50,
             "premium_pct": -3.0, "rrr_pct": 36.7, "name_pt": "Barra T5"},
        ], tier=4)
        self.assertIn("Barra de Aço", out)
        self.assertIn("REFINAR", out)
        self.assertNotIn("Barra T5", out)         # filtro tier=4 exclui o T5

    def test_refinar_sem_dados(self):
        out = self._com_refine([])
        self.assertIn("Sem cotação de refino", out)


class AdvancedHandlerTests(unittest.TestCase):
    """Handlers das 10 análises avançadas. Fixtures batem 1:1 com o contrato REAL
    de cada endpoint (workflow map-endpoint-contracts) — pega erro de nome de
    campo, None e formatação ANTES do deploy. Também exercita o caminho VAZIO
    (killboard/cache frio) que é o estado comum no piloto."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_guild_makeorbuy(self):
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/guild" and params["view"] == "makeorbuy"
                return {"view": "makeorbuy", "rows": [
                    {"item_id": "T5_POTION", "demand_units": 300, "internal_cost": 800,
                     "internal_city": "Thetford", "market_price": 1200, "save_pct": 33,
                     "verdict": "fazer", "name_pt": "Poção de Cura"}]}
        out = self._run(bot.handle_guild(Api(), "fabricar"))
        self.assertIn("Poção de Cura", out)
        self.assertIn("FAZER", out)
        self.assertIn("economia 33%", out)

    def test_guild_watch(self):
        class Api:
            async def get(self, path, params=None):
                return {"watched_count": 0, "view": "watch", "rows": [
                    {"item_id": "T6_HEAD", "destroyed": 42, "active_days": 3,
                     "price": 15000, "score": 630, "name_pt": "Capuz do Mestre"}]}
        out = self._run(bot.handle_guild(Api(), "destruicao"))
        self.assertIn("Capuz do Mestre", out)
        self.assertIn("42 destruídos", out)

    def test_guild_kit_and_empty(self):
        class KitApi:
            async def get(self, path, params=None):
                return {"view": "kit",
                        "basket": [{"item_id": "T6_2H", "name_pt": "Machado",
                                    "weight": 0.4}],
                        "series": [{"day": "2026-07-01", "cost": 500000, "index": 100},
                                   {"day": "2026-07-08", "cost": 550000, "index": 110}]}
        out = self._run(bot.handle_guild(KitApi(), "regear"))
        self.assertIn("Machado", out)
        self.assertIn("110", out)              # índice mais recente

        class Empty:
            async def get(self, path, params=None):
                return {"view": "makeorbuy", "rows": []}
        self.assertIn("killboard",
                      self._run(bot.handle_guild(Empty(), "fabricar")).lower())

    def test_foco(self):
        class Api:
            async def get(self, path, params=None):
                assert params["view"] == "focus"
                return {"view": "focus", "rows": [
                    {"item_id": "T5_METALBAR", "city": "Thetford",
                     "silver_per_focus": 12.5, "focus_gain": 3000, "focus": 200,
                     "is_refining": True, "name_pt": "Barra de Aço", "tipo": "refino"}]}
        out = self._run(bot.handle_foco(Api()))
        self.assertIn("Barra de Aço", out)
        self.assertIn("12.5/foco", out)
        self.assertIn("refino", out)

    def test_demanda_burn_e_quality(self):
        class Burn:
            async def get(self, path, params=None):
                assert params["view"] == "burn"
                return {"view": "burn", "rows": [
                    {"item_id": "T5_POTION", "burned_units": 700, "per_day": 100.0,
                     "market_vol_day": 50.0, "coverage": 0.5, "silver_per_day": 40000,
                     "undersupplied": True, "name_pt": "Poção de Cura"}]}
        out = self._run(bot.handle_demanda(Burn(), "consumo"))
        self.assertIn("Poção de Cura", out)
        self.assertIn("pouca oferta", out)

        class Qual:
            async def get(self, path, params=None):
                assert params["view"] == "quality"
                return {"view": "quality", "rows": [
                    {"item_id": "T6_2H", "destroyed": 30, "share_q4plus_pct": 45.0,
                     "ev_quality_premium": 1.35, "dominant_q": 2, "name_pt": "Machado"}]}
        out2 = self._run(bot.handle_demanda(Qual(), "qualidade"))
        self.assertIn("Q4+ 45.0%", out2)

    def test_escanear(self):
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/scan" and params["cat"] == "weapons"
                return {"items_scanned": 100, "items_total": 100, "opportunities": [
                    {"item_id": "T6_2H_BOW", "name_pt": "Arco", "tier": 6, "ench": 1,
                     "buy_city": "Martlock", "sell_city": "Caerleon", "profit": 25000,
                     "roi_pct": 18.0, "flip_score": 20000,
                     "buy_age_min": 30, "sell_age_min": 90}]}
        out = self._run(bot.handle_escanear(Api(), "weapons", 6))
        self.assertIn("Arco", out)
        self.assertIn("Martlock", out)
        self.assertIn("retorno 18.0%", out)         # ROI traduzido
        self.assertIn("preço visto", out)          # idade do dado visível

    def test_recomendar_mostra_idade_do_dado(self):
        # regressão do bug do Callisto: /recomendar tem que mostrar há quanto
        # tempo o preço de cada ponta foi coletado.
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/recommendations"
                return {"opportunities": [
                    {"item_id": "T4_BAG", "name_pt": "Bolsa", "buy_city": "Lymhurst",
                     "sell_city": "Martlock", "profit": 1500, "roi_pct": 12.0,
                     "buy_age_min": 20, "sell_age_min": 200}]}
        out = self._run(bot.handle_recomendar(Api()))
        self.assertIn("Bolsa", out)
        self.assertIn("preço visto", out)
        self.assertIn("compra", out)
        self.assertIn("venda", out)

    def test_logistica_tres_views(self):
        class BM:
            async def get(self, path, params=None):
                assert params["view"] == "bm"
                return {"view": "bm", "rows": [
                    {"item_id": "T6_2H", "name_pt": "Machado", "quality": 2,
                     "bm_net": 90000, "best_city": "Caerleon", "best_city_net": 70000,
                     "premium_abs": 20000, "premium_pct": 28.5}]}
        self.assertIn("+28.5%",
                      self._run(bot.handle_logistica(BM(), "mercadonegro")))

        class Ladder:
            async def get(self, path, params=None):
                assert params["view"] == "ladder"
                return {"view": "ladder", "rows": [
                    {"item_id": "T6_2H", "name_pt": "Machado", "city": "Thetford",
                     "qualities": [1, 2, 3], "best_step": "q1->q2",
                     "best_premium_pct": 15.0, "best_premium_abs": 5000,
                     "quals": "1,2,3"}]}
        self.assertIn("q1->q2",
                      self._run(bot.handle_logistica(Ladder(), "qualidade")))

        class Restock:
            async def get(self, path, params=None):
                return {"view": "restock", "rows": []}
        self.assertIn("reposição",
                      self._run(bot.handle_logistica(Restock(), "reposicao")).lower())

    def test_lab_e_cold(self):
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/item-analysis"
                return {"item": {"id": "T4_BAG", "pt": "Bolsa", "tier": 4, "ench": 0},
                        "series": [
                            {"item_id": "T4_BAG", "city": "Caerleon", "quality": 1,
                             "points": 20, "vwap": 1000, "latest_price": 1100,
                             "robust_z": 1.5, "price_percentile": 80.0,
                             "momentum_pct": 5.0, "avg_daily_volume": 50,
                             "interpretation": {"stance": "evitar entrada cara",
                                                "notes": []}}],
                        "comparison": {"cheapest_city": "Lymhurst", "cheapest_vwap": 900,
                                       "vwap_spread_pct": 20.0}}
        out = self._run(bot.handle_lab(Api(), "bolsa"))
        self.assertIn("Bolsa", out)
        self.assertIn("evitar entrada cara", out)
        self.assertIn("Lymhurst", out)

        class Cold:
            async def get(self, path, params=None):
                return {"item": {"id": "T4_BAG", "pt": "Bolsa"},
                        "series": [{"item_id": "T4_BAG", "city": "Caerleon",
                                    "quality": 1, "points": 0,
                                    "interpretation": {"stance": "sem dados"}}],
                        "comparison": {}}
        self.assertIn("sem histórico", self._run(bot.handle_lab(Cold(), "bolsa")))

    def test_origem_e_vazio(self):
        # /origem agora é LOCAL (lê o dump do disco, sem plataforma): o teste
        # injeta a fonte local, não uma API falsa.
        orig = bot.local_origem_data
        try:
            bot.local_origem_data = lambda item: {
                "item": {"id": "T5_2H_BOW", "name_pt": "Arco"},
                "sources": [{"mob": "T5_MOB_DEMON_VETERAN_BOSS", "tier": 5,
                             "fame": 12000, "cat": "boss"}]}
            out = self._run(bot.handle_origem(None, "arco"))
            self.assertIn("Demon Veteran Boss", out)   # mob id limpo

            bot.local_origem_data = lambda item: {
                "item": {"id": "T5_METALBAR", "name_pt": "Barra"},
                "sources": []}
            self.assertIn("não vem de mob",
                          self._run(bot.handle_origem(None, "barra")))
        finally:
            bot.local_origem_data = orig

    def test_micro(self):
        class Api:
            async def get(self, path, params=None):
                assert params["view"] == "spread"
                return {"view": "spread", "rows": [
                    {"item_id": "T4_BAG", "city": "Martlock", "quality": 1,
                     "sell_price_min": 1200, "buy_price_max": 1000, "spread_gross": 200,
                     "net_per_unit": 120.0, "net_pct": 12.0, "age_min": 10,
                     "liquidity_day": 40, "potential_day": 960, "name_pt": "Bolsa"}]}
        out = self._run(bot.handle_micro(Api()))
        self.assertIn("Bolsa", out)
        self.assertIn("12.0%", out)

    def test_risco_profile_e_corr(self):
        class Prof:
            async def get(self, path, params=None):
                assert params["view"] == "profile"
                return {"view": "profile", "rows": [
                    {"item_id": "T4_BAG", "name_pt": "Bolsa", "city": "Caerleon",
                     "risk_label": "seguro", "vol_shrunk_pct": 25.0, "vol_ci": "20–30",
                     "max_drawdown_pct": -15.0, "var_1d_pct": -5.0}]}
        out = self._run(bot.handle_risco(Prof(), "perfil"))
        self.assertIn("seguro", out)
        self.assertIn("IC 20–30", out)

        class Corr:
            async def get(self, path, params=None):
                assert params["view"] == "corr"
                return {"view": "corr", "rows": [
                    {"a": "T4_ORE", "b": "T4_METALBAR", "corr": 0.85, "common_days": 30,
                     "a_pt": "Minério", "b_pt": "Barra", "tipo": "andam juntos"}]}
        out2 = self._run(bot.handle_risco(Corr(), "correlacao"))
        self.assertIn("Minério × Barra", out2)
        self.assertIn("andam juntos", out2)

    def test_produzir_e_indisponivel(self):
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/prodchain/plan"
                assert params["qty"] == 20
                return {"item": {"id": "T5_2H_AXE", "name_pt": "Machado Grande",
                                 "tier": 5, "enchant": 0},
                        "qty": 20, "available": True,
                        "shopping": [
                            {"id": "T5_PLANKS", "name_pt": "Tábuas de Pinho",
                             "qty": 320, "unit": 250, "cost": 80000,
                             "city": "Fort Sterling", "priced": True},
                            {"id": "T5_METALBAR", "name_pt": "Barra de Aço",
                             "qty": 160, "unit": 300, "cost": 48000,
                             "city": "Thetford", "priced": True}],
                        "buy_cost": 128000, "focus_points": 0,
                        "revenue": 200000, "profit": 72000, "roi_pct": 56.3,
                        "missing_sell": []}
        out = self._run(bot.handle_produzir(Api(), "machado grande", 20))
        self.assertIn("Produzir 20× Machado Grande", out)
        self.assertIn("Tábuas de Pinho", out)
        self.assertIn("LUCRO", out)
        self.assertIn("retorno 56.3%", out)

        class NoRecipe:
            async def get(self, path, params=None):
                return {"item": {"id": "T4_ORE", "name_pt": "Minério"},
                        "qty": 1, "available": False}
        self.assertIn("sem plano de produção",
                      self._run(bot.handle_produzir(NoRecipe(), "minério", 1)))

    def test_sinais_e_indisponivel(self):
        class Api:
            async def get(self, path, params=None):
                if path == "/api/search":
                    return [{"id": "T6_2H_BOW", "pt": "Arco Longo", "tier": 6,
                             "ench": 0}]
                assert path == "/api/item_signals"
                return {"item_id": "T6_2H_BOW", "available": True, "city": "Caerleon",
                        "points": 90,
                        "risk": {"risk_label": "médio", "vol_annual_pct": 40.0},
                        "reversion": {"direction": "comprar", "target": 100000,
                                      "gap_pct": -12.0, "signal": True},
                        "regime": {"significant": True, "kind": "nível"},
                        "predictability": {"label": "modelável",
                                           "predictability": 0.7}}
        out = self._run(bot.handle_sinais(Api(), "arco longo"))
        self.assertIn("Arco Longo", out)
        self.assertIn("comprar", out)
        self.assertIn("modelável", out)

        class NoHist:
            async def get(self, path, params=None):
                if path == "/api/search":
                    return [{"id": "T6_2H_BOW", "pt": "Arco Longo", "tier": 6,
                             "ench": 0}]
                return {"item_id": "T6_2H_BOW", "available": False,
                        "note": "sem historico"}
        self.assertIn("sem histórico",
                      self._run(bot.handle_sinais(NoHist(), "arco longo")))


class KillfeedBotTests(unittest.TestCase):
    """Mural de Conquistas: embed celebra a vitória; comandos configuram."""

    def test_embed_data_celebra_vitoria(self):
        kill = {
            "killer": "Hedon", "victim": "Bogul", "victim_guild": "Inimigos",
            "victim_alliance": "ALLY", "killer_weapon_pt": "Espada Larga",
            "fame": 123456, "kill_area": "MIST", "killer_ip": 1300,
            "victim_ip": 1250, "guildmates": ["Alba", "Cid"],
            "killer_weapon_icon": "https://render.albiononline.com/v1/item/x.png",
        }
        title, desc, thumb, color = bot.killfeed_embed_data(kill)
        self.assertIn("Hedon", title)
        self.assertIn("abateu", title)
        self.assertIn("Bogul", title)
        # nunca fala de morte de membro; mostra arma, local PT e companheiros
        self.assertNotIn("morreu", (title + desc).lower())
        self.assertIn("Espada Larga", desc)
        self.assertIn("Brumas", desc)              # MIST -> Brumas
        self.assertIn("Alba", desc)
        self.assertEqual(thumb, kill["killer_weapon_icon"])

    def test_embed_data_campos_faltando_nao_quebram(self):
        title, desc, thumb, color = bot.killfeed_embed_data(
            {"killer": "X", "victim": "Y"})
        self.assertIn("X", title)
        self.assertIsNone(thumb)

    def test_mural_guilda_confirma(self):
        class Api:
            async def post(self, path, json=None):
                assert json["action"] == "add_guild"
                return {"ok": True, "channel_id": "42",
                        "guilds": ["Operarius"], "active": True}
        out = asyncio.run(bot.handle_mural_guilda(Api(), 1, "operarius"))
        self.assertIn("Operarius", out)
        self.assertNotIn("Falta o canal", out)     # já tem canal

    def test_mural_guilda_sem_canal_avisa(self):
        class Api:
            async def post(self, path, json=None):
                return {"ok": True, "channel_id": None, "guilds": ["Operarius"]}
        out = asyncio.run(bot.handle_mural_guilda(Api(), 1, "operarius"))
        self.assertIn("Falta o canal", out)

    def test_mural_status_pronto(self):
        class Api:
            async def get(self, path, params=None):
                return {"channel_id": "42", "active": True,
                        "guilds": ["Operarius"], "min_fame": 0}
        out = asyncio.run(bot.handle_mural_status(Api(), 1))
        self.assertIn("Operarius", out)
        self.assertIn("ligado", out)


class LocalItemSearchTests(unittest.TestCase):
    """Autocomplete local: nunca depende da API (incidente jul/2026)."""

    def test_busca_basica_pt(self):
        hits = bot.local_item_search("bolsa t4")
        self.assertTrue(hits, "items_db.json deve estar no repo e achar 'bolsa'")
        for it in hits:
            self.assertEqual(it.get("tier"), 4)
            self.assertIn("bolsa", bot._norm_txt(it.get("pt")))
            self.assertFalse(it.get("ench"))     # sem @N: só variante base

    def test_filtro_de_encanto(self):
        hits = bot.local_item_search("bolsa 4.1")
        self.assertTrue(hits)
        for it in hits:
            self.assertEqual(it.get("tier"), 4)
            self.assertEqual(it.get("ench"), 1)

    def test_termo_sem_resultado(self):
        self.assertEqual(bot.local_item_search("zzzznaoexiste"), [])

    def test_acentos_ignorados(self):
        # "espada" deve achar independente de acento no nome PT
        hits = bot.local_item_search("espada larga t5")
        self.assertTrue(hits)


class VisitorGateTests(unittest.TestCase):
    """Portão do visitante: fora do anel interno, só a vitrine funciona."""

    def test_visitante_so_usa_a_vitrine(self):
        # sem cargo nenhum (ou só @Visitante): vitrine liberada, resto não
        for cmd in ("preco", "flip", "recomendar", "ajuda"):
            self.assertTrue(bot.command_allowed_for(cmd, []))
            self.assertTrue(bot.command_allowed_for(cmd, ["Visitante"]))
        for cmd in ("builds", "craftar", "quadro", "fama", "historico",
                    "reportar", "mural-canal", "organizar-servidor"):
            self.assertFalse(bot.command_allowed_for(cmd, []))
            self.assertFalse(bot.command_allowed_for(cmd, ["Visitante"]))

    def test_anel_interno_usa_tudo(self):
        for papel in ("Aprendiz", "Oficial", "Mestre"):
            for cmd in ("builds", "fama", "quadro", "flip", "mural-canal"):
                self.assertTrue(bot.command_allowed_for(cmd, [papel]))

    def test_mensagem_do_bloqueio_vende_o_peixe(self):
        # convida pro recrutamento e cita as ferramentas liberadas
        for t in ("/preco", "/flip", "/recomendar", "#recrutamento"):
            self.assertIn(t, bot.VISITOR_BLOCK_MSG)

    def test_categorias_antigas_viram_internas(self):
        self.assertIn("📊 FERRAMENTAS", bot.LEGACY_INTERNAL_CATS)
        self.assertIn("💬 COMUNIDADE", bot.LEGACY_INTERNAL_CATS)
        self.assertIn("Canais de voz", bot.LEGACY_INTERNAL_CATS)
        # a ENTRADA e o COMECE AQUI ficam públicos (o funil precisa deles)
        self.assertNotIn("📢 ENTRADA", bot.LEGACY_INTERNAL_CATS)
        self.assertNotIn("📖 COMECE AQUI", bot.LEGACY_INTERNAL_CATS)


class FlipRotaSeguraTests(unittest.TestCase):
    """/flip: rota segura por padrão — Caerleon só quando pedido."""

    class _Api:
        def __init__(self):
            self.params = None

        async def get(self, path, params=None):
            self.params = params
            return {"shopping_list": [], "summary": {}}

    def test_segura_redireciona_caerleon(self):
        api = self._Api()
        out = asyncio.run(bot.handle_flip(api, 500000, "Caerleon", "segura"))
        self.assertEqual(api.params["city"], bot.DEFAULT_SAFE_CITY)
        self.assertEqual(api.params.get("safe_routes"), "true")
        self.assertIn("troquei a compra", out)
        self.assertIn("SEGURA", out)

    def test_segura_respeita_cidade_da_periferia(self):
        api = self._Api()
        out = asyncio.run(bot.handle_flip(api, 500000, "Martlock", "segura"))
        self.assertEqual(api.params["city"], "Martlock")
        self.assertEqual(api.params.get("safe_routes"), "true")
        self.assertNotIn("troquei", out)

    def test_todas_mantem_caerleon_sem_filtro(self):
        api = self._Api()
        out = asyncio.run(bot.handle_flip(api, 500000, "Caerleon", "todas"))
        self.assertEqual(api.params["city"], "Caerleon")
        self.assertNotIn("safe_routes", api.params)
        self.assertIn("TODAS", out)


class FamaHandlerTests(unittest.TestCase):
    """/fama: fama por atividade em PT, com fallback pro nick registrado."""

    _FAMA = {"found": True, "name": "olegislador", "guild": "Operarius",
             "kill_fame": 1234567, "pve_total": 171159341, "pve_mists": 500000,
             "pve_corrupted": 0, "gather_total": 2500000,
             "gather": {"minerio": 1200000, "madeira": 800000, "fibra": 300000,
                        "couro": 150000, "pedra": 50000},
             "craft_total": 3188642383, "fishing": 0, "farming": 42000,
             "crystal": 0, "stats_at": "2026-07-17T03:19:32Z"}

    def test_fama_completa(self):
        # COM nick, /fama vai DIRETO no killboard (sem plataforma): injeta a
        # via local pra o teste seguir hermético (sem rede).
        async def _local(nick):
            assert nick == "olegislador"
            return FamaHandlerTests._FAMA
        orig = bot.local_fama_data
        try:
            bot.local_fama_data = _local
            out = asyncio.run(bot.handle_fama(None, 1, "olegislador"))
        finally:
            bot.local_fama_data = orig
        self.assertIn("olegislador", out)
        self.assertIn("Operarius", out)
        self.assertIn("1,2M", out)          # kill fame legível
        self.assertIn("171,2M", out)        # PvE total
        self.assertIn("3,19B", out)         # craft em bilhões
        self.assertIn("Minério", out)
        self.assertIn("2026-07-17", out)    # data dos dados
        self.assertNotIn("Pesca", out)      # zero não polui

    def test_nao_achado_sugere(self):
        # com nick vai pela via LOCAL: injeta (senão o teste bate na rede)
        async def _local(nick):
            return {"found": False, "candidatos": ["olegislador"]}
        orig = bot.local_fama_data
        try:
            bot.local_fama_data = _local
            out = asyncio.run(bot.handle_fama(None, 1, "olegisladorr"))
        finally:
            bot.local_fama_data = orig
        self.assertIn("Não achei", out)
        self.assertIn("olegislador", out)

    def test_sem_registro_orienta(self):
        class Api:
            async def get(self, path, params=None):
                return {"found": False, "sem_registro": True}
        out = asyncio.run(bot.handle_fama(Api(), 1))
        # sem vínculo: aponta o fluxo do recrutador + o atalho de consulta
        self.assertIn("registrar-membro", out)
        self.assertIn("/fama nick:", out)

    def test_fmt_escala(self):
        self.assertEqual(bot._fama_fmt(999), "999")
        self.assertEqual(bot._fama_fmt(1_500_000), "1,5M")
        self.assertEqual(bot._fama_fmt(2_340_000_000), "2,34B")


class SaudeHandlerTests(unittest.TestCase):
    """/saude: o estado da plataforma em linguagem de gente."""

    def test_tudo_certo(self):
        class Api:
            async def get(self, path, params=None):
                return {"ok": True, "db_ok": True, "db_mb": 180.0,
                        "db_mode": "ok", "coleta_ok": True, "sweep_age_s": 45,
                        "intel_age_s": 300, "vigia": "standby",
                        "uptime_s": 7200, "backend": "postgres"}
        out = asyncio.run(bot.handle_saude(Api()))
        self.assertIn("Tudo certo", out)
        self.assertIn("✅ ativa", out)
        self.assertIn("180 MB", out)
        self.assertIn("prontidão", out)

    def test_coleta_parada_avisa(self):
        class Api:
            async def get(self, path, params=None):
                return {"ok": False, "db_ok": True, "db_mb": 200.0,
                        "db_mode": "ok", "coleta_ok": False,
                        "sweep_age_s": 7200, "intel_age_s": None,
                        "vigia": "ativo", "uptime_s": 60,
                        "backend": "postgres"}
        out = asyncio.run(bot.handle_saude(Api()))
        self.assertIn("⚠️", out)
        self.assertIn("parada", out)
        self.assertIn("ASSUMIU", out)   # vigia segurando a coleta

    def test_api_morta_nao_quebra(self):
        class Api:
            async def get(self, path, params=None):
                raise RuntimeError("down")
        out = asyncio.run(bot.handle_saude(Api()))
        self.assertIn("🔴", out)


class ServerLayersTests(unittest.TestCase):
    """Camadas: Visitante fora; Aprendiz é o 1º nível DENTRO da guild."""

    def test_papeis_dos_mesteres(self):
        nomes = [n for n, _c, _d in bot.SERVER_ROLES]
        for esperado in ("Visitante", "Aprendiz", "Oficial", "Mestre"):
            self.assertIn(esperado, nomes)
        # Visitante NÃO enxerga o interno; Aprendiz enxerga (é de dentro)
        self.assertNotIn("Visitante", bot.RING_INTERNO)
        self.assertIn("Aprendiz", bot.RING_INTERNO)
        # comando é só da oficialidade
        self.assertEqual(set(bot.RING_STAFF), {"Oficial", "Mestre"})
        self.assertNotIn("Aprendiz", bot.RING_STAFF)

    def test_plano_tem_os_tres_aneis(self):
        rings = [b["ring"] for b in bot.SERVER_PLAN]
        self.assertEqual(rings, ["publico", "interno", "staff"])
        # o Mural (#conquistas) mora na área interna
        interno = next(b for b in bot.SERVER_PLAN if b["ring"] == "interno")
        self.assertIn("conquistas", [c["name"] for c in interno["channels"]])
        # #recrutamento fica no PÚBLICO (é onde o Visitante se candidata)
        pub = next(b for b in bot.SERVER_PLAN if b["ring"] == "publico")
        self.assertIn("recrutamento", [c["name"] for c in pub["channels"]])

    def test_resumo_menciona_cargos_e_categorias(self):
        s = bot.server_plan_summary()
        for t in ("@Visitante", "@Aprendiz", "@Oficial", "@Mestre",
                  "ENTRADA", "GUILDA", "COMANDO", "#conquistas"):
            self.assertIn(t, s)

    def test_registrar_membro_resolvido(self):
        class Api:
            async def post(self, path, json=None):
                assert json["char_name"] == "olegislador"
                assert json["discord_user_id"] == 42
                return {"ok": True, "char_name": "olegislador", "char_id": "P1",
                        "resolved": True, "guild": "Operarius"}
        out = asyncio.run(bot.handle_registrar_membro(
            Api(), 42, "olegislador", "Hedon"))
        self.assertIn("olegislador", out)
        self.assertIn("Operarius", out)
        self.assertIn("Hedon", out)

    def test_registrar_membro_nao_achado_avisa(self):
        class Api:
            async def post(self, path, json=None):
                return {"ok": True, "char_name": "calixta00", "char_id": None,
                        "resolved": False, "candidates": ["calixta007"]}
        out = asyncio.run(bot.handle_registrar_membro(
            Api(), 7, "calixta00", "Calixxxtonha"))
        self.assertIn("calixta00", out)
        self.assertIn("killboard", out)

    def test_personagem_e_so_consulta(self):
        class Api:
            async def get(self, path, params=None):
                return {"char_name": "olegislador", "char_id": "P1",
                        "found": True}
        out = asyncio.run(bot.handle_personagem(Api(), 1))
        self.assertIn("olegislador", out)
        # a consulta aponta pro fluxo do recrutador, não pro autosserviço
        self.assertIn("registrar-membro", out)

    def test_personagem_sem_vinculo_orienta_staff(self):
        class Api:
            async def get(self, path, params=None):
                return {"char_name": None, "char_id": None, "found": False}
        out = asyncio.run(bot.handle_personagem(Api(), 1))
        self.assertIn("oficial", out.lower())

    def test_vinculo_e_ato_do_recrutador(self):
        # staff vincula; Aprendiz/Visitante NÃO (decisão do usuário: o
        # recrutador confere o nick com share de tela no recrutamento)
        self.assertTrue(bot.is_recruiter(["Oficial"]))
        self.assertTrue(bot.is_recruiter(["Mestre"]))
        self.assertTrue(bot.is_recruiter([], is_owner=True))
        self.assertTrue(bot.is_recruiter([], is_admin=True))
        self.assertFalse(bot.is_recruiter(["Aprendiz"]))
        self.assertFalse(bot.is_recruiter(["Visitante"]))
        self.assertFalse(bot.is_recruiter([]))


class SemPlataformaTests(unittest.TestCase):
    """DOUTRINA (decisão do usuário, jul/2026): o que NÃO precisa do banco
    acumulado roda LOCAL, no processo do bot. Estes testes provam que o Discord
    continua útil com a PLATAFORMA FORA — a API injetada aqui EXPLODE se alguém
    voltar a acoplar o handler à plataforma."""

    class _Morta:
        """Plataforma caída: qualquer chamada é falha de arquitetura."""

        async def get(self, *a, **k):
            raise AssertionError("handler chamou a plataforma (era p/ ser LOCAL)")

        async def post(self, *a, **k):
            raise AssertionError("handler chamou a plataforma (era p/ ser LOCAL)")

    def test_buscar_sem_plataforma(self):
        out = asyncio.run(bot.handle_buscar(self._Morta(), "bolsa t4"))
        self.assertIn("T4_BAG", out)

    def test_origem_sem_plataforma(self):
        # lê o dump do disco; item de coleta não vem de mob
        out = asyncio.run(bot.handle_origem(self._Morta(), "T4_HIDE"))
        self.assertIn("não vem de mob", out)

    def test_ouro_sem_plataforma(self):
        orig = bot.local_gold_pts

        async def _f(count=48):
            return [{"price": 7000}, {"price": 6000}]
        try:
            bot.local_gold_pts = _f
            out = asyncio.run(bot.handle_ouro(self._Morta()))
            self.assertIn("7.000", out)
        finally:
            bot.local_gold_pts = orig

    def test_fama_com_nick_sem_plataforma(self):
        orig = bot.local_fama_data

        async def _f(nick):
            return FamaHandlerTests._FAMA
        try:
            bot.local_fama_data = _f
            out = asyncio.run(bot.handle_fama(self._Morta(), 1, "olegislador"))
            self.assertIn("olegislador", out)
        finally:
            bot.local_fama_data = orig

    # --- 2ª leva: os comandos que a guilda mais usa também sobrevivem ---
    def _com_precos(self, fn, *a):
        """Roda o handler com a plataforma MORTA e preços locais injetados."""
        async def _f(ids, cities=None, qualities=None):
            return [{"city": "Martlock", "sell_price_min": 1500,
                     "buy_price_max": 1200, "sell_age_min": 10},
                    {"city": "Lymhurst", "sell_price_min": 900,
                     "buy_price_max": 700, "sell_age_min": 20}]
        orig = bot.local_prices_rows
        try:
            bot.local_prices_rows = _f
            return asyncio.run(fn(self._Morta(), *a))
        finally:
            bot.local_prices_rows = orig

    def test_preco_sem_plataforma(self):
        out = self._com_precos(bot.handle_preco, "T4_BAG")
        self.assertIn("Martlock", out)

    def test_comparar_sem_plataforma(self):
        out = self._com_precos(bot.handle_comparar, "T4_BAG")
        self.assertIn("comprar em Lymhurst", out)

    def test_vender_sem_plataforma(self):
        out = self._com_precos(bot.handle_vender, "T4_BAG")
        self.assertIn("MELHOR", out)

    def test_historico_sem_plataforma(self):
        async def _f(ids, cities=None, quality=None, time_scale=24, days=30):
            return [{"item_id": "T4_BAG", "city": "Caerleon", "quality": 1,
                     "data": [{"ts": f"2026-07-{d:02d}T00:00:00",
                               "avg_price": 100 + d, "item_count": 5}
                              for d in range(1, 20)]}]
        orig = bot.local_history_series
        try:
            bot.local_history_series = _f
            res = asyncio.run(bot.handle_historico(self._Morta(), "T4_BAG", 19))
            self.assertIn("Caerleon", res["text"])
        finally:
            bot.local_history_series = orig

    # --- 3ª leva: produção de UM item também cabe no teto da AODP ---
    def test_craftar_sem_plataforma(self):
        async def _f(item_id, premium=True, focus=False):
            return {"item": {"id": item_id, "name_pt": "Espada Larga",
                             "tier": 5, "ench": 0},
                    "rows": [{"craft_city": "Bridgewatch", "rrr_pct": 36.7,
                              "is_bonus_city": True, "materials": 1000,
                              "sell_city": "Martlock", "revenue": 1800,
                              "margin": 800, "margin_pct": 80.0,
                              "sourcing": [{"id": "T5_PLANKS", "count": 20,
                                            "name_pt": "Tábuas de Cedro",
                                            "buy_city": "Lymhurst",
                                            "unit_price": 40}]}]}
        orig = bot.local_craft_data
        try:
            bot.local_craft_data = _f
            out = asyncio.run(bot.handle_craftar(self._Morta(), "T5_MAIN_SWORD"))
            self.assertIn("VALE A PENA", out)
            self.assertIn("Tábuas de Cedro", out)
        finally:
            bot.local_craft_data = orig

    def test_craftar_item_sem_receita_sem_plataforma(self):
        async def _f(item_id, premium=True, focus=False):
            return None                      # recurso bruto: não tem craft
        orig = bot.local_craft_data
        try:
            bot.local_craft_data = _f
            out = asyncio.run(bot.handle_craftar(self._Morta(), "T4_WOOD"))
            self.assertIn("receita de craft", out)
        finally:
            bot.local_craft_data = orig

    def test_refinar_sem_plataforma(self):
        async def _f(tier=None, premium=True, limit=40):
            return [{"item_id": "T4_PLANKS", "name_pt": "Tábuas de Pinho",
                     "city": "Fort Sterling", "margin": 45, "premium_pct": 9.7,
                     "rrr_pct": 36.7}]
        orig = bot.local_refine_rows
        try:
            bot.local_refine_rows = _f
            out = asyncio.run(bot.handle_refinar(self._Morta(), 4))
            self.assertIn("REFINAR", out)
            self.assertIn("Tábuas de Pinho", out)
        finally:
            bot.local_refine_rows = orig

    # --- MODO PONTE: bot de pé SEM plataforma (rodando no PC, nuvem fora) ---
    def test_modo_ponte_explica_em_pt_o_que_depende_da_plataforma(self):
        """O ApiOffline falha RÁPIDO com ApiError; o membro lê uma mensagem PT
        que lista o que ainda funciona, em vez de 'O aplicativo não respondeu'."""
        api = bot.ApiOffline()
        with self.assertRaises(bot.ApiError) as ctx:
            asyncio.run(api.get("/api/recommendations"))
        self.assertEqual(ctx.exception.code, "plataforma_offline")
        msg = bot.friendly_error(ctx.exception)
        self.assertIn("plataforma está fora", msg)
        self.assertIn("/preco", msg)          # aponta o que AINDA funciona
        self.assertIn("/craftar", msg)

    def test_modo_ponte_nao_atrapalha_os_locais(self):
        """Com o ApiOffline no lugar da plataforma, os comandos LOCAIS seguem
        respondendo normalmente (é o ponto do modo ponte)."""
        async def _f(count=48):
            return [{"price": 7000}, {"price": 6000}]
        orig = bot.local_gold_pts
        try:
            bot.local_gold_pts = _f
            out = asyncio.run(bot.handle_ouro(bot.ApiOffline()))
            self.assertIn("7.000", out)
        finally:
            bot.local_gold_pts = orig

    # --- 4ª leva: o Estúdio de Refino (plano/coletor/ranking) é 100% local ---
    # ATENÇÃO: injete a via LOCAL (local_refine_*), NUNCA só a API falsa — sem
    # isso o teste passa a bater na AODP DE VERDADE (a suíte pula de 1s p/ 70s).

    def test_refino_plano_sem_plataforma(self):
        async def _f(fam, tier, **k):
            return {"item_id": "T5_PLANKS", "name_pt": "Tábuas de Cedro",
                    "raw_pt": "Troncos de Cedro", "prev_pt": "Tábuas de Pinho",
                    "city": "Fort Sterling", "is_bonus_city": True,
                    "unit_focus": {"rrr_pct": 53.9, "eff_cost": 610, "margin": 190,
                                   "focus_cost": 94, "silver_per_focus": 2.03,
                                   "raw_id": "T5_WOOD", "raw_count": 3,
                                   "prev_id": "T4_PLANKS", "gross_cost": 1323,
                                   "sell_unit": 875},
                    "unit_plain": {"rrr_pct": 36.7, "eff_cost": 837, "margin": -37,
                                   "raw_id": "T5_WOOD", "raw_count": 3,
                                   "prev_id": "T4_PLANKS", "gross_cost": 1323,
                                   "sell_unit": 875},
                    "phases": [{"phase": "com foco", "qty": 106, "focus_used": 9964,
                                "invested": 66536, "profit": 20182,
                                "margin_unit": 190}],
                    "total": {"qty": 106, "focus_used": 9964, "invested": 66536,
                              "profit": 20182, "roi_pct": 30.3, "raw_used": 146,
                              "prev_used": 49},
                    "limiter": "foco (parada recomendada)", "focus_days": 1.0,
                    "advice": "SÓ é lucrativo COM foco — pare e venda o bruto",
                    "focus_break_even_price": {"com_foco": 457, "sem_foco": 299,
                                               "raw_price_now": 319}}
        orig = bot.local_refine_plan
        try:
            bot.local_refine_plan = _f
            out = asyncio.run(bot.handle_refino(self._Morta(), "wood", 5, 10000))
            self.assertIn("Tábuas de Cedro", out)
            self.assertIn("SÓ é lucrativo COM foco", out)
            self.assertIn("Break-even", out)
        finally:
            bot.local_refine_plan = orig

    def test_refino_estoque_sem_plataforma(self):
        async def _f(fam, stock, **k):
            return {"family": fam, "city": "Fort Sterling",
                    "rows": [{"tier": 4, "refined_id": "T4_PLANKS",
                              "raw_id": "T4_WOOD", "name_pt": "Tábuas de Pinho",
                              "raw_pt": "Troncos de Pinho",
                              "prev_pt": "Tábuas de Castanheira",
                              "made": 948, "made_focus": 0, "raw_used": 1200,
                              "raw_left": 0, "prev_bought": 205, "prev_cost": 51042,
                              "focus_used": 0, "profit": 34507,
                              "bottleneck": "bruto T4", "kept": 948}],
                    "products": [{"item_id": "T4_PLANKS", "qty": 948, "tier": 4}],
                    "profit": 34507, "focus_used": 0, "focus_left": 10000,
                    "silver_spent": 51042, "leftovers": {},
                    "top_refined": "T4_PLANKS", "top_qty": 948}
        orig = bot.local_refine_stock
        try:
            bot.local_refine_stock = _f
            out = asyncio.run(bot.handle_refino_estoque(
                self._Morta(), "wood", 0, 0, 1200, 0, 0, 0, 0, 10000))
            self.assertIn("REFINO DO COLETOR", out)
            self.assertIn("Você fica com", out)
            self.assertIn("Tábuas de Pinho", out)
        finally:
            bot.local_refine_stock = orig

    def test_refino_estoque_vazio_nao_chama_nada(self):
        out = asyncio.run(bot.handle_refino_estoque(self._Morta(), "wood"))
        self.assertIn("recurso BRUTO", out)

    def test_refino_ranking_sem_plataforma(self):
        async def _f(**k):
            return [{"item_id": "T5_METALBAR", "name_pt": "Barra de Aço Titânio",
                     "city": "Thetford", "is_bonus_city": True, "qty": 106,
                     "profit": 40852, "roi_pct": 43.5, "silver_per_focus": 4.1,
                     "focus_gain_per_point": 4.1, "limiter": "foco"}]
        orig = bot.local_refine_ranking
        try:
            bot.local_refine_ranking = _f
            out = asyncio.run(bot.handle_refino_ranking(self._Morta(), 10000))
            self.assertIn("ONDE REFINAR", out)
            self.assertIn("Barra de Aço Titânio", out)
        finally:
            bot.local_refine_ranking = orig


if __name__ == "__main__":
    unittest.main()

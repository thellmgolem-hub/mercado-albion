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
        search = [{"id": "T4_BAG", "pt": "Bolsa", "tier": 4, "ench": 0}]
        prices = [
            {"city": "Caerleon", "sell_price_min": 1000, "buy_price_max": 800},
            {"city": "Martlock", "sell_price_min": 1500, "buy_price_max": 1200},
            {"city": "Lymhurst", "sell_price_min": 900, "buy_price_max": 700},
        ]
        out = asyncio.run(bot.handle_comparar(_FakeApi(search, prices), "bolsa"))
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

    def test_ouro_usa_mais_novo_como_atual(self):
        # /api/gold vem ORDER BY ts DESC (mais NOVO primeiro): pts[0]=agora.
        class GoldApi:
            async def get(self, path, params=None):
                return [{"price": 4000, "ts": "2026-07-14T00:00:00"},   # atual
                        {"price": 3000, "ts": "2026-07-12T00:00:00"}]   # ~48h atrás
        out = asyncio.run(bot.handle_ouro(GoldApi()))
        self.assertIn("4.000", out)        # cotação atual = ponto mais novo
        self.assertIn("subindo", out)      # 4000 vs 3000 -> subiu (sinal correto)

    def test_ouro_preco_zero_nao_quebra(self):
        class GoldApi:
            async def get(self, path, params=None):
                return [{"price": 0, "ts": "2026-07-14T00:00:00"}]
        out = asyncio.run(bot.handle_ouro(GoldApi()))  # sem ZeroDivisionError
        self.assertIn("válida", out)

    def test_historico_grafico_e_estatisticas(self):
        class HistApi:
            async def get(self, path, params=None):
                if path == "/api/search":
                    return [{"id": "T4_BAG", "pt": "Bolsa", "tier": 4, "ench": 0}]
                mk = lambda base, step, cnt: [
                    {"ts": f"2026-07-{d:02d}T00:00:00",
                     "avg_price": base + d * step, "item_count": cnt}
                    for d in range(1, 31)]
                return [{"item_id": "T4_BAG", "city": "Martlock", "quality": 1,
                         "data": mk(900, 5, 10)},
                        {"item_id": "T4_BAG", "city": "Caerleon", "quality": 1,
                         "data": mk(1000, 10, 50)}]
        res = asyncio.run(bot.handle_historico(HistApi(), "bolsa", 30))
        # Caerleon tem mais volume -> vem primeiro no texto e no gráfico
        self.assertTrue(res["text"].startswith("**Caerleon**"))
        self.assertIn("Martlock", res["text"])
        self.assertIn("+", res["text"])            # variação com sinal
        url = res["chart_url"]
        self.assertTrue(url.startswith("https://quickchart.io/chart"))
        self.assertLessEqual(len(url), 2000)       # cabe no limite de URL

    def test_historico_sem_dados(self):
        class EmptyApi:
            async def get(self, path, params=None):
                if path == "/api/search":
                    return [{"id": "T4_BAG", "pt": "Bolsa", "tier": 4, "ench": 0}]
                return []
        res = asyncio.run(bot.handle_historico(EmptyApi(), "bolsa"))
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
    """/craftar e /refinar: formatam a resposta a partir de /api/craft e
    /api/prod?view=refine sem tocar em campo inexistente (contrato do backend)."""

    def test_craftar_veredito_e_lista_de_compras(self):
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/craft", path
                return {
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
                }
        out = asyncio.run(bot.handle_craftar(Api(), "barra de aço"))
        self.assertIn("Barra de Aço", out)
        self.assertIn("VALE A PENA", out)          # margem > 0
        self.assertIn("cidade-bônus", out)
        self.assertIn("Minério de Estanho", out)   # lista de compras em PT

    def test_craftar_margem_negativa(self):
        class Api:
            async def get(self, path, params=None):
                return {"item": {"id": "T4_X", "name_pt": "Item X", "tier": 4,
                                 "ench": 0},
                        "rows": [{"craft_city": "Lymhurst", "rrr_pct": 20.0,
                                  "materials": 900, "revenue": 800,
                                  "sell_city": "Lymhurst", "margin": -100,
                                  "margin_pct": -11.0, "sourcing": []}]}
        out = asyncio.run(bot.handle_craftar(Api(), "x"))
        self.assertIn("NÃO compensa", out)

    def test_craftar_sem_rows(self):
        class Api:
            async def get(self, path, params=None):
                return {"item": {"id": "T4_X", "name_pt": "Item X"}, "rows": []}
        out = asyncio.run(bot.handle_craftar(Api(), "x"))
        self.assertIn("Item X", out)
        self.assertIn("sem cotação", out)

    def test_refinar_ranking_e_filtro_tier(self):
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/prod", path
                assert (params or {}).get("view") == "refine"
                return {"view": "refine", "rows": [
                    {"item_id": "T4_METALBAR", "city": "Thetford", "margin": 300,
                     "premium_pct": 20.0, "rrr_pct": 36.7,
                     "name_pt": "Barra de Aço"},
                    {"item_id": "T5_METALBAR", "city": "Thetford", "margin": -50,
                     "premium_pct": -3.0, "rrr_pct": 36.7, "name_pt": "Barra T5"},
                ]}
        out = asyncio.run(bot.handle_refinar(Api(), 4))
        self.assertIn("Barra de Aço", out)
        self.assertIn("REFINAR", out)
        self.assertNotIn("Barra T5", out)         # filtro tier=4 exclui o T5

    def test_refinar_sem_dados(self):
        class Api:
            async def get(self, path, params=None):
                return {"view": "refine", "rows": []}
        out = asyncio.run(bot.handle_refinar(Api()))
        self.assertIn("Sem dados de refino", out)


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
        class Api:
            async def get(self, path, params=None):
                assert path == "/api/origin"
                return {"item": {"id": "T5_2H_BOW", "name_pt": "Arco"},
                        "sources": [{"mob": "T5_MOB_DEMON_VETERAN_BOSS", "tier": 5,
                                     "fame": 12000, "cat": "boss"}]}
        out = self._run(bot.handle_origem(Api(), "arco"))
        self.assertIn("Demon Veteran Boss", out)      # mob id limpo

        class Empty:
            async def get(self, path, params=None):
                return {"item": {"id": "T5_METALBAR", "name_pt": "Barra"},
                        "sources": []}
        self.assertIn("não vem de mob",
                      self._run(bot.handle_origem(Empty(), "barra")))

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

    def test_personagem_registra_resolvido(self):
        class Api:
            async def post(self, path, json=None):
                assert json["char_name"] == "olegislador"
                return {"ok": True, "char_name": "olegislador", "char_id": "P1",
                        "resolved": True, "guild": "Operarius"}
        out = asyncio.run(bot.handle_personagem(Api(), 1, "olegislador"))
        self.assertIn("olegislador", out)
        self.assertIn("Operarius", out)

    def test_personagem_nao_achado_avisa(self):
        class Api:
            async def post(self, path, json=None):
                return {"ok": True, "char_name": "calixta00", "char_id": None,
                        "resolved": False, "candidates": ["calixta007"]}
        out = asyncio.run(bot.handle_personagem(Api(), 1, "calixta00"))
        self.assertIn("calixta00", out)
        self.assertIn("killboard", out)

    def test_personagem_sem_nick_mostra_atual(self):
        class Api:
            async def get(self, path, params=None):
                return {"char_name": "olegislador", "char_id": "P1",
                        "found": True}
        out = asyncio.run(bot.handle_personagem(Api(), 1))
        self.assertIn("olegislador", out)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Testes dos helpers de builds do bot (data/builds.json + apresentação PT-BR).

Só funções puras — não sobe o bot nem precisa do discord.py conectado.
"""
import asyncio
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
        self.assertIn("Dawnsong", title)
        self.assertIn("Fogo", title)
        url = bot.weapon_icon_url(dawn)
        self.assertTrue(url.startswith("https://render.albiononline.com/v1/item/"))
        self.assertIn("size=128", url)

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


if __name__ == "__main__":
    unittest.main()

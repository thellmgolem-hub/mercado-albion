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


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Calculador de craft/refino com dados REAIS do jogo.

Usa data/craft_data.json (cidade-bônus e RRR derivados de craftingmodifiers)
e data/recipes_craft.json / recipes_refining.json (insumos + foco do dump).

RRR (taxa de retorno de recursos) = 1 - 1/(1 + bônus_estação + bônus_categoria
[+ bônus_foco]) — o retorno reduz o custo efetivo dos insumos:
custo_efetivo = custo_insumos × (1 - RRR).
"""
import json
from pathlib import Path

from . import config
from .flips import sell_revenue

DATA = Path(__file__).resolve().parent.parent / "data"
_cache = {}


def _load(name):
    if name not in _cache:
        p = DATA / name
        _cache[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _cache[name]


def craft_data():
    return _load("craft_data.json")


def recipe_for(item_id):
    """Receita de craft ou de refino (o que existir) para o item."""
    return (_load("recipes_craft.json").get(item_id)
            or _load("recipes_refining.json").get(item_id))


def craft_rrr(category, city, focus=False):
    """RRR de craft/refino para uma categoria numa cidade (fração 0..1)."""
    cd = craft_data()
    station = cd.get("station_refining_bonus") or 0.18
    bonus = (cd.get("crafting") or {}).get(category, {}).get(city, 0.0)
    s = station + bonus + (cd.get("focus_bonus_sum", 0.59) if focus else 0.0)
    return 1 - 1 / (1 + s)


def bonus_city(category):
    cities = (craft_data().get("crafting") or {}).get(category) or {}
    return max(cities, key=cities.get) if cities else None


def margins(item_id, recipe, price_of, premium=True, sell_mode="order",
            focus=False, cities=None, fee=0):
    """Margem de craft por cidade. price_of(id, city) -> menor venda ou None.

    Compra os insumos e vende o produto na MESMA cidade.
    """
    cities = cities or config.ROYAL_CITIES
    cat = recipe.get("category")
    out = []
    for city in cities:
        cost, ok = 0.0, True
        for inp in recipe["inputs"]:
            p = price_of(inp["id"], city)
            if not p:
                ok = False
                break
            cost += inp["count"] * p
        sell = price_of(item_id, city)
        if not ok or not sell:
            continue
        rrr = craft_rrr(cat, city, focus)
        eff = cost * (1 - rrr) + fee
        margin = sell_revenue(sell, sell_mode, premium) - eff
        foc = recipe.get("focus") or 0
        out.append({
            "city": city,
            "category": cat,
            "materials": round(cost),
            "rrr_pct": round(rrr * 100, 1),
            "is_bonus_city": city == bonus_city(cat),
            "eff_cost": round(eff),
            "sell": sell,
            "margin": round(margin),
            "margin_pct": round(100 * margin / eff, 1) if eff else None,
            "focus": foc,
            "silver_per_focus": round(margin / foc, 1) if foc and focus else None,
        })
    out.sort(key=lambda r: -r["margin"])
    return out

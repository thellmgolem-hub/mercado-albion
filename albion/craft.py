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


def craft_rrr(category, city, focus=False, extra=0.0):
    """RRR de craft/refino para uma categoria numa cidade (fração 0..1).

    extra: bônus aditivo opcional (ex.: ciclo diário +0,10 prata / +0,20 ouro),
    somado À PILHA antes da conversão côncava — nunca multiplicativo.
    """
    cd = craft_data()
    station = cd.get("station_refining_bonus") or 0.18
    bonus = (cd.get("crafting") or {}).get(category, {}).get(city, 0.0)
    s = station + bonus + (cd.get("focus_bonus_sum", 0.59) if focus else 0.0) + extra
    return 1 - 1 / (1 + s)


_REFINED_FAMILY = {"METALBAR": "ore", "PLANKS": "wood", "CLOTH": "fiber",
                   "LEATHER": "hide", "STONEBLOCK": "rock"}


def _refining_family(item_id):
    """Família de refino do material refinado (T4_PLANKS -> 'wood') ou None."""
    base = (item_id or "").split("@")[0]
    for token, fam in _REFINED_FAMILY.items():
        if base.endswith(token):
            return fam
    return None


def unified_rrr(item_id, category, city, focus=False, extra=0.0):
    """RRR que conhece TANTO a especialidade de refino (família, +40% na cidade
    certa) QUANTO o bônus de categoria de craft. Para refinados, craft_rrr sozinho
    ignorava o +40% e dava 15,2% em toda cidade — aqui sai 36,7% na cidade-bônus."""
    fam = _refining_family(item_id)
    if fam:
        cd = craft_data()
        ref = (cd.get("refining") or {}).get(fam) or {}
        station = ref.get("station_bonus", cd.get("station_refining_bonus", 0.18))
        specialty = ref.get("specialty_bonus", 0.40) if city == ref.get("city") else 0.0
        s = station + specialty + (cd.get("focus_bonus_sum", 0.59) if focus else 0.0) + extra
        return 1 - 1 / (1 + s)
    return craft_rrr(category, city, focus, extra)


def unified_bonus_city(item_id, category):
    """Cidade-bônus do item: família de refino ou categoria de craft."""
    fam = _refining_family(item_id)
    if fam:
        return ((craft_data().get("refining") or {}).get(fam) or {}).get("city")
    return bonus_city(category)


def anchor_band(quotes, history_med=None):
    """Banda de preço plausível (piso, teto) p/ derrubar ordens-isca.

    Detecta o SALTO (>8×) entre cotações de cidades ordenadas — funciona mesmo
    quando a maioria das cidades cota âncora (o salto separa o cluster real do
    de isca, independe de quem é maioria). history_med (preço REAL negociado)
    aperta a banda quando disponível. Devolve (piso|None, teto|None)."""
    xs = sorted(p for p in quotes if p and p > 0)
    floor = ceiling = None
    if len(xs) >= 2:
        for i in range(len(xs) - 1, 0, -1):       # teto: maior salto, de cima
            if xs[i] > xs[i - 1] * 8:
                ceiling = xs[i - 1] * 3
                break
        for i in range(len(xs) - 1):              # piso: maior salto, de baixo
            if xs[i + 1] > xs[i] * 8:
                floor = xs[i + 1] * 0.3
                break
    if history_med:
        h_floor, h_ceiling = history_med * 0.2, history_med * 5
        floor = max(floor, h_floor) if floor else h_floor
        ceiling = min(ceiling, h_ceiling) if ceiling else h_ceiling
    return floor, ceiling


def focus_cost(base_focus, spec_fce=0):
    """Custo de foco EFETIVO por craft dado o Focus Cost Efficiency (spec).

    A especialização do jogador NÃO muda o RRR/rendimento — ela barateia o foco:
    custo = base × 0,5^(FCE/10000) (cada 10.000 de FCE corta o foco pela metade).
    Verificado (jun/2026) contra wiki + guias; os PONTOS por nível variam por
    patch, então o FCE é entrada editável, não derivado de nível automaticamente.
    """
    if not base_focus:
        return 0.0
    return base_focus * (0.5 ** (max(spec_fce, 0) / 10000.0))


def bonus_city(category):
    cities = (craft_data().get("crafting") or {}).get(category) or {}
    return max(cities, key=cities.get) if cities else None


def margins(item_id, recipe, price_of, premium=True, sell_mode="order",
            focus=False, cities=None, fee=0, bid_of=None):
    """Margem de craft por cidade. price_of(id, city) -> menor venda (ask) ou None.

    Compra os insumos e vende o produto na MESMA cidade. Em sell_mode='instant',
    usa bid_of(id, city) (maior ordem de compra) — sem ele, pula a cidade em vez
    de superestimar usando o ask. RRR ciente de refino (unified_rrr).
    """
    cities = cities or config.ROYAL_CITIES
    cat = recipe.get("category")
    out_qty = recipe.get("output", 1) or 1   # 1 craft rende N (poção/comida)
    bcity = unified_bonus_city(item_id, cat)
    out = []
    for city in cities:
        cost, ok = 0.0, True
        for inp in recipe["inputs"]:
            p = price_of(inp["id"], city)
            if not p:
                ok = False
                break
            cost += inp["count"] * p
        if sell_mode == "instant":
            sell = bid_of(item_id, city) if bid_of else None
        else:
            sell = price_of(item_id, city)
        if not ok or not sell:
            continue
        rrr = unified_rrr(item_id, cat, city, focus)
        eff = cost * (1 - rrr) + fee
        margin = sell_revenue(sell, sell_mode, premium) * out_qty - eff
        foc = recipe.get("focus") or 0
        out.append({
            "city": city,
            "category": cat,
            "materials": round(cost),
            "rrr_pct": round(rrr * 100, 1),
            "is_bonus_city": city == bcity,
            "eff_cost": round(eff),
            "output": out_qty,
            "sell": sell,
            "margin": round(margin),
            "margin_pct": round(100 * margin / eff, 1) if eff else None,
            "focus": foc,
            "silver_per_focus": round(margin / foc, 1) if foc and focus else None,
        })
    out.sort(key=lambda r: -r["margin"])
    return out


def studio(item_id, recipe, acquire_of, bid_of, *, premium=True,
           sell_mode="order", focus=False, spec_fce=0, focus_budget=None,
           daily_bonus=0.0, station_fee=0, source_cities=None,
           sell_cities=None, same_city=False, band_of=None, limit=None):
    """Estúdio de craft: desacopla COMPRA (cidade mais barata por insumo),
    LOCAL de craft (define o RRR pela cidade-bônus) e VENDA (melhor cidade
    líquida) — o que margins() não faz (trava tudo numa cidade só).

    acquire_of(id, city) -> menor venda q1 (custo p/ comprar instantâneo) | None.
    bid_of(id, city)     -> maior compra q1 (p/ vender instantâneo / Mercado Negro).

    A especialização entra SÓ pelo custo de foco (spec_fce), nunca no RRR.
    Sem same_city, o resultado é TEÓRICO: ignora custo/risco de transporte entre
    a cidade de compra, a de craft e a de venda.
    Devolve uma linha por cidade de craft (RRR varia; a melhor cidade-bônus lidera).
    """
    cat = recipe.get("category")
    base_foc = recipe.get("focus") or 0
    out_qty = recipe.get("output", 1) or 1   # 1 craft rende N (poção/comida = 5/10)
    source_cities = list(source_cities or (list(config.ROYAL_CITIES) + ["Brecilien"]))
    sell_cities = list(sell_cities or config.CITIES)
    bcity = unified_bonus_city(item_id, cat)   # conhece refino E craft
    foc_eff = focus_cost(base_foc, spec_fce) if focus else 0.0
    band_of = band_of or (lambda _i: (None, None))
    bm_ok = cat in config.BLACK_MARKET_CATEGORIES   # BM só compra equip. de combate

    def best_acquire(inp_id):
        floor, _ = band_of(inp_id)               # piso anti-isca (ordem de 1 prata)
        best, bc = None, None
        for c in source_cities:
            p = acquire_of(inp_id, c)
            if not p or (floor and p < floor):
                continue
            if best is None or p < best:
                best, bc = p, c
        return best, bc

    _, out_ceiling = band_of(item_id)            # teto anti-âncora do produto

    def sell_net_at(city):
        """(receita líquida, preço de referência) ao vender o produto em city.

        band_of(item) dá um teto que derruba ordens-âncora que sobrevivem ao
        saneamento entre cidades mesmo quando a MAIORIA cota isca — ancorado no
        preço real negociado (history) e/ou no salto entre cidades."""
        if city == "Black Market" and not bm_ok:
            return (None, None)                  # BM não compra este item
        if city == "Black Market" or sell_mode == "instant":
            p = bid_of(item_id, city)            # bate na ordem de compra
        else:
            p = acquire_of(item_id, city)        # lista pelo menor preço de venda
        if not p or (out_ceiling and p > out_ceiling):
            return (None, None)
        mode = "instant" if (city == "Black Market" or sell_mode == "instant") else "order"
        return (sell_revenue(p, mode, premium), p)

    # melhor venda global (independe do local de craft)
    g_net, g_city, g_ref = None, None, None
    for c in sell_cities:
        net, ref = sell_net_at(c)
        if net is not None and (g_net is None or net > g_net):
            g_net, g_city, g_ref = net, c, ref

    out = []
    for craft_city in source_cities:
        sourcing, cost, ok = [], 0.0, True
        for inp in recipe["inputs"]:
            if same_city:
                p, bc = acquire_of(inp["id"], craft_city), craft_city
            else:
                p, bc = best_acquire(inp["id"])
            if not p:
                ok = False
                break
            cost += inp["count"] * p
            sourcing.append({"id": inp["id"], "count": inp["count"],
                             "buy_city": bc, "unit_price": round(p)})
        if not ok:
            continue
        rrr = unified_rrr(item_id, cat, craft_city, focus, extra=daily_bonus)
        eff = cost * (1 - rrr) + station_fee
        if same_city:
            net, ref = sell_net_at(craft_city)
            s_city, s_ref = craft_city, ref
        else:
            net, s_city, s_ref = g_net, g_city, g_ref
        if net is None:
            continue
        revenue = net * out_qty                     # 1 craft rende out_qty itens
        margin = revenue - eff                       # lucro por CRAFT (lote inteiro)
        crafts_day = (focus_budget / foc_eff) if (focus and foc_eff and focus_budget) else None
        out.append({
            "craft_city": craft_city,
            "is_bonus_city": craft_city == bcity,
            "rrr_pct": round(rrr * 100, 1),
            "materials": round(cost),
            "eff_cost": round(eff),
            "output": out_qty,
            "sourcing": sourcing,
            "sell_city": s_city,
            "sell_unit": round(s_ref) if s_ref else None,
            "sell_net": round(net),
            "revenue": round(revenue),
            "margin": round(margin),
            "margin_pct": round(100 * margin / eff, 1) if eff else None,
            "focus": base_foc,
            "focus_cost_eff": round(foc_eff, 1) if foc_eff else None,
            "silver_per_focus": round(margin / foc_eff, 1) if (focus and foc_eff) else None,
            "crafts_per_day": round(crafts_day) if crafts_day else None,
            "items_per_day": round(crafts_day * out_qty) if crafts_day else None,
            "resource_saved_per_day": round(crafts_day * rrr * cost) if crafts_day else None,
        })
    out.sort(key=lambda r: -r["margin"])
    return out[:limit] if limit else out

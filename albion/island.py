# -*- coding: utf-8 -*-
"""Administração de ILHA: trabalhadores (diários), agricultura, pecuária.

Tudo sobre price_q1 (menor venda q1 por (item, cidade), já saneado de âncoras) +
data/island_data.json (mecânicas reais do dump: foco, ciclo, bônus, prole,
ração/nutrição). Cada motor compara cidades para COMPRAR insumos e VENDER
produtos, calcula lucro/dia e o capital de giro por unidade.

Mecânica do jogo (verificada no items.json):
- Agricultura: plantar semente custa @activefarmfocuscost de FOCO por ciclo; COM
  foco a colheita rende @activefarmbonus× (ex.: cenoura 2×). Ciclo em
  @activefarmcyclelengthseconds. A colheita às vezes devolve semente.
- Pecuária: a cria vira adulto em @growtime; tem chance de PROLE (cria extra).
  O animal CONSOME nutrição ao longo do crescimento (consumption.food); a ração
  vem da categoria aceita ('plants' = culturas). Foco reduz a ração necessária.
- Trabalhadores: comprar diário VAZIO + encher de fama (esforço, sem custo de
  prata) + vender CHEIO = o "salário" que o mercado paga pela fama.
"""
from . import config
from . import craft
from .flips import sell_revenue


def _load():
    return craft._load("island_data.json") or {}


def _best_buy(price_q1, item, cities):
    """(cidade, preço) mais barata para COMPRAR o item q1."""
    best = None
    for c in cities:
        p = price_q1.get((item, c))
        if p and (best is None or p < best[1]):
            best = (c, p)
    return best


def _best_sell(price_q1, item, cities, mode, premium):
    """(cidade, líquido) que MAXIMIZA a venda líquida do item q1."""
    best = None
    for c in cities:
        p = price_q1.get((item, c))
        if not p:
            continue
        net = sell_revenue(p, mode, premium)
        if best is None or net > best[1]:
            best = (c, net)
    return best


def _silver_per_nutrition(price_q1, food, cities):
    """Menor prata-por-nutrição entre as comidas de uma categoria (ração)."""
    best = None
    for item, info in food.items():
        nut = info.get("nutrition") or 0
        if nut <= 0:
            continue
        buy = _best_buy(price_q1, item, cities)
        if not buy:
            continue
        spn = buy[1] / nut
        if best is None or spn < best[0]:
            best = (spn, item, buy[0], buy[1])
    return best   # (prata_por_nutricao, item, cidade, preco_unit) ou None


# ------------------------------------------------------------- agricultura
def crop_economy(price_q1, premium=True, sell_mode="order", cities=None,
                 limit=40):
    """Lucro de canteiro por cultura, COM e SEM foco. price_q1 saneado."""
    data = _load().get("crops") or {}
    if not data:
        return {"available": False, "rows": []}
    cities = cities or config.ROYAL_CITIES
    rows = []
    for seed, info in data.items():
        crop = info["crop"]
        buy = _best_buy(price_q1, seed, cities)
        sell = _best_sell(price_q1, crop, cities, sell_mode, premium)
        cyc_d = (info.get("cycle_s") or 0) / 86400
        if not buy or not sell or cyc_d <= 0:
            continue
        seed_cost = buy[1]
        # semente devolvida reduz o custo efetivo
        regrow = (info.get("seed_regrow_chance") or 0) * (info.get("seed_regrow_amount") or 0)
        net_seed = max(seed_cost * (1 - regrow), 0)
        base_yield = info.get("crop_yield") or 0
        bonus = info.get("focus_bonus") or 1.0
        rev_base = base_yield * sell[1]
        rev_focus = base_yield * bonus * sell[1]
        profit_no = rev_base - net_seed
        profit_focus = rev_focus - net_seed
        rows.append({
            "seed": seed, "crop": crop,
            "buy_city": buy[0], "seed_price": round(seed_cost),
            "sell_city": sell[0], "crop_net": round(sell[1]),
            "cycle_days": round(cyc_d, 2),
            "yield_no_focus": round(base_yield, 1),
            "yield_focus": round(base_yield * bonus, 1),
            "profit_no_focus": round(profit_no),
            "profit_focus": round(profit_focus),
            "per_day_no_focus": round(profit_no / cyc_d),
            "per_day_focus": round(profit_focus / cyc_d),
            "focus_cost": round(info.get("focus_cost") or 0),
            "focus_gain": round(profit_focus - profit_no),   # extra prata pelo foco
            "working_capital": round(seed_cost),             # por canteiro/ciclo
            "tier": info.get("tier"),
        })
    rows.sort(key=lambda r: -max(r["per_day_focus"], r["per_day_no_focus"]))
    return {"available": True, "rows": rows[:limit] if limit else rows,
            "priced": len(rows)}


# ---------------------------------------------------------------- pecuária
def animal_economy(price_q1, premium=True, sell_mode="order", cities=None,
                   limit=40):
    """Lucro de pasto por animal, com ração real e foco (reduz ração)."""
    d = _load()
    data = d.get("animals") or {}
    food = d.get("food") or {}
    if not data:
        return {"available": False, "rows": []}
    cities = cities or config.ROYAL_CITIES
    plants_food = {k: v for k, v in food.items() if v.get("category") == "plants"}
    spn = _silver_per_nutrition(price_q1, plants_food, cities)  # ração mais barata
    rows = []
    for baby, info in data.items():
        grown = info.get("grown")
        gt_d = (info.get("growtime_s") or 0) / 86400
        buy = _best_buy(price_q1, baby, cities)
        sell = _best_sell(price_q1, grown, cities, sell_mode, premium) if grown else None
        if not buy or not sell or gt_d <= 0:
            continue
        # ração: nutrição total consumida no crescimento × prata/nutrição
        spnp = info.get("seconds_per_nutrition") or 0
        total_nut = (info.get("growtime_s") or 0) / spnp if spnp else 0
        feed_no = (total_nut * spn[0]) if spn else 0
        # foco no pasto reduz a ração necessária (bonus do dump)
        bonus = 2.0  # focuscost padrao do pasto -> ~metade da racao (efeito de foco)
        feed_focus = feed_no / bonus
        # prole: cria extra (vendida como bebê)
        yield_offspring = (info.get("offspring_chance") or 0) * (info.get("offspring_amount") or 0)
        revenue = sell[1] + yield_offspring * buy[1]   # adulto + bebê(s) extra
        profit_no = revenue - buy[1] - feed_no
        profit_focus = revenue - buy[1] - feed_focus
        rows.append({
            "baby": baby, "grown": grown,
            "buy_city": buy[0], "baby_price": round(buy[1]),
            "sell_city": sell[0], "grown_net": round(sell[1]),
            "grow_days": round(gt_d, 2),
            "offspring": round(yield_offspring, 2),
            "feed_cost": round(feed_no),
            "feed_focus": round(feed_focus),
            "profit_no_focus": round(profit_no),
            "profit_focus": round(profit_focus),
            "per_day_no_focus": round(profit_no / gt_d),
            "per_day_focus": round(profit_focus / gt_d),
            "focus_gain": round(profit_focus - profit_no),
            "working_capital": round(buy[1] + feed_no),    # cria + ração por ciclo
            "tier": info.get("tier"),
        })
    rows.sort(key=lambda r: -max(r["per_day_focus"], r["per_day_no_focus"]))
    return {"available": True, "rows": rows[:limit] if limit else rows,
            "priced": len(rows), "feed_source": spn[1] if spn else None}


# ------------------------------------------------------------ trabalhadores
def laborer_economy(price_q1, premium=True, sell_mode="order", cities=None,
                    limit=60):
    """Margem do diário (vazio->cheio) por família/tier, em todas as cidades.

    margem = vender CHEIO − comprar VAZIO = o que o mercado paga pela fama. Mostra
    também o recurso que aquele trabalhador entrega e onde vendê-lo melhor.
    """
    data = _load().get("laborers") or {}
    if not data:
        return {"available": False, "rows": []}
    cities = cities or config.CITIES
    # recurso bruto representativo da família (T{tier}_{FAMILY}); a contagem
    # exata da entrega depende do building def — aqui damos o melhor mercado dele.
    rows = []
    for empty, info in data.items():
        full = info["full"]
        buy = _best_buy(price_q1, empty, cities)
        sell = _best_sell(price_q1, full, cities, sell_mode, premium)
        if not buy or not sell:
            continue
        margin = sell[1] - buy[1]
        res = f"T{info['tier']}_{info['resource_family']}"
        res_sell = _best_sell(price_q1, res, cities, sell_mode, premium)
        rows.append({
            "empty": empty, "full": full,
            "family": info["family"], "tier": info["tier"],
            "buy_city": buy[0], "empty_price": round(buy[1]),
            "sell_city": sell[0], "full_net": round(sell[1]),
            "margin": round(margin),
            "resource": res,
            "resource_sell_city": res_sell[0] if res_sell else None,
            "resource_net": round(res_sell[1]) if res_sell else None,
        })
    rows.sort(key=lambda r: -r["margin"])
    return {"available": True, "rows": rows[:limit] if limit else rows,
            "priced": len(rows)}

# -*- coding: utf-8 -*-
"""Administração de ILHA: trabalhadores (diários), agricultura, pecuária.

Tudo sobre price_q1 (menor venda q1 por (item, cidade), já saneado de âncoras) +
data/island_data.json (mecânicas reais do dump: foco, ciclo, bônus, prole,
ração/nutrição). Cada motor compara cidades para COMPRAR insumos e VENDER
produtos, calcula lucro/dia e o capital de giro por unidade.

Mecânica do jogo (verificada no items.json + loot.json + wiki, jun/2026):
- Agricultura: a colheita é FIXA (~4.5 por canteiro, igual p/ toda cultura; o
  @activefarmbonus do crop é só 2*(1-rebrota), NÃO um multiplicador de colheita).
  O FOCO não aumenta a colheita: ele GARANTE a volta da semente. Sem foco a
  semente volta com seed_regrow_chance; com foco volta sempre. Logo o ganho do
  foco = semente economizada = seed_cost*(1-rebrota). Ciclo em
  @activefarmcyclelengthseconds.
- Pecuária: a cria vira adulto em @growtime; tem chance de PROLE (cria extra,
  vendida como bebê). O animal CONSOME nutrição ao longo do crescimento
  (consumption.food, categoria 'plants' = culturas) — a ração NÃO é reduzida pelo
  foco. O efeito do foco no pasto (via @activefarmbonus por animal) é modelado
  como PROLE EXTRA esperada — é uma ESTIMATIVA; o ranking usa o número firme
  (sem foco).
- Trabalhadores: comprar diário VAZIO + encher de fama (esforço, sem custo de
  prata) + vender CHEIO = o "salário" que o mercado paga pela fama. Só diários de
  COLETA entregam recurso bruto; fabricantes/pesca não têm recurso (None).
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
        base_yield = info.get("crop_yield") or 0
        rev = base_yield * sell[1]            # colheita fixa: foco NÃO a altera
        # Foco = retorno garantido da semente. Sem foco a semente volta com a
        # chance natural (rebrota); com foco volta sempre (custo de semente ~0).
        regrow = min((info.get("seed_regrow_chance") or 0)
                     * (info.get("seed_regrow_amount") or 1), 1.0)
        net_seed_no = max(seed_cost * (1 - regrow), 0)   # sem foco: parte volta
        net_seed_focus = 0.0                             # com foco: garantida
        profit_no = rev - net_seed_no
        profit_focus = rev - net_seed_focus
        rows.append({
            "seed": seed, "crop": crop,
            "buy_city": buy[0], "seed_price": round(seed_cost),
            "sell_city": sell[0], "crop_net": round(sell[1]),
            "cycle_days": round(cyc_d, 2),
            "crop_yield": round(base_yield, 1),          # fixa (com/sem foco)
            "seed_return_pct": round(regrow * 100),      # rebrota natural s/ foco
            "profit_no_focus": round(profit_no),
            "profit_focus": round(profit_focus),
            "per_day_no_focus": round(profit_no / cyc_d),
            "per_day_focus": round(profit_focus / cyc_d),
            "focus_cost": round(info.get("focus_cost") or 0),
            "focus_gain": round(profit_focus - profit_no),   # semente economizada
            "focus_is_estimate": False,   # agricultura: ganho do foco é DETERMINÍSTICO
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
        # ração: nutrição total consumida no crescimento × prata/nutrição.
        # NÃO é reduzida pelo foco (mito desfeito na auditoria).
        spnp = info.get("seconds_per_nutrition") or 0
        total_nut = (info.get("growtime_s") or 0) / spnp if spnp else 0
        feed = (total_nut * spn[0]) if spn else 0
        # prole: cria extra, vendida como BEBÊ (preço de venda líquido do bebê)
        baby_sell = _best_sell(price_q1, baby, cities, sell_mode, premium)
        baby_net = baby_sell[1] if baby_sell else buy[1]
        off_no = (info.get("offspring_chance") or 0) * (info.get("offspring_amount") or 0)
        # foco no pasto: @activefarmbonus real por animal vira PROLE EXTRA esperada.
        # É ESTIMATIVA — o ranking ordena pelo número firme (sem foco). Só vale para
        # animais que REPRODUZEM (off_no>0); mounts/abate (offspring_chance=0) não
        # ganham prole fantasma com foco — o foco não tem efeito modelado neles.
        off_focus = off_no + ((info.get("farm_bonus") or 0) if off_no > 0 else 0)
        profit_no = sell[1] + off_no * baby_net - buy[1] - feed
        profit_focus = sell[1] + off_focus * baby_net - buy[1] - feed
        rows.append({
            "baby": baby, "grown": grown,
            "buy_city": buy[0], "baby_price": round(buy[1]),
            "sell_city": sell[0], "grown_net": round(sell[1]),
            "grow_days": round(gt_d, 2),
            "offspring": round(off_no, 2),
            "offspring_focus": round(off_focus, 2),       # estimativa c/ foco
            "feed_cost": round(feed),
            "profit_no_focus": round(profit_no),
            "profit_focus": round(profit_focus),          # estimativa
            "per_day_no_focus": round(profit_no / gt_d),
            "per_day_focus": round(profit_focus / gt_d),  # estimativa
            "focus_gain": round(profit_focus - profit_no),
            "focus_is_estimate": True,
            "working_capital": round(buy[1] + feed),       # cria + ração por ciclo
            "tier": info.get("tier"),
        })
    rows.sort(key=lambda r: -r["per_day_no_focus"])   # ordena pelo número firme
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
        # só diários de COLETA têm recurso bruto de id limpo; fabricantes/pesca = None
        fam_res = info.get("resource_family")
        res = f"T{info['tier']}_{fam_res}" if fam_res else None
        res_sell = _best_sell(price_q1, res, cities, sell_mode, premium) if res else None
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

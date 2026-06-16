# -*- coding: utf-8 -*-
"""Arbitragem & logística: carga ótima, reposição, escada de qualidade, BM.

Reaproveita o motor de flips (albion/flips.py) e o cache local. As decisões que
o ranking simples de flips não responde:

- CARGA: o que e quanto enfiar na mula para encher os kg sem estourar a
  liquidez do destino (mochila peso×lucro).
- REPOSIÇÃO: o killboard diz o que o servidor está perdendo; cruzado com o
  spread inter-cidade + peso, vira uma fila de "compre X em A, leve pra B".
- ESCADA DE QUALIDADE: prêmio de prata por degrau de qualidade na mesma cidade.
- MERCADO NEGRO: prêmio do BM sobre a melhor venda nas cidades reais.

A capacidade de carga (kg) da montaria NÃO está nos dumps — é parâmetro do
usuário (padrão: boi de transporte ~1500 kg).
"""
from . import config
from .flips import sell_revenue, where_to_sell

DEFAULT_CARGO_KG = 1500   # boi de transporte (parâmetro; não vem do dump)


def cargo_knapsack(flips, vol_of, w_max=DEFAULT_CARGO_KG,
                   capture_rate=None, limit=40):
    """Mochila fracionária peso×lucro para uma viagem.

    flips: saída de compute_flips (tem profit, weight, profit_per_kg por item).
    vol_of(item_id) -> volume diário no destino (piso da AODP). O teto de
    unidades por item = volume × capture_rate (não adianta levar mais do que o
    mercado absorve). Enche W_max gulosamente por lucro/kg; devolve a cesta e o
    preço-sombra (lucro/kg do primeiro item que ficou de fora).
    """
    capture_rate = config.CAPTURE_RATE if capture_rate is None else capture_rate
    cand = []
    for o in flips:
        w = o.get("weight") or 0
        if w <= 0 or (o.get("profit") or 0) <= 0:
            continue
        cap_units = max(0, int((vol_of(o["item_id"]) or 0) * capture_rate))
        if cap_units <= 0:
            continue
        cand.append({
            "item_id": o["item_id"], "name_pt": o.get("name_pt", o["item_id"]),
            "buy_city": o.get("buy_city"), "sell_city": o.get("sell_city"),
            "quality": o.get("quality"), "weight": w,
            "profit": o["profit"], "profit_per_kg": o["profit"] / w,
            "cap_units": cap_units,
        })
    cand.sort(key=lambda c: -c["profit_per_kg"])
    basket, used_kg, total_profit = [], 0.0, 0.0
    shadow = None
    for c in cand:
        if used_kg >= w_max:
            shadow = shadow or round(c["profit_per_kg"], 1)
            break
        room_kg = w_max - used_kg
        units_by_weight = int(room_kg / c["weight"])
        units = min(c["cap_units"], units_by_weight)
        if units <= 0:
            continue
        kg = units * c["weight"]
        profit = units * c["profit"]
        basket.append({**{k: c[k] for k in ("item_id", "name_pt", "buy_city",
                          "sell_city", "quality", "weight")},
                       "units": units, "kg": round(kg, 1),
                       "profit": round(profit),
                       "profit_per_kg": round(c["profit_per_kg"], 1)})
        used_kg += kg
        total_profit += profit
        if limit and len(basket) >= limit:
            break
    return {
        "w_max": w_max, "used_kg": round(used_kg, 1),
        "trip_profit": round(total_profit),
        "shadow_price_per_kg": shadow,
        "basket": basket,
    }


def quality_ladder(price_rows, items_meta, premium=True, sell_mode="order",
                   min_premium_pct=0, max_premium_pct=500, limit=60):
    """Prêmio de preço por degrau de qualidade dentro da mesma cidade.

    price_rows: linhas do cache (item, city, quality, sell_price_min...).
    Para cada (item, cidade) com >=2 qualidades cotadas: prêmio[q] em prata e %
    sobre q-1. Revela quando a qualidade alta paga (craftar/estocar q alta) e o
    custo de subir um degrau.
    """
    by_ic = {}
    for r in price_rows:
        sp = r.get("sell_price_min") or 0
        if sp > 0:
            by_ic.setdefault((r["item_id"], r["city"]), {})[r["quality"]] = sp
    out = []
    for (item, city), q_price in by_ic.items():
        qs = sorted(q_price)
        # exige a base q1 cotada: uma cidade que só cota q4/q5 quase sempre
        # carrega ordem-âncora de masterpiece, não uma escada real
        if len(qs) < 2 or qs[0] != 1:
            continue
        meta = items_meta.get(item) or {}
        steps = []
        for i in range(1, len(qs)):
            q, qprev = qs[i], qs[i - 1]
            prev_p, cur_p = q_price[qprev], q_price[q]
            steps.append({
                "from_q": qprev, "to_q": q,
                "premium_abs": cur_p - prev_p,
                "premium_pct": round(100 * (cur_p - prev_p) / prev_p, 1)
                if prev_p else None,
            })
        # descarta degraus com prêmio implausível (âncora numa só cidade que o
        # saneamento entre cidades não pega quando só ela cota aquela qualidade)
        steps = [s for s in steps if (s["premium_pct"] or 0) <= max_premium_pct]
        if not steps:
            continue
        top = max(steps, key=lambda s: s["premium_pct"] or 0)
        if (top["premium_pct"] or 0) < min_premium_pct:
            continue
        out.append({
            "item_id": item, "name_pt": meta.get("pt", item), "city": city,
            "qualities": qs,
            "best_step": f"q{top['from_q']}→q{top['to_q']}",
            "best_premium_pct": top["premium_pct"],
            "best_premium_abs": top["premium_abs"],
        })
    out.sort(key=lambda r: -(r["best_premium_pct"] or 0))
    return out[:limit] if limit else out


def black_market_premium(price_rows, items_meta, premium=True,
                         max_premium_pct=300, limit=60):
    """Prêmio do Mercado Negro sobre a melhor venda líquida nas cidades reais.

    BM = venda instantânea numa ordem do sistema: net = preço × (1 - imposto),
    SEM taxa de anúncio (você não cria ordem — confirmado no wiki). Compara com
    a melhor venda líquida nas 5 reais (instantânea ou ordem). Só equipamento de
    combate (categorias que o BM compra).
    """
    BM = "Black Market"
    by_item_q = {}
    for r in price_rows:
        by_item_q.setdefault((r["item_id"], r["quality"]), {})[r["city"]] = r
    out = []
    for (item, q), by_city in by_item_q.items():
        meta = items_meta.get(item) or {}
        if meta.get("cat") not in config.BLACK_MARKET_CATEGORIES:
            continue
        bm = by_city.get(BM)
        bm_price = (bm or {}).get("buy_price_max") or 0
        if bm_price <= 0:
            continue
        bm_net = sell_revenue(bm_price, "instant", premium)
        # melhor venda líquida nas cidades reais
        best_city, best_net = None, 0
        for city, r in by_city.items():
            if city == BM:
                continue
            for mode, field in (("instant", "buy_price_max"),
                                ("order", "sell_price_min")):
                p = r.get(field) or 0
                if p <= 0:
                    continue
                net = sell_revenue(p, mode, premium)
                if net > best_net:
                    best_net, best_city = net, city
        if best_net <= 0:
            continue
        premium_pct = 100 * (bm_net - best_net) / best_net
        # prêmio absurdo = a melhor venda "real" é uma ordem-isca de prata baixa,
        # não um prêmio genuíno do BM; descarta como artefato
        if premium_pct > max_premium_pct:
            continue
        out.append({
            "item_id": item, "name_pt": meta.get("pt", item), "quality": q,
            "tier": meta.get("tier", 0), "ench": meta.get("ench", 0),
            "weight": meta.get("w") or 0,
            "bm_net": round(bm_net), "best_city": best_city,
            "best_city_net": round(best_net),
            "premium_abs": round(bm_net - best_net),
            "premium_pct": round(premium_pct, 1),
        })
    out.sort(key=lambda r: -r["premium_pct"])
    return out[:limit] if limit else out


def restock_map(demand_rows, price_rows, items_meta, premium=True, limit=40):
    """Mapa de reposição: o que o servidor perdeu (killboard) × onde abastecer.

    demand_rows: (item_id, victim_units) recentes de item_demand_daily.
    Para cada item demandado: cidade mais barata p/ comprar (min sell_price_min)
    e melhor cidade p/ vender líquido (where_to_sell). Ordena por
    demanda × lucro_por_kg — a fila de abastecimento mais valiosa.
    """
    demand = {iid: u for iid, u in demand_rows if u}
    # melhor venda líquida por item (q1) via where_to_sell
    sells = where_to_sell([r for r in price_rows if r["quality"] == 1],
                          items_meta, premium=premium, qualities=[1])
    best_sell = {}
    for s in sells:
        opt = s["options"][0] if s.get("options") else None
        if opt:
            best_sell[s["item_id"]] = (opt["city"], opt["net"])
    # cidade mais barata para comprar (q1)
    cheapest = {}
    for r in price_rows:
        if r["quality"] != 1:
            continue
        sp = r.get("sell_price_min") or 0
        if sp <= 0:
            continue
        cur = cheapest.get(r["item_id"])
        if cur is None or sp < cur[1]:
            cheapest[r["item_id"]] = (r["city"], sp)
    out = []
    for item, units in demand.items():
        buy = cheapest.get(item)
        sell = best_sell.get(item)
        if not buy or not sell:
            continue
        meta = items_meta.get(item) or {}
        w = meta.get("w") or 0
        profit = sell[1] - buy[1]
        if profit <= 0:
            continue
        out.append({
            "item_id": item, "name_pt": meta.get("pt", item),
            "tier": meta.get("tier", 0),
            "demand_units": units,
            "buy_city": buy[0], "buy_price": buy[1],
            "sell_city": sell[0], "sell_net": round(sell[1]),
            "profit": round(profit),
            "profit_per_kg": round(profit / w, 1) if w > 0 else None,
            "score": round(units * (profit / w if w > 0 else profit)),
        })
    out.sort(key=lambda r: -r["score"])
    return out[:limit] if limit else out

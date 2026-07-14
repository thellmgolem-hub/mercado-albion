# -*- coding: utf-8 -*-
"""Guild & estratégico: coleta por ROI de informação, cesta de regear, make-or-buy.

Camada para a guild planejar suprimento a partir do que o killboard mostra que
o servidor consome, cruzado com mercado e produção.
"""
from . import config
from . import craft
from . import production
from . import store
from .flips import sell_revenue


def watchlist_roi(con, server, price_of=None, days=7, limit=40):
    """Ranking de destruição: o que o servidor mais perde em PvP, ponderado por
    preço e dias ativos (score = unidades × dias × preço).

    Lê o agregado kill_demand_daily (killboard magro). No piloto não há
    watchlist de coleta, então o ranking é a própria demanda destruída — útil
    para a guild saber o que vale a pena produzir/estocar.
    """
    rows = con.execute(
        """SELECT item_id, SUM(victim_units) AS units,
                  COUNT(DISTINCT day) AS active_days
           FROM kill_demand_daily
           WHERE server=? AND day >= ? AND slot != 'Inventory'
           GROUP BY item_id HAVING SUM(victim_units)>0""",
        [server, store.cutoff_iso(days)]).fetchall()
    add = []
    for item, units, active_days in rows:
        price = price_of(item) if price_of else None
        score = units * active_days * (price or 1)
        add.append({"item_id": item, "destroyed": units,
                    "active_days": active_days, "price": price,
                    "score": round(score)})
    add.sort(key=lambda r: -r["score"])
    return {"add": add[:limit], "watched_count": 0}


def soldier_kit_index(con, server, days=7, hist_days=120, top_k=4):
    """Índice de custo de regear: cesta ponderada pelo uso real no killboard.

    Pesos = participação de cada item por slot na destruição (top-K por slot).
    Série diária = Σ peso × avg_price (history q1, escala 24h), base 100. É um
    CPI de guerra: quanto está custando re-equipar a guild ao longo do tempo.
    """
    slots = ('MainHand', 'OffHand', 'Head', 'Armor', 'Shoes', 'Cape')
    weights = {}
    for slot in slots:
        rows = con.execute(
            """SELECT item_id, SUM(victim_units) AS n FROM kill_demand_daily
               WHERE server=? AND slot=? AND quality>0 AND day >= ?
               GROUP BY item_id ORDER BY n DESC LIMIT ?""",
            [server, slot, store.cutoff_iso(days), top_k]).fetchall()
        tot = sum(n for _, n in rows) or 1
        for item, n in rows:
            # SUM(victim_units) é Decimal no Postgres; float() deixa o peso float
            # e evita Decimal*avg_price (DOUBLE->float) no custo da cesta abaixo.
            weights[item] = weights.get(item, 0) + (float(n) / float(tot)) / len(slots)
    if not weights:
        return {"basket": [], "series": []}
    ids = list(weights)
    rows = con.execute(
        f"""SELECT item_id, substr(ts,1,10) AS day, avg_price
            FROM history WHERE server=? AND time_scale=24 AND quality=1
              AND avg_price>0 AND ts >= ?
              AND item_id IN ({','.join('?' * len(ids))})
            ORDER BY day""",
        [server, store.cutoff_iso(hist_days), *ids]).fetchall()
    by_day = {}
    for item, day, price in rows:
        by_day.setdefault(day, {})[item] = price
    series = []
    for day in sorted(by_day):
        prices = by_day[day]
        # custo da cesta usando só os itens cotados naquele dia (renormaliza)
        wsum = sum(weights[i] for i in prices if i in weights)
        if wsum <= 0:
            continue
        cost = sum(weights[i] * prices[i] for i in prices if i in weights) / wsum
        series.append({"day": day, "cost": round(cost)})
    base = series[0]["cost"] if series else None
    for s in series:
        s["index"] = round(100 * s["cost"] / base, 1) if base else None
    return {"basket": sorted(weights.items(), key=lambda x: -x[1])[:12],
            "series": series}


def make_or_buy(con, server, price_of, days=7, premium=True, focus=False,
                cities=None, limit=40):
    """Banda de preço de transferência interna (fazer) vs mercado (comprar).

    Para cada item que a guild consome (kill_demand_daily) e é craftável: custo
    interno = Σ insumos × (1-RRR) + venda evitada; custo de mercado = menor
    venda. A banda [interno, mercado] é a faixa de preço justo do contrato; o
    tamanho do contrato vem da demanda/dia.
    """
    cities = cities or config.ROYAL_CITIES
    # slot != 'Inventory': conta só gear EQUIPADO destruído (regear), não a
    # carga transportada (runas/almas/stacks) — senão a demanda infla.
    demand = dict(con.execute(
        """SELECT item_id, SUM(victim_units) FROM kill_demand_daily
           WHERE server=? AND day >= ? AND slot != 'Inventory'
           GROUP BY item_id""",
        [server, store.cutoff_iso(days)]).fetchall())
    out = []
    for item, units in demand.items():
        recipe = production._prod_recipe(item)
        if not recipe:
            continue
        market = min((p for c in cities if (p := price_of(item, c))), default=None)
        if not market:
            continue
        # melhor custo interno entre cidades
        best_internal, best_city = None, None
        for city in cities:
            cost, ok = 0.0, True
            for inp in recipe["inputs"]:
                p = price_of(inp["id"], city)
                if not p:
                    ok = False
                    break
                cost += inp["count"] * p
            if not ok:
                continue
            eff = cost * (1 - production.rrr_for(item, recipe, city, focus))
            if best_internal is None or eff < best_internal:
                best_internal, best_city = eff, city
        if best_internal is None:
            continue
        out.append({
            "item_id": item, "demand_units": units,
            "internal_cost": round(best_internal), "internal_city": best_city,
            "market_price": market,
            "save_pct": round(100 * (market - best_internal) / market, 1)
            if market else None,
            "verdict": "fazer" if best_internal < market else "comprar",
        })
    out.sort(key=lambda r: -(r["save_pct"] or -999))
    return out[:limit] if limit else out

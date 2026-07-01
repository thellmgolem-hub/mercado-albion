# -*- coding: utf-8 -*-
"""Consultor de flips por orçamento: "quanto tenho + onde estou -> o que comprar".

O jogador informa a prata disponível e a cidade em que está; o consultor devolve
a lista de compras mais confiável que cabe no orçamento (o que comprar, onde
revender, quantas unidades, custo e lucro líquido). É CACHE-ONLY e somente-
leitura: lê o cache que o sweep mantém fresco 24/7 — não varre o mercado inteiro.

Reusa o que já existe, sem re-derivar nada:
- flips.compute_flips  -> todas as taxas (imposto 4/8%, anúncio 2,5%, Mercado
  Negro sem setup fee) e o ranking por lucro ponderado pela confiança (frescor).
- microstructure.clean_price_rows -> zera preços-âncora (ordens-isca) antes do
  motor; só age com >=3 cidades cotadas (protege sobretudo o lado da venda).
- microstructure._daily_liquidity -> mediana do volume diário por (item, cidade),
  usada para CAPAR o nº de unidades a um valor realista (liquidez x CAPTURE_RATE).

O dict devolvido é plano e autossuficiente — serve à aba web e a um futuro
comando de Discord.
"""
import math

from . import config, flips, microstructure

# Mesmas categorias líquidas do coletor automático (_AUTOCOLLECT_CATS do app).
FLIPABLE_CATS = ["weapons", "armors", "head", "shoes", "offhands", "capes",
                 "bags", "mounts", "consumables", "gathering", "crafting"]
SAFE_ROYAL_CITIES = [c for c in config.ROYAL_CITIES if c != "Caerleon"]
BLACK_MARKET = flips.BLACK_MARKET
_LIQ_QUERY_CAP = 300   # limita o IN da consulta de liquidez (só enchemos max_lines)


def flipable_universe(db, per_cat=400, total=2000):
    """Ids de itens geralmente flipáveis (T3+, encantos 0-3). Espelha o
    _default_watch_seed do app: itens líquidos, não o catálogo inteiro."""
    ids, seen = [], set()
    for cat in FLIPABLE_CATS:
        try:
            rows = db.filter(cat=cat, tier_min=3, ench_list=[0, 1, 2, 3],
                             limit=per_cat)
        except Exception:
            rows = []
        for it in rows:
            iid = it.get("id") if isinstance(it, dict) else it
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
                if len(ids) >= total:
                    return set(ids)
    return set(ids)


def _read_prices(con, server, city_list, qualities, flipable):
    """Lê o cache de preços (RO) restrito a cidades/qualidades e ao universo
    flipável. Só SELECT — nada de função de data no SQL."""
    ph_c = ",".join("?" for _ in city_list)
    where = ["server=?", f"city IN ({ph_c})"]
    params = [server, *city_list]
    if qualities:
        ph_q = ",".join("?" for _ in qualities)
        where.append(f"quality IN ({ph_q})")
        params.extend(qualities)
    rows = con.execute(
        f"SELECT * FROM prices WHERE {' AND '.join(where)}", params).fetchall()
    return [dict(r) for r in rows if r["item_id"] in flipable]


def _base_flags(premium, buy_mode, sell_mode, include_black_market,
                history_days, capture_rate):
    return {"premium": premium, "buy_mode": buy_mode, "sell_mode": sell_mode,
            "include_black_market": include_black_market,
            "history_days": history_days, "capture_rate": capture_rate}


def _empty(city, budget, flags):
    return {
        "source": "cache", "city": city, "budget": budget, "shopping_list": [],
        "summary": {"budget": budget, "orcamento_usado": 0.0,
                    "orcamento_restante": float(budget),
                    "lucro_total_estimado": 0.0, "roi_total_pct": 0.0,
                    "linhas": 0, "capture_rate": flags["capture_rate"],
                    "anti_isca_aplicada": False,
                    "black_market_incluido": flags["include_black_market"]},
        **flags,
    }


def advise(con, server, db, *, budget, city, qualities=(1,),
           premium=True, buy_mode="instant", sell_mode="order",
           include_black_market=False, history_days=7,
           capture_rate=None, min_profit=0, max_lines=40,
           fresh_max_age_min=720, flipable=None):
    """Consultor de flips por orçamento (cache-only, somente-leitura).

    con: conexão store.connect(readonly=True) (ou None -> contrato vazio).
    budget: prata disponível (>0). city: cidade de COMPRA (onde o jogador está).
    Devolve o dict do contrato (shopping_list + summary + flags).
    """
    budget = float(budget or 0)
    if capture_rate is None:
        capture_rate = config.CAPTURE_RATE
    qualities = list(qualities or [1])
    if buy_mode not in ("instant", "order") or sell_mode not in ("instant", "order"):
        raise ValueError("modo de compra/venda inválido")
    flags = _base_flags(premium, buy_mode, sell_mode, include_black_market,
                        history_days, capture_rate)
    if con is None or budget <= 0:
        return _empty(city, budget, flags)

    if flipable is None:
        flipable = flipable_universe(db)

    # Escopo: compra ancorada na cidade do jogador; venda varre as cidades reais
    # seguras (+ a própria, se royal) e o Mercado Negro se pedido.
    sell_scope = set(SAFE_ROYAL_CITIES)
    if city in config.ROYAL_CITIES:
        sell_scope.add(city)
    if include_black_market:
        sell_scope.add(BLACK_MARKET)
    city_list = sorted({city} | sell_scope)

    rows = _read_prices(con, server, city_list, qualities, flipable)
    if not rows:
        return _empty(city, budget, flags)

    rows = microstructure.clean_price_rows(rows)   # anti-isca antes do motor
    metas = {i: (db.get(i) or {}) for i in {r["item_id"] for r in rows}}
    opps = flips.compute_flips(
        rows, metas, premium=premium, buy_mode=buy_mode, sell_mode=sell_mode,
        min_profit=min_profit, qualities=qualities,
        buy_cities={city}, sell_cities=sell_scope,
        max_age_buy=fresh_max_age_min, max_age_sell=fresh_max_age_min)

    # melhor rota por item (compute_flips já vem ordenado por flip_score desc)
    best = {}
    for o in opps:
        best.setdefault(o["item_id"], o)
    candidates = sorted(best.values(),
                        key=lambda o: (-o["flip_score"], -o["roi_pct"]))
    candidates = candidates[:_LIQ_QUERY_CAP]

    # liquidez em lote: mediana do volume diário por (item, cidade de compra)
    pairs = [(o["item_id"], o["buy_city"]) for o in candidates]
    liq = microstructure._daily_liquidity(con, server, pairs, days=history_days)

    shopping, spent, profit_total = [], 0.0, 0.0
    for o in candidates:
        if len(shopping) >= max_lines:
            break
        cost = o["cost"]
        if cost <= 0 or spent + cost > budget:
            continue   # não cabe nem 1 unidade; tenta o próximo (mais barato)
        units_budget = int((budget - spent) // cost)
        if units_budget <= 0:
            continue
        l = liq.get((o["item_id"], o["buy_city"]))
        if l is None:                       # sem history -> sem cap de liquidez
            units, cap_reason, low_liq = units_budget, "budget", True
        else:
            units_liq = int(math.floor(l * capture_rate))
            if units_liq <= 0:
                continue                    # item ilíquido (dado real): descarta
            units = min(units_liq, units_budget)
            cap_reason = "liquidity" if units_liq < units_budget else "budget"
            low_liq = False
        line_cost = units * cost
        line_profit = units * o["profit"]
        spent += line_cost
        profit_total += line_profit
        shopping.append({
            "item_id": o["item_id"], "name_pt": o["name_pt"],
            "name_en": o["name_en"], "tier": o["tier"], "ench": o["ench"],
            "quality": o["quality"], "buy_city": o["buy_city"],
            "sell_city": o["sell_city"], "buy_mode": o["buy_mode"],
            "sell_mode": o["sell_mode"], "buy_price": o["buy_price"],
            "sell_price": o["sell_price"], "unit_cost": o["cost"],
            "unit_profit": o["profit"], "units": units,
            "units_cap_reason": cap_reason, "custo": round(line_cost, 1),
            "lucro_liquido": round(line_profit, 1), "roi_pct": o["roi_pct"],
            "liquidity_day": l, "buy_age_min": o["buy_age_min"],
            "sell_age_min": o["sell_age_min"],
            "confidence_score": o["confidence_score"],
            "confidence_label": o["confidence_label"],
            "confidence_notes": o["confidence_notes"],
            "bm_order_quality": o.get("bm_order_quality"),
            "flip_score": o["flip_score"], "low_liquidity_data": low_liq,
        })

    roi_total = (profit_total / spent * 100) if spent > 0 else 0.0
    return {
        "source": "cache", "city": city, "budget": budget,
        "shopping_list": shopping,
        "summary": {
            "budget": budget, "orcamento_usado": round(spent, 1),
            "orcamento_restante": round(budget - spent, 1),
            "lucro_total_estimado": round(profit_total, 1),
            "roi_total_pct": round(roi_total, 2), "linhas": len(shopping),
            "capture_rate": capture_rate, "anti_isca_aplicada": True,
            "black_market_incluido": include_black_market,
        },
        **flags,
    }

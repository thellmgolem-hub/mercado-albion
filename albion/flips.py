# -*- coding: utf-8 -*-
"""Motor de cálculo de flips.

Fórmulas (verificadas no wiki oficial, jun/2026):
  t = imposto de venda: 4% com premium, 8% sem premium
  f = taxa de anúncio: 2,5% (ordens de venda E de compra; não reembolsável)

  Custo de compra:    instantânea = B          | ordem de compra = B * (1 + f)
  Receita de venda:   instantânea = S * (1-t)  | ordem de venda  = S * (1-t-f)

Mercado Negro: só compra equipamento de combate, só por ordens de compra do
sistema; um item de qualidade q pode preencher ordens de qualidade <= q.
Você VENDE INSTANTANEAMENTE numa ordem do sistema — não cria ordem própria —
então NÃO há taxa de anúncio (setup fee) do seu lado; só incide o imposto de
venda (smugglertransactiontax = 0,08 base, reduzido a 0,04 com premium, igual
ao mercado normal — confirmado no wiki oficial e em gamedata.json). O
smugglersetupfee (1,5%) do dump é do lado do sistema/contrabando, não do
vendedor; por isso o cálculo do BM usa sell_revenue(..., "instant"), sem
SETUP_FEE — e isso está correto.
"""
from datetime import datetime, timezone

from . import config

BLACK_MARKET = "Black Market"


def parse_date(s):
    """Data da API -> datetime UTC, ou None para placeholder/vazio."""
    if not s or s.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_minutes(s, now=None):
    d = parse_date(s)
    if d is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - d).total_seconds() // 60))


def _age_score(age):
    if age is None:
        return 0
    if age <= 30:
        return 100
    if age <= 120:
        return 75
    if age <= 360:
        return 50
    if age <= 1440:
        return 25
    return 10


def confidence(age_buy, age_sell):
    """Nota simples de confianca baseada no lado mais velho da rota."""
    score = min(_age_score(age_buy), _age_score(age_sell))
    if score >= 80:
        label = "alta"
    elif score >= 55:
        label = "media"
    elif score >= 30:
        label = "baixa"
    else:
        label = "muito baixa"
    notes = []
    if age_buy is None:
        notes.append("compra sem data")
    else:
        notes.append(f"compra {age_buy}min")
    if age_sell is None:
        notes.append("venda sem data")
    else:
        notes.append(f"venda {age_sell}min")
    return score, label, "; ".join(notes)


def sales_tax(premium: bool) -> float:
    return config.SALES_TAX_PREMIUM if premium else config.SALES_TAX_NO_PREMIUM


def buy_cost(price: float, buy_mode: str) -> float:
    """Custo total por unidade para adquirir o item."""
    if buy_mode == "order":
        return price * (1 + config.SETUP_FEE)
    return float(price)


def sell_revenue(price: float, sell_mode: str, premium: bool) -> float:
    """Receita líquida por unidade ao vender."""
    t = sales_tax(premium)
    if sell_mode == "order":
        return price * (1 - t - config.SETUP_FEE)
    return price * (1 - t)


def _candidates(rows_by_city, side, mode, bm_sell=False):
    """Preço de referência por cidade para um lado do flip.

    side='buy':  instant -> sell_price_min | order -> buy_price_max
    side='sell': instant -> buy_price_max  | order -> sell_price_min
    """
    out = {}
    for city, r in rows_by_city.items():
        if side == "buy":
            if city == BLACK_MARKET:
                continue  # não se compra no Mercado Negro
            field = "sell_price_min" if mode == "instant" else "buy_price_max"
        else:
            if city == BLACK_MARKET and not bm_sell:
                continue
            # no Mercado Negro só existem ordens de compra do sistema
            field = "buy_price_max" if (mode == "instant" or city == BLACK_MARKET) \
                else "sell_price_min"
        price = r.get(field) or 0
        if price <= 0:
            continue
        out[city] = {
            "price": price,
            "date": r.get(field + "_date"),
            "fetched_at": r.get("fetched_at"),
        }
    return out


def compute_flips(price_rows, items_meta, premium=True, buy_mode="instant",
                  sell_mode="instant", min_profit=0, min_roi=None,
                  same_city=False, max_age_buy=None, max_age_sell=None,
                  qualities=None, buy_cities=None, sell_cities=None):
    """Gera oportunidades de flip a partir das linhas de preço da API.

    price_rows: linhas de get_prices() (uma por item x cidade x qualidade)
    items_meta: dict item_id -> item do ItemDB (nome, peso, categoria...)
    max_age_buy/max_age_sell: idade máxima (minutos) do dado de cada lado
    """
    now = datetime.now(timezone.utc)

    # (item, qualidade) -> cidade -> linha
    grouped = {}
    for r in price_rows:
        grouped.setdefault((r["item_id"], r["quality"]), {})[r["city"]] = r

    # Para o Mercado Negro: melhor ordem de compra com qualidade <= q
    bm_best = {}  # (item_id, q) -> {price, date, fetched_at, order_quality}
    for (item_id, q), by_city in grouped.items():
        r = by_city.get(BLACK_MARKET)
        if not r:
            continue
        price = r.get("buy_price_max") or 0
        if price <= 0:
            continue
        for q_item in range(q, 6):  # item de qualidade q_item preenche ordem q
            cur = bm_best.get((item_id, q_item))
            if cur is None or price > cur["price"]:
                bm_best[(item_id, q_item)] = {
                    "price": price,
                    "date": r.get("buy_price_max_date"),
                    "fetched_at": r.get("fetched_at"),
                    "order_quality": q,
                }

    opps = []
    for (item_id, q), by_city in grouped.items():
        if qualities and q not in qualities:
            continue
        meta = items_meta.get(item_id) or {}
        bm_ok = meta.get("cat") in config.BLACK_MARKET_CATEGORIES

        buys = _candidates(by_city, "buy", buy_mode)
        sells = _candidates(by_city, "sell", sell_mode, bm_sell=bm_ok)

        # substitui a entrada do Mercado Negro pela melhor ordem alcançável
        # (qualidade <= q) — item bom pode preencher ordem de qualidade menor
        if bm_ok and (item_id, q) in bm_best:
            b = bm_best[(item_id, q)]
            sells[BLACK_MARKET] = {
                "price": b["price"], "date": b["date"],
                "fetched_at": b["fetched_at"],
                "bm_order_quality": b["order_quality"],
            }
        elif BLACK_MARKET in sells:
            del sells[BLACK_MARKET]

        for cb, b in buys.items():
            if buy_cities and cb not in buy_cities:
                continue
            age_b = age_minutes(b["date"], now)
            if max_age_buy is not None and (age_b is None or age_b > max_age_buy):
                continue
            for cs, s in sells.items():
                if sell_cities and cs not in sell_cities:
                    continue
                if cb == cs and not same_city:
                    continue
                if cb == cs and buy_mode == "instant" and sell_mode == "instant":
                    continue  # comprar e vender instantâneo no mesmo mercado
                age_s = age_minutes(s["date"], now)
                if max_age_sell is not None and (age_s is None or age_s > max_age_sell):
                    continue

                eff_sell_mode = "instant" if cs == BLACK_MARKET else sell_mode
                cost = buy_cost(b["price"], buy_mode)
                revenue = sell_revenue(s["price"], eff_sell_mode, premium)
                profit = revenue - cost
                if profit < min_profit:
                    continue
                roi = profit / cost if cost > 0 else 0
                if min_roi is not None and roi * 100 < min_roi:
                    continue

                w = meta.get("w") or 0
                conf_score, conf_label, conf_notes = confidence(age_b, age_s)
                opps.append({
                    "item_id": item_id,
                    "name_pt": meta.get("pt", item_id),
                    "name_en": meta.get("en", item_id),
                    "tier": meta.get("tier", 0),
                    "ench": meta.get("ench", 0),
                    "quality": q,
                    "buy_city": cb,
                    "sell_city": cs,
                    "buy_mode": buy_mode,
                    "sell_mode": eff_sell_mode,
                    "buy_price": b["price"],
                    "sell_price": s["price"],
                    "cost": round(cost, 1),
                    "revenue": round(revenue, 1),
                    "profit": round(profit, 1),
                    "roi_pct": round(roi * 100, 2),
                    "weight": w,
                    "profit_per_kg": round(profit / w, 1) if w > 0 else None,
                    "buy_age_min": age_b,
                    "sell_age_min": age_s,
                    "confidence_score": conf_score,
                    "confidence_label": conf_label,
                    "confidence_notes": conf_notes,
                    "bm_order_quality": s.get("bm_order_quality"),
                })

    opps.sort(key=lambda o: -o["profit"])
    return opps


def where_to_sell(price_rows, items_meta, premium=True, qualities=None):
    """Modo 'Onde Vender': para um item que você já tem, ranqueia cidades.

    Retorna por (item, qualidade) as opções: venda instantânea (ordem de
    compra mais alta) e ordem de venda (menor preço anunciado) por cidade.
    """
    now = datetime.now(timezone.utc)
    grouped = {}
    for r in price_rows:
        grouped.setdefault((r["item_id"], r["quality"]), {})[r["city"]] = r

    bm_best = {}
    for (item_id, q), by_city in grouped.items():
        r = by_city.get(BLACK_MARKET)
        if not r:
            continue
        price = r.get("buy_price_max") or 0
        if price <= 0:
            continue
        for q_item in range(q, 6):
            cur = bm_best.get((item_id, q_item))
            if cur is None or price > cur["price"]:
                bm_best[(item_id, q_item)] = {
                    "price": price, "date": r.get("buy_price_max_date"),
                    "order_quality": q,
                }

    results = []
    for (item_id, q), by_city in grouped.items():
        if qualities and q not in qualities:
            continue
        meta = items_meta.get(item_id) or {}
        bm_ok = meta.get("cat") in config.BLACK_MARKET_CATEGORIES
        options = []
        for city, r in by_city.items():
            if city == BLACK_MARKET:
                continue
            bp = r.get("buy_price_max") or 0
            if bp > 0:
                options.append({
                    "city": city, "method": "instant", "price": bp,
                    "net": round(sell_revenue(bp, "instant", premium), 1),
                    "age_min": age_minutes(r.get("buy_price_max_date"), now),
                })
            sp = r.get("sell_price_min") or 0
            if sp > 0:
                options.append({
                    "city": city, "method": "order", "price": sp,
                    "net": round(sell_revenue(sp, "order", premium), 1),
                    "age_min": age_minutes(r.get("sell_price_min_date"), now),
                })
        if bm_ok and (item_id, q) in bm_best:
            b = bm_best[(item_id, q)]
            options.append({
                "city": BLACK_MARKET, "method": "instant", "price": b["price"],
                "net": round(sell_revenue(b["price"], "instant", premium), 1),
                "age_min": age_minutes(b["date"], now),
                "bm_order_quality": b["order_quality"],
            })
        options.sort(key=lambda o: -o["net"])
        if options:
            results.append({
                "item_id": item_id,
                "name_pt": meta.get("pt", item_id),
                "tier": meta.get("tier", 0),
                "ench": meta.get("ench", 0),
                "quality": q,
                "options": options,
            })
    results.sort(key=lambda r: (r["item_id"], r["quality"]))
    return results

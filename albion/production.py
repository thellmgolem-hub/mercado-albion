# -*- coding: utf-8 -*-
"""Economia de produção: cadeia vertical, foco, refinar-vs-vender, qualidade.

Estende albion/craft.py (RRR real do dump) para responder as perguntas do
produtor que o cálculo de uma receita só não cobre:

- onde gastar o FOCO escasso (prata por foco da cadeia inteira);
- vale CRAFTAR cada degrau ou comprar pronto (PnL raw→refino→item);
- vale REFINAR ou vender o bruto (custo de oportunidade do coletor);
- quanto o craft vale de fato quando a QUALIDADE sai acima de q1.

Tudo opera sobre preços já no cache (somente leitura) — ranqueia o que foi
coletado, sem novas chamadas à API.
"""
from . import config
from . import craft
from . import stats
from .flips import sell_revenue
from .microstructure import _robust_z


def _anchor_cities(price_lookup, item_id, cities, q=1, z_threshold=3.5):
    """Cidades onde o preço de venda do item é outlier ALTO entre as cidades.

    Pega ordens-âncora/isca (uma ponta absurda numa cidade) que enganariam a
    escolha da 'melhor cidade'. Só atua com >=3 cidades cotadas.
    """
    vals, keys = [], []
    for c in cities:
        p = price_lookup.get((item_id, c, q)) if q is not None \
            else price_lookup.get((item_id, c))
        if p:
            vals.append(p)
            keys.append(c)
    if len(vals) < 3:
        return set()
    return {c for c, z in zip(keys, _robust_z(vals)) if z > z_threshold}

# material refinado -> família de refino (chave de craft_data["refining"])
_REFINED_FAMILY = {
    "METALBAR": "ore", "PLANKS": "wood", "CLOTH": "fiber",
    "LEATHER": "hide", "STONEBLOCK": "rock",
}

# pesos da distribuição de qualidade do craft (gamedata CraftingQualityChances)
QUALITY_WEIGHTS = {1: 689, 2: 250, 3: 50, 4: 10, 5: 1}


_RAW_TOKENS = {"ORE", "WOOD", "FIBER", "HIDE", "ROCK"}


def refining_family(item_id):
    """Família de refino de um material refinado (ou None se não for um)."""
    base = (item_id or "").split("@")[0]
    for token, fam in _REFINED_FAMILY.items():
        if base.endswith(token):
            return fam
    return None


def is_raw_resource(item_id):
    """True para recurso BRUTO (ORE/WOOD/FIBER/HIDE/ROCK).

    O bruto se obtém coletando/comprando: a receita que o dump traz para ele é
    uma TRANSMUTAÇÃO (sobe 1 tier por uma taxa de prata e SEM retorno de
    recursos — maxreturnamount=0), mecânica de nicho que não entra na cadeia de
    refino padrão. Tratamos o bruto como folha (só comprar).
    """
    parts = (item_id or "").split("@")[0].split("_")
    return len(parts) >= 2 and parts[1] in _RAW_TOKENS


def _prod_recipe(item_id):
    """Receita para a cadeia de produção, ignorando transmutação de bruto."""
    return None if is_raw_resource(item_id) else craft.recipe_for(item_id)


def rrr_for(item_id, recipe, city, focus=False):
    """RRR (fração 0..1) unificado: refino usa a especialidade da família;
    craft usa o bônus de categoria. Resolve o buraco de craft.craft_rrr, que
    não conhecia a especialidade de refino (0,40 na cidade-bônus)."""
    fam = refining_family(item_id)
    if fam:
        cd = craft.craft_data()
        ref = (cd.get("refining") or {}).get(fam) or {}
        station = ref.get("station_bonus", cd.get("station_refining_bonus", 0.18))
        specialty = ref.get("specialty_bonus", 0.40) if city == ref.get("city") else 0.0
        s = station + specialty + (cd.get("focus_bonus_sum", 0.59) if focus else 0)
        return 1 - 1 / (1 + s)
    return craft.craft_rrr(recipe.get("category") if recipe else None, city, focus)


def bonus_city_for(item_id, recipe):
    """Cidade-bônus do item: família de refino ou categoria de craft."""
    fam = refining_family(item_id)
    if fam:
        return ((craft.craft_data().get("refining") or {}).get(fam) or {}).get("city")
    return craft.bonus_city(recipe.get("category") if recipe else None)


# ----------------------------------------------------- 1) eficiência de foco

def focus_efficiency(price_q1, premium=True, sell_mode="order", cities=None,
                     min_silver_per_focus=0, min_margin=0, limit=60):
    """Ranking de prata-por-foco da cadeia inteira (craft + refino).

    price_q1: dict (item_id, city) -> menor preço de venda q1 no cache.
    Para cada receita com foco>0 cujo produto e insumos têm preço, escolhe a
    melhor cidade pela margem COM foco e devolve prata/foco e o ganho que o
    foco trouxe (vs sem foco).
    """
    recipes = {}
    recipes.update(craft._load("recipes_refining.json"))
    recipes.update(craft._load("recipes_craft.json"))
    cities = cities or config.ROYAL_CITIES
    out = []
    for item_id, recipe in recipes.items():
        foc = recipe.get("focus") or 0
        if foc <= 0:
            continue
        out_qty = recipe.get("output", 1) or 1   # poção/comida saem em lote
        anchors = _anchor_cities(price_q1, item_id, cities, q=None)
        best = None
        for city in cities:
            if city in anchors:
                continue
            sell = price_q1.get((item_id, city))
            if not sell:
                continue
            cost, ok = 0.0, True
            for inp in recipe["inputs"]:
                p = price_q1.get((inp["id"], city))
                if not p:
                    ok = False
                    break
                cost += inp["count"] * p
            if not ok:
                continue
            net = sell_revenue(sell, sell_mode, premium) * out_qty
            m_focus = net - cost * (1 - rrr_for(item_id, recipe, city, True))
            m_plain = net - cost * (1 - rrr_for(item_id, recipe, city, False))
            cand = {
                "item_id": item_id, "city": city,
                "margin_focus": round(m_focus), "margin_plain": round(m_plain),
                "focus_gain": round(m_focus - m_plain),
                "focus": foc,
                "silver_per_focus": round(m_focus / foc, 1),
                "is_refining": refining_family(item_id) is not None,
            }
            if best is None or cand["silver_per_focus"] > best["silver_per_focus"]:
                best = cand
        if best and best["margin_focus"] >= min_margin \
                and best["silver_per_focus"] >= min_silver_per_focus:
            out.append(best)
    out.sort(key=lambda r: -r["silver_per_focus"])
    return out[:limit] if limit else out


# -------------------------------------------------- 2) PnL de cadeia vertical

def vertical_pnl(item_id, price_q1, premium=True, sell_mode="order",
                 focus=False, city=None, max_depth=8):
    """Make-vs-buy descendo a árvore de receitas, num único extrato.

    Para cada nó: custo_make = Σ insumos min(comprar, fazer) × (1-RRR); a folha
    é o bruto sem receita (só comprar). Marca o degrau onde fazer deixa de
    compensar (comprar pronto fica <= fazer).
    """
    city = city or bonus_city_for(item_id, craft.recipe_for(item_id)) \
        or config.ROYAL_CITIES[0]
    steps = []

    def cost_make(iid, depth, seen):
        recipe = _prod_recipe(iid)
        buy = price_q1.get((iid, city))
        if not recipe or iid in seen or depth > max_depth:
            return (buy, "comprar", None)   # folha: só comprar
        total, ok = 0.0, True
        for inp in recipe["inputs"]:
            unit, _, _ = cost_make(inp["id"], depth + 1, seen | {iid})
            if unit is None:
                ok = False
                break
            total += unit * inp["count"]
        make = total * (1 - rrr_for(iid, recipe, city, focus)) if ok else None
        # decisão no nó: o menor entre comprar e fazer
        opts = [(buy, "comprar"), (make, "fazer")]
        opts = [(c, lbl) for c, lbl in opts if c is not None]
        if not opts:
            return (None, None, None)
        best_cost, best_lbl = min(opts, key=lambda x: x[0])
        if depth >= 1:  # registra os degraus internos (não a raiz)
            steps.append({
                "item_id": iid, "depth": depth,
                "buy": round(buy) if buy else None,
                "make": round(make) if make is not None else None,
                "decision": best_lbl,
            })
        return (best_cost, best_lbl, make)

    sell = price_q1.get((item_id, city))
    root_make_cost, _, _ = cost_make(item_id, 0, frozenset())
    recipe = _prod_recipe(item_id)
    if recipe:  # custo de fazer a raiz a partir do melhor de cada insumo
        total, ok = 0.0, True
        for inp in recipe["inputs"]:
            unit, _, _ = cost_make(inp["id"], 1, frozenset({item_id}))
            if unit is None:
                ok = False
                break
            total += unit * inp["count"]
        root_make = total * (1 - rrr_for(item_id, recipe, city, focus)) if ok else None
    else:
        root_make = None
    net = sell_revenue(sell, sell_mode, premium) if sell else None
    # dedup de degraus (cada material aparece uma vez, no menor depth)
    uniq = {}
    for s in steps:
        k = s["item_id"]
        if k not in uniq or s["depth"] < uniq[k]["depth"]:
            uniq[k] = s
    return {
        "item_id": item_id, "city": city, "focus": focus,
        "sell": sell, "sell_net": round(net) if net else None,
        "make_cost": round(root_make) if root_make is not None else None,
        "buy_cost": round(sell) if sell else None,
        "pnl_make": round(net - root_make) if (net and root_make is not None) else None,
        "steps": sorted(uniq.values(), key=lambda s: (s["depth"], s["item_id"])),
    }


# --------------------------------------------- 3) refinar vs vender o bruto

def refine_premium_history(refined_id, hist, premium=True, sell_mode="order",
                           focus=False, city=None, min_days=20):
    """Percentil HISTÓRICO do prêmio de refino (não só o instantâneo).

    hist: dict (item, city) -> {dia: avg_price} (history diário). Reconstrói a
    série diária do prêmio de refino numa cidade e situa o prêmio de HOJE no
    seu próprio histórico (percentil + z sobre os resíduos da tendência) — um
    prêmio alto vs o passado é hora de refinar; baixo, de vender o bruto.
    """
    recipe = _prod_recipe(refined_id)
    if not recipe or not refining_family(refined_id):
        return None
    city = city or bonus_city_for(refined_id, recipe) or config.ROYAL_CITIES[0]
    prod_s = hist.get((refined_id, city)) or {}
    in_series = [(inp["count"], hist.get((inp["id"], city)) or {})
                 for inp in recipe["inputs"]]
    if not prod_s or any(not s for _, s in in_series):
        return None
    rrr = rrr_for(refined_id, recipe, city, focus)
    common = set(prod_s)
    for _, s in in_series:
        common &= set(s)
    series = []
    for d in sorted(common):
        cost = sum(cnt * s[d] for cnt, s in in_series)
        if cost <= 0:
            continue
        net = sell_revenue(prod_s[d], sell_mode, premium)
        series.append(100 * (net - cost * (1 - rrr)) / cost)
    if len(series) < min_days:
        return None
    cur = series[-1]
    pct = 100 * sum(1 for v in series if v <= cur) / len(series)
    fit = stats.linear_fit(series)
    resid = stats.residuals(series, fit) if fit else [0]
    sd = stats.std(resid)
    z = resid[-1] / sd if sd else 0.0
    s_sorted = sorted(series)
    verdict = ("refinar (prêmio alto)" if pct >= 60 and cur > 0
               else "vender bruto" if cur < 0
               else "neutro")
    return {
        "item_id": refined_id, "city": city, "days": len(series),
        "premium_now_pct": round(cur, 1),
        "percentile": round(pct),
        "z_resid": round(z, 2),
        "p10": round(s_sorted[len(s_sorted) // 10], 1),
        "median": round(s_sorted[len(s_sorted) // 2], 1),
        "p90": round(s_sorted[9 * len(s_sorted) // 10], 1),
        "verdict": verdict,
    }


def chain_city(item_id, price_q1, weight_of=None, premium=True,
               sell_mode="order", focus=False, cities=None):
    """Cidade-bônus ótima por cadeia: onde CRAFTAR sourcing os insumos onde for
    mais barato (F). Para cada cidade de craft, valora os insumos pelo menor
    preço entre as cidades (melhor fonte) e aplica o RRR de craft local; reporta
    a margem e o PESO a mover (proxy de logística — o dump não tem frete/risco
    de rota, então o transporte fica como peso, não silver).
    """
    recipe = craft.recipe_for(item_id)
    if not recipe or refining_family(item_id) or not recipe.get("inputs"):
        return []
    cat = recipe.get("category")
    cities = cities or config.ROYAL_CITIES
    sourced = []
    for inp in recipe["inputs"]:
        ps = [price_q1.get((inp["id"], c)) for c in cities]
        ps = [p for p in ps if p]
        if not ps:
            return []                      # cobertura de insumo insuficiente
        sourced.append((inp, min(ps)))
    out = []
    for ccity in cities:
        sell = price_q1.get((item_id, ccity))
        if not sell:
            continue
        cost = sum(inp["count"] * cp for inp, cp in sourced)
        rrr = craft.craft_rrr(cat, ccity, focus)
        eff = cost * (1 - rrr)
        margin = sell_revenue(sell, sell_mode, premium) - eff
        weight = sum(inp["count"] * ((weight_of(inp["id"]) if weight_of else 0) or 0)
                     for inp, _ in sourced)
        out.append({
            "item_id": item_id, "craft_city": ccity,
            "rrr_pct": round(rrr * 100, 1),
            "materials": round(cost), "eff_cost": round(eff), "sell": sell,
            "margin": round(margin),
            "margin_pct": round(100 * margin / eff, 1) if eff else None,
            "weight_to_move": round(weight, 1),
            "is_bonus_city": ccity == craft.bonus_city(cat),
        })
    out.sort(key=lambda r: -r["margin"])
    return out


# ----------------------------------------------------- 5) economia de fazenda

def farm_economy(price_q1, premium=True, sell_mode="order", cities=None,
                 min_coverage=2, limit=40):
    """Prata/dia de canteiro: cria/semente (+ração) -> produto (LATENTE).

    Lê data/farm_data.json (se existir, do dump: growtime, offspring, ração) e
    valora cria+ração vs produto. HONESTO: a cobertura de preço dos itens de
    fazenda é esparsa hoje, então a maioria sai sem preço — a função existe e
    ativa conforme a coleta cobrir os itens de fazenda (watch rebuild ajuda).
    """
    fd = craft._load("farm_data.json")
    if not fd:
        return {"available": False, "rows": [], "note":
                "data/farm_data.json ausente — rode scripts/build_farm_data.py"}
    cities = cities or config.ROYAL_CITIES

    def best(item):
        ps = [price_q1.get((item, c)) for c in cities]
        ps = [p for p in ps if p]
        return min(ps) if ps else None

    rows = []
    for baby, info in fd.items():
        grown = info.get("grown")
        growtime_d = (info.get("growtime_s") or 0) / 86400
        yield_n = 1 + (info.get("offspring_amount", 0) * info.get("offspring_chance", 0))
        seed_p = best(baby)
        grown_p = best(grown) if grown else None
        if not seed_p or not grown_p or growtime_d <= 0:
            continue
        feed = info.get("feed_cost") or 0
        revenue = yield_n * sell_revenue(grown_p, sell_mode, premium)
        profit = revenue - seed_p - feed
        rows.append({
            "baby": baby, "grown": grown,
            "yield": round(yield_n, 2),
            "cost": round(seed_p + feed),
            "revenue": round(revenue),
            "profit_per_day": round(profit / growtime_d),
        })
    rows.sort(key=lambda r: -r["profit_per_day"])
    return {"available": True, "rows": rows[:limit] if limit else rows,
            "priced": len(rows)}


def refine_premium(refined_id, price_q1, premium=True, sell_mode="order",
                   focus=False, cities=None):
    """Prêmio de refinar vs vender o bruto, por cidade (instantâneo, cache).

    premio_pct = [receita_refinado(1-imposto-anúncio) - custo_brutos(1-RRR)]
                 / custo_brutos. Positivo = refinar paga; negativo = vender o
    bruto. Use o histórico (cmd) para o percentil do prêmio no tempo.
    """
    recipe = craft.recipe_for(refined_id)
    if not recipe or not refining_family(refined_id):
        return []
    cities = cities or config.ROYAL_CITIES
    anchors = _anchor_cities(price_q1, refined_id, cities, q=None)
    out = []
    for city in cities:
        if city in anchors:
            continue
        sell = price_q1.get((refined_id, city))
        if not sell:
            continue
        cost, ok = 0.0, True
        for inp in recipe["inputs"]:
            p = price_q1.get((inp["id"], city))
            if not p:
                ok = False
                break
            cost += inp["count"] * p
        if not ok or cost <= 0:
            continue
        rrr = rrr_for(refined_id, recipe, city, focus)
        net = sell_revenue(sell, sell_mode, premium)
        margin = net - cost * (1 - rrr)
        out.append({
            "item_id": refined_id, "city": city,
            "raw_cost": round(cost), "rrr_pct": round(rrr * 100, 1),
            "refined_net": round(net), "margin": round(margin),
            "premium_pct": round(100 * margin / cost, 1),
            "is_bonus_city": city == bonus_city_for(refined_id, recipe),
            "verdict": "refinar" if margin > 0 else "vender bruto",
        })
    out.sort(key=lambda r: -r["margin"])
    return out


# ------------------------------------------ 4) valor esperado por qualidade

def quality_ev(item_id, price_allq, premium=True, sell_mode="order",
               focus=False, cities=None, min_qualities=3):
    """EV do craft ponderado pela distribuição de qualidade (não só q1).

    price_allq: dict (item_id, city, quality) -> menor venda. Usa os pesos
    689/250/50/10/1 do dump. Compara a margem esperada vs a margem só-q1
    (a do cálculo padrão). Honesto: exige >= min_qualities qualidades com
    preço, senão a esperança fica enviesada; e NÃO modela o deslocamento de
    qualidade que o foco causa (só o efeito do foco no RRR).
    """
    recipe = craft.recipe_for(item_id)
    if not recipe:
        return []
    cities = cities or config.ROYAL_CITIES
    anchors = _anchor_cities(price_allq, item_id, cities, q=1)
    out = []
    for city in cities:
        if city in anchors:
            continue
        cost, ok = 0.0, True
        for inp in recipe["inputs"]:
            p = price_allq.get((inp["id"], city, 1))
            if not p:
                ok = False
                break
            cost += inp["count"] * p
        if not ok:
            continue
        priced = {q: price_allq.get((item_id, city, q)) for q in QUALITY_WEIGHTS}
        priced = {q: v for q, v in priced.items() if v}
        if len(priced) < min_qualities or 1 not in priced:
            continue
        eff = cost * (1 - rrr_for(item_id, recipe, city, focus))
        # E[venda] só sobre as qualidades com preço (renormaliza os pesos)
        wtot = sum(QUALITY_WEIGHTS[q] for q in priced)
        ev_sell = sum(QUALITY_WEIGHTS[q] / wtot * sell_revenue(priced[q], sell_mode, premium)
                      for q in priced)
        m_q1 = sell_revenue(priced[1], sell_mode, premium) - eff
        m_ev = ev_sell - eff
        out.append({
            "item_id": item_id, "city": city,
            "eff_cost": round(eff), "qualities_priced": sorted(priced),
            "margin_q1": round(m_q1), "margin_ev": round(m_ev),
            "ev_uplift": round(m_ev - m_q1),
            "is_bonus_city": city == bonus_city_for(item_id, recipe),
        })
    out.sort(key=lambda r: -r["margin_ev"])
    return out

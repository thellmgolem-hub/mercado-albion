# -*- coding: utf-8 -*-
"""Estúdio de Refino: o que refinar, quanto dá, e até onde o FOCO aguenta.

Motor PURO (sem banco, sem rede) — recebe preços por uma função `price_of` e
devolve planos. É a base única da aba web, dos comandos do Discord e da CLI:
a conta mora aqui uma vez só.

Três perguntas que nenhuma calculadora de refino responde hoje:

1. **O foco acaba no meio da operação.** O foco é estoque (10.000/dia com
   Premium, teto 30.000), não um interruptor. Um plano grande gasta o foco antes
   do fim: o resto sai com RRR baixo e pode dar PREJUÍZO. Aqui o plano é sempre
   partido em duas fases (com foco / sem foco) e diz onde PARAR.

2. **O retorno de recursos (RRR) cria operações EXTRAS — que gastam foco.** O que
   volta é insumo, e ele é refinado de novo: um estoque E rende E/(c·(1-RRR))
   unidades, não E/c. Com RRR 53,9% isso é +117% de produção — e +117% de foco
   gasto. Ignorar isso subestima o foco pela metade.

3. **A cascata de tiers.** Refinar T5 exige 1 refinado T4 por operação, que exige
   T3... O gargalo do coletor quase nunca é o bruto do tier alvo, é o refinado do
   tier de baixo. `cascade` calcula a árvore inteira (bruto por tier + foco).

Convenções do projeto: RRR reduz o CUSTO EFETIVO dos insumos (custo x (1-RRR));
imposto/taxa de anúncio via flips.sell_revenue; cidade-bônus e RRR de refino via
production.rrr_for (especialidade da família, +40% na cidade certa).
"""
import json
import re
from pathlib import Path

from . import config
from . import craft
from . import production
from .flips import sell_revenue

DATA = Path(__file__).resolve().parent.parent / "data"

# --- foco (mecânica do jogo, verificada jul/2026) ------------------------------
FOCUS_DAILY_REGEN = 10_000    # regeneração diária COM Premium (sem Premium: 0)
FOCUS_CAP = 30_000            # teto do reservatório

# taxa de uso da estação: prata por 100 de NUTRIÇÃO consumida (o dono define).
# Nutrição da operação = itemvalue do produto x 0,1125 (dump).
DEFAULT_STATION_FEE = 500

# família -> (token do bruto, token do refinado, rótulo PT)
FAMILIES = {
    "ore":   ("ORE", "METALBAR", "Minério → Barra"),
    "wood":  ("WOOD", "PLANKS", "Tronco → Tábua"),
    "fiber": ("FIBER", "CLOTH", "Fibra → Tecido"),
    "hide":  ("HIDE", "LEATHER", "Couro cru → Couro"),
    "rock":  ("ROCK", "STONEBLOCK", "Pedra → Bloco"),
}
_ENCH_RE = re.compile(r"@(\d+)$")


def _refine_data():
    return craft._load("refine_data.json")


def item_meta(item_id):
    """tier/ench/peso/itemvalue/nutrição do item (dump), ou {} se desconhecido."""
    return (_refine_data().get("items") or {}).get(item_id) or {}


def family_of(item_id):
    """Família de refino do REFINADO (T4_PLANKS -> 'wood') ou None."""
    return production.refining_family(item_id)


def raw_family_of(item_id):
    """Família do BRUTO (T4_WOOD -> 'wood') ou None."""
    base = (item_id or "").split("@")[0].split("_")
    if len(base) < 2:
        return None
    for fam, (raw_tok, _, _) in FAMILIES.items():
        if base[1] == raw_tok:
            return fam
    return None


def refined_id(fam, tier, ench=0):
    """id do material refinado: ('wood', 5, 0) -> 'T5_PLANKS'.

    Encantado usa a forma LONGA do dump (T5_PLANKS_LEVEL2@2) — a mesma que
    recipes_refining.json indexa e que a AODP cota."""
    tok = FAMILIES[fam][1]
    return f"T{tier}_{tok}_LEVEL{ench}@{ench}" if ench else f"T{tier}_{tok}"


def raw_id(fam, tier, ench=0):
    """id do recurso bruto: ('wood', 5, 0) -> 'T5_WOOD'."""
    tok = FAMILIES[fam][0]
    return f"T{tier}_{tok}_LEVEL{ench}@{ench}" if ench else f"T{tier}_{tok}"


def bonus_city(fam):
    """Cidade com a especialidade da família (RRR 36,7% sem foco / 53,9% com)."""
    return ((craft.craft_data().get("refining") or {}).get(fam) or {}).get("city")


def tier_of(item_id):
    m = re.match(r"^T(\d)_", item_id or "")
    return int(m.group(1)) if m else None


def ench_of(item_id):
    m = _ENCH_RE.search(item_id or "")
    return int(m.group(1)) if m else 0


def rrr(item_id, city, focus=False):
    """RRR (0..1) do refino do item na cidade — com ou sem foco."""
    return production.rrr_for(item_id, craft.recipe_for(item_id), city, focus)


def station_fee(item_id, fee_per_100=DEFAULT_STATION_FEE):
    """Taxa de uso da estação por operação, em prata.

    fee = nutrição x (taxa/100), com nutrição = itemvalue x 0,1125 (dump). Em
    T4 isso é ~9 prata a 500 de taxa — irrelevante; em T8 encantado passa de
    1.000 e come a margem, por isso entra na conta em vez de ser ignorado."""
    nutri = item_meta(item_id).get("nutrition") or 0
    return nutri * (fee_per_100 or 0) / 100.0


def focus_per_op(item_id, spec_fce=0):
    """Foco gasto por OPERAÇÃO (1 unidade produzida), já com a especialização.

    A especialização não muda o RRR: ela barateia o foco (0,5^(FCE/10000))."""
    recipe = craft.recipe_for(item_id)
    return craft.focus_cost((recipe or {}).get("focus") or 0, spec_fce)


def _recipe_parts(item_id):
    """(bruto, qtd_bruto, refinado_anterior|None) da receita de refino."""
    recipe = craft.recipe_for(item_id)
    if not recipe:
        return (None, 0, None)
    raw, cnt, prev = None, 0, None
    for inp in recipe["inputs"]:
        if production.is_raw_resource(inp["id"]):
            raw, cnt = inp["id"], inp["count"]
        else:
            prev = inp["id"]
    return (raw, cnt, prev)


# ------------------------------------------------------- 1) economia unitária

def unit_economics(item_id, city, price_of, *, focus=False, premium=True,
                   sell_mode="order", spec_fce=0, fee_per_100=DEFAULT_STATION_FEE,
                   sell_city=None, sell_price=None):
    """Economia de UMA unidade refinada (uma operação) na cidade.

    price_of(id, city) -> menor preço de venda (o que custa comprar). O produto
    é vendido em `sell_city` (padrão: a mesma cidade) por `sell_price` se dado.
    Devolve None se faltar cotação de algum insumo ou do produto.
    """
    raw, cnt, prev = _recipe_parts(item_id)
    if not raw:
        return None
    p_raw = price_of(raw, city)
    p_prev = price_of(prev, city) if prev else 0.0
    if not p_raw or (prev and not p_prev):
        return None
    sell = sell_price if sell_price is not None else price_of(item_id, sell_city or city)
    if not sell:
        return None
    r = rrr(item_id, city, focus)
    gross = cnt * p_raw + (p_prev or 0)          # insumos a preço cheio
    eff = gross * (1 - r)                        # o retorno abate o consumo
    fee = station_fee(item_id, fee_per_100)
    net = sell_revenue(sell, sell_mode, premium)
    foc = focus_per_op(item_id, spec_fce) if focus else 0.0
    margin = net - eff - fee
    return {
        "item_id": item_id, "city": city, "focus": bool(focus),
        "raw_id": raw, "raw_count": cnt, "prev_id": prev,
        "raw_price": round(p_raw, 1), "prev_price": round(p_prev or 0, 1),
        "rrr_pct": round(r * 100, 1),
        "gross_cost": round(gross, 1),      # prata desembolsada por operação
        "eff_cost": round(eff, 1),          # custo real (já com o retorno)
        "station_fee": round(fee, 1),
        "sell_unit": round(sell),
        "sell_net": round(net, 1),
        "margin": round(margin, 1),
        "margin_pct": round(100 * margin / (eff + fee), 1) if (eff + fee) else None,
        "focus_cost": round(foc, 1),
        "silver_per_focus": round(margin / foc, 2) if foc else None,
        # físico: quanto de insumo some do estoque por unidade produzida
        "raw_per_unit": round(cnt * (1 - r), 4),
        "prev_per_unit": round(1 - r, 4) if prev else 0.0,
    }


# ------------------------------------- 2) plano: foco escasso + orçamento + estoque

def focus_gain_per_point(u_focus, u_plain):
    """Prata que o FOCO adiciona por ponto — a métrica certa p/ alocar foco.

    Se a operação já paga sem foco, o foco só compra a DIFERENÇA de margem; se
    ela só existe com foco, o ganho é a margem inteira. Ordenar por
    margem_com_foco/foco (o jeito ingênuo) manda o foco pro degrau errado: um
    tier que lucra bem sem foco parece ótimo, mas ali o foco agrega pouco.
    """
    if not u_focus or not u_focus.get("focus_cost"):
        return None
    base = max((u_plain or {}).get("margin") or 0, 0)
    return round((u_focus["margin"] - base) / u_focus["focus_cost"], 2)


def _phase(unit, ops):
    """Agrega uma fase do plano (ops operações com a mesma economia unitária)."""
    return {
        "ops": int(ops),
        "qty": int(ops),                       # 1 operação = 1 unidade refinada
        "focus_used": round(ops * (unit["focus_cost"] or 0)),
        "raw_used": round(ops * unit["raw_per_unit"], 1),
        "prev_used": round(ops * unit["prev_per_unit"], 1),
        "invested": round(ops * (unit["eff_cost"] + unit["station_fee"])),
        "revenue": round(ops * unit["sell_net"]),
        "profit": round(ops * unit["margin"]),
        "margin_unit": unit["margin"],
        "rrr_pct": unit["rrr_pct"],
    }


def plan(item_id, city, price_of, *, focus_available=0, spec_fce=0,
         budget=None, raw_stock=None, prev_stock=None, target_qty=None,
         premium=True, sell_mode="order", fee_per_100=DEFAULT_STATION_FEE,
         sell_city=None, allow_no_focus="auto", default_ops=100):
    """Plano de refino de UM material, respeitando os três limites reais.

    Limites (o que travar primeiro manda):
      - `focus_available`: foco no reservatório (0 = plano todo sem foco);
      - `budget`: prata para comprar insumo (None = ilimitada);
      - `raw_stock` / `prev_stock`: o que você JÁ tem (modo coletor; o que faltar
        é comprado se houver orçamento);
      - `target_qty`: teto de produção desejado.

    O plano sai em DUAS fases: enquanto há foco (RRR alto) e depois sem foco
    (RRR baixo). `advice` diz se vale seguir sem foco — quando a margem sem foco
    é negativa, seguir queima prata, e o certo é parar e vender o bruto.
    """
    u_focus = unit_economics(item_id, city, price_of, focus=True, premium=premium,
                             sell_mode=sell_mode, spec_fce=spec_fce,
                             fee_per_100=fee_per_100, sell_city=sell_city)
    u_plain = unit_economics(item_id, city, price_of, focus=False, premium=premium,
                             sell_mode=sell_mode, spec_fce=spec_fce,
                             fee_per_100=fee_per_100, sell_city=sell_city)
    if not u_plain:
        return None

    def _cap(unit, budget_left, raw_left, prev_left, focus_left, qty_left):
        """Quantas operações cabem nos limites (None = limite não informado).

        Estoque próprio abastece PRIMEIRO; o que passar dele é comprado com o
        orçamento. Além do estoque assumimos a compra de TODOS os insumos da
        operação (conservador: se o estoque de um insumo sobreviver ao do outro,
        o plano real rende um pouco mais, nunca menos)."""
        per_raw, per_prev = unit["raw_per_unit"], unit["prev_per_unit"]
        caps = []                           # (operações, o que limitou)
        if qty_left is not None:
            caps.append((qty_left, "alvo atingido"))
        if unit["focus_cost"]:
            caps.append((focus_left / unit["focus_cost"], "foco"))
        stock_caps = []
        if raw_left is not None and per_raw:
            stock_caps.append(raw_left / per_raw)
        if prev_left is not None and per_prev:
            stock_caps.append(prev_left / per_prev)
        free = min(stock_caps) if stock_caps else 0.0
        if budget_left is None:
            if stock_caps:
                caps.append((free, "estoque"))   # sem prata p/ comprar: para no estoque
        else:
            unit_buy = (per_raw * unit["raw_price"]
                        + per_prev * (unit["prev_price"] or 0) + unit["station_fee"])
            paid = (budget_left / unit_buy) if unit_buy > 0 else float("inf")
            caps.append((free + max(paid, 0), "prata"))
        if not caps:                        # nenhum limite informado: lote padrão
            return int(default_ops), f"lote padrão ({default_ops} un.)"
        ops, label = min(caps, key=lambda c: c[0])
        return max(int(ops), 0), label

    focus_left = max(focus_available or 0, 0)
    ops_focus = 0
    lim_focus = lim_plain = None
    phases = []
    if u_focus and focus_left >= (u_focus["focus_cost"] or 0) > 0:
        ops_focus, lim_focus = _cap(u_focus, budget, raw_stock, prev_stock,
                                    focus_left, target_qty)
        if ops_focus:
            ph = _phase(u_focus, ops_focus)
            ph["phase"] = "com foco"
            phases.append(ph)

    used = phases[0] if phases else None
    budget_left = None if budget is None else budget - (used["invested"] if used else 0)
    raw_left = None if raw_stock is None else raw_stock - (used["raw_used"] if used else 0)
    prev_left = None if prev_stock is None else prev_stock - (used["prev_used"] if used else 0)
    qty_left = None if target_qty is None else target_qty - (used["qty"] if used else 0)

    # "auto" (padrão): só segue sem foco se a operação ainda paga. É o erro que
    # mais custa prata no refino — o foco acaba e o jogador continua no piloto
    # automático com RRR baixo. Passe True para forçar mesmo no prejuízo.
    go_plain = (u_plain["margin"] > 0) if allow_no_focus == "auto" else bool(allow_no_focus)
    ops_plain = 0
    if go_plain and (qty_left is None or qty_left > 0):
        ops_plain, lim_plain = _cap(u_plain, budget_left, raw_left, prev_left,
                                    0, qty_left)
        if ops_plain:
            ph = _phase(u_plain, ops_plain)
            ph["phase"] = "sem foco"
            phases.append(ph)

    total = {
        "qty": sum(p["qty"] for p in phases),
        "focus_used": sum(p["focus_used"] for p in phases),
        "invested": sum(p["invested"] for p in phases),
        "revenue": sum(p["revenue"] for p in phases),
        "profit": sum(p["profit"] for p in phases),
        "raw_used": round(sum(p["raw_used"] for p in phases), 1),
        "prev_used": round(sum(p["prev_used"] for p in phases), 1),
    }
    total["roi_pct"] = (round(100 * total["profit"] / total["invested"], 1)
                        if total["invested"] else None)
    # o que travou o plano (a resposta que o jogador quer: "o que me limita?").
    # Quem manda é o limite da ÚLTIMA fase executada; sem 2ª fase (parada
    # deliberada no foco) o foco é o limitador.
    if not phases:
        limiter = "sem cotação ou limite zerado"
    elif not go_plain and ops_focus:
        limiter = "foco (parada recomendada)"
    else:
        limiter = lim_plain or lim_focus

    # conselho honesto sobre seguir sem foco
    if u_plain["margin"] > 0:
        advice = "sem foco ainda dá lucro — pode seguir depois que o foco acabar"
    elif u_focus and u_focus["margin"] > 0:
        advice = ("SÓ é lucrativo COM foco — quando o foco acabar, pare e "
                  "venda o bruto (cada unidade a mais perde "
                  f"{abs(round(u_plain['margin']))} de prata)")
    else:
        advice = "não compensa nem com foco — venda o bruto"

    focus_days = (round(total["focus_used"] / FOCUS_DAILY_REGEN, 1)
                  if total["focus_used"] else 0)
    return {
        "item_id": item_id, "city": city,
        "is_bonus_city": city == bonus_city(family_of(item_id)),
        "unit_focus": u_focus, "unit_plain": u_plain,
        "phases": phases, "total": total,
        "focus_gain_per_point": focus_gain_per_point(u_focus, u_plain),
        "limiter": limiter, "advice": advice,
        "focus_days": focus_days,          # dias de regen que esse plano consome
        "focus_break_even_price": break_even_raw_price(
            item_id, city, price_of, premium=premium, sell_mode=sell_mode,
            fee_per_100=fee_per_100, sell_city=sell_city),
    }


def break_even_raw_price(item_id, city, price_of, *, premium=True,
                         sell_mode="order", fee_per_100=DEFAULT_STATION_FEE,
                         sell_city=None):
    """Preço MÁXIMO do bruto que ainda paga a operação, com e sem foco.

    Serve pro comprador: "até quanto posso pagar no minério e ainda lucrar?".
    Mantém o refinado do tier anterior no preço de mercado.
    """
    raw, cnt, prev = _recipe_parts(item_id)
    if not raw:
        return None
    sell = price_of(item_id, sell_city or city)
    p_prev = price_of(prev, city) if prev else 0.0
    if not sell or (prev and not p_prev):
        return None
    net = sell_revenue(sell, sell_mode, premium) - station_fee(item_id, fee_per_100)
    out = {}
    for label, foc in (("com_foco", True), ("sem_foco", False)):
        r = rrr(item_id, city, foc)
        if cnt <= 0 or (1 - r) <= 0:
            continue
        # net = (cnt*p_raw + p_prev) * (1-r)  =>  p_raw = [net/(1-r) - p_prev]/cnt
        p_max = (net / (1 - r) - (p_prev or 0)) / cnt
        out[label] = round(p_max, 1)
    out["raw_id"] = raw
    out["raw_price_now"] = round(price_of(raw, city) or 0, 1)
    return out


# ------------------------------------------------- 3) cascata de tiers (coletor)

def cascade(fam, target_tier, qty, city, *, ench=0, focus=True, spec_fce=0,
            min_tier=2, buy_from_tier=None):
    """Árvore FÍSICA para produzir `qty` do refinado alvo fazendo os degraus.

    Responde "quanto de bruto de cada tier e quanto de foco eu preciso?" — a
    dúvida do coletor que descobre que refinar T4 exige um refinado T3.
    `buy_from_tier`: até que tier o refinado anterior é COMPRADO em vez de feito
    (ex.: 4 = compro a barra T3 e refino do T4 pra cima).
    """
    steps = []
    need = float(qty)          # unidades do refinado do tier corrente
    tier = target_tier
    while tier >= min_tier and need > 0:
        rid = refined_id(fam, tier, ench)
        raw, cnt, prev = _recipe_parts(rid)
        if not raw:
            break
        r = rrr(rid, city, focus)
        ops = need                              # 1 operação = 1 unidade
        raw_needed = ops * cnt * (1 - r)
        prev_needed = ops * (1 - r) if prev else 0.0
        steps.append({
            "tier": tier, "refined_id": rid, "ops": round(ops, 1),
            "raw_id": raw, "raw_needed": round(raw_needed, 1),
            "prev_id": prev, "prev_needed": round(prev_needed, 1),
            "rrr_pct": round(r * 100, 1),
            "focus": round(ops * focus_per_op(rid, spec_fce)) if focus else 0,
        })
        if not prev or (buy_from_tier is not None and tier <= buy_from_tier):
            break                               # o degrau de baixo é comprado
        need, tier = prev_needed, tier - 1
    return {
        "family": fam, "target": refined_id(fam, target_tier, ench),
        "qty": qty, "city": city, "focus": bool(focus),
        "steps": steps,
        "focus_total": sum(s["focus"] for s in steps),
        "raw_total": {s["raw_id"]: s["raw_needed"] for s in steps},
    }


def from_stock(fam, stock, city, price_of, *, focus_available=0, spec_fce=0,
               ench=0, premium=True, sell_mode="order",
               fee_per_100=DEFAULT_STATION_FEE, min_tier=2, max_tier=8,
               buy_missing_prev=True, focus_tiers=None, only_profitable=True,
               budget=None):
    """Modo COLETOR: "tenho tanto de bruto em cada tier — o que sai disso?"

    stock: {tier: unidades de bruto} (ex.: {3: 500, 4: 1200, 5: 300}).
    Sobe tier a tier: o refinado produzido num degrau alimenta o seguinte, como
    no jogo. Reporta por tier a produção, o gargalo, o foco gasto e as sobras.

    Duas coisas que o coletor descobre na prática e que estão modeladas aqui:

    - **Falta o refinado do tier de baixo.** Refinar T4 exige 1 tábua T3 por
      operação; quem só coleta bruto trava no primeiro degrau. Com
      `buy_missing_prev` (padrão) o que falta é COMPRADO no mercado, e o plano
      diz quanto e quanto custa (`prev_bought`). Com `budget`, a compra respeita
      a prata disponível.
    - **O foco rende diferente em cada tier.** Por padrão (`focus_tiers=None`) o
      foco é alocado onde a prata/foco é MAIOR, não de baixo pra cima: uma
      passada seca (sem foco) estima a quantidade de cada degrau, e o foco é
      distribuído por ordem de prata/foco. Passe uma lista de tiers para mandar
      você mesmo.

    `only_profitable` (padrão) impede subir um degrau cuja margem unitária é
    negativa — o refinado do degrau anterior fica como produto final.
    """
    focus_total = max(focus_available or 0, 0)
    tiers = [t for t in range(min_tier, max_tier + 1)
             if craft.recipe_for(refined_id(fam, t, ench))]
    econ = {}
    for t in tiers:
        rid = refined_id(fam, t, ench)
        econ[t] = {
            "rid": rid,
            "f_op": focus_per_op(rid, spec_fce),
            "focus": unit_economics(rid, city, price_of, focus=True, premium=premium,
                                    sell_mode=sell_mode, spec_fce=spec_fce,
                                    fee_per_100=fee_per_100),
            "plain": unit_economics(rid, city, price_of, focus=False, premium=premium,
                                    sell_mode=sell_mode, spec_fce=spec_fce,
                                    fee_per_100=fee_per_100),
        }

    def _run(focus_budget_by_tier, spend_budget):
        """Uma passada da cascata. Devolve (linhas, foco gasto, prata gasta)."""
        carry, rows, spent = 0.0, [], 0.0
        leftovers, focus_spent = {}, 0.0
        for tier in tiers:
            e = econ[tier]
            rid, raw, cnt, prev = e["rid"], *_recipe_parts(e["rid"])
            have_raw = float(stock.get(tier, 0) or 0)
            if have_raw <= 0 and carry <= 0:
                continue
            u_plain, u_focus = e["plain"], e["focus"]
            if only_profitable and u_plain and u_focus \
                    and u_plain["margin"] <= 0 and u_focus["margin"] <= 0:
                # nem com foco compensa: para aqui e guarda o que veio de baixo
                if carry > 0:
                    leftovers[refined_id(fam, tier - 1, ench)] = round(carry, 1)
                    carry = 0.0
                if have_raw:
                    leftovers[raw] = round(have_raw, 1)
                continue
            f_op, f_left = e["f_op"], focus_budget_by_tier.get(tier, 0.0)
            made_focus = made_plain = used_raw = used_prev = bought_prev = 0.0
            for use_focus in (True, False):
                unit = u_focus if use_focus else u_plain
                if not unit:
                    continue
                if use_focus and (f_op <= 0 or f_left < f_op):
                    continue
                # a fase SEM foco é onde o coletor queima prata no automático:
                # se ela não paga, o degrau para quando o foco acaba
                if only_profitable and unit["margin"] <= 0:
                    continue
                per_raw, per_prev = unit["raw_per_unit"], unit["prev_per_unit"]
                caps = []
                if per_raw:
                    caps.append((have_raw - used_raw) / per_raw)
                if prev and per_prev:
                    own = max(carry - used_prev, 0.0)
                    if buy_missing_prev:
                        if spend_budget is not None:
                            extra = max(spend_budget - spent, 0.0)
                            price_prev = unit["prev_price"] or 0
                            buyable = (extra / (per_prev * price_prev)
                                       if price_prev else float("inf"))
                            caps.append(own / per_prev + buyable)
                    else:
                        caps.append(own / per_prev)
                if use_focus:
                    caps.append(f_left / f_op)
                ops = int(max(min(caps), 0)) if caps else 0
                if ops <= 0:
                    continue
                used_raw += ops * per_raw
                need_prev = ops * per_prev
                from_own = min(need_prev, max(carry - used_prev, 0.0))
                bought = need_prev - from_own
                bought_prev += bought
                spent += bought * (unit["prev_price"] or 0)
                used_prev += need_prev
                if from_own and rows:
                    # o que este degrau comeu da produção do degrau de baixo
                    rows[-1]["consumed_by_next"] = round(
                        rows[-1].get("consumed_by_next", 0) + from_own, 1)
                if use_focus:
                    made_focus += ops
                    f_left -= ops * f_op
                    focus_spent += ops * f_op
                else:
                    made_plain += ops
            made = made_focus + made_plain
            rest_raw = have_raw - used_raw
            if made <= 0:
                if have_raw:
                    leftovers[raw] = round(have_raw, 1)
                continue
            unit_ref = u_focus if made_focus >= made_plain else u_plain
            # gargalo = o que impede MAIS UMA operação (não sobra fracionária)
            per_raw_last = (unit_ref or {}).get("raw_per_unit") or 0
            per_prev_last = (unit_ref or {}).get("prev_per_unit") or 0
            if per_raw_last and rest_raw < per_raw_last:
                gargalo = f"bruto T{tier}"
            elif prev and not buy_missing_prev \
                    and (carry - used_prev) < per_prev_last:
                gargalo = f"refinado T{tier - 1}"
            elif prev and buy_missing_prev and spend_budget is not None \
                    and (spend_budget - spent) < per_prev_last * (unit_ref["prev_price"] or 0):
                gargalo = "prata"
            elif f_op and f_left < f_op:
                gargalo = "foco"
            else:
                gargalo = "—"
            rows.append({
                "tier": tier, "refined_id": rid, "raw_id": raw,
                "made": int(made), "made_focus": int(made_focus),
                "made_plain": int(made_plain),
                "consumed_by_next": 0,     # preenchido pelo degrau de cima
                "raw_used": round(used_raw, 1), "raw_left": round(max(rest_raw, 0), 1),
                "prev_id": prev, "prev_used": round(used_prev, 1),
                "prev_bought": round(bought_prev, 1),
                "prev_cost": round(bought_prev * ((unit_ref or {}).get("prev_price") or 0)),
                "focus_used": round(made_focus * f_op),
                "silver_per_focus": (u_focus or {}).get("silver_per_focus"),
                "focus_gain_per_point": focus_gain_per_point(u_focus, u_plain),
                "bottleneck": gargalo,
                "unit_margin": (unit_ref or {}).get("margin"),
                # receita SE vender neste degrau; o tier intermediário é
                # consumido pelo próximo (não somar as linhas)
                "value_if_sold": round(made * unit_ref["sell_net"]) if unit_ref else None,
                # valor AGREGADO por este degrau (insumos valorados a mercado);
                # somar as linhas aqui é legítimo, é a cadeia telescópica
                "profit": round(made_focus * (u_focus["margin"] if u_focus else 0)
                                + made_plain * (u_plain["margin"] if u_plain else 0)),
            })
            if rest_raw > 0.5:
                leftovers[raw] = round(rest_raw, 1)
            carry = made          # o refinado deste tier alimenta o próximo
        return rows, focus_spent, spent, leftovers, carry

    # Passada SECA só para estimar a quantidade de cada degrau. Ela roda com
    # foco ILIMITADO de propósito: um degrau que só é lucrativo COM foco (comum
    # no tier alto) desaparece de uma estimativa sem foco e nunca receberia
    # orçamento de foco na distribuição — ficava fora do plano por engano.
    dry_rows, _, _, _, _ = _run({t: float("inf") for t in tiers}, budget)
    dry_qty = {r["tier"]: r["made"] for r in dry_rows}

    # orçamento de foco por tier: onde o foco AGREGA mais (ganho incremental)
    if focus_tiers is None:
        order = sorted(dry_qty, key=lambda t: -(
            focus_gain_per_point(econ[t]["focus"], econ[t]["plain"]) or 0))
    else:
        order = [t for t in focus_tiers if t in dry_qty]
    budget_by_tier, left = {}, focus_total
    for t in order:
        f_op = econ[t]["f_op"]
        if f_op <= 0 or left <= 0:
            continue
        want = dry_qty.get(t, 0) * f_op       # já estimado no cenário com foco
        take = min(want, left)
        budget_by_tier[t] = take
        left -= take

    rows, focus_spent, spent, leftovers, _ = _run(budget_by_tier, budget)
    # produto FINAL de cada degrau = o que não virou insumo do degrau de cima
    products = []
    for r in rows:
        r["kept"] = int(max(r["made"] - (r.get("consumed_by_next") or 0), 0))
        if r["kept"]:
            products.append({"item_id": r["refined_id"], "qty": r["kept"],
                             "tier": r["tier"]})
    return {
        "family": fam, "city": city, "rows": rows, "products": products,
        "focus_used": round(focus_spent),
        "focus_left": round(focus_total - focus_spent),
        "focus_order": order,
        "silver_spent": round(spent),
        "profit": sum(r["profit"] for r in rows),
        "top_refined": rows[-1]["refined_id"] if rows else None,
        "top_qty": rows[-1]["made"] if rows else 0,
        "leftovers": leftovers,
    }


# ----------------------------------------------------------- 4) ranking geral

def ranking(price_of, *, cities=None, focus_available=None, spec_fce=0,
            budget=None, premium=True, sell_mode="order",
            fee_per_100=DEFAULT_STATION_FEE, tiers=None, families=None,
            ench=0, limit=40, sort="profit"):
    """Ranking de "onde ponho meu foco e minha prata agora".

    Diferente de production.refine_premium (que ordena por margem UNITÁRIA),
    aqui o ranking respeita os limites reais: com X de foco e Y de prata, quanto
    cada refinado renderia NO TOTAL. É a resposta do usuário que quer lucro
    ABSOLUTO, não porcentagem bonita em cima de uma operação só.
    """
    cities = cities or config.ROYAL_CITIES
    tiers = tiers or [2, 3, 4, 5, 6, 7, 8]
    families = families or list(FAMILIES)
    out = []
    for fam in families:
        for tier in tiers:
            rid = refined_id(fam, tier, ench)
            if not craft.recipe_for(rid):
                continue
            best = None
            for city in cities:
                p = plan(rid, city, price_of, focus_available=focus_available or 0,
                         spec_fce=spec_fce, budget=budget, premium=premium,
                         sell_mode=sell_mode, fee_per_100=fee_per_100)
                if not p or not p["phases"]:
                    continue
                row = {
                    "item_id": rid, "family": fam, "tier": tier, "city": city,
                    "is_bonus_city": p["is_bonus_city"],
                    "qty": p["total"]["qty"],
                    "profit": p["total"]["profit"],
                    "invested": p["total"]["invested"],
                    "roi_pct": p["total"]["roi_pct"],
                    "focus_used": p["total"]["focus_used"],
                    "margin_unit": p["unit_focus"]["margin"] if p["unit_focus"]
                    else p["unit_plain"]["margin"],
                    "silver_per_focus": (p["unit_focus"] or {}).get("silver_per_focus"),
                    "focus_gain_per_point": p["focus_gain_per_point"],
                    "limiter": p["limiter"],
                }
                if best is None or row["profit"] > best["profit"]:
                    best = row
            if best:
                out.append(best)
    keys = {"profit": lambda r: -(r["profit"] or 0),
            "roi": lambda r: -(r["roi_pct"] or 0),
            # "focus" ordena pelo que o foco AGREGA, não pela margem total/foco
            "focus": lambda r: -(r["focus_gain_per_point"] or 0),
            "unit": lambda r: -(r["margin_unit"] or 0)}
    out.sort(key=keys.get(sort, keys["profit"]))
    return out[:limit] if limit else out

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
  crafting_laborer_economy modela o trabalhador de FABRICAÇÃO por inteiro:
  encher craftando (custo real de material, fama do dump) + o RETORNO que o
  trabalhador devolve (lootlist ponderada por peso) — Play B (alimentar) vs Play
  A (só flipar o cheio).
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


# ------------------------------------------ trabalhador de FABRICAÇÃO (completo)
def _loot_id(entry):
    """id precificável do item de loot: 'T6_PLANKS_LEVEL1' + ench 1 -> ...@1.

    O refinado encantado já vem na forma LONGA no dump (T6_PLANKS_LEVEL1); o
    cache/items_db indexa exatamente 'T6_PLANKS_LEVEL1@1'. Sem encantamento
    (ench 0) o id é o próprio (T6_PLANKS)."""
    ench = entry.get("ench") or 0
    return f"{entry['item']}@{ench}" if ench else entry["item"]


def _fill_plan(info, price_q1, cities, premium, sell_mode):
    """Como ENCHER o diário craftando — a jogada REAL: você crafta o item, ganha
    a fama (enche o diário) e VENDE o item. Encher NÃO consome o item.

    Por isso o custo real de encher NÃO é o material bruto: é a MARGEM DE CRAFT
    (venda_líquida_do_item − custo_material_net_RRR) somada sobre os n_crafts.
    Se o craft dá lucro (margem ≥ 0), encher pode SAIR DE GRAÇA ou até PAGAR.

    Fama por craft: o dump dá famefillingmissions.craftitemfame.@value — a fama
    de UM craft de item no @mintier. Todos os validitem de um diário são do
    mintier daquele diário (verificado: T6 só tem itens T6), então tratamos a
    fama/craft como constante = fame_value p/ TODO validitem. Isso é PROXY
    (fill_cost_is_proxy): a fama real escala com o itemvalue dos insumos, então
    um item maior renderia mais fama e encheria em menos crafts.

    n_crafts = max_fame / fame_value. Para cada validitem VENDÁVEL (venda_líq>0):
      custo_material = Σ(count × best_buy(recurso)) × (1 - RRR)   [cidade-bônus]
      margem_craft   = venda_líquida(validitem) − custo_material
    Escolhe o validitem que MAXIMIZA n_crafts × margem_craft (o craft mais
    lucrativo / menos deficitário e vendável). Devolve dois custos de encher:
      fill_cost_selling    = −(n_crafts × margem_craft)  (crédito se margem>0)
      fill_cost_discarding =  n_crafts × custo_material   (pessimista: item fora)
    """
    fill = info.get("fill")
    if not fill or not fill.get("fame_value"):
        return None
    fame_value = fill["fame_value"]
    crafts = info["max_fame"] / fame_value if fame_value else 0
    # candidatos vendáveis. Maximizamos margem de craft, MAS o "melhor/maior"
    # preço é o alvo preferido de ordem-isca: um validitem cotado em <3 cidades
    # pode ser âncora que escapou do saneamento (clean_price_rows só age com >=3
    # cidades). Por isso preferimos o de MAIOR margem entre os LÍQUIDOS (>=3
    # cidades cotando venda); só caímos nos ilíquidos se nenhum líquido existir
    # (e aí o flag fill_liquidez_baixa avisa). Assim o default não persegue isca.
    cands = []   # (margem, vid, bcity, rrr%, eff_mat, item_net, item_city, n_cid)
    for vid in fill["items"]:
        recipe = craft.recipe_for(vid)
        if not recipe or not recipe.get("inputs"):
            continue
        item_sell = _best_sell(price_q1, vid, cities, sell_mode, premium)
        if not item_sell or item_sell[1] <= 0:
            continue                  # exige item vendável (com cotação de venda)
        cat = recipe.get("category")
        bcity = craft.unified_bonus_city(vid, cat) or (cities[0] if cities else None)
        mat, ok = 0.0, True
        for inp in recipe["inputs"]:
            b = _best_buy(price_q1, inp["id"], cities)
            if not b:
                ok = False
                break
            mat += inp["count"] * b[1]
        if not ok:
            continue
        rrr = craft.unified_rrr(vid, cat, bcity) if bcity else 0.0
        out_qty = recipe.get("output", 1) or 1
        eff_mat = mat * (1 - rrr)
        margin = item_sell[1] * out_qty - eff_mat     # margem de craft (por craft)
        n_sell_cities = sum(1 for c in cities if (price_q1.get((vid, c)) or 0) > 0)
        cands.append((margin, vid, bcity, round(rrr * 100, 1), round(eff_mat),
                      round(item_sell[1]), item_sell[0], n_sell_cities))
    if not cands:
        return None
    liquid = [c for c in cands if c[7] >= 3]
    best = max(liquid or cands, key=lambda c: c[0])   # maior margem; líquido 1º
    margin, vid, bcity, rrr_pct, eff_mat, item_net, item_city, n_cities = best
    return {
        # jogada real: encher = pagar a margem NEGATIVA de craft (crédito se +).
        "fill_cost_selling": round(-crafts * margin),
        # pessimista: item jogado fora, custo = material bruto net RRR.
        "fill_cost_discarding": round(crafts * eff_mat),
        "crafts_to_fill": round(crafts, 2),
        "fill_item": vid,
        "fill_city": bcity,
        "fill_rrr_pct": rrr_pct,
        "craft_margin_unit": round(margin),      # margem de craft por unidade
        "craft_sell_net": item_net,              # venda líquida do item craftado
        "craft_sell_city": item_city,
        "fill_sell_cities": n_cities,            # cidades cotando venda do item
        "material_cost_unit": eff_mat,           # material net RRR por craft
        "fill_cost_is_proxy": True,   # fama/craft = fame_value p/ todo validitem
    }


def crafting_laborer_economy(price_q1, premium=True, sell_mode="order",
                             cities=None, limit=60):
    """Modelo COMPLETO do trabalhador de FABRICAÇÃO (diário que enche craftando).

    Diferente de laborer_economy (que só mede a margem de FLIP do diário
    vazio->cheio), aqui modelamos os DOIS lados do trabalhador:

    - retorno_por_diario = base_loot_amount × Σ_loot[ p(i) × amount(i) ×
      best_sell_net(loot_i) ], p(i) = weight_i / Σweight (do dump). O trabalhador
      faz base_loot_amount rolagens por diário cheio; cada rolagem sorteia um item
      da lootlist (refinados + variantes encantadas) por peso. Itens de loot sem
      cotação contribuem 0 (piso conservador; veja loot_priced_weight_pct).
    - ENCHER craftando é a jogada real: você crafta o validitem, ganha a fama e
      VENDE o item — não o descarta. Logo o custo de encher é a MARGEM DE CRAFT,
      não o material bruto (ver _fill_plan). Escolhe o validitem que maximiza
      n_crafts × margem_craft (vendável, venda_líq>0). fama/craft é PROXY.
    - vazio = best_buy(diário _EMPTY); cheio = best_sell(diário _FULL).
    - lucro_alimentar_vendendo = retorno_por_diario + n_crafts × margem_craft
      − vazio   (Play B REAL: enche craftando e VENDE os itens craftados).
    - lucro_alimentar_descartando = retorno_por_diario − n_crafts × material
      − vazio   (pessimista: o item craftado é jogado fora).
    - lucro_flip = cheio − vazio   (Play A: só vender o diário cheio — a
      alternativa que laborer_economy mede).

    Só diários com fill.items (enchem craftando: HUNTER/MAGE/MERCENARY/TOOLMAKER/
    WARRIOR). Coleta/pesca enchem coletando e ficam de fora. Ordena por
    lucro_alimentar_vendendo desc. Somente-leitura sobre price_q1 (dict), sem SQL.
    """
    data = _load().get("laborers") or {}
    if not data:
        return {"available": False, "rows": []}
    cities = cities or config.CITIES
    rows = []
    for empty, info in data.items():
        if not (info.get("fill") and info["fill"].get("items")):
            continue                      # só trabalhador de FABRICAÇÃO
        loot = info.get("loot") or []
        total_w = sum(l.get("weight") or 0 for l in loot)
        if total_w <= 0:
            continue
        ret_per_full, priced_w, top = 0.0, 0.0, None
        for l in loot:
            w = l.get("weight") or 0
            if w <= 0:
                continue
            lid = _loot_id(l)
            s = _best_sell(price_q1, lid, cities, sell_mode, premium)
            if not s:
                continue
            contrib = (w / total_w) * (l.get("amount") or 0) * s[1]
            ret_per_full += contrib
            priced_w += w
            if top is None or contrib > top[0]:
                top = (contrib, l["item"], s[0], round(s[1]))
        ret_per_full *= info.get("base_loot_amount") or 0
        buy = _best_buy(price_q1, empty, cities)
        sell = _best_sell(price_q1, info["full"], cities, sell_mode, premium)
        fp = _fill_plan(info, price_q1, cities, premium, sell_mode)
        if not buy or not sell or fp is None:
            continue
        empty_price, full_net = buy[1], sell[1]
        # encher VENDENDO: custo = fill_cost_selling (crédito se margem>0)
        lucro_vendendo = ret_per_full - fp["fill_cost_selling"] - empty_price
        lucro_descartando = ret_per_full - fp["fill_cost_discarding"] - empty_price
        lucro_flip = full_net - empty_price
        # liquidez: o item de fill é pouco cotado? O motor pega a MAIOR margem,
        # então um item vendido em <3 cidades pode ser ordem-isca que escapou do
        # saneamento (que só zera outlier com >=3 cidades). Aviso p/ o usuário.
        thin = (fp["fill_city"] is None or fp["craft_sell_net"] <= 0
                or fp["fill_sell_cities"] < 3)
        rows.append({
            "empty": empty, "full": info["full"],
            "family": info["family"], "tier": info["tier"],
            "retorno_por_diario": round(ret_per_full),
            "loot_top": top[1] if top else None,          # recurso mais devolvido
            "loot_sell_city": top[2] if top else None,
            "loot_priced_weight_pct": round(100 * priced_w / total_w, 1),
            # encher (jogada real: crafta, ganha fama e VENDE o item)
            "custo_encher_vendendo": fp["fill_cost_selling"],
            "custo_encher_descartando": fp["fill_cost_discarding"],
            "fill_item": fp["fill_item"],
            "fill_city": fp["fill_city"],
            "crafts_to_fill": fp["crafts_to_fill"],
            "margem_craft_un": fp["craft_margin_unit"],   # margem de craft/un
            "craft_venda_liquida": fp["craft_sell_net"],  # venda líq do item
            "material_un": fp["material_cost_unit"],
            "fill_sell_cities": fp["fill_sell_cities"],   # cidades cotando o item
            "fill_cost_is_proxy": fp["fill_cost_is_proxy"],
            "fill_liquidez_baixa": thin,   # item de fill pouco/nada cotado (<3 cid.)
            "buy_city": buy[0], "vazio": round(empty_price),
            "sell_city": sell[0], "cheio": round(full_net),
            "lucro_alimentar_vendendo": round(lucro_vendendo),
            "lucro_alimentar_descartando": round(lucro_descartando),
            "lucro_flip": round(lucro_flip),
        })
    rows.sort(key=lambda r: -r["lucro_alimentar_vendendo"])
    return {"available": True, "rows": rows[:limit] if limit else rows,
            "priced": len(rows)}

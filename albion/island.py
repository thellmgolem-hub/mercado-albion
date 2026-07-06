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
import re

from . import config
from . import craft
from .flips import sell_revenue

# Refinado BÁSICO: as 5 famílias de barra/tábua/couro/tecido/bloco, qualquer
# tier e encanto (_LEVELx). É o insumo "limpo" que o dono aceita no item de fill.
_REFINED_INPUT = re.compile(r"^T\d+_(PLANKS|METALBAR|LEATHER|CLOTH|STONEBLOCK)(_LEVEL\d+)?$")


def _load():
    return craft._load("island_data.json") or {}


def _farm_items():
    """Ids de itens FARMÁVEIS na fazenda (data/island_data.json): sementes e
    produtos de cultura + bebês/adultos de pecuária + comidas. São insumos
    'limpos' que o dono aceita no item de fill (ração/carne/cultura), além dos
    refinados básicos."""
    d = _load()
    out = set()
    for seed, info in (d.get("crops") or {}).items():
        out.add(seed)                       # a semente plantável
        if info.get("crop"):
            out.add(info["crop"])           # o produto colhido
    for baby, info in (d.get("animals") or {}).items():
        out.add(baby)
        if info.get("grown"):
            out.add(info["grown"])
    out.update(d.get("food") or {})         # comidas (culturas/carne)
    return out


def _input_is_clean(input_id, farm_ids, allow_farm):
    """O insumo direto do item de fill é aceitável? True se for refinado básico
    (PLANKS/METALBAR/LEATHER/CLOTH/STONEBLOCK, qualquer tier/encanto) ou, se
    allow_farm, um item farmável. Qualquer outro (SKILLBOOK, ARTEFACT, RUNE/SOUL/
    RELIC, ESSENCE, TOME, componente de mob, etc.) reprova o item de fill."""
    if _REFINED_INPUT.match(input_id or ""):
        return True
    if allow_farm and input_id in farm_ids:
        return True
    return False


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


# refinado -> família de refino -> recurso BRUTO coletável (p/ estimativa de coleta)
_REFINED_TO_RAW = {"PLANKS": "WOOD", "METALBAR": "ORE", "LEATHER": "HIDE",
                   "CLOTH": "FIBER", "STONEBLOCK": "ROCK"}


def _refined_family(input_id):
    """'T6_PLANKS' / 'T6_PLANKS_LEVEL2' -> 'PLANKS'; None se não for refinado."""
    m = _REFINED_INPUT.match(input_id or "")
    return m.group(1) if m else None


def _fill_candidates(item_ids, price_q1, cities, premium, sell_mode, *,
                     sell_ok=None, refined_only=True, allow_farm=True,
                     farm_ids=None):
    """Candidatos de FILL avaliados: margem/un, custo de material net RRR,
    elegibilidade (>=3 cidades + banda VWAP + insumo limpo) e a receita quebrada
    por família de refinado (p/ agregar material/coleta no laborer_plan).

    Reusado por _fill_plan (escolhe o melhor) e laborer_plan (aloca a cesta).
    Devolve lista de dicts; NÃO ordena.
    """
    sell_ok = sell_ok or (lambda _i, _p: True)
    farm_ids = farm_ids if farm_ids is not None else _farm_items()
    cands = []
    for vid in item_ids:
        recipe = craft.recipe_for(vid)
        if not recipe or not recipe.get("inputs"):
            continue
        item_sell = _best_sell(price_q1, vid, cities, sell_mode, premium)
        if not item_sell or item_sell[1] <= 0:
            continue                  # exige item vendável (com cotação de venda)
        item_city, item_net = item_sell[0], item_sell[1]
        raw_price = price_q1.get((vid, item_city)) or 0   # ask p/ a banda de VWAP
        cat = recipe.get("category")
        bcity = craft.unified_bonus_city(vid, cat) or (cities[0] if cities else None)
        rrr = craft.unified_rrr(vid, cat, bcity) if bcity else 0.0
        mat, ok = 0.0, True
        inputs = [{"id": i["id"], "count": i["count"]} for i in recipe["inputs"]]
        # material refinado por família, JÁ net RRR (a RRR devolve recurso, então
        # abate a QUANTIDADE efetiva de insumo — não o custo, senão dupla contagem).
        refined_by_family = {}
        for inp in recipe["inputs"]:
            b = _best_buy(price_q1, inp["id"], cities)
            if not b:
                ok = False
                break
            mat += inp["count"] * b[1]
            fam = _refined_family(inp["id"])
            if fam:
                refined_by_family[fam] = (refined_by_family.get(fam, 0.0)
                                          + inp["count"] * (1 - rrr))
        if not ok:
            continue
        clean = all(_input_is_clean(i["id"], farm_ids, allow_farm) for i in inputs)
        out_qty = recipe.get("output", 1) or 1
        eff_mat = mat * (1 - rrr)
        margin = item_net * out_qty - eff_mat     # margem de craft (por craft)
        n_sell_cities = sum(1 for c in cities if (price_q1.get((vid, c)) or 0) > 0)
        price_ok = sell_ok(vid, raw_price)
        eligible = (n_sell_cities >= 3 and price_ok
                    and (clean or not refined_only))
        cands.append({
            "margin": margin, "vid": vid, "bcity": bcity,
            "rrr_pct": round(rrr * 100, 1), "eff_mat": round(eff_mat),
            "item_net": round(item_net), "item_city": item_city,
            "n_cities": n_sell_cities, "price_ok": price_ok, "clean": clean,
            "inputs": inputs, "eligible": eligible,
            "refined_by_family": refined_by_family,   # {'PLANKS': qtd net RRR, ...}
        })
    return cands


def _fill_plan(info, price_q1, cities, premium, sell_mode, sell_ok=None,
               refined_only=True, allow_farm=True, farm_ids=None):
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

    Defesa anti-isca: como o motor persegue o MAIOR preço de venda (alvo de
    ordem-isca), um candidato só é ELEGÍVEL se: cotado em >=3 cidades E aprovado
    por sell_ok(item_id, preço_bruto) (banda de VWAP histórico, injetada pela
    CLI — island.py fica sem SQL). Escolhe o de maior margem ENTRE os elegíveis;
    se nenhum for elegível, cai no de maior margem geral e marca os flags
    (fill_liquidez_baixa / fill_isca). sell_ok=None => tudo aprovado (sem histórico).

    Insumo LIMPO (refined_only, preferência do dono): o item de fill só é
    elegível se TODOS os seus insumos DIRETOS forem refinado básico (PLANKS/
    METALBAR/LEATHER/CLOTH/STONEBLOCK, qualquer tier/encanto) ou — se allow_farm
    — item farmável (crops/animais/comida do island_data). Qualquer componente
    especial (SKILLBOOK, ARTEFACT, RUNE/SOUL/RELIC, ESSENCE, TOME, item de mob)
    torna o validitem INELEGÍVEL. Ex.: T4_BAG_INSIGHT (usa T4_SKILLBOOK_STANDARD)
    é EXCLUÍDO; T4_BAG (só T4_CLOTH+T4_LEATHER) passa. fill_item_inputs no retorno
    deixa a decisão auditável.
    """
    fill = info.get("fill")
    if not fill or not fill.get("fame_value"):
        return None
    fame_value = fill["fame_value"]
    crafts = info["max_fame"] / fame_value if fame_value else 0
    cands = _fill_candidates(fill["items"], price_q1, cities, premium, sell_mode,
                             sell_ok=sell_ok, refined_only=refined_only,
                             allow_farm=allow_farm, farm_ids=farm_ids)
    if not cands:
        return None
    pool = [c for c in cands if c["eligible"]]
    best = max(pool or cands, key=lambda c: c["margin"])  # maior margem; elegível 1º
    chosen_eligible = bool(pool)
    return {
        # jogada real: encher = pagar a margem NEGATIVA de craft (crédito se +).
        "fill_cost_selling": round(-crafts * best["margin"]),
        # pessimista: item jogado fora, custo = material bruto net RRR.
        "fill_cost_discarding": round(crafts * best["eff_mat"]),
        "crafts_to_fill": round(crafts, 2),
        "fill_item": best["vid"],
        "fill_city": best["bcity"],
        "fill_rrr_pct": best["rrr_pct"],
        "craft_margin_unit": round(best["margin"]),  # margem de craft por unidade
        "craft_sell_net": best["item_net"],          # venda líquida do item craftado
        "craft_sell_city": best["item_city"],
        "fill_sell_cities": best["n_cities"],        # cidades cotando venda do item
        "material_cost_unit": best["eff_mat"],       # material net RRR por craft
        "fill_item_inputs": best["inputs"],          # insumos DIRETOS (auditável)
        # sem candidato elegível: o escolhido é suspeito (isca / mercado fino /
        # insumo especial). Flags separam o motivo.
        "fill_eligivel": chosen_eligible,
        "fill_liquidez_baixa": not (chosen_eligible or best["n_cities"] >= 3),
        "fill_isca": not chosen_eligible and not best["price_ok"],
        "fill_impuro": not chosen_eligible and refined_only and not best["clean"],
        "fill_cost_is_proxy": True,   # fama/craft = fame_value p/ todo validitem
    }


def crafting_laborer_economy(price_q1, premium=True, sell_mode="order",
                             cities=None, limit=60, station_fee=0,
                             fill_sell_ok=None, refined_only=True,
                             allow_farm=True, yield_pct=None):
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
      n_crafts × margem_craft ENTRE os elegíveis (>=3 cidades E dentro da banda
      de VWAP via fill_sell_ok). fama/craft é PROXY.
    - vazio = best_buy(diário _EMPTY); cheio = best_sell(diário _FULL).
    - lucro_alimentar_vendendo = retorno_por_diario + n_crafts × margem_craft
      − n_crafts × station_fee − vazio   (Play B REAL: enche craftando e VENDE
      os itens; station_fee = taxa do NPC de craft por craft).
    - lucro_alimentar_descartando = retorno_por_diario − n_crafts × material
      − n_crafts × station_fee − vazio   (pessimista: o item é jogado fora).
    - lucro_flip = cheio − vazio   (Play A: só vender o diário cheio — a
      alternativa que laborer_economy mede).

    station_fee (prata por CRAFT, default 0): a taxa do NPC da estação varia por
    cidade/dia e é ENTRADA do usuário — na plataforma/Discord vira um CAMPO
    preenchido na hora do craft. NÃO tem default != 0 (não inventamos taxa).
    fill_sell_ok(item_id, preço_bruto) -> bool: validador anti-isca por VWAP
    histórico, injetado pela CLI (island.py fica sem SQL). None => sem histórico
    (tudo aprovado; a defesa cai só nas >=3 cidades).
    refined_only (default True, preferência do dono): o item de fill só é
    elegível se TODOS os insumos diretos forem refinado básico (+farm se
    allow_farm); item com componente especial (skillbook/artefato/runa/…) é
    excluído. fill_item_inputs no retorno deixa isso auditável.
    yield_pct (default None = loot pleno): % de rendimento de FELICIDADE
    (island.journal_yield, 100..150) que multiplica o retorno_por_diario.
    Ainda SEM consumidor na CLI/endpoint — integração de UX fica p/ depois.

    Só diários com fill.items (enchem craftando: HUNTER/MAGE/MERCENARY/TOOLMAKER/
    WARRIOR). Coleta/pesca enchem coletando e ficam de fora. Ordena por
    lucro_alimentar_vendendo desc. Somente-leitura sobre price_q1 (dict), sem SQL.
    """
    data = _load().get("laborers") or {}
    if not data:
        return {"available": False, "rows": []}
    farm_ids = _farm_items()
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
        if yield_pct is not None:
            # rendimento de FELICIDADE (journal_yield): 100 = loot pleno
            ret_per_full *= float(yield_pct) / 100.0
        buy = _best_buy(price_q1, empty, cities)
        sell = _best_sell(price_q1, info["full"], cities, sell_mode, premium)
        fp = _fill_plan(info, price_q1, cities, premium, sell_mode,
                        sell_ok=fill_sell_ok, refined_only=refined_only,
                        allow_farm=allow_farm, farm_ids=farm_ids)
        if not buy or not sell or fp is None:
            continue
        empty_price, full_net = buy[1], sell[1]
        crafts = fp["crafts_to_fill"]
        fee_total = crafts * (station_fee or 0)   # taxa do NPC × nº de crafts
        # encher VENDENDO: custo = fill_cost_selling (crédito se margem>0) + taxa
        lucro_vendendo = (ret_per_full - fp["fill_cost_selling"] - fee_total
                          - empty_price)
        lucro_descartando = (ret_per_full - fp["fill_cost_discarding"] - fee_total
                             - empty_price)
        lucro_flip = full_net - empty_price
        thin = fp["fill_liquidez_baixa"]
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
            "fill_item_inputs": fp["fill_item_inputs"],   # insumos diretos (auditável)
            "station_fee": round(station_fee or 0),       # taxa NPC por craft (entrada)
            "custo_taxa_estacao": round(fee_total),        # taxa × n_crafts
            "fill_cost_is_proxy": fp["fill_cost_is_proxy"],
            "fill_eligivel": fp["fill_eligivel"],   # o escolhido passou nos filtros
            "fill_liquidez_baixa": thin,   # nenhum candidato elegível (<3 cid.)
            "fill_isca": fp["fill_isca"],  # escolhido caiu fora da banda de VWAP
            "fill_impuro": fp["fill_impuro"],  # escolhido tem insumo especial
            "buy_city": buy[0], "vazio": round(empty_price),
            "sell_city": sell[0], "cheio": round(full_net),
            "lucro_alimentar_vendendo": round(lucro_vendendo),
            "lucro_alimentar_descartando": round(lucro_descartando),
            "lucro_flip": round(lucro_flip),
        })
    rows.sort(key=lambda r: -r["lucro_alimentar_vendendo"])
    return {"available": True, "rows": rows[:limit] if limit else rows,
            "priced": len(rows)}


# ------------------------------------ OTIMIZADOR de diversificação + produção
def _worker_return(info, price_q1, cities, premium, sell_mode):
    """Retorno do trabalhador por diário cheio = base_loot_amount × Σ_loot[
    p(i) × amount(i) × venda_líq(loot_i) ]. Devolve (retorno, cobertura_peso%)."""
    loot = info.get("loot") or []
    total_w = sum(l.get("weight") or 0 for l in loot)
    if total_w <= 0:
        return 0.0, 0.0
    ret, priced_w = 0.0, 0.0
    for l in loot:
        w = l.get("weight") or 0
        if w <= 0:
            continue
        s = _best_sell(price_q1, _loot_id(l), cities, sell_mode, premium)
        if not s:
            continue
        ret += (w / total_w) * (l.get("amount") or 0) * s[1]
        priced_w += w
    return ret * (info.get("base_loot_amount") or 0), 100 * priced_w / total_w


def laborer_plan(price_q1, *, family, tier, n_laborers, journals_per_day=1,
                 n_crafts=3, market_depth=0.2, station_fee=0, premium=True,
                 sell_mode="order", cities=None, item_volumes=None,
                 refined_only=True, allow_farm=True, farm_ids=None,
                 fill_sell_ok=None, yield_pct=None):
    """Otimizador de DIVERSIFICAÇÃO de fill + lista de produção p/ N trabalhadores.

    Mecânica: 1 diário/dia por laborer (processa ~22h≈1d), n_crafts por diário.
    Com muitos laborers da MESMA família, encher todos com o MESMO item de fill
    derruba o preço dele — então diversificamos: aloca a produção entre vários
    itens de fill elegíveis, cada um até ~market_depth (20%) do seu volume diário.

    Passos:
      1. Candidatos = itens de fill ELEGÍVEIS de (family,tier): refined-only +
         >=3 cidades + banda VWAP (via _fill_candidates). Exige volume_diário>0
         em item_volumes (sem volume não dá p/ vender — EXCLUI).
      2. crafts_needed = n_laborers × journals_per_day × n_crafts.
      3. cap_item = floor(market_depth × volume_diário) crafts/dia.
      4. GULOSO por margem/un desc: enche cada item até seu cap até somar
         crafts_needed. Se Σcaps < needed => market_limited.
      5. Agrega material refinado/dia por família (net RRR) e a coleta bruta
         (~1 bruto/refinado; estimativa).
      6. lucro_dia = N_diários×retorno_trab + Σ(crafts×margem/un)
                     − N_diários×vazio − crafts_alocados×station_fee.

    Somente-leitura sobre price_q1 (dict); volumes e VWAP vêm de fora (sem SQL).
    yield_pct (default None = loot pleno): % de rendimento de FELICIDADE
    (island.journal_yield, 100..150) que multiplica o retorno do trabalhador.
    Ainda SEM consumidor na CLI/endpoint — integração de UX fica p/ depois.
    """
    cities = cities or config.CITIES
    fam = (family or "").upper()
    empty = f"T{tier}_JOURNAL_{fam}_EMPTY"
    info = (_load().get("laborers") or {}).get(empty)
    if not info or not (info.get("fill") and info["fill"].get("items")):
        return {"available": False, "reason": "família/tier sem diário de fabricação",
                "family": fam, "tier": tier}
    item_volumes = item_volumes or {}
    farm_ids = farm_ids if farm_ids is not None else _farm_items()
    cands = _fill_candidates(info["fill"]["items"], price_q1, cities, premium,
                             sell_mode, sell_ok=fill_sell_ok,
                             refined_only=refined_only, allow_farm=allow_farm,
                             farm_ids=farm_ids)
    # só elegíveis E com volume diário conhecido (>0) — sem volume não vende.
    pool = []
    for c in cands:
        if not c["eligible"]:
            continue
        vol = item_volumes.get(c["vid"]) or 0
        if vol <= 0:
            continue
        c = {**c, "volume": vol, "cap": int(market_depth * vol)}
        if c["cap"] <= 0:
            continue
        pool.append(c)
    pool.sort(key=lambda c: -c["margin"])   # guloso: melhor margem primeiro

    crafts_needed = n_laborers * journals_per_day * n_crafts
    basket, allocated = [], 0
    for c in pool:
        if allocated >= crafts_needed:
            break
        take = min(c["cap"], crafts_needed - allocated)
        if take <= 0:
            continue
        allocated += take
        basket.append({
            "item": c["vid"], "crafts_dia": take, "cap": c["cap"],
            "volume_dia": c["volume"],
            "pct_mercado": round(100 * take / c["volume"], 1) if c["volume"] else None,
            "margem_un": round(c["margin"]),
            "sell_city": c["item_city"], "craft_city": c["bcity"],
            "lucro_item_dia": round(take * c["margin"]),
            "inputs": c["inputs"],
            "_refined": c["refined_by_family"],
        })
    total_caps = sum(c["cap"] for c in pool)
    market_limited = allocated < crafts_needed

    # material refinado/dia por família (net RRR) + coleta bruta estimada (~1:1)
    refined_day, raw_day = {}, {}
    for b in basket:
        for f, per_craft in b["_refined"].items():
            qty = per_craft * b["crafts_dia"]
            refined_day[f] = refined_day.get(f, 0.0) + qty
            raw = _REFINED_TO_RAW.get(f)
            if raw:
                raw_day[raw] = raw_day.get(raw, 0.0) + qty   # ~1 bruto/refinado
    for b in basket:
        b.pop("_refined", None)

    ret_per_journal, loot_cov = _worker_return(info, price_q1, cities, premium,
                                               sell_mode)
    if yield_pct is not None:
        # rendimento de FELICIDADE (journal_yield): 100 = loot pleno
        ret_per_journal *= float(yield_pct) / 100.0
    buy = _best_buy(price_q1, empty, cities)
    empty_price = buy[1] if buy else None
    n_journals = n_laborers * journals_per_day
    margin_total = sum(b["lucro_item_dia"] for b in basket)
    ret_total = n_journals * ret_per_journal
    empty_total = n_journals * (empty_price or 0)
    fee_total = allocated * (station_fee or 0)
    profit_day = ret_total + margin_total - empty_total - fee_total

    laborers_feedable = (allocated // (journals_per_day * n_crafts)
                         if journals_per_day * n_crafts else 0)
    return {
        "available": True,
        "family": fam, "tier": tier,
        "n_laborers": n_laborers, "journals_per_day": journals_per_day,
        "n_crafts": n_crafts, "market_depth": market_depth,
        "crafts_needed": crafts_needed, "crafts_allocated": allocated,
        "market_capacity": total_caps,        # Σ caps (crafts/dia absorvíveis)
        "market_limited": market_limited,
        "laborers_feedable": laborers_feedable,   # quantos dá p/ alimentar de fato
        "empty_missing": empty_price is None,
        # cesta diversificada
        "basket": basket,
        # material & coleta por dia
        "refined_per_day": {f: round(q) for f, q in refined_day.items()},
        "raw_gather_per_day": {r: round(q) for r, q in raw_day.items()},
        "raw_is_estimate": True,               # coleta ~1 bruto/refinado
        # resumo do lucro (partes expostas)
        "return_per_journal": round(ret_per_journal),
        "loot_priced_weight_pct": round(loot_cov, 1),
        "empty_price": round(empty_price) if empty_price is not None else None,
        "station_fee": round(station_fee or 0),
        "worker_return_total": round(ret_total),
        "craft_margin_total": round(margin_total),
        "empty_cost_total": round(empty_total),
        "station_fee_total": round(fee_total),
        "profit_day": round(profit_day),
        "fill_cost_is_proxy": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Felicidade do trabalhador — MODELO REAL DO PAINEL do jogo (calibrado em
# 2026-07-05 com screenshot ao vivo: Ferreiro T6 em Guild Hall T7 → painel
# "Camas 200/200 · Mesas 200/200 · Troféus 25/100 · Total 425/500" e rendimento
# por tier de diário T2=150%, T3=150%, T4=112,5%).
#
# 1. Teto do painel por tier L do TRABALHADOR: total_max = 100×L − 100.
#    Split: cap_camas = cap_mesas = (total_max − 100)/2 = 50×L − 100;
#    cap_trofeus = 100 (FIXO). Confirmação independente: T4 antigo = "300/300"
#    com troféu "100/100" (fórum) → 100+100+100.
# 2. Camas/mesas pontuam o CAP quando há peças de tier >= L em quantidade
#    suficiente (1 cama cobre 1 trabalhador; 1 mesa cobre 6). Abaixo disso a
#    pontuação cai — modelamos score = cap × min(1, tier/L) × min(1, cobertura)
#    e marcamos como ESTIMATIVA (só o caso "maxado" está calibrado com o jogo).
# 3. Troféus (0..100): +5 por TIER de troféu GERAL com cobertura (T2..T8; mesmo
#    tier não empilha; cada cópia cobre 3 → ceil(N/3) cópias p/ N trabalhadores);
#    +10 por tier de troféu de TIPO — só COLETA (WOOD/ORE/STONE/HIDE/FIBER/
#    FISHERMAN) + MERCENARY; tubarão T8 +5 (cobre 10); Spyglass do fundador +5
#    (cobre 3; INOBTENÍVEL). Fabricação (WARRIOR/MAGE/HUNTER/TOOLMAKER) NÃO tem
#    troféu de tipo → nunca fecha os 100 de troféu.
# 4. RENDIMENTO por tier J do DIÁRIO:
#    yield% = clamp(100 + 0,5 × (total − 100×J), 100, 150).
#    Verificado no print: total 425 → T2 150, T3 150, T4 112,5. O PISO de 100%
#    (total <= 100×J) é calibrado-PARCIAL (previsão; aguarda confirmação visual).
# 5. Restrição de prédio: mobília e troféu só cabem com tier <= tier do prédio
#    (prédio T7 tranca troféu geral T8 e o tubarão).
TROPHY_CAP = 100                      # cap FIXO da linha "Troféus x/100"
_TROPHY_GENERAL = 5                   # +5 por tier de troféu GERAL coberto
_TROPHY_TYPED = 10                    # +10 por tier de troféu de TIPO coberto
_TROPHY_SHARK = 5                     # tubarão T8 (+5; 1 cobre 10)
_TROPHY_SPYGLASS = 5                  # Spyglass do fundador (+5; inobtenível)
_TROPHY_TIER_MIN, _TROPHY_TIER_MAX = 2, 8
_TABLE_COVERS = 6                     # 1 mesa cobre 6 trabalhadores
_GENERAL_COVERS = 3                   # 1 cópia de troféu geral cobre 3
_SHARK_COVERS = 10                    # 1 tubarão cobre 10
YIELD_FLOOR_PCT = 100.0               # piso do rendimento (calibrado-parcial)
YIELD_CEIL_PCT = 150.0                # teto do rendimento (verificado no print)
# Famílias de laborer que TÊM troféu de tipo (+10); as demais (fabricação de
# gear: WARRIOR/MAGE/HUNTER/TOOLMAKER) só têm o geral.
_FAMILY_HAS_TYPED_TROPHY = frozenset(
    {"WOOD", "ORE", "STONE", "HIDE", "FIBER", "FISHERMAN", "MERCENARY"})


def happiness_total_max(laborer_tier):
    """Teto do painel ("Total x/MAX") p/ trabalhador de tier L: 100×L − 100."""
    return 100 * int(laborer_tier) - 100


def furniture_cap(laborer_tier):
    """Cap de camas (= cap de mesas) no painel: 50×L − 100."""
    return 50 * int(laborer_tier) - 100


def _clean_trophy_tiers(tiers, building_tier, kind):
    """Normaliza um iterável de tiers de troféu COM COBERTURA: dedup, ordena e
    valida 2..8 e tier <= prédio (prédio tranca troféu acima do próprio tier)."""
    out = set()
    for t in tiers or ():
        t = int(t)
        if not (_TROPHY_TIER_MIN <= t <= _TROPHY_TIER_MAX):
            raise ValueError(f"troféu {kind} T{t}: tier deve ser "
                             f"{_TROPHY_TIER_MIN}..{_TROPHY_TIER_MAX}")
        if t > int(building_tier):
            raise ValueError(f"troféu {kind} T{t} não cabe em prédio "
                             f"T{int(building_tier)}")
        out.add(t)
    return tuple(sorted(out))


def happiness_panel(laborer_tier, *, building_tier, n_laborers, beds, bed_tier,
                    tables, table_tier, general_tiers=(), typed_tiers=(),
                    family=None, shark=False, spyglass=False):
    """Espelha o PAINEL de felicidade do jogo p/ trabalhadores de tier L.

    general_tiers/typed_tiers são ITERÁVEIS de tiers de troféu presentes COM
    COBERTURA (ex.: (2,3,4,5,6,7)); typed é IGNORADO p/ família de fabricação
    (não existe troféu de tipo). Valida mobília/troféu <= tier do prédio.
    Devolve {camas: {score, cap, estimado}, mesas: {...}, trofeus: {score,
    cap: 100, ...}, total, total_max, detalhe}. A parte PROPORCIONAL de camas/
    mesas (tier < L ou cobertura < 100%) é estimativa — só o caso maxado foi
    calibrado no jogo."""
    L, B, n = int(laborer_tier), int(building_tier), int(n_laborers)
    if not (2 <= L <= 8):
        raise ValueError("laborer_tier deve ser 2..8")
    if not (1 <= B <= 8):
        raise ValueError("building_tier deve ser 1..8")
    if n < 1:
        raise ValueError("n_laborers deve ser >= 1")
    beds, tables = int(beds or 0), int(tables or 0)
    if beds < 0 or tables < 0:
        raise ValueError("beds/tables devem ser >= 0")
    cap = furniture_cap(L)

    def _furniture(count, tier, what, covers=1):
        """Score de camas/mesas: cap × min(1, tier/L) × min(1, cobertura)."""
        if count <= 0:
            return 0.0, count > 0
        if tier is None:
            raise ValueError(f"informe o tier da {what}")
        tier = int(tier)
        if not (1 <= tier <= 8):
            raise ValueError(f"tier da {what} deve ser 1..8")
        if tier > B:
            raise ValueError(f"{what} T{tier} não cabe em prédio T{B} "
                             "(mobília <= tier do prédio)")
        quality = min(1.0, tier / L)
        coverage = min(1.0, count * covers / n)
        return cap * quality * coverage, (quality < 1.0 or coverage < 1.0)

    bed_score, bed_est = _furniture(beds, bed_tier, "cama")
    tbl_score, tbl_est = _furniture(tables, table_tier, "mesa",
                                    covers=_TABLE_COVERS)
    bed_est = bed_est or beds < n           # sem cama tb. é "abaixo do maxado"
    tbl_est = tbl_est or tables * _TABLE_COVERS < n

    fam = (family or "").upper() or ""
    has_typed = fam in _FAMILY_HAS_TYPED_TROPHY
    gset = _clean_trophy_tiers(general_tiers, B, "geral")
    tset = _clean_trophy_tiers(typed_tiers, B, "de tipo") if has_typed else ()
    if shark and B < 8:
        raise ValueError(f"tubarão é troféu T8 — não cabe em prédio T{B}")
    g = _TROPHY_GENERAL * len(gset)
    ty = _TROPHY_TYPED * len(tset)
    sh = _TROPHY_SHARK if shark else 0
    sp = _TROPHY_SPYGLASS if spyglass else 0
    raw = g + ty + sh + sp
    tscore = min(TROPHY_CAP, raw)

    total = round(bed_score + tbl_score + tscore, 1)
    return {
        "laborer_tier": L, "building_tier": B, "n_laborers": n, "family": fam,
        "camas": {"score": round(bed_score, 1), "cap": cap, "estimado": bed_est},
        "mesas": {"score": round(tbl_score, 1), "cap": cap, "estimado": tbl_est},
        "trofeus": {"score": tscore, "cap": TROPHY_CAP, "geral": g, "tipo": ty,
                    "tubarao": sh, "spyglass": sp, "bruto": raw,
                    "has_typed": has_typed, "geral_tiers": list(gset),
                    "tipo_tiers": list(tset)},
        "total": total,
        "total_max": happiness_total_max(L),
        "estimado": bed_est or tbl_est,     # parte proporcional não calibrada
        "detalhe": {
            "camas_cobertura": f"{beds} cama(s) p/ {n} trabalhador(es) "
                               "(1 cama cobre 1)",
            "mesas_cobertura": f"{tables} mesa(s) cobrem "
                               f"{tables * _TABLE_COVERS} (1 mesa cobre "
                               f"{_TABLE_COVERS})",
            "trofeu_geral_copias_para_cobrir": -(-n // _GENERAL_COVERS),
            "tubarao_copias_para_cobrir": -(-n // _SHARK_COVERS),
            "calibracao": "caso maxado calibrado com o jogo (2026-07-05); a "
                          "parte proporcional de camas/mesas e o piso de 100% "
                          "do rendimento são estimativa",
        },
    }


def journal_yield(total, journal_tier):
    """% de rendimento do diário de tier J dado o TOTAL do painel:
    clamp(100 + 0,5 × (total − 100×J), 100, 150). Verificado (total 425):
    T2=150, T3=150, T4=112,5. O piso de 100% é calibrado-parcial."""
    y = 100.0 + 0.5 * (float(total) - 100.0 * int(journal_tier))
    return max(YIELD_FLOOR_PCT, min(YIELD_CEIL_PCT, y))


def trophy_ceiling(family=None, building_tier=8, *, spyglass=False):
    """Teto de TROFÉU alcançável p/ a família no prédio dado (tiers <= prédio;
    tubarão só em T8+; Spyglass do fundador só entra se spyglass=True — é
    inobtenível). Fabricação não tem troféu de tipo → nunca fecha os 100."""
    B = int(building_tier)
    fam = (family or "").upper() or ""
    has_typed = fam in _FAMILY_HAS_TYPED_TROPHY
    n_tiers = max(0, min(_TROPHY_TIER_MAX, B) - _TROPHY_TIER_MIN + 1)
    general = _TROPHY_GENERAL * n_tiers
    typed = _TROPHY_TYPED * n_tiers if has_typed else 0
    shark = _TROPHY_SHARK if B >= 8 else 0
    sp = _TROPHY_SPYGLASS if spyglass else 0
    return {"general_max": general, "typed_max": typed, "shark_max": shark,
            "spyglass": sp, "has_typed": has_typed,
            "total_max": min(TROPHY_CAP, general + typed + shark + sp)}


def happiness_advice(laborer_tier, *, building_tier, n_laborers, beds, bed_tier,
                     tables, table_tier, general_tiers=(), typed_tiers=(),
                     family=None, shark=False, spyglass=False):
    """Painel + rendimento por tier de diário (2..L) + diagnóstico em PT-BR.

    Devolve o painel (happiness_panel) e mais:
    - yield_por_diario: [{diario_tier, yield_pct, yield_alcancavel_pct}] p/ os
      diários T2..L (alcançável = com o painel no máximo ALCANÇÁVEL, não no teto
      teórico — fabricação nunca fecha os 100 de troféu; prédio < T8 tranca o
      geral T8 e o tubarão; Spyglass inobtenível fica FORA a menos que já o tenha).
    - hints: o que falta e é ALCANÇÁVEL, citando os pontos exatos.
    - trancados: o que NÃO é alcançável (e por quê).
    - alcancavel: {camas, mesas, trofeus, total} máximos reais desta config."""
    panel = happiness_panel(
        laborer_tier, building_tier=building_tier, n_laborers=n_laborers,
        beds=beds, bed_tier=bed_tier, tables=tables, table_tier=table_tier,
        general_tiers=general_tiers, typed_tiers=typed_tiers, family=family,
        shark=shark, spyglass=spyglass)
    L, B, n = panel["laborer_tier"], panel["building_tier"], panel["n_laborers"]
    fam = panel["family"]
    cap = panel["camas"]["cap"]
    beds, tables = int(beds or 0), int(tables or 0)
    hints, locked = [], []

    # mobília: alcançável = cap se o prédio permite peça de tier >= L
    furn_reach = round(cap * min(1.0, B / L), 1)
    if B < L:
        locked.append(
            f"prédio T{B} < trabalhador T{L}: cama/mesa não chegam ao cap "
            f"{cap} (máx ~{furn_reach:g} cada — estimativa)")
    if beds < n:
        hints.append(f"faltam {n - beds} cama(s) p/ {n} trabalhador(es): "
                     f"camas {panel['camas']['score']:g}/{cap}")
    if beds and bed_tier is not None and int(bed_tier) < L and B >= L:
        hints.append(f"cama T{int(bed_tier)} abaixo do tier do trabalhador "
                     f"(T{L}): suba p/ T{L}+ p/ fechar o cap {cap}")
    need_tables = -(-n // _TABLE_COVERS)
    if tables < need_tables:
        hints.append(f"mesas cobrem {tables * _TABLE_COVERS} de {n} "
                     f"trabalhador(es) (1 mesa cobre {_TABLE_COVERS}): precisa "
                     f"de {need_tables} mesa(s)")
    if tables and table_tier is not None and int(table_tier) < L and B >= L:
        hints.append(f"mesa T{int(table_tier)} abaixo do tier do trabalhador "
                     f"(T{L}): suba p/ T{L}+ p/ fechar o cap {cap}")

    # troféus: o que falta (alcançável) vs o que o prédio/família tranca
    tinfo = panel["trofeus"]
    got_g, got_t = set(tinfo["geral_tiers"]), set(tinfo["tipo_tiers"])
    copies = -(-n // _GENERAL_COVERS)
    for t in range(_TROPHY_TIER_MIN, min(_TROPHY_TIER_MAX, B) + 1):
        if t not in got_g:
            hints.append(f"troféu geral T{t} sem cobertura: +{_TROPHY_GENERAL} "
                         f"({copies} cópia(s) p/ cobrir {n})")
    for t in range(max(B + 1, _TROPHY_TIER_MIN), _TROPHY_TIER_MAX + 1):
        locked.append(f"troféu geral T{t}: +{_TROPHY_GENERAL} trancado pelo "
                      f"prédio T{B}")
    if tinfo["has_typed"]:
        for t in range(_TROPHY_TIER_MIN, min(_TROPHY_TIER_MAX, B) + 1):
            if t not in got_t:
                hints.append(f"troféu de tipo ({fam}) T{t} sem cobertura: "
                             f"+{_TROPHY_TYPED}")
        for t in range(max(B + 1, _TROPHY_TIER_MIN), _TROPHY_TIER_MAX + 1):
            locked.append(f"troféu de tipo ({fam}) T{t}: +{_TROPHY_TYPED} "
                          f"trancado pelo prédio T{B}")
    if B >= 8 and not shark:
        hints.append(f"tubarão T8 sem cobertura: +{_TROPHY_SHARK} "
                     f"(1 cobre {_SHARK_COVERS})")
    elif B < 8:
        locked.append(f"tubarão T8: +{_TROPHY_SHARK} trancado pelo prédio T{B}")
    if not spyglass:
        locked.append(f"Spyglass do fundador: +{_TROPHY_SPYGLASS} (inobtenível "
                      "— fora do alcançável)")

    ceiling = trophy_ceiling(fam, B, spyglass=spyglass)
    trophy_reach = ceiling["total_max"]
    if fam and not tinfo["has_typed"]:
        locked.append(
            f"família {fam} é de FABRICAÇÃO: não existe troféu de tipo — o "
            f"troféu satura em {trophy_reach}/{TROPHY_CAP} neste prédio "
            f"(T{B}); 'Troféus 100/100' é inalcançável")

    total_reach = round(2 * furn_reach + trophy_reach, 1)
    yields = []
    for j in range(_TROPHY_TIER_MIN, L + 1):
        yields.append({
            "diario_tier": j,
            "yield_pct": journal_yield(panel["total"], j),
            "yield_alcancavel_pct": journal_yield(total_reach, j),
        })
    if panel["total"] >= total_reach:
        hints.append("painel no MÁXIMO alcançável desta configuração — o que "
                     "resta está trancado (veja 'trancados')")

    return {
        **panel,
        "yield_por_diario": yields,
        "hints": hints,
        "trancados": locked,
        "trophy_ceiling": ceiling,
        "alcancavel": {"camas": furn_reach, "mesas": furn_reach,
                       "trofeus": trophy_reach, "total": total_reach},
    }

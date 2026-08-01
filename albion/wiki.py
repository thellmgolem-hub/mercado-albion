# -*- coding: utf-8 -*-
"""Camada "wiki" do item: cadeia de produção recursiva, lista de compras de
recurso bruto, índice reverso (usado-em) e detalhes do dump.

Tudo derivado dos dados que já temos (recipes_craft/refining, items_db,
items_raw, craft_data). Somente-leitura. As decisões de RRR reaproveitam
albion/production (cada nó é craftado na SUA cidade-bônus — ótimo teórico,
ignora transporte) e albion/craft.
"""
import json
import threading
from pathlib import Path

from . import config, craft, production

_DATA = Path(__file__).resolve().parent.parent / "data"

def canonical_id(item_id):
    """recipes_craft referencia refinado encantado como X@n, mas o DB/mercado/
    busca usam X_LEVELn@n. Canoniza p/ o id REAL (navegável e precificável).
    Equipamento acabado (X@n não-refinado) é id válido — fica intacto."""
    base, _, e = item_id.partition("@")
    if (e and e != "0" and "_LEVEL" not in base
            and any(base.endswith(t) for t in craft._REFINED_FAMILY)):
        return f"{base}_LEVEL{e}@{e}"
    return item_id


# ----------------------------------------------------------------- usado-em
_USAGE = None


def _usage_index():
    """{insumo_id canônico: set(produtos que o consomem)} craft + refino. 1x."""
    global _USAGE
    if _USAGE is None:
        idx = {}
        for fname in ("recipes_refining.json", "recipes_craft.json"):
            for out_id, recipe in (craft._load(fname) or {}).items():
                for inp in recipe.get("inputs", []):
                    idx.setdefault(canonical_id(inp["id"]), set()).add(out_id)
        _USAGE = {k: sorted(v) for k, v in idx.items()}
    return _USAGE


def used_in(item_id, name_of=None, limit=60, tier_of=None):
    """Produtos que CONSOMEM este item (índice reverso das receitas).
    Devolve (linhas, total) — total real antes do corte, p/ a UI sinalizar."""
    name_of = name_of or (lambda i: i)
    tier_of = tier_of or (lambda i: 0)
    outs = _usage_index().get(canonical_id(item_id), [])
    rows = []
    for oid in outs:
        recipe = craft.recipe_for(oid) or {}
        cnt = next((i["count"] for i in recipe.get("inputs", [])
                    if canonical_id(i["id"]) == canonical_id(item_id)), None)
        rows.append({"item_id": oid, "name_pt": name_of(oid),
                     "count": cnt, "category": recipe.get("category")})
    # ordena por relevância (categoria, tier, id) p/ o corte não apagar
    # categorias inteiras do mesmo tier silenciosamente
    rows.sort(key=lambda r: (r["category"] or "", tier_of(r["item_id"]) or 0,
                             r["item_id"]))
    total = len(rows)
    return (rows[:limit] if limit else rows), total


# ------------------------------------------------------- cadeia de produção
def _chain_recipe(item_id):
    """Receita p/ a cadeia (item_id já canônico): _prod_recipe direto, com
    fallback ao alias por segurança."""
    recipe = production._prod_recipe(item_id)
    if recipe is None and "@" in item_id:
        base, e = item_id.split("@", 1)
        recipe = production._prod_recipe(f"{base}_LEVEL{e}@{e}")
    return recipe


def chain_item_ids(item_id, max_depth=10):
    """Todos os ids (canônicos) que aparecem na cadeia (p/ buscar preços)."""
    ids = set()

    def walk(iid, depth, seen):
        iid = canonical_id(iid)
        ids.add(iid)
        recipe = _chain_recipe(iid)
        if not recipe or depth >= max_depth or iid in seen:
            return
        for inp in recipe["inputs"]:
            walk(inp["id"], depth + 1, seen | {iid})

    walk(item_id, 0, frozenset())
    return ids


def _node(item_id, price_of, name_of, tier_of, focus, depth, max_depth, seen):
    """Nó recursivo da árvore de produção (custos POR UNIDADE do item)."""
    item_id = canonical_id(item_id)            # id real (navegável/precificável)
    recipe = _chain_recipe(item_id)
    buy = price_of(item_id)
    base = {
        "id": item_id, "name_pt": name_of(item_id),
        "tier": tier_of(item_id), "buy_unit": buy,
    }
    if not recipe or depth >= max_depth or item_id in seen:
        # folha: bruto, sem receita, ou corte de profundidade -> só comprar
        base.update({"is_raw": production.is_raw_resource(item_id),
                     "make_unit": None, "best_unit": buy,
                     "decision": "comprar", "children": []})
        return base
    city = production.bonus_city_for(item_id, recipe) or config.ROYAL_CITIES[0]
    output = recipe.get("output", 1) or 1
    rrr = production.rrr_for(item_id, recipe, city, focus)
    children, mat, ok = [], 0.0, True
    for inp in recipe["inputs"]:
        ch = _node(inp["id"], price_of, name_of, tier_of, focus, depth + 1,
                   max_depth, seen | {item_id})
        ch["count"] = inp["count"]               # unidades p/ 1 craft do pai
        children.append(ch)
        if ch["best_unit"] is None:
            ok = False
        else:
            mat += ch["best_unit"] * inp["count"]
    make_unit = (mat * (1 - rrr) / output) if ok else None
    opts = [c for c in (buy, make_unit) if c is not None]
    best = min(opts) if opts else None
    decision = "fazer" if (make_unit is not None and
                           (buy is None or make_unit <= buy)) else "comprar"
    base.update({
        "is_raw": False, "make_unit": round(make_unit) if make_unit is not None else None,
        "best_unit": round(best) if best is not None else None,
        "decision": decision, "craft_city": city,
        "rrr_pct": round(rrr * 100, 1), "focus": recipe.get("focus") or 0,
        "output": output, "children": children,
    })
    return base


def production_tree(item_id, price_of, name_of, tier_of, *, focus=False,
                    max_depth=10, qty=None):
    """Árvore completa de produção + lista de compras de bruto + totais.

    price_of(id)->menor compra (q1, cidade mais barata) | None.
    Expande TODA a cadeia até o bruto (mostra a estrutura), anotando fazer-vs-
    comprar e o RRR por nó (cada um na sua cidade-bônus). A lista de compras
    assume CRAFTAR tudo a partir do bruto, para `qty` unidades do item final
    (default = 1 craft = output unidades)."""
    tree = _node(item_id, price_of, name_of, tier_of, focus, 0, max_depth,
                 frozenset())

    item_id = canonical_id(item_id)
    root_recipe = _chain_recipe(item_id)
    output = (root_recipe or {}).get("output", 1) or 1
    target = qty if qty else output
    shopping = {}

    def expand(iid, units, depth, seen):
        iid = canonical_id(iid)
        recipe = _chain_recipe(iid)
        if not recipe or depth >= max_depth or iid in seen:  # mesma folha de _node
            shopping[iid] = shopping.get(iid, 0.0) + units
            return
        crafts = units / (recipe.get("output", 1) or 1)
        for inp in recipe["inputs"]:
            expand(inp["id"], inp["count"] * crafts, depth + 1, seen | {iid})

    expand(item_id, target, 0, frozenset())
    shop = []
    raw_cost = 0.0
    unpriced = []
    for iid, units in sorted(shopping.items(), key=lambda kv: -kv[1]):
        p = price_of(iid)
        line = {"id": iid, "name_pt": name_of(iid), "tier": tier_of(iid),
                "units": round(units, 1), "unit_price": p,
                "subtotal": round(p * units) if p else None}
        if p:
            raw_cost += p * units
        else:
            unpriced.append(iid)
        shop.append(line)
    # raw_cost por unidade do item final, base comparável a make/buy_unit
    raw_cost_unit = round(raw_cost / target) if (raw_cost and target) else None
    return {
        "tree": tree, "shopping": shop, "target_qty": round(target, 1),
        "output": output,
        "raw_cost": round(raw_cost) if raw_cost else None,
        "raw_cost_unit": raw_cost_unit,
        "unpriced": unpriced,            # brutos sem cotação (lista incompleta)
        "make_unit": tree.get("make_unit"), "buy_unit": tree.get("buy_unit"),
        "best_unit": tree.get("best_unit"),
    }


# ----------------------------------------------------------- detalhes (dump)
_DETAILS = None
_DETAILS_LOCK = threading.Lock()   # MEM-2: serializa o parse de 17 MB (ver abaixo)
# campos do dump dignos de uma ficha de wiki (rótulo PT, chave @no dump)
_DUMP_FIELDS = [
    ("item_power", "@itempower"), ("ability_power", "@abilitypower"),
    ("attack_damage", "@attackdamage"), ("attack_speed", "@attackspeed"),
    ("attack_range", "@attackrange"), ("attack_type", "@attacktype"),
    ("two_handed", "@twohanded"), ("slot", "@slottype"),
    ("durability", "@durability"), ("armor", "@armor"),
    ("magic_resistance", "@magicresistance"),
    ("active_slots", "@activespellslots"), ("passive_slots", "@passivespellslots"),
    ("max_quality", "@maxqualitylevel"), ("crafting_category", "@craftingcategory"),
]


def _build_details_index():
    """Parseia items_raw.json UMA vez -> {base_id: {campos de ficha}}. ~17MB,
    custo pago no 1º acesso à wiki; depois fica em cache no processo."""
    idx = {}
    try:
        raw = json.loads((_DATA / "items_raw.json").read_text(encoding="utf-8"))
    except Exception:
        return idx

    def visit(o):
        if isinstance(o, dict):
            uid = o.get("@uniquename")
            if uid and ("@tier" in o or "craftingrequirements" in o):
                d = {}
                for label, key in _DUMP_FIELDS:
                    if key in o and o[key] not in (None, ""):
                        d[label] = o[key]
                if d:
                    idx.setdefault(uid, d)
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(raw)
    return idx


def item_details(item_id, base_meta=None):
    """Ficha de detalhes: básicos (items_db via base_meta) + stats do dump.
    base_meta: dict do items_db (tier, ench, cat, sub, w, maxq) — opcional."""
    global _DETAILS
    if _DETAILS is None:
        # MEM-2: o parse de items_raw.json (~17 MB no disco) custa ~47 MB de PICO
        # transitório. Sem lock, N threads do pool que chegassem juntas num
        # processo FRIO parseariam TODAS ao mesmo tempo (10 x 47 MB) e estourariam
        # os 512 MB do free — OOM = crash duro = queda de web E bot juntos. Com o
        # lock, uma constrói e as outras esperam e reusam (dupla checagem).
        with _DETAILS_LOCK:
            if _DETAILS is None:
                _DETAILS = _build_details_index()
    base = item_id.split("@")[0]
    dump = _DETAILS.get(item_id) or _DETAILS.get(base) or {}
    out = {"id": item_id}
    if base_meta:
        out.update({
            "name_pt": base_meta.get("pt"), "name_en": base_meta.get("en"),
            "tier": base_meta.get("tier"), "ench": base_meta.get("ench"),
            "category": base_meta.get("cat"), "subcategory": base_meta.get("sub"),
            "weight": base_meta.get("w"), "max_quality": base_meta.get("maxq"),
        })
    # normaliza alguns numéricos do dump
    def num(v):
        try:
            f = float(v)
            return int(f) if f == int(f) else round(f, 2)
        except (TypeError, ValueError):
            return v
    out["stats"] = {k: num(v) for k, v in dump.items()}
    return out


# -*- coding: utf-8 -*-
"""Extrai receitas de CRAFT (equipamento, comida, poções...) do dump.

Gera data/recipes_craft.json: por item craftável presente no items_db,
guarda os insumos (id de mercado + quantidade), o foco, o lote (output) e a
categoria de craft (@craftingcategory) — usada para achar a cidade-bônus.

IMPORTANTE: cada nível de ENCANTO tem sua PRÓPRIA receita no dump
(enchantments.enchantment[].craftingrequirements): adiciona um reagente de
encanto (ALCHEMY_EXTRACT/FISHSAUCE/runas...) e tem foco/lote próprios. Ler só o
craftingrequirements de topo subestimava custo e foco dos encantados.

Complementa recipes_refining.json (recursos refinados). Rodar após
build_items_db.py.  Uso:  python scripts/build_craft_recipes.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SKIP_KEYS = {"@xmlns:xsi", "@xsi:noNamespaceSchemaLocation", "shopcategories"}
# famílias de recurso refinado: no dump aparecem como X_LEVELn (encantado) e o
# id de mercado é X@n; reagentes de encanto (ALCHEMY_EXTRACT_LEVEL1 etc.) NÃO —
# o próprio _LEVELn é o id de mercado.
_REFINED = ("METALBAR", "PLANKS", "CLOTH", "LEATHER", "STONEBLOCK")
_LEVEL_RE = re.compile(r"^(.+)_LEVEL([1-9])$")


def market_id(uniquename: str, ench) -> str:
    e = int(ench or 0)
    return f"{uniquename}@{e}" if e > 0 else uniquename


def input_market_id(uniquename: str) -> str:
    """Id de mercado de um craftresource. Refinado X_LEVELn -> X@n; reagente
    (ALCHEMY_EXTRACT_LEVEL1...) mantém o nome (é assim que vai pro mercado)."""
    m = _LEVEL_RE.match(uniquename)
    if m and any(m.group(1).endswith(r) for r in _REFINED):
        return f"{m.group(1)}@{m.group(2)}"
    return uniquename


def iter_items(raw):
    for key, val in raw["items"].items():
        if key in SKIP_KEYS:
            continue
        entries = val if isinstance(val, list) else [val]
        for e in entries:
            if isinstance(e, dict) and "@uniquename" in e:
                yield e


def parse_reqs(block):
    """(inputs, focus, output) de um bloco craftingrequirements (base ou encanto).
    Lê o craftresource COMPLETO — inclui o reagente de encanto quando presente."""
    if isinstance(block, list):
        block = block[0] if block else None
    if not block:
        return None
    res = block.get("craftresource")
    if isinstance(res, dict):
        res = [res]
    if not res:
        return None
    inputs = [{"id": input_market_id(x["@uniquename"]), "count": int(x["@count"])}
              for x in res if "@uniquename" in x]
    if not inputs:
        return None
    output = int(float(block.get("@amountcrafted", 1) or 1))
    return {"inputs": inputs,
            "focus": int(float(block.get("@craftingfocus", 0))),
            "output": max(output, 1)}


def enchant_recipes(entry):
    """{nível_encanto: receita} a partir do bloco enchantments do item."""
    block = entry.get("enchantments")
    if not isinstance(block, dict):
        return {}
    el = block.get("enchantment")
    el = el if isinstance(el, list) else ([el] if el else [])
    out = {}
    for x in el:
        lvl = int(x.get("@enchantmentlevel", 0) or 0)
        rec = parse_reqs(x.get("craftingrequirements"))
        if rec and lvl:
            out[lvl] = rec
    return out


def main():
    items_db = json.loads((DATA / "items_db.json").read_text(encoding="utf-8"))
    db_ids = {i["id"] for i in items_db}
    # recursos refinados já estão em recipes_refining.json
    refined = {i["id"] for i in items_db
               if i["cat"] == "crafting" and i["sub"] == "refinedresources"}
    refined_bases = {r.split("@")[0] for r in refined}
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))

    recipes = {}
    for e in iter_items(raw):
        base = e["@uniquename"]
        cat = e.get("@craftingcategory")
        base_recipe = parse_reqs(e.get("craftingrequirements"))
        if base_recipe is None:
            continue
        ench_map = enchant_recipes(e)
        for ench in range(0, 5):
            mid = market_id(base, ench)
            if mid not in db_ids or mid in refined:
                continue
            if ench == 0:
                rec = dict(base_recipe)
            elif ench in ench_map:
                # receita REAL do encanto (com reagente + foco/lote certos)
                rec = dict(ench_map[ench])
            else:
                # fallback heurístico: sobe os refinados-base para @ench
                rec = {"inputs": [{"id": market_id(i["id"].split("@")[0], ench)
                                   if i["id"].split("@")[0] in refined_bases
                                   else i["id"],
                                   "count": i["count"]}
                                  for i in base_recipe["inputs"]],
                       "focus": base_recipe["focus"],
                       "output": base_recipe["output"]}
            rec["category"] = cat
            recipes[mid] = rec

    dest = DATA / "recipes_craft.json"
    dest.write_text(json.dumps(recipes, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"{len(recipes)} receitas de craft -> {dest} "
          f"({dest.stat().st_size / 1e6:.1f} MB)")
    cats = {}
    for r in recipes.values():
        cats[r.get("category")] = cats.get(r.get("category"), 0) + 1
    print("  por categoria de craft:",
          dict(sorted(cats.items(), key=lambda x: -x[1])[:8]))


if __name__ == "__main__":
    main()

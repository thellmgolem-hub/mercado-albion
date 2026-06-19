# -*- coding: utf-8 -*-
"""Extrai receitas de CRAFT (equipamento, comida, poções...) do dump.

Gera data/recipes_craft.json: por item craftável presente no items_db,
guarda os insumos (id de mercado + quantidade), o foco e a categoria de
craft (@craftingcategory) — usada para achar a cidade-bônus em craft_data.

Complementa recipes_refining.json (recursos refinados). Rodar após
build_items_db.py.  Uso:  python scripts/build_craft_recipes.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SKIP_KEYS = {"@xmlns:xsi", "@xsi:noNamespaceSchemaLocation", "shopcategories"}


def market_id(uniquename: str, ench) -> str:
    e = int(ench or 0)
    return f"{uniquename}@{e}" if e > 0 else uniquename


def iter_items(raw):
    for key, val in raw["items"].items():
        if key in SKIP_KEYS:
            continue
        entries = val if isinstance(val, list) else [val]
        for e in entries:
            if isinstance(e, dict) and "@uniquename" in e:
                yield e


def first_recipe(entry):
    reqs = entry.get("craftingrequirements")
    if isinstance(reqs, dict):
        reqs = [reqs]
    if not reqs:
        return None
    r0 = reqs[0]
    res = r0.get("craftresource")
    if isinstance(res, dict):
        res = [res]
    if not res:
        return None
    inputs = [{"id": market_id(x["@uniquename"], x.get("@enchantmentlevel", "0")),
               "count": int(x["@count"])}
              for x in res if "@uniquename" in x]
    if not inputs:
        return None
    # @amountcrafted: quantos itens UM craft produz (poções/comida saem em lote
    # de 5; equipamento sai 1). Sem isso, a margem de consumíveis ficava por-1.
    output = int(float(r0.get("@amountcrafted", 1) or 1))
    return {"inputs": inputs, "focus": int(float(r0.get("@craftingfocus", 0))),
            "output": max(output, 1)}


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
        for ench in range(0, 5):
            mid = market_id(base, ench)
            if mid not in db_ids or mid in refined:
                continue
            rec = first_recipe(e)
            if rec is None:
                break  # item sem receita; não tenta encantos
            # insumos herdam o encanto do produto (refinados @N)
            if ench > 0:
                rec = {"inputs": [{"id": market_id(i["id"].split("@")[0], ench)
                                   if i["id"].split("@")[0] in refined_bases
                                   else i["id"],
                                   "count": i["count"]} for i in rec["inputs"]],
                       "focus": rec["focus"], "output": rec.get("output", 1)}
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

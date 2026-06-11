# -*- coding: utf-8 -*-
"""Extrai as receitas de refino do dump bruto -> data/recipes_refining.json.

Fonte: craftingrequirements dos recursos refinados em data/items_raw.json
(números exatos do jogo, nada hardcoded). Rodar após build_items_db.py.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def market_id(uniquename: str, ench: str) -> str:
    e = int(ench or 0)
    return f"{uniquename}@{e}" if e > 0 else uniquename


def main():
    items_db = json.loads((DATA / "items_db.json").read_text(encoding="utf-8"))
    refined_ids = {i["id"] for i in items_db
                   if i["cat"] == "crafting" and i["sub"] == "refinedresources"}
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))
    # no dump as variantes encantadas são entradas próprias SEM o @N do
    # mercado: T6_LEATHER_LEVEL2 (dump) <-> T6_LEATHER_LEVEL2@2 (mercado)
    by_name = {e.get("@uniquename"): e for e in raw["items"]["simpleitem"]
               if isinstance(e, dict)}
    recipes = {}
    for uid in sorted(refined_ids):
        entry = by_name.get(uid.split("@")[0])
        if not entry:
            continue
        reqs = entry.get("craftingrequirements")
        if isinstance(reqs, dict):
            reqs = [reqs]
        if not reqs:
            continue
        res = reqs[0].get("craftresource")
        if isinstance(res, dict):
            res = [res]
        if not res:
            continue
        recipes[uid] = {
            "inputs": [{"id": market_id(r["@uniquename"],
                                        r.get("@enchantmentlevel", "0")),
                        "count": int(r["@count"])} for r in res],
            "focus": int(float(reqs[0].get("@craftingfocus", 0))),
        }
    dest = DATA / "recipes_refining.json"
    dest.write_text(json.dumps(recipes, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"{len(recipes)} receitas de refino -> {dest}")


if __name__ == "__main__":
    main()

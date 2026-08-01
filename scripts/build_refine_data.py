# -*- coding: utf-8 -*-
"""Gera data/refine_data.json — o dado ESTÁTICO da ferramenta de refino.

Tira do dump oficial (data/items_raw.json, 17 MB) só o que o motor de refino
precisa por item de refino: tier, encantamento, PESO (logística) e ITEMVALUE.

O itemvalue é o que define a TAXA DE USO da estação: a nutrição consumida por
operação é itemvalue x 0,1125, e a estação cobra em prata por 100 de nutrição
(taxa que o dono da estação escolhe). Sem esse número a conta de refino ignora
um custo real — pequeno no T4, relevante no T7/T8 e nos encantados.

Rodar depois de um patch:  python scripts/build_refine_data.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# família de refino -> (token do bruto, token do refinado)
FAMILIES = {
    "ore":   ("ORE", "METALBAR"),
    "wood":  ("WOOD", "PLANKS"),
    "fiber": ("FIBER", "CLOTH"),
    "hide":  ("HIDE", "LEATHER"),
    "rock":  ("ROCK", "STONEBLOCK"),
}
NUTRITION_PER_ITEMVALUE = 0.1125   # nutrição consumida por ponto de itemvalue


def _canon(uniquename, ench):
    """T4_ORE_LEVEL1 -> T4_ORE_LEVEL1@1 (a forma que o app/AODP usam)."""
    if ench and "@" not in uniquename:
        return f"{uniquename}@{ench}"
    return uniquename


def main():
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))
    simple = raw["items"]["simpleitem"]

    token_fam = {}
    for fam, (raw_tok, ref_tok) in FAMILIES.items():
        token_fam[raw_tok] = (fam, "raw")
        token_fam[ref_tok] = (fam, "refined")

    items = {}
    for it in simple:
        uniq = it.get("@uniquename") or ""
        parts = uniq.split("@")[0].split("_")
        if len(parts) < 2:
            continue
        got = token_fam.get(parts[1])
        if not got:
            continue
        fam, kind = got
        ench = int(it.get("@enchantmentlevel") or 0)
        item_id = _canon(uniq, ench)
        itemvalue = float(it.get("@itemvalue") or 0)
        items[item_id] = {
            "fam": fam,
            "kind": kind,
            "tier": int(it.get("@tier") or 0),
            "ench": ench,
            "weight": float(it.get("@weight") or 0),
            "itemvalue": itemvalue,
            # nutrição por operação (refinar 1 unidade); só o produto consome
            "nutrition": round(itemvalue * NUTRITION_PER_ITEMVALUE, 4),
        }

    out = {
        "nutrition_per_itemvalue": NUTRITION_PER_ITEMVALUE,
        "families": {fam: {"raw_token": r, "refined_token": rf}
                     for fam, (r, rf) in FAMILIES.items()},
        "items": dict(sorted(items.items())),
        "_fonte": "items_raw.json (ao-bin-dumps): @itemvalue, @weight, @tier",
    }
    dest = DATA / "refine_data.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    refined = sum(1 for v in items.values() if v["kind"] == "refined")
    print(f"{dest.name}: {len(items)} itens ({refined} refinados, "
          f"{len(items) - refined} brutos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Extrai as cadeias de fazenda (cria/semente -> produto) do dump.

Lê data/items_raw.json e produz data/farm_data.json keyed por item-bebê:
  {grown, growtime_s, offspring_chance, offspring_amount, feed_cost}

feed_cost fica 0 por ora: mapear o @foodcategory consumido ao item de ração
mais barato exige um cruzamento extra ainda não derivado — albion/production.py
trata o lucro como SEM ração e isso fica documentado (margem otimista p/ animais).
Uso: python scripts/build_farm_data.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def walk(o, out):
    if isinstance(o, dict):
        if "grownitem" in o and o.get("@uniquename"):
            g = o["grownitem"]
            if isinstance(g, dict) and g.get("@uniquename"):
                off = g.get("offspring") or {}
                out[o["@uniquename"]] = {
                    "grown": g["@uniquename"],
                    "growtime_s": float(g.get("@growtime", 0) or 0),
                    "offspring_chance": float(off.get("@chance", 0) or 0),
                    "offspring_amount": float(off.get("@amount", 0) or 0),
                    "feed_cost": 0,
                }
        for v in o.values():
            walk(v, out)
    elif isinstance(o, list):
        for x in o:
            walk(x, out)


def main():
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))
    out = {}
    walk(raw, out)
    (DATA / "farm_data.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"farm_data.json: {len(out)} cadeias de fazenda")


if __name__ == "__main__":
    main()

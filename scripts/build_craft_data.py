# -*- coding: utf-8 -*-
"""Extrai dados REAIS de refino/craft do dump -> data/craft_data.json.

Fontes (ao-bin-dumps):
  - craftingmodifiers.json: bônus de estação (refiningbonus/craftingbonus 0.18)
    e modificadores por categoria por cluster (especialidade da cidade).
  - formatted/world.json: clusterid -> nome da cidade (para não chutar).

Mecânica verificada: a Taxa de Retorno de Recursos (RRR) sai dos bônus pela
fórmula  RRR = 1 - 1/(1 + soma_de_bônus):
  - estação sem especialidade (0.18):              1 - 1/1.18 = 15,2%  (base)
  - cidade com bônus da família (0.18 + 0.40):      1 - 1/1.58 = 36,7%
Conferem exatamente com os valores conhecidos da comunidade. O foco acrescenta
~0.59 à soma de bônus (calibrado pelos alvos públicos 43,5%/53,9% com foco).

Uso:  python scripts/build_craft_data.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ROYAL = {"Thetford", "Lymhurst", "Bridgewatch", "Martlock",
         "Fort Sterling", "Caerleon"}
RESOURCE_CATS = {"fiber", "ore", "rock", "wood", "hide"}
FOCUS_BONUS_SUM = 0.59  # contribuição do foco à soma de bônus (ver docstring)


def rrr(bonus_sum: float) -> float:
    return round((1 - 1 / (1 + bonus_sum)) * 100, 1)


def main():
    world = json.loads((DATA / "world.json").read_text(encoding="utf-8"))
    cid2city = {}
    for c in world:
        cid = str(c.get("Index") or "")
        name = c.get("UniqueName") or c.get("Name")
        if name in ROYAL:
            cid2city.setdefault(cid, name)

    cm = json.loads((DATA / "craftingmodifiers.json"
                     ).read_text(encoding="utf-8"))["craftingmodifiers"]
    locs = cm["craftinglocation"]
    if isinstance(locs, dict):
        locs = [locs]

    station_ref = None
    # refining[resource] = {city, rrr_base, rrr_bonus, rrr_base_focus, rrr_bonus_focus}
    refining = {}
    # crafting[category] = {city: bonus}
    crafting = {}

    for loc in locs:
        cid = str(loc.get("@clusterid") or "")
        city = cid2city.get(cid)
        if not city:
            continue  # só cidades reais (ignora Rest/ilha/hideout/mundo)
        rb = loc.get("refiningbonus")
        if rb and station_ref is None:
            station_ref = float(rb["@value"])
        station = float(rb["@value"]) if rb else 0.0
        mods = loc.get("craftingmodifier") or []
        if isinstance(mods, dict):
            mods = [mods]
        for m in mods:
            cat, val = m["@name"], float(m["@value"])
            if cat in RESOURCE_CATS:
                # especialidade de refino daquela cidade
                refining[cat] = {
                    "city": city,
                    "rrr_base_pct": rrr(station),
                    "rrr_bonus_pct": rrr(station + val),
                    "rrr_base_focus_pct": rrr(station + FOCUS_BONUS_SUM),
                    "rrr_bonus_focus_pct": rrr(station + val + FOCUS_BONUS_SUM),
                    "station_bonus": station,
                    "specialty_bonus": val,
                }
            else:
                crafting.setdefault(cat, {})[city] = val

    out = {
        "station_refining_bonus": station_ref,
        "focus_bonus_sum": FOCUS_BONUS_SUM,
        "refining": refining,
        "crafting": crafting,
        "_fonte": "craftingmodifiers.json + world.json (ao-bin-dumps)",
    }
    dest = DATA / "craft_data.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"OK -> {dest}")
    print(f"  refino: " + ", ".join(
        f"{r}={d['city']}({d['rrr_base_pct']}%/{d['rrr_bonus_pct']}%)"
        for r, d in refining.items()))
    print(f"  categorias de craft com cidade-bônus: {len(crafting)}")
    multi = {c: list(v) for c, v in crafting.items() if len(v) > 1}
    if multi:
        print(f"  ex. categorias craftáveis em +1 cidade: "
              f"{dict(list(multi.items())[:4])}")


if __name__ == "__main__":
    main()

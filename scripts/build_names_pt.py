# -*- coding: utf-8 -*-
"""Grava buildName_pt em cada build de data/builds.json — o NOME da build em
PT-BR, derivado do nome PT da ARMA (items.weapon.pt) sem o sufixo de tier
("do Ancião", "do Perito", ...). O guia interno segue com buildName em EN pra
manutenção; o bot (embed) e a imagem de loadout exibem o buildName_pt.

Guilda 100% BR: nada de nome de build em inglês na cara do usuário.
Roda OFFLINE (1x por rebuild):  python scripts/build_names_pt.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "data" / "builds.json"

# sufixo de tier do Albion em PT (com ou sem "Elevado"; masc/fem)
_TIER = re.compile(
    r"\s+(Elevad[oa]\s+)?d[oa]\s+"
    r"(Novato|Iniciante|Adepto|Perito|Mestre|Grão-mestre|Ancião)\s*$")


def pt_name(build):
    """Nome PT da build = nome PT da arma sem o sufixo de tier; cai pro
    buildName EN só se a arma não tiver nome PT (nunca deve acontecer)."""
    w = (build.get("items") or {}).get("weapon") or {}
    pt = (w.get("pt") or "").strip()
    if pt:
        return _TIER.sub("", pt).strip() or pt
    return build.get("buildName", "")


def main():
    doc = json.load(open(BUILDS, encoding="utf-8"))
    builds = doc.get("builds", [])
    for b in builds:
        b["buildName_pt"] = pt_name(b)
    json.dump(doc, open(BUILDS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"buildName_pt gravado em {len(builds)} builds. Amostra:")
    for b in builds[:8]:
        print(f"   {b.get('buildName')!r:28} -> {b.get('buildName_pt')!r}")


if __name__ == "__main__":
    main()

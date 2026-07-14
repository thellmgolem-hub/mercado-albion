# -*- coding: utf-8 -*-
"""Traduz os NOMES de item/skill em inglês embutidos nos textos de estratégia
(execution/note) das builds para PT-BR, gravando execution_pt/note_pt. O guia
interno mantém execution/note em EN pra manutenção; o bot exibe *_pt.

Fontes do dicionário EN->PT (oficiais, já no repo):
- data/items_db.json (12k itens): nome base EN (sem prefixo de tier "Adept's")
  -> nome base PT (sem sufixo " do Adepto").
- data/builds.json abilities: q/w/e/passive EN -> *_pt (nomes de skill em uso).

Substituição: só termos EXATOS conhecidos, do MAIS LONGO pro mais curto, com
fronteira de palavra. Termo desconhecido fica como está (e é REPORTADO), pra
não inventar tradução. Roda offline: python scripts/build_notes_pt.py [--write]
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "data" / "builds.json"
ITEMS = ROOT / "data" / "items_db.json"

# prefixo de tier EN (Adept's, Elder's, ...) e sufixo de tier PT (do Adepto, ...)
_EN_TIER = re.compile(
    r"^(Novice's|Journeyman's|Adept's|Expert's|Master's|Grandmaster's|Elder's|"
    r"Beginner's|Trainee's)\s+")
_PT_TIER = re.compile(
    r"\s+(Elevad[oa]\s+)?d[oa]\s+"
    r"(Novato|Iniciante|Adepto|Perito|Mestre|Grão-mestre|Ancião)\s*$")


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.strip().lower())


# termos que NÃO vêm dos dois dicionários automáticos (skills não usadas por
# nenhuma build; itens cujo nome-base diverge; adjetivos de linha/conteúdo).
# Curados à mão contra o jogo em PT-BR. Word-boundary na substituição.
SUPPLEMENT = {
    "Fire Wave": "Onda de Fogo",
    "Death Curse": "Maldição da Morte",
    "Internal Bleeding": "Sangramento Interno",
    "Glacial Obelisk": "Obelisco Glacial",
    "Royal Banner": "Estandarte Real",
    "Great Frost": "Grande Gelo",
    "Frost Lance": "Lança de Gelo",
    "Frost Nova": "Nova de Gelo",
    "Ice Storm": "Tempestade de Gelo",
    "Bloody Reap": "Ceifa Sangrenta",
    "Healing": "Cura",
    "Avalonian": "Avaloniana",
    "Druidic": "Druídico",
    "Stalker": "Perseguidor",
    "Assassin": "Assassino",
    "Cursed": "Amaldiçoado",
    "Dodge": "esquiva",
    "Wildfire": "Incendiário",
    "Quiver": "Aljava",
}


def build_term_map():
    """{EN (Title Case original): PT} de itens (nome base) + skills das builds."""
    term = dict(SUPPLEMENT)
    # 1) itens: nome base EN -> nome base PT
    for it in json.load(open(ITEMS, encoding="utf-8")):
        en = _EN_TIER.sub("", (it.get("en") or "").strip()).strip()
        pt = _PT_TIER.sub("", (it.get("pt") or "").strip()).strip()
        if en and pt and en.lower() != pt.lower() and en[0].isupper():
            term.setdefault(en, pt)
    # 2) skills das builds (EN -> *_pt), sobrepõe (nome de skill é mais preciso)
    for b in json.load(open(BUILDS, encoding="utf-8")).get("builds", []):
        ab = b.get("abilities") or {}
        for k in ("q", "w", "e", "passive"):
            en, pt = ab.get(k), ab.get(k + "_pt")
            if en and pt and norm(en) != norm(pt):
                # compostos "A ou B" viram nomes separados
                for part_en, part_pt in zip(re.split(r"\s+ou\s+", en),
                                            re.split(r"\s+ou\s+", pt)):
                    part_en, part_pt = part_en.strip(), part_pt.strip()
                    if part_en and part_pt and norm(part_en) != norm(part_pt):
                        term[part_en] = part_pt
    return term


def make_translator(term):
    # mais longo primeiro pra "Cleric Robe" ganhar de "Robe"
    keys = sorted((k for k in term if len(k) >= 4), key=len, reverse=True)
    if not keys:
        return lambda s: (s, set())
    pat = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b")

    def tr(s):
        used = set()

        def repl(m):
            used.add(m.group(0))
            return term[m.group(0)]
        return pat.sub(repl, s), used
    return tr


def main():
    write = "--write" in sys.argv
    term = build_term_map()
    tr = make_translator(term)
    doc = json.load(open(BUILDS, encoding="utf-8"))
    changed = 0
    for b in doc.get("builds", []):
        for f in ("execution", "note"):
            src = b.get(f)
            if not src:
                continue
            out, used = tr(src)
            b[f + "_pt"] = out
            if out != src:
                changed += 1
                print(f"[{b.get('buildName')}/{f}] {sorted(used)}")
                print(f"   {out}")
    # termos EN candidatos que NÃO foram traduzidos (reporta p/ revisão)
    leftover = {}
    for b in doc.get("builds", []):
        for f in ("execution_pt", "note_pt"):
            for m in re.finditer(
                    r"[A-Z][a-zA-Z]+(?:\s+(?:of\s+)?[A-Z][a-zA-Z]+)+", b.get(f, "")):
                leftover[m.group(0)] = leftover.get(m.group(0), 0) + 1
    print("\n=== termos EN multi-palavra ainda presentes (revisar) ===")
    for k, v in sorted(leftover.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {v:2}  {k}")
    print(f"\n{changed} campos traduzidos. write={write}")
    if write:
        json.dump(doc, open(BUILDS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("builds.json gravado.")


if __name__ == "__main__":
    main()

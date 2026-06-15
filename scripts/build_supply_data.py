# -*- coding: utf-8 -*-
"""De onde os itens NASCEM: mapeia item -> mobs que o dropam (lado da oferta).

Cadeia do dump: mobs.json (mob -> LootListReference) -> loot.json (lista de
loot -> Item + sub-listas) -> item. Resolve o grafo de listas (com OR/AND e
referências aninhadas) e inverte para item -> fontes (mob, tier, fame,
categoria). Gera data/supply_data.json.

Complementa o killboard (destruição/demanda): aqui é a entrada de itens no
mercado via PvE. Rodar após build_items_db.py.
Uso:  python scripts/build_supply_data.py
"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MAX_SOURCES = 10        # mobs por item (os de maior fame)
MAX_DEPTH = 6           # profundidade de resolução de sub-listas


def walk_lootlist(node):
    """Extrai (itens_diretos, nomes_de_sub-listas) de um nó de lootlist,
    descendo por Item / OR / AND / LootListReference em qualquer nível."""
    items, refs = [], []

    def rec(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "@type" and isinstance(v, str):
                    items.append(v)
                elif k == "@name" and isinstance(v, str):
                    refs.append(v)
                elif k.startswith("@"):
                    continue
                else:
                    rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    # não capturar o @name da própria lista como referência
    for k, v in node.items():
        if k in ("@name", "@listtype"):
            continue
        rec(v)
    return items, refs


def main():
    items_db = json.loads((DATA / "items_db.json").read_text(encoding="utf-8"))
    meta = {i["id"]: i for i in items_db}

    loot = json.loads((DATA / "loot.json").read_text(encoding="utf-8"))
    lls = loot["LootDefinition"]["Lootlist"]
    if isinstance(lls, dict):
        lls = [lls]
    graph = {}
    for ll in lls:
        name = ll.get("@name")
        if name:
            graph[name] = walk_lootlist(ll)

    # closure: lista -> conjunto de item ids (memoizado, sem ciclo)
    closure = {}

    def resolve(name, depth=0, stack=()):
        if name in closure:
            return closure[name]
        if depth > MAX_DEPTH or name in stack or name not in graph:
            return set()
        direct, refs = graph[name]
        acc = set(direct)
        for r in refs:
            acc |= resolve(r, depth + 1, stack + (name,))
        closure[name] = acc
        return acc

    mobs = json.loads((DATA / "mobs.json").read_text(encoding="utf-8"))
    mob_list = mobs["Mobs"]["Mob"]
    if isinstance(mob_list, dict):
        mob_list = [mob_list]

    item_sources = defaultdict(list)
    for mob in mob_list:
        loot_node = mob.get("Loot")
        if not loot_node:
            continue
        refs = loot_node.get("LootListReference") or []
        if isinstance(refs, dict):
            refs = [refs]
        names = [r.get("@name") for r in refs if r.get("@name")]
        if not names:
            continue
        try:
            fame = int(float(mob.get("@fame", 0)))
            tier = int(mob.get("@tier", 0))
        except ValueError:
            fame, tier = 0, 0
        src = {"mob": mob.get("@uniquename"), "tier": tier, "fame": fame,
               "cat": mob.get("@mobtypecategory")}
        seen = set()
        for nm in names:
            for iid in resolve(nm):
                if iid in meta and iid not in seen:
                    seen.add(iid)
                    item_sources[iid].append(src)

    # mantém só as fontes de maior fame por item (mob mais "forte" = conteúdo
    # mais relevante), dedup por (mob, tier)
    out = {}
    for iid, srcs in item_sources.items():
        uniq = {}
        for s in srcs:
            uniq[(s["mob"], s["tier"])] = s
        top = sorted(uniq.values(), key=lambda s: -s["fame"])[:MAX_SOURCES]
        out[iid] = top

    dest = DATA / "supply_data.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"{len(out)} itens com fonte de drop -> {dest} "
          f"({dest.stat().st_size / 1e6:.1f} MB)")
    # amostra
    for iid in ("T4_2H_BOW", "T6_HEAD_PLATE_SET1", "T4_PLANKS"):
        s = out.get(iid)
        print(f"  {iid}: {len(s) if s else 0} fontes",
              f"(ex: {s[0]['mob']} T{s[0]['tier']} fama {s[0]['fame']})" if s else "")


if __name__ == "__main__":
    main()

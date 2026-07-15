# -*- coding: utf-8 -*-
"""Anexa builds NOVAS (formato-fonte: tree, buildName, content, abilities,
execution, note + nomes EN por slot) ao data/builds.json existente, resolvendo
os itens (id + nome PT + ícone) via o Resolver do build_builds_data. Dedup por
(tree, buildName, content). Reporta itens não resolvidos p/ criar alias.

Uso:  python scripts/build_add_builds.py <arquivo_do_workflow.json>
Depois: build_names_pt.py, build_spells_pt.py <loc>, build_notes_pt.py --write,
        build_build_images.py.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_builds_data import Resolver, SLOTS, clean_pt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "data" / "builds.json"


def enrich(b, r):
    items, unresolved = {}, []
    for slot in SLOTS:
        name = b.get(slot)
        if not name:                       # 2H => offhand "" => None
            items[slot] = None
            continue
        res = r.resolve(name, tier=8)
        if not res:
            unresolved.append((b.get("buildName"), slot, name))
            items[slot] = {"name_en": name, "id": None, "pt": name,
                           "pt_clean": name, "icon": None}
        else:
            items[slot] = {"name_en": name, "id": res["id"], "pt": res["pt"],
                           "pt_clean": clean_pt(res["pt"]), "icon": res["icon"]}
    return {
        "tree": b["tree"], "buildName": b["buildName"], "content": b["content"],
        "role": b.get("role", ""), "abilities": b.get("abilities", {}),
        "execution": b.get("execution", ""), "note": b.get("note"),
        "items": items,
    }, unresolved


def collect(x, acc):
    """Aceita {result:[{tree,builds}]}, lista de {tree,builds}, ou lista nua."""
    if isinstance(x, dict) and "builds" in x and isinstance(x["builds"], list):
        acc.extend(x["builds"])
    elif isinstance(x, dict) and "buildName" in x:
        acc.append(x)
    elif isinstance(x, list):
        for e in x:
            collect(e, acc)
    elif isinstance(x, dict):
        for v in x.values():
            collect(v, acc)


def main():
    src = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    new = []
    collect(src.get("result", src) if isinstance(src, dict) else src, new)
    doc = json.loads(BUILDS.read_text(encoding="utf-8"))
    builds = doc.get("builds", [])
    seen = {(b.get("tree"), b.get("buildName"), b.get("content")) for b in builds}
    r = Resolver()
    added, unresolved = 0, []
    for b in new:
        if not b.get("buildName") or not b.get("tree"):
            continue
        key = (b.get("tree"), b.get("buildName"), b.get("content"))
        if key in seen:
            continue
        seen.add(key)
        eb, unres = enrich(b, r)
        builds.append(eb)
        unresolved += unres
        added += 1
    doc["builds"] = builds
    doc["count"] = len(builds)
    BUILDS.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"{added} builds anexadas. Total agora: {len(builds)}.")
    if unresolved:
        print(f"\nITENS NAO RESOLVIDOS ({len(unresolved)}) — criar alias/corrigir:")
        for bn, slot, name in unresolved:
            print(f"  [{bn}] {slot} = {name!r}")


if __name__ == "__main__":
    main()

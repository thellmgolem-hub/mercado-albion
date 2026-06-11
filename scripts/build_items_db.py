# -*- coding: utf-8 -*-
"""
Gera data/items_db.json a partir dos dumps do ao-bin-dumps:
  - data/items_raw.json       (categorias, tier, peso, qualidade máxima)
  - data/items_formatted.json (nomes localizados PT-BR / EN-US)

Cada entrada do items_db.json:
  {"id": "T4_BAG@1", "en": "...", "pt": "...", "tier": 4, "ench": 1,
   "cat": "bags", "sub": "bag", "w": 1.5, "maxq": 5}

Uso:  python scripts/build_items_db.py [--refresh]
      --refresh  baixa os dumps novamente antes de gerar
"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DUMPS = {
    "items_raw.json": "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/items.json",
    "items_formatted.json": "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
}


def download_dumps():
    for name, url in DUMPS.items():
        dest = DATA / name
        print(f"Baixando {url} ...")
        with urllib.request.urlopen(url, timeout=300) as r:
            dest.write_bytes(r.read())
        print(f"  -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def iter_raw_items(raw):
    """Itera todas as entradas de item do dump bruto (listas ou dicts únicos)."""
    items = raw["items"]
    for key, val in items.items():
        if key in ("@xmlns:xsi", "@xsi:noNamespaceSchemaLocation", "shopcategories"):
            continue
        entries = val if isinstance(val, list) else [val]
        for e in entries:
            if isinstance(e, dict) and "@uniquename" in e:
                yield e


def build():
    print("Lendo dumps...")
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))
    fmt = json.loads((DATA / "items_formatted.json").read_text(encoding="utf-8"))

    meta = {}
    for e in iter_raw_items(raw):
        uid = e["@uniquename"]
        try:
            tier = int(e.get("@tier", 0))
        except ValueError:
            tier = 0
        try:
            weight = float(e.get("@weight", 0))
        except ValueError:
            weight = 0.0
        try:
            maxq = int(e.get("@maxqualitylevel", 1))
        except ValueError:
            maxq = 1
        meta[uid] = {
            "tier": tier,
            "cat": e.get("@shopcategory", "other"),
            "sub": e.get("@shopsubcategory1", ""),
            "w": weight,
            "maxq": maxq,
        }

    out = []
    missing_meta = 0
    for e in fmt:
        uid = e.get("UniqueName")
        if not uid:
            continue
        base = uid.split("@")[0]
        ench = int(uid.split("@")[1]) if "@" in uid else 0
        m = meta.get(uid) or meta.get(base)
        if m is None:
            missing_meta += 1
            m = {"tier": 0, "cat": "other", "sub": "", "w": 0.0, "maxq": 1}
        names = e.get("LocalizedNames") or {}
        en = names.get("EN-US") or uid
        pt = names.get("PT-BR") or en
        tier = m["tier"]
        if tier == 0 and uid[:1] == "T" and uid[1:2].isdigit():
            tier = int(uid[1])
        out.append({
            "id": uid,
            "en": en,
            "pt": pt,
            "tier": tier,
            "ench": ench,
            "cat": m["cat"],
            "sub": m["sub"],
            "w": m["w"],
            "maxq": m["maxq"],
        })

    dest = DATA / "items_db.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    cats = {}
    for i in out:
        cats[i["cat"]] = cats.get(i["cat"], 0) + 1
    print(f"OK: {len(out)} itens -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    print(f"  sem metadados no dump bruto: {missing_meta}")
    print("  itens por categoria:")
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"    {c:<12} {n}")


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        download_dumps()
    build()

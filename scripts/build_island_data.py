# -*- coding: utf-8 -*-
"""Extrai os dados de ILHA (culturas, animais, trabalhadores) dos dumps.

Lê data/items_raw.json (mecânica de fazenda: foco, ciclo, bônus, prole,
consumo/ração) + data/loot.json (rendimento da colheita) e produz
data/island_data.json — pequeno, versionado, usado em runtime por albion/island.py.

Fontes do ao-bin-dumps (re-baixáveis):
  items_raw.json = master/items.json ; loot.json = master/loot.json
Os dumps grandes podem ser apagados depois; só o island_data.json fica.

Uso: python scripts/build_island_data.py            # build completo (exige loot.json)
     python scripts/build_island_data.py --laborers  # só reconstrói trabalhadores
                                                      # (merge; NÃO exige loot.json)
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Famílias de diário de COLETA -> recurso BRUTO (id único por tier).
# Só as famílias de coleta com recurso de id limpo entram aqui. As famílias de
# FABRICAÇÃO (HUNTER/MAGE/MERCENARY/TOOLMAKER/WARRIOR) não entregam recurso bruto
# e a PESCA não tem id único por tier (T*_FISH_FRESHWATER_...). Para essas, o
# recurso fica None — o valor do diário é a margem vazio->cheio, não a entrega.
JOURNAL_RESOURCE = {
    "WOOD": "WOOD", "ORE": "ORE", "HIDE": "HIDE", "FIBER": "FIBER",
    "STONE": "ROCK",
}


def _avg_amount(s):
    """'3-6' -> 4.5 ; '2' -> 2.0."""
    s = str(s or "0")
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if m:
        return (int(m.group(1)) + int(m.group(2))) / 2
    try:
        return float(s)
    except ValueError:
        return 0.0


def _f(o, k, default=0.0):
    try:
        return float(o.get(k, default) or default)
    except (TypeError, ValueError):
        return default


def index_items(raw):
    """Indexa TODO item por @uniquename (a árvore é XML->JSON aninhada)."""
    idx = {}

    def walk(o):
        if isinstance(o, dict):
            u = o.get("@uniquename")
            if u and u not in idx:
                idx[u] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(raw)
    return idx


def index_loot(loot):
    out = {}
    for ll in loot["LootDefinition"]["Lootlist"]:
        n = ll.get("@name")
        if n:
            items = ll.get("Item") or []
            if isinstance(items, dict):
                items = [items]
            out[n] = items
    return out


def build_crops(items, loot):
    """Culturas: item com 'harvest' (a semente plantável)."""
    out = {}
    for uid, o in items.items():
        h = o.get("harvest")
        if not isinstance(h, dict) or not h.get("@lootlist"):
            continue
        ll = loot.get(h["@lootlist"]) or []
        crop_id, crop_amt, byproduct = None, 0.0, None
        for it in ll:
            t = it.get("@type", "")
            amt = _avg_amount(it.get("@amount")) * _f(it, "@chance", 1.0)
            # o produto principal é o de maior chance×amount sem ser worm/seed
            if "WORM" in t or "SEED" in t:
                byproduct = byproduct or t
                continue
            if amt > crop_amt:
                crop_id, crop_amt = t, amt
        if not crop_id:
            continue
        seed = h.get("seed") or {}
        out[uid] = {
            "crop": crop_id,
            "crop_yield": round(crop_amt, 2),       # média por colheita (sem foco)
            "byproduct": byproduct,
            "focus_cost": _f(o, "@activefarmfocuscost"),
            "cycle_s": _f(o, "@activefarmcyclelengthseconds"),
            # @activefarmbonus do crop é apenas 2*(1-rebrota) — NÃO é multiplicador
            # de colheita (a colheita é fixa ~4.5). O foco GARANTE a volta da
            # semente; modelamos isso por seed_regrow_chance, não por este campo.
            "seed_regrow_chance": _f(seed, "@chance"),
            "seed_regrow_amount": _f(seed, "@amount"),
            "tier": int(_f(o, "@tier")),
        }
    return out


def build_animals(items):
    """Animais: item com 'grownitem' (a cria). Inclui consumo/ração + foco."""
    out = {}
    for uid, o in items.items():
        g = o.get("grownitem")
        if not isinstance(g, dict) or not g.get("@uniquename"):
            continue
        off = g.get("offspring") or {}
        cons = (o.get("consumption") or {}).get("food") or {}
        accepted = (cons.get("acceptedfood") or {}).get("@foodcategory")
        out[uid] = {
            "grown": g["@uniquename"],
            "growtime_s": _f(g, "@growtime"),
            "offspring_chance": _f(off, "@chance"),
            "offspring_amount": _f(off, "@amount"),
            # @activefarmbonus por animal (0.06–0.8, decresce com tier): efeito
            # do FOCO no pasto, modelado como prole extra esperada (estimativa).
            "farm_bonus": _f(o, "@activefarmbonus"),
            "focus_cost": _f(o, "@activefarmfocuscost"),
            "food_category": accepted,                 # 'plants' / 'meat'
            "nutrition_max": _f(cons, "@nutritionmax"),
            "seconds_per_nutrition": _f(cons, "@secondspernutrition"),
            "tier": int(_f(o, "@tier")),
        }
    return out


def build_food(items):
    """Valor de nutrição por item de comida (p/ custo de ração dos animais)."""
    out = {}
    for uid, o in items.items():
        nv = _f(o, "@nutrition")
        fc = o.get("@foodcategory")
        if nv > 0 and fc:
            out[uid] = {"nutrition": nv, "category": fc}
    return out


def _journal_detail(node):
    """Extrai do NÓ-BASE do diário (items_raw) os campos do modelo do trabalhador.

    node = idx['T6_JOURNAL_TOOLMAKER'] (sem sufixo _EMPTY/_FULL). Devolve:
      max_fame, base_loot_amount, fill (o que ENCHE craftando) e loot (o que o
      trabalhador DEVOLVE). fill fica None em diários de coleta/pesca (que enchem
      coletando, não craftando — não têm famefillingmissions.craftitemfame).
    """
    fill = None
    ff = node.get("famefillingmissions") or {}
    cif = ff.get("craftitemfame")
    if isinstance(cif, dict):
        vi = cif.get("validitem") or []
        if isinstance(vi, dict):
            vi = [vi]
        items = [v.get("@id") for v in vi if v.get("@id")]
        if items:
            fill = {
                "fame_value": _f(cif, "@value"),   # fama por craft de 1 item @mintier
                "min_tier": int(_f(cif, "@mintier")),
                "items": items,
            }
    loot = []
    ll = (node.get("lootlist") or {}).get("loot")
    if isinstance(ll, dict):
        ll = [ll]
    for e in (ll or []):
        name = e.get("@itemname")
        if not name:
            continue
        loot.append({
            "item": name,
            "ench": int(_f(e, "@itemenchantmentlevel")),   # 0 se ausente
            "amount": _avg_amount(e.get("@itemamount") or "1"),
            "weight": _f(e, "@weight"),
        })
    return {
        "max_fame": _f(node, "@maxfame"),
        "base_loot_amount": _f(node, "@baselootamount"),
        "fill": fill,
        "loot": loot,
    }


def build_laborers(item_ids, items=None):
    """Pares de diário vazio/cheio por família/tier + o recurso produzido.

    Os pares _EMPTY/_FULL ficam no catálogo processado (items_db.json); o
    NÓ-BASE do diário (ex.: 'T6_JOURNAL_TOOLMAKER', sem sufixo) fica em
    items_raw.json e traz o modelo do trabalhador (max_fame, base_loot_amount,
    fill=itens que enchem craftando, loot=o que o trabalhador devolve). Passe
    `items` (índice de items_raw por @uniquename) para enriquecer; sem ele o
    comportamento antigo (só o par + família/tier/recurso) é preservado.
    """
    items = items or {}
    out = {}
    for uid in item_ids:
        if "_JOURNAL_" not in uid or not uid.endswith("_EMPTY"):
            continue
        full = uid[:-6] + "_FULL"
        if full not in item_ids:
            continue
        m = re.match(r"^T(\d+)_JOURNAL_([A-Z]+)", uid)
        if not m:
            continue
        tier, fam = int(m.group(1)), m.group(2)
        entry = {
            "full": full,
            "family": fam,
            "tier": tier,
            # None p/ fabricantes/pesca (não entregam recurso bruto de id limpo)
            "resource_family": JOURNAL_RESOURCE.get(fam),
        }
        base = uid[:-6]                       # 'T6_JOURNAL_TOOLMAKER' (nó do dump)
        node = items.get(base)
        if node is not None:
            entry.update(_journal_detail(node))
        out[uid] = entry
    return out


def rebuild_laborers():
    """Reconstrói SÓ a seção 'laborers' do items_raw.json e faz MERGE no
    data/island_data.json existente (preserva crops/animals/food).

    Não depende de loot.json (que pode ter sido apagado) — só de items_raw.json
    (o modelo do trabalhador) + items_db.json (os pares _EMPTY/_FULL). Use quando
    quiser atualizar os trabalhadores sem re-baixar/rodar tudo (build main()).
    """
    for fname in ("items_raw.json", "items_db.json", "island_data.json"):
        if not (DATA / fname).exists():
            raise SystemExit(
                f"Falta data/{fname} para reconstruir os trabalhadores.\n"
                "  items_raw.json = master/items.json (ao-bin-dumps)\n"
                "  items_db.json  = scripts/build_items_db.py\n"
                "  island_data.json = scripts/build_island_data.py")
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))
    db_ids = {i["id"] for i in
              json.loads((DATA / "items_db.json").read_text(encoding="utf-8"))}
    items = index_items(raw)
    laborers = build_laborers(db_ids, items)
    data = json.loads((DATA / "island_data.json").read_text(encoding="utf-8"))
    before = {k: len(data.get(k, {})) for k in ("crops", "animals", "food")}
    data["laborers"] = laborers        # MERGE: só troca a seção de trabalhadores
    (DATA / "island_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    after = {k: len(data.get(k, {})) for k in ("crops", "animals", "food")}
    with_fill = sum(1 for v in laborers.values() if v.get("fill"))
    print(f"laborers reconstruídos: {len(laborers)} diários "
          f"({with_fill} enchem craftando). crops/animals/food preservados: "
          f"{before} -> {after}"
          + ("  [OK]" if before == after else "  [ALERTA: contagem mudou!]"))
    return laborers


def main():
    for fname in ("items_raw.json", "loot.json", "items_db.json"):
        if not (DATA / fname).exists():
            raise SystemExit(
                f"Falta data/{fname}. Re-baixe os dumps do ao-bin-dumps:\n"
                "  items_raw.json = master/items.json ; loot.json = master/loot.json\n"
                "  (items_db.json vem de scripts/build_items_db.py)")
    raw = json.loads((DATA / "items_raw.json").read_text(encoding="utf-8"))
    loot = json.loads((DATA / "loot.json").read_text(encoding="utf-8"))
    db_ids = {i["id"] for i in
              json.loads((DATA / "items_db.json").read_text(encoding="utf-8"))}
    items = index_items(raw)
    lootidx = index_loot(loot)
    out = {
        "crops": build_crops(items, lootidx),
        "animals": build_animals(items),
        "food": build_food(items),
        "laborers": build_laborers(db_ids, items),
        "_fonte": "items.json + loot.json + items_db.json — ao-bin-dumps",
    }
    (DATA / "island_data.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"island_data.json: {len(out['crops'])} culturas, "
          f"{len(out['animals'])} animais, {len(out['food'])} comidas, "
          f"{len(out['laborers'])} diários")


if __name__ == "__main__":
    import sys
    if "--laborers" in sys.argv:   # só reconstrói trabalhadores (não exige loot.json)
        rebuild_laborers()
    else:
        main()

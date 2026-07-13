# -*- coding: utf-8 -*-
"""Resolve os itens das builds (nome EN do guia) para id + nome PT oficial + icone.

Fluxo:
  1. resolve(name, tier) -> {id, en, pt, icon} usando data/items_db.json
  2. (modo --audit) testa a cobertura contra a lista de itens do guia e
     imprime os que NAO resolveram, pra a gente criar alias.
  3. (uso normal, depois) le as builds extraidas (JSON) e gera data/builds.json.

O icone vem do proxy do proprio app: /icon?id=<ID> (cache em disco + fallback).
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

_TIER_PREFIX = re.compile(
    r"^(adept|expert|master|grandmaster|elder|journeyman|novice|beginner)'?s\s+",
    re.IGNORECASE)


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_en(en: str) -> str:
    """nome EN sem o prefixo de tier (Elder's/Master's/...)."""
    return norm(_TIER_PREFIX.sub("", en or ""))


# Aliases: nome do guia -> nome-base EN no items_db (quando nao batem direto).
ALIAS = {
    "1h fire staff": "fire staff",
    "1h frost staff": "frost staff",
    "1h nature staff": "nature staff",
    "1h cursed": "cursed staff",
    "1h fire": "fire staff",
    "1h frost": "frost staff",
    "1h nature": "nature staff",
    "bow": "bow",
    "rosalia's diary": "rosalias diary",
    "rosalias diary": "rosalias diary",
    "1h": "",
}


class Resolver:
    def __init__(self):
        items = json.loads((DATA / "items_db.json").read_text(encoding="utf-8"))
        # index: base_en -> lista de entradas
        self.by_base = {}
        for it in items:
            b = base_en(it.get("en", ""))
            self.by_base.setdefault(b, []).append(it)
        self.items = items

    def _pick(self, candidates, tier):
        # prefere o tier pedido e encanto 0; senao o maior tier disp., ench 0
        ench0 = [c for c in candidates if (c.get("ench", 0) == 0)]
        pool = ench0 or candidates
        exact = [c for c in pool if c.get("tier") == tier]
        if exact:
            return exact[0]
        pool2 = sorted(pool, key=lambda c: -(c.get("tier") or 0))
        return pool2[0]

    def resolve(self, name, tier=8):
        key = norm(name)
        key = ALIAS.get(key, key)
        if not key:
            return None
        cands = self.by_base.get(key)
        if not cands:
            # tenta: guia-name eh sufixo/subset de um base_en unico
            hits = [b for b in self.by_base if key and (b == key or b.endswith(" " + key) or key in b.split())]
            # match mais especifico: base igual ao key
            hits = [b for b in self.by_base if b == key] or \
                   [b for b in self.by_base if b.endswith(key)] or hits
            if len(hits) == 1:
                cands = self.by_base[hits[0]]
        if not cands:
            return None
        it = self._pick(cands, tier)
        # o endpoint real é /icon/{id} (path), público (não começa com /api/)
        return {"id": it["id"], "en": it["en"], "pt": it.get("pt", it["id"]),
                "tier": it.get("tier"), "icon": f"/icon/{it['id']}"}


# Lista de itens distintos vistos no guia (pra auditoria de cobertura).
AUDIT_ITEMS = [
    # armas
    "Dawnsong", "Brimstone Staff", "1H Fire Staff", "Wildfire Staff",
    "Permafrost Prism", "Icicle Staff", "Great Frost Staff", "Arctic Staff",
    "Glacial Staff", "1H Frost Staff", "Longbow", "Wailing Bow", "Bow",
    "Bow of Badon", "Skystrider Bow", "Warbow", "Blight Staff", "Rampant Staff",
    "1H Nature Staff", "Ironroot Staff", "Forgebark Staff", "Great Nature Staff",
    "Lifecurse Staff", "Damnation Staff", "Rotcaller Staff", "Great Cursed Staff",
    "Cursed Skull", "Staff of Balance", "Grailseeker", "Phantom Twinblade",
    "Soulscythe", "Quarterstaff", "Realmbreaker", "Infernal Scythe",
    "Carrioncaller", "Crystal Reaper", "Battleaxe", "Bear Paws",
    # off-hands
    "Cryptcandle", "Rosalia's Diary", "Mistcaller", "Muisak", "Tome of Spells",
    "Celestial Censer", "Torch", "Taproot",
    # cabeca
    "Knight Helmet", "Assassin Hood", "Druid Cowl", "Feyscale Hat", "Royal Hood",
    "Hunter Hood", "Cleric Cowl", "Cowl of Purity", "Hood of Tenacity",
    "Guardian Helmet", "Graveguard Helmet", "Mistwalker Hood", "Specter Hood",
    "Mercenary Hood", "Scholar Cowl", "Stalker Hood", "Hellion Hood",
    "Fiend Cowl", "Helmet of Valor", "Royal Cowl",
    # peito
    "Feyscale Robe", "Cleric Robe", "Scholar Robe", "Mage Robe", "Knight Armor",
    "Judicator Armor", "Jacket of Tenacity", "Robe of Purity", "Hellion Jacket",
    "Mistwalker Jacket", "Assassin Jacket", "Mercenary Jacket", "Duskweaver Armor",
    "Royal Jacket", "Royal Armor", "Soldier Armor",
    # pes
    "Graveguard Boots", "Royal Sandals", "Royal Shoes", "Mercenary Shoes",
    "Stalker Shoes", "Boots of Valor", "Shoes of Tenacity", "Soldier Boots",
    "Scholar Sandals", "Feyscale Sandals", "Hunter Shoes", "Assassin Shoes",
    # capas
    "Martlock Cape", "Brecilien Cape", "Fort Sterling Cape", "Thetford Cape",
    "Lymhurst Cape", "Morgana Cape", "Keeper Cape", "Undead Cape", "Smuggler Cape",
    "Bridgewatch Cape", "Caerleon Cape", "Avalonian Cape",
    # comida
    "Deadwater Eel Stew", "Pork Omelette", "Beef Stew", "Avalonian Pork Omelette",
    "Beef Sandwich", "Roasted Puremist Snapper", "Roast Pork",
    "Thunderfall Lurcher Sandwich", "Dusthole Crab Omelette",
    # pocoes
    "Resistance Potion", "Gigantify Potion", "Healing Potion", "Hellfire Potion",
    "Cleansing Potion", "Energy Potion", "Acid Potion", "Poison Potion",
]


def audit():
    r = Resolver()
    ok, bad = [], []
    for name in AUDIT_ITEMS:
        res = r.resolve(name, tier=8)
        if res:
            ok.append((name, res["id"], res["pt"]))
        else:
            bad.append(name)
    print(f"RESOLVIDOS: {len(ok)}/{len(AUDIT_ITEMS)}")
    if bad:
        print("\nNAO RESOLVIDOS (precisam de alias):")
        for n in bad:
            print("  -", n)
    print("\nAmostra resolvida:")
    for name, iid, pt in ok[:12]:
        print(f"  {name:22s} -> {iid:24s} {pt}")


SLOTS = ["weapon", "offhand", "head", "chest", "shoes", "cape", "potion", "food"]

_TIER_PT = re.compile(
    r"\s+d[oa]s?\s+(Anci[aã]o|Gr[aã]o-Mestre|Mestre|Perito|Adepto|"
    r"Jornaleiro|Novato|Iniciante)\s*$", re.IGNORECASE)


def clean_pt(pt: str) -> str:
    """Remove o sufixo de tier do nome PT (ex.: 'Robe Feerico do Anciao' -> 'Robe Feerico')."""
    return _TIER_PT.sub("", pt or "").strip()


def generate(out_path):
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))
    result = data.get("result", data)
    if isinstance(result, str):
        result = json.loads(result)
    builds = result["builds"]
    r = Resolver()
    unresolved = []
    enriched = []
    for b in builds:
        items = {}
        for slot in SLOTS:
            name = b.get(slot)
            if not name:
                items[slot] = None
                continue
            res = r.resolve(name, tier=8)
            if not res:
                unresolved.append((b["buildName"], slot, name))
                items[slot] = {"name_en": name, "id": None, "pt": name,
                               "pt_clean": name, "icon": None}
            else:
                items[slot] = {"name_en": name, "id": res["id"], "pt": res["pt"],
                               "pt_clean": clean_pt(res["pt"]), "icon": res["icon"]}
        enriched.append({
            "tree": b["tree"], "buildName": b["buildName"],
            "content": b["content"], "role": b.get("role", ""),
            "abilities": b.get("abilities", {}),
            "execution": b.get("execution", ""), "note": b.get("note"),
            "items": items,
        })
    out = {"trees": result.get("trees", []), "count": len(enriched),
           "builds": enriched}
    (DATA / "builds.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"data/builds.json gerado: {len(enriched)} builds")
    if unresolved:
        print(f"\nITENS NAO RESOLVIDOS ({len(unresolved)}):")
        for bn, slot, name in unresolved:
            print(f"  [{bn}] {slot} = {name}")
    else:
        print("Todos os itens de todas as builds resolvidos.")


if __name__ == "__main__":
    if "--audit" in sys.argv:
        audit()
    elif "--generate" in sys.argv:
        i = sys.argv.index("--generate")
        generate(sys.argv[i + 1])
    else:
        print("use --audit  ou  --generate <arquivo_saida_workflow>")

"""Traduz os nomes de HABILIDADE (Q/W/E/passiva) das builds para PT-BR usando o
dump oficial de localização do Albion (ao-bin-dumps/localization.json, formato
TMX->JSON: cada `tu` tem `tuv` por idioma com `@xml:lang` e `seg`).

Roda OFFLINE (1x). Adiciona q_pt/w_pt/e_pt/passive_pt em cada build["abilities"]
de data/builds.json, caindo pro nome EN quando não acha (nunca quebra). O bot lê
o campo *_pt (com fallback pro EN). Uso:

    python scripts/build_spells_pt.py <caminho/localization.json>

Guilda 100% BR: exibimos só o PT (o guia interno segue em EN pra manutenção).
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "data" / "builds.json"

# nomes que NÃO são feitiço (ruído do guia) — deixa como está, sem procurar
_NOT_A_SPELL = {"exclusivo da arma", "arma", "nenhum", "-", ""}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.strip().lower())


def load_en_pt(loc_path):
    """{norm(EN-US): PT-BR} de todas as unidades de tradução do dump."""
    data = json.load(open(loc_path, encoding="utf-8"))
    tus = (((data.get("tmx") or {}).get("body") or {}).get("tu")) or []
    if isinstance(tus, dict):
        tus = [tus]
    out = {}
    for tu in tus:
        if not isinstance(tu, dict):
            continue
        tuv = tu.get("tuv") or []
        if isinstance(tuv, dict):
            tuv = [tuv]
        en = pt = None
        for t in tuv:
            if not isinstance(t, dict):
                continue
            seg = t.get("seg")
            if isinstance(seg, dict):          # seg com formatação -> texto
                seg = seg.get("#text") or seg.get("$") or ""
            if not isinstance(seg, str):
                continue
            lang = (t.get("@xml:lang") or "").upper()
            if lang == "EN-US":
                en = seg
            elif lang == "PT-BR":
                pt = seg
        if en and pt:
            out.setdefault(norm(en), pt)
    return out


def translate(name, en_pt, missing):
    """Traduz um nome de skill, tratando compostos ('A ou B', 'A/B')."""
    if not name or norm(name) in _NOT_A_SPELL:
        return name
    parts = re.split(r"\s+ou\s+|\s*/\s*", name)
    done = []
    for p in parts:
        p = p.strip()
        pt = en_pt.get(norm(p))
        if pt:
            done.append(pt)
        else:
            done.append(p)
            if norm(p) not in _NOT_A_SPELL:
                missing.add(p)
    return " ou ".join(done)


def main():
    if len(sys.argv) < 2:
        print("uso: python scripts/build_spells_pt.py <localization.json>")
        raise SystemExit(2)
    en_pt = load_en_pt(sys.argv[1])
    print(f"localização: {len(en_pt)} pares EN->PT")
    doc = json.load(open(BUILDS, encoding="utf-8"))
    builds = doc.get("builds") or []
    missing = set()
    n_ok = n_total = 0
    for b in builds:
        ab = b.get("abilities") or {}
        for key in ("q", "w", "e", "passive"):
            v = ab.get(key)
            if not v:
                continue
            n_total += 1
            pt = translate(v, en_pt, missing)
            ab[key + "_pt"] = pt
            if norm(pt) != norm(v):
                n_ok += 1
        b["abilities"] = ab
    json.dump(doc, open(BUILDS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"builds.json atualizado: {n_ok}/{n_total} nomes traduzidos.")
    if missing:
        print(f"{len(missing)} sem tradução (mantidos em EN):")
        for m in sorted(missing):
            print("   ", m)


if __name__ == "__main__":
    main()

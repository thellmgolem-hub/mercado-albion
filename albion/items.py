# -*- coding: utf-8 -*-
"""Banco de itens em memória: busca por nome PT-BR/EN com filtros."""
import json
import re
import unicodedata
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

_TIER_ENCH_TOKEN = re.compile(r"^([1-8])\.([0-4])$")  # ex.: "4.1" = tier 4 encanto 1
_TIER_TOKEN = re.compile(r"^t([1-8])$")               # ex.: "t6"
_ENCH_TOKEN = re.compile(r"^@([0-4])$")               # ex.: "@2"


def norm(s: str) -> str:
    """minúsculas + sem acentos"""
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


class ItemDB:
    def __init__(self, path: Path = DATA / "items_db.json"):
        self.items = json.loads(Path(path).read_text(encoding="utf-8"))
        self.by_id = {}
        for it in self.items:
            it["_npt"] = norm(it["pt"])
            it["_nen"] = norm(it["en"])
            it["_nid"] = it["id"].lower()
            self.by_id[it["id"]] = it

    def get(self, item_id: str):
        return self.by_id.get(item_id) or self.by_id.get(item_id.upper())

    def search(self, q: str, cat=None, sub=None, tier_min=None, tier_max=None,
               ench=None, limit=30, group=False):
        """Busca por nome (PT/EN) ou id. Tokens especiais: 't4', '@1', '4.1'.

        group=True colapsa as variantes de encanto (@0..@4) numa linha por
        item-base, anexando `enchants` com os níveis disponíveis. O limite
        passa a contar itens-base — sem isso os encantos entopem a lista e
        escondem a maioria dos itens em buscas amplas.
        """
        tokens = norm(q).split()
        text_tokens = []
        f_tier, f_ench = None, None
        for t in tokens:
            m = _TIER_ENCH_TOKEN.match(t)
            if m:
                f_tier, f_ench = int(m.group(1)), int(m.group(2))
                continue
            m = _TIER_TOKEN.match(t)
            if m:
                f_tier = int(m.group(1))
                continue
            m = _ENCH_TOKEN.match(t)
            if m:
                f_ench = int(m.group(1))
                continue
            text_tokens.append(t)

        def passes(it):
            if cat and it["cat"] != cat:
                return False
            if sub and it["sub"] != sub:
                return False
            if f_tier is not None and it["tier"] != f_tier:
                return False
            if tier_min is not None and it["tier"] < tier_min:
                return False
            if tier_max is not None and it["tier"] > tier_max:
                return False
            if f_ench is not None and it["ench"] != f_ench:
                return False
            if ench is not None and it["ench"] != ench:
                return False
            return True

        candidates = [it for it in self.items if passes(it)]

        # passo 1: AND estrito (todos os tokens de texto casam)
        results = []
        for it in candidates:
            score = self._score(it, text_tokens)
            if score is not None:
                results.append((score, it))

        # passo 2: fallback parcial — se o AND não achou nada e há 2+ tokens,
        # aceita itens que casam a MAIORIA dos tokens (resolve "manto de
        # thetford", palavra a mais, sinônimo). Penaliza os tokens que faltam.
        if not results and len(text_tokens) >= 2:
            need = (len(text_tokens) + 1) // 2
            for it in candidates:
                matched, sc = 0, 0
                for t in text_tokens:
                    cand = self._token_score(it, t)
                    if cand is not None:
                        matched += 1
                        sc += cand
                if matched >= need:
                    missed = len(text_tokens) - matched
                    results.append((100 + missed * 50 + sc, it))

        results.sort(key=lambda r: (r[0], r[1]["tier"], r[1]["ench"], r[1]["id"]))

        if not group:
            return [self._public(it) for _, it in results[:limit]]

        # colapsa por item-base (id sem o sufixo @N); o limite conta bases
        bases = {}
        order = []
        for _, it in results:
            base = it["id"].split("@")[0]
            rep = bases.get(base)
            if rep is None:
                if len(bases) >= limit:
                    continue
                rep = self._public(it)  # primeiro = melhor pontuado (ench baixo)
                rep["base_id"] = base
                rep["enchants"] = []
                bases[base] = rep
                order.append(base)
            if it["ench"] not in rep["enchants"]:
                rep["enchants"].append(it["ench"])
        for base in order:
            bases[base]["enchants"].sort()
        return [bases[b] for b in order]

    @staticmethod
    def _token_score(it, t):
        """Pontua um token contra um item. None = não casa; menor = melhor.

        1 = começo do nome, 2 = começo de palavra, 4 = substring qualquer.
        Tolera plurais comuns ("placas"->"placa", "pocoes"->"pocao").
        """
        variants = [t]
        if len(t) > 3 and t.endswith("s"):
            variants.append(t[:-1])
            if t.endswith("oes"):
                variants.append(t[:-3] + "ao")
            elif t.endswith("es"):
                variants.append(t[:-2])
        best = None
        for v in variants:
            if v in it["_npt"] or v in it["_nen"] or v in it["_nid"]:
                if it["_npt"].startswith(v) or it["_nen"].startswith(v):
                    cand = 1
                elif f" {v}" in f" {it['_npt']}" or f" {v}" in f" {it['_nen']}":
                    cand = 2
                else:
                    cand = 4
                best = cand if best is None else min(best, cand)
        return best

    @classmethod
    def _score(cls, it, tokens):
        """AND estrito: None se algum token não casa; menor = melhor."""
        if not tokens:
            return 50
        joined = " ".join(tokens)
        if it["_nid"] == joined or it["id"].lower() == joined:
            return 0
        score = 0
        for t in tokens:
            cand = cls._token_score(it, t)
            if cand is None:
                return None
            score += cand
        return score

    def filter(self, cat=None, sub=None, tier_min=None, tier_max=None,
               ench_list=None, ids=None, limit=None):
        """Filtro estrutural (para o scanner)."""
        out = []
        for it in self.items:
            if ids is not None and it["id"] not in ids:
                continue
            if cat and it["cat"] != cat:
                continue
            if sub and it["sub"] != sub:
                continue
            if tier_min is not None and it["tier"] < tier_min:
                continue
            if tier_max is not None and it["tier"] > tier_max:
                continue
            if ench_list is not None and it["ench"] not in ench_list:
                continue
            out.append(self._public(it))
            if limit and len(out) >= limit:
                break
        return out

    def categories(self):
        """Categorias e subcategorias com contagem (para montar filtros na UI)."""
        cats = {}
        for it in self.items:
            c = cats.setdefault(it["cat"], {"count": 0, "subs": {}})
            c["count"] += 1
            c["subs"][it["sub"]] = c["subs"].get(it["sub"], 0) + 1
        return cats

    @staticmethod
    def _public(it):
        return {k: v for k, v in it.items() if not k.startswith("_")}

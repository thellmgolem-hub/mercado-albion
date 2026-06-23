# -*- coding: utf-8 -*-
"""Linha de Produção: cadeia de craft/refino inteira da guild como grafo (DAG).

Expande a árvore de RECEITA (BOM) de um ou mais produtos finais até o recurso
bruto (coleta/ilha) e enriquece cada item com RRR ciente de refino (com e sem
foco, por cidade real), foco base, e preços de compra/venda saneados de âncoras.
Com isso o cliente monta o grafo visual e PROPAGA a quantidade de trás p/ frente:
fixa-se o alvo no produto final e cada etapa mostra os recursos necessários.

Divisão de trabalho:
- build_graph(): grafo ESTÁTICO (estrutura + RRR por cidade + preços). Não
  depende do alvo nem dos toggles — é o que vai ao cliente.
- solve(): espelha em Python a propagação que o cliente roda ao vivo (para CLI,
  testes e validação). Dado o alvo + estado por nó (fabricar/comprar, foco,
  cidade), devolve demanda/crafts por etapa, lista de compras, foco total e
  lucro.

Tudo reusa albion/craft.py e albion/production.py (mesma fonte de verdade das
demais análises). Pontos de mecânica fixados pela auditoria (workflow):
- BRUTO é folha: a "receita" de transmutação de recurso (ex.: T5_ORE<-T4_ORE)
  NÃO entra na cadeia — production.is_raw_resource() decide, não recipe_for.
- RRR reduz a QUANTIDADE de insumo (devolve material); por isso abatemos RRR na
  quantidade propagada e NÃO no custo (senão é dupla contagem).
- foco é por BATCH de craft (recipe['focus']), não por unidade; e é um recurso
  à parte (pontos) — só vira prata se o usuário der um preço/ponto.
- price_of(id, city) deve vir do q1 SANEADO de âncoras (clean_price_rows).
"""
import json
import math
import time
from collections import defaultdict, deque
from contextlib import contextmanager

from . import config
from . import craft
from . import production as prod
from .flips import sell_revenue

MAX_DEPTH = 12

_FARM_GOODS = None


def _farm_goods():
    """Conjunto de itens que NASCEM na ilha (culturas, comidas, animais adultos)
    — usado para classificar o setor 'Ilha' na cadeia."""
    global _FARM_GOODS
    if _FARM_GOODS is None:
        d = craft._load("island_data.json") or {}
        s = set()
        for info in (d.get("crops") or {}).values():
            if info.get("crop"):
                s.add(info["crop"])
            if info.get("byproduct"):
                s.add(info["byproduct"])
        s.update((d.get("food") or {}).keys())
        for info in (d.get("animals") or {}).values():
            if info.get("grown"):
                s.add(info["grown"])
        _FARM_GOODS = s
    return _FARM_GOODS


def _sector(item, has_recipe):
    """Classifica a etapa na infraestrutura da guild: refino, fabricação, ilha
    (fazenda), coleta (bruto) ou compra (folha sem receita nem fazenda)."""
    if has_recipe:
        return "refine" if craft._refining_family(item) else "craft"
    if item in _farm_goods():
        return "farm"
    if prod.is_raw_resource(item):
        return "raw"
    return "buy"


def _prod_recipe(item_id):
    """Receita para a cadeia de produção (ignora transmutação de bruto)."""
    return None if prod.is_raw_resource(item_id) else craft.recipe_for(item_id)


def _best_sell(price_of, item, cities, mode, premium):
    best = None
    for c in cities:
        p = price_of(item, c)
        if not p:
            continue
        net = sell_revenue(p, mode, premium)
        if best is None or net > best[1]:
            best = (c, net, p)
    return best   # (city, net, gross) | None


def build_graph(roots, price_of, *, premium=True, sell_mode="order",
                cities=None, max_depth=MAX_DEPTH):
    """Grafo de receita enriquecido a partir de `roots` (1+ produtos finais).

    Devolve {"roots", "nodes": {item: node}, "cities", "fees", ...}. Cada nó:
      item, is_raw, buy_by_city {cidade: preço}, buy_city/buy_price (mais barato),
      e — se craftável — recipe [{id,count}], output, focus_base, category,
      bonus_city, rrr_by_city {cidade: {nf, f}}. Raízes ganham sell_city/
      sell_gross/sell_net (líquido pré-calculado; o cliente recalcula conforme
      premium a partir do gross, então o premium é reativo sem novo fetch).
    Cada item único é um nó (intermediários compartilhados somam demanda).
    """
    cities = list(cities or config.ROYAL_CITIES)
    if isinstance(roots, str):
        roots = [roots]
    nodes = {}

    def buy_by_city(item):
        m = {}
        for c in cities:
            p = price_of(item, c)
            if p:
                m[c] = round(p)
        return m

    def visit(item, depth):
        if item in nodes:
            return
        rec = _prod_recipe(item) if depth < max_depth else None
        bm = buy_by_city(item)
        best = min(bm.items(), key=lambda kv: kv[1]) if bm else None
        node = {
            "item": item,
            "buy_by_city": bm,
            "buy_city": best[0] if best else None,
            "buy_price": best[1] if best else None,
        }
        if rec and rec.get("inputs"):
            cat = rec.get("category")
            node.update({
                "is_raw": False,
                "recipe": [{"id": i["id"], "count": i["count"]}
                           for i in rec["inputs"]],
                "output": rec.get("output", 1) or 1,
                "focus_base": rec.get("focus", 0) or 0,
                "category": cat,
                "bonus_city": craft.unified_bonus_city(item, cat),
                "rrr_by_city": {
                    c: {"nf": round(craft.unified_rrr(item, cat, c, False), 4),
                        "f": round(craft.unified_rrr(item, cat, c, True), 4)}
                    for c in cities},
            })
            node["sector"] = _sector(item, True)
            nodes[item] = node
            for inp in rec["inputs"]:
                visit(inp["id"], depth + 1)
        else:
            node.update({"is_raw": True, "recipe": None})
            node["sector"] = _sector(item, False)
            nodes[item] = node

    for r in roots:
        visit(r, 0)
    for r in roots:
        sell = _best_sell(price_of, r, cities, sell_mode, premium)
        if r in nodes:
            nodes[r]["sell_city"] = sell[0] if sell else None
            nodes[r]["sell_gross"] = round(sell[2]) if sell else None
            nodes[r]["sell_net"] = round(sell[1]) if sell else None
    return {
        "roots": list(roots),
        "nodes": nodes,
        "cities": cities,
        "premium": premium,
        "sell_mode": sell_mode,
        "fees": {"tax_premium": config.SALES_TAX_PREMIUM,
                 "tax_no_premium": config.SALES_TAX_NO_PREMIUM,
                 "setup": config.SETUP_FEE},
    }


# --------------------------------------------------------------- propagação
def _node_city(nodes, state, item):
    n = nodes[item]
    return ((state.get(item) or {}).get("city")
            or n.get("bonus_city") or n.get("buy_city")
            or (n.get("rrr_by_city") and next(iter(n["rrr_by_city"])))
            or "Caerleon")


def _rrr(nodes, state, item):
    n = nodes[item]
    if n.get("is_raw") or not n.get("recipe"):
        return 0.0
    c = _node_city(nodes, state, item)
    band = n["rrr_by_city"].get(c) or {}
    return band.get("f" if (state.get(item) or {}).get("focus") else "nf", 0.0)


def _is_buy(nodes, state, item):
    n = nodes[item]
    if n.get("is_raw") or not n.get("recipe"):
        return True
    return (state.get(item) or {}).get("mode") == "buy"


def _buy_price(nodes, state, item):
    n = nodes[item]
    c = _node_city(nodes, state, item)
    return (n.get("buy_by_city") or {}).get(c) or n.get("buy_price")


def solve(graph, targets, *, state=None, spec_fce=0, station_fee=0.0,
          focus_price=0.0):
    """Propaga a quantidade do(s) alvo(s) pela cadeia e calcula custo/lucro.

    targets: {item_id: qtd_final}. state: {item: {mode:'make'|'buy', focus:bool,
    city:str}}. spec_fce: Focus Cost Efficiency (barateia foco; não muda RRR).
    station_fee: prata por craft. focus_price: prata por ponto de foco (0 = só
    reporta pontos). Espelha a propagação do cliente (arredonda crafts p/ cima).
    """
    nodes = graph["nodes"]
    state = state or {}
    demand = defaultdict(float)     # unidades de saída necessárias por item
    crafts = defaultdict(int)       # nº de batches (inteiro) por item

    # in-degree só conta arestas de nós que serão FABRICADOS (buy não expande)
    indeg = defaultdict(int)
    for item, n in nodes.items():
        if _is_buy(nodes, state, item):
            continue
        for inp in n["recipe"]:
            indeg[inp["id"]] += 1
    for item, q in targets.items():
        demand[item] += q
    ready = deque(it for it in nodes if indeg[it] == 0)
    processed = set()
    while ready:
        item = ready.popleft()
        if item in processed:
            continue
        processed.add(item)
        n = nodes[item]
        if not _is_buy(nodes, state, item) and demand[item] > 0:
            out = n["output"] or 1
            batches = math.ceil(demand[item] / out)
            crafts[item] = batches
            rrr = _rrr(nodes, state, item)
            for inp in n["recipe"]:
                demand[inp["id"]] += batches * inp["count"] * (1 - rrr)
        for inp in (n.get("recipe") or []):
            if not _is_buy(nodes, state, item):
                indeg[inp["id"]] -= 1
                if indeg[inp["id"]] == 0:
                    ready.append(inp["id"])

    # custos
    shopping = {}
    focus_points = 0.0
    station_total = 0.0
    for item in nodes:
        q = demand[item]
        if q <= 0:
            continue
        if _is_buy(nodes, state, item):
            qty = math.ceil(q)
            unit = _buy_price(nodes, state, item)
            shopping[item] = {
                "qty": qty,
                "unit": unit,
                "cost": qty * unit if unit else None,
                "city": _node_city(nodes, state, item) if not nodes[item].get("is_raw")
                        else nodes[item].get("buy_city"),
                "priced": unit is not None,
            }
        else:
            b = crafts[item]
            station_total += b * station_fee
            if (state.get(item) or {}).get("focus"):
                focus_points += b * craft.focus_cost(
                    nodes[item]["focus_base"], spec_fce)

    buy_cost = sum(s["cost"] for s in shopping.values() if s["cost"])
    missing = sorted(it for it, s in shopping.items() if not s["priced"])
    focus_silver = focus_points * focus_price
    revenue = 0.0
    missing_sell = []          # produtos finais SEM cotação de venda (receita 0)
    for item, qty in targets.items():
        net = (nodes.get(item) or {}).get("sell_net")
        if net:
            revenue += net * qty
        else:
            missing_sell.append(item)
    total_target = sum(targets.values())
    profit = revenue - buy_cost - station_total - focus_silver

    out_nodes = {}
    for item in nodes:
        q = demand[item]
        if q <= 0:
            continue
        out_nodes[item] = {
            "demand": round(q, 2),
            "buy_qty": math.ceil(q),
            "crafts": crafts[item],
            "leaf": _is_buy(nodes, state, item),
            "city": _node_city(nodes, state, item),
            "rrr": round(_rrr(nodes, state, item), 4) if not _is_buy(nodes, state, item) else None,
        }
    return {
        "nodes": out_nodes,
        "shopping": shopping,
        "buy_cost": round(buy_cost),
        "station_cost": round(station_total),
        "focus_points": round(focus_points),
        "focus_cost": round(focus_silver),
        "revenue": round(revenue),
        "profit": round(profit),
        "profit_per_unit": round(profit / total_target) if total_target else 0,
        "roi_pct": round(100 * profit / buy_cost, 1) if buy_cost else None,
        "missing_prices": missing,
        "missing_sell": missing_sell,
    }


def make_or_buy(graph, *, state=None, spec_fce=0, station_fee=0.0,
                focus_price=0.0):
    """Veredito intrínseco fabricar-vs-comprar por nó (custo unitário mínimo).

    Independe do alvo: para cada item craftável compara o custo de FABRICAR uma
    unidade (insumos pela via mais barata + estação + foco) com o preço de
    COMPRA. Memoizado sobre o DAG (sem ciclos: tiers decrescem)."""
    nodes = graph["nodes"]
    state = state or {}
    memo = {}
    verdict = {}

    def unit_cost(item):
        if item in memo:
            return memo[item]
        memo[item] = None   # quebra ciclo eventual
        n = nodes[item]
        buy = _buy_price(nodes, state, item)
        if n.get("is_raw") or not n.get("recipe"):
            memo[item] = buy
            return buy
        out = n["output"] or 1
        rrr = _rrr(nodes, state, item)
        inp_cost, ok = 0.0, True
        for inp in n["recipe"]:
            uc = unit_cost(inp["id"])
            if uc is None:
                ok = False
                break
            inp_cost += uc * inp["count"]
        make = None
        if ok:
            foc = (craft.focus_cost(n["focus_base"], spec_fce) * focus_price
                   if (state.get(item) or {}).get("focus") else 0.0)
            make = (inp_cost * (1 - rrr) + station_fee + foc) / out
        options = [x for x in (buy, make) if x is not None]
        best = min(options) if options else None
        if not n.get("is_raw") and n.get("recipe"):
            verdict[item] = {
                "make_unit": round(make) if make is not None else None,
                "buy_unit": round(buy) if buy is not None else None,
                "verdict": ("make" if make is not None and (buy is None or make < buy)
                            else "buy" if buy is not None else None),
                "savings": (round(abs(buy - make))
                            if make is not None and buy is not None else None),
            }
        memo[item] = best
        return best

    for item in nodes:
        unit_cost(item)
    return verdict


# ------------------------------------------------------- salvar por conta
_CHAINS_SQLITE = """
CREATE TABLE IF NOT EXISTS production_chains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_production_chains_owner
  ON production_chains (owner_user_id);
"""
_CHAINS_PG = """
CREATE TABLE IF NOT EXISTS production_chains (
  id SERIAL PRIMARY KEY,
  owner_user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  UNIQUE(owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_production_chains_owner
  ON production_chains (owner_user_id);
"""

MAX_PAYLOAD = 400_000   # ~400 KB por cadeia salva (generoso p/ grafos grandes)


class ChainStore:
    """CRUD de cadeias de produção salvas POR CONTA (dual SQLite/Postgres).

    Espelha o padrão do AuthManager: cria o próprio schema (idempotente), _tx
    para escrita (commit/rollback) e _read que encerra a transação no Postgres
    (evita 'idle in transaction' no pooler). Toda operação é escopada pelo
    owner_user_id — um dono nunca enxerga/edita a cadeia de outro.
    """

    def __init__(self, con, lock):
        self.con = con
        self.lock = lock
        with self._tx() as c:
            if getattr(c, "backend", "sqlite") == "sqlite":
                c.executescript(_CHAINS_SQLITE)
            else:
                for stmt in _CHAINS_PG.split(";"):
                    if stmt.strip():
                        c.execute(stmt)

    @contextmanager
    def _tx(self):
        with self.lock:
            try:
                yield self.con
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise

    @contextmanager
    def _read(self):
        with self.lock:
            try:
                yield self.con
            finally:
                try:
                    self.con.rollback()
                except Exception:
                    pass

    @staticmethod
    def _insert_id(con, sql, params):
        if getattr(con, "backend", "sqlite") == "sqlite":
            return con.execute(sql, params).lastrowid
        return con.execute(sql + " RETURNING id", params).fetchone()[0]

    def list(self, owner):
        with self._read() as con:
            rows = con.execute(
                "SELECT id,name,updated_at FROM production_chains "
                "WHERE owner_user_id=? ORDER BY updated_at DESC",
                [owner]).fetchall()
        return [{"id": r[0], "name": r[1], "updated_at": r[2]} for r in rows]

    def get(self, owner, cid):
        with self._read() as con:
            row = con.execute(
                "SELECT id,name,payload,updated_at FROM production_chains "
                "WHERE id=? AND owner_user_id=?", [cid, owner]).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[2])
        except (ValueError, TypeError):
            payload = {}
        return {"id": row[0], "name": row[1], "payload": payload,
                "updated_at": row[3]}

    def save(self, owner, name, payload, cid=None):
        """Cria ou atualiza. Sem cid: upsert por (owner,name). Devolve o id."""
        name = (name or "").strip()[:80] or "Sem nome"
        body = json.dumps(payload, ensure_ascii=False)
        if len(body) > MAX_PAYLOAD:
            raise ValueError("cadeia grande demais para salvar")
        now = time.time()
        with self._tx() as con:
            if cid is not None:
                cur = con.execute(
                    "UPDATE production_chains SET name=?,payload=?,updated_at=? "
                    "WHERE id=? AND owner_user_id=?",
                    [name, body, now, cid, owner])
                if not cur.rowcount:
                    raise KeyError("cadeia não encontrada")
                return cid
            existing = con.execute(
                "SELECT id FROM production_chains "
                "WHERE owner_user_id=? AND name=?", [owner, name]).fetchone()
            if existing:
                con.execute(
                    "UPDATE production_chains SET payload=?,updated_at=? "
                    "WHERE id=?", [body, now, existing[0]])
                return existing[0]
            return self._insert_id(con,
                "INSERT INTO production_chains "
                "(owner_user_id,name,payload,updated_at) VALUES (?,?,?,?)",
                [owner, name, body, now])

    def delete(self, owner, cid):
        with self._tx() as con:
            cur = con.execute(
                "DELETE FROM production_chains WHERE id=? AND owner_user_id=?",
                [cid, owner])
            return bool(cur.rowcount)

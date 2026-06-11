# -*- coding: utf-8 -*-
"""Backtest de validação de sinal sobre os snapshots próprios.

Pergunta respondida: "o lucro que o app PROMETEU numa coleta se realizou na
coleta seguinte?" Para cada par de rodadas consecutivas de coleta (R1, R2):

  1. reconstrói os preços como estavam em R1 (apenas dados <= R1);
  2. calcula os flips instantâneos que o app teria recomendado em R1;
  3. mede o lucro REALIZADO usando o preço do lado de venda em R2
     (compra executada em R1, venda na chegada ~R2);
  4. compara esperado × realizado por faixa de ROI, rota e frescor do dado.

É um backtest de sinal (não simula fila, quantidade nem slippage de
profundidade) — valida se o ranking do app aponta na direção certa e mede o
"fator de captura" real entre coletas. Os vieses conhecidos estão descritos
na AUDITORIA.md (fase 4).
"""
from .flips import BLACK_MARKET, buy_cost, compute_flips, sell_revenue

MIN_GAP_MIN = 10
MAX_GAP_MIN = 12 * 60

ROI_BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 50), (50, None)]
AGE_BUCKETS = [(0, 30), (30, 120), (120, 480), (480, None)]

PRICE_COLS = ("item_id, city, quality, sell_price_min, sell_price_min_date,"
              " sell_price_max, sell_price_max_date, buy_price_min,"
              " buy_price_min_date, buy_price_max, buy_price_max_date,"
              " fetched_at")


def _runs(con, server, max_runs):
    rows = con.execute(
        "SELECT started_at, finished_at FROM collection_runs"
        " WHERE server=? AND ok=1 AND items > 0"
        " ORDER BY started_at DESC LIMIT ?", [server, max_runs]).fetchall()
    return sorted(rows)


def _snapshot_prices(con, server, started, finished):
    """Última linha de snapshot por (item, cidade, qualidade) na janela da rodada."""
    rows = con.execute(
        f"""SELECT {PRICE_COLS} FROM price_snapshots
            WHERE server=? AND fetched_at BETWEEN ? AND ?
            ORDER BY fetched_at""",
        [server, started - 60, finished + 60]).fetchall()
    cols = [c.strip() for c in PRICE_COLS.split(",")]
    out = {}
    for r in rows:
        d = dict(zip(cols, r))
        out[(d["item_id"], d["city"], d["quality"])] = d
    return out


def _bucket(value, buckets):
    for lo, hi in buckets:
        if value >= lo and (hi is None or value < hi):
            return f"{lo}-{hi}" if hi is not None else f"{lo}+"
    return None


def _agg_new():
    return {"n": 0, "hits": 0, "expected_sum": 0.0, "realized_sum": 0.0}


def _agg_add(agg, expected, realized):
    agg["n"] += 1
    if realized > 0:
        agg["hits"] += 1
    agg["expected_sum"] += expected
    agg["realized_sum"] += realized


def _agg_out(agg):
    n = agg["n"]
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "hit_rate_pct": round(100 * agg["hits"] / n, 1),
        "expected_profit_avg": round(agg["expected_sum"] / n, 1),
        "realized_profit_avg": round(agg["realized_sum"] / n, 1),
        "capture_pct": round(100 * agg["realized_sum"] / agg["expected_sum"], 1)
        if agg["expected_sum"] > 0 else None,
    }


def signal_backtest(con, server, items_meta, premium=True, min_profit=500,
                    max_runs=60, top_per_run=300):
    """Roda o backtest de sinal sobre as últimas `max_runs` rodadas de coleta.

    items_meta: dict item_id -> metadados do ItemDB (p/ regras do Mercado Negro).
    """
    runs = _runs(con, server, max_runs)
    overall = _agg_new()
    by_roi = {}
    by_route = {"Mercado Negro": _agg_new(), "entre cidades": _agg_new()}
    by_age = {}
    pairs_used = 0
    no_counterparty = 0

    for (s1, f1), (s2, f2) in zip(runs, runs[1:]):
        gap_min = (s2 - f1) / 60
        if not MIN_GAP_MIN <= gap_min <= MAX_GAP_MIN:
            continue
        p1 = _snapshot_prices(con, server, s1, f1)
        p2 = _snapshot_prices(con, server, s2, f2)
        if not p1 or not p2:
            continue
        pairs_used += 1
        opps = compute_flips(list(p1.values()), items_meta, premium=premium,
                             buy_mode="instant", sell_mode="instant",
                             min_profit=min_profit)[:top_per_run]
        for o in opps:
            later = p2.get((o["item_id"], o["sell_city"], o["quality"]))
            if later is None:
                continue
            realized_sell = later["buy_price_max"] or 0
            if realized_sell <= 0:
                # a contraparte sumiu por completo: não dá para medir o preço
                # de saída — conta à parte, fora das médias de lucro
                no_counterparty += 1
                continue
            realized_profit = (sell_revenue(realized_sell, "instant", premium)
                               - buy_cost(o["buy_price"], "instant"))
            expected = o["profit"]
            _agg_add(overall, expected, realized_profit)
            rb = _bucket(o["roi_pct"], ROI_BUCKETS)
            if rb:
                _agg_add(by_roi.setdefault(rb, _agg_new()), expected,
                         realized_profit)
            route = ("Mercado Negro" if o["sell_city"] == BLACK_MARKET
                     else "entre cidades")
            _agg_add(by_route[route], expected, realized_profit)
            age = o.get("sell_age_min")
            if age is not None:
                ab = _bucket(age, AGE_BUCKETS)
                if ab:
                    _agg_add(by_age.setdefault(ab, _agg_new()), expected,
                             realized_profit)

    return {
        "runs_total": len(runs),
        "run_pairs_used": pairs_used,
        "no_counterparty": no_counterparty,
        "premium": premium,
        "min_profit": min_profit,
        "overall": _agg_out(overall),
        "by_expected_roi": {k: _agg_out(v) for k, v in sorted(
            by_roi.items(), key=lambda kv: float(kv[0].split("-")[0].rstrip("+")))},
        "by_route": {k: _agg_out(v) for k, v in by_route.items()},
        "by_sell_age_min": {k: _agg_out(v) for k, v in sorted(
            by_age.items(), key=lambda kv: float(kv[0].split("-")[0].rstrip("+")))},
        "caveats": [
            "Backtest de sinal: nao simula quantidade, fila nem profundidade.",
            "Venda medida no preco do lado de venda na coleta seguinte;",
            "contraparte que sumiu fica fora das medias (no_counterparty).",
        ],
    }

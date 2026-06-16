# -*- coding: utf-8 -*-
"""Persistência de ordens a partir dos snapshots próprios (price_snapshots).

Mede, para pares de coletas consecutivas do mesmo (item, cidade, qualidade),
se a MESMA ordem do topo do livro (mesmo preço + mesma data de criação)
continuava lá na coleta seguinte. Agrupado pela idade da ordem na primeira
observação, isso estima a probabilidade de "flip fantasma": quanto mais velha
a ordem, menor a chance de ela ainda existir quando você chegar à cidade.

Honestidade metodológica: isto é persistência ENTRE COLETAS (intervalo
variável, sem distinguir consumo de reprecificação) — um precursor simples da
análise de sobrevivência formal (Kaplan-Meier), suficiente para calibrar as
faixas de idade da UI conforme os dados próprios acumulam.
"""
from datetime import datetime, timezone

# (idade mínima, idade máxima) em minutos; None = sem teto
AGE_BUCKETS = [(0, 30), (30, 60), (60, 120), (120, 240),
               (240, 480), (480, 1440), (1440, None)]

MIN_GAP_MIN = 10        # pares mais próximos que isso não informam nada
MAX_GAP_MIN = 12 * 60   # pares muito distantes não são comparáveis

SIDES = {
    "sell": ("sell_price_min", "sell_price_min_date"),
    "buy": ("buy_price_max", "buy_price_max_date"),
}


def _epoch(date_str):
    if not date_str or date_str.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(date_str[:19]).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _bucket_label(lo, hi):
    return f"{lo}-{hi} min" if hi is not None else f"{lo}+ min"


def persistence(con, server, item_id=None, city=None, quality=None, days=3):
    """Calcula a persistência por faixa de idade, para os dois lados.

    con: conexão sqlite (somente leitura) com a tabela price_snapshots.
    days: janela de tempo (padrão 7) — sem isto a consulta varria a tabela
    inteira (milhões de linhas, ~20 s e ~2 GB de RAM); a persistência só
    precisa dos pares recentes. Usa o índice (server, fetched_at).
    Retorna {"sides": {sell: {...}, buy: {...}}, "snapshot_pairs": N}.
    """
    import time as _time
    where = ["server=?"]
    params = [server]
    if days:
        where.append("fetched_at >= ?")
        params.append(_time.time() - days * 86400)
    if item_id:
        where.append("item_id=?")
        params.append(item_id)
    if city:
        where.append("city=?")
        params.append(city)
    if quality:
        where.append("quality=?")
        params.append(quality)
    rows = con.execute(
        f"""SELECT item_id, city, quality, fetched_at,
                   sell_price_min, sell_price_min_date,
                   buy_price_max, buy_price_max_date
            FROM price_snapshots
            WHERE {' AND '.join(where)}
            ORDER BY item_id, city, quality, fetched_at""",
        params).fetchall()

    counters = {side: [{"pairs": 0, "survived": 0, "gaps": []}
                       for _ in AGE_BUCKETS] for side in SIDES}
    total_pairs = 0
    prev = None
    for r in rows:
        (item, cty, q, fetched, sell_p, sell_d, buy_p, buy_d) = r
        cur = {"key": (item, cty, q), "fetched": fetched,
               "sell": (sell_p, sell_d), "buy": (buy_p, buy_d)}
        if prev and prev["key"] == cur["key"]:
            gap_min = (cur["fetched"] - prev["fetched"]) / 60
            if MIN_GAP_MIN <= gap_min <= MAX_GAP_MIN:
                total_pairs += 1
                for side, (pf, df) in SIDES.items():
                    p1, d1 = prev[side]
                    p2, d2 = cur[side]
                    d1_epoch = _epoch(d1)
                    if not p1 or p1 <= 0 or d1_epoch is None:
                        continue
                    age_min = max(0, (prev["fetched"] - d1_epoch) / 60)
                    for i, (lo, hi) in enumerate(AGE_BUCKETS):
                        if age_min >= lo and (hi is None or age_min < hi):
                            c = counters[side][i]
                            c["pairs"] += 1
                            c["gaps"].append(gap_min)
                            if p2 == p1 and d2 == d1:
                                c["survived"] += 1
                            break
        prev = cur

    out = {}
    for side, buckets in counters.items():
        rows_out = []
        # E1: curva de sobrevivência estilo Kaplan-Meier — S(idade) acumula os
        # hazards por faixa: probabilidade de a ordem ainda existir tendo
        # alcançado aquela idade = produto das taxas de sobrevivência por
        # coleta até ali. (rate_pct é P(sobrevive 1 coleta | idade na faixa).)
        km = 1.0
        for (lo, hi), c in zip(AGE_BUCKETS, buckets):
            gaps = sorted(c["gaps"])
            rate = (c["survived"] / c["pairs"]) if c["pairs"] else None
            if rate is not None:
                km *= rate
            rows_out.append({
                "age_label": _bucket_label(lo, hi),
                "age_min": lo,
                "pairs": c["pairs"],
                "survived": c["survived"],
                "rate_pct": round(100 * rate, 1) if rate is not None else None,
                "km_survival_pct": round(100 * km, 1) if c["pairs"] else None,
                "median_gap_min": round(gaps[len(gaps) // 2], 1) if gaps else None,
            })
        out[side] = {"buckets": rows_out,
                     "pairs": sum(b["pairs"] for b in rows_out)}
    return {"sides": out, "snapshot_pairs": total_pairs}

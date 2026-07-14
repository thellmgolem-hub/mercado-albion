# -*- coding: utf-8 -*-
"""Microestrutura de mercado: market-making intra-cidade, livro e armadilhas.

Tudo opera sobre o cache local (somente leitura): a tabela `prices` é o livro
ATUAL (o último snapshot por item×cidade×qualidade) e `price_snapshots` é a
série de ~30 em 30 min dos últimos dias. Nenhuma destas análises existia: os
motores de flip/sell/craft sempre cruzam cidades ou usam preço instantâneo;
aqui o ganho é postar ordem de COMPRA e de VENDA no MESMO mercado e embolsar o
spread, descontadas as taxas (imposto de venda + 2,5% de anúncio nas DUAS
pontas, porque você cria as duas ordens).

Honestidade: a maioria dos itens líquidos tem spread líquido fino (imposto +
anúncio comem ~9% do round-trip), então o valor vem do GIRO, não da margem
unitária; e o risco de fila (a ordem ser consumida/reprecificada antes de
encher) é real — por isso o giro entra como multiplicador e o módulo de
persistência (survival) mede o tempo de fila em separado.
"""
import time

from . import config
from . import store

SALES = {True: config.SALES_TAX_PREMIUM, False: config.SALES_TAX_NO_PREMIUM}


def _placeholders(n):
    return ",".join("?" * n)


def _daily_liquidity(con, server, pairs, days=7):
    """Mediana do volume diário (history, escala 24h, q1) por (item, cidade).

    pairs: iterável de (item_id, city). Retorna {(item, city): mediana_dia}.
    O volume da AODP é um piso censurado (só conta quando alguém abre o
    mercado com o cliente de coleta) — usar a mediana dos dias com dado é mais
    robusto que a média contra os zeros de dias não observados.
    """
    items = sorted({p[0] for p in pairs})
    cities = sorted({p[1] for p in pairs})
    if not items or not cities:
        return {}
    rows = con.execute(
        f"""SELECT item_id, city, substr(ts,1,10) AS dia, SUM(item_count) AS n
            FROM history
            WHERE server=? AND time_scale=24 AND quality=1 AND item_count>0
              AND ts >= ?
              AND item_id IN ({_placeholders(len(items))})
              AND city IN ({_placeholders(len(cities))})
            GROUP BY item_id, city, dia""",
        [server, store.cutoff_iso(days), *items, *cities]).fetchall()
    by_pair = {}
    for item, city, _dia, n in rows:
        by_pair.setdefault((item, city), []).append(n)
    out = {}
    for k, vals in by_pair.items():
        vals.sort()
        # SUM(item_count) volta Decimal no Postgres; float() aqui evita o
        # Decimal*float la em market_making_menu/advisor (raiz do 'liquidity_day').
        out[k] = float(vals[len(vals) // 2])  # mediana
    return out


def _current_book(con, server, cities=None, item_ids=None, max_age_min=None,
                  exclude_bm=False):
    """Linhas do livro atual (tabela prices) com ambas as pontas vivas."""
    where = ["server=?", "sell_price_min>0", "buy_price_max>0"]
    params = [server]
    if exclude_bm:
        where.append("city<>?")
        params.append("Black Market")
    if cities:
        where.append(f"city IN ({_placeholders(len(cities))})")
        params += list(cities)
    if item_ids:
        where.append(f"item_id IN ({_placeholders(len(item_ids))})")
        params += list(item_ids)
    if max_age_min is not None:
        where.append("fetched_at >= ?")
        params.append(time.time() - max_age_min * 60)
    return con.execute(
        f"""SELECT item_id, city, quality, sell_price_min, sell_price_max,
                   buy_price_min, buy_price_max, fetched_at
            FROM prices WHERE {' AND '.join(where)}""", params).fetchall()


def _outlier_keys(rows, z_threshold=3.5):
    """(item,city,q) cujo sell_min OU buy_max é outlier entre cidades (z robusto).

    Pega as ordens-âncora/isca (preço absurdo de uma ponta) que inflam spreads
    falsos — não servem para market-making porque a contraparte real está
    noutro nível. Só avalia itens com >=3 cidades cotadas.
    """
    by_iq = {}
    for r in rows:
        item, city, q, smin = r[0], r[1], r[2], r[3]
        bmax = r[6]
        by_iq.setdefault((item, q), []).append((city, smin, bmax))
    bad = set()
    for (item, q), recs in by_iq.items():
        if len(recs) < 3:
            continue
        for idx, field in ((1, "sell"), (2, "buy")):
            zs = _robust_z([rec[idx] for rec in recs])
            for rec, z in zip(recs, zs):
                if abs(z) > z_threshold:
                    bad.add((item, rec[0], q))
    return bad


def market_making_menu(con, server, premium=True, max_age_min=720,
                       cities=None, item_ids=None, qualities=None,
                       hist_days=7, min_net=1, min_liquidity=0,
                       max_net_pct=80, include_suspect=False, limit=60):
    """Cardápio de market-making intra-cidade.

    Para cada (item, cidade, qualidade) com livro vivo: posta ordem de compra
    logo acima do bid e ordem de venda logo abaixo do ask e captura
        net = sell_price_min*(1 - imposto - anúncio) - buy_price_max*(1 + anúncio)
    Ordena por net × liquidez (a margem fina só compensa com giro).

    Por padrão exclui o Mercado Negro (não há ordens de venda do jogador lá),
    descarta livros cruzados e preços-âncora (outliers entre cidades) e corta
    spreads líquidos acima de max_net_pct — um net% gigante quase sempre é uma
    ponta falsa, não uma oportunidade de MM (que vive de margem fina + giro).
    """
    t = SALES[bool(premium)]
    f = config.SETUP_FEE
    rows = _current_book(con, server, cities, item_ids, max_age_min,
                         exclude_bm=True)
    bad = set() if include_suspect else _outlier_keys(rows)
    cand = []
    for (item, city, q, smin, smax, bmin, bmax, fetched) in rows:
        if qualities and q not in qualities:
            continue
        if not include_suspect and (item, city, q) in bad:
            continue
        if smin <= bmax:                       # livro cruzado: impossível
            continue
        net = smin * (1 - t - f) - bmax * (1 + f)
        if net < min_net:
            continue
        net_pct = 100 * net / (bmax * (1 + f))
        if not include_suspect and net_pct > max_net_pct:
            continue
        cand.append({
            "item_id": item, "city": city, "quality": q,
            "sell_price_min": smin, "buy_price_max": bmax,
            "spread_gross": smin - bmax,
            "net_per_unit": round(net, 1),
            "net_pct": round(net_pct, 2),
            "age_min": int(max(0, (time.time() - fetched) / 60)),
        })
    liq = _daily_liquidity(con, server,
                           {(c["item_id"], c["city"]) for c in cand}, hist_days)
    out = []
    for c in cand:
        c["liquidity_day"] = liq.get((c["item_id"], c["city"]), 0)
        if c["liquidity_day"] < min_liquidity:
            continue
        # potencial/dia realista = net unitário × giro capturável (CAPTURE_RATE)
        c["potential_day"] = round(
            c["net_per_unit"] * c["liquidity_day"] * config.CAPTURE_RATE, 0)
        out.append(c)
    out.sort(key=lambda r: -(r["potential_day"] or 0))
    return out[:limit] if limit else out


def book_metrics(con, server, cities=None, item_ids=None, qualities=None,
                 max_age_min=720, limit=60):
    """Largura/dispersão do topo do livro (PROXY — a AODP não expõe quantidades).

    largura = (max-min)/min de cada lado. Quanto mais larga, mais 'casca fina'
    o topo: o segundo nível está longe, então o preço anda fácil se você empurra
    volume. Não é profundidade real (ordersize), e sim dispersão dos preços de
    topo coletados.
    """
    rows = _current_book(con, server, cities, item_ids, max_age_min)
    out = []
    for (item, city, q, smin, smax, bmin, bmax, fetched) in rows:
        if qualities and q not in qualities:
            continue
        sell_w = (smax - smin) / smin if smin > 0 and smax >= smin else None
        buy_w = (bmax - bmin) / bmax if bmax > 0 and bmax >= bmin else None
        out.append({
            "item_id": item, "city": city, "quality": q,
            "sell_min": smin, "sell_max": smax, "buy_min": bmin, "buy_max": bmax,
            "sell_width_pct": round(100 * sell_w, 1) if sell_w is not None else None,
            "buy_width_pct": round(100 * buy_w, 1) if buy_w is not None else None,
            "age_min": int(max(0, (time.time() - fetched) / 60)),
        })
    out.sort(key=lambda r: -(r["sell_width_pct"] or 0))
    return out[:limit] if limit else out


def clean_price_rows(rows, z_threshold=4.0):
    """Zera preços-âncora (outliers entre cidades) em linhas no formato da API.

    Para cada (item, qualidade), calcula o z robusto (mediana+MAD) de
    sell_price_min e buy_price_max entre as cidades e ZERA a ponta que for
    outlier (alta OU baixa) — uma ordem de 1 prata ou de 751 milhões numa única
    cidade deixa de contaminar qualquer análise que pegue o melhor/maior. Só
    atua com >=3 cidades cotadas naquela ponta. O Mercado Negro fica FORA do
    corte transversal: as ordens lá são do sistema (não são isca) e o prêmio
    sobre as cidades royal é estrutural — zerá-lo censuraria justamente os
    maiores prêmios de BM. Devolve cópias das linhas.
    """
    out = [dict(r) for r in rows]
    by_iq = {}
    for idx, r in enumerate(out):
        by_iq.setdefault((r["item_id"], r["quality"]), []).append(idx)
    for idxs in by_iq.values():
        for field in ("sell_price_min", "buy_price_max"):
            vals = [(i, out[i].get(field) or 0) for i in idxs
                    if out[i].get("city") != "Black Market"
                    and (out[i].get(field) or 0) > 0]
            if len(vals) < 3:
                continue
            zs = _robust_z([v for _, v in vals])
            for (i, _), z in zip(vals, zs):
                if abs(z) > z_threshold:
                    out[i][field] = 0
                    out[i][field + "_date"] = "0001-01-01T00:00:00"
    return out


def _robust_z(values):
    """z robusto (mediana + MAD) por elemento; lista alinhada à entrada."""
    xs = sorted(values)
    n = len(xs)
    if n < 3:
        return [0.0] * len(values)
    med = xs[n // 2]
    dev = sorted(abs(v - med) for v in values)
    mad = dev[n // 2] or 1e-9
    return [0.6745 * (v - med) / mad for v in values]


def trap_signals(con, server, cities=None, item_ids=None, qualities=None,
                 max_age_min=720, z_threshold=3.5, check_ghost=True, limit=80):
    """Sinais de ordem-armadilha sobre o livro atual.

    (1) CRUZADO: sell_price_min <= buy_price_max no mesmo (item,city,q) — livro
        impossível, dado podre ou ordem que já sumiu.
    (2) OUTLIER: z robusto do sell_price_min do item/qualidade contra as outras
        cidades no mesmo instante > limiar — preço fora da curva (isca/erro).
    (3) NOVO/FANTASMA: a ordem do topo NÃO estava na coleta anterior (apareceu
        agora) — não-confirmada, mais sujeita a evaporar; vs 'persistido', que
        já sobreviveu a um ciclo de coleta. Olha price_snapshots por-ordem.
    Camada de saneamento p/ flips/recommend não apontarem para preços que somem.
    """
    rows = _current_book(con, server, cities, item_ids, max_age_min=None)
    # agrupa por (item, q) para o corte transversal entre cidades
    by_item_q = {}
    flags = []
    for (item, city, q, smin, smax, bmin, bmax, fetched) in rows:
        if qualities and q not in qualities:
            continue
        age = int(max(0, (time.time() - fetched) / 60))
        rec = {"item_id": item, "city": city, "quality": q,
               "sell_min": smin, "buy_max": bmax, "age_min": age, "flags": []}
        if max_age_min is None or age <= max_age_min:
            if smin <= bmax:
                rec["flags"].append("cruzado")
        by_item_q.setdefault((item, q), []).append(rec)
        flags.append(rec)
    for (item, q), recs in by_item_q.items():
        sells = [r["sell_min"] for r in recs]
        if len(sells) >= 3:
            zs = _robust_z(sells)
            for r, z in zip(recs, zs):
                r["sell_z"] = round(z, 2)
                if abs(z) > z_threshold:
                    r["flags"].append("outlier")
    out = [r for r in flags if r["flags"]]
    out.sort(key=lambda r: (0 if "cruzado" in r["flags"] else 1,
                            -abs(r.get("sell_z") or 0)))
    out = out[:limit] if limit else out
    if check_ghost:
        # 3º sinal por-ordem: a ordem do topo (sell) persistiu da coleta
        # anterior? Só p/ os já flagrados (poucos) — consulta indexada.
        # price_snapshots é legado local: ausente no piloto Postgres, então o
        # sinal de persistência degrada para "?" em vez de quebrar.
        for r in out:
            try:
                snaps = con.execute(
                    """SELECT sell_price_min FROM price_snapshots
                       WHERE server=? AND item_id=? AND city=? AND quality=?
                       ORDER BY fetched_at DESC LIMIT 2""",
                    [server, r["item_id"], r["city"], r["quality"]]).fetchall()
            except Exception:
                r["persist"] = "?"
                continue
            if len(snaps) >= 2:
                persisted = snaps[0][0] == snaps[1][0] and snaps[0][0]
                r["persist"] = "persistido" if persisted else "novo"
                if not persisted:
                    r["flags"].append("novo")
            else:
                r["persist"] = "?"
    return out


def capital_allocation(con, server, capital=None, premium=True,
                       max_age_min=720, cities=None, hist_days=7,
                       min_liquidity=1, fill_rate=None, limit=40):
    """Alocação de capital escasso por VELOCIDADE (lucro/dia por prata investida).

    Sobre o cardápio de market-making: para cada oportunidade, o giro
    capturável/dia limita quanto capital faz sentido parar nela. Aloca
    gulosamente por rendimento/dia (ROI por round-trip × giro) até esgotar o
    capital. Reporta o rendimento marginal do primeiro item que ficou de fora.

    fill_rate (0..1): fração das ordens do topo que realmente enchem num ciclo,
    medida pelo módulo survival (persistência). Sem ela assume 1.0 (teto
    orientativo); com ela o giro capturável vira (liquidez × CAPTURE_RATE ×
    fill_rate) — quanto mais a ordem evapora antes de encher, menos giro real.
    """
    capital = capital or config.ORDER_MAX_CAPITAL
    f = config.SETUP_FEE
    fill = 1.0 if fill_rate is None else max(0.05, min(1.0, fill_rate))
    menu = market_making_menu(con, server, premium=premium,
                              max_age_min=max_age_min, cities=cities,
                              hist_days=hist_days, min_liquidity=min_liquidity,
                              limit=0)
    # rendimento diário por unidade de capital = (net/unidade) / (capital/unidade)
    for m in menu:
        cap_unit = m["buy_price_max"] * (1 + f)
        m["cap_unit"] = round(cap_unit, 1)
        m["units_day"] = round(m["liquidity_day"] * config.CAPTURE_RATE * fill, 1)
        m["yield_day_pct"] = round(100 * m["net_per_unit"] / cap_unit, 2) \
            if cap_unit > 0 else 0
    menu = [m for m in menu if m["units_day"] >= 0.5 and m["yield_day_pct"] > 0]
    menu.sort(key=lambda m: -m["yield_day_pct"])
    budget = capital
    plan = []
    marginal = None
    for m in menu:
        cap_need = m["cap_unit"] * m["units_day"]   # capital p/ girar 1 dia
        if budget <= 0:
            marginal = marginal or m["yield_day_pct"]
            break
        alloc = min(budget, cap_need)
        units = alloc / m["cap_unit"] if m["cap_unit"] > 0 else 0
        plan.append({
            **{k: m[k] for k in ("item_id", "city", "quality", "net_per_unit",
                                 "yield_day_pct", "liquidity_day")},
            "alloc_capital": round(alloc, 0),
            "units": round(units, 0),
            "profit_day": round(m["net_per_unit"] * units, 0),
        })
        budget -= alloc
        if limit and len(plan) >= limit:
            break
    return {
        "capital": capital,
        "capital_used": round(capital - budget, 0),
        "profit_day_total": round(sum(p["profit_day"] for p in plan), 0),
        "marginal_yield_pct": marginal,
        "plan": plan,
    }


def hourly_spread(con, server, item_id, city, quality=1, premium=True, days=7):
    """Spread líquido médio por hora UTC (price_snapshots).

    AVISO: só price_snapshots tem granularidade intradiária e a retenção é de
    ~7 dias; com a coleta não cobrindo as 24 h, o padrão observado pode ser de
    QUANDO o coletor roda, não do mercado. O nº de buckets por hora é devolvido
    para leitura honesta.
    """
    t = SALES[bool(premium)]
    f = config.SETUP_FEE
    # price_snapshots (série intradiária) é legado SQLite: o piloto Postgres não
    # acumula snapshots finos, então esta sub-visão sai vazia em vez de quebrar.
    if store.backend() != "sqlite":
        return []
    rows = con.execute(
        """SELECT strftime('%H', datetime(fetched_at,'unixepoch')) AS h,
                  sell_price_min, buy_price_max
           FROM price_snapshots
           WHERE server=? AND item_id=? AND city=? AND quality=?
             AND fetched_at >= ? AND sell_price_min>0 AND buy_price_max>0""",
        [server, item_id, city, quality, time.time() - days * 86400]).fetchall()
    by_h = {}
    for h, smin, bmax in rows:
        by_h.setdefault(h, []).append(smin * (1 - t - f) - bmax * (1 + f))
    out = []
    for h in sorted(by_h):
        vals = sorted(by_h[h])
        out.append({
            "hour_utc": h,
            "net_median": round(vals[len(vals) // 2], 1),
            "samples": len(vals),
        })
    return out

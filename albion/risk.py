# -*- coding: utf-8 -*-
"""Risco & portfólio: volatilidade, drawdown, VaR, sizing e correlação.

Transforma "quanto lucro" em "quanto posso perder". Opera sobre history
(avg_price diário, q1, escala 24h) — a série mais longa do cache (até ~6 meses).

- PERFIL DE RISCO: volatilidade anualizada, máximo drawdown, VaR 1-dia (5%),
  downside deviation e um selo absoluto seguro/médio/especulativo.
- SIZING: quanto comprar de cada oportunidade dado o capital, ajustando pelo
  risco (fração tipo Kelly) e pelo giro/persistência que o mercado absorve.
- CORRELAÇÃO: quais itens andam juntos (concentração) e quais protegem (hedge),
  sobre RETORNOS log diários — não níveis.
"""
import math

from . import config

# faixas absolutas de volatilidade anualizada -> selo (calibráveis)
VOL_BANDS = [(0.35, "seguro"), (0.75, "médio")]


def _log_returns(prices):
    out = []
    for a, b in zip(prices, prices[1:]):
        if a and b and a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _pstdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _percentile(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def risk_profile(prices, min_points=30):
    """Métricas de risco de uma série de preços diários (cronológica).

    Retorna None se a série for curta demais para ser informativa.
    """
    prices = [p for p in prices if p and p > 0]
    if len(prices) < min_points:
        return None
    r = _log_returns(prices)
    if len(r) < min_points - 1:
        return None
    vol_annual = _pstdev(r) * math.sqrt(365)
    # máximo drawdown sobre o nível de preço
    peak, max_dd = prices[0], 0.0
    for p in prices:
        peak = max(peak, p)
        max_dd = min(max_dd, p / peak - 1)
    var5 = _percentile(r, 5)               # retorno do 5º percentil (negativo)
    downside = _pstdev([x for x in r if x < 0])
    mean_r = sum(r) / len(r)
    sortino = (mean_r / downside * math.sqrt(365)) if downside else None
    label = "especulativo"
    for thr, lbl in VOL_BANDS:
        if vol_annual <= thr:
            label = lbl
            break
    return {
        "points": len(prices),
        "vol_annual_pct": round(vol_annual * 100, 1),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "var_1d_pct": round((var5 or 0) * 100, 1),
        "downside_dev_pct": round(downside * 100, 2),
        "sortino": round(sortino, 2) if sortino is not None else None,
        "risk_label": label,
    }


def position_size(profit_per_unit, buy_price, vol_annual, liquidity_day,
                  persistence=1.0, capital=None, kelly_cap=0.25):
    """Quanto comprar de uma oportunidade, ajustado a risco.

    Combina três tetos: (1) o giro que o mercado absorve
    (liquidity_day × CAPTURE_RATE × persistência da ordem); (2) o capital
    disponível via fração tipo Kelly (edge/risco, limitada por kelly_cap);
    (3) a própria liquidez. Devolve N sugerido e a fração de Kelly usada.
    """
    capital = capital or config.ORDER_MAX_CAPITAL
    if buy_price <= 0:
        return None
    vol_daily = (vol_annual / math.sqrt(365)) if vol_annual else 0
    edge = profit_per_unit / buy_price                      # retorno por unidade
    risk = vol_daily or 0.05                                # vol como proxy de risco
    kelly = max(0.0, min(kelly_cap, edge / risk)) if risk else 0
    n_liquidity = liquidity_day * config.CAPTURE_RATE * max(0.0, min(1.0, persistence))
    n_capital = capital * kelly / buy_price
    n = int(max(0, min(n_liquidity, n_capital)))
    return {
        "units": n,
        "kelly_frac": round(kelly, 3),
        "capital_used": round(n * buy_price),
        "profit_total": round(n * profit_per_unit),
        "limited_by": "liquidez" if n_liquidity <= n_capital else "capital",
    }


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx and sy else None


def correlation_pairs(series_by_item, min_common=60, limit=60):
    """Correlação de Pearson dos RETORNOS log diários entre itens.

    series_by_item: {item_id: {dia: preço}}. Para cada par com >= min_common
    dias em comum, alinha por dia, calcula retornos e a correlação. Devolve
    pares ordenados por |corr|, separando concentração (corr alta) de hedge
    (corr baixa/negativa).
    """
    items = list(series_by_item)
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = series_by_item[items[i]], series_by_item[items[j]]
            common = sorted(set(a) & set(b))
            if len(common) < min_common:
                continue
            pa = [a[d] for d in common]
            pb = [b[d] for d in common]
            ra, rb = _log_returns(pa), _log_returns(pb)
            if len(ra) != len(rb) or len(ra) < min_common - 1:
                continue
            c = _pearson(ra, rb)
            if c is None:
                continue
            out.append({"a": items[i], "b": items[j],
                        "corr": round(c, 3), "common_days": len(common)})
    out.sort(key=lambda r: -abs(r["corr"]))
    return out[:limit] if limit else out

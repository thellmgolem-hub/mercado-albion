# -*- coding: utf-8 -*-
"""Previsão & econometria: reversão à média, par trading, previsibilidade.

Reaproveita o núcleo de albion/stats.py (linear_fit, residuals, std — Python
puro, sem numpy). Opera sobre o history diário (avg_price, escala 24h).

HONESTIDADE: para alguns itens a série fina ainda é curta; cada função exige um
mínimo de pontos e devolve a confiança/meia-vida para o usuário pesar. O nowcast
intradiário ainda depende de cobertura horária que não temos; a detecção de
regime (quebra estrutural) está implementada em structural_break().
"""
import math
import random

from . import config, stats


def _ssr(values):
    """Soma dos quadrados dos resíduos da tendência linear."""
    fit = stats.linear_fit(values)
    if not fit:
        return 0.0
    return sum(r * r for r in stats.residuals(values, fit))


def structural_break(prices, min_seg=8, permutations=150, p_threshold=0.1,
                     seed=1234):
    """Detecta quebra de regime (estilo Chow) sobre log-preço diário.

    Varre o ponto t* que mais reduz a SSR ao dividir a série em dois ajustes
    lineares; a significância vem de um teste de permutação (embaralha a série N
    vezes e mede com que frequência o ganho máximo iguala/supera o observado —
    Python puro, sem scipy). Classifica a quebra como de NÍVEL, TENDÊNCIA ou
    VOLATILIDADE e devolve a janela válida (pós-quebra), que os demais sinais
    (reversão/VWAP/previsibilidade) deveriam usar para não se envenenar com
    dados de um regime morto.
    """
    prices = [p for p in prices if p and p > 0]
    n = len(prices)
    if n < 2 * min_seg + 1:
        return None
    logp = [math.log(p) for p in prices]

    def max_gain(ys):
        full = _ssr(ys)
        best, bt = 0.0, None
        for t in range(min_seg, len(ys) - min_seg):
            gain = full - (_ssr(ys[:t]) + _ssr(ys[t:]))
            if gain > best:
                best, bt = gain, t
        return best, bt

    obs_gain, t_star = max_gain(logp)
    if t_star is None or obs_gain <= 0:
        return None
    rng = random.Random(seed)
    ge = 0
    for _ in range(permutations):
        sh = logp[:]
        rng.shuffle(sh)
        if max_gain(sh)[0] >= obs_gain:
            ge += 1
    pval = (ge + 1) / (permutations + 1)

    left, right = prices[:t_star], prices[t_star:]
    ml, mr = sum(left) / len(left), sum(right) / len(right)
    fl, fr = stats.linear_fit([math.log(p) for p in left]), \
        stats.linear_fit([math.log(p) for p in right])
    slope_l = fl[1] if fl else 0
    slope_r = fr[1] if fr else 0
    sd_l = stats.std(left) or 1e-9
    sd_r = stats.std(right) or 1e-9
    # mudança normalizada em nível, tendência e dispersão -> a dominante
    chg = {
        "nível": abs(mr - ml) / (ml or 1),
        "tendência": abs(slope_r - slope_l) / (abs(slope_l) + 1e-6),
        "volatilidade": abs(sd_r - sd_l) / (sd_l or 1),
    }
    kind = max(chg, key=chg.get)
    return {
        "points": n,
        "break_index": t_star,
        "break_frac": round(t_star / n, 2),
        "p_value": round(pval, 3),
        "significant": pval <= p_threshold,
        "kind": kind,
        "mean_before": round(ml),
        "mean_after": round(mr),
        "level_change_pct": round(100 * (mr / ml - 1), 1) if ml else None,
        "valid_points": n - t_star,
    }


def _ar1(resid):
    """Coef. AR(1) b e meia-vida de reversão dos resíduos (ou (b, None))."""
    x, y = resid[:-1], resid[1:]
    if len(x) < 5:
        return None, None
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    den = sum((xi - mx) ** 2 for xi in x)
    if den == 0:
        return None, None
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / den
    if not 0 < b < 1:               # não reverte (random walk / explosivo)
        return b, None
    return b, -math.log(2) / math.log(b)


def mean_reversion(prices, min_points=20, max_halflife=7, z_gate=1.5):
    """Reversão à média acionável: meia-vida + banda + alvo, sobre resíduos.

    Destendência por log(preço); AR(1) dos resíduos dá a meia-vida; o alvo é o
    preço de equilíbrio (tendência no dia) e a banda é ±1 desvio dos resíduos.
    Só vira sinal se a meia-vida <= max_halflife E |z| >= z_gate.
    """
    prices = [p for p in prices if p and p > 0]
    if len(prices) < min_points:
        return None
    logp = [math.log(p) for p in prices]
    fit = stats.linear_fit(logp)
    if not fit:
        return None
    resid = stats.residuals(logp, fit)
    sd = stats.std(resid)
    if not sd:
        return None
    z = resid[-1] / sd
    b, hl = _ar1(resid)
    a, btrend = fit
    trend_now = a + btrend * (len(logp) - 1)
    target = math.exp(trend_now)
    # guarda contra ponto final anômalo (preço de 1 dia 5x fora da mediana é
    # dado podre, não reversão tradável)
    med = sorted(prices)[len(prices) // 2]
    sane = med > 0 and 0.2 <= prices[-1] / med <= 5
    signal = (sane and hl is not None and hl <= max_halflife
              and abs(z) >= z_gate)
    return {
        "points": len(prices),
        "current": round(prices[-1]),
        "target": round(target),
        "band_low": round(math.exp(trend_now - sd)),
        "band_high": round(math.exp(trend_now + sd)),
        "z_resid": round(z, 2),
        "halflife_days": round(hl, 1) if hl else None,
        "gap_pct": round(100 * (target / prices[-1] - 1), 1),
        "direction": "comprar" if z < 0 else "vender/esperar",
        "signal": bool(signal),
    }


def backtest_reversion(prices, min_history=25, z_gate=1.5, max_halflife=7):
    """Walk-forward do sinal de reversão: ele de fato pagou no passado?

    Para cada dia com histórico suficiente, recalcula mean_reversion SÓ com os
    dados até ali (sem espiar o futuro) e, quando o sinal dispara, mede o
    retorno realizado nos ~meia-vida dias seguintes, com o sinal da direção
    prevista. Devolve nº de sinais, taxa de acerto e edge médio — separa sinal
    de promessa não-auditada. (O(n²) por série; varredura limita as séries.)
    """
    prices = [p for p in prices if p and p > 0]
    n = len(prices)
    if n < min_history + 2:
        return None
    edges = []
    for t in range(min_history, n - 1):
        sig = mean_reversion(prices[:t + 1], min_points=min_history,
                             z_gate=z_gate, max_halflife=max_halflife)
        if not sig or not sig["signal"]:
            continue
        h = min(int(round(sig["halflife_days"] or 3)), n - 1 - t)
        if h < 1:
            continue
        fwd = prices[t + h] / prices[t] - 1            # retorno realizado
        edges.append(fwd if sig["direction"] == "comprar" else -fwd)
    if not edges:
        return None
    hits = sum(1 for e in edges if e > 0)
    edges_s = sorted(edges)
    return {
        "points": n,
        "n_signals": len(edges),
        "hit_rate_pct": round(100 * hits / len(edges), 1),
        "avg_edge_pct": round(100 * sum(edges) / len(edges), 2),
        "median_edge_pct": round(100 * edges_s[len(edges_s) // 2], 2),
    }


def predictability(prices, counts=None, min_points=20):
    """Score 0..1 de previsibilidade + rótulo modelável/cautela/ruído.

    Combina: autocorrelação lag-1 dos resíduos (alto=previsível), R² da
    tendência linear, e estabilidade de volume (1-CV). Porteiro para os demais
    sinais — calar onde a série é ruído.
    """
    prices = [p for p in prices if p and p > 0]
    if len(prices) < min_points:
        return None
    logp = [math.log(p) for p in prices]
    fit = stats.linear_fit(logp)
    resid = stats.residuals(logp, fit)
    # autocorrelação lag-1 dos resíduos
    x, y = resid[:-1], resid[1:]
    mx = sum(x) / len(x) if x else 0
    den = sum((xi - mx) ** 2 for xi in x) or 1e-9
    ac = sum((xi - mx) * (yi - mx) for xi, yi in zip(x, y)) / den
    ac01 = max(0.0, min(1.0, abs(ac)))
    # R² da tendência
    a, b = fit
    sst = sum((v - sum(logp) / len(logp)) ** 2 for v in logp) or 1e-9
    ssr = sum(r ** 2 for r in resid)
    r2 = max(0.0, 1 - ssr / sst)
    # estabilidade de volume
    vol_stab = 0.5
    if counts:
        cs = [c for c in counts if c and c > 0]
        if len(cs) >= 2:
            m = sum(cs) / len(cs)
            cv = (stats.std(cs) or 0) / m if m else 1
            vol_stab = max(0.0, min(1.0, 1 - cv))
    score = 0.45 * ac01 + 0.35 * r2 + 0.20 * vol_stab
    label = "modelável" if score >= 0.55 else ("cautela" if score >= 0.3 else "ruído")
    return {
        "points": len(prices),
        "autocorr_lag1": round(ac, 2),
        "trend_r2": round(r2, 2),
        "vol_stability": round(vol_stab, 2),
        "predictability": round(score, 2),
        "label": label,
    }


def _ols(xs, ys):
    """OLS y = a + b*x por fórmula manual -> (a, b)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return my - b * mx, b


def pair_trade(prices_a, prices_b, min_points=60, z_gate=2.0,
               fee_pct=None):
    """Par trading entre duas séries (mesmas datas alinhadas).

    spread = log(P_a) - hedge(log(P_b)); cointegração via meia-vida AR(1) do
    resíduo; sinal se |z_spread| >= z_gate, a meia-vida é finita/curta e o
    desvio bruto cobre as taxas com folga. Direção: vender a cara, comprar a
    barata.
    """
    if fee_pct is None:
        fee_pct = (config.SALES_TAX_NO_PREMIUM + 2 * config.SETUP_FEE) * 100
    a = [math.log(p) for p in prices_a]
    b = [math.log(p) for p in prices_b]
    if len(a) != len(b) or len(a) < min_points:
        return None
    ols = _ols(b, a)
    if not ols:
        return None
    alpha, beta = ols
    spread = [ai - (alpha + beta * bi) for ai, bi in zip(a, b)]
    sd = stats.std(spread)
    if not sd:
        return None
    mu = sum(spread) / len(spread)
    z = (spread[-1] - mu) / sd
    _, hl = _ar1([s - mu for s in spread])
    gross_gap_pct = abs(math.exp(abs(spread[-1] - mu)) - 1) * 100
    signal = (hl is not None and hl <= 14 and abs(z) >= z_gate
              and gross_gap_pct > fee_pct)
    return {
        "points": len(a),
        "beta": round(beta, 2),
        "z_spread": round(z, 2),
        "halflife_days": round(hl, 1) if hl else None,
        "gross_gap_pct": round(gross_gap_pct, 1),
        "fee_pct": round(fee_pct, 1),
        # z>0: a está cara vs b -> venda A / compre B
        "action": "vender A / comprar B" if z > 0 else "comprar A / vender B",
        "signal": bool(signal),
    }

# -*- coding: utf-8 -*-
"""Fusão de sinais: um escore composto que junta lucro, risco e previsão.

O opportunity_score (app.py) pondera só lucro/frescor/ROI/liquidez. Aqui ele
vira a BASE de um escore que ainda:
  - usa a PREVISIBILIDADE como porteiro (cala onde a série é ruído);
  - PENALIZA risco (volatilidade alta);
  - BONIFICA quando há sinal de reversão a favor (preço esticado p/ baixo) e
    quando o killboard mostra divergência (demanda subindo, preço atrasado).

Função pura: recebe os componentes já calculados (risk_profile, mean_reversion,
predictability, divergência) e devolve o composto 0..100 + a decomposição, para
o usuário ver de onde veio cada ponto.
"""


def composite(opp_score, risk_profile=None, reversion=None,
              predictability=None, divergence=False):
    """Escore composto 0..100 + parts (contribuição de cada fator)."""
    score = float(opp_score or 0)
    parts = {"base": round(score, 1)}

    # 1) previsibilidade como porteiro: série-ruído encolhe o escore (0.6..1.0)
    if predictability:
        g = 0.6 + 0.4 * max(0.0, min(1.0, predictability.get("predictability", 0.5)))
        score *= g
        parts["gate_previsibilidade"] = round(g, 2)

    # 2) penalidade de risco: vol anualizada alta tira pontos (até -25)
    if risk_profile and risk_profile.get("vol_annual_pct") is not None:
        vol = risk_profile["vol_annual_pct"] / 100
        pen = min(25.0, vol * 20)
        score -= pen
        parts["pen_risco"] = -round(pen, 1)

    # 3) bônus de reversão A FAVOR (preço abaixo da tendência, sinal de comprar)
    if reversion and reversion.get("signal") and reversion.get("direction") == "comprar":
        b = min(12.0, abs(reversion.get("z_resid", 0)) * 3)
        score += b
        parts["bonus_reversao"] = round(b, 1)

    # 4) bônus de divergência (killboard: destruição subindo, preço atrasado)
    if divergence:
        score += 10
        parts["bonus_divergencia"] = 10

    final = max(0.0, min(100.0, score))
    return round(final, 1), parts


def enrich(con, server, opps, history_days=120):
    """Anexa o escore composto + risco/reversão/divergência a uma lista de
    oportunidades (cada uma com item_id e opportunity_score). Carrega a série
    diária dos itens (history q1) e o conjunto de divergência do killboard;
    reordena por composite_score. Compartilhado por CLI e API (somente leitura).
    """
    from . import risk, forecast as fc
    from . import store
    ids = list({o["item_id"] for o in opps})
    if not ids:
        return opps
    ph = ",".join("?" * len(ids))
    # corte ISO em Python (portável SQLite/Postgres) — NÃO date('now') do SQLite
    rows = con.execute(
        f"""SELECT item_id, city, substr(ts,1,10) AS day, avg_price
            FROM history WHERE server=? AND time_scale=24 AND quality=1
              AND avg_price>0 AND ts >= ?
              AND item_id IN ({ph})
            ORDER BY item_id, city, day""",
        [server, store.cutoff_iso(history_days), *ids]).fetchall()
    div_set = set()
    try:
        from . import gameinfo
        for s in gameinfo.demand_price_divergence(con, server):
            div_set.add(s.get("item_id"))
    except Exception:
        pass
    by_ic = {}
    for item, city, _day, price in rows:
        by_ic.setdefault((item, city), []).append(price)
    best = {}
    for (item, city), series in by_ic.items():
        if item not in best or len(series) > len(best[item]):
            best[item] = series
    for o in opps:
        s = best.get(o["item_id"])
        rp = risk.risk_profile(s) if s else None
        rv = fc.mean_reversion(s) if s else None
        pr = fc.predictability(s) if s else None
        div = o["item_id"] in div_set
        comp, parts = composite(o.get("opportunity_score"), rp, rv, pr, div)
        o["composite_score"] = comp
        o["composite_parts"] = parts
        o["vol_pct"] = rp["vol_annual_pct"] if rp else None
        o["var_1d_pct"] = rp["var_1d_pct"] if rp else None
        o["revert_signal"] = bool(rv and rv.get("signal")
                                  and rv["direction"] == "comprar")
        o["divergence"] = div
    opps.sort(key=lambda o: -(o.get("composite_score") or 0))
    return opps

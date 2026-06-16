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

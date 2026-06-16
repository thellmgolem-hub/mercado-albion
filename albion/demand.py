# -*- coding: utf-8 -*-
"""Inteligência de demanda a partir do killboard (kill_event_equipment).

O que o servidor está perdendo de verdade — consumíveis queimados, qualidade do
gear destruído, build que está subindo — antes de o preço reagir. Tudo sobre as
tabelas de killboard já ingeridas (gameinfo) cruzadas com o mercado.

HONESTIDADE: a série de killboard é curta (poucos dias) e mal distribuída entre
os dias da semana; tendências semanais saem ruidosas. Cada função devolve o
tamanho da amostra para o usuário pesar.
"""
import re

from . import config
from .flips import sell_revenue

_TIER_PREFIX = re.compile(r"^T\d+_")
# sufixos de canal de artefato: a linha-base da arma é a mesma (ex.: arco normal
# e Wailing/Keeper são "2H_BOW"). Agrupar por base evita fragmentar a meta.
_ARTIFACT_SUFFIX = re.compile(r"_(KEEPER|HELL|UNDEAD|AVALON|MORGANA|FEY|CRYSTAL)$")


def item_family(item_id, base_only=True):
    """T4_MAIN_AXE@1 -> MAIN_AXE. Com base_only, também funde o canal de
    artefato (T4_2H_BOW_KEEPER@4 -> 2H_BOW), para a meta não fragmentar entre
    variantes da mesma linha de arma."""
    base = (item_id or "").split("@")[0]
    fam = _TIER_PREFIX.sub("", base)
    if base_only:
        fam = _ARTIFACT_SUFFIX.sub("", fam)
    return fam


def _exposure_days(con, server, days):
    """Dias EFETIVOS de exposição da ingestão (horas com ao menos 1 kill / 24).

    O killboard só é coletado quando o servidor está ligado — contar por
    dia-calendário superestima dias de pouca coleta. Normalizar por horas
    observadas torna 'por dia' comparável entre janelas."""
    hours = con.execute(
        """SELECT COUNT(DISTINCT strftime('%Y-%m-%dT%H', ts))
           FROM kill_events WHERE server=? AND ts >= datetime('now', ?)""",
        [server, f"-{int(days)} days"]).fetchone()[0] or 0
    return max(hours / 24.0, 1 / 24.0)   # piso de 1h p/ não dividir por zero


def consumable_burn(con, server, days=7, price_of=None, vol_of=None, limit=40):
    """Queima de consumíveis (poções/comida) por dia vs oferta do mercado.

    Soma as unidades destruídas por item (slot Potion/Food) na janela, converte
    em unidades/dia e cruza com o volume de mercado (history) — cobertura < 1
    sinaliza consumível SUBABASTECIDO (preço pronto pra subir). Valora em
    prata/dia pelo preço de venda.
    price_of(item)->preço q1; vol_of(item)->volume diário de mercado.
    """
    rows = con.execute(
        """SELECT e.item_id, SUM(e.count) AS units, COUNT(DISTINCT e.event_id) AS evs
           FROM kill_event_equipment e JOIN kill_events k
             ON k.server=e.server AND k.event_id=e.event_id
           WHERE e.server=? AND e.role='victim' AND e.slot IN ('Potion','Food')
             AND k.ts >= datetime('now', ?)
           GROUP BY e.item_id HAVING units>0""",
        [server, f"-{int(days)} days"]).fetchall()
    exp_days = _exposure_days(con, server, days)   # normaliza por exposição
    out = []
    for item, units, _evs in rows:
        per_day = units / exp_days
        price = price_of(item) if price_of else None
        market_vol = vol_of(item) if vol_of else None
        coverage = (market_vol / per_day) if (market_vol and per_day) else None
        out.append({
            "item_id": item,
            "burned_units": units,
            "per_day": round(per_day, 1),
            "market_vol_day": round(market_vol, 1) if market_vol else None,
            "coverage": round(coverage, 2) if coverage is not None else None,
            "silver_per_day": round(per_day * sell_revenue(price, "order", True))
            if price else None,
            "undersupplied": coverage is not None and coverage < 1,
        })
    out.sort(key=lambda r: -(r["silver_per_day"] or r["per_day"]))
    return out[:limit] if limit else out


def destroyed_quality(con, server, days=7, price_q=None, limit=40):
    """Qualidade do gear destruído × prêmio de qualidade no mercado.

    Por item: distribuição de destruição por qualidade (1-5) e o prêmio de
    preço de cada qualidade sobre q1. O indicador (E[qualidade demandada] em
    prêmio) revela quando vale craftar/estocar qualidade alta em vez de q1.
    price_q(item, q)->preço de venda q.
    """
    rows = con.execute(
        """SELECT item_id, quality, SUM(count) AS units
           FROM kill_event_equipment
           WHERE server=? AND role='victim'
             AND slot IN ('MainHand','OffHand','Head','Armor','Shoes','Cape')
             AND quality>0
             AND event_id IN (SELECT event_id FROM kill_events
                              WHERE server=? AND ts >= datetime('now', ?))
           GROUP BY item_id, quality""",
        [server, server, f"-{int(days)} days"]).fetchall()
    by_item = {}
    for item, q, units in rows:
        by_item.setdefault(item, {})[q] = units
    out = []
    for item, qd in by_item.items():
        total = sum(qd.values())
        if total < 5 or not price_q:
            continue
        p1 = price_q(item, 1)
        if not p1:
            continue
        # E[prêmio] = Σ share_q × (preço_q/preço_q1)
        ev_premium, hi_share = 0.0, 0.0
        for q, units in qd.items():
            pq = price_q(item, q)
            if not pq:
                continue
            share = units / total
            ev_premium += share * (pq / p1)
            if q >= 4:
                hi_share += share
        out.append({
            "item_id": item, "destroyed": total,
            "share_q4plus_pct": round(100 * hi_share, 1),
            "ev_quality_premium": round(ev_premium, 2),
            "dominant_q": max(qd, key=qd.get),
        })
    out.sort(key=lambda r: -r["ev_quality_premium"])
    return out[:limit] if limit else out


def meta_shift(con, server, days=7, recent=2, limit=30, min_recent_n=15):
    """Builds (arma+armadura) cuja participação nas mortes está subindo.

    Self-join de kill_event_equipment (victim) MainHand × Armor por evento;
    compara a participação da família na janela recente vs o resto. Δshare alto
    = build emergente, sinal de compra dos insumos antes do preço reagir.
    """
    def combo_counts(since, until=None):
        cond = "k.ts >= datetime('now', ?)"
        params = [server, f"-{int(since)} days"]
        if until is not None:
            cond += " AND k.ts < datetime('now', ?)"
            params.append(f"-{int(until)} days")
        rows = con.execute(
            f"""SELECT m.item_id AS weapon, a.item_id AS armor, COUNT(*) AS n
                FROM kill_event_equipment m
                JOIN kill_event_equipment a
                  ON a.server=m.server AND a.event_id=m.event_id
                     AND a.role='victim' AND a.slot='Armor'
                JOIN kill_events k
                  ON k.server=m.server AND k.event_id=m.event_id
                WHERE m.server=? AND m.role='victim' AND m.slot='MainHand'
                  AND {cond}
                GROUP BY weapon, armor""",
            params).fetchall()
        agg = {}
        total = 0
        for w, a, n in rows:
            key = (item_family(w), item_family(a))
            agg[key] = agg.get(key, 0) + n
            total += n
        return agg, total

    recent_agg, recent_total = combo_counts(recent)
    base_agg, base_total = combo_counts(days, recent)
    if not recent_total:
        return []
    out = []
    for key, n in recent_agg.items():
        # C4: gate contra ruído — uma build que aparece só com poucas mortes na
        # janela recente (ex.: um pico de 3 h) não é "meta subindo". Exige um
        # mínimo de observações recentes antes de emitir o sinal de Δshare.
        if n < min_recent_n:
            continue
        share_r = n / recent_total
        share_b = (base_agg.get(key, 0) / base_total) if base_total else 0
        out.append({
            "build": f"{key[0]} + {key[1]}",
            "recent_n": n,
            "share_recent_pct": round(100 * share_r, 2),
            "share_base_pct": round(100 * share_b, 2),
            "delta_pct": round(100 * (share_r - share_b), 2),
        })
    out.sort(key=lambda r: -r["delta_pct"])
    return out[:limit] if limit else out

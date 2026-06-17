# -*- coding: utf-8 -*-
"""PvP / meta de builds a partir do killboard (gameinfo).

Taxa de vitória derivada do feed de kills: um build que aparece como KILLER
levou aquele confronto; como VÍTIMA, perdeu. Então, por arma (MainHand) ou
build (arma+armadura):
    win% = vitórias / (vitórias + derrotas)
onde vitórias = vezes que a arma deu o golpe final e derrotas = vezes que
morreu portando-a. Métricas: pick rate (popularidade), K/D, IP médio, fama,
solo vs grupo.

HONESTIDADE (vieses conhecidos do dado):
- O killboard é uma AMOSTRA (ingerimos parte dos eventos); a flag de saturação
  (public_data_runs) avisa quando o pico foi subamostrado.
- "Vitória" é a posse do golpe final — em luta de grupo, atribui a UM killer;
  o killboard só guarda equipamento de killer e vítima (não dos demais do
  grupo). Logo armas de BURST/single-target inflam o win% vs suportes/tanques.
- n_participants é capado em 4 no dump, então "grupo" (>=4) é um piso.
A leitura honesta: win% alto = a arma costuma estar do lado que fecha o abate;
cruze com pick rate (amostra) antes de concluir que é "a meta".
"""
from .demand import item_family


def _slot_counts(con, server, days, slot, role):
    """{família da arma -> nº de eventos} para um slot e papel (killer/victim).

    Dirige a consulta por kill_events (índice em ts) e junta o equipamento pela
    PK — bem mais rápido que `event_id IN (subquery)`."""
    rows = con.execute(
        """SELECT e.item_id, COUNT(*) AS n
           FROM kill_events k
           JOIN kill_event_equipment e
             ON e.server=k.server AND e.event_id=k.event_id
           WHERE k.server=? AND k.ts >= datetime('now', ?)
             AND e.role=? AND e.slot=?
           GROUP BY e.item_id""",
        [server, f"-{int(days)} days", role, slot]).fetchall()
    out = {}
    for item_id, n in rows:
        fam = item_family(item_id)
        out[fam] = out.get(fam, 0) + n
    return out


def weapon_meta(con, server, days=7, min_fights=20, limit=40):
    """Win rate por ARMA (família de MainHand): vitórias (killer) vs derrotas
    (vítima), com pick rate e K/D. Gate por nº mínimo de confrontos."""
    wins = _slot_counts(con, server, days, "MainHand", "killer")
    losses = _slot_counts(con, server, days, "MainHand", "victim")
    total = sum(wins.values()) + sum(losses.values())
    out = []
    for fam in set(wins) | set(losses):
        w, l = wins.get(fam, 0), losses.get(fam, 0)
        n = w + l
        if n < min_fights:
            continue
        out.append({
            "weapon": fam, "wins": w, "losses": l, "fights": n,
            "win_rate_pct": round(100 * w / n, 1),
            "kd": round(w / l, 2) if l else None,
            "pick_rate_pct": round(100 * n / total, 2) if total else 0,
        })
    out.sort(key=lambda r: (-r["win_rate_pct"], -r["fights"]))
    return {"total_fights": total, "rows": out[:limit] if limit else out}


def _build_counts(con, server, days, role):
    """{(arma, armadura) -> nº de eventos} por papel, self-join MainHand×Armor."""
    rows = con.execute(
        """SELECT m.item_id AS weapon, a.item_id AS armor, COUNT(*) AS n
           FROM kill_events k
           JOIN kill_event_equipment m
             ON m.server=k.server AND m.event_id=k.event_id
                AND m.role=? AND m.slot='MainHand'
           JOIN kill_event_equipment a
             ON a.server=k.server AND a.event_id=k.event_id
                AND a.role=? AND a.slot='Armor'
           WHERE k.server=? AND k.ts >= datetime('now', ?)
           GROUP BY weapon, armor""",
        [role, role, server, f"-{int(days)} days"]).fetchall()
    out = {}
    for w, a, n in rows:
        key = (item_family(w), item_family(a))
        out[key] = out.get(key, 0) + n
    return out


def build_meta(con, server, days=7, min_fights=15, limit=40):
    """Win rate por BUILD (arma + armadura)."""
    wins = _build_counts(con, server, days, "killer")
    losses = _build_counts(con, server, days, "victim")
    total = sum(wins.values()) + sum(losses.values())
    out = []
    for key in set(wins) | set(losses):
        w, l = wins.get(key, 0), losses.get(key, 0)
        n = w + l
        if n < min_fights:
            continue
        out.append({
            "build": f"{key[0]} + {key[1]}", "weapon": key[0], "armor": key[1],
            "wins": w, "losses": l, "fights": n,
            "win_rate_pct": round(100 * w / n, 1),
            "kd": round(w / l, 2) if l else None,
            "pick_rate_pct": round(100 * n / total, 2) if total else 0,
        })
    out.sort(key=lambda r: (-r["win_rate_pct"], -r["fights"]))
    return {"total_fights": total, "rows": out[:limit] if limit else out}


def overview(con, server, days=7):
    """Métricas-resumo de PvP: volume, solo vs grupo, IP médio, fama."""
    k = con.execute(
        """SELECT COUNT(*) AS kills, AVG(NULLIF(n_participants,0)) AS avg_part,
                  SUM(CASE WHEN n_participants<=1 THEN 1 ELSE 0 END) AS solo,
                  SUM(CASE WHEN n_participants>=4 THEN 1 ELSE 0 END) AS grupo,
                  SUM(total_victim_kill_fame) AS fame
           FROM kill_events WHERE server=? AND ts >= datetime('now', ?)""",
        [server, f"-{int(days)} days"]).fetchone()
    ip = dict(con.execute(
        """SELECT a.role, AVG(a.avg_ip)
           FROM kill_events k
           JOIN kill_event_actors a
             ON a.server=k.server AND a.event_id=k.event_id
           WHERE k.server=? AND k.ts >= datetime('now', ?)
             AND a.role IN ('killer','victim') AND a.avg_ip>0
           GROUP BY a.role""",
        [server, f"-{int(days)} days"]).fetchall())
    kills = k[0] or 0
    return {
        "kills": kills,
        "avg_participants": round(k[1], 1) if k[1] else None,
        "solo_pct": round(100 * (k[2] or 0) / kills, 1) if kills else None,
        "group_pct": round(100 * (k[3] or 0) / kills, 1) if kills else None,
        "total_fame": k[4] or 0,
        "avg_ip_killer": round(ip.get("killer", 0)) if ip.get("killer") else None,
        "avg_ip_victim": round(ip.get("victim", 0)) if ip.get("victim") else None,
    }

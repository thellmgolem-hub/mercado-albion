# -*- coding: utf-8 -*-
"""Motor de ordens de serviço: converte sinais técnicos em missões simples.

Regra do plano: um sinal só vira ordem quando tem ação clara, perfil
responsável, prazo, quantidade sugerida, risco, confiança e explicação curta.
A linguagem é para a camada leiga; a evidência técnica continua nos painéis.
Geradores (todos cache-only):
  - trader:    recomendações com selo 'executar'
  - coletor:   recurso da watchlist vendendo acima da média 7d em alguma cidade
  - crafter:   sinais de divergência demanda x preço (killboard)
  - refinador: margem de refino positiva com preços locais
  - aviso:     pico de ganks/ZvZ recente (risco para coleta/transporte)
"""
import json
import time
from pathlib import Path

from . import config
from .flips import sell_revenue

DATA = Path(__file__).resolve().parent.parent / "data"
SAFE_ROYAL_CITIES = tuple(c for c in config.ROYAL_CITIES if c != "Caerleon")

ORDER_TTL_H = 4  # ordens expiram em 4 h (mercado muda rápido)

FAMILIES = {"WOOD": "PLANKS", "ORE": "METALBAR", "FIBER": "CLOTH",
            "HIDE": "LEATHER", "ROCK": "STONEBLOCK"}


def _order(profile, action, explanation, item_id=None, city_from=None,
           city_to=None, qty=None, capital=None, profit=None, roi=None,
           risk="médio", confidence="média", source="auto"):
    return {
        "profile_target": profile, "action_type": action,
        "item_id": item_id, "city_from": city_from, "city_to": city_to,
        "quantity_base": qty, "capital_required": capital,
        "expected_profit": profit, "expected_roi": roi,
        "risk_level": risk, "confidence": confidence,
        "explanation_short": explanation, "source_signal": source,
    }


def trader_orders(recommendations, limit=5,
                  max_capital=config.ORDER_MAX_CAPITAL):
    out = []
    for o in recommendations:
        if o.get("opportunity_label") != "executar":
            continue
        qty = max(1, int(o.get("liquidity_day", 0)
                         * (o.get("capture_rate") or 0.2)))
        # missão precisa caber no caixa: limita a quantidade ao teto de capital
        if o["cost"] > 0:
            qty = min(qty, int(max_capital // o["cost"]))
        if qty < 1:
            continue  # nem 1 unidade cabe no teto — não vira missão leiga
        out.append(_order(
            "trader", "transportar_e_vender",
            f"Compre por {o['buy_price']:,.0f} em {o['buy_city']} e venda em "
            f"{o['sell_city']} — lucro líq. {o['profit']:,.0f}/un "
            f"({o['roi_pct']:.0f}%), dado {o['buy_age_min']} min",
            item_id=o["item_id"], city_from=o["buy_city"],
            city_to=o["sell_city"], qty=qty,
            capital=round(o["cost"] * qty),
            profit=round(o["profit"] * qty), roi=o["roi_pct"],
            risk="médio" if o["buy_city"] != o["sell_city"] else "baixo",
            confidence=o.get("confidence_label", "média"),
            source="recommendations"))
        if len(out) >= limit:
            break
    return out


def gatherer_sell_orders(con, server, min_premium_pct=10, min_volume=20,
                         max_age_min=240, limit=5):
    """Recurso bruto com VENDA INSTANTÂNEA acima da média 7d.

    Usa buy_price_max (ordem de compra real, dinheiro comprometido) em vez do
    menor anúncio — imune ao anúncio absurdo de 999.999 que dispararia uma
    missão inexecutável para a camada leiga.
    """
    rows = con.execute("""
        WITH vwap7 AS (
          SELECT item_id, city,
                 SUM(item_count * avg_price) * 1.0 / SUM(item_count) AS vwap,
                 SUM(item_count) * 1.0
                   / COUNT(DISTINCT substr(ts,1,10)) AS vol_dia
          FROM history
          WHERE server = :srv AND time_scale = 24 AND quality = 1
            AND item_count > 0 AND avg_price > 0
            AND ts >= strftime('%Y-%m-%dT00:00:00', 'now', '-7 days')
          GROUP BY item_id, city
        )
        SELECT p.item_id, p.city, p.buy_price_max, v.vwap, v.vol_dia,
               p.buy_price_max_date
        FROM prices p
        JOIN vwap7 v ON v.item_id = p.item_id AND v.city = p.city
        JOIN static_items si ON si.item_id = p.item_id
        WHERE p.server = :srv AND p.quality = 1 AND p.buy_price_max > 0
          AND si.cat = 'crafting' AND si.sub = 'resources'
          AND p.city IN ('Bridgewatch','Fort Sterling','Lymhurst',
                         'Martlock','Thetford')
          AND v.vol_dia >= :vol
          AND p.buy_price_max >= v.vwap * (1 + :prem / 100.0)
        ORDER BY p.buy_price_max / v.vwap DESC
    """, {"srv": server, "vol": min_volume,
          "prem": min_premium_pct}).fetchall()
    from .flips import age_minutes
    out = []
    for item_id, city, price, vwap, vol, pdate in rows:
        age = age_minutes(pdate)
        if age is None or age > max_age_min:
            continue
        pct = 100 * (price / vwap - 1)
        net = sell_revenue(price, "instant", True)
        out.append(_order(
            "coletor", "vender",
            f"Venda instantânea em {city}: ordem de compra paga {pct:.0f}% "
            f"acima da média 7d ({price:,.0f} vs {vwap:,.0f}), volume "
            f"{vol:.0f}/dia, dado {age} min",
            item_id=item_id, city_to=city, qty=int(vol // 4) or 1,
            profit=round(net - vwap), roi=round(pct, 1),
            risk="baixo", confidence="média", source="compra_max_vs_vwap7"))
        if len(out) >= limit:
            break
    return out


def crafter_orders(divergence_signals, limit=4):
    out = []
    for s in divergence_signals[:limit]:
        ratio = s.get("demanda_ratio")
        motivo = (f"destruição ×{ratio:.1f} vs base" if ratio
                  else "demanda nova no killboard")
        out.append(_order(
            "crafter", "produzir_ou_estocar",
            f"{motivo}, preço ainda {100 * (s['preco_ratio'] - 1):+.0f}% "
            f"(7d), mercado gira {s['volume_dia']:,.0f}/dia",
            item_id=s["item_id"],
            qty=int(s["demanda_dia_recente"]) or 1,
            profit=None, roi=None, risk="médio",
            confidence="baixa" if not ratio else "média",
            source="divergencia_demanda_preco"))
    return out


def refiner_orders(con, server, premium=True, min_margin_pct=5,
                   tiers=(4, 5, 6), limit=4):
    """Margem de refino comprando e vendendo na mesma cidade.

    O RRR de bônus (36,7%) só vale na cidade com bônus da família
    (config.REFINING_BONUS_CITY); nas demais aplica-se o RRR base (~15,2%) —
    sem isso a margem fora do bônus sai inflada e a missão engana o leigo.
    """
    recipes_path = DATA / "recipes_refining.json"
    if not recipes_path.exists():
        return []
    recipes = json.loads(recipes_path.read_text(encoding="utf-8"))

    def assumption(key, default):
        r = con.execute(
            "SELECT value FROM economic_assumptions WHERE key=?",
            [key]).fetchone()
        return (r[0] if r else default) / 100

    rrr_bonus = assumption("refining_rrr", 36.7)
    rrr_base = assumption("refining_rrr_base", 15.2)

    def price(item_id, city):
        r = con.execute(
            "SELECT sell_price_min FROM prices WHERE server=? AND item_id=?"
            " AND city=? AND quality=1 AND sell_price_min > 0",
            [server, item_id, city]).fetchone()
        return r[0] if r else None

    out = []
    for fam, ref in FAMILIES.items():
        bonus_city = config.REFINING_BONUS_CITY.get(fam)
        for tier in tiers:
            rid = f"T{tier}_{ref}"
            recipe = recipes.get(rid)
            if not recipe:
                continue
            best = None
            for city in SAFE_ROYAL_CITIES:
                cost = 0
                ok = True
                for inp in recipe["inputs"]:
                    p = price(inp["id"], city)
                    if not p:
                        ok = False
                        break
                    cost += inp["count"] * p
                sell = price(rid, city)
                if not ok or not sell:
                    continue
                rrr = rrr_bonus if city == bonus_city else rrr_base
                eff = cost * (1 - rrr)
                margin = sell_revenue(sell, "order", premium) - eff
                pct = 100 * margin / eff if eff else 0
                if pct >= min_margin_pct and (best is None or pct > best[1]):
                    best = (city, pct, margin, eff, rrr)
            if best:
                city, pct, margin, eff, rrr = best
                bonus_txt = ("com bônus" if city == bonus_city
                             else "SEM bônus")
                out.append(_order(
                    "refinador", "refinar",
                    f"Refine em {city} ({bonus_txt}): margem {pct:.0f}% "
                    f"({margin:,.0f}/un, RRR {rrr * 100:.0f}%)",
                    item_id=rid, city_to=city, qty=50,
                    capital=round(eff * 50), profit=round(margin * 50),
                    roi=round(pct, 1), risk="baixo", confidence="média",
                    source="refino_margem"))
    out.sort(key=lambda o: -(o["expected_roi"] or 0))
    return out[:limit]


def risk_warnings(classification_recent, classification_day,
                  min_recent_deaths=30):
    """Aviso geral quando o padrão de gank das últimas horas piora."""
    out = []
    rec = {c["classe"]: c for c in classification_recent.get("classes", [])}
    day = {c["classe"]: c for c in classification_day.get("classes", [])}
    g_rec = rec.get("gank_provavel", {}).get("pct", 0)
    g_day = day.get("gank_provavel", {}).get("pct", 0)
    total_rec = classification_recent.get("total_mortes", 0)
    if total_rec >= min_recent_deaths and g_rec >= g_day + 10:
        msg = (f"Ganks acima do normal nas últimas 2h: {g_rec:.0f}% das "
               f"{total_rec} mortes (média 24h: {g_day:.0f}%). Transporte só "
               "com spread maior ou espere a janela passar.")
        for profile in ("coletor", "transportador"):
            out.append(_order(
                profile, "evitar_risco", msg, risk="alto",
                confidence="média", source="classificacao_killboard"))
    return out


EXPIRED_RETENTION_DAYS = 7  # expiradas ficam p/ backtest curto, depois saem


def persist(aodp, orders) -> int:
    """Substitui as ordens abertas geradas automaticamente pelas novas.

    Transacional: se algo falhar no meio, faz rollback — sem isso o UPDATE
    inicial deixaria o painel vazio e a transação suja na conexão.
    """
    now = time.time()
    with aodp.db_lock:
        try:
            aodp.db.execute(
                "UPDATE service_orders SET status='expirada'"
                " WHERE server=? AND status='aberta'", [aodp.server])
            aodp.db.execute(
                "DELETE FROM service_orders WHERE server=?"
                " AND status='expirada' AND created_at < ?",
                [aodp.server, now - EXPIRED_RETENTION_DAYS * 86400])
            aodp.db.executemany("""
                INSERT INTO service_orders (server, created_at, expires_at,
                  profile_target, action_type, item_id, city_from, city_to,
                  quantity_base, capital_required, expected_profit,
                  expected_roi, risk_level, confidence, explanation_short,
                  source_signal, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'aberta')
            """, [(aodp.server, now, now + ORDER_TTL_H * 3600,
                   o["profile_target"], o["action_type"], o["item_id"],
                   o["city_from"], o["city_to"], o["quantity_base"],
                   o["capital_required"], o["expected_profit"],
                   o["expected_roi"], o["risk_level"], o["confidence"],
                   o["explanation_short"], o["source_signal"])
                  for o in orders])
            aodp.db.commit()
        except Exception:
            aodp.db.rollback()
            raise
    return len(orders)


def list_open(con, server):
    rows = con.execute("""
        SELECT order_id, created_at, expires_at, profile_target, action_type,
               item_id, city_from, city_to, quantity_base, capital_required,
               expected_profit, expected_roi, risk_level, confidence,
               explanation_short, source_signal
        FROM service_orders
        WHERE server=? AND status='aberta'
          AND expires_at > strftime('%s', 'now')
        ORDER BY profile_target, expected_profit DESC
    """, [server]).fetchall()
    cols = ["order_id", "created_at", "expires_at", "profile_target",
            "action_type", "item_id", "city_from", "city_to",
            "quantity_base", "capital_required", "expected_profit",
            "expected_roi", "risk_level", "confidence",
            "explanation_short", "source_signal"]
    return [dict(zip(cols, r)) for r in rows]

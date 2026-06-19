# -*- coding: utf-8 -*-
"""Servidor local do app de mercado do Albion Online (Américas).

Rodar:  python app.py        (abre o navegador em http://127.0.0.1:8528)
"""
import re
import sqlite3
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from albion import config
from albion import stats
from albion import survival as survival_mod
from albion.client import AODP
from albion.flips import age_minutes, compute_flips, where_to_sell
from albion.items import ItemDB

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8528

app = FastAPI(title="Mercado Albion — Américas")
db = ItemDB()
aodp = AODP(server=config.DEFAULT_SERVER)


if config.ACCESS_TOKEN:
    @app.middleware("http")
    async def _token_guard(request, call_next):
        tok = config.ACCESS_TOKEN
        ok = (request.cookies.get("albion_token") == tok
              or request.query_params.get("token") == tok
              or request.headers.get("x-token") == tok)
        if not ok:
            return Response("acesso negado — abra com ?token=SEU_TOKEN",
                            status_code=401)
        resp = await call_next(request)
        resp.set_cookie("albion_token", tok, max_age=30 * 86400)
        return resp
SAFE_ROYAL_CITIES = [c for c in config.ROYAL_CITIES if c != "Caerleon"]


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _csv_int(value: str | None) -> list[int] | None:
    vals = _csv(value)
    return [int(v) for v in vals] if vals else None


def _resolve_items(ids: list[str]) -> list[str]:
    out, unknown = [], []
    for i in ids:
        it = db.get(i)
        (out if it else unknown).append(it["id"] if it else i)
    if unknown:
        raise HTTPException(400, f"Itens desconhecidos: {', '.join(unknown[:10])}")
    return out


def _api_guard(fn):
    try:
        return fn()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Erro ao consultar a API do Albion Data: {e}")


@app.get("/api/meta")
def meta():
    cats = db.categories()
    categories = {}
    for cat, info in sorted(cats.items(), key=lambda kv: -kv[1]["count"]):
        categories[cat] = {
            "label": config.CATEGORIES_PT.get(cat, cat),
            "count": info["count"],
            "subs": {
                sub: {"label": config.SUBCATEGORIES_PT.get(sub, sub or "—"),
                      "count": n}
                for sub, n in sorted(info["subs"].items(), key=lambda kv: -kv[1])
            },
        }
    return {
        "server": aodp.server,
        "server_label": "Américas",
        "cities": config.CITIES,
        "city_labels": config.CITY_LABELS_PT,
        "royal_cities": config.ROYAL_CITIES,
        "qualities": config.QUALITIES,
        "categories": categories,
        "black_market_categories": sorted(config.BLACK_MARKET_CATEGORIES),
        "taxes": {
            "sales_tax_premium": config.SALES_TAX_PREMIUM,
            "sales_tax_no_premium": config.SALES_TAX_NO_PREMIUM,
            "setup_fee": config.SETUP_FEE,
        },
        "item_count": len(db.items),
    }


@app.get("/api/status")
def status():
    """Resumo local do cache para diagnostico rapido da plataforma."""
    db_path = ROOT / "data" / "cache.db"
    tables = {}
    if db_path.exists():
        con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            existing = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            specs = {
                "prices": "MAX(datetime(fetched_at,'unixepoch'))",
                "price_snapshots": "MAX(datetime(fetched_at,'unixepoch'))",
                "history": "MAX(datetime(fetched_at,'unixepoch'))",
                "fetch_log": "MAX(datetime(fetched_at,'unixepoch'))",
                "gold": "MAX(ts)",
                "watchlist": "MAX(datetime(added_at,'unixepoch'))",
                "collection_runs": "MAX(datetime(started_at,'unixepoch'))",
            }
            for name, latest_expr in specs.items():
                if name not in existing:
                    tables[name] = {"rows": 0, "latest": None}
                    continue
                row = con.execute(
                    f"SELECT COUNT(*), {latest_expr} FROM {name}").fetchone()
                tables[name] = {"rows": row[0], "latest": row[1]}
        finally:
            con.close()
    return {
        "server": aodp.server,
        "item_count": len(db.items),
        "cache_exists": db_path.exists(),
        "cache_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "tables": tables,
    }


@app.get("/api/search")
def search(q: str = "", cat: str | None = None, sub: str | None = None,
           tier_min: int | None = None, tier_max: int | None = None,
           ench: int | None = None, limit: int = Query(30, le=300),
           group: bool = False):
    return db.search(q, cat=cat, sub=sub, tier_min=tier_min,
                     tier_max=tier_max, ench=ench, limit=limit, group=group)


@app.get("/api/prices")
def prices(items: str, cities: str | None = None, qualities: str | None = None,
           max_age: int = Query(config.PRICES_TTL, ge=0)):
    item_ids = _resolve_items(_csv(items) or [])
    if not item_ids:
        raise HTTPException(400, "Informe ao menos um item")
    city_list = _csv(cities) or config.CITIES
    quals = _csv_int(qualities)
    rows = _api_guard(lambda: aodp.get_prices(item_ids, city_list, max_age=max_age))
    out = []
    for r in rows:
        if quals and r["quality"] not in quals:
            continue
        it = db.get(r["item_id"]) or {}
        out.append({
            **r,
            "name_pt": it.get("pt", r["item_id"]),
            "tier": it.get("tier", 0),
            "ench": it.get("ench", 0),
            "sell_age_min": age_minutes(r["sell_price_min_date"]),
            "buy_age_min": age_minutes(r["buy_price_max_date"]),
        })
    return out


def _city_scope(cities, buy_cities, sell_cities):
    """Resolve as listas de cidades: união para buscar, conjuntos para filtrar."""
    buy = _csv(buy_cities)
    sell = _csv(sell_cities)
    base = _csv(cities)
    if base is None:
        base = sorted({*(buy or []), *(sell or [])}) or config.CITIES
    return base, (set(buy) if buy else None), (set(sell) if sell else None)


def _cache_connection():
    db_path = ROOT / "data" / "cache.db"
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _generate_service_orders():
    """Gera missoes simples a partir dos sinais ja materializados no cache."""
    from albion import gameinfo
    from albion import orders as orders_mod

    recs = recommendations(
        qualities="1", premium=True, buy_mode="instant", sell_mode="order",
        min_profit=0, min_roi=None, max_age_buy=720, max_age_sell=720,
        min_daily_volume=20, min_active_days=2, history_days=7,
        capture_rate=config.CAPTURE_RATE, same_city=False,
        exclude_outliers=True, limit=12)
    rec_opps = recs.get("opportunities", [])

    with aodp.db_lock:
        sigs = gameinfo.demand_price_divergence(
            aodp.db, aodp.server, min_units_day=10, limit=8)
        cls_recent = gameinfo.classification_summary(
            aodp.db, aodp.server, days=2 / 24)
        cls_day = gameinfo.classification_summary(
            aodp.db, aodp.server, days=1)
        generated = []
        generated.extend(orders_mod.trader_orders(rec_opps, limit=4))
        generated.extend(orders_mod.crafter_orders(sigs, limit=4))
        generated.extend(orders_mod.gatherer_sell_orders(
            aodp.db, aodp.server, limit=4))
        generated.extend(orders_mod.refiner_orders(
            aodp.db, aodp.server, limit=4))
        generated.extend(orders_mod.risk_warnings(cls_recent, cls_day))

    count = orders_mod.persist(aodp, generated)
    return count


def _service_orders_payload():
    from albion import orders as orders_mod

    with aodp.db_lock:
        rows = orders_mod.list_open(aodp.db, aodp.server)
    for r in rows:
        meta = db.get(r.get("item_id")) if r.get("item_id") else None
        if meta:
            r["name_pt"] = meta.get("pt", r["item_id"])
            r["tier"] = meta.get("tier", 0)
            r["ench"] = meta.get("ench", 0)
        else:
            r["name_pt"] = r.get("item_id") or ""
            r["tier"] = 0
            r["ench"] = 0
    return {"orders": rows, "count": len(rows)}


def _placeholders(vals):
    return ",".join("?" for _ in vals)


def _item_matches(it, cat=None, sub=None, tier_min=None, tier_max=None,
                  ench_list=None):
    if not it:
        return False
    if cat and it.get("cat") != cat:
        return False
    if sub and it.get("sub") != sub:
        return False
    if tier_min is not None and it.get("tier", 0) < tier_min:
        return False
    if tier_max is not None and it.get("tier", 0) > tier_max:
        return False
    if ench_list is not None and it.get("ench") not in ench_list:
        return False
    return True


def _cached_price_rows(con, city_list, qualities, cat=None, sub=None,
                       tier_min=None, tier_max=None, ench=None):
    """Le os precos ja cacheados e aplica filtros de metadados locais."""
    params = [aodp.server, *city_list]
    where = [f"server=?", f"city IN ({_placeholders(city_list)})"]
    if qualities:
        where.append(f"quality IN ({_placeholders(qualities)})")
        params.extend(qualities)
    rows = con.execute(
        f"SELECT * FROM prices WHERE {' AND '.join(where)}", params).fetchall()
    ench_list = _csv_int(ench)
    out = []
    for r in rows:
        it = db.get(r["item_id"])
        if _item_matches(it, cat=cat, sub=sub, tier_min=tier_min,
                         tier_max=tier_max, ench_list=ench_list):
            out.append(dict(r))
    return out


def _cache_coverage(con):
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }

    def one(table):
        if table not in tables:
            return 0
        query = f"SELECT COUNT(DISTINCT item_id) FROM {table}"
        row = con.execute(query).fetchone()
        return row[0] if row else 0
    return {
        "catalog_items": len(db.items),
        "price_items": one("prices"),
        "history_items": one("history"),
        "snapshot_items": one("price_snapshots"),
    }


def _history_stats(con, item_ids, city_list, qualities, days):
    """Media diaria, dias ativos e VWAP historico para medir liquidez."""
    if not item_ids or not city_list:
        return {}, None
    max_ts = con.execute(
        f"""SELECT MAX(ts) AS m FROM history
            WHERE server=? AND time_scale=24
              AND city IN ({_placeholders(city_list)})""",
        [aodp.server, *city_list]).fetchone()["m"]
    if not max_ts:
        return {}, None
    ref = datetime.fromisoformat(max_ts[:19])
    start = (ref - timedelta(days=max(days - 1, 0))).isoformat(timespec="seconds")
    params = [aodp.server, start, *item_ids, *city_list]
    where = [
        "server=?",
        "time_scale=24",
        "ts>=?",
        f"item_id IN ({_placeholders(item_ids)})",
        f"city IN ({_placeholders(city_list)})",
        "item_count>0",
        "avg_price>0",
    ]
    if qualities:
        where.append(f"quality IN ({_placeholders(qualities)})")
        params.extend(qualities)
    stats = {}
    for r in con.execute(
            f"""SELECT item_id, city, quality,
                       SUM(item_count) AS total_volume,
                       COUNT(DISTINCT substr(ts,1,10)) AS active_days,
                       SUM(item_count * avg_price) AS traded_value
                FROM history
                WHERE {' AND '.join(where)}
                GROUP BY item_id, city, quality""", params):
        total = r["total_volume"] or 0
        vwap = ((r["traded_value"] or 0) / total) if total else None
        stats[(r["item_id"], r["city"], r["quality"])] = {
            "avg_daily": total / max(days, 1),
            "active_days": r["active_days"] or 0,
            "active_ratio": (r["active_days"] or 0) / max(days, 1),
            "total_volume": total,
            "vwap": vwap,
        }
    return stats, max_ts


def _price_ratio(price, hist):
    vwap = hist.get("vwap") if hist else None
    if not price or not vwap:
        return None
    return price / vwap


def _liquidity_adjusted_confidence(o):
    """Confiança = frescor limitado pela liquidez (dado fresco de item que
    não vende não merece 'alta')."""
    liq = o.get("liquidity_day")
    base = o.get("confidence_score") or 0
    score = base
    if liq is not None:
        if liq < 1:
            score = min(score, 35)
        elif liq < 5:
            score = min(score, 55)
    if score >= 80:
        label = "alta"
    elif score >= 55:
        label = "media"
    elif score >= 30:
        label = "baixa"
    else:
        label = "muito baixa"
    notes = o.get("confidence_notes") or ""
    if score < base:
        notes = (notes + "; " if notes else "") + f"liquidez baixa ({liq}/dia)"
    o["confidence_score"] = score
    o["confidence_label"] = label
    o["confidence_notes"] = notes
    return o


def _score_recommendations(opps):
    """Score multifator com âncoras ABSOLUTAS (config.SCORE_ANCHORS).

    Escala log: âncora -> 100 pontos. O score de uma oportunidade não depende
    das outras da lista — é comparável entre consultas e ao longo do tempo.
    """
    anchors = config.SCORE_ANCHORS
    for o in opps:
        profit_score = stats.abs_score(o["profit"], anchors["profit"])
        # usa o potencial realista (com taxa de captura) contra âncora na
        # mesma escala, mantendo o score estável e comparável
        cap = o.get("capture_rate") or 1
        daily_score = stats.abs_score(
            o.get("daily_realistic") or o.get("daily_potential"),
            anchors["daily"] * cap)
        liq_score = stats.abs_score(o.get("liquidity_day"), anchors["liquidity"])
        roi_score = min(max(o["roi_pct"], 0) / 30 * 100, 100)
        fresh_score = o.get("confidence_score") or 0
        score = (0.30 * daily_score + 0.25 * fresh_score +
                 0.20 * roi_score + 0.15 * liq_score + 0.10 * profit_score)
        o["opportunity_score"] = round(score, 1)
        if score >= 75:
            o["opportunity_label"] = "executar"
        elif score >= 55:
            o["opportunity_label"] = "monitorar"
        else:
            o["opportunity_label"] = "cautela"
    opps.sort(key=lambda x: (-x["opportunity_score"], -(x.get("daily_potential") or 0)))
    return opps


@app.get("/api/recommendations")
def recommendations(cat: str | None = None, sub: str | None = None,
                    tier_min: int | None = None, tier_max: int | None = None,
                    ench: str | None = None, qualities: str | None = "1",
                    premium: bool = True, buy_mode: str = "instant",
                    sell_mode: str = "order", min_profit: float = 0,
                    min_roi: float | None = None,
                    max_age_buy: int | None = Query(720, ge=0),
                    max_age_sell: int | None = Query(720, ge=0),
                    min_daily_volume: float = Query(20, ge=0),
                    min_active_days: int = Query(2, ge=0),
                    history_days: int = Query(7, ge=1, le=180),
                    capture_rate: float = Query(config.CAPTURE_RATE,
                                                ge=0.01, le=1),
                    buy_cities: str | None = None,
                    sell_cities: str | None = None,
                    same_city: bool = False,
                    exclude_outliers: bool = True,
                    fused: bool = False,
                    limit: int = Query(25, le=200)):
    """Recomendacoes cache-only de flips por caracteristicas, sem item escolhido."""
    if buy_mode not in ("instant", "order") or sell_mode not in ("instant", "order"):
        raise HTTPException(400, "buy_mode/sell_mode deve ser 'instant' ou 'order'")
    buy = _csv(buy_cities) or SAFE_ROYAL_CITIES
    sell = _csv(sell_cities) or SAFE_ROYAL_CITIES
    city_list = sorted(set(buy) | set(sell))
    quals = _csv_int(qualities)
    con = _cache_connection()
    if con is None:
        return {
            "source": "cache",
            "items_considered": 0,
            "coverage": {"catalog_items": len(db.items), "price_items": 0,
                         "history_items": 0, "snapshot_items": 0},
            "history_until": None,
            "opportunities": [],
        }
    try:
        coverage = _cache_coverage(con)
        rows = _cached_price_rows(
            con, city_list, quals, cat=cat, sub=sub, tier_min=tier_min,
            tier_max=tier_max, ench=ench)
        item_ids = sorted({r["item_id"] for r in rows})
        if not item_ids:
            return {
                "source": "cache",
                "items_considered": 0,
                "coverage": coverage,
                "history_until": None,
                "opportunities": [],
            }

        metas = {item_id: db.get(item_id) for item_id in item_ids}
        opps = compute_flips(
            rows, metas, premium=premium, buy_mode=buy_mode,
            sell_mode=sell_mode, min_profit=min_profit, min_roi=min_roi,
            same_city=same_city, max_age_buy=max_age_buy,
            max_age_sell=max_age_sell, qualities=quals,
            buy_cities=set(buy), sell_cities=set(sell))

        stats, history_until = _history_stats(con, item_ids, city_list, quals, history_days)
        annotated = []
        for o in opps:
            vb = stats.get((o["item_id"], o["buy_city"], o["quality"]))
            vs = stats.get((o["item_id"], o["sell_city"], o["quality"]))
            if not vb or not vs:
                continue
            liq = min(vb["avg_daily"], vs["avg_daily"])
            active_days = min(vb["active_days"], vs["active_days"])
            if liq < min_daily_volume or active_days < min_active_days:
                continue
            buy_ratio = _price_ratio(o["buy_price"], vb)
            sell_ratio = _price_ratio(o["sell_price"], vs)
            flags = []
            if buy_ratio is not None and not 0.35 <= buy_ratio <= 3:
                flags.append("compra fora do historico")
            if sell_ratio is not None and not 0.35 <= sell_ratio <= 3:
                flags.append("venda fora do historico")
            if exclude_outliers and flags:
                continue
            meta = metas.get(o["item_id"]) or {}
            o.update({
                "cat": meta.get("cat"),
                "sub": meta.get("sub"),
                "vol_buy_day": round(vb["avg_daily"], 1),
                "vol_sell_day": round(vs["avg_daily"], 1),
                "liquidity_day": round(liq, 1),
                "vol_buy_total": round(vb["total_volume"], 1),
                "vol_sell_total": round(vs["total_volume"], 1),
                "volume_min_total": round(min(vb["total_volume"], vs["total_volume"]), 1),
                "sales_days_buy": vb["active_days"],
                "sales_days_sell": vs["active_days"],
                "sales_days_min": active_days,
                "history_days": history_days,
                "buy_hist_avg": round(vb["vwap"], 1) if vb["vwap"] else None,
                "sell_hist_avg": round(vs["vwap"], 1) if vs["vwap"] else None,
                "buy_price_vs_hist": round(buy_ratio, 2) if buy_ratio else None,
                "sell_price_vs_hist": round(sell_ratio, 2) if sell_ratio else None,
                "price_flags": flags,
                "daily_potential": round(liq * o["profit"], 1),
                "daily_realistic": round(liq * o["profit"] * capture_rate, 1),
                "capture_rate": capture_rate,
            })
            annotated.append(_liquidity_adjusted_confidence(o))

        _score_recommendations(annotated)
        top = annotated[:limit]
        if fused and top:
            # escore COMPOSTO: funde risco/reversão/divergência ao score base
            from albion import fusion
            top = fusion.enrich(con, config.DEFAULT_SERVER, top)
        return {
            "source": "cache",
            "items_considered": len(item_ids),
            "price_rows_considered": len(rows),
            "coverage": coverage,
            "history_until": history_until,
            "fused": fused,
            "opportunities": top,
        }
    finally:
        con.close()


@app.get("/api/flips")
def flips(items: str, cities: str | None = None, qualities: str | None = None,
          premium: bool = True, buy_mode: str = "instant",
          sell_mode: str = "instant", min_profit: float = 0,
          min_roi: float | None = None, same_city: bool = False,
          max_age_buy: int | None = None, max_age_sell: int | None = None,
          buy_cities: str | None = None, sell_cities: str | None = None,
          max_age: int = Query(config.PRICES_TTL, ge=0)):
    if buy_mode not in ("instant", "order") or sell_mode not in ("instant", "order"):
        raise HTTPException(400, "buy_mode/sell_mode deve ser 'instant' ou 'order'")
    item_ids = _resolve_items(_csv(items) or [])
    if not item_ids:
        raise HTTPException(400, "Informe ao menos um item")
    city_list, buy_set, sell_set = _city_scope(cities, buy_cities, sell_cities)
    rows = _api_guard(lambda: aodp.get_prices(item_ids, city_list, max_age=max_age))
    metas = {i: db.get(i) for i in item_ids}
    return compute_flips(
        rows, metas, premium=premium, buy_mode=buy_mode, sell_mode=sell_mode,
        min_profit=min_profit, min_roi=min_roi, same_city=same_city,
        max_age_buy=max_age_buy, max_age_sell=max_age_sell,
        qualities=_csv_int(qualities), buy_cities=buy_set, sell_cities=sell_set)


@app.get("/api/scan")
def scan(cat: str | None = None, sub: str | None = None,
         tier_min: int | None = None, tier_max: int | None = None,
         ench: str | None = None, qualities: str | None = None,
         cities: str | None = None, premium: bool = True,
         buy_mode: str = "instant", sell_mode: str = "instant",
         min_profit: float = 0, min_roi: float | None = None,
         same_city: bool = False, max_age_buy: int | None = None,
         max_age_sell: int | None = None,
         buy_cities: str | None = None, sell_cities: str | None = None,
         max_items: int = Query(300, le=1500), limit: int = Query(100, le=500),
         with_volume: bool = True, max_age: int = Query(config.PRICES_TTL, ge=0)):
    """Escaneia uma categoria inteira em busca de oportunidades de flip."""
    if buy_mode not in ("instant", "order") or sell_mode not in ("instant", "order"):
        raise HTTPException(400, "buy_mode/sell_mode deve ser 'instant' ou 'order'")
    all_items = db.filter(cat=cat, sub=sub, tier_min=tier_min, tier_max=tier_max,
                          ench_list=_csv_int(ench))
    items_total = len(all_items)
    if not all_items:
        return {"items_scanned": 0, "items_total": 0, "opportunities": []}
    # ordena por tier desc (itens de maior valor primeiro) em vez da ordem de
    # arquivo — sem isto o cap cobre só as primeiras famílias do JSON
    all_items.sort(key=lambda i: (-i["tier"], i["id"]))
    items = all_items[:max_items]
    item_ids = [i["id"] for i in items]
    city_list, buy_set, sell_set = _city_scope(cities, buy_cities, sell_cities)
    rows = _api_guard(lambda: aodp.get_prices(item_ids, city_list, max_age=max_age))
    metas = {i["id"]: i for i in items}
    opps = compute_flips(
        rows, metas, premium=premium, buy_mode=buy_mode, sell_mode=sell_mode,
        min_profit=min_profit, min_roi=min_roi, same_city=same_city,
        max_age_buy=max_age_buy, max_age_sell=max_age_sell,
        qualities=_csv_int(qualities), buy_cities=buy_set,
        sell_cities=sell_set)[:limit]

    if with_volume and opps:
        top_ids = list(dict.fromkeys(o["item_id"] for o in opps))[:60]
        vol_cities = sorted({c for o in opps for c in (o["buy_city"], o["sell_city"])})
        series = _api_guard(lambda: aodp.get_history(
            top_ids, vol_cities, time_scale=24, days=7))
        vol = {}  # (item, city, quality) -> média diária
        for s in series:
            pts = s["data"]
            if pts:
                total = sum(p["item_count"] for p in pts)
                days_n = max(len({p["ts"][:10] for p in pts}), 1)
                vol[(s["item_id"], s["city"], s["quality"])] = round(total / days_n)
        for o in opps:
            vb = vol.get((o["item_id"], o["buy_city"], o["quality"]))
            vs = vol.get((o["item_id"], o["sell_city"], o["quality"]))
            o["vol_buy_day"] = vb
            o["vol_sell_day"] = vs
            # 0 é dado real (ninguém vendeu) e mata a oportunidade; None = sem dado
            o["daily_potential"] = (min(vb, vs) * o["profit"]
                                    if vb is not None and vs is not None else None)
            o["daily_realistic"] = (
                round(o["daily_potential"] * config.CAPTURE_RATE, 1)
                if o["daily_potential"] is not None else None)
            o["capture_rate"] = config.CAPTURE_RATE
            o["liquidity_day"] = (min(vb, vs)
                                  if vb is not None and vs is not None else None)
            _liquidity_adjusted_confidence(o)

    return {"items_scanned": len(item_ids), "items_total": items_total,
            "opportunities": opps}


@app.get("/api/craft")
def craft_margin(item: str, premium: bool = True, sell_mode: str = "order",
                 focus: bool = False, spec_fce: int = Query(0, ge=0, le=80000),
                 focus_budget: int | None = Query(None, ge=0, le=100_000_000),
                 daily_bonus: float = Query(0.0, ge=0, le=0.5),
                 station_fee: float = Query(0, ge=0), same_city: bool = False,
                 source_cities: str | None = None, sell_cities: str | None = None,
                 fee: float = 0,  # compat: alias antigo de station_fee
                 max_age: int = Query(config.PRICES_TTL, ge=0)):
    """Estúdio de craft: melhor cidade p/ comprar CADA insumo + melhor cidade de
    venda + RRR pela cidade-bônus + camada de especialização (custo de foco).
    A spec (FCE) só barateia o foco — não muda o RRR (rendimento de recursos)."""
    from albion import craft as craft_mod
    item_id = _resolve_items([item])[0]
    recipe = craft_mod.recipe_for(item_id)
    if recipe is None:
        raise HTTPException(404, "Item sem receita de craft no dump")
    src = _csv(source_cities) or (list(config.ROYAL_CITIES) + ["Brecilien"])
    sells = _csv(sell_cities) or list(config.CITIES)
    all_cities = list(dict.fromkeys(src + sells))
    ids = [item_id] + [i["id"] for i in recipe["inputs"]]
    rows = _api_guard(lambda: aodp.get_prices(ids, all_cities, max_age=max_age))
    # O estúdio pega o MENOR custo de compra e a MAIOR venda entre cidades —
    # exatamente o que ordens-isca exploram, dos dois lados. Defesa: uma BANDA
    # de preço plausível por item (piso E teto) via craft.anchor_band — salto
    # >8× entre cidades (pega âncora mesmo em maioria) + preço REAL negociado
    # (history mediana ~21d) quando há. Construímos sobre os preços CRUS (sem
    # zerar nada antes: o saneamento por z robusto erra quando a isca é maioria).
    acq, bid, quotes = {}, {}, {}
    for r in rows:
        if r["quality"] != 1:
            continue
        key = (r["item_id"], r["city"])
        sp = r.get("sell_price_min") or 0
        bp = r.get("buy_price_max") or 0
        if sp > 0:
            if key not in acq or sp < acq[key]:
                acq[key] = sp
            quotes.setdefault(r["item_id"], []).append(sp)
        if bp > 0 and (key not in bid or bp > bid[key]):
            bid[key] = bp
    hist_med = {}
    _hcon = _cache_connection()
    if _hcon is not None:
        try:
            for iid in ids:
                hv = sorted(v[0] for v in _hcon.execute(
                    """SELECT avg_price FROM history WHERE server=? AND item_id=?
                       AND quality=1 AND time_scale=24 AND avg_price>0
                       AND ts>=date('now','-21 days')""", [aodp.server, iid]).fetchall())
                if hv:
                    hist_med[iid] = hv[len(hv) // 2]
        finally:
            _hcon.close()
    bands = {iid: craft_mod.anchor_band(quotes.get(iid, []), hist_med.get(iid))
             for iid in ids}
    res = craft_mod.studio(
        item_id, recipe, lambda i, c: acq.get((i, c)),
        lambda i, c: bid.get((i, c)), premium=premium, sell_mode=sell_mode,
        focus=focus, spec_fce=spec_fce, focus_budget=focus_budget,
        daily_bonus=daily_bonus, station_fee=station_fee or fee,
        source_cities=src, sell_cities=sells, same_city=same_city,
        band_of=lambda i: bands.get(i, (None, None)))
    name = lambda i: (db.get(i) or {}).get("pt", i)
    for row in res:
        for s in row.get("sourcing", []):
            s["name_pt"] = name(s["id"])
    meta = db.get(item_id) or {}
    return {
        "item": {"id": item_id, "name_pt": meta.get("pt", item_id),
                 "tier": meta.get("tier", 0), "ench": meta.get("ench", 0)},
        "category": recipe.get("category"),
        "bonus_city": craft_mod.unified_bonus_city(item_id, recipe.get("category")),
        "focus": recipe.get("focus"), "spec_fce": spec_fce,
        "focus_budget": focus_budget, "same_city": same_city,
        "inputs": [{"id": i["id"], "count": i["count"], "name_pt": name(i["id"])}
                   for i in recipe["inputs"]],
        "rows": res,
    }


_supply_cache = {}


@app.get("/api/origin")
def origin(item: str):
    """De onde o item nasce: mobs/conteúdo que o dropam (lado da oferta)."""
    item_id = _resolve_items([item])[0]
    if "data" not in _supply_cache:
        p = ROOT / "data" / "supply_data.json"
        import json as _j
        _supply_cache["data"] = (_j.loads(p.read_text(encoding="utf-8"))
                                 if p.exists() else {})
    supply = _supply_cache["data"]
    srcs = supply.get(item_id) or supply.get(item_id.split("@")[0]) or []
    meta = db.get(item_id) or {}
    return {"item": {"id": item_id, "name_pt": meta.get("pt", item_id)},
            "sources": srcs}


@app.get("/api/sell")
def sell(items: str, qualities: str | None = None, premium: bool = True,
         cities: str | None = None,
         max_age: int = Query(config.PRICES_TTL, ge=0)):
    """Modo 'Onde Vender': melhores cidades/métodos para vender o que você tem."""
    item_ids = _resolve_items(_csv(items) or [])
    if not item_ids:
        raise HTTPException(400, "Informe ao menos um item")
    city_list = _csv(cities) or config.CITIES
    rows = _api_guard(lambda: aodp.get_prices(item_ids, city_list, max_age=max_age))
    metas = {i: db.get(i) for i in item_ids}
    return where_to_sell(rows, metas, premium=premium,
                         qualities=_csv_int(qualities))


def _cached_history_series(item_ids, city_list, quality, time_scale, days):
    con = _cache_connection()
    if con is None:
        return []
    try:
        filters = [
            "server=?",
            "time_scale=?",
            f"item_id IN ({_placeholders(item_ids)})",
            f"city IN ({_placeholders(city_list)})",
        ]
        params = [aodp.server, time_scale, *item_ids, *city_list]
        if quality is not None:
            filters.insert(2, "quality=?")
            params.insert(2, quality)
        max_ts = con.execute(
            f"""SELECT MAX(ts) AS m FROM history
                WHERE {' AND '.join(filters)}""",
            params).fetchone()["m"]
        if not max_ts:
            return []
        ref = stats.parse_ts(max_ts)
        start = (ref - timedelta(days=days)).isoformat(timespec="seconds")
        filters.append("ts>=?")
        params_with_start = [*params, start]
        rows = con.execute(
            f"""SELECT item_id, city, quality, ts, item_count, avg_price
                FROM history
                WHERE {' AND '.join(filters)}
                ORDER BY ts""",
            params_with_start
        ).fetchall()
        out = {}
        for r in rows:
            s = out.setdefault((r["item_id"], r["city"], r["quality"]), {
                "item_id": r["item_id"],
                "city": r["city"],
                "quality": r["quality"],
                "data": [],
            })
            s["data"].append({
                "ts": r["ts"],
                "item_count": r["item_count"],
                "avg_price": r["avg_price"],
            })
        return list(out.values())
    finally:
        con.close()


@app.get("/api/item-analysis")
def item_analysis(item: str, cities: str | None = None,
                  quality: int = Query(1, ge=1, le=5),
                  time_scale: int = Query(24), days: int = Query(30, le=365),
                  max_age: int = Query(config.HISTORY_TTL, ge=0),
                  cache_only: bool = True):
    """Item Lab: estatisticas, tendencia e interpretacao do historico."""
    if time_scale not in (1, 6, 24):
        raise HTTPException(400, "time_scale deve ser 1, 6 ou 24")
    item_id = _resolve_items([item])[0]
    city_list = _csv(cities) or SAFE_ROYAL_CITIES
    if cache_only:
        series = _cached_history_series([item_id], city_list, quality, time_scale, days)
    else:
        series = _api_guard(lambda: aodp.get_history(
            [item_id], city_list, time_scale=time_scale, days=days,
            max_age=max_age))
        series = [s for s in series if s["quality"] == quality]
    all_ts = [stats.parse_ts(p["ts"]) for s in series for p in s.get("data", [])]
    global_latest = max([t for t in all_ts if t], default=None)
    analyses = [
        stats.analyze_history_series(s, days, time_scale, global_latest)
        for s in series
    ]
    analyses.sort(key=lambda a: (a.get("city") or "", a.get("quality") or 0))
    it = db.get(item_id) or {"id": item_id}
    return {
        "item": {k: v for k, v in it.items() if not k.startswith("_")},
        "quality": quality,
        "days": days,
        "time_scale": time_scale,
        "cities": city_list,
        "series": analyses,
        "comparison": stats.compare_item_series(analyses),
    }


@app.get("/api/history")
def history(items: str, cities: str | None = None, quality: int | None = None,
            time_scale: int = Query(24), days: int = Query(30, le=365),
            max_age: int = Query(config.HISTORY_TTL, ge=0),
            cache_only: bool = False):
    if time_scale not in (1, 6, 24):
        raise HTTPException(400, "time_scale deve ser 1, 6 ou 24")
    item_ids = _resolve_items(_csv(items) or [])
    city_list = _csv(cities) or config.CITIES
    if cache_only:
        series = _cached_history_series(item_ids, city_list, quality, time_scale, days)
    else:
        series = _api_guard(lambda: aodp.get_history(
            item_ids, city_list, time_scale=time_scale, days=days,
            max_age=max_age))
    if quality is not None:
        series = [s for s in series if s["quality"] == quality]
    for s in series:
        it = db.get(s["item_id"]) or {}
        s["name_pt"] = it.get("pt", s["item_id"])
    return series


@app.get("/api/gold")
def gold(count: int = Query(24, le=720)):
    return _api_guard(lambda: aodp.get_gold(count))


# ---------------------------------------------------------------- watchlist

@app.get("/api/watchlist")
def watchlist():
    items = []
    for w in aodp.watch_list():
        it = db.get(w["item_id"]) or {}
        items.append({"item_id": w["item_id"],
                      "name_pt": it.get("pt", w["item_id"]),
                      "tier": it.get("tier", 0), "ench": it.get("ench", 0)})
    return {"items": items, "count": len(items)}


@app.post("/api/watchlist/{item_id}")
def watchlist_add(item_id: str):
    iid = _resolve_items([item_id])[0]
    aodp.watch_add([iid])
    return {"ok": True, "item_id": iid, "count": len(aodp.watch_list())}


@app.delete("/api/watchlist/{item_id}")
def watchlist_remove(item_id: str):
    aodp.watch_remove([item_id])
    return {"ok": True, "count": len(aodp.watch_list())}


@app.post("/api/collect")
def collect(days: int = Query(30, le=180), cities: str | None = None,
            max_items: int = Query(config.COLLECT_MAX_ITEMS,
                                   le=config.COLLECT_MAX_ITEMS)):
    """Coleta preços + histórico de toda a watchlist (alimenta o Item Lab)."""
    city_list = _csv(cities) or config.CITIES
    result = _api_guard(lambda: aodp.collect(
        cities=city_list, days=days, max_items=max_items, source="manual"))
    return result


@app.get("/api/backtest")
def backtest(premium: bool = True, min_profit: float = Query(500, ge=0),
             max_runs: int = Query(24, le=500)):
    """Backtest de sinal sobre as rodadas de coleta acumuladas.

    Custo ~0,8 s por par de rodadas (reconstrói o mercado por rodada). O
    default 24 responde em ~18 s; aumente max_runs para análises mais fundas.
    """
    from albion import backtest as backtest_mod
    con = _cache_connection()
    if con is None:
        return {"runs_total": 0, "run_pairs_used": 0, "overall": {"n": 0}}
    try:
        metas = {i["id"]: i for i in db.items}
        return backtest_mod.signal_backtest(
            con, aodp.server, metas, premium=premium,
            min_profit=min_profit, max_runs=max_runs)
    finally:
        con.close()


@app.get("/api/survival")
def order_survival(item: str | None = None, city: str | None = None,
                   quality: int | None = Query(None, ge=1, le=5)):
    """Persistência das ordens do topo do livro, medida nos snapshots próprios."""
    con = _cache_connection()
    if con is None:
        return {"sides": {}, "snapshot_pairs": 0}
    try:
        item_id = _resolve_items([item])[0] if item else None
        return survival_mod.persistence(
            con, aodp.server, item_id=item_id, city=city, quality=quality)
    finally:
        con.close()


@app.get("/api/micro")
def micro(view: str = "spread", premium: bool = True,
          min_volume: float = Query(0, ge=0), capital: float | None = None,
          limit: int = Query(40, ge=1, le=200)):
    """Microestrutura p/ a aba Avançado: market-making (spread) e alocação de capital."""
    from albion import microstructure as mc
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        name = lambda i: (db.get(i) or {}).get("pt", i)
        if view == "capital":
            # giro a 100% (teto orientativo) — a taxa de preenchimento real
            # (survival) é pesada p/ a web; use `analyze.py micro capital` p/ ela
            res = mc.capital_allocation(
                con, aodp.server, capital=capital, premium=premium,
                min_liquidity=max(1, min_volume), fill_rate=None, limit=limit)
            for p in res["plan"]:
                p["name_pt"] = name(p["item_id"])
            res["view"] = "capital"
            res["fill_rate"] = None
            return res
        rows = mc.market_making_menu(
            con, aodp.server, premium=premium, min_liquidity=min_volume,
            limit=limit)
        for r in rows:
            r["name_pt"] = name(r["item_id"])
        return {"view": "spread", "rows": rows}
    finally:
        con.close()


@app.get("/api/pvp")
def pvp_meta(view: str = "weapon", days: float = Query(7, ge=0.25, le=30),
             min_fights: int = Query(20, ge=1), limit: int = Query(40, ge=1, le=200)):
    """PvP: meta de builds com taxa de vitória (killboard). view=weapon|builds|overview."""
    from albion import pvp
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        if view == "overview":
            return {"view": "overview", **pvp.overview(con, aodp.server, days=days)}
        if view == "builds":
            return {"view": "builds",
                    **pvp.build_meta(con, aodp.server, days=days,
                                     min_fights=min_fights, limit=limit)}
        return {"view": "weapon",
                **pvp.weapon_meta(con, aodp.server, days=days,
                                  min_fights=min_fights, limit=limit)}
    finally:
        con.close()


def _price_lookups(con, max_age_days=3):
    """(q1, allq) das linhas de prices saneadas (sem âncora) e frescas — base
    das análises do hub Avançado. Espelha analyze.py _clean_price_lookups."""
    from albion.microstructure import clean_price_rows
    cutoff = con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S','now', ?)",
                         [f"-{int(max_age_days)} days"]).fetchone()[0]
    rows = clean_price_rows(
        [dict(r) for r in con.execute(
            "SELECT * FROM prices WHERE server=?", [aodp.server]).fetchall()])
    q1, allq = {}, {}
    for r in rows:
        sp = r.get("sell_price_min") or 0
        if sp <= 0 or (r.get("sell_price_min_date") or "") < cutoff:
            continue
        item, city, q = r["item_id"], r["city"], r["quality"]
        allq[(item, city, q)] = min(allq.get((item, city, q), sp), sp)
        if q == 1:
            q1[(item, city)] = min(q1.get((item, city), sp), sp)
    return q1, allq


def _cheapest_by_item(q1):
    out = {}
    for (item, _c), p in q1.items():
        if item not in out or p < out[item]:
            out[item] = p
    return out


def _market_volume(con, days=7):
    return {i: n for i, n in con.execute(
        """SELECT item_id, SUM(item_count)*1.0/COUNT(DISTINCT substr(ts,1,10))
           FROM history WHERE server=? AND time_scale=24 AND quality=1
             AND item_count>0 AND ts>=date('now', ?) GROUP BY item_id""",
        [aodp.server, f"-{int(days)} days"]).fetchall()}


@app.get("/api/prod")
def prod_view(view: str = "focus", premium: bool = True,
              limit: int = Query(40, ge=1, le=200)):
    """Produção p/ o hub Avançado: prata/foco e refinar-vs-vender (rankings)."""
    from albion import production as prod
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        q1, _ = _price_lookups(con)
        name = lambda i: (db.get(i) or {}).get("pt", i)
        if view == "refine":
            recipes = prod.craft._load("recipes_refining.json")
            best = []
            for rid in recipes:
                r = prod.refine_premium(rid, q1, premium=premium)
                if r:
                    top = r[0]
                    top["name_pt"] = name(rid)
                    best.append(top)
            best.sort(key=lambda r: -r["premium_pct"])
            return {"view": "refine", "rows": best[:limit]}
        rows = prod.focus_efficiency(q1, premium=premium, limit=limit)
        for r in rows:
            r["name_pt"] = name(r["item_id"])
            r["tipo"] = "refino" if r["is_refining"] else "craft"
        return {"view": "focus", "rows": rows}
    finally:
        con.close()


@app.get("/api/demand")
def demand_view(view: str = "burn", days: float = Query(7, ge=0.25, le=30),
                recent: float = Query(2, ge=0.25, le=14),
                limit: int = Query(40, ge=1, le=200)):
    """Demanda (killboard) p/ o hub: consumíveis, qualidade destruída, meta."""
    from albion import demand as dm
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        name = lambda i: (db.get(i) or {}).get("pt", i)
        if view == "meta":
            rows = dm.meta_shift(con, aodp.server, days=days, recent=recent,
                                 limit=limit)
            return {"view": "meta", "rows": rows}
        q1, allq = _price_lookups(con)
        if view == "quality":
            pq = {}
            for (item, _c, q), p in allq.items():
                pq[(item, q)] = min(pq.get((item, q), p), p)
            rows = dm.destroyed_quality(con, aodp.server, days=days,
                                        price_q=lambda i, q: pq.get((i, q)),
                                        limit=limit)
            for r in rows:
                r["name_pt"] = name(r["item_id"])
            return {"view": "quality", "rows": rows}
        price_item = _cheapest_by_item(q1)
        vol = _market_volume(con, days=days)
        rows = dm.consumable_burn(con, aodp.server, days=days,
                                  price_of=price_item.get, vol_of=vol.get,
                                  limit=limit)
        for r in rows:
            r["name_pt"] = name(r["item_id"])
        return {"view": "burn", "rows": rows}
    finally:
        con.close()


@app.get("/api/guild")
def guild_view(view: str = "watch", days: float = Query(7, ge=0.25, le=30),
               premium: bool = True, limit: int = Query(40, ge=1, le=200)):
    """Guild p/ o hub: ROI de coleta, cesta de regear, make-or-buy."""
    from albion import guild as gd
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        name = lambda i: (db.get(i) or {}).get("pt", i)
        if view == "kit":
            res = gd.soldier_kit_index(con, aodp.server, days=days)
            res["basket"] = [{"item_id": i, "name_pt": name(i), "weight": w}
                             for i, w in res.get("basket", [])]
            res["view"] = "kit"
            return res
        q1, _ = _price_lookups(con)
        if view == "makeorbuy":
            rows = gd.make_or_buy(con, aodp.server,
                                  price_of=lambda i, c: q1.get((i, c)),
                                  days=days, premium=premium, limit=limit)
            for r in rows:
                r["name_pt"] = name(r["item_id"])
            return {"view": "makeorbuy", "rows": rows}
        price_item = _cheapest_by_item(q1)
        res = gd.watchlist_roi(con, aodp.server, price_of=price_item.get,
                               days=days, limit=limit)
        for r in res.get("add", []):
            r["name_pt"] = name(r["item_id"])
        res["view"] = "watch"
        res["rows"] = res.pop("add", [])
        return res
    finally:
        con.close()


def _clean_prows(con):
    """Linhas de prices saneadas (sem âncora) no formato dos rows da API —
    base das análises de logística (carga, escada de qualidade, BM, reposição)."""
    from albion.microstructure import clean_price_rows
    return clean_price_rows([dict(r) for r in con.execute(
        "SELECT * FROM prices WHERE server=?", [aodp.server]).fetchall()])


def _metas_for(rows):
    return {iid: m for iid in {r["item_id"] for r in rows} if (m := db.get(iid))}


def _history_daily_rows(con, item_ids=None, cities=None, days=120,
                        with_count=False):
    """(item, city, dia, avg_price[, item_count]) do history q1 — espelha
    analyze.py _history_daily, sobre a conexão somente-leitura do servidor."""
    where = ["server=?", "time_scale=24", "quality=1", "avg_price>0",
             "ts >= date('now', ?)"]
    params = [aodp.server, f"-{int(days)} days"]
    if item_ids:
        where.append(f"item_id IN ({_placeholders(item_ids)})")
        params += list(item_ids)
    if cities:
        where.append(f"city IN ({_placeholders(cities)})")
        params += list(cities)
    cols = "item_id, city, substr(ts,1,10) AS day, avg_price" + (
        ", item_count" if with_count else "")
    return con.execute(
        f"""SELECT {cols} FROM history WHERE {' AND '.join(where)}
            ORDER BY item_id, city, day""", params).fetchall()


@app.get("/api/logi")
def logi_view(view: str = "bm", premium: bool = True,
              days: float = Query(7, ge=0.25, le=30),
              limit: int = Query(40, ge=1, le=200)):
    """Logística p/ o hub Avançado: prêmio do Mercado Negro, escada de
    qualidade e mapa de reposição (killboard × onde abastecer)."""
    from albion import logistics as logi
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        prows = _clean_prows(con)
        metas = _metas_for(prows)
        if view == "ladder":
            rows = logi.quality_ladder(prows, metas, premium=premium, limit=limit)
            for r in rows:
                r["quals"] = ",".join(map(str, r.get("qualities", [])))
        elif view == "restock":
            demand = con.execute(
                """SELECT item_id, SUM(victim_units) AS u FROM item_demand_daily
                   WHERE server=? AND day >= date('now', ?)
                   GROUP BY item_id HAVING u>0 ORDER BY u DESC LIMIT 400""",
                [aodp.server, f"-{int(days)} days"]).fetchall()
            rows = logi.restock_map(demand, prows, metas, premium=premium,
                                    limit=limit) if demand else []
        else:  # bm
            rows = logi.black_market_premium(prows, metas, premium=premium,
                                             limit=limit)
        return {"view": view, "rows": rows}
    finally:
        con.close()


@app.get("/api/risk")
def risk_view(view: str = "profile", days: int = Query(120, ge=30, le=365),
              min_points: int = Query(30, ge=10, le=200),
              limit: int = Query(60, ge=1, le=200),
              cat: str | None = None, sub: str | None = None,
              tier_min: int | None = None, tier_max: int | None = None):
    """Risco & portfólio p/ o hub: perfil de risco (vol/drawdown/VaR, com IC
    bootstrap, EWMA, shrinkage e selo calibrado) e correlação de retornos."""
    from albion import risk
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        item_ids = None
        if cat or sub or tier_min or tier_max:
            item_ids = [i["id"] for i in db.filter(
                cat=cat, sub=sub, tier_min=tier_min, tier_max=tier_max)]
        else:
            # sem filtro: limita o universo aos itens mais líquidos (top vol 7d).
            # O bootstrap de IC por série é caro; rodá-lo sobre milhares de séries
            # esparsas levaria ~1 min e mediria risco de itens que ninguém troca.
            vol = _market_volume(con, days=7)
            item_ids = [i for i, _v in sorted(
                vol.items(), key=lambda kv: -kv[1])[:250]]
        # item_ids vazio (filtro sem match OU sem volume no cache) NÃO pode cair
        # no caminho ilimitado de _history_daily_rows (que ignoraria um IN vazio
        # e varreria tudo, ~1 min). Curto-circuita p/ vazio.
        if not item_ids:
            return {"view": view, "rows": []}
        rows = _history_daily_rows(con, item_ids, None, days)
        name = lambda i: (db.get(i) or {}).get("pt", i)
        if not rows:
            return {"view": view, "rows": []}
        if view == "corr":
            by_item = {}
            for item, _city, day, price in rows:
                by_item.setdefault(item, {}).setdefault(day, []).append(price)
            series = {it: {d: sum(v) / len(v) for d, v in dd.items()}
                      for it, dd in by_item.items()}
            # 80 séries mais ricas (mais dias) p/ a matriz — melhora o default
            # do CLI (primeiras 80 arbitrárias) sem mudar a metodologia
            series = dict(sorted(series.items(),
                                 key=lambda kv: -len(kv[1]))[:80])
            pairs = risk.correlation_pairs(series, min_common=min_points,
                                           limit=limit)
            for p in pairs:
                p["a_pt"], p["b_pt"] = name(p["a"]), name(p["b"])
                p["tipo"] = ("andam juntos" if p["corr"] >= 0.6 else
                             ("hedge" if p["corr"] <= 0.1 else "fraca"))
            return {"view": "corr", "rows": pairs}
        # profile — uma série por ITEM (a cidade com mais pontos = mais
        # confiável). O bootstrap de IC é caro; um perfil por item, em vez de
        # um por (item,cidade), deixa a tabela responsiva e o ranking limpo.
        series = {}
        for item, city, _day, price in rows:
            series.setdefault((item, city), []).append(price)
        best = {}
        for (item, city), prices in series.items():
            if item not in best or len(prices) > len(best[item][1]):
                best[item] = (city, prices)
        out = []
        for item, (city, prices) in best.items():
            rp = risk.risk_profile(prices, min_points=min_points)
            if rp:
                out.append({"item_id": item, "name_pt": name(item),
                            "city": city, **rp})
        if not out:
            return {"view": "profile", "rows": []}
        bands = risk.calibrate_bands([r["vol_annual_pct"] / 100 for r in out])
        prior = sorted(r["vol_annual_pct"] for r in out)[len(out) // 2]
        for r in out:
            shrunk = risk.shrink(r["vol_annual_pct"], r["points"] - 1, prior, k=20)
            r["vol_shrunk_pct"] = round(shrunk, 1)
            r["risk_label"] = risk.label_for(shrunk / 100, bands)
            r["vol_ci"] = (f"{r['vol_ci_pct'][0]:.0f}–{r['vol_ci_pct'][1]:.0f}"
                           if r.get("vol_ci_pct") else "—")
            r.pop("vol_ci_pct", None)
        order = {"seguro": 0, "médio": 1, "especulativo": 2}
        out.sort(key=lambda r: (order.get(r["risk_label"], 9),
                                -r["vol_shrunk_pct"]))
        return {"view": "profile", "rows": out[:limit]}
    finally:
        con.close()


@app.get("/api/item_signals")
def item_signals(item: str, days: int = Query(180, ge=30, le=400)):
    """Risco + reversão + regime + previsibilidade de um item, da melhor série
    de history (cidade com mais pontos). Para a sub-aba 'Risco & Previsão'."""
    from albion import risk, forecast as fc
    try:
        ids = _resolve_items([item])     # resolve id/nome; 404 se desconhecido
    except HTTPException:
        raise HTTPException(404, "item nao encontrado")
    iid = ids[0]
    con = _cache_connection()
    if con is None:
        return {"item_id": iid, "available": False}
    try:
        rows = con.execute(
            """SELECT city, substr(ts,1,10) AS day, avg_price FROM history
               WHERE server=? AND time_scale=24 AND quality=1 AND avg_price>0
                 AND ts >= date('now', ?) AND item_id=?
               ORDER BY city, day""",
            [aodp.server, f"-{int(days)} days", iid]).fetchall()
    finally:
        con.close()
    by_city = {}
    for city, _day, price in rows:
        by_city.setdefault(city, []).append(price)
    if not by_city:
        return {"item_id": iid, "available": False,
                "note": "sem histórico para o item — colete-o primeiro"}
    best_city = max(by_city, key=lambda c: len(by_city[c]))
    series = by_city[best_city]
    return {
        "item_id": iid, "available": True, "city": best_city,
        "points": len(series),
        "risk": risk.risk_profile(series),
        "reversion": fc.mean_reversion(series),
        "regime": fc.structural_break(series, permutations=100)
        if len(series) >= 20 else None,
        "predictability": fc.predictability(series),
    }


# ------------------------------------------------------- coleta automática
def _auto_collect_loop():
    """Cadência dupla: killboard (intel) mais frequente que o mercado."""
    tick = 60 * max(1, min(config.AUTO_INTEL_INTERVAL_MIN,
                           config.AUTO_COLLECT_INTERVAL_MIN))
    last_market = 0.0
    last_signal_log = 0.0
    last_gold = 0.0
    last_prune = time.time()  # não poda logo no boot; espera o intervalo
    while True:
        time.sleep(tick)
        try:
            from albion import gameinfo
            gameinfo.ingest_events(aodp)
            gameinfo.ingest_battles(aodp)
            gameinfo.aggregate_demand_daily(aodp)
            # snapshot horário dos sinais -> demand_signal_log (backtest)
            if time.time() - last_signal_log >= 3600:
                with aodp.db_lock:
                    sigs = gameinfo.demand_price_divergence(
                        aodp.db, aodp.server)
                if sigs:
                    gameinfo.log_signals(aodp, sigs)
                last_signal_log = time.time()
        except Exception:
            pass  # registrado em public_data_runs quando possível
        try:
            if (time.time() - last_market
                    >= config.AUTO_COLLECT_INTERVAL_MIN * 60
                    and aodp.watch_list()):
                aodp.collect(source="auto")
                last_market = time.time()
                # ordens acompanham a cadência do mercado (insumo principal);
                # regenerar a cada tick só inflaria service_orders
                _generate_service_orders()
        except Exception:
            pass  # registrado em collection_runs pelo collect()
        try:
            # B7: 1x/dia estende a janela do ouro (a AODP serve N pontos por
            # count) p/ ~30 d+ — fator macro para risco/previsão
            if time.time() - last_gold >= 24 * 3600:
                aodp.get_gold(count=1000)
                last_gold = time.time()
        except Exception:
            pass
        try:
            # poda diária: agrega snapshots brutos antigos e limita a tabela
            # (sem VACUUM para não segurar o lock; o espaço é reclamado pelo
            # `analyze.py prune` manual). Causa-raiz da lentidão se não rodar.
            if (config.AUTO_PRUNE_INTERVAL_H > 0 and
                    time.time() - last_prune >= config.AUTO_PRUNE_INTERVAL_H * 3600):
                aodp.snapshot_prune(vacuum=False)
                # mantém as estatísticas do planejador frescas (sem isto o
                # planner ignora os índices e as consultas de PvP/meta sobre
                # kill_event_equipment caem de ~0,5 s para ~15 s)
                with aodp.db_lock:
                    aodp.db.execute("PRAGMA optimize")
                last_prune = time.time()
        except Exception:
            pass


@app.on_event("startup")
def _start_auto_collect():
    from albion import gameinfo
    gameinfo.ensure_assumptions(aodp)
    aodp.sync_static_items(db.items)
    gameinfo.seed_combat_tags(aodp)
    if config.AUTO_COLLECT_INTERVAL_MIN > 0:
        threading.Thread(target=_auto_collect_loop, daemon=True,
                         name="auto-collect").start()


@app.get("/api/intel/risk")
def intel_risk(days: float = Query(1, gt=0, le=30)):
    """Risco estrutural: classes de morte, vítimas econômicas e ZvZ."""
    from albion import gameinfo
    con = _cache_connection()
    if con is None:
        return {}
    try:
        return {
            **gameinfo.risk_summary(con, aodp.server, days=days),
            "classificacao": gameinfo.classification_summary(
                con, aodp.server, days=days),
        }
    finally:
        con.close()


@app.get("/api/intel/validate")
def intel_validate():
    from albion import gameinfo
    con = _cache_connection()
    if con is None:
        return {"horizons": []}
    try:
        return {"horizons": [
            gameinfo.validate_signals(con, aodp.server, horizon_days=h)
            for h in (1, 3)]}
    finally:
        con.close()


@app.get("/api/intel/signals")
def intel_signals(limit: int = Query(20, le=100),
                  min_units: float = Query(10, ge=0)):
    """Divergência demanda × preço: destruição subindo, preço atrasado."""
    from albion import gameinfo
    con = _cache_connection()
    if con is None:
        return {"signals": [], "demand_days": 0}
    try:
        sigs = gameinfo.demand_price_divergence(
            con, aodp.server, min_units_day=min_units, limit=limit)
        days = con.execute(
            "SELECT COUNT(DISTINCT day) FROM item_demand_daily WHERE server=?",
            [aodp.server]).fetchone()[0]
    finally:
        con.close()
    for s in sigs:
        meta = db.get(s["item_id"]) or {}
        s["name_pt"] = meta.get("pt", s["item_id"])
        s["tier"] = meta.get("tier", 0)
        s["ench"] = meta.get("ench", 0)
    return {"signals": sigs, "demand_days": days}


@app.get("/api/intel/top")
def intel_top(days: float = Query(1, gt=0, le=30), role: str = "victim",
              inventory: bool = False, limit: int = Query(20, le=100)):
    """Índice de destruição: itens mais perdidos/usados em kills recentes."""
    from albion import gameinfo
    if role not in ("victim", "killer"):
        raise HTTPException(400, "role deve ser victim ou killer")
    con = _cache_connection()
    if con is None:
        return {"items": [], "status": {}}
    try:
        top = gameinfo.destruction_top(
            con, aodp.server, days=days, role=role,
            include_inventory=inventory, limit=limit)
        status = gameinfo.intel_status(con, aodp.server)
    finally:
        con.close()
    for t in top:
        meta = db.get(t["item_id"]) or {}
        t["name_pt"] = meta.get("pt", t["item_id"])
        t["tier"] = meta.get("tier", 0)
        t["ench"] = meta.get("ench", 0)
    return {"items": top, "status": status}


@app.get("/api/service-orders")
def service_orders():
    """Ordens de serviço abertas (somente leitura — gerar é no POST refresh)."""
    return _service_orders_payload()


@app.post("/api/service-orders/refresh")
def service_orders_refresh():
    generated = _generate_service_orders()
    payload = _service_orders_payload()
    payload["generated"] = generated
    return payload


# ---------------------------------------------------------------- ícones
# O serviço de render (render.albiononline.com) bloqueia clientes com TLS do
# OpenSSL (curl/httpx) via Cloudflare; navegadores reais passam. Este proxy é
# o fallback do frontend: baixa via Schannel (PowerShell) e cacheia em disco.
ICONS_DIR = ROOT / "data" / "icons"
ICONS_DIR.mkdir(parents=True, exist_ok=True)
_ICON_ID_RE = re.compile(r"^[A-Za-z0-9_@\-\.]+$")
_icon_sem = threading.Semaphore(4)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
               " (KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _download_icon(url: str, dest: Path) -> bool:
    try:
        r = httpx.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=15,
                      follow_redirects=True)
        if r.status_code == 200 and r.content[:4] == b"\x89PNG":
            dest.write_bytes(r.content)
            return True
    except httpx.HTTPError:
        pass
    tmp = dest.with_suffix(".tmp.png")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Net.ServicePointManager]::SecurityProtocol = "
             "[Net.ServicePointManager]::SecurityProtocol -bor 3072; "
             f"Invoke-WebRequest -Uri '{url}' -OutFile '{tmp}'"
             f" -UserAgent '{_BROWSER_UA}' -TimeoutSec 20 -UseBasicParsing"],
            capture_output=True, timeout=30)
        if r.returncode != 0:
            (ICONS_DIR / "icon_errors.log").open("ab").write(
                b"rc=%d url=%s\n" % (r.returncode, url.encode())
                + r.stderr[:2000] + b"\n---\n")
        if tmp.exists() and tmp.read_bytes()[:4] == b"\x89PNG":
            tmp.replace(dest)
            return True
        tmp.unlink(missing_ok=True)
    except (subprocess.SubprocessError, OSError) as e:
        # nunca usar print aqui: mensagens do Windows em PT-BR têm acentos e
        # console cp1252 lançaria UnicodeEncodeError dentro do handler
        (ICONS_DIR / "icon_errors.log").open("ab").write(
            ("exc url=%s: %r\n---\n" % (url, e)).encode("utf-8", "replace"))
    return False


@app.get("/icon/{item_id}")
def icon(item_id: str, quality: int = Query(0, ge=0, le=5),
         size: int = Query(64, ge=16, le=217)):
    if not _ICON_ID_RE.match(item_id):
        raise HTTPException(400, "id de item inválido")
    dest = ICONS_DIR / f"{item_id}_q{quality}_s{size}.png"
    if not dest.exists():
        url = (f"https://render.albiononline.com/v1/item/{quote(item_id)}.png"
               f"?size={size}" + (f"&quality={quality}" if quality > 1 else ""))
        with _icon_sem:
            if not dest.exists() and not _download_icon(url, dest):
                return Response(status_code=404)
    return FileResponse(dest, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    serve_host = "0.0.0.0" if config.SERVE_LAN else HOST
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    print(f"Mercado Albion (Américas) — http://{HOST}:{PORT}"
          + (" (acessível pela rede local)" if config.SERVE_LAN else ""))
    uvicorn.run(app, host=serve_host, port=PORT, log_level="warning")

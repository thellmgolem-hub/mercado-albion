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
           ench: int | None = None, limit: int = Query(30, le=200)):
    return db.search(q, cat=cat, sub=sub, tier_min=tier_min,
                     tier_max=tier_max, ench=ench, limit=limit)


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
        return {
            "source": "cache",
            "items_considered": len(item_ids),
            "price_rows_considered": len(rows),
            "coverage": coverage,
            "history_until": history_until,
            "opportunities": annotated[:limit],
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
         max_items: int = Query(300, le=800), limit: int = Query(100, le=500),
         with_volume: bool = True, max_age: int = Query(config.PRICES_TTL, ge=0)):
    """Escaneia uma categoria inteira em busca de oportunidades de flip."""
    if buy_mode not in ("instant", "order") or sell_mode not in ("instant", "order"):
        raise HTTPException(400, "buy_mode/sell_mode deve ser 'instant' ou 'order'")
    items = db.filter(cat=cat, sub=sub, tier_min=tier_min, tier_max=tier_max,
                      ench_list=_csv_int(ench), limit=max_items)
    if not items:
        return {"items_scanned": 0, "opportunities": []}
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

    return {"items_scanned": len(item_ids), "opportunities": opps}


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
            max_items: int = Query(config.COLLECT_MAX_ITEMS, le=600)):
    """Coleta preços + histórico de toda a watchlist (alimenta o Item Lab)."""
    city_list = _csv(cities) or config.CITIES
    result = _api_guard(lambda: aodp.collect(
        cities=city_list, days=days, max_items=max_items, source="manual"))
    return result


@app.get("/api/backtest")
def backtest(premium: bool = True, min_profit: float = Query(500, ge=0),
             max_runs: int = Query(60, le=500)):
    """Backtest de sinal sobre as rodadas de coleta acumuladas."""
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


# ------------------------------------------------------- coleta automática
def _auto_collect_loop():
    interval = config.AUTO_COLLECT_INTERVAL_MIN * 60
    while True:
        time.sleep(interval)
        try:
            if aodp.watch_list():
                aodp.collect(source="auto")
        except Exception:
            pass  # erro já registrado em collection_runs pelo collect()


@app.on_event("startup")
def _start_auto_collect():
    if config.AUTO_COLLECT_INTERVAL_MIN > 0:
        threading.Thread(target=_auto_collect_loop, daemon=True,
                         name="auto-collect").start()


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
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    print(f"Mercado Albion (Américas) — http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

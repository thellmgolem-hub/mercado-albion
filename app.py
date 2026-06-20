# -*- coding: utf-8 -*-
"""Servidor local do app de mercado do Albion Online (Américas).

Rodar:  python app.py        (abre o navegador em http://127.0.0.1:8528)
"""
import hmac
import os
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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from albion import config
from albion import stats
from albion import store
from albion.auth import (ADMIN_ROLES, OPERATOR_ROLES, PROFILES, ROLES,
                         AuthError, AuthManager)
from albion.client import AODP
from albion.flips import age_minutes, compute_flips, where_to_sell
from albion.items import ItemDB

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8528

app = FastAPI(title="Mercado Albion — Américas")
db = ItemDB()
aodp = AODP(server=config.DEFAULT_SERVER)
auth_manager = AuthManager(aodp.db, aodp.db_lock)
SAFE_ROYAL_CITIES = [c for c in config.ROYAL_CITIES if c != "Caerleon"]


def _maybe_bootstrap_admin():
    """Cria o admin inicial na 1ª subida da nuvem, sem shell.

    Se ALBION_BOOTSTRAP_ADMIN=<usuario> e ainda não há admin, cria a conta e
    IMPRIME a senha temporária no log (visível no painel do host). O leigo lê o
    log, entra, e troca a senha (must_change_password já força isso). No deploy
    local normal a variável fica vazia e nada acontece — usa-se a CLI.
    """
    name = os.environ.get("ALBION_BOOTSTRAP_ADMIN", "").strip()
    if not name:
        return
    try:
        if auth_manager.has_admin():
            return
        res = auth_manager.bootstrap_admin(name)
        print("=" * 56, flush=True)
        print(f"[BOOTSTRAP] admin '{res['username']}' criado.", flush=True)
        print(f"[BOOTSTRAP] SENHA TEMPORARIA: {res['temporary_password']}",
              flush=True)
        print("[BOOTSTRAP] entre, troque a senha, e remova a variavel "
              "ALBION_BOOTSTRAP_ADMIN.", flush=True)
        print("=" * 56, flush=True)
    except Exception as e:  # nunca derruba o boot por causa do bootstrap
        print(f"[BOOTSTRAP] ignorado: {e!r}", flush=True)


_maybe_bootstrap_admin()

# /api/sweep e /api/intel-sweep são tocados por cron externo (sem sessão) —
# protegidos por token próprio
PUBLIC_AUTH_PATHS = {"/api/auth/login", "/api/auth/bootstrap-status",
                     "/api/sweep", "/api/intel-sweep"}
PROTECTED_DOC_PATHS = {"/docs", "/redoc", "/openapi.json"}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    device_label: str | None = Field(default=None, max_length=64)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class AccountCreateBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str | None = Field(default=None, max_length=80)
    albion_nick: str | None = Field(default=None, max_length=80)
    discord_nick: str | None = Field(default=None, max_length=80)
    role: str = "member"
    profiles: list[str] = Field(default_factory=list)


class AccountUpdateBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    albion_nick: str | None = Field(default=None, max_length=80)
    discord_nick: str | None = Field(default=None, max_length=80)
    role: str | None = None
    active: bool | None = None
    profiles: list[str] | None = None


def _auth_json(error: AuthError):
    return JSONResponse({"detail": error.message, "code": error.code},
                        status_code=error.status,
                        headers={"Cache-Control": "no-store"})


def _cookie_secure(request: Request):
    return config.AUTH_COOKIE_SECURE or request.url.scheme == "https"


def _set_login_cookies(response, request, result):
    secure = _cookie_secure(request)
    response.set_cookie(
        config.AUTH_SESSION_COOKIE, result["session_token"],
        max_age=7 * 86400, httponly=True, secure=secure,
        samesite="strict", path="/")
    if result.get("device_token"):
        response.set_cookie(
            config.AUTH_DEVICE_COOKIE, result["device_token"],
            max_age=90 * 86400, httponly=True, secure=secure,
            samesite="strict", path="/")


def _clear_auth_cookies(response):
    response.delete_cookie(config.AUTH_SESSION_COOKIE, path="/")
    # O dispositivo permanece no logout; e o vinculo de um PC, nao a sessao.


def _require_role(request: Request, allowed):
    if not config.AUTH_REQUIRED:
        return {"id": 0, "username": "local", "role": "admin"}
    account = getattr(request.state, "auth", {}).get("account")
    if not account or account.get("role") not in allowed:
        raise AuthError("Voce nao tem permissao para esta operacao.",
                        "forbidden", 403)
    return account


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api/") or path in PROTECTED_DOC_PATHS
    public = path in PUBLIC_AUTH_PATHS
    if config.AUTH_REQUIRED and protected and not public:
        try:
            if not auth_manager.has_admin():
                raise AuthError(
                    "Nenhum administrador configurado. Execute "
                    "manage_accounts.py bootstrap.", "bootstrap_required", 503)
            ctx = auth_manager.authenticate(
                request.cookies.get(config.AUTH_SESSION_COOKIE),
                request.cookies.get(config.AUTH_DEVICE_COOKIE))
            request.state.auth = ctx
            password_paths = {
                "/api/auth/me", "/api/auth/logout",
                "/api/auth/change-password",
            }
            if (ctx["account"]["must_change_password"]
                    and path not in password_paths):
                raise AuthError(
                    "Troque a senha temporaria antes de usar a plataforma.",
                    "password_change_required", 403)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                csrf = request.headers.get("x-csrf-token")
                if not auth_manager.verify_csrf(ctx, csrf):
                    raise AuthError("Token CSRF ausente ou invalido.",
                                    "csrf_invalid", 403)
        except AuthError as exc:
            return _auth_json(exc)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://render.albiononline.com data:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'self'")
    return response


@app.exception_handler(AuthError)
async def _auth_exception_handler(_request, exc):
    return _auth_json(exc)


@app.get("/api/auth/bootstrap-status")
def auth_bootstrap_status():
    # NÃO expõe account_count a não-autenticados (era reconhecimento numa LAN);
    # o frontend só usa ready + command.
    return {"ready": auth_manager.has_admin(),
            "command": ".\\.venv\\Scripts\\python.exe -B "
                       "manage_accounts.py bootstrap --username admin"}


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request):
    if not auth_manager.has_admin():
        raise AuthError("Crie o primeiro administrador pela CLI.",
                        "bootstrap_required", 503)
    result = auth_manager.login(
        body.username, body.password,
        request.cookies.get(config.AUTH_DEVICE_COOKIE), body.device_label)
    response = JSONResponse({
        "account": result["account"], "csrf": result["csrf_token"],
        "roles": ROLES, "profiles_catalog": PROFILES,
    }, headers={"Cache-Control": "no-store"})
    _set_login_cookies(response, request, result)
    return response


@app.get("/api/auth/me")
def auth_me(request: Request):
    session_token = request.cookies.get(config.AUTH_SESSION_COOKIE)
    csrf = auth_manager.rotate_csrf(session_token)
    return JSONResponse({
        "account": request.state.auth["account"], "csrf": csrf,
        "roles": ROLES, "profiles_catalog": PROFILES,
    }, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    account = request.state.auth["account"]
    token = request.cookies.get(config.AUTH_SESSION_COOKIE)
    auth_manager.logout(token, account["id"])
    response = JSONResponse({"ok": True},
                            headers={"Cache-Control": "no-store"})
    _clear_auth_cookies(response)
    return response


@app.post("/api/auth/change-password")
def auth_change_password(body: ChangePasswordBody, request: Request):
    account = request.state.auth["account"]
    auth_manager.change_password(
        account["id"], body.current_password, body.new_password)
    response = JSONResponse(
        {"ok": True, "reauthenticate": True},
        headers={"Cache-Control": "no-store"})
    _clear_auth_cookies(response)
    return response


@app.get("/api/admin/accounts")
def admin_accounts(request: Request):
    _require_role(request, ADMIN_ROLES)
    return {"accounts": auth_manager.list_accounts(),
            "roles": ROLES, "profiles_catalog": PROFILES}


@app.post("/api/admin/accounts")
def admin_create_account(body: AccountCreateBody, request: Request):
    actor = _require_role(request, ADMIN_ROLES)
    created = auth_manager.create_account(
        actor["id"], body.username, role=body.role,
        profiles=body.profiles, display_name=body.display_name,
        albion_nick=body.albion_nick, discord_nick=body.discord_nick)
    # A senha temporaria so existe nesta resposta.
    return {**created, "account": auth_manager.get_account(
        created["account_id"])}


@app.patch("/api/admin/accounts/{account_id}")
def admin_update_account(account_id: int, body: AccountUpdateBody,
                         request: Request):
    actor = _require_role(request, ADMIN_ROLES)
    supplied = getattr(body, "model_fields_set",
                       getattr(body, "__fields_set__", set()))
    kwargs = {}
    for field in ("role", "active", "profiles", "display_name",
                  "albion_nick", "discord_nick"):
        if field in supplied:
            kwargs[field] = getattr(body, field)
    return {"account": auth_manager.update_account(
        actor["id"], account_id, **kwargs)}


@app.delete("/api/admin/accounts/{account_id}")
def admin_disable_account(account_id: int, request: Request):
    actor = _require_role(request, ADMIN_ROLES)
    return {"account": auth_manager.update_account(
        actor["id"], account_id, active=False)}


@app.post("/api/admin/accounts/{account_id}/reset-password")
def admin_reset_password(account_id: int, request: Request):
    actor = _require_role(request, ADMIN_ROLES)
    return auth_manager.reset_password(actor["id"], account_id)


@app.post("/api/admin/accounts/{account_id}/reset-device")
def admin_reset_device(account_id: int, request: Request):
    actor = _require_role(request, ADMIN_ROLES)
    auth_manager.reset_device(actor["id"], account_id)
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(request: Request, limit: int = Query(200, ge=1, le=1000)):
    _require_role(request, ADMIN_ROLES)
    return {"events": auth_manager.audit_log(limit)}


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
        if not it and "@" in i and "_LEVEL" not in i:
            # alias de refinado encantado X@n -> id real X_LEVELn@n
            base, _, e = i.partition("@")
            if e and e != "0":
                it = db.get(f"{base}_LEVEL{e}@{e}")
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
    """Resumo do cache para diagnostico rapido (SQLite local ou Postgres)."""
    # Tabelas com carimbo de tempo em epoch (fetched_at) e a 'gold' (ts texto).
    epoch_tables = ["prices", "history", "fetch_log"]
    tables = {}
    con = _cache_connection()
    cache_exists = con is not None
    if con is not None:
        try:
            for name in epoch_tables:
                try:
                    row = con.execute(
                        f"SELECT COUNT(*), MAX(fetched_at) FROM {name}"
                    ).fetchone()
                except Exception:
                    tables[name] = {"rows": 0, "latest": None}
                    continue
                latest = row[1]
                if latest:  # epoch -> ISO UTC
                    latest = datetime.utcfromtimestamp(
                        float(latest)).strftime("%Y-%m-%d %H:%M:%S")
                tables[name] = {"rows": row[0] or 0, "latest": latest}
            try:
                row = con.execute("SELECT COUNT(*), MAX(ts) FROM gold").fetchone()
                tables["gold"] = {"rows": row[0] or 0, "latest": row[1]}
            except Exception:
                tables["gold"] = {"rows": 0, "latest": None}
        finally:
            con.close()
    db_path = ROOT / "data" / "cache.db"
    size = db_path.stat().st_size if (store.backend() == "sqlite"
                                      and db_path.exists()) else 0
    return {
        "server": aodp.server,
        "backend": store.backend(),
        "item_count": len(db.items),
        "cache_exists": cache_exists,
        "cache_size_bytes": size,
        "tables": tables,
    }


_INTEL_LOCK = threading.Lock()


def _check_sweep_token(token: str):
    """Gate dos endpoints de sweep: fail-closed em prod sem token; comparação em
    tempo constante por BYTES (str não-ASCII em compare_digest levantaria 500)."""
    if store.backend() != "sqlite" and not config.SWEEP_TOKEN:
        raise HTTPException(503, "sweep desabilitado: defina ALBION_SWEEP_TOKEN")
    if config.SWEEP_TOKEN and not hmac.compare_digest(
            token.encode("utf-8", "ignore"),
            config.SWEEP_TOKEN.encode("utf-8")):
        raise HTTPException(403, "token de sweep invalido")


def _market_universe():
    """Universo de varredura: TODO item negociável, ordem estável.

    Exclui só 'vanity' (skins não-vendáveis) e subcategorias-lixo (quest/loot),
    além de tier 0. Inclui diários, labourers, mobília, mapas — tudo que tem
    mercado na AODP. A ordem por id é determinística (o cursor é um offset aqui).
    """
    skip_cat = config.SWEEP_SKIP_CATEGORIES
    skip_sub = config.SWEEP_SKIP_SUBS
    return sorted(
        it["id"] for it in db.items
        if it.get("cat") not in skip_cat and it.get("sub") not in skip_sub
        and (it.get("tier") or 0) >= 1)


@app.get("/api/sweep")
@app.post("/api/sweep")
def sweep(token: str = "",
          count: int = Query(config.SWEEP_ITEMS_PER_TICK, ge=10, le=400)):
    """Um TOQUE do sweep fatiado (tocado por cron a cada minuto).

    Avança um cursor pelo universo de mercado, busca a fatia (preços sempre
    frescos + histórico se velho) e grava no store. Em ~3 h varre tudo e recicla.
    Protegido por ALBION_SWEEP_TOKEN (se definido).
    """
    _check_sweep_token(token)
    universe = _market_universe()
    n = len(universe)
    if not n:
        return {"ok": False, "note": "universo vazio"}
    # reserva ATÔMICA da fatia: avança o cursor antes de buscar, então dois
    # ticks concorrentes do cron pegam trechos diferentes (sem duplicar trabalho)
    res = aodp.sweep_reserve(n, count)
    slice_ids = universe[res["start"]:res["start"] + res["take"]]
    if not slice_ids:
        return {"ok": True, "universe": n, "took": 0,
                "next_cursor": res["new_cursor"], "cycle": res["cycle"]}
    pr = _api_guard(lambda: aodp.get_prices(slice_ids, max_age=0))
    hi = _api_guard(lambda: aodp.get_history(
        slice_ids, time_scale=24, days=config.SWEEP_HISTORY_DAYS,
        max_age=config.SWEEP_HISTORY_TTL))
    return {
        "ok": True, "universe": n, "from_cursor": res["start"],
        "took": len(slice_ids), "next_cursor": res["new_cursor"],
        "cycle": res["cycle"], "price_rows": len(pr),
        "history_series": len(hi),
        "progress_pct": round(100 * res["new_cursor"] / n, 1)
        if res["new_cursor"] else 100.0,
    }


@app.get("/api/intel-sweep")
@app.post("/api/intel-sweep")
def intel_sweep(token: str = ""):
    """Um toque do sweep de KILLBOARD magro (tocado por cron, ~10 em 10 min).

    Pagina o gameinfo e agrega o equipamento das vítimas em kill_demand_daily
    (sem eventos crus), depois poda dias além da retenção. Alimenta Guild
    (fazer-vs-comprar, regear, ranking de destruição) e Logística (reposição).
    """
    from albion import gameinfo
    _check_sweep_token(token)
    # serializa a ingestão: dois toques concorrentes leriam o mesmo checkpoint e
    # contariam a demanda em DOBRO. Se já está rodando, devolve sem reprocessar.
    if not _INTEL_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "ja em execucao"}
    try:
        res = _api_guard(lambda: gameinfo.ingest_demand_lean(aodp))
        # poda best-effort: rollback defensivo se o DELETE falhar — não pode
        # deixar a conexão gravável compartilhada em transação abortada (PG).
        cutoff = store.cutoff_iso(config.KILL_DEMAND_RETENTION_DAYS)
        try:
            with aodp.db_lock:
                aodp.db.execute(
                    "DELETE FROM kill_demand_daily WHERE server=? AND day < ?",
                    [aodp.server, cutoff])
                aodp.db.commit()
        except Exception:
            try:
                aodp.db.rollback()
            except Exception:
                pass
        if res and res.get("saturated"):
            print("[intel-sweep] SATURADO: burst > paginas; possivel gap de "
                  "eventos perdidos nesta rodada.", flush=True)
        return res
    finally:
        _INTEL_LOCK.release()


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
    """Conexão somente-leitura ao cache: SQLite local ou Postgres (prod)."""
    return store.connect(readonly=True)


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
    def one(table):
        # tabela pode não existir (ex.: price_snapshots não migra ao Postgres)
        try:
            row = con.execute(
                f"SELECT COUNT(DISTINCT item_id) FROM {table}").fetchone()
            return (row[0] if row else 0) or 0
        except Exception:
            return 0
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
                       AND ts>=?""",
                    [aodp.server, iid, store.cutoff_iso(21)]).fetchall())
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


def _wiki_name(item_id):
    """Nome PT do item, com fallback do encanto (T4_METALBAR@1 -> base + encanto)."""
    meta = db.get(item_id)
    if meta and meta.get("pt"):
        return meta["pt"]
    base, _, e = item_id.partition("@")
    nm = (db.get(base) or {}).get("pt", item_id)
    return f"{nm} (encanto {e})" if e and e != "0" else nm


@app.get("/api/wiki")
def wiki_view(item: str, focus: bool = False,
              qty: float | None = Query(None, gt=0, le=100000),
              max_age: int = Query(config.PRICES_TTL, ge=0)):
    """Ficha-wiki do item: detalhes do dump, receita, usado-em (índice reverso),
    drops e a CADEIA DE PRODUÇÃO recursiva + lista de compras de bruto."""
    from albion import wiki as wk
    from albion import craft as craft_mod
    from albion.microstructure import clean_price_rows
    item_id = _resolve_items([item])[0]
    meta = db.get(item_id) or {}
    tier_of = lambda i: (db.get(i.split("@")[0]) or {}).get("tier")
    # preços de TODA a cadeia (menor venda q1 entre cidades reais, sem âncora)
    chain_ids = list(wk.chain_item_ids(item_id))
    cheapest = {}
    if chain_ids:
        cities = list(config.ROYAL_CITIES) + ["Brecilien"]
        rows = _api_guard(lambda: aodp.get_prices(chain_ids, cities, max_age=max_age))
        for r in clean_price_rows(rows):
            if r["quality"] != 1:
                continue
            sp = r.get("sell_price_min") or 0
            if sp > 0 and (r["item_id"] not in cheapest or sp < cheapest[r["item_id"]]):
                cheapest[r["item_id"]] = sp
    price_of = cheapest.get
    tree = wk.production_tree(item_id, price_of, _wiki_name, tier_of,
                             focus=focus, qty=qty)
    recipe = craft_mod.recipe_for(item_id)
    recipe_out = None
    if recipe:
        def _inp(i):
            cid = wk.canonical_id(i["id"])      # id real (navegável/precificável)
            return {"id": cid, "count": i["count"], "name_pt": _wiki_name(cid),
                    "tier": tier_of(cid), "buy": price_of(cid)}
        recipe_out = {
            "inputs": [_inp(i) for i in recipe["inputs"]],
            "focus": recipe.get("focus"), "output": recipe.get("output", 1),
            "category": recipe.get("category"),
            "bonus_city": craft_mod.unified_bonus_city(item_id, recipe.get("category")),
        }
    used_rows, used_total = wk.used_in(item_id, _wiki_name, tier_of=tier_of)
    return {
        "details": wk.item_details(item_id, meta),
        "recipe": recipe_out,
        "used_in": used_rows, "used_in_total": used_total,
        "tree": tree["tree"], "shopping": tree["shopping"],
        "totals": {k: tree[k] for k in ("target_qty", "output", "raw_cost",
                                        "raw_cost_unit", "unpriced",
                                        "make_unit", "buy_unit", "best_unit")},
    }


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


def _price_lookups(con, max_age_days=3):
    """(q1, allq) das linhas de prices saneadas (sem âncora) e frescas — base
    das análises do hub Avançado. Espelha analyze.py _clean_price_lookups."""
    from albion.microstructure import clean_price_rows
    # corte ISO calculado em Python (portável SQLite/Postgres)
    cutoff = (datetime.utcnow()
              - timedelta(days=int(max_age_days))).strftime("%Y-%m-%dT%H:%M:%S")
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
    return {r[0]: r[1] for r in con.execute(
        """SELECT item_id, SUM(item_count)*1.0/COUNT(DISTINCT substr(ts,1,10))
           FROM history WHERE server=? AND time_scale=24 AND quality=1
             AND item_count>0 AND ts>=? GROUP BY item_id""",
        [aodp.server, store.cutoff_iso(days)]).fetchall()}


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


@app.get("/api/guild")
def guild_view(view: str = "watch", days: float = Query(7, ge=0.25, le=30),
               premium: bool = True, limit: int = Query(40, ge=1, le=200)):
    """Guild p/ o hub: ranking de destruição, cesta de regear, make-or-buy.

    Lê o agregado kill_demand_daily (killboard MAGRO, alimentado por /api/sweep
    com kind=intel). Sai vazio até o sweep de killboard acumular dados."""
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


@app.get("/api/demand")
def demand_view(view: str = "burn", days: float = Query(7, ge=0.25, le=45),
                premium: bool = True, limit: int = Query(40, ge=1, le=200)):
    """Demanda do killboard MAGRO (kill_demand_daily): giro de consumíveis
    (poções/comida queimadas/dia vs oferta) e qualidade do gear destruído.
    Sai vazio até o /api/intel-sweep acumular dados."""
    from albion import demand as dm
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        name = lambda i: (db.get(i) or {}).get("pt", i)
        q1, allq = _price_lookups(con)
        if view == "quality":
            priceq = {}
            for (i, _c, qq), p in allq.items():
                k = (i, qq)
                if k not in priceq or p < priceq[k]:
                    priceq[k] = p
            rows = dm.destroyed_quality(con, aodp.server, days=days,
                                        price_q=lambda i, q: priceq.get((i, q)),
                                        limit=limit)
        else:  # burn
            price_item = _cheapest_by_item(q1)
            vol = _market_volume(con, days=7)
            rows = dm.consumable_burn(con, aodp.server, days=days,
                                      price_of=price_item.get,
                                      vol_of=vol.get, limit=limit)
        for r in rows:
            r["name_pt"] = name(r["item_id"])
        return {"view": view, "rows": rows}
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
             "ts >= ?"]
    params = [aodp.server, store.cutoff_iso(days)]
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
            # restock cruza killboard (kill_demand_daily, agregado magro) —
            # vazio até o /api/intel-sweep acumular; degrada sem quebrar.
            try:
                # slot != 'Inventory': só gear equipado destruído (não carga)
                demand = con.execute(
                    """SELECT item_id, SUM(victim_units) AS u
                       FROM kill_demand_daily
                       WHERE server=? AND day >= ? AND slot != 'Inventory'
                       GROUP BY item_id HAVING SUM(victim_units)>0
                       ORDER BY u DESC LIMIT 400""",
                    [aodp.server, store.cutoff_iso(days)]).fetchall()
            except Exception:
                demand = []
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
                 AND ts >= ? AND item_id=?
               ORDER BY city, day""",
            [aodp.server, store.cutoff_iso(days), iid]).fetchall()
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
# ---------------------------------------------------------------- ícones
# O serviço de render (render.albiononline.com) bloqueia clientes com TLS do
# OpenSSL (curl/httpx) via Cloudflare; navegadores reais passam. Este proxy é
# o fallback do frontend: baixa via Schannel (PowerShell) e cacheia em disco.
# Cache de ícones em disco. Na nuvem (Vercel) o diretório do projeto é
# SOMENTE-LEITURA; cai para um diretório temporário gravável (efêmero por
# instância — o cache se refaz, e o frontend tem fallback direto se faltar).
ICONS_DIR = ROOT / "data" / "icons"
try:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    import tempfile
    ICONS_DIR = Path(tempfile.gettempdir()) / "albion_icons"
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
    # Servir o login na LAN por HTTP puro expõe o cookie de sessão (sem Secure)
    # a sniffing/replay. Avisa alto e exige opt-in consciente do risco.
    if (config.SERVE_LAN and config.AUTH_REQUIRED
            and not config.AUTH_COOKIE_SECURE):
        print("\n" + "=" * 70 + "\n  AVISO DE SEGURANÇA: SERVE_LAN=1 sem HTTPS.\n"
              "  O cookie de sessão trafega em TEXTO CLARO na rede local e pode\n"
              "  ser capturado/reusado. Use um proxy reverso HTTPS e defina\n"
              "  ALBION_AUTH_COOKIE_SECURE=1, OU mantenha só em 127.0.0.1.\n"
              + "=" * 70 + "\n")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    print(f"Mercado Albion (Américas) — http://{HOST}:{PORT}"
          + (" (acessível pela rede local)" if config.SERVE_LAN else ""))
    uvicorn.run(app, host=serve_host, port=PORT, log_level="warning")

# -*- coding: utf-8 -*-
"""Servidor local do app de mercado do Albion Online (Américas).

Rodar:  python app.py        (abre o navegador em http://127.0.0.1:8528)
"""
import asyncio
import hmac
import ipaddress
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
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


def _setup_logging():
    """Logging leve: arquivo rotativo data/server.log + avisos no console.

    Os prints de UX existentes (senha do BOOTSTRAP, progresso da coleta)
    continuam indo direto ao console; o logger cobre erros/avisos — que saem
    no console (stderr usa backslashreplace, então acentos não estouram no
    cp1252 do Windows) E ficam no arquivo p/ diagnóstico pós-mortem."""
    log_path = ROOT / "data" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # delay=True: só abre o arquivo no 1º registro emitido. Sem isso, a CLI
    # (analyze.py importa app p/ recommendations) abriria server.log só por
    # importar e, no Windows, o handle extra faz o doRollover do servidor
    # falhar com WinError 32 (rename de arquivo aberto) — traceback no stderr
    # a cada registro e rodízio de log quebrado enquanto os dois vivem.
    file_h = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8", delay=True)
    file_h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console_h = logging.StreamHandler()
    console_h.setLevel(logging.WARNING)   # console só vê warning/error
    console_h.setFormatter(logging.Formatter(
        "%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_h, console_h])
    # httpx loga CADA request em INFO — na coleta automática isso inundaria o
    # arquivo (milhares de linhas/ciclo). Só warnings dele interessam.
    logging.getLogger("httpx").setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("albion.app")


async def _run_discord_bot_inproc():
    """Bot Discord DENTRO do processo do servidor (nuvem free: 1 serviço só).

    Liga quando DISCORD_BOT_TOKEN existe e discord.py está instalado. Provisiona
    o PRÓPRIO token de serviço (label 'inproc-bot', rotacionado a cada boot —
    nunca precisa de env de token de serviço) e fala com a API via loopback.
    O cron de 1 min que mantém o Render acordado mantém o bot online 24/7.
    Qualquer falha loga e desiste — jamais derruba o servidor."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        import discord  # noqa: F401
    except ImportError:
        log.warning("DISCORD_BOT_TOKEN definido mas discord.py não instalado "
                    "(pip install discord.py) — bot embarcado desligado.")
        return
    try:
        import sys as _sys
        tools_dir = str(Path(__file__).resolve().parent / "tools")
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        import discord_bot as _dbot

        # Label ÚNICO por boot: auth_service_tokens tem UNIQUE(label) GLOBAL,
        # então a linha REVOGADA do boot anterior segue ocupando "inproc-bot"
        # e reusar o label quebrava TODO reboot na nuvem (UniqueViolation ->
        # bot morto silenciosamente). Revoga qualquer inproc ativo e cria com
        # sufixo de época (1 linha revogada/deploy — auditável, sem conflito).
        prefix = "inproc-bot"
        for t in auth_manager.list_service_tokens():
            if (str(t.get("name") or "").startswith(prefix)
                    and not t.get("revoked_at")):
                auth_manager.revoke_service_token(t["id"])
        svc = auth_manager.create_service_token(
            f"{prefix}-{int(time.time())}",
            ["discord_link", "discord_read",
             "guild_report", "guild_audit"])["token"]
        port = os.environ.get("PORT", str(PORT))   # PORT do módulo (local 8528)
        api = _dbot.ApiClient(f"http://127.0.0.1:{port}", svc)
        bot = _dbot.build_bot(api)
        log.info("bot Discord embarcado: conectando (loopback :%s)", port)
        await bot.start(token)
    except Exception:
        log.exception("bot Discord embarcado morreu — servidor segue normal.")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Sobe/desce do servidor (substitui @app.on_event('startup'), deprecado).

    TestClient dispara o lifespan ao entrar no context manager — mesmo gatilho
    dos antigos eventos de startup, comportamento preservado."""
    log.info("servidor iniciando (backend=%s)", store.backend())
    _start_auto_collector()   # no-op na nuvem/testes (guardas internas)
    bot_task = None
    if os.environ.get("DISCORD_BOT_TOKEN", "").strip():
        bot_task = asyncio.create_task(_run_discord_bot_inproc())
    yield
    if bot_task:
        bot_task.cancel()
    # nada mais a desligar: o coletor local é thread daemon


app = FastAPI(title="Mercado Albion — Américas", lifespan=_lifespan)
db = ItemDB()
aodp = AODP(server=config.DEFAULT_SERVER)
auth_manager = AuthManager(aodp.db, aodp.db_lock)
from albion import prodchain as _prodchain  # noqa: E402
from albion import advisor as _advisor  # noqa: E402
chain_store = _prodchain.ChainStore(aodp.db, aodp.db_lock)
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


# ---- Coleta automática LOCAL (só SQLite). Na nuvem (Postgres) quem coleta é o
# cron /api/sweep — aqui religamos o "abre → atualiza sozinho" para o uso local.
_AUTOCOLLECT_CATS = ["weapons", "armors", "head", "shoes", "offhands", "capes",
                     "bags", "mounts", "consumables", "gathering", "crafting"]
_autocollect_started = False
# progresso da coleta automática, lido por /api/collect-status (barra no front)
_autocollect_progress = {"running": False, "done": 0, "total": 0}

# prioridade de coleta: categorias mais líquidas e tiers mais negociados primeiro,
# para a Início mostrar recomendações em ~30 s (1º bloco) sem esperar tudo.
_CAT_PRIORITY = {"bags": 0, "capes": 0, "consumables": 1, "gathering": 1,
                 "crafting": 1, "mounts": 2, "head": 3, "shoes": 3,
                 "offhands": 3, "weapons": 4, "armors": 4}
_TIER_PRIORITY = {4: 0, 5: 0, 6: 0, 7: 1, 8: 1, 3: 2}


def _prioritize_watch(items):
    """Ordena a watchlist: mais líquido primeiro (categoria, tier, encanto)."""
    def key(iid):
        m = db.get(iid) or {}
        return (_CAT_PRIORITY.get(m.get("cat"), 9),
                _TIER_PRIORITY.get(m.get("tier"), 3),
                m.get("ench", 0), iid)
    return sorted(items, key=key)


def _default_watch_seed(per_cat=400, total=2000):
    """Cesta ampla de itens negociáveis p/ a watchlist (T3+, encantos 0-3):
    armas/armaduras/acessórios + consumíveis + recursos brutos e refinados.
    Limitada a `total` (e o collect ainda respeita COLLECT_MAX_ITEMS)."""
    ids, seen = [], set()
    for cat in _AUTOCOLLECT_CATS:
        try:
            rows = db.filter(cat=cat, tier_min=3, ench_list=[0, 1, 2, 3],
                             limit=per_cat)
        except Exception:
            rows = []
        for it in rows:
            iid = it.get("id") if isinstance(it, dict) else it
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
                if len(ids) >= total:
                    return ids
    return ids


def _start_auto_collector():
    """Liga o coletor local: semeia a watchlist no 1º uso e coleta preços+
    histórico ao subir + a cada AUTO_COLLECT_INTERVAL_MIN (rate-limit cuidado no
    client). Só roda no SQLite local e fora de testes."""
    global _autocollect_started
    if _autocollect_started or config.AUTO_COLLECT_INTERVAL_MIN <= 0:
        return
    if getattr(aodp.db, "backend", "sqlite") != "sqlite":
        return  # nuvem: ingestão é pelo /api/sweep (cron)
    if os.environ.get("ALBION_NO_AUTOCOLLECT") == "1":
        return
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return  # nunca bater na API durante a suíte
    _autocollect_started = True

    def loop():
        try:
            # COMPLETA a watchlist com a cesta padrão (idempotente: só adiciona o
            # que falta) — aplica a cesta ampla mesmo se já houver itens.
            seed = _default_watch_seed()
            existing = {w["item_id"] for w in aodp.watch_list()}
            novos = [i for i in seed if i not in existing]
            if novos:
                aodp.watch_add(novos)
                print(f"[auto-collect] watchlist: +{len(novos)} itens "
                      f"(total {len(existing) + len(novos)}).", flush=True)
        except Exception as e:
            log.warning("[auto-collect] seed falhou: %s", repr(e)[:200])
        time.sleep(4)   # deixa o servidor subir antes de bater na API
        CHUNK = 150
        last_prune = 0.0   # monotonic da última poda (0 = poda já no 1º ciclo)
        while True:
            try:
                items = _prioritize_watch([w["item_id"] for w in aodp.watch_list()])
                _autocollect_progress.update({"running": True, "done": 0,
                                              "total": len(items)})
                done = 0
                for i in range(0, len(items), CHUNK):
                    aodp.collect(item_ids=items[i:i + CHUNK], source="auto")
                    done = min(i + CHUNK, len(items))
                    _autocollect_progress["done"] = done
                    print(f"[auto-collect] {done}/{len(items)} itens coletados.",
                          flush=True)
            except Exception as e:
                log.error("[auto-collect] erro: %s", repr(e)[:200])
            finally:
                _autocollect_progress["running"] = False
            # Poda DIÁRIA do cache — a mesma do CLI `prune` (agrega snapshots
            # além da retenção em price_snapshots_daily e apaga os brutos);
            # sem ela o cache.db cresce sem fim. Só no SQLite local. SEM
            # VACUUM (seguraria o lock do banco grande por minutos); o
            # wal_checkpoint(TRUNCATE) ao menos recolhe o -wal.
            if (store.backend() == "sqlite"
                    and (last_prune == 0.0
                         or time.monotonic() - last_prune >= 86400)):
                try:
                    res = aodp.snapshot_prune(vacuum=False)
                    with aodp.db_lock:
                        aodp.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    print(f"[auto-collect] prune: "
                          f"{res.get('deleted_rows', 0)} snapshots brutos "
                          "agregados e removidos.", flush=True)
                except Exception as e:
                    log.warning("[auto-collect] prune falhou: %s",
                                repr(e)[:200])
                last_prune = time.monotonic()
            time.sleep(max(60, config.AUTO_COLLECT_INTERVAL_MIN * 60))

    threading.Thread(target=loop, daemon=True, name="auto-collect").start()
    print("[auto-collect] coletor local ligado "
          f"(a cada {config.AUTO_COLLECT_INTERVAL_MIN} min).", flush=True)


@app.get("/api/collect-status")
def collect_status():
    """Progresso da coleta automática local (alimenta a barra de progresso)."""
    p = _autocollect_progress
    return {"running": bool(p["running"]), "done": p["done"], "total": p["total"]}


# /api/sweep e /api/intel-sweep são tocados por cron externo (sem sessão) —
# protegidos por token próprio
PUBLIC_AUTH_PATHS = {"/api/auth/login", "/api/auth/bootstrap-status",
                     "/api/sweep", "/api/intel-sweep"}
PROTECTED_DOC_PATHS = {"/docs", "/redoc", "/openapi.json"}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


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


def _normalize_ip(s: str) -> str:
    """Canoniza um IP: tira zona IPv6 (%eth0) e porta, valida com ipaddress.
    Sem isso, o MESMO cliente com porta/zona variável viraria 'IPs' diferentes
    (burlaria o limite e geraria ip_unrecognized espúrio)."""
    s = (s or "").strip().split("%")[0]
    if s.startswith("[") and "]" in s:        # [IPv6]:porta
        s = s[1:s.index("]")]
    elif s.count(":") == 1 and "." in s:      # IPv4:porta
        s = s.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return (s[:45] or "0.0.0.0")


def _client_ip(request: Request) -> str:
    """IP real do cliente atrás de proxy de confiança (Render/CDN).

    TODO header HTTP chega forjável pelo cliente; só é confiável o que o
    PRÓPRIO proxy à nossa frente escreve. Por isso, com AUTH_TRUST_PROXY
    ligado (opt-in; default desligado): (1) um header de borda
    (CF-Connecting-IP e afins) só é honrado se ALBION_EDGE_HEADER o nomear —
    a borda o sobrescreve, mas sem borda na frente ele seria forjável;
    (2) do X-Forwarded-For usa-se o token MAIS À DIREITA, o que o proxy
    imediato ANEXOU — os da esquerda vêm do cliente e são forjáveis.
    Sem proxy (local), request.client.host. Vínculo por IP é
    defesa-em-profundidade sobre a sessão."""
    if config.AUTH_TRUST_PROXY:
        if config.AUTH_EDGE_HEADER:
            v = request.headers.get(config.AUTH_EDGE_HEADER)
            if v:
                return _normalize_ip(v.split(",")[0])
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return _normalize_ip(xff.split(",")[-1])
    return _normalize_ip(request.client.host if request.client else "0.0.0.0")


def _set_login_cookies(response, request, result):
    secure = _cookie_secure(request)
    response.set_cookie(
        config.AUTH_SESSION_COOKIE, result["session_token"],
        max_age=7 * 86400, httponly=True, secure=secure,
        samesite="strict", path="/")


def _clear_auth_cookies(response):
    response.delete_cookie(config.AUTH_SESSION_COOKIE, path="/")
    # Os IPs registrados permanecem no logout; só a sessão (cookie) é apagada.


def _require_role(request: Request, allowed, *, org=None):
    if not config.AUTH_REQUIRED:
        return {"id": 0, "username": "local", "role": "admin",
                "org_id": 1, "is_super": True}
    account = getattr(request.state, "auth", {}).get("account")
    if not account or account.get("role") not in allowed:
        raise AuthError("Voce nao tem permissao para esta operacao.",
                        "forbidden", 403)
    # Escopo de inquilino: um admin-de-guilda só opera na própria org; o
    # super-admin (dono) cruza inquilinos.
    if (org is not None and not account.get("is_super")
            and int(account.get("org_id") or 0) != int(org)):
        raise AuthError("Operacao fora do seu inquilino.",
                        "org_forbidden", 403)
    return account


def _actor_org(request: Request) -> int:
    """id do inquilino (org) do solicitante. 1 = org default / modo local."""
    if not config.AUTH_REQUIRED:
        return 1
    acct = (getattr(request.state, "auth", {}) or {}).get("account") or {}
    return int(acct.get("org_id") or 1)


def _require_service_scope(request: Request, scope: str):
    """Gate dos endpoints de bot: exige ctx de SERVIÇO com o escopo dado.

    Humano com sessão não tem escopos -> 403 (endpoints /api/discord/* são
    exclusivos do bot); token sem o escopo -> 403. Modo local libera."""
    if not config.AUTH_REQUIRED:
        return {"service": True, "name": "local", "scopes": [scope],
                "account": None}
    ctx = getattr(request.state, "auth", {}) or {}
    if not ctx.get("service"):
        raise AuthError("Endpoint exclusivo de servico (X-Service-Token).",
                        "service_required", 403)
    if scope not in (ctx.get("scopes") or []):
        raise AuthError("Token de servico sem o escopo necessario.",
                        "service_scope", 403)
    return ctx


def _org_entitlement(org: int, scope: str):
    """Checagem crua do direito do inquilino (fail-closed sem registro).

    Usada pela web (org do ator) E pelos endpoints de bot, onde a org vem do
    MEMBRO vinculado — nunca do servidor Discord."""
    with aodp.db_lock:
        try:
            row = aodp.db.execute(
                "SELECT active,expires_at FROM org_entitlements "
                "WHERE org_id=? AND scope=?", [org, scope]).fetchone()
        finally:
            try:
                aodp.db.rollback()
            except Exception:
                pass
    if row is None:
        raise AuthError("Inquilino sem direito de acesso a esta area.",
                        "entitlement_missing", 403)
    active, expires = row[0], row[1]
    if not active or (expires is not None
                      and str(expires) < store.cutoff_iso(0)):
        raise AuthError("Acesso a esta area expirou. Renove a contribuicao.",
                        "entitlement_expired", 403)


def _require_entitlement(request: Request, scope: str):
    """Gate da superfície operacional pelo DIREITO do inquilino.

    Fase 1: a org nº 1 nasce com o direito ativo e sem prazo (permissivo) e o
    modo local libera — a trava real (cobrança) morde na Fase 2. Lê
    org_entitlements do inquilino do solicitante; fail-closed se não houver
    registro, para não vazar a superfície. Contexto de SERVIÇO é barrado aqui:
    o bot só fala com /api/discord/* (escopos próprios); sem isto um token
    resolveria _actor_org p/ a org 1 e leria cadeias/metas de console."""
    if not config.AUTH_REQUIRED:
        return
    ctx = getattr(request.state, "auth", {}) or {}
    if ctx.get("service"):
        raise AuthError("Endpoint de console exige conta humana "
                        "(nao token de servico).", "account_required", 403)
    _org_entitlement(_actor_org(request), scope)


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
            service_token = request.headers.get("x-service-token")
            if service_token:
                # Token de SERVIÇO (bot): caminho paralelo à sessão — sem
                # conta, sem vínculo de IP e sem CSRF. O ctx sem 'account'
                # barra o console (_require_role) por construção.
                ctx = auth_manager.authenticate_service(service_token)
                if ctx is None:
                    raise AuthError("Token de servico invalido ou revogado.",
                                    "service_invalid", 401)
                request.state.auth = ctx
            else:
                ctx = auth_manager.authenticate(
                    request.cookies.get(config.AUTH_SESSION_COOKIE),
                    ip=_client_ip(request))
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


# Defesa de DoS no login: cada tentativa roda 1 scrypt (~32MB, ~167ms) mesmo p/
# usuário inexistente. Sem isto, um anônimo faz spray de usernames e estoura a
# RAM/threadpool do free-tier. O throttle por-username (auth.py) NÃO cobre spray.
_LOGIN_SEM = threading.BoundedSemaphore(3)   # scrypt concorrente -> teto de RAM
_LOGIN_HITS = {}                              # ip -> [timestamps recentes]
_LOGIN_HITS_LOCK = threading.Lock()
_LOGIN_WINDOW_S = 60
_LOGIN_MAX_PER_WINDOW = 12


def _login_rate_ok(ip: str) -> bool:
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_S
    with _LOGIN_HITS_LOCK:
        hits = [t for t in _LOGIN_HITS.get(ip, ()) if t >= cutoff]
        if len(hits) >= _LOGIN_MAX_PER_WINDOW:
            _LOGIN_HITS[ip] = hits
            return False
        hits.append(now)
        _LOGIN_HITS[ip] = hits
        if len(_LOGIN_HITS) > 4096:          # poda para não crescer sem limite
            for k in [k for k, v in list(_LOGIN_HITS.items())
                      if not v or v[-1] < cutoff]:
                _LOGIN_HITS.pop(k, None)
        return True


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    if not _login_rate_ok(ip):
        raise AuthError("Muitas tentativas de login deste IP. Aguarde um minuto.",
                        "login_rate_limited", 429)
    if not auth_manager.has_admin():
        raise AuthError("Crie o primeiro administrador pela CLI.",
                        "bootstrap_required", 503)
    with _LOGIN_SEM:                          # serializa o scrypt (limita RAM)
        result = auth_manager.login(body.username, body.password, ip=ip)
    response = JSONResponse({
        "account": result["account"], "csrf": result["csrf_token"],
        "roles": ROLES, "profiles_catalog": PROFILES,
    }, headers={"Cache-Control": "no-store"})
    _set_login_cookies(response, request, result)
    return response


@app.get("/api/auth/me")
def auth_me(request: Request):
    # modo LOCAL (auth desligada): devolve um admin local para o frontend não
    # travar na tela de login. A nuvem sempre roda com AUTH_REQUIRED=True.
    if not config.AUTH_REQUIRED:
        return JSONResponse({
            "account": {"id": 0, "username": "local", "role": "admin",
                        "active": 1, "must_change_password": False,
                        "profiles": [], "ips": []},
            "csrf": "", "roles": ROLES, "profiles_catalog": PROFILES,
        }, headers={"Cache-Control": "no-store"})
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


def _sweep_token(request: Request, token: str) -> str:
    """Token do sweep: preferir o header X-Sweep-Token (não vaza no access-log
    do uvicorn como a query string faz a cada minuto); cair na query por compat."""
    return request.headers.get("x-sweep-token") or token or ""


def _check_sweep_token(token: str):
    """Gate dos endpoints de sweep: fail-closed sem token em prod E no SQLite
    exposto à LAN (SERVE_LAN); comparação em tempo constante por BYTES (str
    não-ASCII em compare_digest levantaria 500)."""
    if not config.SWEEP_TOKEN and (store.backend() != "sqlite" or config.SERVE_LAN):
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


# epoch da última poda do sweep (0 = poda já no 1º tick após o boot)
_SWEEP_LAST_PRUNE = 0.0


@app.get("/api/sweep")
@app.post("/api/sweep")
def sweep(request: Request, token: str = "",
          count: int = Query(config.SWEEP_ITEMS_PER_TICK, ge=10, le=400)):
    """Um TOQUE do sweep fatiado (tocado por cron a cada minuto).

    Avança um cursor pelo universo de mercado, busca a fatia (preços sempre
    frescos + histórico se velho) e grava no store. Em ~3 h varre tudo e recicla.
    Protegido por ALBION_SWEEP_TOKEN (header X-Sweep-Token ou ?token=).
    """
    _check_sweep_token(_sweep_token(request, token))
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
    out = {
        "ok": True, "universe": n, "from_cursor": res["start"],
        "took": len(slice_ids), "next_cursor": res["new_cursor"],
        "cycle": res["cycle"], "price_rows": len(pr),
        "history_series": len(hi),
        "progress_pct": round(100 * res["new_cursor"] / n, 1)
        if res["new_cursor"] else 100.0,
    }
    # Poda DIÁRIA best-effort (espelha o coletor local): sem ela o sweep
    # enchia o Postgres free sem limite. Estado só em memória — no pior caso
    # (reboot) roda uma poda extra, que sai barata quando não há nada a podar.
    global _SWEEP_LAST_PRUNE
    if time.time() - _SWEEP_LAST_PRUNE >= 86400:
        _SWEEP_LAST_PRUNE = time.time()      # antes do trabalho: sem re-entrada
        try:
            out["prune"] = aodp.snapshot_prune(vacuum=False)
        except Exception as exc:             # nunca derruba o tick do cron
            log.warning("[sweep] prune falhou: %s", exc)
            out["prune"] = {"error": str(exc)[:200]}
        try:
            # poda de history: o vilão real do disco no Postgres free — sem ela
            # a tabela acumula a janela buscada sem limite (chegou a 1.2 GB).
            out["history_prune"] = aodp.history_prune()
        except Exception as exc:
            log.warning("[sweep] history_prune falhou: %s", exc)
            out["history_prune"] = {"error": str(exc)[:200]}
    return out


@app.get("/api/intel-sweep")
@app.post("/api/intel-sweep")
def intel_sweep(request: Request, token: str = ""):
    """Um toque do sweep de KILLBOARD magro (tocado por cron, ~10 em 10 min).

    Pagina o gameinfo e agrega o equipamento das vítimas em kill_demand_daily
    (sem eventos crus), depois poda dias além da retenção. Alimenta Guild
    (fazer-vs-comprar, regear, ranking de destruição) e Logística (reposição).
    """
    from albion import gameinfo
    _check_sweep_token(_sweep_token(request, token))
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
        # Postgres devolve SUM de coluna inteira como Decimal e SUM de produto
        # com preco (double) como float; misturar os dois numa divisao estoura
        # (float / Decimal). No SQLite tudo ja vem float. Forcar float/int aqui
        # deixa a aritmetica dual-safe.
        total = float(r["total_volume"] or 0)
        traded = float(r["traded_value"] or 0)
        active_days = int(r["active_days"] or 0)
        vwap = (traded / total) if total else None
        stats[(r["item_id"], r["city"], r["quality"])] = {
            "avg_daily": total / max(days, 1),
            "active_days": active_days,
            "active_ratio": active_days / max(days, 1),
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


def _price_lookups(con, max_age_days=3, quality=None):
    """(q1, allq) das linhas de prices saneadas (sem âncora) e frescas — base
    das análises do hub Avançado. Espelha analyze.py _clean_price_lookups.

    quality: se setado (ex.: 1), filtra a query no SQL — bem mais leve no Postgres
    (evita o SELECT * da tabela inteira de prices). Use quando o chamador só precisa
    daquela qualidade (ilha/laborplan usam só q1). None = todas (padrão; NÃO mudar
    p/ não afetar prod/guild/prodchain, que usam allq)."""
    from albion.microstructure import clean_price_rows
    # corte ISO calculado em Python (portável SQLite/Postgres)
    cutoff = (datetime.utcnow()
              - timedelta(days=int(max_age_days))).strftime("%Y-%m-%dT%H:%M:%S")
    # descarta linhas MORTAS (sem preço nenhum) já no SQL: são a maioria da
    # tabela e o Postgres free trava se varrer tudo (o clean_price_rows só olha
    # campos > 0 mesmo, então filtrar aqui é equivalente e MUITO mais leve).
    sql = ("SELECT * FROM prices WHERE server=? "
           "AND (sell_price_min > 0 OR buy_price_max > 0)")
    params = [aodp.server]
    if quality is not None:
        sql += " AND quality=?"
        params.append(int(quality))
    rows = clean_price_rows(
        [dict(r) for r in con.execute(sql, params).fetchall()])
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
        q1, _ = _price_lookups(con, quality=1)   # prod só usa q1; evita scan pesado
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
        q1, _ = _price_lookups(con, quality=1)   # watch/makeorbuy só usam q1
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
        if view == "quality":
            _, allq = _price_lookups(con)   # quality precisa de TODAS as qual.
            priceq = {}
            for (i, _c, qq), p in allq.items():
                k = (i, qq)
                if k not in priceq or p < priceq[k]:
                    priceq[k] = p
            rows = dm.destroyed_quality(con, aodp.server, days=days,
                                        price_q=lambda i, q: priceq.get((i, q)),
                                        limit=limit)
        else:  # burn — só usa q1
            q1, _ = _price_lookups(con, quality=1)
            price_item = _cheapest_by_item(q1)
            # mesma janela nos dois lados (queima do killboard vs volume AODP)
            vol = _market_volume(con, days=days)
            rows = dm.consumable_burn(con, aodp.server, days=days,
                                      price_of=price_item.get, vol_of=vol.get,
                                      premium=premium, limit=limit)
        for r in rows:
            r["name_pt"] = name(r["item_id"])
        return {"view": view, "rows": rows}
    finally:
        con.close()


@app.get("/api/island")
def island_view(view: str = "laborers", premium: bool = True,
                sell_mode: str = "order", limit: int = Query(80, ge=1, le=300)):
    """Administração de ILHA: trabalhadores (diários), agricultura, pecuária.

    Lê data/island_data.json (mecânicas do dump) cruzado com os preços q1
    saneados por cidade. Cada linha traz onde COMPRAR insumos, onde VENDER, lucro
    com/sem foco e capital de giro. Vazio até a coleta cobrir os itens de ilha."""
    if view not in ("laborers", "crops", "animals"):
        raise HTTPException(status_code=400,
                            detail="view deve ser laborers, crops ou animals")
    if sell_mode not in ("instant", "order"):
        raise HTTPException(status_code=400,
                            detail="sell_mode deve ser instant ou order")
    from albion import island as isl
    con = _cache_connection()
    if con is None:
        return {"view": view, "rows": []}
    try:
        name = lambda i: (db.get(i) or {}).get("pt", i)
        q1, _ = _price_lookups(con, quality=1)   # ilha usa só q1; evita scan pesado
        if view == "crops":
            res = isl.crop_economy(q1, premium=premium, sell_mode=sell_mode,
                                   limit=limit)
            for r in res.get("rows", []):
                r["seed_pt"] = name(r["seed"])
                r["crop_pt"] = name(r["crop"])
        elif view == "animals":
            res = isl.animal_economy(q1, premium=premium, sell_mode=sell_mode,
                                     limit=limit)
            for r in res.get("rows", []):
                r["baby_pt"] = name(r["baby"])
                r["grown_pt"] = name(r["grown"])
        else:  # laborers
            res = isl.laborer_economy(q1, premium=premium, sell_mode=sell_mode,
                                      limit=limit)
            for r in res.get("rows", []):
                r["empty_pt"] = name(r["empty"])
                r["resource_pt"] = name(r["resource"]) if r.get("resource") else None
        res["view"] = view
        return res
    finally:
        con.close()


@app.get("/api/laborer-happiness")
def laborer_happiness_view(laborer_tier: int, building_tier: int = 8,
                           n_laborers: int = 1, beds: int = None,
                           bed_tier: int = None, tables: int = None,
                           table_tier: int = None, general_tiers: str = "",
                           typed_tiers: str = "", family: str = None,
                           shark: bool = False, spyglass: bool = False):
    """PAINEL de felicidade do trabalhador — modelo REAL do jogo (calibrado
    2026-07-05 com screenshot ao vivo). Puro cálculo, NÃO toca o cache.

    Teto do painel = 100×L−100 (camas/mesas 50×L−100 cada + troféus 100 fixo);
    rendimento por diário T_J = clamp(100 + 0,5×(total − 100×J), 100, 150).
    general_tiers/typed_tiers: CSV de tiers de troféu COM cobertura (ex.:
    "2,3,4,5,6,7"); typed é ignorado p/ fabricação (WARRIOR/MAGE/HUNTER/
    TOOLMAKER — sem troféu de tipo). Prédio tranca mobília/troféu de tier maior
    (geral T8 e tubarão só em prédio T8). Defaults: beds = n_laborers,
    tables = ceil(n_laborers/6), tiers de mobília = tier do prédio.
    Resposta = island.happiness_advice completo: painel (camas/mesas/trofeus/
    total/total_max), yield_por_diario (T2..L, atual e alcançável), hints,
    trancados e alcancavel."""
    from albion import island as isl

    def _csv(s, name):
        if not s:
            return ()
        try:
            return tuple(int(x) for x in s.replace(" ", "").split(",") if x)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"{name} deve ser CSV de tiers (2,3,4)")

    n = max(1, n_laborers)
    if beds is None:
        beds = n
    if bed_tier is None:
        bed_tier = building_tier
    if tables is None:
        tables = -(-n // isl._TABLE_COVERS)
    if table_tier is None:
        table_tier = building_tier
    try:
        return isl.happiness_advice(
            laborer_tier, building_tier=building_tier, n_laborers=n,
            beds=beds, bed_tier=bed_tier, tables=tables, table_tier=table_tier,
            general_tiers=_csv(general_tiers, "general_tiers"),
            typed_tiers=_csv(typed_tiers, "typed_tiers"),
            family=family, shark=shark, spyglass=spyglass)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# famílias com diário de FABRICAÇÃO (enchem craftando — únicas com laborplan)
_LABORPLAN_FAMILIES = ("WARRIOR", "HUNTER", "MAGE", "TOOLMAKER", "MERCENARY")


def _history_vwap_volumes(con, days=30.0, item_ids=None):
    """VWAP por item (Σ preço×volume / Σ volume) e volume DIÁRIO (Σ volume/dias)
    do histórico q1/24h — espelha analyze._laborer_market_data. Corte por texto
    ISO via store.cutoff_iso (portável SQLite/Postgres; nunca date('now')).

    item_ids: se dado, restringe a query a esses itens — bem mais leve no Postgres
    (o laborplan só precisa dos itens de fill da família/tier, não do universo de
    ~10k). None = varre tudo (padrão)."""
    if item_ids is not None and not item_ids:
        return {}, {}
    where = ["server=?", "time_scale=24", "quality=1", "avg_price>0", "ts >= ?"]
    params = [aodp.server, store.cutoff_iso(days)]
    if item_ids:
        where.append(f"item_id IN ({_placeholders(item_ids)})")
        params.extend(item_ids)
    rows = con.execute(
        f"SELECT item_id, avg_price, item_count FROM history WHERE "
        + " AND ".join(where), params).fetchall()
    num, den = {}, {}
    for r in rows:
        iid, avg, cnt = r["item_id"], r["avg_price"] or 0, r["item_count"] or 0
        if avg <= 0 or cnt <= 0:
            continue
        num[iid] = num.get(iid, 0) + avg * cnt
        den[iid] = den.get(iid, 0) + cnt
    vwap = {i: num[i] / den[i] for i in den if den[i] > 0}
    days = max(days, 1)
    volumes = {i: den[i] / days for i in den}     # unidades negociadas/dia
    return vwap, volumes


@app.get("/api/laborplan")
def laborplan_view(family: str, tier: int, laborers: int,
                   journals_per_day: int = 1, market_depth: float = 0.2,
                   station_fee: float = 0.0, premium: bool = True,
                   sell_mode: str = "order"):
    """Plano de produção p/ N trabalhadores de FABRICAÇÃO (island.laborer_plan).

    Diversifica o item de fill entre os candidatos elegíveis (cada um até
    market_depth do volume diário dele) e devolve a cesta + material/dia +
    resumo/dia. Leitura-só do cache: q1 saneado (_price_lookups, anti-âncora)
    + VWAP/volume do histórico com a banda anti-isca 0,35..3× do VWAP — os
    mesmos insumos que a CLI monta em analyze._laborer_market_data."""
    fam = (family or "").strip().upper()
    if fam not in _LABORPLAN_FAMILIES:
        raise HTTPException(status_code=400,
                            detail="family deve ser "
                                   + "|".join(_LABORPLAN_FAMILIES))
    if not (2 <= tier <= 8):
        raise HTTPException(status_code=400, detail="tier deve ser 2..8")
    if not (1 <= laborers <= 500):
        raise HTTPException(status_code=400, detail="laborers deve ser 1..500")
    if not (1 <= journals_per_day <= 10):
        raise HTTPException(status_code=400,
                            detail="journals_per_day deve ser 1..10")
    if not (0 < market_depth <= 1):
        raise HTTPException(status_code=400,
                            detail="market_depth deve ser fração em (0, 1]")
    if station_fee < 0:
        raise HTTPException(status_code=400, detail="station_fee deve ser >= 0")
    if sell_mode not in ("instant", "order"):
        raise HTTPException(status_code=400,
                            detail="sell_mode deve ser instant ou order")
    from albion import island as isl
    con = _cache_connection()
    if con is None:
        return {"available": False, "reason": "cache de preços vazio",
                "family": fam, "tier": tier}
    try:
        # ESCOPO (senão o scan da tabela inteira estoura o timeout no Postgres
        # free): só q1 nos preços e só os itens de FILL desta família/tier no
        # histórico — que é tudo que o laborer_plan consulta de vwap/volume.
        fill_items = (((isl._load().get("laborers") or {})
                       .get(f"T{tier}_JOURNAL_{fam}_EMPTY") or {})
                      .get("fill", {}).get("items", []))
        q1, _ = _price_lookups(con, quality=1)
        vwap, volumes = _history_vwap_volumes(con, days=30, item_ids=fill_items)
    finally:
        con.close()

    def fill_sell_ok(item_id, price):
        # banda anti-isca do item de FILL: sem VWAP => rejeita (nunca negociou);
        # com VWAP, exige 0,35×VWAP <= preço <= 3×VWAP (mesma régua da CLI)
        v = vwap.get(item_id)
        if not v:
            return False
        return 0.35 * v <= price <= 3.0 * v

    res = isl.laborer_plan(
        q1, family=fam, tier=tier, n_laborers=laborers,
        journals_per_day=journals_per_day, market_depth=market_depth,
        station_fee=station_fee, premium=premium, sell_mode=sell_mode,
        item_volumes=volumes, fill_sell_ok=fill_sell_ok)
    name = lambda i: (db.get(i) or {}).get("pt", i)
    for b in res.get("basket", []):
        b["item_pt"] = name(b["item"])
        for inp in b.get("inputs", []):
            inp["name_pt"] = name(inp["id"])
    return res


_ENCH_RE = re.compile(r"^(.*)@(\d+)$")


def _item_meta(iid):
    """db.get com fallback p/ ENCANTADO: o catálogo só guarda o item base, então
    T8_WOOD@4 herda nome/tier de T8_WOOD e marca o encanto (.4)."""
    m = db.get(iid)
    if m:
        return m
    mm = _ENCH_RE.match(iid or "")
    if mm:
        base = db.get(mm.group(1))
        if base:
            e = int(mm.group(2))
            return {**base, "pt": f"{base.get('pt', mm.group(1))} .{e}", "enchant": e}
    return {}


@app.get("/api/prodchain")
def prodchain_graph(item: str, premium: bool = True, sell_mode: str = "order"):
    """Grafo de receita (BOM) de 1+ produtos finais p/ a Linha de Produção.

    `item` aceita vários ids separados por vírgula (cadeia com múltiplos alvos).
    Devolve o grafo ESTÁTICO enriquecido (estrutura + RRR por cidade + preços
    saneados); a propagação de quantidade/custo roda no cliente ao vivo."""
    if sell_mode not in ("instant", "order"):
        raise HTTPException(status_code=400,
                            detail="sell_mode deve ser instant ou order")
    roots = [s.strip() for s in (item or "").split(",") if s.strip()][:12]
    if not roots:
        return {"roots": [], "nodes": {}, "cities": config.ROYAL_CITIES}
    con = _cache_connection()
    if con is None:
        return {"roots": roots, "nodes": {}, "cities": config.ROYAL_CITIES}
    try:
        q1, _ = _price_lookups(con)
        g = _prodchain.build_graph(
            roots, lambda i, c: q1.get((i, c)),
            premium=premium, sell_mode=sell_mode, cities=config.ROYAL_CITIES)
        for iid, node in g["nodes"].items():
            meta = _item_meta(iid)
            node["name_pt"] = meta.get("pt") or iid   # nunca nome vazio
            node["tier"] = meta.get("tier")
            node["enchant"] = meta.get("enchant", 0)
        return g
    finally:
        con.close()


class ChainSaveBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    payload: dict
    id: int | None = None


def _chain_owner(request: Request) -> int:
    """id da conta logada (dona da cadeia). 0 = modo local sem auth."""
    if not config.AUTH_REQUIRED:
        return 0
    acct = (getattr(request.state, "auth", {}) or {}).get("account") or {}
    return int(acct.get("id", 0) or 0)


def _chain_as_operator(request: Request) -> bool:
    """Operador/admin pode editar/apagar cadeia alheia DA MESMA org (o
    escopo de org já é o do próprio ator via _actor_org). Modo local libera."""
    if not config.AUTH_REQUIRED:
        return True
    acct = (getattr(request.state, "auth", {}) or {}).get("account") or {}
    return acct.get("role") in OPERATOR_ROLES


def _chain_owner_names(ids):
    """Mapa owner_user_id -> username (p/ rotular o dono no quadro da org)."""
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with aodp.db_lock:
        try:
            rows = aodp.db.execute(
                "SELECT id, username FROM auth_accounts "
                f"WHERE id IN ({marks})", ids).fetchall()
        except Exception:
            rows = []
        finally:
            try:
                aodp.db.rollback()
            except Exception:
                pass
    return {r[0]: r[1] for r in rows}


@app.get("/api/prodchain/chains")
def prodchain_chains_list(request: Request):
    """Quadro vivo: TODAS as cadeias da org do ator (mine marca as dele)."""
    _require_entitlement(request, "operacao")
    chains = chain_store.list(_actor_org(request), _chain_owner(request))
    names = _chain_owner_names(c["owner_user_id"] for c in chains)
    for c in chains:
        c["owner_username"] = names.get(c["owner_user_id"]) or "local"
    return {"chains": chains}


@app.post("/api/prodchain/chains")
def prodchain_chains_save(body: ChainSaveBody, request: Request):
    _require_entitlement(request, "operacao")
    try:
        cid = chain_store.save(_actor_org(request), _chain_owner(request),
                               body.name, body.payload, body.id,
                               as_operator=_chain_as_operator(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="cadeia de outro dono (só ele ou um "
                                   "operador/admin da org editam)")
    except KeyError:
        raise HTTPException(status_code=404, detail="cadeia não encontrada")
    return {"id": cid, "ok": True}


@app.get("/api/prodchain/chains/{cid}")
def prodchain_chains_get(cid: int, request: Request):
    """Cadeia de QUALQUER dono da org do ator (read-only p/ não-donos)."""
    _require_entitlement(request, "operacao")
    ch = chain_store.get(_actor_org(request), _chain_owner(request), cid)
    if not ch:
        raise HTTPException(status_code=404, detail="cadeia não encontrada")
    ch["owner_username"] = (_chain_owner_names([ch["owner_user_id"]])
                            .get(ch["owner_user_id"]) or "local")
    return {"chain": ch}


@app.delete("/api/prodchain/chains/{cid}")
def prodchain_chains_delete(cid: int, request: Request):
    _require_entitlement(request, "operacao")
    try:
        ok = chain_store.delete(_actor_org(request), _chain_owner(request),
                                cid, as_operator=_chain_as_operator(request))
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="cadeia de outro dono (só ele ou um "
                                   "operador/admin da org excluem)")
    if not ok:
        raise HTTPException(status_code=404, detail="cadeia não encontrada")
    return {"ok": True}


_ADVISOR_REFRESH_CAP = 300


def _advisor_refresh_city(city, flipable):
    """Refresh best-effort de UMA cidade no submit: só os itens flipáveis que já
    têm cotação nela, limitado a _ADVISOR_REFRESH_CAP ids. Nunca varre o mercado
    inteiro e nunca derruba a resposta — o cache 24/7 é o caminho principal."""
    try:
        con = _cache_connection()
        if con is None:
            return
        try:
            rows = con.execute(
                "SELECT DISTINCT item_id FROM prices WHERE server=? AND city=?",
                [aodp.server, city]).fetchall()
        finally:
            con.close()
        ids = [r[0] for r in rows if r[0] in flipable][:_ADVISOR_REFRESH_CAP]
        if ids:
            aodp.get_prices(ids, [city], max_age=0)   # 1 refresh, 1 cidade
    except Exception:
        pass


@app.get("/api/flip-advisor")
def flip_advisor(
    request: Request,
    budget: float = Query(..., gt=0),
    city: str = Query(...),
    qualities: str | None = "1",
    premium: bool = True,
    buy_mode: str = "instant",
    sell_mode: str = "order",
    include_black_market: bool = False,
    history_days: int = Query(7, ge=1, le=180),
    min_profit: float = 0,
    max_lines: int = Query(40, ge=1, le=200),
    fresh_max_age_min: int = Query(720, ge=0),
    refresh: bool = False,
):
    """Consultor de flips por orçamento: prata + cidade -> melhores compras.

    Lê o cache que o sweep mantém fresco (cache-only). `refresh=true` dispara 1
    atualização limitada da cidade escolhida antes de calcular."""
    if buy_mode not in ("instant", "order") or sell_mode not in ("instant", "order"):
        raise HTTPException(status_code=400, detail="modo de compra/venda inválido")
    quals = _csv_int(qualities) or [1]
    flipable = set(_default_watch_seed())
    if refresh:
        _advisor_refresh_city(city, flipable)
    con = _cache_connection()
    if con is None:
        return _advisor.advise(None, aodp.server, db, budget=budget, city=city,
                               capture_rate=config.CAPTURE_RATE)
    try:
        res = _advisor.advise(
            con, aodp.server, db, budget=budget, city=city, qualities=quals,
            premium=premium, buy_mode=buy_mode, sell_mode=sell_mode,
            include_black_market=include_black_market, history_days=history_days,
            min_profit=min_profit, max_lines=max_lines,
            fresh_max_age_min=fresh_max_age_min, flipable=flipable)
        res["coverage"] = _cache_coverage(con)
        res["refreshed_city"] = bool(refresh)
        return res
    finally:
        con.close()


def _clean_prows(con):
    """Linhas de prices saneadas (sem âncora) no formato dos rows da API —
    base das análises de logística (carga, escada de qualidade, BM, reposição)."""
    from albion.microstructure import clean_price_rows
    # filtra linhas mortas no SQL (idem _price_lookups): leve no Postgres free
    return clean_price_rows([dict(r) for r in con.execute(
        "SELECT * FROM prices WHERE server=? "
        "AND (sell_price_min > 0 OR buy_price_max > 0)",
        [aodp.server]).fetchall()])


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


# ------------------------------------------- tributo da guild (núcleo web)
# Metas semanais + reportes + relógio de 14 dias (docs/PLANO_DISCORD_GUILD.md
# §5, adaptado ao multi-inquilino: org_id no lugar do guild_id do Discord).
# As rotas têm paths próprios (/api/guild/assign etc.) — o GET /api/guild
# analítico acima segue intocado.
from albion import tribute as _tribute  # noqa: E402
tribute_store = _tribute.TributeStore(aodp.db, aodp.db_lock)

# /api/guild/clock-tick é tocado por cron externo (sem sessão), protegido por
# ALBION_GUILD_TOKEN — mesmo molde fail-closed do /api/sweep.
PUBLIC_AUTH_PATHS.add("/api/guild/clock-tick")


class GuildAssignBody(BaseModel):
    account_id: int
    item_id: str = Field(min_length=1, max_length=64)
    qty_target: int = Field(gt=0, le=_tribute.QTY_MAX)
    week_start: str | None = Field(default=None, max_length=10)
    sector: str | None = Field(default=None, max_length=16)
    from_chain_id: int | None = None
    note: str | None = Field(default=None, max_length=280)


class GuildReportBody(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0, le=_tribute.QTY_MAX)
    assignment_id: int | None = None
    member_id: int | None = None   # operador reportando em nome do membro
    note: str | None = Field(default=None, max_length=280)


class GuildAuditBody(BaseModel):
    report_id: int
    note: str | None = Field(default=None, max_length=280)


def _guild_account(request: Request) -> dict:
    """Conta autenticada do solicitante (qualquer papel). Modo local = admin."""
    if not config.AUTH_REQUIRED:
        return {"id": 0, "username": "local", "role": "admin",
                "org_id": 1, "is_super": True}
    account = getattr(request.state, "auth", {}).get("account")
    if not account:
        raise AuthError("Autenticacao ausente.", "auth_missing", 401)
    return account


def _member_in_org(org: int, account_id: int) -> bool:
    """True se a conta existe, está ativa e pertence à org (isolamento)."""
    if not config.AUTH_REQUIRED:
        return True
    with aodp.db_lock:
        try:
            row = aodp.db.execute(
                "SELECT org_id FROM auth_accounts WHERE id=? AND active=1",
                [account_id]).fetchone()
        finally:
            try:
                aodp.db.rollback()
            except Exception:
                pass
    return bool(row and int(row[0] or 1) == int(org))


def _check_guild_token(token: str):
    """Gate do clock-tick: fail-closed sem token em prod E no SQLite exposto à
    LAN (SERVE_LAN); comparação em tempo constante por BYTES — mesmo molde do
    _check_sweep_token. Lê a env na hora p/ permitir rotação sem reboot."""
    secret = os.environ.get("ALBION_GUILD_TOKEN", "")
    if not secret and (store.backend() != "sqlite" or config.SERVE_LAN):
        raise HTTPException(503,
                            "clock-tick desabilitado: defina ALBION_GUILD_TOKEN")
    if secret and not hmac.compare_digest(
            token.encode("utf-8", "ignore"), secret.encode("utf-8")):
        raise HTTPException(403, "token de guild invalido")


@app.post("/api/guild/assign")
def guild_assign(body: GuildAssignBody, request: Request):
    """Cria/atualiza meta semanal (membro+item+qtd). Operador da própria org."""
    _require_entitlement(request, "operacao")
    actor = _require_role(request, OPERATOR_ROLES)
    org = _actor_org(request)
    if not _member_in_org(org, body.account_id):
        raise HTTPException(404, "membro não encontrado nesta organização")
    try:
        res = tribute_store.assign(
            org, body.account_id, body.item_id, body.qty_target,
            week_start=body.week_start, sector=body.sector,
            from_chain_id=body.from_chain_id, note=body.note,
            created_by=int(actor.get("id") or 0))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **res}


@app.get("/api/guild/assignments")
def guild_assignments(request: Request, week_start: str | None = None,
                      account_id: int | None = None):
    """Metas da org. Operador vê todas (filtros opcionais); membro só as suas."""
    _require_entitlement(request, "operacao")
    account = _guild_account(request)
    org = _actor_org(request)
    if account.get("role") not in OPERATOR_ROLES:
        account_id = int(account.get("id") or 0)   # membro: só as próprias
    return {"week_now": _tribute.week_start_iso(),
            "assignments": tribute_store.assignments(
                org, week_start=week_start, account_id=account_id)}


@app.post("/api/guild/report")
def guild_report(body: GuildReportBody, request: Request):
    """Membro alega entrega (relógio PAUSA). Sem member_id = auto-reporte de
    qualquer conta da org; com member_id = operador em nome do membro."""
    _require_entitlement(request, "operacao")
    account = _guild_account(request)
    org = _actor_org(request)
    actor_id = int(account.get("id") or 0)
    member_id = body.member_id if body.member_id is not None else actor_id
    if member_id != actor_id:
        _require_role(request, OPERATOR_ROLES)
        if not _member_in_org(org, member_id):
            raise HTTPException(404, "membro não encontrado nesta organização")
    try:
        res = tribute_store.report(
            org, member_id, body.item_id, body.qty,
            assignment_id=body.assignment_id, note=body.note,
            actor_id=actor_id)
    except KeyError:
        raise HTTPException(404, "meta não encontrada")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **res}


@app.get("/api/guild/pending")
def guild_pending(request: Request, limit: int = Query(200, ge=1, le=500)):
    """Fila de auditoria (reportes pendentes). Operador da própria org."""
    _require_entitlement(request, "operacao")
    _require_role(request, OPERATOR_ROLES)
    return {"pending": tribute_store.pending(_actor_org(request), limit=limit)}


def _apply_report_to_chain(org, res, actor_id):
    """Ponte tributo→quadro vivo: soma a entrega aprovada ao estoque do nó.

    Se a meta nasceu de uma cadeia (from_chain_id), o stock do nó com o item
    da meta cresce qty_reported — a Linha de Produção da org reflete a entrega
    na hora. Falha NUNCA quebra o approve: cadeia apagada/nó ausente viram
    entrada 'chain_bridge_fail' (com detail) no guild_audit_log."""
    cid = res.get("from_chain_id")
    item = res.get("item_id")
    qty = int(res.get("qty_reported") or 0)
    if not cid or not item or qty <= 0:
        return
    detail = {"report_id": res.get("id"), "chain_id": cid,
              "item_id": item, "qty": qty}
    try:
        # atômico no ChainStore: get+update na MESMA transação — aprovações
        # concorrentes não se perdem e o save do dono não é sobrescrito.
        chain_store.add_stock(org, cid, item, qty)
    except Exception as exc:                   # noqa: BLE001 — auditada
        detail["reason"] = str(exc) or type(exc).__name__
        try:
            tribute_store.audit_event(org, actor_id, "chain_bridge_fail",
                                      target=res.get("account_id"),
                                      details=detail)
        except Exception:
            pass                               # auditoria nunca derruba o approve


@app.post("/api/guild/approve")
def guild_approve(body: GuildAuditBody, request: Request):
    """Aprova um reporte: relógio ZERA; se estava desligado, reativa.

    Se a meta veio de uma cadeia da Linha de Produção (from_chain_id), a
    entrega aprovada é somada ao estoque do nó (ponte tributo→quadro)."""
    _require_entitlement(request, "operacao")
    actor = _require_role(request, OPERATOR_ROLES)
    try:
        res = tribute_store.approve(_actor_org(request), body.report_id,
                                    int(actor.get("id") or 0), note=body.note)
    except KeyError:
        raise HTTPException(404, "reporte não encontrado")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    _apply_report_to_chain(_actor_org(request), res,
                           int(actor.get("id") or 0))
    return {"ok": True, **res}


@app.post("/api/guild/reject")
def guild_reject(body: GuildAuditBody, request: Request):
    """Rejeita um reporte: relógio RETOMA a contagem do mesmo marco."""
    _require_entitlement(request, "operacao")
    actor = _require_role(request, OPERATOR_ROLES)
    try:
        res = tribute_store.reject(_actor_org(request), body.report_id,
                                   int(actor.get("id") or 0), note=body.note)
    except KeyError:
        raise HTTPException(404, "reporte não encontrado")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, **res}


@app.get("/api/guild/member-status")
def guild_member_status(request: Request, account_id: int | None = None):
    """Relógio por membro (dias ao vivo). Operador vê todos; membro só o seu."""
    _require_entitlement(request, "operacao")
    account = _guild_account(request)
    org = _actor_org(request)
    if account.get("role") not in OPERATOR_ROLES:
        account_id = int(account.get("id") or 0)
    return {"members": tribute_store.member_status(org, account_id=account_id)}


@app.get("/api/guild/members")
def guild_members(request: Request):
    """Contas ativas da org do ator (id/username/role) — p/ o form de
    atribuição. NÃO reusa /api/admin/accounts (que expõe muito mais)."""
    _require_entitlement(request, "operacao")
    _require_role(request, OPERATOR_ROLES)
    org = _actor_org(request)
    with aodp.db_lock:
        try:
            rows = aodp.db.execute(
                "SELECT id, username, role FROM auth_accounts "
                "WHERE org_id=? AND active=1 ORDER BY username",
                [org]).fetchall()
        finally:
            try:
                aodp.db.rollback()
            except Exception:
                pass
    return {"members": [{"id": r[0], "username": r[1], "role": r[2]}
                        for r in rows]}


@app.get("/api/guild/clock-tick")
@app.post("/api/guild/clock-tick")
def guild_clock_tick(request: Request, token: str = ""):
    """Tick diário do relógio (cron): transições por tempo, idempotente.

    Protegido por ALBION_GUILD_TOKEN (header X-Guild-Token ou ?token=),
    isento de sessão — mesmo padrão do /api/sweep. Varre TODAS as orgs."""
    _check_guild_token(request.headers.get("x-guild-token") or token or "")
    return {"ok": True, **tribute_store.clock_tick()}


# ------------------- Discord Fase 1: API para o bot (token de serviço) ----
# O bot autentica com o header X-Service-Token (ctx de serviço, SEM conta) e
# só enxerga dados do MEMBRO vinculado em auth_discord_links. A org usada é
# SEMPRE a do membro (auth_accounts.org_id) — nunca o snowflake do servidor.


class DiscordLinkBody(BaseModel):
    code: str = Field(min_length=4, max_length=16)
    discord_user_id: int = Field(gt=0)


def _discord_member(discord_user_id: int) -> dict:
    """Conta ativa vinculada ao snowflake, ou 404 (nunca dados de terceiros)."""
    account = auth_manager.get_account_by_discord_id(discord_user_id)
    if not account:
        raise HTTPException(404, "vinculo Discord nao encontrado")
    return account


@app.post("/api/discord/link")
def discord_link(body: DiscordLinkBody, request: Request):
    """Consome o código gerado na console (/vincular do bot) e grava o
    vínculo Discord -> conta. Escopo: discord_link."""
    _require_service_scope(request, "discord_link")
    account = auth_manager.link_discord(body.code, body.discord_user_id)
    return {"ok": True, "account_id": account["id"],
            "username": account["username"], "role": account["role"],
            "org_id": account["org_id"]}


@app.get("/api/discord/whoami")
def discord_whoami(request: Request, discord_user_id: int = Query(gt=0)):
    """Resolve snowflake -> conta vinculada. Escopo: discord_read."""
    _require_service_scope(request, "discord_read")
    a = _discord_member(discord_user_id)
    return {"account_id": a["id"], "username": a["username"],
            "role": a["role"], "org_id": a["org_id"]}


@app.get("/api/discord/my-assignments")
def discord_my_assignments(request: Request,
                           discord_user_id: int = Query(gt=0),
                           week_start: str | None = None):
    """Metas SÓ do membro vinculado, na org DELE. Escopo: discord_read.
    Org sem entitlement 'operacao' -> 403, igual à web."""
    _require_service_scope(request, "discord_read")
    a = _discord_member(discord_user_id)
    org = int(a.get("org_id") or 1)
    _org_entitlement(org, "operacao")
    return {"week_now": _tribute.week_start_iso(),
            "assignments": tribute_store.assignments(
                org, week_start=week_start, account_id=int(a["id"]))}


@app.get("/api/discord/my-status")
def discord_my_status(request: Request, discord_user_id: int = Query(gt=0)):
    """Relógio de tributo SÓ do membro vinculado. Escopo: discord_read."""
    _require_service_scope(request, "discord_read")
    a = _discord_member(discord_user_id)
    org = int(a.get("org_id") or 1)
    _org_entitlement(org, "operacao")
    rows = tribute_store.member_status(org, account_id=int(a["id"]))
    return {"status": rows[0] if rows else None}


class DiscordReportBody(BaseModel):
    discord_user_id: int = Field(gt=0)
    item_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0, le=_tribute.QTY_MAX)
    assignment_id: int | None = None
    note: str | None = Field(default=None, max_length=280)


class DiscordAuditBody(BaseModel):
    discord_user_id: int = Field(gt=0)
    report_id: int
    note: str | None = Field(default=None, max_length=280)


def _discord_operator(discord_user_id: int) -> tuple[dict, int]:
    """Gate DUPLO da auditoria via bot: além do ESCOPO do token (checado no
    endpoint), o HUMANO vinculado precisa ser operador na org DELE — um token
    guild_audit apontado p/ um membro comum não aprova nada. 404 sem vínculo,
    403 sem entitlement da org ou sem papel."""
    a = _discord_member(discord_user_id)
    org = int(a.get("org_id") or 1)
    _org_entitlement(org, "operacao")
    if a.get("role") not in OPERATOR_ROLES:
        raise AuthError("Conta vinculada sem papel de operador.",
                        "forbidden", 403)
    return a, org


@app.post("/api/discord/report")
def discord_report(body: DiscordReportBody, request: Request):
    """Membro reporta entrega PELO BOT (relógio PAUSA). Escopo: guild_report.

    Auto-reporte sempre: actor_id = a própria conta vinculada (o bot nunca
    reporta 'em nome de' — isso é da web). Respostas iguais ao /api/guild/report."""
    _require_service_scope(request, "guild_report")
    a = _discord_member(body.discord_user_id)
    org = int(a.get("org_id") or 1)
    _org_entitlement(org, "operacao")
    member_id = int(a["id"])
    try:
        res = tribute_store.report(
            org, member_id, body.item_id, body.qty,
            assignment_id=body.assignment_id, note=body.note,
            actor_id=member_id)
    except KeyError:
        raise HTTPException(404, "meta não encontrada")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **res}


@app.get("/api/discord/pending")
def discord_pending(request: Request, discord_user_id: int = Query(gt=0),
                    limit: int = Query(200, ge=1, le=500)):
    """Fila de auditoria da org do OPERADOR vinculado. Escopo: guild_audit
    + papel operador (gate duplo em _discord_operator)."""
    _require_service_scope(request, "guild_audit")
    _, org = _discord_operator(discord_user_id)
    return {"pending": tribute_store.pending(org, limit=limit)}


@app.post("/api/discord/approve")
def discord_approve(body: DiscordAuditBody, request: Request):
    """Aprova reporte pelo bot: relógio ZERA; auditoria no nome do HUMANO
    vinculado (nunca do bot). Escopo: guild_audit + papel operador. Passa
    pela MESMA ponte tributo->quadro do endpoint web."""
    _require_service_scope(request, "guild_audit")
    a, org = _discord_operator(body.discord_user_id)
    actor_id = int(a["id"])
    try:
        res = tribute_store.approve(org, body.report_id, actor_id,
                                    note=body.note)
    except KeyError:
        raise HTTPException(404, "reporte não encontrado")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    _apply_report_to_chain(org, res, actor_id)
    return {"ok": True, **res}


@app.post("/api/discord/reject")
def discord_reject(body: DiscordAuditBody, request: Request):
    """Rejeita reporte pelo bot: relógio RETOMA. Escopo: guild_audit + papel
    operador; auditoria no nome do humano vinculado."""
    _require_service_scope(request, "guild_audit")
    a, org = _discord_operator(body.discord_user_id)
    try:
        res = tribute_store.reject(org, body.report_id, int(a["id"]),
                                   note=body.note)
    except KeyError:
        raise HTTPException(404, "reporte não encontrado")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, **res}


@app.get("/api/discord/board")
def discord_board(request: Request, discord_user_id: int = Query(gt=0)):
    """Quadro da SEMANA ATUAL da org do membro vinculado (qualquer papel).

    Escopo: discord_read. Por membro ativo da org: metas da semana com
    qty_aprovada (soma dos reportes 'approved' ligados à meta) e o estado do
    relógio. SQL só com placeholders (semana calculada em Python — nada de
    funções de data no SQL); roda na MESMA conexão do TributeStore p/ os
    testes injetarem o banco. Nunca vaza outra org (org_id em toda query)."""
    _require_service_scope(request, "discord_read")
    a = _discord_member(discord_user_id)
    org = int(a.get("org_id") or 1)
    _org_entitlement(org, "operacao")
    week = _tribute.week_start_iso()
    con, lock = tribute_store.con, tribute_store.lock
    with lock:
        try:
            accounts = con.execute(
                "SELECT id, username FROM auth_accounts "
                "WHERE org_id=? AND active=1 ORDER BY username",
                [org]).fetchall()
            metas = con.execute(
                "SELECT id, account_id, item_id, qty_target "
                "FROM weekly_assignments WHERE org_id=? AND week_start=? "
                "ORDER BY account_id, item_id", [org, week]).fetchall()
            approved = con.execute(
                "SELECT r.assignment_id, SUM(r.qty_reported) "
                "FROM member_reports r JOIN weekly_assignments a "
                "ON a.id = r.assignment_id AND a.org_id = r.org_id "
                "WHERE r.org_id=? AND r.status='approved' AND a.week_start=? "
                "GROUP BY r.assignment_id", [org, week]).fetchall()
        finally:
            try:
                con.rollback()
            except Exception:
                pass
    ok_by_meta = {r[0]: int(r[1] or 0) for r in approved}
    metas_by_member: dict[int, list] = {}
    for m in metas:
        metas_by_member.setdefault(int(m[1]), []).append(
            {"assignment_id": m[0], "item_id": m[2], "qty_target": m[3],
             "qty_aprovada": ok_by_meta.get(m[0], 0)})
    # member_status pega o MESMO lock: chamar fora do bloco acima (não-reentrante)
    clocks = {s["account_id"]: s for s in tribute_store.member_status(org)}
    members = []
    for acc_id, username in accounts:
        st = clocks.get(acc_id) or {}
        members.append({
            "account_id": acc_id, "username": username,
            "state": st.get("state"), "clock_days": st.get("clock_days"),
            "days_left": st.get("days_left"), "paused": st.get("paused"),
            "metas": metas_by_member.get(int(acc_id), [])})
    return {"week_start": week, "org_id": org, "members": members}


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
_ICON_ID_RE = re.compile(r"\A[A-Za-z0-9_@\-\.]+\Z")   # \Z (não $) barra \n final
_icon_sem = threading.Semaphore(4)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
               " (KHTML, like Gecko) Chrome/125.0 Safari/537.36")
# Cache NEGATIVO em memória: falha de download vale por 5 min. Sem ele, cada
# <img> quebrado no frontend dispararia um novo download (PowerShell + rede)
# a cada refresh — amplificação de falha contra o serviço de render e contra
# a própria máquina quando o Cloudflare bloqueia.
_ICON_NEG_TTL = 300.0
_icon_neg_cache: dict[tuple[str, int, int], float] = {}  # chave -> expiry (monotonic)
_ICON_LOG_MAX_BYTES = 5 * 1024 * 1024


def _log_icon_error(b: bytes):
    """Anexa ao icon_errors.log, truncando o arquivo acima de 5 MB — uma falha
    persistente (ex. bloqueio do Cloudflare) não pode encher o disco."""
    p = ICONS_DIR / "icon_errors.log"
    try:
        if p.exists() and p.stat().st_size > _ICON_LOG_MAX_BYTES:
            p.unlink()
        p.open("ab").write(b)
    except OSError:
        pass  # log de erro nunca derruba o handler


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
            _log_icon_error(b"rc=%d url=%s\n" % (r.returncode, url.encode())
                            + r.stderr[:2000] + b"\n---\n")
        if tmp.exists() and tmp.read_bytes()[:4] == b"\x89PNG":
            tmp.replace(dest)
            return True
        tmp.unlink(missing_ok=True)
    except (subprocess.SubprocessError, OSError) as e:
        # nunca usar print aqui: mensagens do Windows em PT-BR têm acentos e
        # console cp1252 lançaria UnicodeEncodeError dentro do handler
        _log_icon_error(("exc url=%s: %r\n---\n" % (url, e))
                        .encode("utf-8", "replace"))
    return False


@app.get("/icon/{item_id}")
def icon(item_id: str, quality: int = Query(0, ge=0, le=5),
         size: int = Query(64, ge=16, le=217)):
    if not _ICON_ID_RE.match(item_id):
        raise HTTPException(400, "id de item inválido")
    dest = ICONS_DIR / f"{item_id}_q{quality}_s{size}.png"
    if not dest.exists():
        # falha recente? devolve 404 direto sem tentar baixar de novo
        key = (item_id, quality, size)
        exp = _icon_neg_cache.get(key)
        if exp is not None:
            if exp > time.monotonic():
                return Response(status_code=404)
            _icon_neg_cache.pop(key, None)   # expirou: pode tentar de novo
        url = (f"https://render.albiononline.com/v1/item/{quote(item_id)}.png"
               f"?size={size}" + (f"&quality={quality}" if quality > 1 else ""))
        with _icon_sem:
            if not dest.exists() and not _download_icon(url, dest):
                if len(_icon_neg_cache) > 4096:   # não crescer sem limite
                    now = time.monotonic()
                    for k in [k for k, v in list(_icon_neg_cache.items())
                              if v <= now]:
                        _icon_neg_cache.pop(k, None)
                    # teto RÍGIDO: se ainda cheio de entradas vivas, corta as
                    # mais antigas (senão o dict cresceria ~TTL*taxa sem evicção)
                    excess = len(_icon_neg_cache) - 4096
                    if excess > 0:
                        for k, _v in sorted(_icon_neg_cache.items(),
                                            key=lambda kv: kv[1])[:excess]:
                            _icon_neg_cache.pop(k, None)
                _icon_neg_cache[key] = time.monotonic() + _ICON_NEG_TTL
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

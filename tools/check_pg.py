# -*- coding: utf-8 -*-
"""Validador do caminho Postgres — roda contra um banco REAL (Supabase).

Uso (no provisionamento):

    pip install "psycopg[binary]"
    set DATABASE_URL=postgresql://...   (Windows: $env:DATABASE_URL = "...")
    python tools/check_pg.py

O que faz: cria o schema, sobe o app com auth desligada e bate em TODOS os
endpoints sobreviventes do piloto. Como o banco está vazio, o esperado é HTTP
200 com listas vazias — qualquer 500 denuncia um erro de dialeto (SQL que vale
no SQLite mas não no Postgres). Imprime um relatório e sai com código !=0 se
algo falhar, para servir de "porta" antes de publicar.
"""
import os
import sys

# auth fora do caminho: validamos só o dialeto de dados
os.environ.setdefault("ALBION_AUTH_DISABLED", "1")


def main() -> int:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERRO: defina DATABASE_URL (string de conexão do Supabase).")
        return 2
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        print('ERRO: instale o driver -> pip install "psycopg[binary]"')
        return 2

    from albion import store
    print(f"backend = {store.backend()}  (esperado: postgres)")
    if store.backend() != "postgres":
        print("ERRO: DATABASE_URL não foi reconhecida; backend != postgres.")
        return 2

    # 1) schema
    con = store.connect()
    store.init_schema(con)
    con.close()
    print("schema criado/conferido OK")

    # 2) sobe o app e exercita os endpoints
    from fastapi.testclient import TestClient
    import app as appmod
    client = TestClient(appmod.app)

    # (rota, params) — endpoints agregados que LEEM o banco. Banco vazio => 200
    # com listas vazias; um 500 indica SQL incompatível com Postgres.
    checks = [
        ("/api/status", {}),
        ("/api/recommendations", {"min_daily_volume": 0, "limit": 3}),
        ("/api/recommendations", {"fused": "true", "min_daily_volume": 0,
                                  "limit": 3}),
        ("/api/prod", {"view": "focus", "limit": 3}),
        ("/api/prod", {"view": "refine", "limit": 3}),
        ("/api/logi", {"view": "bm", "limit": 3}),
        ("/api/logi", {"view": "ladder", "limit": 3}),
        ("/api/logi", {"view": "restock", "limit": 3}),
        ("/api/risk", {"view": "profile", "limit": 3}),
        ("/api/risk", {"view": "corr", "limit": 3}),
        ("/api/micro", {"view": "spread", "limit": 3}),
        ("/api/micro", {"view": "book", "limit": 3}),
        ("/api/micro", {"view": "traps", "limit": 3}),
        ("/api/micro", {"view": "capital", "limit": 3}),
        ("/api/guild", {"view": "makeorbuy", "limit": 3}),
        ("/api/guild", {"view": "watch", "limit": 3}),
        ("/api/guild", {"view": "kit"}),
        ("/api/logi", {"view": "restock", "limit": 3}),
        ("/api/item_signals", {"item": "T4_BAG"}),
    ]
    failures = []
    for path, params in checks:
        try:
            r = client.get(path, params=params)
        except Exception as e:  # erro de dialeto vira exceção aqui
            failures.append((path, params, "EXC", repr(e)[:300]))
            print(f"  ✗ {path} {params} -> EXCEÇÃO {type(e).__name__}")
            continue
        tag = "ok" if r.status_code < 500 else "ERRO500"
        mark = "✓" if r.status_code < 500 else "✗"
        print(f"  {mark} {path} {params} -> {r.status_code} {tag}")
        if r.status_code >= 500:
            failures.append((path, params, r.status_code, r.text[:300]))

    # 3) auth no Postgres: um login com credencial inválida exercita os
    # caminhos PG mais delicados (ON CONFLICT do throttle, INSERT SERIAL de
    # auditoria, commit-and-raise). Esperado: AuthError (não erro de dialeto).
    try:
        import threading
        from albion.auth import AuthManager, AuthError
        am = AuthManager(store.connect(), threading.Lock())
        am.has_admin()  # leitura
        try:
            am.login("qa_inexistente", "senhaErrada123!")
            print("  ✗ auth: login inválido NÃO levantou (inesperado)")
            failures.append(("auth.login", {}, "no-raise", "deveria recusar"))
        except AuthError:
            print("  ✓ auth: schema + throttle/ON CONFLICT + audit OK")
    except Exception as e:
        print(f"  ✗ auth: ERRO de dialeto {type(e).__name__}: {repr(e)[:200]}")
        failures.append(("auth", {}, "EXC", repr(e)[:300]))

    print("-" * 60)
    if failures:
        print(f"FALHOU: {len(failures)} endpoint(s) com erro de dialeto:")
        for path, params, code, detail in failures:
            print(f"  {path} {params} [{code}]\n     {detail}")
        return 1
    print("TUDO OK — o caminho Postgres está válido para todos os endpoints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

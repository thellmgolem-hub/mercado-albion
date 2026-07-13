# -*- coding: utf-8 -*-
"""Gera uma senha nova para o admin no banco apontado por DATABASE_URL.

Uso (nuvem): definir DATABASE_URL (a URL do Supabase) e rodar
    python tools/reset_cloud_admin.py

Acha o administrador ativo mais antigo, reseta a senha (que passa a exigir
troca no 1o login) e imprime usuario + senha nova UMA vez. Nada e gravado em
texto; a senha some da tela quando voce fechar.
"""
import os
import sys

# permite rodar de qualquer lugar: coloca a raiz do projeto no sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main():
    if not os.environ.get("DATABASE_URL"):
        print("ERRO: defina a variavel DATABASE_URL (a URL do Supabase) antes "
              "de rodar este script.")
        print("      No Render: Environment -> olho na linha DATABASE_URL -> copie.")
        return 2

    from albion.client import AODP
    from albion.auth import AuthManager

    client = AODP()
    try:
        mgr = AuthManager(client.db, client.db_lock)
        with mgr.lock:
            row = mgr.con.execute(
                "SELECT id, username FROM auth_accounts "
                "WHERE role='admin' AND active=1 ORDER BY id LIMIT 1"
            ).fetchone()
        if not row:
            print("Nenhum administrador ativo foi encontrado no banco.")
            print("Verifique se a DATABASE_URL e a do Supabase de producao.")
            return 1
        admin_id, username = row[0], row[1]
        out = mgr.reset_password(admin_id, admin_id)
        print("")
        print("=" * 52)
        print("  Senha do admin redefinida com sucesso!")
        print("")
        print(f"    USUARIO:  {username}")
        print(f"    SENHA:    {out['temporary_password']}")
        print("")
        print("  Entre em https://mercado-albion.onrender.com com esses")
        print("  dados. O sistema vai pedir para voce criar uma senha nova.")
        print("=" * 52)
        return 0
    finally:
        try:
            client.db.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

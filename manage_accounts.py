# -*- coding: utf-8 -*-
"""Administracao local de contas pseudonimas do Mercado Albion.

As senhas temporarias aparecem uma unica vez e nunca sao gravadas em texto.
"""
import argparse
import json
import sys

from albion.auth import AuthError, AuthManager, PROFILES, ROLES
from albion.client import AODP


def _profiles(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _manager():
    client = AODP()
    return client, AuthManager(client.db, client.db_lock)


def _actor_id(manager):
    with manager.lock:
        row = manager.con.execute(
            "SELECT id FROM auth_accounts WHERE role='admin' AND active=1 "
            "ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise AuthError("Nenhum administrador ativo.", "no_admin", 409)
    return row[0]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Gerencia contas locais sem e-mail ou dados pessoais.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap", help="cria o primeiro administrador")
    p.add_argument("--username", required=True)
    p.add_argument("--display-name")

    p = sub.add_parser("create", help="cria conta com senha temporaria")
    p.add_argument("--username", required=True)
    p.add_argument("--role", choices=ROLES, default="member")
    p.add_argument("--profiles", default="")
    p.add_argument("--display-name")
    p.add_argument("--albion-nick")
    p.add_argument("--discord-nick")

    sub.add_parser("list", help="lista contas sem segredos")
    for name, help_text in (
            ("reset-password", "gera nova senha temporaria"),
            ("reset-device", "libera vinculo para outro dispositivo"),
            ("disable", "desativa a conta e suas sessoes"),
            ("enable", "reativa a conta")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("account_id", type=int)

    p = sub.add_parser("set-role", help="altera o papel e revoga sessoes")
    p.add_argument("account_id", type=int)
    p.add_argument("role", choices=ROLES)

    args = parser.parse_args(argv)
    client, manager = _manager()
    try:
        if args.command == "bootstrap":
            out = manager.bootstrap_admin(args.username, args.display_name)
        elif args.command == "list":
            out = manager.list_accounts()
        else:
            actor = _actor_id(manager)
            if args.command == "create":
                out = manager.create_account(
                    actor, args.username, role=args.role,
                    profiles=_profiles(args.profiles),
                    display_name=args.display_name,
                    albion_nick=args.albion_nick,
                    discord_nick=args.discord_nick)
            elif args.command == "reset-password":
                out = manager.reset_password(actor, args.account_id)
            elif args.command == "reset-device":
                manager.reset_device(actor, args.account_id)
                out = {"ok": True}
            elif args.command in ("disable", "enable"):
                out = manager.update_account(
                    actor, args.account_id, active=args.command == "enable")
            else:
                out = manager.update_account(
                    actor, args.account_id, role=args.role)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if isinstance(out, dict) and out.get("temporary_password"):
            print("\nATENCAO: anote a senha temporaria agora; ela nao sera exibida de novo.")
        return 0
    except AuthError as exc:
        print(f"erro [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    finally:
        client.db.close()


if __name__ == "__main__":
    raise SystemExit(main())

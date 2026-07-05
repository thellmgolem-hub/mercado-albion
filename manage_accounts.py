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
            ("reset-device", "libera os IPs registrados da conta (limite de 2)"),
            ("disable", "desativa a conta e suas sessoes"),
            ("enable", "reativa a conta")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("account_id", type=int)

    p = sub.add_parser("set-role", help="altera o papel e revoga sessoes")
    p.add_argument("account_id", type=int)
    p.add_argument("role", choices=ROLES)

    p = sub.add_parser(
        "service-token",
        help="tokens de servico do bot Discord (create|list|revoke)")
    p.add_argument("action", choices=["create", "list", "revoke"])
    p.add_argument("--label", help="nome unico do token (create)")
    p.add_argument("--scopes", default="",
                   help="escopos separados por virgula, ex.: "
                        "discord_link,discord_read")
    p.add_argument("--id", type=int, dest="token_id",
                   help="id do token (revoke; veja em list)")

    p = sub.add_parser(
        "link-code",
        help="gera codigo de vinculo Discord da conta (15 min, 1 uso)")
    p.add_argument("account_id", type=int)

    args = parser.parse_args(argv)
    client, manager = _manager()
    try:
        if args.command == "bootstrap":
            out = manager.bootstrap_admin(args.username, args.display_name)
        elif args.command == "list":
            out = manager.list_accounts()
        elif args.command == "service-token" and args.action == "list":
            out = manager.list_service_tokens()
        else:
            actor = _actor_id(manager)
            if args.command == "service-token":
                if args.action == "create":
                    if not args.label:
                        parser.error("--label e obrigatorio em create")
                    out = manager.create_service_token(
                        args.label, _profiles(args.scopes), actor_id=actor)
                else:   # revoke
                    if args.token_id is None:
                        parser.error("--id e obrigatorio em revoke")
                    out = {"ok": manager.revoke_service_token(
                        args.token_id, actor_id=actor)}
            elif args.command == "link-code":
                out = {"account_id": args.account_id,
                       "code": manager.gen_link_code(
                           args.account_id, actor_id=actor),
                       "expires_in_s": 900}
            elif args.command == "create":
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
        if isinstance(out, dict) and out.get("token"):
            print("\nATENCAO: anote o token de servico agora; o banco guarda "
                  "so o hash e ele nao sera exibido de novo.")
        return 0
    except AuthError as exc:
        print(f"erro [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    finally:
        client.db.close()


if __name__ == "__main__":
    raise SystemExit(main())

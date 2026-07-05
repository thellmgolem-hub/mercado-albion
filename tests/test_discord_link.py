# -*- coding: utf-8 -*-
"""Discord Fase 1 — tokens de serviço + vínculo membro→conta→org.

Duas camadas: AuthManager (token/código/vínculo em SQLite :memory:, o mesmo
caminho dual do Postgres) e a API via TestClient (guard com X-Service-Token,
escopos, isolamento do membro e rejeição do ctx de serviço no console).
"""
import os
import sqlite3
import threading
import unittest

from fastapi.testclient import TestClient

# Mesmo molde do test_core: o app importa config com auth desligada e cada
# teste de API religa AUTH_REQUIRED explicitamente (e restaura no tearDown).
os.environ.setdefault("ALBION_AUTH_DISABLED", "1")

import app
from albion.auth import AuthError, AuthManager
from albion.tribute import TributeStore

SNOWFLAKE = 111222333444555666        # discord_user_id (64 bits) do membro 1
SNOWFLAKE_2 = 999888777666555444      # do membro 2 (nunca vinculado nos setUp)


class ServiceTokenAndLinkTests(unittest.TestCase):
    """AuthManager: ciclo de vida de token de serviço e código de vínculo."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.auth = AuthManager(self.con, threading.Lock())
        self.admin = self.auth.bootstrap_admin("admin.local")
        self.member = self.auth.create_account(
            self.admin["account_id"], "membro.um", role="member")

    def tearDown(self):
        self.con.close()

    def test_token_criado_guarda_hash_e_autentica(self):
        res = self.auth.create_service_token(
            "discord_bot", ["discord_read", "discord_link"])
        token = res["token"]
        self.assertTrue(token.startswith("svc_"))
        row = self.con.execute(
            "SELECT label,token_hash,scopes_json FROM auth_service_tokens"
        ).fetchone()
        self.assertEqual(row[0], "discord_bot")
        self.assertNotIn(token, (row[1], row[2]))   # segredo NUNCA em claro
        ctx = self.auth.authenticate_service(token)
        self.assertTrue(ctx["service"])
        self.assertEqual(ctx["name"], "discord_bot")
        self.assertEqual(ctx["scopes"], ["discord_link", "discord_read"])
        self.assertIsNone(ctx["account"])           # sem conta: fora do console

    def test_token_invalido_revogado_ou_sem_prefixo(self):
        res = self.auth.create_service_token("bot", ["discord_read"])
        self.assertIsNone(self.auth.authenticate_service("svc_forjado"))
        self.assertIsNone(self.auth.authenticate_service(None))
        self.assertIsNone(self.auth.authenticate_service("qualquercoisa"))
        self.assertTrue(self.auth.revoke_service_token(res["id"]))
        self.assertIsNone(self.auth.authenticate_service(res["token"]))
        self.assertFalse(self.auth.revoke_service_token(res["id"]))  # 2x = no-op

    def test_escopo_desconhecido_e_label_duplicado(self):
        with self.assertRaises(AuthError) as ctx:
            self.auth.create_service_token("bot", ["root_total"])
        self.assertEqual(ctx.exception.code, "invalid_scope")
        with self.assertRaises(AuthError):
            self.auth.create_service_token("bot", [])
        self.auth.create_service_token("bot", ["discord_read"])
        with self.assertRaises(AuthError) as ctx:
            self.auth.create_service_token("bot", ["discord_read"])
        self.assertEqual(ctx.exception.code, "label_exists")

    def test_link_code_um_uso(self):
        mid = self.member["account_id"]
        code = self.auth.gen_link_code(mid)
        self.assertEqual(len(code), 8)
        acct = self.auth.link_discord(code, SNOWFLAKE)
        self.assertEqual(acct["id"], mid)
        got = self.auth.get_account_by_discord_id(SNOWFLAKE)
        self.assertEqual(got["id"], mid)
        with self.assertRaises(AuthError) as ctx:       # reuso do MESMO código
            self.auth.link_discord(code, SNOWFLAKE_2)
        self.assertEqual(ctx.exception.code, "link_code_used")

    def test_link_code_expira(self):
        code = self.auth.gen_link_code(self.member["account_id"], ttl=-1)
        with self.assertRaises(AuthError) as ctx:
            self.auth.link_discord(code, SNOWFLAKE)
        self.assertEqual(ctx.exception.code, "link_code_expired")
        self.assertIsNone(self.auth.get_account_by_discord_id(SNOWFLAKE))

    def test_link_code_inexistente_e_snowflake_ja_vinculado(self):
        with self.assertRaises(AuthError) as ctx:
            self.auth.link_discord("NAOEXIST", SNOWFLAKE)
        self.assertEqual(ctx.exception.code, "link_code_invalid")
        mid = self.member["account_id"]
        self.auth.link_discord(self.auth.gen_link_code(mid), SNOWFLAKE)
        other = self.auth.create_account(
            self.admin["account_id"], "membro.dois", role="member")
        code2 = self.auth.gen_link_code(other["account_id"])
        with self.assertRaises(AuthError) as ctx:   # 1 snowflake -> 1 conta
            self.auth.link_discord(code2, SNOWFLAKE)
        self.assertEqual(ctx.exception.code, "discord_already_linked")

    def test_snowflake_desconhecido_devolve_none(self):
        self.assertIsNone(self.auth.get_account_by_discord_id(123))
        self.assertIsNone(self.auth.get_account_by_discord_id("abc"))


class DiscordApiTests(unittest.TestCase):
    """API /api/discord/* pelo guard real (X-Service-Token + escopos)."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        lock = threading.Lock()
        self.auth = AuthManager(self.con, lock)
        self.tribute = TributeStore(self.con, lock)
        self.admin = self.auth.bootstrap_admin("admin.local")
        aid = self.admin["account_id"]
        self.m1 = self.auth.create_account(aid, "membro.um",
                                           role="member")["account_id"]
        self.m2 = self.auth.create_account(aid, "membro.dois",
                                           role="member")["account_id"]
        self.auth.link_discord(self.auth.gen_link_code(self.m1), SNOWFLAKE)
        # metas na org 1: uma p/ cada membro — my-assignments não pode vazar
        self.tribute.assign(1, self.m1, "T4_ORE", 100, created_by=aid)
        self.tribute.assign(1, self.m2, "T4_WOOD", 50, created_by=aid)
        self.bot = self.auth.create_service_token(
            "discord_bot", ["discord_link", "discord_read"])["token"]
        self.link_only = self.auth.create_service_token(
            "bot_link", ["discord_link"])["token"]
        self.read_only = self.auth.create_service_token(
            "bot_read", ["discord_read"])["token"]
        self._old = (app.auth_manager, app.tribute_store,
                     app.config.AUTH_REQUIRED)
        app.auth_manager = self.auth
        app.tribute_store = self.tribute
        app.config.AUTH_REQUIRED = True
        self.client = TestClient(app.app)

    def tearDown(self):
        (app.auth_manager, app.tribute_store,
         app.config.AUTH_REQUIRED) = self._old
        self.con.close()

    def _get(self, path, token, **params):
        return self.client.get(path, params=params,
                               headers={"X-Service-Token": token})

    def test_whoami_com_token_valido(self):
        r = self._get("/api/discord/whoami", self.bot,
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"account_id": self.m1,
                                    "username": "membro.um",
                                    "role": "member", "org_id": 1})

    def test_token_invalido_401_e_ausente_401(self):
        r = self._get("/api/discord/whoami", "svc_forjado",
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "service_invalid")
        # sem header e sem cookie: cai no fluxo de sessão -> 401
        r = self.client.get("/api/discord/whoami",
                            params={"discord_user_id": SNOWFLAKE})
        self.assertEqual(r.status_code, 401)

    def test_escopo_errado_403(self):
        r = self._get("/api/discord/whoami", self.link_only,
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "service_scope")
        r = self.client.post("/api/discord/link",
                             headers={"X-Service-Token": self.read_only},
                             json={"code": "ABCD2345",
                                   "discord_user_id": SNOWFLAKE_2})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "service_scope")

    def test_link_via_api_um_uso(self):
        code = self.auth.gen_link_code(self.m2)
        r = self.client.post("/api/discord/link",
                             headers={"X-Service-Token": self.bot},
                             json={"code": code,
                                   "discord_user_id": SNOWFLAKE_2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["account_id"], self.m2)
        self.assertEqual(r.json()["org_id"], 1)
        who = self._get("/api/discord/whoami", self.bot,
                        discord_user_id=SNOWFLAKE_2)
        self.assertEqual(who.json()["username"], "membro.dois")
        again = self.client.post("/api/discord/link",
                                 headers={"X-Service-Token": self.bot},
                                 json={"code": code,
                                       "discord_user_id": SNOWFLAKE_2})
        self.assertEqual(again.status_code, 409)      # código é de 1 uso
        self.assertEqual(again.json()["code"], "link_code_used")

    def test_whoami_sem_vinculo_404(self):
        r = self._get("/api/discord/whoami", self.bot,
                      discord_user_id=42424242)
        self.assertEqual(r.status_code, 404)

    def test_my_assignments_so_do_proprio_membro(self):
        r = self._get("/api/discord/my-assignments", self.bot,
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 200)
        rows = r.json()["assignments"]
        self.assertEqual([a["item_id"] for a in rows], ["T4_ORE"])
        self.assertEqual({a["account_id"] for a in rows}, {self.m1})
        # a meta do membro 2 (T4_WOOD) NUNCA aparece pelo snowflake do 1

    def test_my_status_so_do_proprio_membro(self):
        r = self._get("/api/discord/my-status", self.bot,
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 200)
        st = r.json()["status"]
        self.assertEqual(st["account_id"], self.m1)
        self.assertIn("state", st)

    def test_org_sem_entitlement_403_igual_web(self):
        # move o membro p/ uma org sem direito 'operacao' (fail-closed)
        self.con.execute("UPDATE auth_accounts SET org_id=99 WHERE id=?",
                         [self.m1])
        self.con.commit()
        for path in ("/api/discord/my-assignments", "/api/discord/my-status"):
            r = self._get(path, self.bot, discord_user_id=SNOWFLAKE)
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["code"], "entitlement_missing")
        # whoami segue funcionando (não expõe dados de tributo)
        r = self._get("/api/discord/whoami", self.bot,
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["org_id"], 99)

    def test_ctx_de_servico_nao_acessa_console(self):
        # endpoints de humano exigem conta/role: serviço barra por construção
        r = self._get("/api/guild/pending", self.bot)
        self.assertEqual(r.status_code, 403)
        r = self._get("/api/admin/accounts", self.bot)
        self.assertEqual(r.status_code, 403)
        # e POST de console sem CSRF nem conta também não passa
        r = self.client.post("/api/guild/approve",
                             headers={"X-Service-Token": self.bot},
                             json={"report_id": 1})
        self.assertEqual(r.status_code, 403)
        # regressão do escape de escopo: /api/prodchain/chains só tem gate de
        # entitlement (sem role) — o ctx de serviço tem de bater no 403
        # account_required, nunca listar/ler cadeias da org.
        r = self._get("/api/prodchain/chains", self.bot)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("detail", {}).get("code")
                         if isinstance(r.json().get("detail"), dict)
                         else r.json().get("code"), "account_required")


if __name__ == "__main__":
    unittest.main()

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


class DiscordTributeApiTests(unittest.TestCase):
    """Tributo pelo bot: /api/discord/report|pending|approve|reject.

    Gate DUPLO nos de auditoria: escopo do token (guild_audit) E papel de
    operador do HUMANO vinculado. Org 1 (entitlement real seeded) — o quadro
    2-orgs vive em DiscordBoardTests."""

    OP_SNOWFLAKE = 555444333222111000     # admin vinculado (auditor humano)

    def setUp(self):
        from albion import prodchain
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        lock = threading.Lock()
        self.auth = AuthManager(self.con, lock)
        self.tribute = TributeStore(self.con, lock)
        self.chains = prodchain.ChainStore(self.con, lock)
        self.admin = self.auth.bootstrap_admin("admin.local")
        aid = self.admin["account_id"]
        self.m1 = self.auth.create_account(aid, "membro.um",
                                           role="member")["account_id"]
        self.m2 = self.auth.create_account(aid, "membro.dois",
                                           role="member")["account_id"]
        self.auth.link_discord(self.auth.gen_link_code(self.m1), SNOWFLAKE)
        self.auth.link_discord(self.auth.gen_link_code(aid),
                               self.OP_SNOWFLAKE)   # humano operador (admin)
        self.report_tok = self.auth.create_service_token(
            "bot_report", ["guild_report"])["token"]
        self.audit_tok = self.auth.create_service_token(
            "bot_audit", ["guild_audit"])["token"]
        self.read_tok = self.auth.create_service_token(
            "bot_read", ["discord_read"])["token"]
        self._old = (app.auth_manager, app.tribute_store, app.chain_store,
                     app.config.AUTH_REQUIRED)
        app.auth_manager = self.auth
        app.tribute_store = self.tribute
        app.chain_store = self.chains
        app.config.AUTH_REQUIRED = True
        self.client = TestClient(app.app)

    def tearDown(self):
        (app.auth_manager, app.tribute_store, app.chain_store,
         app.config.AUTH_REQUIRED) = self._old
        self.con.close()

    def _post(self, path, token, **body):
        return self.client.post(path, json=body,
                                headers={"X-Service-Token": token})

    def _get(self, path, token, **params):
        return self.client.get(path, params=params,
                               headers={"X-Service-Token": token})

    def _report(self, assignment_id=None, qty=30, item="T4_ORE"):
        body = {"discord_user_id": SNOWFLAKE, "item_id": item, "qty": qty}
        if assignment_id is not None:
            body["assignment_id"] = assignment_id
        return self._post("/api/discord/report", self.report_tok, **body)

    def test_report_pausa_relogio_e_e_auto_reporte(self):
        a = self.tribute.assign(1, self.m1, "T4_ORE", 100,
                                created_by=self.admin["account_id"])
        r = self._report(assignment_id=a["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "pendente")
        st = self.tribute.member_status(1, account_id=self.m1)[0]
        self.assertEqual(st["state"], "pendente")
        self.assertTrue(st["paused"])                 # relógio PAUSADO
        ev = [e for e in self.tribute.audit_log(1) if e["action"] == "report"]
        self.assertEqual(ev[0]["actor_account_id"], self.m1)   # ele mesmo
        self.assertFalse(ev[0]["details"]["on_behalf"])

    def test_report_escopo_errado_403_e_sem_vinculo_404(self):
        r = self._post("/api/discord/report", self.read_tok,
                       discord_user_id=SNOWFLAKE, item_id="T4_ORE", qty=5)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "service_scope")
        r = self._post("/api/discord/report", self.report_tok,
                       discord_user_id=42424242, item_id="T4_ORE", qty=5)
        self.assertEqual(r.status_code, 404)

    def test_report_meta_de_outro_membro_404(self):
        a2 = self.tribute.assign(1, self.m2, "T4_WOOD", 50,
                                 created_by=self.admin["account_id"])
        r = self._report(assignment_id=a2["id"])       # meta NÃO é do m1
        self.assertEqual(r.status_code, 404)

    def test_auditoria_escopo_errado_403(self):
        for path, tok in (("/api/discord/approve", self.report_tok),
                          ("/api/discord/approve", self.read_tok),
                          ("/api/discord/reject", self.read_tok)):
            r = self._post(path, tok, discord_user_id=self.OP_SNOWFLAKE,
                           report_id=1)
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["code"], "service_scope")
        r = self._get("/api/discord/pending", self.read_tok,
                      discord_user_id=self.OP_SNOWFLAKE)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "service_scope")

    def test_gate_duplo_membro_comum_com_token_audit_403(self):
        # token TEM guild_audit, mas o humano vinculado é 'member' -> 403
        r = self._get("/api/discord/pending", self.audit_tok,
                      discord_user_id=SNOWFLAKE)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "forbidden")
        for path in ("/api/discord/approve", "/api/discord/reject"):
            r = self._post(path, self.audit_tok, discord_user_id=SNOWFLAKE,
                           report_id=1)
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["code"], "forbidden")

    def test_pending_lista_fila_da_org_do_operador(self):
        self._report()                                 # reporte-bônus do m1
        r = self._get("/api/discord/pending", self.audit_tok,
                      discord_user_id=self.OP_SNOWFLAKE)
        self.assertEqual(r.status_code, 200)
        fila = r.json()["pending"]
        self.assertEqual(len(fila), 1)
        self.assertEqual(fila[0]["account_id"], self.m1)

    def test_approve_zera_relogio_e_alimenta_ponte(self):
        cid = self.chains.save(1, 10, "linha da guild", {
            "v": 1, "roots": ["T4_METALBAR"],
            "ns": {"T4_METALBAR": {"mode": "make"}, "T4_ORE": {"mode": "buy"}},
            "stock": {"T4_ORE": 5}})
        a = self.tribute.assign(1, self.m1, "T4_ORE", 100, from_chain_id=cid,
                                created_by=self.admin["account_id"])
        rid = self._report(assignment_id=a["id"], qty=30).json()["id"]
        r = self._post("/api/discord/approve", self.audit_tok,
                       discord_user_id=self.OP_SNOWFLAKE, report_id=rid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "em_dia")
        st = self.tribute.member_status(1, account_id=self.m1)[0]
        self.assertEqual(st["state"], "em_dia")        # relógio ZEROU
        self.assertEqual(st["clock_days"], 0)
        self.assertFalse(st["paused"])
        ch = self.chains.get(1, 10, cid)               # ponte tributo->quadro
        self.assertEqual(ch["payload"]["stock"]["T4_ORE"], 35)   # 5 + 30
        ev = [e for e in self.tribute.audit_log(1) if e["action"] == "approve"]
        self.assertEqual(ev[0]["actor_account_id"],
                         self.admin["account_id"])     # humano, não o bot
        acts = [e["action"] for e in self.tribute.audit_log(1)]
        self.assertNotIn("chain_bridge_fail", acts)
        # reprocessar o MESMO reporte -> 409 (igual à web)
        again = self._post("/api/discord/approve", self.audit_tok,
                           discord_user_id=self.OP_SNOWFLAKE, report_id=rid)
        self.assertEqual(again.status_code, 409)

    def test_reject_retoma_relogio_e_404_inexistente(self):
        rid = self._report().json()["id"]
        r = self._post("/api/discord/reject", self.audit_tok,
                       discord_user_id=self.OP_SNOWFLAKE, report_id=rid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "atrasado")   # RETOMOU a contagem
        st = self.tribute.member_status(1, account_id=self.m1)[0]
        self.assertFalse(st["paused"])
        r = self._post("/api/discord/reject", self.audit_tok,
                       discord_user_id=self.OP_SNOWFLAKE, report_id=99999)
        self.assertEqual(r.status_code, 404)


class DiscordBoardTests(unittest.TestCase):
    """/api/discord/board: quadro semanal da org do membro — 2 orgs no
    fixture, o quadro NUNCA vaza a outra. Entitlements ficam no MESMO banco
    in-memory (app.aodp apontado p/ ele), p/ a org 2 ter direito 'operacao'."""

    SNOW_ORG2 = 777666555444333222

    def setUp(self):
        import time as _time
        import types
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
        self.m3 = self.auth.create_account(aid, "membro.outra.org",
                                           role="member")["account_id"]
        self.con.execute("UPDATE auth_accounts SET org_id=2 WHERE id=?",
                         [self.m3])
        # direitos das DUAS orgs no banco do teste (dialeto do store)
        now = _time.time()
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS org_entitlements ("
            "org_id INTEGER NOT NULL, scope TEXT NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1, expires_at TEXT, "
            "updated_at REAL NOT NULL, PRIMARY KEY (org_id, scope))")
        for org in (1, 2):
            self.con.execute(
                "INSERT INTO org_entitlements (org_id, scope, active, "
                "expires_at, updated_at) VALUES (?, 'operacao', 1, NULL, ?)",
                [org, now])
        self.con.commit()
        self.auth.link_discord(self.auth.gen_link_code(self.m1), SNOWFLAKE)
        self.auth.link_discord(self.auth.gen_link_code(self.m3),
                               self.SNOW_ORG2)
        # metas da SEMANA ATUAL nas duas orgs + reportes do m1
        a1 = self.tribute.assign(1, self.m1, "T4_ORE", 100, created_by=aid)
        self.tribute.assign(1, self.m2, "T4_WOOD", 50, created_by=aid)
        a3 = self.tribute.assign(2, self.m3, "T5_HIDE", 40, created_by=aid)
        for qty in (30, 20):                       # 2 aprovados = 50
            rep = self.tribute.report(1, self.m1, "T4_ORE", qty,
                                      assignment_id=a1["id"])
            self.tribute.approve(1, rep["id"], aid)
        self.tribute.report(1, self.m1, "T4_ORE", 10,
                            assignment_id=a1["id"])   # pendente NÃO soma
        rep3 = self.tribute.report(2, self.m3, "T5_HIDE", 15,
                                   assignment_id=a3["id"])
        self.tribute.approve(2, rep3["id"], aid)
        self.read_tok = self.auth.create_service_token(
            "bot_read", ["discord_read"])["token"]
        self.link_tok = self.auth.create_service_token(
            "bot_link", ["discord_link"])["token"]
        self._old = (app.auth_manager, app.tribute_store, app.aodp,
                     app.config.AUTH_REQUIRED)
        app.auth_manager = self.auth
        app.tribute_store = self.tribute
        # _org_entitlement lê aodp.db: aponta p/ o banco do teste
        app.aodp = types.SimpleNamespace(db=self.con, db_lock=lock)
        app.config.AUTH_REQUIRED = True
        self.client = TestClient(app.app)

    def tearDown(self):
        (app.auth_manager, app.tribute_store, app.aodp,
         app.config.AUTH_REQUIRED) = self._old
        self.con.close()

    def _board(self, token, snowflake):
        return self.client.get("/api/discord/board",
                               params={"discord_user_id": snowflake},
                               headers={"X-Service-Token": token})

    def test_board_soma_qty_aprovada_e_traz_relogio(self):
        from albion.tribute import week_start_iso
        r = self._board(self.read_tok, SNOWFLAKE)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["week_start"], week_start_iso())
        self.assertEqual(data["org_id"], 1)
        por_nome = {m["username"]: m for m in data["members"]}
        meta1 = por_nome["membro.um"]["metas"][0]
        self.assertEqual(meta1["item_id"], "T4_ORE")
        self.assertEqual(meta1["qty_target"], 100)
        self.assertEqual(meta1["qty_aprovada"], 50)    # 30+20; pendente fora
        self.assertEqual(por_nome["membro.um"]["state"], "pendente")
        meta2 = por_nome["membro.dois"]["metas"][0]
        self.assertEqual(meta2["item_id"], "T4_WOOD")
        self.assertEqual(meta2["qty_aprovada"], 0)     # sem reporte aprovado
        self.assertEqual(por_nome["admin.local"]["metas"], [])

    def test_board_nao_vaza_outra_org(self):
        r1 = self._board(self.read_tok, SNOWFLAKE).json()
        nomes1 = {m["username"] for m in r1["members"]}
        itens1 = {mt["item_id"] for m in r1["members"] for mt in m["metas"]}
        self.assertNotIn("membro.outra.org", nomes1)
        self.assertNotIn("T5_HIDE", itens1)
        r2 = self._board(self.read_tok, self.SNOW_ORG2).json()
        self.assertEqual(r2["org_id"], 2)
        nomes2 = {m["username"] for m in r2["members"]}
        self.assertEqual(nomes2, {"membro.outra.org"})
        meta3 = r2["members"][0]["metas"][0]
        self.assertEqual(meta3["item_id"], "T5_HIDE")
        self.assertEqual(meta3["qty_aprovada"], 15)

    def test_board_escopo_errado_403_e_sem_vinculo_404(self):
        r = self._board(self.link_tok, SNOWFLAKE)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "service_scope")
        r = self._board(self.read_tok, 42424242)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()

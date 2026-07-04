# -*- coding: utf-8 -*-
"""Tributo da guild: máquina de estados do relógio de 14 dias (TributeStore).

Cobre as 7 transições do plano (docs/PLANO_DISCORD_GUILD.md §5.2) uma a uma
com relógio INJETADO (now), a idempotência do clock_tick e o isolamento entre
orgs. Tudo em SQLite :memory: — o mesmo caminho dual que roda no Postgres.
"""
import sqlite3
import threading
import unittest
from datetime import datetime, timezone

from albion import tribute
from albion.tribute import (DISMISS_AFTER_DAYS, LATE_AFTER_DAYS, STATE_LATE,
                            STATE_OFF, STATE_OK, STATE_PENDING, TributeStore)

DAY = 86400.0
T0 = 1_751_000_000.0   # marco zero do relógio injetado (unix, arbitrário)


class TributeStateMachineTests(unittest.TestCase):
    """As 7 transições da §5.2, com o relógio avançado à mão."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.ts = TributeStore(self.con, threading.Lock())
        # meta semanal cria o relógio do membro 10 (em_dia, âncora = T0)
        self.aid = self.ts.assign(1, 10, "T4_ORE", 100,
                                  created_by=1, now=T0)["id"]

    def tearDown(self):
        self.con.close()

    def _status(self, now, org=1, acc=10):
        rows = self.ts.member_status(org, account_id=acc, now=now)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_t1_em_dia_reporta_vira_pendente_e_pausa(self):
        r = self.ts.report(1, 10, "T4_ORE", 100,
                           assignment_id=self.aid, now=T0 + 2 * DAY)
        self.assertEqual(r["state"], STATE_PENDING)
        # relógio CONGELADO na pausa: 7 dias depois ainda marca 2 dias
        st = self._status(T0 + 9 * DAY)
        self.assertEqual(st["state"], STATE_PENDING)
        self.assertTrue(st["paused"])
        self.assertEqual(st["clock_days"], 2)

    def test_t2_em_dia_tick_7_dias_vira_atrasado(self):
        res = self.ts.clock_tick(now=T0 + LATE_AFTER_DAYS * DAY)
        self.assertEqual(res["para_atrasado"], 1)
        self.assertEqual(res["desligados"], 0)
        st = self._status(T0 + LATE_AFTER_DAYS * DAY)
        self.assertEqual(st["state"], STATE_LATE)
        self.assertEqual(st["clock_days"], 7)

    def test_t3_pendente_aprovado_zera_e_volta_em_dia(self):
        rid = self.ts.report(1, 10, "T4_ORE", 100, now=T0 + 2 * DAY)["id"]
        res = self.ts.approve(1, rid, 1, note="conferido", now=T0 + 3 * DAY)
        self.assertEqual(res["state"], STATE_OK)
        self.assertFalse(res["reactivated"])
        # âncora reescrita: 6 dias após a aprovação o relógio marca 6, não 9
        st = self._status(T0 + 9 * DAY)
        self.assertEqual(st["state"], STATE_OK)
        self.assertEqual(st["clock_days"], 6)

    def test_t4_pendente_rejeitado_retoma_do_mesmo_marco(self):
        rid = self.ts.report(1, 10, "T4_ORE", 100, now=T0 + 2 * DAY)["id"]
        res = self.ts.reject(1, rid, 1, note="baú vazio", now=T0 + 3 * DAY)
        self.assertEqual(res["state"], STATE_LATE)
        # a contagem RETOMA da âncora ANTIGA (T0): aos 8 dias marca 8, não 5
        st = self._status(T0 + 8 * DAY)
        self.assertEqual(st["state"], STATE_LATE)
        self.assertFalse(st["paused"])
        self.assertEqual(st["clock_days"], 8)

    def test_t4b_rejeicao_com_outro_pendente_mantem_pausa(self):
        # metas por (membro, semana, ITEM): dois reportes pendentes é o caso
        # normal. Rejeitar UM não pode retomar o relógio enquanto o outro
        # aguarda auditoria (reporte pendente congela o relógio, §5.2).
        aid2 = self.ts.assign(1, 10, "T4_WOOD", 100, created_by=1, now=T0)["id"]
        r1 = self.ts.report(1, 10, "T4_ORE", 100,
                            assignment_id=self.aid, now=T0 + 13 * DAY)["id"]
        r2 = self.ts.report(1, 10, "T4_WOOD", 100,
                            assignment_id=aid2, now=T0 + 13 * DAY)["id"]
        res = self.ts.reject(1, r1, 1, now=T0 + 14.2 * DAY)
        self.assertEqual(res["state"], STATE_PENDING)   # segue congelado
        st = self._status(T0 + 14.2 * DAY)
        self.assertEqual(st["state"], STATE_PENDING)
        self.assertTrue(st["paused"])
        self.assertEqual(st["clock_days"], 13)          # pausa original vale
        # tick NÃO desliga: r2 ainda venceu o relógio
        tick = self.ts.clock_tick(now=T0 + 14.5 * DAY)
        self.assertEqual(tick["desligados"], 0)
        self.assertFalse(self._status(T0 + 14.5 * DAY)["tools_revoked"])
        # rejeitado o ÚLTIMO pendente, aí sim retoma da âncora antiga
        res = self.ts.reject(1, r2, 1, now=T0 + 14.6 * DAY)
        self.assertEqual(res["state"], STATE_LATE)
        st = self._status(T0 + 14.6 * DAY)
        self.assertFalse(st["paused"])
        self.assertEqual(st["clock_days"], 14)

    def test_t5_atrasado_reporta_vira_pendente_e_pausa(self):
        self.ts.clock_tick(now=T0 + 7 * DAY)              # em_dia -> atrasado
        r = self.ts.report(1, 10, "T4_ORE", 50, now=T0 + 8 * DAY)
        self.assertEqual(r["state"], STATE_PENDING)
        # congelado aos 8 dias, mesmo muito depois
        st = self._status(T0 + 20 * DAY)
        self.assertEqual(st["clock_days"], 8)
        # tick posterior PULA quem está pendente (reporte venceu o relógio)
        res = self.ts.clock_tick(now=T0 + 20 * DAY)
        self.assertEqual(res["desligados"], 0)
        self.assertEqual(self._status(T0 + 20 * DAY)["state"], STATE_PENDING)

    def test_t6_atrasado_tick_14_dias_desliga(self):
        self.ts.clock_tick(now=T0 + 7 * DAY)              # em_dia -> atrasado
        res = self.ts.clock_tick(now=T0 + DISMISS_AFTER_DAYS * DAY)
        self.assertEqual(res["desligados"], 1)
        st = self._status(T0 + DISMISS_AFTER_DAYS * DAY)
        self.assertEqual(st["state"], STATE_OFF)
        self.assertTrue(st["tools_revoked"])
        self.assertIsNotNone(st["dismissed_at"])

    def test_t7_desligado_aprovado_reativa(self):
        self.ts.clock_tick(now=T0 + 7 * DAY)
        self.ts.clock_tick(now=T0 + 14 * DAY)             # -> desligado
        # reporte de quem está desligado NÃO mexe no relógio (só a aprovação)
        rid = self.ts.report(1, 10, "T4_ORE", 100, now=T0 + 15 * DAY)["id"]
        self.assertEqual(self._status(T0 + 15 * DAY)["state"], STATE_OFF)
        res = self.ts.approve(1, rid, 1, now=T0 + 16 * DAY)
        self.assertEqual(res["state"], STATE_OK)
        self.assertTrue(res["reactivated"])               # devolver o cargo
        st = self._status(T0 + 16 * DAY)
        self.assertEqual(st["state"], STATE_OK)
        self.assertFalse(st["tools_revoked"])
        self.assertEqual(st["clock_days"], 0)
        self.assertIsNotNone(st["reactivated_at"])
        # auditoria registrou o restore além do approve
        actions = [e["action"] for e in self.ts.audit_log(1)]
        self.assertIn("restore", actions)
        self.assertIn("approve", actions)

    def test_tick_idempotente_mesmo_now(self):
        now = T0 + 7 * DAY
        first = self.ts.clock_tick(now=now)
        self.assertEqual(first["para_atrasado"], 1)
        again = self.ts.clock_tick(now=now)               # reprocessa: nada muda
        self.assertEqual(again["para_atrasado"], 0)
        self.assertEqual(again["desligados"], 0)
        self.assertEqual(self._status(now)["state"], STATE_LATE)

    def test_tick_atrasadissimo_desliga_num_passe_so(self):
        # cron pulou dias: em_dia com 15 dias desliga DIRETO no 1º tick,
        # e o 2º tick com o MESMO now não muda mais nada (idempotência real)
        now = T0 + 15 * DAY
        first = self.ts.clock_tick(now=now)
        self.assertEqual(first["desligados"], 1)
        self.assertEqual(first["para_atrasado"], 0)
        again = self.ts.clock_tick(now=now)
        self.assertEqual(again["desligados"], 0)
        self.assertEqual(self._status(now)["state"], STATE_OFF)

    def test_relogio_aceita_iso_alem_de_unix(self):
        iso = datetime.fromtimestamp(T0 + 7 * DAY, tz=timezone.utc).isoformat()
        res = self.ts.clock_tick(now=iso)
        self.assertEqual(res["para_atrasado"], 1)
        self.assertEqual(self._status(iso)["clock_days"], 7)

    def test_assign_upsert_atualiza_sem_duplicar(self):
        again = self.ts.assign(1, 10, "T4_ORE", 250, created_by=1,
                               now=T0 + DAY,
                               week_start=tribute.week_start_iso(T0))
        # mesma (org, membro, semana, item): id estável, qtd atualizada
        rows = self.ts.assignments(1, account_id=10)
        same_week = [a for a in rows if a["item_id"] == "T4_ORE"]
        # T0 e T0+DAY podem cair em semanas ISO diferentes; força a mesma
        self.assertEqual(again["id"], self.aid)
        self.assertFalse(again["created"])
        self.assertEqual(len(same_week), 1)
        self.assertEqual(same_week[0]["qty_target"], 250)

    def test_report_contra_meta_de_outro_membro_falha(self):
        with self.assertRaises(KeyError):
            self.ts.report(1, 99, "T4_ORE", 100,
                           assignment_id=self.aid, now=T0 + DAY)

    def test_reporte_processado_nao_reprocessa(self):
        rid = self.ts.report(1, 10, "T4_ORE", 100, now=T0 + DAY)["id"]
        self.ts.approve(1, rid, 1, now=T0 + 2 * DAY)
        with self.assertRaises(ValueError):
            self.ts.approve(1, rid, 1, now=T0 + 3 * DAY)
        with self.assertRaises(ValueError):
            self.ts.reject(1, rid, 1, now=T0 + 3 * DAY)

    def test_report_em_nome_do_membro_audita_on_behalf(self):
        self.ts.report(1, 10, "T4_ORE", 100, actor_id=1, now=T0 + DAY)
        entry = next(e for e in self.ts.audit_log(1) if e["action"] == "report")
        self.assertTrue(entry["details"]["on_behalf"])
        self.assertEqual(entry["actor_account_id"], 1)
        self.assertEqual(entry["target_account_id"], 10)
        # auto-reporte NÃO leva a flag
        self.ts.report(1, 10, "T4_ORE", 10, actor_id=10, now=T0 + DAY)
        entry = next(e for e in self.ts.audit_log(1) if e["action"] == "report")
        self.assertFalse(entry["details"]["on_behalf"])


class TributeIsolationTests(unittest.TestCase):
    """Org 1 nunca enxerga metas/reportes/relógios da org 2 (multi-inquilino)."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.ts = TributeStore(self.con, threading.Lock())
        week = tribute.week_start_iso(T0)
        self.a1 = self.ts.assign(1, 10, "T4_ORE", 100, created_by=1,
                                 week_start=week, now=T0)["id"]
        self.a2 = self.ts.assign(2, 20, "T5_HIDE", 50, created_by=2,
                                 week_start=week, now=T0)["id"]
        self.ts.assign(2, 21, "T4_WOOD", 30, created_by=2,
                       week_start=week, now=T0)

    def tearDown(self):
        self.con.close()

    def test_assignments_por_org(self):
        contas1 = {a["account_id"] for a in self.ts.assignments(1)}
        contas2 = {a["account_id"] for a in self.ts.assignments(2)}
        self.assertEqual(contas1, {10})
        self.assertEqual(contas2, {20, 21})

    def test_pending_e_member_status_por_org(self):
        rid = self.ts.report(2, 20, "T5_HIDE", 50,
                             assignment_id=self.a2, now=T0 + DAY)["id"]
        self.assertEqual(self.ts.pending(1), [])
        self.assertEqual([p["id"] for p in self.ts.pending(2)], [rid])
        s1 = {m["account_id"] for m in self.ts.member_status(1, now=T0 + DAY)}
        s2 = {m["account_id"] for m in self.ts.member_status(2, now=T0 + DAY)}
        self.assertEqual(s1, {10})
        self.assertEqual(s2, {20, 21})

    def test_org_errada_nao_aprova_nem_rejeita(self):
        rid = self.ts.report(2, 20, "T5_HIDE", 50, now=T0 + DAY)["id"]
        with self.assertRaises(KeyError):
            self.ts.approve(1, rid, 1, now=T0 + 2 * DAY)
        with self.assertRaises(KeyError):
            self.ts.reject(1, rid, 1, now=T0 + 2 * DAY)
        # segue pendente e pausado na org 2
        self.assertEqual([p["id"] for p in self.ts.pending(2)], [rid])
        st = self.ts.member_status(2, account_id=20, now=T0 + 2 * DAY)[0]
        self.assertEqual(st["state"], STATE_PENDING)

    def test_meta_de_outra_org_nao_recebe_reporte(self):
        with self.assertRaises(KeyError):
            self.ts.report(1, 10, "T5_HIDE", 50,
                           assignment_id=self.a2, now=T0 + DAY)

    def test_tick_escopado_por_org_nao_vaza(self):
        res = self.ts.clock_tick(1, now=T0 + 7 * DAY)
        self.assertEqual(res["para_atrasado"], 1)
        # org 2 intocada pelo tick escopado
        for m in self.ts.member_status(2, now=T0 + 7 * DAY):
            self.assertEqual(m["state"], STATE_OK)
        # tick global (cron) pega o resto
        res = self.ts.clock_tick(now=T0 + 7 * DAY)
        self.assertEqual(res["para_atrasado"], 2)


if __name__ == "__main__":
    unittest.main()

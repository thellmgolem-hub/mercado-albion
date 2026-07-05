# -*- coding: utf-8 -*-
"""Tributo da guild: metas semanais, reportes de entrega e relógio de 14 dias.

Núcleo web-first do plano Discord (docs/PLANO_DISCORD_GUILD.md §3/§5),
adaptado ao multi-inquilino da Fase 1: `org_id INTEGER` referencia `orgs.id`
(o snowflake do Discord mora em orgs.discord_guild_id p/ o futuro). O DDL dual
vive em store._TRIBUTE_SQLITE/_TRIBUTE_PG (init_tribute_schema).

Máquina de estados (§5.2) — decisões fechadas pelo dono:
- reporte PAUSA o relógio (paused_ts = now);
- aprovação ZERA o relógio (anchor_ts = now, paused_ts = NULL) e, se o membro
  estava desligado, REATIVA (tools_revoked=0);
- rejeição RETOMA a contagem (paused_ts = NULL, do MESMO anchor_ts antigo);
- 7 dias sem aprovação: em_dia -> atrasado (a janela semanal fechou);
- 14 dias sem entrega aprovada DESLIGA as ferramentas (tools_revoked=1).

A fonte da verdade do tempo são anchor_ts/paused_ts (REAL unix, aritmética em
Python — nunca no SQL); clock_days é só espelho barato p/ leitura,
materializado pelo tick. TODO método que decide por tempo recebe `now`
INJETÁVEL (unix float OU texto ISO) — jamais lê time.time() dentro da decisão,
para os testes controlarem o relógio.
"""
import json
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import store

# Estados do relógio (member_clock.state)
STATE_OK = "em_dia"        # cumpriu a última meta; relógio conta 0-13 dias
STATE_PENDING = "pendente"  # reportou; relógio CONGELADO à espera do auditor
STATE_LATE = "atrasado"    # passou a semana sem aprovação; relógio conta
STATE_OFF = "desligado"    # estourou 14 dias; ferramentas removidas

LATE_AFTER_DAYS = 7        # em_dia -> atrasado (fechou a janela semanal)
DISMISS_AFTER_DAYS = 14    # atrasado -> desligado (remove ferramentas)
DAY_S = 86400.0

# Teto de quantidade (meta/reporte): qty_target/qty_reported são INTEGER
# (int4) no Postgres — sem teto, um int gigante estoura no driver (psycopg
# NumericValueOutOfRange) e vira 500; no SQLite, > 2^63-1 dá OverflowError.
# 1e9 cabe folgado no int4 e é absurdo o bastante p/ qualquer meta real.
QTY_MAX = 1_000_000_000

_WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ts(now=None) -> float:
    """Coage o relógio injetado: None -> agora; float/int -> unix; str -> ISO."""
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    s = str(now).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def clock_days(now: float, anchor_ts: float, paused_ts=None) -> int:
    """Dias corridos do relógio. paused_ts preenchido => congelado na pausa."""
    ref = paused_ts if paused_ts is not None else now
    return max(0, int((ref - anchor_ts) // DAY_S))


def week_start_iso(now=None) -> str:
    """ISO (YYYY-MM-DD) da SEGUNDA-FEIRA da semana de `now` (semana ISO, UTC)."""
    d = datetime.fromtimestamp(_ts(now), tz=timezone.utc).date()
    return (d - timedelta(days=d.weekday())).isoformat()


class TributeStore:
    """Metas semanais + reportes + relógio de 14 dias, POR ORG (dual).

    Espelha o padrão do ChainStore: cria o próprio schema (idempotente), _tx
    para escrita (commit/rollback) e _read que encerra a transação no Postgres
    (evita 'idle in transaction' no pooler). `org` é o PRIMEIRO argumento
    posicional de todo método — uma guilda nunca enxerga metas, reportes ou
    relógios de outra. Toda escrita audita em guild_audit_log.
    """

    def __init__(self, con, lock):
        self.con = con
        self.lock = lock
        with self.lock:
            store.init_tribute_schema(self.con)

    @contextmanager
    def _tx(self):
        with self.lock:
            try:
                yield self.con
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise

    @contextmanager
    def _read(self):
        with self.lock:
            try:
                yield self.con
            finally:
                try:
                    self.con.rollback()
                except Exception:
                    pass

    @staticmethod
    def _insert_id(con, sql, params):
        if getattr(con, "backend", "sqlite") == "sqlite":
            return con.execute(sql, params).lastrowid
        return con.execute(sql + " RETURNING id", params).fetchone()[0]

    # ------------------------------------------------------------ internos
    def _audit(self, con, org, actor, action, target=None, details=None,
               ts=None):
        """Grava a pista de auditoria (actor None = máquina/cron)."""
        con.execute(
            "INSERT INTO guild_audit_log (org_id, actor_account_id, action, "
            "target_account_id, details_json, created_at) VALUES (?,?,?,?,?,?)",
            [org, actor, action, target,
             json.dumps(details, ensure_ascii=False) if details else None, ts])

    def _ensure_clock(self, con, org, account_id, ts):
        """Inicializa o relógio do membro (em_dia, âncora = agora) se faltar."""
        row = con.execute(
            "SELECT state FROM member_clock WHERE org_id=? AND account_id=?",
            [org, account_id]).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO member_clock (org_id, account_id, state, "
                "anchor_ts, paused_ts, clock_days, tools_revoked, updated_at) "
                "VALUES (?,?,?,?,NULL,0,0,?)",
                [org, account_id, STATE_OK, ts, ts])

    # -------------------------------------------------------------- metas
    def assign(self, org, account_id, item_id, qty_target, *, week_start=None,
               sector=None, from_chain_id=None, note=None, created_by=0,
               now=None):
        """Cria/atualiza a meta semanal (org, membro, semana, item) -> qtd.

        Upsert manual por SELECT+UPDATE para o id da meta ser ESTÁVEL (reportes
        podem referenciá-lo; INSERT OR REPLACE trocaria o id no SQLite).
        """
        ts = _ts(now)
        item_id = (item_id or "").strip()
        if not item_id:
            raise ValueError("item da meta vazio")
        qty = int(qty_target)
        if qty <= 0:
            raise ValueError("quantidade da meta deve ser positiva")
        if qty > QTY_MAX:
            raise ValueError("quantidade da meta acima do limite (1 bilhão)")
        week = (week_start or "").strip()
        if week:
            # normaliza p/ a SEGUNDA-FEIRA: a chave de upsert (org, membro,
            # semana, item) fragmentaria se cada data da mesma semana valesse,
            # e a meta sumiria do filtro week_now (que usa week_start_iso).
            if not _WEEK_RE.match(week):
                raise ValueError("week_start deve ser ISO (YYYY-MM-DD)")
            try:
                d = date.fromisoformat(week)
            except ValueError:
                raise ValueError("week_start inválido (data inexistente)")
            week = (d - timedelta(days=d.weekday())).isoformat()
        else:
            week = week_start_iso(ts)
        with self._tx() as con:
            row = con.execute(
                "SELECT id FROM weekly_assignments WHERE org_id=? AND "
                "account_id=? AND week_start=? AND item_id=?",
                [org, account_id, week, item_id]).fetchone()
            if row:
                aid = row[0]
                con.execute(
                    "UPDATE weekly_assignments SET qty_target=?, sector=?, "
                    "from_chain_id=?, note=?, created_by=? WHERE id=?",
                    [qty, sector, from_chain_id, note, created_by, aid])
                created = False
            else:
                aid = self._insert_id(con,
                    "INSERT INTO weekly_assignments (org_id, account_id, "
                    "week_start, item_id, qty_target, sector, from_chain_id, "
                    "note, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [org, account_id, week, item_id, qty, sector,
                     from_chain_id, note, ts, created_by])
                created = True
            # membro que ganha meta entra na máquina (âncora = agora)
            self._ensure_clock(con, org, account_id, ts)
            self._audit(con, org, created_by, "assign", account_id,
                        {"assignment_id": aid, "item_id": item_id, "qty": qty,
                         "week_start": week}, ts)
        return {"id": aid, "week_start": week, "created": created}

    def assignments(self, org, week_start=None, account_id=None):
        """Metas da org, opcionalmente filtradas por semana e/ou membro."""
        sql = ("SELECT id, account_id, week_start, item_id, qty_target, "
               "sector, from_chain_id, note, created_at, created_by "
               "FROM weekly_assignments WHERE org_id=?")
        params = [org]
        if week_start:
            sql += " AND week_start=?"
            params.append(week_start)
        if account_id is not None:
            sql += " AND account_id=?"
            params.append(account_id)
        sql += " ORDER BY week_start DESC, account_id, item_id"
        with self._read() as con:
            rows = con.execute(sql, params).fetchall()
        return [{"id": r[0], "account_id": r[1], "week_start": r[2],
                 "item_id": r[3], "qty_target": r[4], "sector": r[5],
                 "from_chain_id": r[6], "note": r[7], "created_at": r[8],
                 "created_by": r[9]} for r in rows]

    # ------------------------------------------------------------ reportes
    def report(self, org, account_id, item_id, qty, *, assignment_id=None,
               note=None, actor_id=None, now=None):
        """Membro alega entrega -> reporte 'pending' e o relógio PAUSA.

        `actor_id` != account_id = operador registrando em nome do membro
        (flag on_behalf no audit log). assignment_id opcional (NULL = bônus);
        se vier, precisa ser meta do MESMO membro na MESMA org (KeyError).
        Transições: em_dia|atrasado -> pendente; pendente fica (pausa original
        vale); desligado fica (só a aprovação reativa).
        """
        ts = _ts(now)
        item_id = (item_id or "").strip()
        if not item_id:
            raise ValueError("item do reporte vazio")
        qty = int(qty)
        if qty <= 0:
            raise ValueError("quantidade reportada deve ser positiva")
        if qty > QTY_MAX:
            raise ValueError("quantidade reportada acima do limite (1 bilhão)")
        with self._tx() as con:
            if assignment_id is not None:
                row = con.execute(
                    "SELECT id FROM weekly_assignments WHERE id=? AND "
                    "org_id=? AND account_id=?",
                    [assignment_id, org, account_id]).fetchone()
                if not row:
                    raise KeyError("meta não encontrada")
            rid = self._insert_id(con,
                "INSERT INTO member_reports (org_id, account_id, "
                "assignment_id, item_id, qty_reported, status, reported_at, "
                "created_at) VALUES (?,?,?,?,?,'pending',?,?)",
                [org, account_id, assignment_id, item_id, qty, ts, ts])
            self._ensure_clock(con, org, account_id, ts)
            crow = con.execute(
                "SELECT state, anchor_ts FROM member_clock "
                "WHERE org_id=? AND account_id=?", [org, account_id]).fetchone()
            state = crow[0]
            if state in (STATE_OK, STATE_LATE):
                con.execute(
                    "UPDATE member_clock SET state=?, paused_ts=?, "
                    "clock_days=?, updated_at=? WHERE org_id=? AND account_id=?",
                    [STATE_PENDING, ts, clock_days(ts, crow[1], ts), ts,
                     org, account_id])
                state = STATE_PENDING
            on_behalf = actor_id is not None and int(actor_id) != int(account_id)
            self._audit(con, org,
                        actor_id if actor_id is not None else account_id,
                        "report", account_id,
                        {"report_id": rid, "item_id": item_id, "qty": qty,
                         "on_behalf": on_behalf, "note": note}, ts)
        return {"id": rid, "state": state}

    def pending(self, org, limit=200):
        """Fila de auditoria: reportes 'pending' da org, mais antigo primeiro."""
        with self._read() as con:
            rows = con.execute(
                "SELECT id, account_id, assignment_id, item_id, qty_reported, "
                "reported_at FROM member_reports WHERE org_id=? AND "
                "status='pending' ORDER BY reported_at LIMIT ?",
                [org, int(limit)]).fetchall()
        return [{"id": r[0], "account_id": r[1], "assignment_id": r[2],
                 "item_id": r[3], "qty_reported": r[4], "reported_at": r[5]}
                for r in rows]

    def _take_report(self, con, org, report_id):
        """Carrega um reporte 'pending' da org (KeyError/ValueError se não dá)."""
        row = con.execute(
            "SELECT id, account_id, status FROM member_reports "
            "WHERE id=? AND org_id=?", [report_id, org]).fetchone()
        if not row:
            raise KeyError("reporte não encontrado")
        if row[2] != "pending":
            raise ValueError("reporte já processado")
        return row[1]

    def approve(self, org, report_id, auditor_id, *, note=None, now=None):
        """Auditor aprova: relógio ZERA (anchor=now) e volta a em_dia.

        Se o membro estava desligado, REATIVA (tools_revoked=0, reactivated_at)
        e o chamador recebe reactivated=True (p/ devolver o cargo no Discord,
        quando existir). O retorno leva também item_id/qty_reported/
        from_chain_id (este da META, se o reporte referencia uma) — insumo da
        ponte tributo→quadro: a Linha de Produção soma a entrega aprovada ao
        estoque do nó da cadeia de origem.
        """
        ts = _ts(now)
        with self._tx() as con:
            member = self._take_report(con, org, report_id)
            # dados da ponte (LEFT JOIN: reporte-bônus não tem meta/cadeia)
            brow = con.execute(
                "SELECT r.item_id, r.qty_reported, a.from_chain_id "
                "FROM member_reports r LEFT JOIN weekly_assignments a "
                "ON a.id = r.assignment_id AND a.org_id = r.org_id "
                "WHERE r.id=?", [report_id]).fetchone()
            con.execute(
                "UPDATE member_reports SET status='approved', "
                "auditor_account_id=?, audit_note=?, processed_at=? WHERE id=?",
                [auditor_id, note, ts, report_id])
            self._ensure_clock(con, org, member, ts)
            crow = con.execute(
                "SELECT state, tools_revoked FROM member_clock "
                "WHERE org_id=? AND account_id=?", [org, member]).fetchone()
            reactivated = (crow[0] == STATE_OFF) or bool(crow[1])
            if reactivated:
                con.execute(
                    "UPDATE member_clock SET state=?, anchor_ts=?, "
                    "paused_ts=NULL, clock_days=0, tools_revoked=0, "
                    "reactivated_at=?, updated_at=? "
                    "WHERE org_id=? AND account_id=?",
                    [STATE_OK, ts, ts, ts, org, member])
            else:
                con.execute(
                    "UPDATE member_clock SET state=?, anchor_ts=?, "
                    "paused_ts=NULL, clock_days=0, updated_at=? "
                    "WHERE org_id=? AND account_id=?",
                    [STATE_OK, ts, ts, org, member])
            self._audit(con, org, auditor_id, "approve", member,
                        {"report_id": report_id, "note": note}, ts)
            if reactivated:
                self._audit(con, org, auditor_id, "restore", member,
                            {"report_id": report_id}, ts)
        return {"id": report_id, "account_id": member, "state": STATE_OK,
                "reactivated": reactivated,
                "item_id": brow[0] if brow else None,
                "qty_reported": brow[1] if brow else 0,
                "from_chain_id": brow[2] if brow else None}

    def audit_event(self, org, actor_id, action, *, target=None, details=None,
                    now=None):
        """Registro avulso na pista de auditoria (ex.: ponte tributo→quadro
        que falhou fora da transação do approve — nunca quebra o chamador)."""
        with self._tx() as con:
            self._audit(con, org, actor_id, action, target, details, _ts(now))

    def reject(self, org, report_id, auditor_id, *, note=None, now=None):
        """Auditor rejeita: o relógio RETOMA do MESMO anchor_ts (-> atrasado).

        Só retoma se NÃO restar outro reporte 'pending' do membro — metas são
        por (membro, semana, ITEM), então múltiplos reportes pendentes são o
        caso normal, e reporte pendente congela o relógio (§5.2). Enquanto
        houver fila, o estado segue 'pendente' com a pausa original.
        """
        ts = _ts(now)
        with self._tx() as con:
            member = self._take_report(con, org, report_id)
            con.execute(
                "UPDATE member_reports SET status='rejected', "
                "auditor_account_id=?, audit_note=?, processed_at=? WHERE id=?",
                [auditor_id, note, ts, report_id])
            crow = con.execute(
                "SELECT state, anchor_ts FROM member_clock "
                "WHERE org_id=? AND account_id=?", [org, member]).fetchone()
            state = crow[0] if crow else None
            if crow and crow[0] == STATE_PENDING:
                left = con.execute(
                    "SELECT COUNT(*) FROM member_reports WHERE org_id=? AND "
                    "account_id=? AND status='pending'",
                    [org, member]).fetchone()[0]
                if not left:
                    con.execute(
                        "UPDATE member_clock SET state=?, paused_ts=NULL, "
                        "clock_days=?, updated_at=? "
                        "WHERE org_id=? AND account_id=?",
                        [STATE_LATE, clock_days(ts, crow[1], None), ts,
                         org, member])
                    state = STATE_LATE
            self._audit(con, org, auditor_id, "reject", member,
                        {"report_id": report_id, "note": note}, ts)
        return {"id": report_id, "account_id": member, "state": state}

    # ------------------------------------------------------------- relógio
    def member_status(self, org, account_id=None, now=None):
        """Estado do relógio por membro (dias calculados AO VIVO de anchor/paused)."""
        ts = _ts(now)
        sql = ("SELECT account_id, state, anchor_ts, paused_ts, tools_revoked, "
               "dismissed_at, reactivated_at, updated_at FROM member_clock "
               "WHERE org_id=?")
        params = [org]
        if account_id is not None:
            sql += " AND account_id=?"
            params.append(account_id)
        with self._read() as con:
            rows = con.execute(sql + " ORDER BY account_id", params).fetchall()
        out = []
        for r in rows:
            days = clock_days(ts, r[2], r[3])
            out.append({
                "account_id": r[0], "state": r[1], "clock_days": days,
                "days_left": max(0, DISMISS_AFTER_DAYS - days),
                "paused": r[3] is not None, "tools_revoked": bool(r[4]),
                "anchor_ts": r[2], "paused_ts": r[3], "dismissed_at": r[5],
                "reactivated_at": r[6], "updated_at": r[7],
            })
        return out

    def clock_tick(self, org=None, *, now=None):
        """Um tick do cron (1x/dia): transições por TEMPO, idempotente.

        org=None varre TODAS as orgs (o cron é da plataforma). Quem está
        pendente (paused_ts) NUNCA avança — o reporte venceu o relógio (§5.2).
        Num único passe: >=14 dias desliga DIRETO (mesmo de em_dia, se o cron
        pulou dias), senão em_dia com >=7 vira atrasado. Reprocessar com o
        mesmo `now` não muda nada.
        """
        ts = _ts(now)
        res = {"vistos": 0, "para_atrasado": 0, "desligados": 0}
        with self._tx() as con:
            sql = ("SELECT org_id, account_id, state, anchor_ts, paused_ts, "
                   "clock_days FROM member_clock WHERE state IN (?,?)")
            params = [STATE_OK, STATE_LATE]
            if org is not None:
                sql += " AND org_id=?"
                params.append(org)
            rows = con.execute(sql, params).fetchall()
            for r in rows:
                res["vistos"] += 1
                if r[4] is not None:   # pendente disfarçado: nunca avança
                    continue
                days = clock_days(ts, r[3], None)
                if days >= DISMISS_AFTER_DAYS:
                    con.execute(
                        "UPDATE member_clock SET state=?, clock_days=?, "
                        "tools_revoked=1, dismissed_at=?, updated_at=? "
                        "WHERE org_id=? AND account_id=?",
                        [STATE_OFF, days, ts, ts, r[0], r[1]])
                    self._audit(con, r[0], None, "revoke", r[1],
                                {"clock_days": days}, ts)
                    res["desligados"] += 1
                elif r[2] == STATE_OK and days >= LATE_AFTER_DAYS:
                    con.execute(
                        "UPDATE member_clock SET state=?, clock_days=?, "
                        "updated_at=? WHERE org_id=? AND account_id=?",
                        [STATE_LATE, days, ts, r[0], r[1]])
                    res["para_atrasado"] += 1
                elif days != r[5]:     # só materializa o espelho de leitura
                    con.execute(
                        "UPDATE member_clock SET clock_days=?, updated_at=? "
                        "WHERE org_id=? AND account_id=?",
                        [days, ts, r[0], r[1]])
        return res

    # ------------------------------------------------------------ auditoria
    def audit_log(self, org, limit=100):
        """Últimas entradas da pista de auditoria da org (mais recente 1º)."""
        with self._read() as con:
            rows = con.execute(
                "SELECT id, actor_account_id, action, target_account_id, "
                "details_json, created_at FROM guild_audit_log WHERE org_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                [org, int(limit)]).fetchall()
        out = []
        for r in rows:
            try:
                details = json.loads(r[4]) if r[4] else None
            except (ValueError, TypeError):
                details = None
            out.append({"id": r[0], "actor_account_id": r[1], "action": r[2],
                        "target_account_id": r[3], "details": details,
                        "created_at": r[5]})
        return out

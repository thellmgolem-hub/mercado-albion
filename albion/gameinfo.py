# -*- coding: utf-8 -*-
"""Ingestão incremental da API pública de kills/batalhas (gameinfo).

Schema real validado em docs/SCHEMA_GAMEINFO.md. Princípios (ver
docs/PLANO_DADOS_PUBLICOS_ECONOMIA_AVANCADA.md): somente HTTP público,
coleta incremental idempotente, backoff em erro, registro auditável em
public_data_runs, e honestidade sobre vieses (killboard é amostra, Location
costuma vir nulo).
"""
import time

import httpx

from . import config

EQUIP_SLOTS = ("MainHand", "OffHand", "Head", "Armor", "Shoes", "Bag",
               "Cape", "Mount", "Potion", "Food")
PAGE_SIZE = 51
MAX_OFFSET = 1000


class GameinfoClient:
    def __init__(self, server: str = config.DEFAULT_SERVER):
        self.base = config.GAMEINFO_BASES[server]
        self.http = httpx.Client(timeout=30,
                                 headers={"User-Agent": config.USER_AGENT})

    def _get(self, path: str, params: dict):
        for attempt in range(4):
            try:
                r = self.http.get(self.base + path, params=params)
            except httpx.HTTPError:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 500 or r.status_code == 429:
                if attempt == 3:
                    r.raise_for_status()
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()

    def events_page(self, offset: int):
        return self._get("/events", {"limit": PAGE_SIZE, "offset": offset})

    def battles_page(self, offset: int):
        return self._get("/battles", {"limit": PAGE_SIZE, "offset": offset,
                                      "sort": "recent"})


def _actor_row(server, event_id, role, a):
    return (server, event_id, role, a.get("Id") or "?", a.get("Name"),
            a.get("GuildId"), a.get("GuildName"), a.get("AllianceId"),
            a.get("AllianceName"), a.get("AverageItemPower"),
            a.get("KillFame"), a.get("DeathFame"), a.get("DamageDone"),
            a.get("SupportHealingDone"))


def _equipment_rows(server, event_id, role, actor, with_inventory=False):
    rows = {}
    eq = actor.get("Equipment") or {}
    for slot in EQUIP_SLOTS:
        it = eq.get(slot)
        if it and it.get("Type"):
            key = (server, event_id, role, slot, it["Type"])
            rows[key] = (*key, it.get("Count") or 1, it.get("Quality") or 1)
    if with_inventory:
        for it in actor.get("Inventory") or []:
            if it and it.get("Type"):
                key = (server, event_id, role, "Inventory", it["Type"])
                if key in rows:  # mesmo item em múltiplos slots: soma
                    prev = rows[key]
                    rows[key] = (*key, prev[5] + (it.get("Count") or 1),
                                 prev[6])
                else:
                    rows[key] = (*key, it.get("Count") or 1,
                                 it.get("Quality") or 1)
    return list(rows.values())


def _checkpoint(aodp, source):
    with aodp.db_lock:
        row = aodp.db.execute(
            "SELECT cursor_value FROM public_ingest_checkpoints"
            " WHERE server=? AND source=?", [aodp.server, source]).fetchone()
    return row[0] if row else 0


def _save_checkpoint(aodp, source, cursor):
    with aodp.db_lock:
        aodp.db.execute(
            "INSERT OR REPLACE INTO public_ingest_checkpoints VALUES (?,?,?,?)",
            [aodp.server, source, cursor, time.time()])
        aodp.db.commit()


def _log_run(aodp, source, started, pages, seen, inserted, newest, ok, error):
    with aodp.db_lock:
        aodp.db.execute(
            "INSERT OR REPLACE INTO public_data_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            [aodp.server, source, started, time.time(), pages, seen,
             inserted, newest, ok, error])
        aodp.db.commit()


def ingest_events(aodp, client: GameinfoClient | None = None,
                  max_pages: int = config.GAMEINFO_EVENT_PAGES) -> dict:
    """Varre páginas recentes até alcançar o checkpoint (incremental)."""
    client = client or GameinfoClient(aodp.server)
    started = time.time()
    known_max = _checkpoint(aodp, "events")
    seen = inserted = pages = 0
    newest = known_max
    ok, error = 1, None
    try:
        for page in range(max_pages):
            offset = page * PAGE_SIZE
            if offset > MAX_OFFSET:
                break
            events = client.events_page(offset)
            if not events:
                break
            pages += 1
            seen += len(events)
            fetched = time.time()
            ev_rows, actor_rows, equip_rows = [], [], []
            page_min_id = None
            for e in events:
                eid = e.get("EventId")
                if not eid:
                    continue
                page_min_id = eid if page_min_id is None else min(page_min_id, eid)
                newest = max(newest, eid)
                if eid <= known_max:
                    continue
                killer, victim = e.get("Killer") or {}, e.get("Victim") or {}
                ev_rows.append((
                    aodp.server, eid, e.get("TimeStamp"), e.get("BattleId"),
                    e.get("Type"), e.get("KillArea"), e.get("Location"),
                    e.get("TotalVictimKillFame"),
                    e.get("numberOfParticipants"), e.get("groupMemberCount"),
                    killer.get("Id"), victim.get("Id"), fetched))
                actor_rows.append(_actor_row(aodp.server, eid, "killer", killer))
                actor_rows.append(_actor_row(aodp.server, eid, "victim", victim))
                for p in e.get("Participants") or []:
                    actor_rows.append(_actor_row(aodp.server, eid,
                                                 "participant", p))
                equip_rows += _equipment_rows(aodp.server, eid, "killer", killer)
                equip_rows += _equipment_rows(aodp.server, eid, "victim",
                                              victim, with_inventory=True)
            if ev_rows:
                with aodp.db_lock:
                    cur = aodp.db.executemany(
                        "INSERT OR IGNORE INTO kill_events VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?)", ev_rows)
                    inserted += cur.rowcount
                    aodp.db.executemany(
                        "INSERT OR IGNORE INTO kill_event_actors VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", actor_rows)
                    aodp.db.executemany(
                        "INSERT OR IGNORE INTO kill_event_equipment VALUES "
                        "(?,?,?,?,?,?,?)", equip_rows)
                    aodp.db.commit()
            # página inteira já conhecida -> alcançamos o checkpoint
            if page_min_id is not None and page_min_id <= known_max:
                break
            time.sleep(1)  # cortesia: ~1 req/s
    except Exception as e:
        ok, error = 0, repr(e)[:500]
    if newest > known_max:
        _save_checkpoint(aodp, "events", newest)
    _log_run(aodp, "events", started, pages, seen, inserted, newest, ok, error)
    return {"source": "events", "pages": pages, "seen": seen,
            "inserted": inserted, "ok": ok, "error": error}


def ingest_battles(aodp, client: GameinfoClient | None = None,
                   max_pages: int = config.GAMEINFO_BATTLE_PAGES) -> dict:
    client = client or GameinfoClient(aodp.server)
    started = time.time()
    known_max = _checkpoint(aodp, "battles")
    seen = inserted = pages = 0
    newest = known_max
    ok, error = 1, None
    try:
        for page in range(max_pages):
            battles = client.battles_page(page * PAGE_SIZE)
            if not battles:
                break
            pages += 1
            seen += len(battles)
            fetched = time.time()
            b_rows, e_rows = [], []
            page_min = None
            for b in battles:
                bid = b.get("id")
                if not bid:
                    continue
                page_min = bid if page_min is None else min(page_min, bid)
                newest = max(newest, bid)
                if bid <= known_max:
                    continue
                b_rows.append((
                    aodp.server, bid, b.get("startTime"), b.get("endTime"),
                    b.get("totalFame"), b.get("totalKills"),
                    len(b.get("players") or {}), b.get("clusterName"),
                    fetched))
                for etype in ("guilds", "alliances"):
                    for ent_id, ent in (b.get(etype) or {}).items():
                        e_rows.append((
                            aodp.server, bid, etype[:-1], ent_id,
                            ent.get("name"), ent.get("alliance"),
                            ent.get("kills"), ent.get("deaths"),
                            ent.get("killFame")))
            if b_rows:
                with aodp.db_lock:
                    cur = aodp.db.executemany(
                        "INSERT OR IGNORE INTO battle_summaries VALUES "
                        "(?,?,?,?,?,?,?,?,?)", b_rows)
                    inserted += cur.rowcount
                    aodp.db.executemany(
                        "INSERT OR IGNORE INTO battle_entities VALUES "
                        "(?,?,?,?,?,?,?,?,?)", e_rows)
                    aodp.db.commit()
            if page_min is not None and page_min <= known_max:
                break
            time.sleep(1)
    except Exception as e:
        ok, error = 0, repr(e)[:500]
    if newest > known_max:
        _save_checkpoint(aodp, "battles", newest)
    _log_run(aodp, "battles", started, pages, seen, inserted, newest, ok, error)
    return {"source": "battles", "pages": pages, "seen": seen,
            "inserted": inserted, "ok": ok, "error": error}


def destruction_top(con, server, days: float = 1, role: str = "victim",
                    include_inventory: bool = False, limit: int = 30):
    """Itens mais perdidos/usados em kills na janela, com valor de mercado.

    Valor = aparições × menor preço de venda q1 nas cidades reais (referência
    honesta, SEM ajuste de trash rate — é o valor de mercado em jogo, não a
    destruição líquida).
    """
    slot_filter = "" if include_inventory else " AND eq.slot != 'Inventory'"
    rows = con.execute(f"""
        WITH janela AS (
          -- ts da API usa 'T' como separador; strftime gera no mesmo formato
          SELECT event_id FROM kill_events
          WHERE server=:srv
            AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
        ),
        usados AS (
          SELECT eq.item_id,
                 SUM(eq.count) AS unidades,
                 COUNT(DISTINCT eq.event_id) AS eventos
          FROM kill_event_equipment eq
          JOIN janela j ON j.event_id = eq.event_id
          WHERE eq.server=:srv AND eq.role=:role {slot_filter}
          GROUP BY eq.item_id
        ),
        ref AS (
          SELECT item_id, MIN(sell_price_min) AS preco
          FROM prices
          WHERE server=:srv AND quality=1 AND sell_price_min > 0
            AND city IN ('Bridgewatch','Caerleon','Fort Sterling',
                         'Lymhurst','Martlock','Thetford')
          GROUP BY item_id
        )
        SELECT u.item_id, u.unidades, u.eventos, ref.preco,
               u.unidades * ref.preco AS valor_estimado
        FROM usados u LEFT JOIN ref ON ref.item_id = u.item_id
        ORDER BY u.unidades DESC
        LIMIT :lim
    """, {"srv": server, "delta": f"-{days} days", "role": role,
          "lim": limit}).fetchall()
    return [{"item_id": r[0], "unidades": r[1], "eventos": r[2],
             "preco_ref": r[3], "valor_estimado": r[4]} for r in rows]


def intel_status(con, server):
    out = {}
    for table in ("kill_events", "kill_event_actors", "kill_event_equipment",
                  "battle_summaries", "battle_entities"):
        out[table] = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE server=?",
            [server]).fetchone()[0]
    out["runs"] = con.execute(
        "SELECT COUNT(*) FROM public_data_runs WHERE server=?",
        [server]).fetchone()[0]
    row = con.execute(
        "SELECT MIN(ts), MAX(ts) FROM kill_events WHERE server=?",
        [server]).fetchone()
    out["janela_eventos"] = {"de": row[0], "ate": row[1]}
    return out

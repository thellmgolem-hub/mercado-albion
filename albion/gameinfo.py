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
    # leitura pura: _read encerra a transação (Postgres não fica idle)
    with aodp._read():
        row = aodp.db.execute(
            "SELECT cursor_value FROM public_ingest_checkpoints"
            " WHERE server=? AND source=?", [aodp.server, source]).fetchone()
    return row[0] if row else 0


def _save_checkpoint(aodp, source, cursor):
    from . import store
    with aodp.db_lock:
        store.upsert(
            aodp.db, "public_ingest_checkpoints",
            ["server", "source", "cursor_value", "last_success_at"],
            [(aodp.server, source, cursor, time.time())], ["server", "source"])
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
    caught_up = (known_max == 0)   # 1ª carga não tem gap por definição
    try:
        for page in range(max_pages):
            offset = page * PAGE_SIZE
            if offset > MAX_OFFSET:
                break
            events = client.events_page(offset)
            if not events:
                caught_up = True   # API esgotou os eventos -> não há gap
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
                caught_up = True
                break
            time.sleep(1)  # cortesia: ~1 req/s
    except Exception as e:
        ok, error = 0, repr(e)[:500]
    # B5: se a varredura terminou sem alcançar o checkpoint e ainda havia
    # eventos novos, houve burst maior que a janela -> gap (eventos perdidos).
    # Registra para sabermos quando o killboard ficou subamostrado.
    saturated = ok and not caught_up and newest > known_max
    if saturated and error is None:
        error = f"SATURADO: burst > {max_pages} pgs; gap possível (eventos perdidos)"
    if newest > known_max:
        _save_checkpoint(aodp, "events", newest)
    _log_run(aodp, "events", started, pages, seen, inserted, newest, ok, error)
    return {"source": "events", "pages": pages, "seen": seen,
            "inserted": inserted, "ok": ok, "error": error,
            "saturated": bool(saturated)}


def ingest_demand_lean(aodp, client: GameinfoClient | None = None,
                       max_pages: int = config.GAMEINFO_EVENT_PAGES) -> dict:
    """Ingestão MAGRA do killboard (piloto na nuvem): agrega o equipamento das
    VÍTIMAS em kill_demand_daily (server, day, item_id, slot, quality ->
    victim_units, victim_events), SEM guardar eventos crus.

    Cabe no Postgres gratuito porque guarda só o agregado diário (~milhares de
    linhas/dia), não o firehose. Idempotente via checkpoint 'demand_lean': só
    conta eventos com EventId > checkpoint, então cada evento entra uma vez.
    Funciona em SQLite e Postgres (store.upsert_add soma no conflito).
    """
    from . import store
    # No SQLite local com eventos crus presentes, kill_demand_daily é
    # reconstruído por materialize_kill_demand_daily (REPLACE) via `intel
    # collect` — somar (upsert_add) por cima inflaria a contagem. Aborta com
    # no-op; o caminho magro (SUM) é exclusivo do Postgres (sem tabelas cruas).
    if getattr(aodp.db, "backend", "sqlite") == "sqlite":
        try:
            has_raw = aodp.db.execute(
                "SELECT 1 FROM kill_event_equipment LIMIT 1").fetchone()
        except Exception:
            has_raw = None
        if has_raw:
            return {"source": "demand_lean", "ok": 1, "pages": 0, "seen": 0,
                    "keys": 0, "skipped": "sqlite com eventos crus "
                    "(use `intel collect` p/ materializar)"}
    client = client or GameinfoClient(aodp.server)
    started = time.time()
    known_max = _checkpoint(aodp, "demand_lean")
    seen = pages = 0
    newest = known_max
    caught_up = (known_max == 0)
    agg = {}            # (day, item_id, slot, quality) -> [units, events]
    ok, error = 1, None
    try:
        for page in range(max_pages):
            offset = page * PAGE_SIZE
            if offset > MAX_OFFSET:
                break
            events = client.events_page(offset)
            if not events:
                caught_up = True
                break
            pages += 1
            seen += len(events)
            page_min_id = None
            for e in events:
                eid = e.get("EventId")
                if not eid:
                    continue
                page_min_id = eid if page_min_id is None else min(page_min_id, eid)
                newest = max(newest, eid)
                if eid <= known_max:
                    continue
                day = (e.get("TimeStamp") or "")[:10]
                if not day:
                    continue
                victim = e.get("Victim") or {}
                eq_rows = _equipment_rows(aodp.server, eid, "victim", victim,
                                          with_inventory=True)
                counted = set()  # +1 victim_event por chave por evento
                for r in eq_rows:
                    slot, item_id, cnt, qual = r[3], r[4], r[5], r[6]
                    key = (day, item_id, slot, qual)
                    cell = agg.get(key)
                    if cell is None:
                        agg[key] = [cnt, 1]
                        counted.add(key)
                    else:
                        cell[0] += cnt
                        if key not in counted:
                            cell[1] += 1
                            counted.add(key)
            if page_min_id is not None and page_min_id <= known_max:
                caught_up = True
                break
            time.sleep(1)  # cortesia: ~1 req/s
    except Exception as e:
        ok, error = 0, repr(e)[:500]
    rows_out = [(aodp.server, day, item_id, slot, qual, u, ev)
                for (day, item_id, slot, qual), (u, ev) in agg.items()]
    # SÓ grava se a paginação completou (ok=1): em falha no meio, descarta o
    # parcial e NÃO avança o checkpoint — a próxima rodada reprocessa do mesmo
    # ponto, sem lacuna permanente e sem dupla contagem. Agregado + checkpoint
    # na MESMA transação (atômico contra queda entre os dois).
    if ok and (rows_out or newest > known_max):
        with aodp.db_lock:
            if rows_out:
                store.upsert_add(
                    aodp.db, "kill_demand_daily",
                    ["server", "day", "item_id", "slot", "quality",
                     "victim_units", "victim_events"],
                    rows_out,
                    ["server", "day", "item_id", "slot", "quality"],
                    ["victim_units", "victim_events"])
            if newest > known_max:
                store.upsert(
                    aodp.db, "public_ingest_checkpoints",
                    ["server", "source", "cursor_value", "last_success_at"],
                    [(aodp.server, "demand_lean", newest, time.time())],
                    ["server", "source"])
            aodp.db.commit()
    saturated = bool(ok and not caught_up and newest > known_max)
    return {"source": "demand_lean", "pages": pages, "seen": seen,
            "keys": len(agg), "newest": newest, "ok": ok, "error": error,
            "saturated": saturated}


def materialize_kill_demand_daily(aodp) -> int:
    """Reconstrói kill_demand_daily a partir de kill_event_equipment (CLI local).

    Rebuild COMPLETO e idempotente (REPLACE) sobre os eventos crus já ingeridos
    pela CLI (`intel collect`). No servidor magro/Postgres a mesma tabela é
    alimentada incrementalmente por ingest_demand_lean — assim Guild/Logística
    funcionam tanto no SQLite local quanto na nuvem. Só SQLite (usa as tabelas
    cruas, que não existem no piloto Postgres)."""
    if getattr(aodp.db, "backend", "sqlite") != "sqlite":
        return 0
    with aodp.db_lock:
        rows = aodp.db.execute(
            """SELECT e.server, substr(k.ts,1,10) AS day, e.item_id, e.slot,
                      e.quality, SUM(e.count) AS units,
                      COUNT(DISTINCT e.event_id) AS evs
               FROM kill_event_equipment e JOIN kill_events k
                 ON k.server=e.server AND k.event_id=e.event_id
               WHERE e.role='victim' AND e.server=?
               GROUP BY e.server, day, e.item_id, e.slot, e.quality""",
            [aodp.server]).fetchall()
        aodp.db.executemany(
            "INSERT OR REPLACE INTO kill_demand_daily VALUES (?,?,?,?,?,?,?)",
            [tuple(r) for r in rows])
        aodp.db.commit()
    return len(rows)


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
    trash = get_assumption(con, "trash_rate_base", 0.30)
    return [{"item_id": r[0], "unidades": r[1], "eventos": r[2],
             "preco_ref": r[3], "valor_estimado": r[4],
             "valor_trash_estimado": round(r[4] * trash) if r[4] else None,
             "trash_rate": trash} for r in rows]


DEFAULT_ASSUMPTIONS = [
    # (key, value, source, confidence, notes) — NUNCA tratar como verdade:
    # são parâmetros configuráveis, a calibrar por backtest (ver plano)
    ("trash_rate_base", 0.30, "regra comunitária não validada", "baixa",
     "fração de equipamento destruída na morte; o resto vira loot"),
    ("regear_capture_rate", 0.25, "estimativa inicial", "baixa",
     "fração da destruição que vira compra no mercado em até 24h"),
    ("refining_rrr", 36.7, "valor público de cidade com bônus, sem foco",
     "média", "retorno de recursos no refino NA cidade com bônus da família"),
    ("refining_rrr_base", 15.2, "valor público fora de bônus", "média",
     "retorno de recursos no refino fora da cidade com bônus"),
]


def ensure_assumptions(aodp):
    with aodp.db_lock:
        aodp.db.executemany(
            "INSERT OR IGNORE INTO economic_assumptions VALUES (?,?,?,?,?,?)",
            [(k, v, s, c, time.time(), n) for k, v, s, c, n
             in DEFAULT_ASSUMPTIONS])
        aodp.db.commit()


def get_assumption(con, key, default=None):
    row = con.execute("SELECT value FROM economic_assumptions WHERE key=?",
                      [key]).fetchone()
    return row[0] if row else default


def aggregate_demand_daily(aodp, days_back: int = 3) -> int:
    """Materializa item_demand_daily reagregando os últimos N dias.

    Idempotente: os dias recentes (janela ainda aberta) são recalculados a
    cada chamada; dias antigos ficam congelados.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days_back)).strftime("%Y-%m-%d")
    with aodp.db_lock:
        aodp.db.execute(
            "DELETE FROM item_demand_daily WHERE server=? AND day >= ?",
            [aodp.server, cutoff])
        cur = aodp.db.execute("""
            INSERT INTO item_demand_daily
            SELECT eq.server, substr(ke.ts, 1, 10) AS day, eq.item_id,
              SUM(CASE WHEN eq.role='victim' AND eq.slot!='Inventory'
                       THEN eq.count ELSE 0 END),
              COUNT(DISTINCT CASE WHEN eq.role='victim'
                                   AND eq.slot!='Inventory'
                                  THEN eq.event_id END),
              SUM(CASE WHEN eq.role='killer' THEN eq.count ELSE 0 END),
              SUM(CASE WHEN eq.role='victim' AND eq.slot='Inventory'
                       THEN eq.count ELSE 0 END)
            FROM kill_event_equipment eq
            JOIN kill_events ke
              ON ke.server = eq.server AND ke.event_id = eq.event_id
            WHERE eq.server=? AND substr(ke.ts, 1, 10) >= ?
            GROUP BY day, eq.item_id
        """, [aodp.server, cutoff])
        n = cur.rowcount
        aodp.db.commit()
    return n


def demand_price_divergence(con, server, recent_days: int = 2,
                            base_days: int = 5, min_units_day: float = 10,
                            demand_rise: float = 1.3,
                            price_lag: float = 1.05, limit: int = 20):
    """Sinal-chefe do plano: destruição subindo com preço ainda atrasado.

    demanda = média de unidades perdidas/dia (vítimas, sem inventário) nos
    últimos `recent_days` vs nos `base_days` anteriores; preço = VWAP diário
    q1 nas cidades reais (history), último dia vs média da base.
    """
    rows = con.execute(f"""
        WITH demanda AS (
          SELECT item_id,
                 CASE WHEN day >= date('now', :rec_delta)
                      THEN 'recente' ELSE 'base' END AS fase,
                 SUM(victim_units) * 1.0 / COUNT(DISTINCT day) AS unid_dia
          FROM item_demand_daily
          WHERE server = :srv AND day >= date('now', :janela_dias)
          GROUP BY item_id, fase
        ),
        precos AS (
          SELECT item_id, substr(ts, 1, 10) AS day,
                 SUM(item_count * avg_price) * 1.0 / SUM(item_count) AS vwap,
                 SUM(item_count) AS volume
          FROM history
          WHERE server = :srv AND time_scale = 24 AND quality = 1
            AND item_count > 0 AND avg_price > 0
            AND ts >= strftime('%Y-%m-%dT00:00:00', 'now', :janela)
            AND city IN ('Bridgewatch','Caerleon','Fort Sterling',
                         'Lymhurst','Martlock','Thetford')
          GROUP BY item_id, day
        ),
        preco_agg AS (
          SELECT item_id,
                 AVG(CASE WHEN day >= date('now', :rec_delta)
                          THEN vwap END) AS preco_recente,
                 AVG(CASE WHEN day < date('now', :rec_delta)
                          THEN vwap END) AS preco_base,
                 SUM(volume) * 1.0 / COUNT(DISTINCT day) AS volume_dia
          FROM precos GROUP BY item_id
        )
        SELECT r.item_id, r.unid_dia AS demanda_recente,
               COALESCE(b.unid_dia, 0) AS demanda_base,
               p.preco_recente, p.preco_base, p.volume_dia
        FROM demanda r
        LEFT JOIN demanda b ON b.item_id = r.item_id AND b.fase = 'base'
        JOIN preco_agg p ON p.item_id = r.item_id
        WHERE r.fase = 'recente' AND r.unid_dia >= :min_units
          AND p.preco_recente IS NOT NULL AND p.preco_base IS NOT NULL
    """, {"srv": server,
          "janela": f"-{recent_days + base_days} days",
          "janela_dias": f"-{recent_days + base_days} days",
          # -(recent_days-1): 'recente' = hoje + (recent_days-1) dias anteriores
          # = exatamente recent_days dias-calendário (SQL-3)
          "rec_delta": f"-{max(recent_days - 1, 0)} days",
          "min_units": min_units_day}).fetchall()
    out = []
    for item_id, dem_rec, dem_base, p_rec, p_base, vol in rows:
        dem_ratio = (dem_rec / dem_base) if dem_base > 0 else None
        price_ratio = p_rec / p_base if p_base else None
        # demanda nova (sem base) também é sinal, com ressalva
        rising = dem_ratio is None or dem_ratio >= demand_rise
        lagging = price_ratio is not None and price_ratio <= price_lag
        if rising and lagging:
            out.append({
                "item_id": item_id,
                "demanda_dia_recente": round(dem_rec, 1),
                "demanda_dia_base": round(dem_base, 1),
                "demanda_ratio": round(dem_ratio, 2) if dem_ratio else None,
                "preco_recente": round(p_rec, 1),
                "preco_base": round(p_base, 1),
                "preco_ratio": round(price_ratio, 3),
                "volume_dia": round(vol, 1),
                "nota": "demanda nova (sem base de comparação)"
                if dem_ratio is None else None,
            })
    out.sort(key=lambda o: -(o["demanda_ratio"] or 99))
    return out[:limit]


def log_signals(aodp, signals) -> int:
    """Persiste os sinais de divergência para backtest futuro (prometido
    vs realizado). Chamado pelo loop do servidor ~1x/hora."""
    now = time.time()
    with aodp.db_lock:
        aodp.db.executemany(
            "INSERT OR IGNORE INTO demand_signal_log VALUES (?,?,?,?,?,?,?,?)",
            [(aodp.server, now, s["item_id"], s["demanda_dia_recente"],
              s.get("demanda_ratio"), s["preco_recente"], s["preco_ratio"],
              s["volume_dia"]) for s in signals])
        aodp.db.commit()
    return len(signals)


def validate_signals(con, server, horizon_days: int = 1):
    """Backtest dos alertas de divergência: o preço subiu após o sinal?

    Compara o preço no momento do alerta com o VWAP diário `horizon_days`
    depois (history q1, cidades reais). Só avalia sinais antigos o
    suficiente para o horizonte já ter fechado.
    """
    rows = con.execute("""
        WITH sinais AS (
          SELECT generated_at, item_id, preco_recente, demanda_ratio,
                 date(generated_at, 'unixepoch', '+' || :h || ' days') AS alvo
          FROM demand_signal_log
          WHERE server = :srv
            AND generated_at < strftime('%s', 'now') - :h * 86400
        ),
        precos AS (
          SELECT item_id, substr(ts, 1, 10) AS day,
                 SUM(item_count * avg_price) * 1.0 / SUM(item_count) AS vwap
          FROM history
          WHERE server = :srv AND time_scale = 24 AND quality = 1
            AND item_count > 0 AND avg_price > 0
            AND city IN ('Bridgewatch','Caerleon','Fort Sterling',
                         'Lymhurst','Martlock','Thetford')
          GROUP BY item_id, day
        )
        SELECT s.item_id, s.generated_at, s.preco_recente, s.demanda_ratio,
               p.vwap
        FROM sinais s
        JOIN precos p ON p.item_id = s.item_id AND p.day = s.alvo
        WHERE s.preco_recente > 0
    """, {"srv": server, "h": horizon_days}).fetchall()
    if not rows:
        return {"n": 0, "horizon_days": horizon_days}
    returns = [(vwap / p0 - 1) for (_, _, p0, _, vwap) in rows]
    returns.sort()
    hits = sum(1 for r in returns if r > 0)
    return {
        "n": len(returns),
        "horizon_days": horizon_days,
        "hit_rate_pct": round(100 * hits / len(returns), 1),
        "retorno_medio_pct": round(100 * sum(returns) / len(returns), 2),
        "retorno_mediano_pct": round(100 * returns[len(returns) // 2], 2),
        "pior_pct": round(100 * returns[0], 2),
        "melhor_pct": round(100 * returns[-1], 2),
    }


def seed_combat_tags(aodp) -> int:
    """Tags de combate conservadoras e editáveis (item_combat_tags).

    Só o que é defensável por padrão de id: montarias de transporte.
    Classificações por arsenal (gank/zvz por arma) ficam para preenchimento
    manual/curado, como o plano exige.
    """
    with aodp.db_lock:
        cur = aodp.db.execute("""
            INSERT OR IGNORE INTO item_combat_tags
            SELECT item_id, 'transport', 'alta', 'padrão de id',
                   'montaria de carga'
            FROM static_items
            WHERE (item_id LIKE 'T_\\_MOUNT_OX' ESCAPE '\\'
                   OR item_id LIKE 'T_\\_MOUNT_MULE' ESCAPE '\\'
                   OR item_id LIKE '%MOUNT_MAMMOTH_TRANSPORT%')
              AND item_id NOT LIKE 'UNIQUE%'
        """)
        aodp.db.commit()
        return cur.rowcount


def classification_summary(con, server, days: float = 1):
    """Classifica mortes por porte e flags econômicas (estrutural + tags).

    Conservador: 'gank_provavel' = <=2 participantes; 'zvz' = >=15;
    vítima coletora = traje de coleta vestido; transporte = montaria de
    carga (tag) ou inventário com 10+ unidades.
    """
    params = {"srv": server, "delta": f"-{days} days"}
    classes = con.execute("""
        SELECT CASE WHEN n_participants >= 15 THEN 'zvz'
                    WHEN n_participants <= 2 THEN 'gank_provavel'
                    ELSE 'small_scale' END AS classe,
               COUNT(*) AS mortes,
               SUM(total_victim_kill_fame) AS fama
        FROM kill_events
        WHERE server = :srv
          AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
        GROUP BY classe ORDER BY mortes DESC
    """, params).fetchall()
    gatherers = con.execute("""
        SELECT COUNT(DISTINCT eq.event_id)
        FROM kill_event_equipment eq
        JOIN kill_events ke
          ON ke.server = eq.server AND ke.event_id = eq.event_id
        JOIN static_items si ON si.item_id = eq.item_id
        WHERE eq.server = :srv AND eq.role = 'victim'
          AND eq.slot IN ('Head', 'Armor', 'Shoes')
          AND si.cat = 'gathering'
          AND ke.ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
    """, params).fetchone()[0]
    transport_mount = con.execute("""
        SELECT COUNT(DISTINCT eq.event_id)
        FROM kill_event_equipment eq
        JOIN kill_events ke
          ON ke.server = eq.server AND ke.event_id = eq.event_id
        WHERE eq.server = :srv AND eq.role = 'victim' AND eq.slot = 'Mount'
          AND eq.item_id IN (SELECT item_id FROM item_combat_tags
                             WHERE role_tag = 'transport')
          AND ke.ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
    """, params).fetchone()[0]
    heavy_inventory = con.execute("""
        SELECT COUNT(*) FROM (
          SELECT eq.event_id, SUM(eq.count) AS s
          FROM kill_event_equipment eq
          JOIN kill_events ke
            ON ke.server = eq.server AND ke.event_id = eq.event_id
          WHERE eq.server = :srv AND eq.role = 'victim'
            AND eq.slot = 'Inventory'
            AND ke.ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
          GROUP BY eq.event_id HAVING s >= 10)
    """, params).fetchone()[0]
    total = sum(c[1] for c in classes) or 1
    return {
        "classes": [{"classe": c, "mortes": m, "fama": f,
                     "pct": round(100 * m / total, 1)}
                    for c, m, f in classes],
        "vitimas_coletoras": gatherers,
        "vitimas_montaria_transporte": transport_mount,
        "vitimas_inventario_pesado": heavy_inventory,
        "total_mortes": total,
    }


def risk_summary(con, server, days: float = 1):
    """Risco estrutural com o que é público HOJE (Location vem nulo):
    mortes por KillArea × hora UTC + batalhas grandes (proxy de ZvZ)."""
    by_area_hour = con.execute("""
        SELECT kill_area, substr(ts, 12, 2) AS hora_utc,
               COUNT(*) AS mortes, SUM(total_victim_kill_fame) AS fama
        FROM kill_events
        WHERE server = :srv
          AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
        GROUP BY kill_area, hora_utc
        ORDER BY mortes DESC
    """, {"srv": server, "delta": f"-{days} days"}).fetchall()
    zvz = con.execute("""
        SELECT b.battle_id, b.start_time, b.total_kills, b.total_fame,
               b.players, b.cluster_name,
               COUNT(DISTINCT e.entity_id) AS guildas
        FROM battle_summaries b
        LEFT JOIN battle_entities e
          ON e.server = b.server AND e.battle_id = b.battle_id
         AND e.entity_type = 'guild'
        WHERE b.server = :srv AND b.total_kills >= 15
          AND b.start_time >= strftime('%Y-%m-%dT%H:%M:%S', 'now', :delta)
        GROUP BY b.battle_id
        ORDER BY b.total_fame DESC LIMIT 15
    """, {"srv": server, "delta": f"-{days} days"}).fetchall()
    return {
        "por_area_hora": [{"kill_area": a or "?", "hora_utc": h,
                           "mortes": m, "fama": f}
                          for a, h, m, f in by_area_hour],
        "zvz_recentes": [{"battle_id": b, "inicio": s, "kills": k,
                          "fama": f, "jogadores": p, "zona": c,
                          "guildas": g}
                         for b, s, k, f, p, c, g in zvz],
    }


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

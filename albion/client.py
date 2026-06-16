# -*- coding: utf-8 -*-
"""Cliente da API do Albion Online Data Project com throttle e cache SQLite."""
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from . import config

DATA = Path(__file__).resolve().parent.parent / "data"

PRICE_FIELDS = [
    "sell_price_min", "sell_price_min_date", "sell_price_max", "sell_price_max_date",
    "buy_price_min", "buy_price_min_date", "buy_price_max", "buy_price_max_date",
]


class Throttle:
    """Respeita múltiplas janelas de rate limit (ex.: 180/min e 300/5min)."""

    def __init__(self, windows=config.RATE_WINDOWS):
        self.windows = windows
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                oldest_window = max(w for w, _ in self.windows)
                while self.calls and now - self.calls[0] > oldest_window:
                    self.calls.popleft()
                delay = 0.0
                for win, max_calls in self.windows:
                    recent = [t for t in self.calls if now - t <= win]
                    if len(recent) >= max_calls:
                        delay = max(delay, recent[0] + win - now)
                if delay <= 0:
                    self.calls.append(now)
                    return
            time.sleep(min(delay + 0.05, 5.0))


class AODP:
    """Cliente com cache em SQLite. Thread-safe (lock por conexão)."""

    def __init__(self, server: str = config.DEFAULT_SERVER,
                 db_path: Path = DATA / "cache.db"):
        self.server = server
        self.base = config.SERVERS[server]
        self.http = httpx.Client(timeout=40, headers={"User-Agent": config.USER_AGENT})
        self.throttle = Throttle()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db_lock = threading.Lock()
        self._init_db()

    # ---------------------------------------------------------------- DB

    def _init_db(self):
        with self.db_lock:
            # WAL + busy_timeout: servidor e CLI compartilham o mesmo arquivo
            # de cache em processos diferentes — sem isso há 'database is locked'
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS prices (
              server TEXT, item_id TEXT, city TEXT, quality INTEGER,
              sell_price_min INTEGER, sell_price_min_date TEXT,
              sell_price_max INTEGER, sell_price_max_date TEXT,
              buy_price_min INTEGER, buy_price_min_date TEXT,
              buy_price_max INTEGER, buy_price_max_date TEXT,
              fetched_at REAL,
              PRIMARY KEY (server, item_id, city, quality)
            );
            CREATE TABLE IF NOT EXISTS price_snapshots (
              server TEXT, item_id TEXT, city TEXT, quality INTEGER,
              sell_price_min INTEGER, sell_price_min_date TEXT,
              sell_price_max INTEGER, sell_price_max_date TEXT,
              buy_price_min INTEGER, buy_price_min_date TEXT,
              buy_price_max INTEGER, buy_price_max_date TEXT,
              fetched_at REAL,
              PRIMARY KEY (server, item_id, city, quality, fetched_at)
            );
            CREATE INDEX IF NOT EXISTS idx_price_snapshots_lookup
              ON price_snapshots (server, item_id, city, quality, fetched_at);
            -- seek por intervalo de tempo (backtest/survival): sem este índice
            -- as consultas por janela varrem a tabela inteira (ver AUDITORIA2)
            CREATE INDEX IF NOT EXISTS idx_price_snapshots_time
              ON price_snapshots (server, fetched_at);
            CREATE TABLE IF NOT EXISTS fetch_log (
              server TEXT, kind TEXT, key TEXT, fetched_at REAL,
              PRIMARY KEY (server, kind, key)
            );
            CREATE TABLE IF NOT EXISTS history (
              server TEXT, item_id TEXT, city TEXT, quality INTEGER,
              time_scale INTEGER, ts TEXT, item_count INTEGER, avg_price REAL,
              fetched_at REAL,
              PRIMARY KEY (server, item_id, city, quality, time_scale, ts)
            );
            CREATE TABLE IF NOT EXISTS gold (
              server TEXT, ts TEXT, price INTEGER,
              PRIMARY KEY (server, ts)
            );
            CREATE TABLE IF NOT EXISTS watchlist (
              server TEXT, item_id TEXT, added_at REAL,
              PRIMARY KEY (server, item_id)
            );
            CREATE TABLE IF NOT EXISTS positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              server TEXT, item_id TEXT, quality INTEGER, qty INTEGER,
              buy_price REAL, buy_city TEXT, opened_at REAL,
              sell_price REAL, sell_city TEXT, closed_at REAL, note TEXT
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
              server TEXT, started_at REAL, finished_at REAL, source TEXT,
              items INTEGER, price_rows INTEGER, history_series INTEGER,
              ok INTEGER, error TEXT,
              PRIMARY KEY (server, started_at)
            );
            CREATE TABLE IF NOT EXISTS public_data_runs (
              server TEXT, source TEXT, started_at REAL, finished_at REAL,
              pages INTEGER, rows_seen INTEGER, rows_inserted INTEGER,
              newest_remote_id INTEGER, ok INTEGER, error TEXT,
              PRIMARY KEY (server, source, started_at)
            );
            CREATE TABLE IF NOT EXISTS public_ingest_checkpoints (
              server TEXT, source TEXT, cursor_value INTEGER,
              last_success_at REAL,
              PRIMARY KEY (server, source)
            );
            CREATE TABLE IF NOT EXISTS kill_events (
              server TEXT, event_id INTEGER, ts TEXT, battle_id INTEGER,
              type TEXT, kill_area TEXT, location TEXT,
              total_victim_kill_fame INTEGER, n_participants INTEGER,
              group_members INTEGER, killer_id TEXT, victim_id TEXT,
              fetched_at REAL,
              PRIMARY KEY (server, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kill_events_ts
              ON kill_events (server, ts);
            CREATE TABLE IF NOT EXISTS kill_event_actors (
              server TEXT, event_id INTEGER, role TEXT, player_id TEXT,
              player_name TEXT, guild_id TEXT, guild_name TEXT,
              alliance_id TEXT, alliance_name TEXT, avg_ip REAL,
              kill_fame INTEGER, death_fame INTEGER, damage_done REAL,
              healing_done REAL,
              PRIMARY KEY (server, event_id, role, player_id)
            );
            CREATE TABLE IF NOT EXISTS kill_event_equipment (
              server TEXT, event_id INTEGER, role TEXT, slot TEXT,
              item_id TEXT, count INTEGER, quality INTEGER,
              PRIMARY KEY (server, event_id, role, slot, item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kill_equip_item
              ON kill_event_equipment (server, item_id);
            CREATE TABLE IF NOT EXISTS battle_summaries (
              server TEXT, battle_id INTEGER, start_time TEXT, end_time TEXT,
              total_fame INTEGER, total_kills INTEGER, players INTEGER,
              cluster_name TEXT, fetched_at REAL,
              PRIMARY KEY (server, battle_id)
            );
            CREATE TABLE IF NOT EXISTS battle_entities (
              server TEXT, battle_id INTEGER, entity_type TEXT,
              entity_id TEXT, entity_name TEXT, alliance_name TEXT,
              kills INTEGER, deaths INTEGER, kill_fame INTEGER,
              PRIMARY KEY (server, battle_id, entity_type, entity_id)
            );
            CREATE TABLE IF NOT EXISTS item_demand_daily (
              server TEXT, day TEXT, item_id TEXT,
              victim_units INTEGER, victim_events INTEGER,
              killer_units INTEGER, inventory_units INTEGER,
              PRIMARY KEY (server, day, item_id)
            );
            CREATE TABLE IF NOT EXISTS static_items (
              item_id TEXT PRIMARY KEY, name_pt TEXT, cat TEXT, sub TEXT,
              tier INTEGER, ench INTEGER, weight REAL
            );
            CREATE TABLE IF NOT EXISTS item_combat_tags (
              item_id TEXT, role_tag TEXT, confidence TEXT, source TEXT,
              notes TEXT,
              PRIMARY KEY (item_id, role_tag)
            );
            CREATE TABLE IF NOT EXISTS demand_signal_log (
              server TEXT, generated_at REAL, item_id TEXT,
              demanda_dia_recente REAL, demanda_ratio REAL,
              preco_recente REAL, preco_ratio REAL, volume_dia REAL,
              PRIMARY KEY (server, generated_at, item_id)
            );
            CREATE TABLE IF NOT EXISTS service_orders (
              order_id INTEGER PRIMARY KEY AUTOINCREMENT,
              server TEXT, created_at REAL, expires_at REAL,
              profile_target TEXT, action_type TEXT,
              item_id TEXT, city_from TEXT, city_to TEXT,
              quantity_base INTEGER, capital_required REAL,
              expected_profit REAL, expected_roi REAL,
              risk_level TEXT, confidence TEXT,
              explanation_short TEXT, source_signal TEXT, status TEXT
            );
            CREATE TABLE IF NOT EXISTS service_order_feedback (
              order_id INTEGER, feedback_at REAL, accepted INTEGER,
              realized_profit REAL, notes TEXT,
              PRIMARY KEY (order_id, feedback_at)
            );
            CREATE TABLE IF NOT EXISTS economic_assumptions (
              key TEXT PRIMARY KEY, value REAL, source TEXT,
              confidence TEXT, updated_at REAL, notes TEXT
            );
            CREATE TABLE IF NOT EXISTS price_snapshots_daily (
              server TEXT, item_id TEXT, city TEXT, quality INTEGER, day TEXT,
              sell_min INTEGER, sell_avg REAL, sell_max INTEGER,
              buy_min INTEGER, buy_avg REAL, buy_max INTEGER,
              samples INTEGER,
              PRIMARY KEY (server, item_id, city, quality, day)
            );
            """)
            self.db.commit()

    def _fetch_ages(self, kind: str, keys: list[str]) -> dict[str, float]:
        """fetched_at por chave do fetch_log."""
        out = {}
        with self.db_lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self.db.execute(
                    f"SELECT key, fetched_at FROM fetch_log"
                    f" WHERE server=? AND kind=? AND key IN ({ph})",
                    [self.server, kind, *chunk]).fetchall()
                out.update(dict(rows))
        return out

    def _mark_fetched(self, kind: str, keys: list[str], when: float):
        with self.db_lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO fetch_log (server, kind, key, fetched_at)"
                " VALUES (?,?,?,?)",
                [(self.server, kind, k, when) for k in keys])
            self.db.commit()

    # ---------------------------------------------------------------- HTTP

    def _get(self, path: str, params: dict | None = None):
        url = self.base + path
        for attempt in range(4):
            self.throttle.wait()
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 500:  # 5xx esporádico do serviço comunitário
                if attempt == 3:
                    r.raise_for_status()
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"API continuou retornando 429 para {url}")

    @staticmethod
    def _chunk_items(item_ids: list[str], base_len: int) -> list[list[str]]:
        """Divide a lista de itens respeitando o limite de URL e de contagem."""
        chunks, cur, cur_len = [], [], base_len
        for iid in item_ids:
            add = len(quote(iid)) + 1
            if cur and (cur_len + add > config.MAX_URL_LEN
                        or len(cur) >= config.MAX_ITEMS_PER_REQUEST):
                chunks.append(cur)
                cur, cur_len = [], base_len
            cur.append(iid)
            cur_len += add
        if cur:
            chunks.append(cur)
        return chunks

    # ---------------------------------------------------------------- preços

    def get_prices(self, item_ids: list[str], cities: list[str] | None = None,
                   max_age: int = config.PRICES_TTL) -> list[dict]:
        """Preços atuais (todas as qualidades) com cache.

        Retorna linhas do cache para (item, cidade) solicitados; busca na API
        o que estiver mais velho que max_age segundos.
        """
        cities = cities or config.CITIES
        item_ids = list(dict.fromkeys(item_ids))  # únicos, mantém ordem
        now = time.time()

        keys = [f"{i}|{c}" for i in item_ids for c in cities]
        ages = self._fetch_ages("prices", keys)
        stale_items = sorted({k.split("|")[0] for k in keys
                              if now - ages.get(k, 0) > max_age})

        if stale_items:
            loc_param = ",".join(cities)
            base_len = len(self.base) + len("/api/v2/stats/prices/.json") \
                + len("?locations=") + len(quote(loc_param)) + len("&qualities=0")
            for chunk in self._chunk_items(stale_items, base_len):
                rows = self._get(
                    "/api/v2/stats/prices/" + quote(",".join(chunk)) + ".json",
                    params={"locations": loc_param})
                fetched = time.time()
                valid_rows = [r for r in rows if r.get("city") not in (None, "0")]
                with self.db_lock:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO prices VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(self.server, r["item_id"], r["city"], r["quality"],
                          r["sell_price_min"], r["sell_price_min_date"],
                          r["sell_price_max"], r["sell_price_max_date"],
                          r["buy_price_min"], r["buy_price_min_date"],
                          r["buy_price_max"], r["buy_price_max_date"], fetched)
                         for r in valid_rows])
                    self.db.executemany(
                        "INSERT OR IGNORE INTO price_snapshots VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(self.server, r["item_id"], r["city"], r["quality"],
                          r["sell_price_min"], r["sell_price_min_date"],
                          r["sell_price_max"], r["sell_price_max_date"],
                          r["buy_price_min"], r["buy_price_min_date"],
                          r["buy_price_max"], r["buy_price_max_date"], fetched)
                         for r in valid_rows])
                    self.db.commit()
                self._mark_fetched(
                    "prices", [f"{i}|{c}" for i in chunk for c in cities], fetched)

        out = []
        with self.db_lock:
            for i in range(0, len(item_ids), 400):
                chunk = item_ids[i:i + 400]
                ph_i = ",".join("?" * len(chunk))
                ph_c = ",".join("?" * len(cities))
                rows = self.db.execute(
                    f"SELECT item_id, city, quality, {', '.join(PRICE_FIELDS)},"
                    f" fetched_at FROM prices WHERE server=?"
                    f" AND item_id IN ({ph_i}) AND city IN ({ph_c})",
                    [self.server, *chunk, *cities]).fetchall()
                cols = ["item_id", "city", "quality", *PRICE_FIELDS, "fetched_at"]
                out.extend(dict(zip(cols, r)) for r in rows)
        return out

    # ---------------------------------------------------------------- histórico

    def get_history(self, item_ids: list[str], cities: list[str] | None = None,
                    time_scale: int = 24, days: int = 30,
                    max_age: int = config.HISTORY_TTL) -> list[dict]:
        """Séries históricas (volume + preço médio) com cache.

        Retorna [{item_id, city, quality, data: [{ts, item_count, avg_price}]}]
        """
        cities = cities or config.CITIES
        item_ids = list(dict.fromkeys(item_ids))
        now = time.time()
        today = datetime.now(timezone.utc)
        date_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

        # janela canônica: busca-se sempre uma janela >= à pedida, e qualquer
        # janela canônica fresca que cubra o pedido evita nova ida à API
        windows = config.HISTORY_FETCH_WINDOWS.get(time_scale, [days])
        covering = [w for w in windows if w >= days] or [days]
        fetch_days = covering[0] if covering[0] >= days else max(days, windows[-1])

        all_keys = [f"{i}|{c}|{time_scale}|{w}"
                    for i in item_ids for c in cities for w in covering]
        ages = self._fetch_ages("history", all_keys)

        def is_fresh(item, city):
            return any(now - ages.get(f"{item}|{city}|{time_scale}|{w}", 0) <= max_age
                       for w in covering)

        stale_items = sorted({i for i in item_ids for c in cities
                              if not is_fresh(i, c)})

        if stale_items:
            fetch_from = (today - timedelta(days=fetch_days)).strftime("%Y-%m-%d")
            loc_param = ",".join(cities)
            base_len = len(self.base) + 120 + len(quote(loc_param))
            for chunk in self._chunk_items(stale_items, base_len):
                series = self._get(
                    "/api/v2/stats/history/" + quote(",".join(chunk)) + ".json",
                    params={"locations": loc_param, "time-scale": time_scale,
                            "date": fetch_from, "end_date": date_to})
                fetched = time.time()
                with self.db_lock:
                    for s in series:
                        if s.get("location") in (None, "0"):
                            continue
                        self.db.executemany(
                            "INSERT OR REPLACE INTO history VALUES "
                            "(?,?,?,?,?,?,?,?,?)",
                            [(self.server, s["item_id"], s["location"],
                              s["quality"], time_scale, p["timestamp"],
                              p["item_count"], p["avg_price"], fetched)
                             for p in s.get("data", [])])
                    self.db.commit()
                self._mark_fetched(
                    "history",
                    [f"{i}|{c}|{time_scale}|{fetch_days}"
                     for i in chunk for c in cities],
                    fetched)

        out = {}
        with self.db_lock:
            for i in range(0, len(item_ids), 400):
                chunk = item_ids[i:i + 400]
                ph_i = ",".join("?" * len(chunk))
                ph_c = ",".join("?" * len(cities))
                rows = self.db.execute(
                    "SELECT item_id, city, quality, ts, item_count, avg_price"
                    f" FROM history WHERE server=? AND time_scale=? AND ts>=?"
                    f" AND item_id IN ({ph_i}) AND city IN ({ph_c})"
                    " ORDER BY ts",
                    [self.server, time_scale, date_from, *chunk, *cities]
                ).fetchall()
                for item_id, city, quality, ts, cnt, avg in rows:
                    s = out.setdefault((item_id, city, quality), {
                        "item_id": item_id, "city": city, "quality": quality,
                        "data": []})
                    s["data"].append({"ts": ts, "item_count": cnt, "avg_price": avg})
        return list(out.values())

    # ---------------------------------------------------------------- ouro

    def get_gold(self, count: int = 24,
                 max_age: int = config.GOLD_TTL) -> list[dict]:
        ages = self._fetch_ages("gold", ["gold"])
        with self.db_lock:
            cached = self.db.execute(
                "SELECT COUNT(*) FROM gold WHERE server=?",
                [self.server]).fetchone()[0]
        if time.time() - ages.get("gold", 0) > max_age or cached < count:
            rows = self._get("/api/v2/stats/gold.json", params={"count": count})
            fetched = time.time()
            with self.db_lock:
                self.db.executemany(
                    "INSERT OR REPLACE INTO gold VALUES (?,?,?)",
                    [(self.server, r["timestamp"], r["price"]) for r in rows])
                self.db.commit()
            self._mark_fetched("gold", ["gold"], fetched)
        with self.db_lock:
            rows = self.db.execute(
                "SELECT ts, price FROM gold WHERE server=?"
                " ORDER BY ts DESC LIMIT ?", [self.server, count]).fetchall()
        return [{"ts": ts, "price": price} for ts, price in rows]

    # ---------------------------------------------------------------- watchlist

    def watch_add(self, item_ids: list[str]) -> int:
        with self.db_lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO watchlist VALUES (?,?,?)",
                [(self.server, i, time.time()) for i in item_ids])
            self.db.commit()
        return len(item_ids)

    def watch_remove(self, item_ids: list[str]) -> None:
        with self.db_lock:
            self.db.executemany(
                "DELETE FROM watchlist WHERE server=? AND item_id=?",
                [(self.server, i) for i in item_ids])
            self.db.commit()

    def watch_list(self) -> list[dict]:
        with self.db_lock:
            rows = self.db.execute(
                "SELECT item_id, added_at FROM watchlist WHERE server=?"
                " ORDER BY item_id", [self.server]).fetchall()
        return [{"item_id": i, "added_at": a} for i, a in rows]

    def collect(self, item_ids: list[str] | None = None,
                cities: list[str] | None = None, days: int = 30,
                max_items: int = config.COLLECT_MAX_ITEMS,
                source: str = "manual") -> dict:
        """Coleta disciplinada: preços frescos + histórico para a watchlist.

        Alimenta prices/price_snapshots/history e registra a rodada em
        collection_runs (auditoria da coleta de longo prazo).
        """
        if item_ids is None:
            item_ids = [w["item_id"] for w in self.watch_list()]
        item_ids = item_ids[:max_items]
        started = time.time()
        result = {"items": len(item_ids), "price_rows": 0,
                  "history_series": 0, "source": source}
        if not item_ids:
            return result
        cities = cities or config.CITIES
        ok, error = 1, None
        try:
            price_rows = self.get_prices(item_ids, cities, max_age=0)
            series = self.get_history(item_ids, cities, time_scale=24,
                                      days=days, max_age=0)
            result["price_rows"] = len(price_rows)
            result["history_series"] = len(series)
            # B1: mantém o roll-up diário sempre atual (não só na poda). Agrega
            # os ultimos 2 dias dos itens coletados (today + boundary de ontem) —
            # idempotente por (server,item,city,quality,day). Antes,
            # price_snapshots_daily so era escrito na poda (>7d) e ficava vazio.
            self._rollup_daily(item_ids, days=2)
        except Exception as e:
            ok, error = 0, repr(e)[:500]
            raise
        finally:
            with self.db_lock:
                self.db.execute(
                    "INSERT OR REPLACE INTO collection_runs VALUES "
                    "(?,?,?,?,?,?,?,?,?)",
                    (self.server, started, time.time(), source,
                     result["items"], result["price_rows"],
                     result["history_series"], ok, error))
                self.db.commit()
        return result

    def _rollup_daily(self, item_ids: list[str] | None = None, days: int = 2):
        """Agrega price_snapshots -> price_snapshots_daily na janela recente.

        Roda na coleta para o roll-up diário ficar sempre atual (a poda só
        cuidava dos dias > retenção). Idempotente: INSERT OR REPLACE pela PK do
        dia. Restringe aos item_ids coletados para limitar o custo."""
        cutoff = time.time() - days * 86400
        where = ["server=?", "fetched_at >= ?"]
        params = [self.server, cutoff]
        if item_ids:
            ph = ",".join("?" * len(item_ids))
            where.append(f"item_id IN ({ph})")
            params += list(item_ids)
        with self.db_lock:
            self.db.execute(f"""
                INSERT OR REPLACE INTO price_snapshots_daily
                SELECT server, item_id, city, quality,
                       date(fetched_at, 'unixepoch') AS day,
                       MIN(NULLIF(sell_price_min,0)), AVG(NULLIF(sell_price_min,0)),
                       MAX(NULLIF(sell_price_min,0)),
                       MIN(NULLIF(buy_price_max,0)), AVG(NULLIF(buy_price_max,0)),
                       MAX(NULLIF(buy_price_max,0)), COUNT(*)
                FROM price_snapshots
                WHERE {' AND '.join(where)}
                GROUP BY server, item_id, city, quality, day
            """, params)
            self.db.commit()

    def sync_static_items(self, items: list[dict]) -> int:
        """Espelha o catálogo de itens no SQLite (joins do killboard etc.)."""
        with self.db_lock:
            n = self.db.execute(
                "SELECT COUNT(*) FROM static_items").fetchone()[0]
            if n == len(items):
                return 0
            self.db.execute("DELETE FROM static_items")
            self.db.executemany(
                "INSERT OR REPLACE INTO static_items VALUES (?,?,?,?,?,?,?)",
                [(i["id"], i["pt"], i["cat"], i["sub"], i["tier"],
                  i["ench"], i["w"]) for i in items])
            self.db.commit()
        return len(items)

    # ---------------------------------------------------------------- posições

    def pos_add(self, item_id, qty, buy_price, buy_city=None, quality=1,
                note=None) -> int:
        with self.db_lock:
            cur = self.db.execute(
                "INSERT INTO positions (server,item_id,quality,qty,buy_price,"
                "buy_city,opened_at,note) VALUES (?,?,?,?,?,?,?,?)",
                (self.server, item_id, quality, qty, buy_price, buy_city,
                 time.time(), note))
            self.db.commit()
            return cur.lastrowid

    def pos_close(self, pos_id, sell_price, sell_city=None) -> bool:
        with self.db_lock:
            cur = self.db.execute(
                "UPDATE positions SET sell_price=?, sell_city=?, closed_at=?"
                " WHERE id=? AND server=? AND closed_at IS NULL",
                (sell_price, sell_city, time.time(), pos_id, self.server))
            self.db.commit()
            return cur.rowcount > 0

    def pos_delete(self, pos_id) -> bool:
        with self.db_lock:
            cur = self.db.execute(
                "DELETE FROM positions WHERE id=? AND server=?",
                (pos_id, self.server))
            self.db.commit()
            return cur.rowcount > 0

    def pos_list(self, include_closed=False) -> list[dict]:
        q = ("SELECT id, item_id, quality, qty, buy_price, buy_city,"
             " opened_at, sell_price, sell_city, closed_at, note"
             " FROM positions WHERE server=?")
        if not include_closed:
            q += " AND closed_at IS NULL"
        with self.db_lock:
            rows = self.db.execute(q + " ORDER BY id", [self.server]).fetchall()
        cols = ["id", "item_id", "quality", "qty", "buy_price", "buy_city",
                "opened_at", "sell_price", "sell_city", "closed_at", "note"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------------------------------------------------------------- retenção

    def snapshot_prune(self, days: int = config.SNAPSHOT_RETENTION_DAYS,
                       vacuum: bool = True) -> dict:
        """Agrega snapshots brutos de dias COMPLETOS antigos em
        price_snapshots_daily e apaga.

        Corta na meia-noite UTC (limite de dia), não no instante atual: assim
        um dia de fronteira nunca é agregado pela metade e re-agregado/
        subcontado numa execução posterior (SQL-4). O corte por epoch usa o
        índice (server, fetched_at). vacuum=False pula o VACUUM (que segura o
        lock) — usado pela poda automática do servidor.
        """
        cutoff = ((datetime.now(timezone.utc) - timedelta(days=days))
                  .replace(hour=0, minute=0, second=0, microsecond=0)
                  .timestamp())
        with self.db_lock:
            cur_agg = self.db.execute("""
                INSERT OR REPLACE INTO price_snapshots_daily
                SELECT server, item_id, city, quality,
                       date(fetched_at, 'unixepoch') AS day,
                       MIN(NULLIF(sell_price_min,0)),
                       AVG(NULLIF(sell_price_min,0)),
                       MAX(NULLIF(sell_price_min,0)),
                       MIN(NULLIF(buy_price_max,0)),
                       AVG(NULLIF(buy_price_max,0)),
                       MAX(NULLIF(buy_price_max,0)),
                       COUNT(*)
                FROM price_snapshots
                WHERE fetched_at < ?
                GROUP BY server, item_id, city, quality, day
            """, [cutoff])
            aggregated = cur_agg.rowcount
            cur = self.db.execute(
                "DELETE FROM price_snapshots WHERE fetched_at < ?", [cutoff])
            deleted = cur.rowcount
            self.db.commit()
            if vacuum:
                self.db.execute("VACUUM")
        return {"aggregated_days": aggregated, "deleted_rows": deleted}

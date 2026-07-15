# -*- coding: utf-8 -*-
"""Camada de dados dual: SQLite (dev local) ou Postgres (nuvem/Supabase).

Por que existe
--------------
O app nasceu sobre SQLite local (`data/cache.db`). Para o piloto gratuito
(Vercel + Supabase, sem PC ligado) o mesmo código precisa falar com Postgres.
Em vez de reescrever as ~8 análises que leem o cache, este módulo dá um
*seam* único:

- `connect()` devolve uma conexão cujo `.execute(sql, params)` funciona igual
  nos dois bancos. As linhas devolvidas imitam `sqlite3.Row`: aceitam acesso
  posicional (`r[0]`), por nome (`r["city"]`), desempacotamento
  (`for a, b in rows`) e `dict(r)`. Assim `microstructure.py` (posicional) e
  `app.py` (`dict(r)`/`r["x"]`) seguem inalterados.
- Placeholders são sempre `?` no código; no Postgres viram `%s` aqui.
- Escritas com conflito de chave usam `upsert()`/`upsert_ignore()`, que emitem
  `INSERT OR REPLACE`/`OR IGNORE` (SQLite) ou `ON CONFLICT` (Postgres).

Regra de ouro do código que usa este módulo: **nada de funções de data do SQL**
(`date('now', ?)`, `strftime`, `datetime(...)`). Calcule o corte em Python e
passe a string ISO como parâmetro — `substr(ts,1,10)` é portável e pode ficar.

Seleção do backend: se a env `DATABASE_URL` existir -> Postgres; senão SQLite.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SQLITE = DATA / "cache.db"


def database_url() -> str | None:
    """URL do Postgres (Supabase) ou None para usar SQLite local."""
    url = os.environ.get("DATABASE_URL", "").strip()
    return url or None


def backend() -> str:
    return "postgres" if database_url() else "sqlite"


# --------------------------------------------------------------------- Row
class Row:
    """Linha que imita `sqlite3.Row` (posicional + nome + dict() + unpack)."""

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: list[str], vals: list):
        self._cols = cols
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return list(self._cols)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, ValueError, IndexError):
            return default

    def __repr__(self):
        return f"Row({dict(zip(self._cols, self._vals))!r})"


class _Cur:
    """Cursor mínimo para o backend Postgres (fetchall/fetchone/iter)."""

    def __init__(self, rows: list, rowcount: int = -1):
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


# --------------------------------------------------------------- conexões
class _SqliteConn:
    """Fina casca sobre sqlite3 — preserva 100% do comportamento atual."""

    backend = "sqlite"

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return self.raw.execute(sql, params)

    def executemany(self, sql, seq):
        return self.raw.executemany(sql, seq)

    def executescript(self, sql):
        return self.raw.executescript(sql)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


class _PgConn:
    """Casca sobre psycopg que fala o mesmo dialeto que `_SqliteConn`."""

    backend = "postgres"

    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _tr(sql: str) -> str:
        # 1) escapa % literal (psycopg usa %); 2) ? -> %s
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql, params=()):
        cur = self.raw.cursor()
        cur.execute(self._tr(sql), tuple(params))
        if cur.description is not None:
            cols = [d.name for d in cur.description]
            rows = [Row(cols, list(v)) for v in cur.fetchall()]
            rc = cur.rowcount
            cur.close()
            return _Cur(rows, rc)
        rc = cur.rowcount
        cur.close()
        return _Cur([], rc)

    def executemany(self, sql, seq):
        cur = self.raw.cursor()
        cur.executemany(self._tr(sql), [tuple(x) for x in seq])
        cur.close()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


_PG_LOCK = threading.Lock()


def connect(readonly: bool = False, path: str | Path | None = None):
    """Abre uma conexão no backend ativo.

    `path` aponta o arquivo SQLite (default `data/cache.db`); é IGNORADO quando
    `DATABASE_URL` existe (prod usa Postgres). `readonly` só vale para SQLite.
    """
    if backend() == "postgres":
        import psycopg  # import tardio: dev local não precisa do driver

        # readonly -> autocommit=True: cada SELECT é sua própria transação, então
        # uma query que falha (tabela legada ausente) NÃO envenena as seguintes.
        # Conexão gravável -> autocommit=False; quem escreve chama commit().
        # prepare_threshold=None desliga prepared statements: obrigatório no
        # pooler do Supabase em modo TRANSAÇÃO (porta 6543), que serverless usa.
        conn = psycopg.connect(database_url(), autocommit=readonly,
                               prepare_threshold=None)
        return _PgConn(conn)

    import sqlite3

    sqlite_path = Path(path) if path else DEFAULT_SQLITE
    if readonly:
        if not sqlite_path.exists():
            return None
        raw = sqlite3.connect(
            f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
        # Caminho de ANÁLISE: linhas tipo dict (r["city"], dict(r)).
        raw.row_factory = sqlite3.Row
    else:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(sqlite_path), check_same_thread=False)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA busy_timeout=5000")
        raw.execute("PRAGMA synchronous=NORMAL")
        # Conexão GRAVÁVEL (cliente AODP): linhas como TUPLA simples, igual ao
        # comportamento original — o client.py lê por posição/desempacote.
    return _SqliteConn(raw)


# ------------------------------------------------------------------ upsert
def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def upsert(conn, table: str, columns: list[str], rows, conflict: list[str]):
    """INSERT que sobrescreve em conflito de chave (REPLACE / ON CONFLICT)."""
    rows = list(rows)
    if not rows:
        return
    cols = ",".join(columns)
    ph = _placeholders(len(columns))
    if conn.backend == "sqlite":
        sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({ph})"
    else:
        upd = ",".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in conflict)
        on = ",".join(conflict)
        sql = (f"INSERT INTO {table} ({cols}) VALUES ({ph}) "
               f"ON CONFLICT ({on}) DO UPDATE SET {upd}")
    conn.executemany(sql, rows)


def upsert_ignore(conn, table: str, columns: list[str], rows,
                  conflict: list[str]):
    """INSERT que ignora em conflito de chave (OR IGNORE / DO NOTHING)."""
    rows = list(rows)
    if not rows:
        return
    cols = ",".join(columns)
    ph = _placeholders(len(columns))
    if conn.backend == "sqlite":
        sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({ph})"
    else:
        on = ",".join(conflict)
        sql = (f"INSERT INTO {table} ({cols}) VALUES ({ph}) "
               f"ON CONFLICT ({on}) DO NOTHING")
    conn.executemany(sql, rows)


def upsert_add(conn, table: str, columns: list[str], rows,
               conflict: list[str], add_cols: list[str]):
    """INSERT que SOMA as colunas `add_cols` em conflito de chave.

    Para agregados incrementais (ex.: killboard magro): cada rodada vê eventos
    novos e ACUMULA a contagem do dia, em vez de sobrescrever. Os dois backends
    suportam `ON CONFLICT(...) DO UPDATE SET col = tabela.col + excluded.col`.
    """
    rows = list(rows)
    if not rows:
        return
    cols = ",".join(columns)
    ph = _placeholders(len(columns))
    on = ",".join(conflict)
    # SQLite usa o nome real da tabela; Postgres idem (qualifica a coluna alvo).
    sets = ",".join(f"{c}={table}.{c}+excluded.{c}" for c in add_cols)
    if conn.backend == "sqlite":
        sql = (f"INSERT INTO {table} ({cols}) VALUES ({ph}) "
               f"ON CONFLICT({on}) DO UPDATE SET {sets}")
    else:
        sets_pg = ",".join(f"{c}={table}.{c}+EXCLUDED.{c}" for c in add_cols)
        sql = (f"INSERT INTO {table} ({cols}) VALUES ({ph}) "
               f"ON CONFLICT ({on}) DO UPDATE SET {sets_pg}")
    conn.executemany(sql, rows)


# ------------------------------------------------------------------ datas
def cutoff_iso(days: float) -> str:
    """Corte ISO (UTC) para `ts >= ?` — substitui `date('now', '-N days')`.

    Como `ts` é texto ISO de largura fixa, a comparação lexicográfica equivale
    à temporal. Retorna só a data (YYYY-MM-DD), suficiente para os filtros.
    """
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ schema
# Tabelas do PILOTO (somente as vivas). Mortas (killboard, snapshots finos,
# service_orders, etc.) não migram — ver reconciliação no CLAUDE.md.
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
  server TEXT, item_id TEXT, city TEXT, quality INTEGER,
  sell_price_min INTEGER, sell_price_min_date TEXT,
  sell_price_max INTEGER, sell_price_max_date TEXT,
  buy_price_min INTEGER, buy_price_min_date TEXT,
  buy_price_max INTEGER, buy_price_max_date TEXT,
  fetched_at REAL,
  PRIMARY KEY (server, item_id, city, quality)
);
CREATE TABLE IF NOT EXISTS history (
  server TEXT, item_id TEXT, city TEXT, quality INTEGER,
  time_scale INTEGER, ts TEXT, item_count INTEGER, avg_price REAL,
  fetched_at REAL,
  PRIMARY KEY (server, item_id, city, quality, time_scale, ts)
);
CREATE INDEX IF NOT EXISTS idx_history_lookup
  ON history (server, time_scale, ts);
CREATE TABLE IF NOT EXISTS gold (
  server TEXT, ts TEXT, price INTEGER,
  PRIMARY KEY (server, ts)
);
CREATE TABLE IF NOT EXISTS fetch_log (
  server TEXT, kind TEXT, key TEXT, fetched_at REAL,
  PRIMARY KEY (server, kind, key)
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  server TEXT, item_id TEXT, quality INTEGER, qty INTEGER,
  buy_price REAL, buy_city TEXT, opened_at REAL,
  sell_price REAL, sell_city TEXT, closed_at REAL, note TEXT,
  org_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sweep_state (
  server TEXT PRIMARY KEY, cursor INTEGER, cycle INTEGER,
  updated_at REAL, last_items INTEGER
);
CREATE TABLE IF NOT EXISTS kill_demand_daily (
  server TEXT, day TEXT, item_id TEXT, slot TEXT, quality INTEGER,
  victim_units INTEGER, victim_events INTEGER,
  PRIMARY KEY (server, day, item_id, slot, quality)
);
CREATE INDEX IF NOT EXISTS idx_kill_demand_day
  ON kill_demand_daily (server, day);
CREATE TABLE IF NOT EXISTS public_ingest_checkpoints (
  server TEXT, source TEXT, cursor_value INTEGER, last_success_at REAL,
  PRIMARY KEY (server, source)
);
CREATE TABLE IF NOT EXISTS orgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  discord_guild_id INTEGER,
  active INTEGER NOT NULL DEFAULT 1,
  plan TEXT NOT NULL DEFAULT 'pilot',
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_discord
  ON orgs (discord_guild_id);
CREATE TABLE IF NOT EXISTS org_entitlements (
  org_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY (org_id, scope)
);
CREATE TABLE IF NOT EXISTS account_entitlements (
  account_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  granted_at REAL NOT NULL,
  renewed_at REAL,
  expires_at TEXT,
  source TEXT,
  PRIMARY KEY (account_id, scope)
);
CREATE INDEX IF NOT EXISTS idx_acct_ent_expiry
  ON account_entitlements (scope, expires_at);
CREATE TABLE IF NOT EXISTS production_chains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at REAL NOT NULL,
  org_id INTEGER NOT NULL DEFAULT 1,
  UNIQUE(owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_production_chains_owner
  ON production_chains (owner_user_id);
CREATE TABLE IF NOT EXISTS killfeed_settings (
  org_id     INTEGER PRIMARY KEY,
  channel_id TEXT,
  min_fame   INTEGER NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS killfeed_watch (
  org_id          INTEGER NOT NULL,
  guild_name      TEXT NOT NULL,
  guild_name_norm TEXT NOT NULL,
  guild_id        TEXT,
  added_at        REAL,
  PRIMARY KEY (org_id, guild_name_norm)
);
"""

# Postgres: mesmos campos, tipos nativos. SERIAL para positions.id.
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
  server TEXT, item_id TEXT, city TEXT, quality INTEGER,
  sell_price_min BIGINT, sell_price_min_date TEXT,
  sell_price_max BIGINT, sell_price_max_date TEXT,
  buy_price_min BIGINT, buy_price_min_date TEXT,
  buy_price_max BIGINT, buy_price_max_date TEXT,
  fetched_at DOUBLE PRECISION,
  PRIMARY KEY (server, item_id, city, quality)
);
CREATE TABLE IF NOT EXISTS history (
  server TEXT, item_id TEXT, city TEXT, quality INTEGER,
  time_scale INTEGER, ts TEXT, item_count BIGINT, avg_price DOUBLE PRECISION,
  fetched_at DOUBLE PRECISION,
  PRIMARY KEY (server, item_id, city, quality, time_scale, ts)
);
CREATE INDEX IF NOT EXISTS idx_history_lookup
  ON history (server, time_scale, ts);
CREATE TABLE IF NOT EXISTS gold (
  server TEXT, ts TEXT, price BIGINT,
  PRIMARY KEY (server, ts)
);
CREATE TABLE IF NOT EXISTS fetch_log (
  server TEXT, kind TEXT, key TEXT, fetched_at DOUBLE PRECISION,
  PRIMARY KEY (server, kind, key)
);
CREATE TABLE IF NOT EXISTS positions (
  id SERIAL PRIMARY KEY,
  server TEXT, item_id TEXT, quality INTEGER, qty INTEGER,
  buy_price DOUBLE PRECISION, buy_city TEXT, opened_at DOUBLE PRECISION,
  sell_price DOUBLE PRECISION, sell_city TEXT, closed_at DOUBLE PRECISION,
  note TEXT,
  org_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sweep_state (
  server TEXT PRIMARY KEY, cursor INTEGER, cycle INTEGER,
  updated_at DOUBLE PRECISION, last_items INTEGER
);
CREATE TABLE IF NOT EXISTS kill_demand_daily (
  server TEXT, day TEXT, item_id TEXT, slot TEXT, quality INTEGER,
  victim_units BIGINT, victim_events BIGINT,
  PRIMARY KEY (server, day, item_id, slot, quality)
);
CREATE INDEX IF NOT EXISTS idx_kill_demand_day
  ON kill_demand_daily (server, day);
CREATE TABLE IF NOT EXISTS public_ingest_checkpoints (
  server TEXT, source TEXT, cursor_value BIGINT, last_success_at DOUBLE PRECISION,
  PRIMARY KEY (server, source)
);
CREATE TABLE IF NOT EXISTS orgs (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  discord_guild_id BIGINT,
  active INTEGER NOT NULL DEFAULT 1,
  plan TEXT NOT NULL DEFAULT 'pilot',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_discord
  ON orgs (discord_guild_id);
CREATE TABLE IF NOT EXISTS org_entitlements (
  org_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (org_id, scope)
);
CREATE TABLE IF NOT EXISTS account_entitlements (
  account_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  granted_at DOUBLE PRECISION NOT NULL,
  renewed_at DOUBLE PRECISION,
  expires_at TEXT,
  source TEXT,
  PRIMARY KEY (account_id, scope)
);
CREATE INDEX IF NOT EXISTS idx_acct_ent_expiry
  ON account_entitlements (scope, expires_at);
CREATE TABLE IF NOT EXISTS production_chains (
  id SERIAL PRIMARY KEY,
  owner_user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  org_id INTEGER NOT NULL DEFAULT 1,
  UNIQUE(owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_production_chains_owner
  ON production_chains (owner_user_id);
CREATE TABLE IF NOT EXISTS killfeed_settings (
  org_id     INTEGER PRIMARY KEY,
  channel_id TEXT,
  min_fame   BIGINT NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  updated_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS killfeed_watch (
  org_id          INTEGER NOT NULL,
  guild_name      TEXT NOT NULL,
  guild_name_norm TEXT NOT NULL,
  guild_id        TEXT,
  added_at        DOUBLE PRECISION,
  PRIMARY KEY (org_id, guild_name_norm)
);
"""


# --------------------------------------------------- tributo da guild (dual)
# Núcleo web-first do plano Discord (docs/PLANO_DISCORD_GUILD.md §3.2),
# adaptado ao multi-inquilino: org_id INTEGER referencia orgs.id (o snowflake
# do Discord mora em orgs.discord_guild_id p/ o futuro). Timestamps de evento
# são REAL unix (a aritmética do relógio roda em Python, nunca no SQL);
# week_start é texto ISO da segunda-feira, comparado com >= quando preciso.
# Sem FOREIGN KEY declarada, como o resto do store — integridade no código.
_TRIBUTE_SQLITE = """
CREATE TABLE IF NOT EXISTS weekly_assignments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id        INTEGER NOT NULL,
  account_id    INTEGER NOT NULL,
  week_start    TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  qty_target    INTEGER NOT NULL,
  sector        TEXT,
  from_chain_id INTEGER,
  note          TEXT,
  created_at    REAL NOT NULL,
  created_by    INTEGER NOT NULL,
  UNIQUE (org_id, account_id, week_start, item_id)
);
CREATE INDEX IF NOT EXISTS idx_assign_week
  ON weekly_assignments (org_id, week_start);
CREATE INDEX IF NOT EXISTS idx_assign_member
  ON weekly_assignments (org_id, account_id, week_start);
CREATE TABLE IF NOT EXISTS member_reports (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id             INTEGER NOT NULL,
  account_id         INTEGER NOT NULL,
  assignment_id      INTEGER,
  item_id            TEXT NOT NULL,
  qty_reported       INTEGER NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending',
  reported_at        REAL NOT NULL,
  auditor_account_id INTEGER,
  audit_note         TEXT,
  processed_at       REAL,
  discord_message_id INTEGER,
  created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_pending
  ON member_reports (org_id, status, reported_at);
CREATE INDEX IF NOT EXISTS idx_reports_member
  ON member_reports (org_id, account_id, status);
CREATE TABLE IF NOT EXISTS member_clock (
  org_id          INTEGER NOT NULL,
  account_id      INTEGER NOT NULL,
  state           TEXT NOT NULL DEFAULT 'em_dia',
  anchor_ts       REAL NOT NULL,
  paused_ts       REAL,
  clock_days      INTEGER NOT NULL DEFAULT 0,
  tools_revoked   INTEGER NOT NULL DEFAULT 0,
  dismissed_at    REAL,
  reactivated_at  REAL,
  updated_at      REAL NOT NULL,
  PRIMARY KEY (org_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_clock_state
  ON member_clock (org_id, state);
CREATE TABLE IF NOT EXISTS guild_audit_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id            INTEGER NOT NULL,
  actor_account_id  INTEGER,
  action            TEXT NOT NULL,
  target_account_id INTEGER,
  details_json      TEXT,
  created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_audit_time
  ON guild_audit_log (org_id, created_at DESC);
"""

# Postgres: SERIAL nos ids, DOUBLE PRECISION nos timestamps, BIGINT só no
# discord_message_id (snowflake 64-bit estoura int4).
_TRIBUTE_PG = """
CREATE TABLE IF NOT EXISTS weekly_assignments (
  id            SERIAL PRIMARY KEY,
  org_id        INTEGER NOT NULL,
  account_id    INTEGER NOT NULL,
  week_start    TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  qty_target    INTEGER NOT NULL,
  sector        TEXT,
  from_chain_id INTEGER,
  note          TEXT,
  created_at    DOUBLE PRECISION NOT NULL,
  created_by    INTEGER NOT NULL,
  UNIQUE (org_id, account_id, week_start, item_id)
);
CREATE INDEX IF NOT EXISTS idx_assign_week
  ON weekly_assignments (org_id, week_start);
CREATE INDEX IF NOT EXISTS idx_assign_member
  ON weekly_assignments (org_id, account_id, week_start);
CREATE TABLE IF NOT EXISTS member_reports (
  id                 SERIAL PRIMARY KEY,
  org_id             INTEGER NOT NULL,
  account_id         INTEGER NOT NULL,
  assignment_id      INTEGER,
  item_id            TEXT NOT NULL,
  qty_reported       INTEGER NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending',
  reported_at        DOUBLE PRECISION NOT NULL,
  auditor_account_id INTEGER,
  audit_note         TEXT,
  processed_at       DOUBLE PRECISION,
  discord_message_id BIGINT,
  created_at         DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_pending
  ON member_reports (org_id, status, reported_at);
CREATE INDEX IF NOT EXISTS idx_reports_member
  ON member_reports (org_id, account_id, status);
CREATE TABLE IF NOT EXISTS member_clock (
  org_id          INTEGER NOT NULL,
  account_id      INTEGER NOT NULL,
  state           TEXT NOT NULL DEFAULT 'em_dia',
  anchor_ts       DOUBLE PRECISION NOT NULL,
  paused_ts       DOUBLE PRECISION,
  clock_days      INTEGER NOT NULL DEFAULT 0,
  tools_revoked   INTEGER NOT NULL DEFAULT 0,
  dismissed_at    DOUBLE PRECISION,
  reactivated_at  DOUBLE PRECISION,
  updated_at      DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (org_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_clock_state
  ON member_clock (org_id, state);
CREATE TABLE IF NOT EXISTS guild_audit_log (
  id                SERIAL PRIMARY KEY,
  org_id            INTEGER NOT NULL,
  actor_account_id  INTEGER,
  action            TEXT NOT NULL,
  target_account_id INTEGER,
  details_json      TEXT,
  created_at        DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_audit_time
  ON guild_audit_log (org_id, created_at DESC);
"""


def init_tribute_schema(conn) -> None:
    """Cria as tabelas do tributo da guild (idempotente, dual).

    Chamada por init_schema (nuvem) e pelo TributeStore no boot (local/testes),
    no mesmo molde do ChainStore: quem usa garante o próprio schema.
    """
    if getattr(conn, "backend", "sqlite") == "sqlite":
        conn.executescript(_TRIBUTE_SQLITE)
    else:
        for stmt in _TRIBUTE_PG.split(";"):
            if stmt.strip():
                conn.execute(stmt)
    conn.commit()


def _has_column(conn, table: str, col: str) -> bool:
    """True se `table.col` existe no backend ativo."""
    if getattr(conn, "backend", "sqlite") == "postgres":
        cur = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=? AND column_name=?", (table, col))
        return cur.fetchone() is not None
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())


def add_column(conn, table: str, coldef: str) -> None:
    """Adiciona uma coluna de forma idempotente nos dois backends.

    `coldef` é "<nome> <tipo> [DEFAULT ...]". `ADD COLUMN NOT NULL` em tabela
    populada EXIGE `DEFAULT` constante. Verifica a pós-condição e levanta se a
    coluna não existir depois — falhar alto, porque o código novo depende dela
    (nunca o molde `except: pass` cego).
    """
    col = coldef.split()[0]
    if getattr(conn, "backend", "sqlite") == "postgres":
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {coldef}")
            conn.commit()
        except Exception:
            conn.rollback()  # ALTER que falha não envenena a transação seguinte
    else:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            conn.commit()
        except Exception as e:  # só "duplicate column name" é idempotência
            if "duplicate column" not in str(e).lower():
                raise
    if not _has_column(conn, table, col):
        raise RuntimeError(
            f"migração falhou: {table}.{col} não existe após ADD COLUMN")


def ensure_default_org(conn) -> None:
    """Semeia a org nº 1 e seus direitos (idempotente, fora do bootstrap).

    A nuvem do piloto já tem admin -> o bootstrap nunca mais roda; por isso a
    org default NÃO pode depender dele. Força `id=1` explícito e, no Postgres,
    avança a sequência do SERIAL para um INSERT futuro não reusar o id 1.
    """
    now = datetime.now(timezone.utc).timestamp()
    if getattr(conn, "backend", "sqlite") == "postgres":
        conn.execute(
            "INSERT INTO orgs (id, name, plan, active, created_at) "
            "VALUES (1, 'Guilda principal', 'pilot', 1, ?) "
            "ON CONFLICT (id) DO NOTHING", (now,))
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('orgs','id'), "
            "GREATEST((SELECT MAX(id) FROM orgs), 1))")
        for scope in ("operacao", "analitico"):
            conn.execute(
                "INSERT INTO org_entitlements "
                "(org_id, scope, active, expires_at, updated_at) "
                "VALUES (1, ?, 1, NULL, ?) "
                "ON CONFLICT (org_id, scope) DO NOTHING", (scope, now))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO orgs (id, name, plan, active, created_at) "
            "VALUES (1, 'Guilda principal', 'pilot', 1, ?)", (now,))
        for scope in ("operacao", "analitico"):
            conn.execute(
                "INSERT OR IGNORE INTO org_entitlements "
                "(org_id, scope, active, expires_at, updated_at) "
                "VALUES (1, ?, 1, NULL, ?)", (scope, now))
    conn.commit()


def _is_readonly_error(exc) -> bool:
    """True se for erro de Postgres em modo somente-leitura (o que acontece no
    plano free quando o disco enche). Serve p/ o app subir degradado em vez de
    entrar em crash-loop no boot."""
    s = str(exc).lower()
    return "read-only" in s or "read only" in s or "readonlysql" in s


def init_schema(conn):
    """Cria as tabelas do piloto no backend ativo (idempotente).

    Se o Postgres estiver em somente-leitura (disco cheio no free), o DDL falha
    no boot. Em vez de derrubar o processo em loop, o app SOBE em modo
    degradado: as tabelas já existem de um boot são, então as leituras (site,
    /preco, /historico) funcionam; as escritas caem por endpoint até o disco
    ser liberado. Só o erro de read-only é engolido; qualquer outro sobe."""
    try:
        if getattr(conn, "backend", "sqlite") == "sqlite":
            conn.executescript(_SQLITE_SCHEMA)
        else:
            for stmt in _PG_SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
        conn.commit()
        # Migração idempotente para bases que predatam o multi-inquilino: o
        # CREATE ... IF NOT EXISTS acima NÃO altera tabela viva sem org_id.
        add_column(conn, "production_chains", "org_id INTEGER NOT NULL DEFAULT 1")
        add_column(conn, "positions", "org_id INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_production_chains_org "
                     "ON production_chains (org_id, owner_user_id)")
        conn.commit()
        ensure_default_org(conn)
        init_tribute_schema(conn)   # tributo da guild (idempotente, dual)
    except Exception as exc:
        if _is_readonly_error(exc):
            try:
                conn.rollback()
            except Exception:
                pass
            print("[store] init_schema pulado: banco em somente-leitura "
                  "(disco cheio?). App sobe em modo degradado.", flush=True)
            return
        raise

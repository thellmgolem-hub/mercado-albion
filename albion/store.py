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
  sell_price REAL, sell_city TEXT, closed_at REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS sweep_state (
  server TEXT PRIMARY KEY, cursor INTEGER, cycle INTEGER,
  updated_at REAL, last_items INTEGER
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
  note TEXT
);
CREATE TABLE IF NOT EXISTS sweep_state (
  server TEXT PRIMARY KEY, cursor INTEGER, cycle INTEGER,
  updated_at DOUBLE PRECISION, last_items INTEGER
);
"""


def init_schema(conn):
    """Cria as tabelas do piloto no backend ativo (idempotente)."""
    if conn.backend == "sqlite":
        conn.executescript(_SQLITE_SCHEMA)
    else:
        for stmt in _PG_SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
    conn.commit()

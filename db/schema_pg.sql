-- Schema Postgres do PILOTO (Supabase). Cole isto no SQL Editor do Supabase
-- UMA vez (Dashboard > SQL Editor > New query > Run).
--
-- O app também cria estas tabelas sozinho no primeiro boot (store.init_schema),
-- mas rodar aqui garante o esquema antes do primeiro sweep e evita depender de
-- DDL pelo pooler de conexões.
--
-- Espelha albion/store.py (_PG_SCHEMA). Só as tabelas VIVAS do piloto — os
-- dados de killboard/snapshots finos não migram (não cabem no plano gratuito).

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
CREATE INDEX IF NOT EXISTS idx_history_lookup ON history (server, time_scale, ts);

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

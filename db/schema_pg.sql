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
  note TEXT,
  org_id INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sweep_state (
  server TEXT PRIMARY KEY, cursor INTEGER, cycle INTEGER,
  updated_at DOUBLE PRECISION, last_items INTEGER
);

-- Killboard MAGRO: agregado diário de destruição (alimenta Guild + Logística
-- restock). Só o agregado cabe no free; o firehose cru não.
CREATE TABLE IF NOT EXISTS kill_demand_daily (
  server TEXT, day TEXT, item_id TEXT, slot TEXT, quality INTEGER,
  victim_units BIGINT, victim_events BIGINT,
  PRIMARY KEY (server, day, item_id, slot, quality)
);
CREATE INDEX IF NOT EXISTS idx_kill_demand_day ON kill_demand_daily (server, day);
CREATE TABLE IF NOT EXISTS public_ingest_checkpoints (
  server TEXT, source TEXT, cursor_value BIGINT, last_success_at DOUBLE PRECISION,
  PRIMARY KEY (server, source)
);

-- ===== Autenticação (espelha albion/auth.py _AUTH_SCHEMA_PG) ============
CREATE TABLE IF NOT EXISTS auth_accounts (
  id SERIAL PRIMARY KEY,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  display_name TEXT, albion_nick TEXT, discord_nick TEXT,
  role TEXT NOT NULL,
  password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
  password_algo TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  session_version INTEGER NOT NULL DEFAULT 1,
  created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
  last_login_at DOUBLE PRECISION, created_by INTEGER,
  org_id INTEGER NOT NULL DEFAULT 1,
  is_super INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_profiles (
  account_id INTEGER NOT NULL, profile TEXT NOT NULL,
  PRIMARY KEY (account_id, profile)
);
CREATE TABLE IF NOT EXISTS auth_devices (
  id SERIAL PRIMARY KEY,
  account_id INTEGER NOT NULL, token_hash TEXT NOT NULL,
  label TEXT, approved_at DOUBLE PRECISION NOT NULL, last_seen_at DOUBLE PRECISION,
  revoked_at DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_device_active_token
  ON auth_devices (account_id, token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_device_account
  ON auth_devices (account_id, revoked_at);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
  csrf_hash TEXT NOT NULL, session_version INTEGER NOT NULL,
  created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
  idle_expires_at DOUBLE PRECISION NOT NULL, last_seen_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_session_account
  ON auth_sessions (account_id);
CREATE TABLE IF NOT EXISTS auth_login_throttle (
  username_norm TEXT PRIMARY KEY, failures INTEGER NOT NULL,
  locked_until DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_audit (
  id SERIAL PRIMARY KEY,
  actor_account_id INTEGER, target_account_id INTEGER,
  action TEXT NOT NULL, details_json TEXT, created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_time ON auth_audit (created_at DESC);

-- ===== Multi-inquilino (espelha albion/store.py) =======================
-- Organizações (guildas-inquilinas). A org nº 1 é semeada por ensure_default_org.
CREATE TABLE IF NOT EXISTS orgs (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  discord_guild_id BIGINT,
  active INTEGER NOT NULL DEFAULT 1,
  plan TEXT NOT NULL DEFAULT 'pilot',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_discord ON orgs (discord_guild_id);
-- Direito da ORG (operacao/analitico) — renovado por contribuição auditada.
CREATE TABLE IF NOT EXISTS org_entitlements (
  org_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (org_id, scope)
);
-- Direito da CONTA (pedágio do membro).
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

-- Linha de Produção: cadeias salvas por conta (também criada no boot do app).
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
CREATE INDEX IF NOT EXISTS idx_production_chains_org
  ON production_chains (org_id, owner_user_id);


-- ==== Tributo da guild (multi-org) ====
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

-- ==== Discord Fase 1: vinculo + tokens de servico ====
CREATE TABLE IF NOT EXISTS auth_discord_links (
  account_id INTEGER PRIMARY KEY,
  discord_user_id INTEGER NOT NULL UNIQUE,
  linked_at REAL NOT NULL,
  unlinked_at REAL
);

CREATE TABLE IF NOT EXISTS auth_discord_link_codes (
  code TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_service_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  scopes_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_used_at REAL,
  revoked_at REAL
);

CREATE TABLE IF NOT EXISTS auth_discord_links (
  account_id INTEGER PRIMARY KEY,
  discord_user_id BIGINT NOT NULL UNIQUE,
  linked_at DOUBLE PRECISION NOT NULL,
  unlinked_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS auth_discord_link_codes (
  code TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL,
  expires_at DOUBLE PRECISION NOT NULL,
  used_at DOUBLE PRECISION,
  created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_service_tokens (
  id SERIAL PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  scopes_json TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  last_used_at DOUBLE PRECISION,
  revoked_at DOUBLE PRECISION
);

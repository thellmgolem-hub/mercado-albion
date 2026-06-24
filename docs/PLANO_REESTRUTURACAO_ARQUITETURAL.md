# Plano de Reestruturação Arquitetural — Plataforma Albion (multi-inquilino)

> Documento de arquitetura. Alvo: `docs/PLANO_REESTRUTURACAO_ARQUITETURAL.md`.
> Consistente com `docs/PLANO_DISCORD_GUILD.md` (relógio de tributo, token de
> serviço, webhook de interações). Faseamento: **Fase 1 valida com a guilda do
> dono como inquilino nº 1 ANTES de abrir para outras guildas**; cobrança +
> multi-Discord são Fase 2. Toda mudança de schema passa por `tools/check_pg.py`
> com `DATABASE_URL`.

Este plano foi escrito depois de reler o código real. Onde os desenhos
preliminares erravam (contagem de cópias de DDL, `has_admin`, `cutoff_iso`
data-apenas, colisão entre `org_id` e o sentinela local, chave de inquilino do
relógio), as correções dos verificadores estão **aplicadas no texto**, não
deixadas como observação.

---

## 1. Visão e princípios

### 1.1 O que muda

O app nasceu como ferramenta de **uma** guilda: uma instância, um conjunto de
contas, um servidor Discord implícito. A reestruturação o transforma em um
**SaaS multi-inquilino**: várias guildas (organizações) compartilham o mesmo
processo e o mesmo dado de mercado, mas seu dado operacional (cadeias de
produção, metas, relatórios, relógio de tributo, auditoria) é isolado por
inquilino. O dono da plataforma opera acima de todos os inquilinos.

O eixo central é uma distinção que **já existe no código** e que o plano apenas
formaliza: há dado de **mercado** (público, do servidor Américas, varrido por
cron global) e dado **operacional** (de cada guilda). O multi-inquilino carimba
`org_id` somente no segundo.

### 1.2 Os quatro atores

| Ator | Quem é | Como entra | Superfície |
|---|---|---|---|
| **Super-admin (dono)** | Dono da plataforma | conta web, `role='admin'` + flag `is_super` | tudo, cruza inquilinos |
| **Admin-de-guilda** | Líder/tesoureiro/oficial de uma org | conta web, role em `OPERATOR_ROLES`, travado em `org_id` | operação da própria org |
| **Membro** | Coletor/crafter/etc. de uma org | conta web (`member`/`viewer`/`external`) + vínculo Discord | analítica + reporte da própria org |
| **Assinante individual** (Fase 2) | Usuário avulso sem guilda | conta web ou só identidade Discord numa org-sentinela | analítica compartilhada |

Não se inventa o role `auditor`: a auditoria continua sendo `OPERATOR_ROLES`
(conta web) + cargo Discord "Auditoria" (lado bot), exatamente como
`PLANO_DISCORD_GUILD.md` fixou. A verificação de entrega é **sempre humana** —
Albion não expõe baú/inventário/compras via API, então nenhum endpoint de
inventário é inventado em lugar algum deste plano.

### 1.3 As duas superfícies

- **Analítica (compartilhada).** Preço, flip, histórico, scanner, grafo de
  cadeia (cálculo), killboard agregado. Lê só tabelas chaveadas por `server`
  (`prices`, `history`, `gold`, `kill_demand_daily`, …). É o mesmo dado para
  todos; **não recebe `org_id`** e é alimentada pelo cron global
  (`/api/sweep`, `/api/intel-sweep`), que roda **sem contexto de inquilino**.
- **Operacional (escopada por org).** O que é **salvo, atribuído, reportado e
  auditado**: `production_chains`, `positions`, e as tabelas operacionais da
  Fase 2 do Discord. Carrega `org_id` em **toda** query.

O modelo de referência dessa separação já está no código: `/api/prodchain`
(grafo, `app.py:1640`, sem dono) é analítico; `/api/prodchain/chains*`
(`app.py:1685-1714`, escopo por dono) é operacional.

### 1.4 Acesso por direito renovado por contribuição auditada

O acesso operacional não é "logou, pode tudo". É um **direito (entitlement)**
que vive enquanto a contribuição do membro/guilda está em dia. O relógio de 14
dias do `PLANO_DISCORD_GUILD.md` (`member_clock`) é a fonte da verdade do tempo;
a aprovação humana de uma contribuição renova o direito, a expiração o suspende.
Cobrança real (Fase 2) é só mais uma fonte de renovação do direito da org.

---

## 2. Modelo de dados

### 2.1 Classificação confirmada (lida do `store.py`)

**Públicas / compartilhadas — NUNCA recebem `org_id`** (chaveadas por `server`,
varridas por cron global): `prices`, `history`, `gold`, `fetch_log`,
`sweep_state`, `kill_demand_daily`, `public_ingest_checkpoints`. Carimbar
`org_id` nelas quebraria o cron, que não tem inquilino — invariante dura.

**Sensíveis / por inquilino — recebem `org_id`**: `auth_accounts` (a âncora),
`production_chains`, `positions`. As demais `auth_*` herdam o inquilino
transitivamente via `account_id` (não ganham `org_id` próprio na Fase 1; exceção
em §9 para `auth_audit`).

### 2.2 Convenção de tipos (fiel ao `store.py`)

- `org_id` é um **inteiro de inquilino** (`SERIAL`/`int4` no PG, `INTEGER` no
  SQLite). **Não** é snowflake. Só o snowflake do Discord
  (`orgs.discord_guild_id`) vira `BIGINT` no PG.
- Timestamps unix em `REAL` (SQLite) / `DOUBLE PRECISION` (PG).
- **Nada de funções de data no SQL.** Validade de direito comparada em Python
  (ver §3.4). `substr(ts,1,10)` continua permitido.
- Conflitos via `store.upsert`/`upsert_ignore`/`upsert_add`; placeholder `?`
  (traduzido para `%s` por `_PgConn._tr`); IDs via `_insert_id` (lastrowid /
  `RETURNING`); `store.Row` imita `sqlite3.Row`.

### 2.3 A org default e o sentinela local — decisão (resolve a contradição)

Os desenhos preliminares prometiam **duas** coisas incompatíveis: `DEFAULT 1`
(para a base já populada migrar sem backfill) **e** `org=0` no modo local. Em uma
base local já existente, o `ALTER ... DEFAULT 1` backfilla as cadeias para
`org_id=1` e um filtro `WHERE org_id=0` nunca mais as acha — as cadeias locais
somem. **Decisão única:**

> **A org default é `id=1` em todos os backends.** O modo local **não usa um
> `org_id` sentinela separado**: ele resolve para **`org_id=1` também**.
> `_actor_org(request)` devolve `1` quando `not config.AUTH_REQUIRED`. Isso casa
> com o `DEFAULT 1` do `ALTER` e com o backfill — nada some.

O sentinela do modo local continua sendo o **owner** (`_chain_owner` devolve
`0`), não a org. Assim `_chain_owner=0` (single-user) e `org_id=1` (org única
local) convivem: as cadeias locais ficam em `(org_id=1, owner_user_id=0)` e são
lidas por `WHERE org_id=1 AND owner_user_id=0`. O `is_super` do modo local (§4)
dá o bypass cross-org quando necessário, sem depender de `org_id` nulo.

### 2.4 DDL SQLite (novas/alteradas)

```sql
-- Tabela-raiz do inquilino. PG: id SERIAL; discord_guild_id BIGINT;
--   created_at DOUBLE PRECISION.
CREATE TABLE IF NOT EXISTS orgs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT NOT NULL,
  discord_guild_id INTEGER,            -- snowflake (BIGINT no PG); NULL até vincular
  active           INTEGER NOT NULL DEFAULT 1,
  plan             TEXT NOT NULL DEFAULT 'pilot',
  created_at       REAL NOT NULL       -- unix UTC; NUNCA date('now')
);
-- UNIQUE só vale para valores não-NULL nos dois bancos:
CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_discord
  ON orgs (discord_guild_id);

-- Direito da ORG inteira (separado do direito da CONTA — ver §3). PG: org_id
--   INTEGER; updated_at DOUBLE PRECISION.
CREATE TABLE IF NOT EXISTS org_entitlements (
  org_id      INTEGER NOT NULL,        -- FK lógica orgs.id (sem FK declarada)
  scope       TEXT NOT NULL,           -- 'analitico' | 'operacao'
  active      INTEGER NOT NULL DEFAULT 1,
  expires_at  TEXT,                    -- ISO YYYY-MM-DD; NULL = sem prazo
  updated_at  REAL NOT NULL,
  PRIMARY KEY (org_id, scope)
);

-- Direito da CONTA (pedágio do membro). PK por (account_id, scope).
CREATE TABLE IF NOT EXISTS account_entitlements (
  account_id  INTEGER NOT NULL,        -- FK lógica auth_accounts.id
  scope       TEXT NOT NULL,           -- 'analitico' | 'operacao'
  active      INTEGER NOT NULL DEFAULT 1,
  granted_at  REAL NOT NULL,
  renewed_at  REAL,                    -- última baixa do auditor que renovou
  expires_at  TEXT,                    -- ISO YYYY-MM-DD; NULL = sem prazo
  source      TEXT,                    -- 'bootstrap' | 'pedagio' | 'plano_guilda'
  PRIMARY KEY (account_id, scope)
);
CREATE INDEX IF NOT EXISTS idx_acct_ent_expiry
  ON account_entitlements (scope, expires_at);

-- auth_accounts ganha a âncora do inquilino. Na base nova nasce na coluna;
-- na base viva, migração idempotente (ver §8). NUNCA NOT NULL sem DEFAULT.
ALTER TABLE auth_accounts ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE auth_accounts ADD COLUMN is_super INTEGER NOT NULL DEFAULT 0;

-- production_chains: org_id ADICIONAL ao owner (recriação do zero usa o UNIQUE
-- composto; base viva usa ALTER + índice). As CINCO cópias do DDL idênticas.
CREATE TABLE IF NOT EXISTS production_chains (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id        INTEGER NOT NULL DEFAULT 1,   -- FK lógica orgs.id
  owner_user_id INTEGER NOT NULL,             -- FK lógica auth_accounts.id (mantido)
  name          TEXT NOT NULL,
  payload       TEXT NOT NULL,
  updated_at    REAL NOT NULL,
  UNIQUE(org_id, owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_production_chains_org
  ON production_chains (org_id, owner_user_id);

-- positions: coluna entra DESDE JÁ mas SEM consumidor escopado na Fase 1
-- (ver §9, open question por-conta vs por-guild). NÃO criar falso isolamento.
ALTER TABLE positions ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1;
```

### 2.5 Notas Postgres (fiéis ao `_PG_SCHEMA`/`_AUTH_SCHEMA_PG`)

- `orgs.id SERIAL PRIMARY KEY`; `orgs.discord_guild_id BIGINT`;
  `created_at DOUBLE PRECISION`. `org_id`/`account_id` permanecem `INTEGER`
  (casam com `SERIAL`/`int4`).
- `production_chains.id SERIAL`; `updated_at DOUBLE PRECISION`;
  `UNIQUE(org_id, owner_user_id, name)`; mesmo índice.
- `expires_at` dos entitlements é **`TEXT` ISO** nos dois bancos (comparado em
  Python — §3.4), nunca `timestamp`/`now()`.
- DDL sobe **statement-a-statement** no PG (`split(';')`), nunca
  `executescript`. Cada `CREATE`/`ALTER` termina em `;`.
- O bloco SQLite e o bloco PG de `orgs`, `org_entitlements` e
  `account_entitlements` têm de ser **escritos por extenso nos dois dialetos**
  (não só "notas"), senão `_init_schema` não os cria no PG.
- Pooler 6543 em modo transação: `store.connect` já faz
  `prepare_threshold=None` — não adicionar prepared statements; leituras de
  entitlement usam `_read()` (rollback ao sair).

### 2.6 As cópias de DDL — inventário correto (corrige a contagem)

Verificado por leitura, **não** "3 lugares":

- **`production_chains` — CINCO statements `CREATE`:** `store.py:335` (SQLite),
  `store.py:396` (PG), `prodchain.py:401` (`_CHAINS_SQLITE`), `prodchain.py:413`
  (`_CHAINS_PG`), `db/schema_pg.sql:114`. **Todas** ganham `org_id` idênticas; o
  `CREATE TABLE IF NOT EXISTS` mascara divergência (quem cria primeiro vence).
  *Recomendação para matar a classe de bug:* fazer `prodchain.ChainStore`
  **importar** o literal de `store.py` (uma fonte única) e remover
  `_CHAINS_SQLITE`/`_CHAINS_PG`. Se não fizer o refactor, editar as cinco.
- **`auth_accounts` — TRÊS cópias:** `auth.py:169` (SQLite), `auth.py:221` (PG),
  `db/schema_pg.sql:66`. Todas ganham `org_id`/`is_super`.
- **`positions` — QUATRO caminhos de criação:** `store.py:314` (SQLite),
  `store.py:374` (PG), `db/schema_pg.sql:39`, **e `client.py:126`** (caminho
  LOCAL). A 4ª cópia (`client.py`) é a que cria `positions` no modo local; o
  `ALTER` idempotente do `store` precisa rodar sobre a mesma tabela, ou
  `client.py` ganha a coluna também. Como `positions` é só CLI na Fase 1 (sem
  consumidor escopado), o caminho mais seguro é alterar `client.py:126` **e**
  não expor leitura escopada ainda (§9).

---

## 3. Direitos (entitlements) e relógio de contribuição

### 3.1 Dois eixos de direito

- **Direito analítico** (`scope='analitico'`): passe da superfície compartilhada
  de leitura. Toda conta ativa de um inquilino o tem. Na Fase 1 nasce com
  `expires_at=NULL` (sem pedágio) até o dono ligar o relógio.
- **Direito operacional** (`scope='operacao'`): passe das tabelas operacionais
  da **própria org**. Mora em **`org_entitlements`** (é da organização, não de
  uma conta — isso evita o `account_id` sentinela colidindo com o `id:0` do modo
  local). Na Fase 1 a org nº 1 nasce `operacao active=1 expires_at=NULL` por
  bootstrap: **porta pronta, trava desligada**.

O direito do **membro** (pedágio individual) mora em `account_entitlements`. O
direito da **org** (operação/cobrança) mora em `org_entitlements`. Os dois são
lidos pelo gate (§5).

### 3.2 O relógio é o `member_clock`, NÃO uma máquina nova

O relógio de verdade é o `member_clock` do `PLANO_DISCORD_GUILD.md`
(`anchor_ts`/`paused_ts`, estados `em_dia|pendente|atrasado|desligado`,
transições já fechadas pelo dono). **Não se reescreve nada.** Os entitlements são
a **projeção consultável** do estado do relógio para o gate.

> **Correção de chave (bloqueante).** `member_clock` continua com
> `PRIMARY KEY (guild_id, account_id)`, onde `guild_id` é o **snowflake** do
> Discord (`BIGINT` no PG), exatamente como o plano Discord define. **Não** se
> rechaveia `member_clock` por `org_id`. A ponte entre os dois mundos é
> `orgs.discord_guild_id`: `renew`/`expire` traduzem `guild_id → org_id` por esse
> índice. Misturar `guild_id` (snowflake/BIGINT) com `org_id` (int4) é o erro que
> este plano proíbe explicitamente.

### 3.3 Acoplamento relógio ↔ direito (uma função, na mesma transação)

A integração é mínima e atômica:

- **Aprovação** (`approve_report`, Fase 2 do Discord): depois de zerar o relógio
  (`anchor_ts=now`, `paused_ts=NULL`, `state='em_dia'`), **na MESMA `_tx`** chama
  `renew_entitlement`: estende `expires_at` e marca `active=1`. Pedágio do membro
  renova `account_entitlements('operacao')` (e `'analitico'` se ligado); baixa de
  plano de guilda renova `org_entitlements('operacao')` da org inteira.
- **Rejeição** (`reject_report`): o relógio **corre** (`paused_ts=NULL`, mesmo
  `anchor_ts`). **Não renova** — o direito vai vencer.
- **Tick** (`/api/guild/clock-tick`): no mesmo ramo que vira `member_clock` para
  `desligado` aos 14d, chama `expire_entitlement` (`active=0`). **Só** nesse ramo,
  nunca para quem está `pendente` (`paused_ts != NULL`) — senão revoga direito de
  quem acabou de reportar.

**Atomicidade (correção):** `renew_entitlement`/`expire_entitlement` recebem a
conexão `con` já aberta pela `_tx` do `approve_report`/do tick (no molde de
`AuthManager._audit(con, ...)`, que já recebe `con`). **Não** se chama outro
`Store` que faz `commit` por conta própria — `ContributionLedger`/`GuildStore` e
`AuthManager` compartilham a **mesma** `con`+`lock` justamente para permitir uma
transação única.

### 3.4 Como o gate lê a validade (corrige `cutoff_iso` data-apenas)

`store.cutoff_iso(0)` devolve **só a data** (`YYYY-MM-DD`) — granularidade de
dia. Comparar um `expires_at` ISO completo com isso é incoerente. **Decisão:**

> `expires_at` dos entitlements é **`TEXT` ISO de data (`YYYY-MM-DD`)**, com
> granularidade de **dia**. O gate compara `expires_at >= store.cutoff_iso(0)`
> em Python (ou `active=1 AND (expires_at IS NULL OR expires_at >= hoje)`).
> **Nunca** `date()`/`now()` no SQL. A cobrança expira à meia-noite UTC do dia —
> granularidade de dia basta para tributo/assinatura.

### 3.5 Reavaliação por requisição

O entitlement é anexado ao contexto em `_account_dict` (como `profiles`/`ips`
hoje), e `authenticate()` o carrega. Mas a sessão dura até 7 dias
(`SESSION_DAYS`), então um direito pode **vencer no meio da sessão**. Por isso o
gate `_require_entitlement` **recompara `expires_at` com `cutoff_iso(0)` na hora**
da requisição, não confia só no snapshot do login. Na Fase 1 isso é barato
(comparação em memória do que já veio no ctx); o congelamento da Fase 2 morde
sessões abertas.

---

## 4. Hierarquia de papéis

### 4.1 Três estratos, sem inventar `auditor`

| Estrato | Papel | Escopo |
|---|---|---|
| Super-admin | `role='admin'` **+ `is_super=1`** | todos os inquilinos |
| Admin-de-guilda | `OPERATOR_ROLES` (`admin`, `guild_leader`, `economic_officer`, `treasurer`) | travado em `org_id` |
| Membro | `member`/`viewer`/`external` | a própria org, conforme entitlement |

### 4.2 Por que `is_super` em vez de um role `super_admin` (correção bloqueante)

`auth.py:347` `has_admin()` faz `SELECT ... WHERE role='admin' AND active=1`, e o
middleware `_auth_guard` (`app.py:291`) levanta **503 `bootstrap_required`** para
toda rota `/api/` se `has_admin()` for falso. `manage_accounts.py` usa o mesmo
predicado. Se o dono virasse `role='super_admin'`, na Fase 1 (ele é a única
conta) **não sobraria nenhum `role='admin'`** → a plataforma inteira responde
503 e `_maybe_bootstrap_admin` tentaria re-bootstrap.

> **Decisão:** **não** se cria um role novo. O super-poder do dono é uma
> **coluna/flag `is_super`** em `auth_accounts` (DEFAULT 0). O dono permanece
> `role='admin'`, então `has_admin()`, a guarda `last_admin` e o gate de
> bootstrap continuam válidos **sem reescrita**. `SUPER_ADMIN` = `is_super=1`.

Conjunto auxiliar no código (`auth.py`, ao lado de `OPERATOR_ROLES`/`ADMIN_ROLES`):
nenhuma mudança em `ROLES`. A checagem de super vira
`account.get('is_super')` — não `role in {...}`.

### 4.3 Mudanças em `auth.py`

- `_account_select()` (`auth.py:381-385`) **e** as `keys` de `_account_dict`
  (`auth.py:367-369`) ganham `org_id` **e** `is_super`, **na mesma posição**.
  Correção do bug de `dict(zip)` posicional: adicionar ambos como **últimas**
  colunas do `SELECT` **e** últimos itens de `keys`. Teste obrigatório:
  `account['role']=='admin'` e `account['org_id']==1` após o bootstrap (pega
  corrupção de posição, em que `role`/`active` viriam de outra coluna).
- `create_account`/`update_account` recebem o **org do actor** (via
  `SELECT org_id WHERE id=actor_id`, já que a assinatura atual só tem
  `actor_id`): admin-de-guilda carimba o próprio `org_id` e ignora qualquer
  `org_id` do corpo; super-admin pode informar `org_id` de destino.
- **Emissão de super-poder restrita:** atribuir `is_super=1` exige actor com
  `is_super=1`. Sem isso, um admin comum escalaria para cross-org.
- **Guardas `last_admin`/`self_lockout`** (`auth.py:474-485`): a contagem de
  "último admin ativo" passa a ser **por org** (`AND org_id=?`) na Fase 2; na
  Fase 1 (1 org) o comportamento atual vale. Invariante adicional: **≥1 conta
  `is_super=1` ativa no sistema** — rebaixar/desativar o último super levanta
  `last_super_admin`. Documentar a pendência da contagem por-org.

### 4.4 Mudanças em `_require_role` (`app.py:274`)

Assinatura: `_require_role(request, allowed, *, org=None)`.

- `not config.AUTH_REQUIRED` → admin sintético, agora
  `{"id":0,"username":"local","role":"admin","org_id":1,"is_super":1}` (org 1, e
  super para o bypass local nunca quebrar). **Antes** de qualquer checagem de org.
- Com auth: além de `role in allowed`, se `org is not None` exige
  `account.get('is_super')` **OU** `int(account['org_id'])==int(org)`, senão
  `AuthError('Operação fora do seu inquilino.','org_forbidden',403)`.
- Ramo de serviço (Fase 2): `ctx.get('service_label')` → 403 `service_forbidden`
  (o bot nunca toca a console). Já previsto no plano Discord.

`_actor_org(request)` (novo, espelha `_chain_owner` em `app.py:1677`): local → 1;
com auth → `request.state.auth['account']['org_id']`.

---

## 5. Isolamento das duas superfícies e enforcement anti-vazamento

### 5.1 Invariante

> Vazamento cross-inquilino só pode acontecer se uma query **operacional** rodar
> **sem `org_id=?`**. A defesa por construção remove a possibilidade de escrever
> a query crua no caller.

### 5.2 O helper único que injeta `org_id` (centralização)

Toda query operacional passa por um `Store` (`ChainStore`, e o `GuildStore` da
Fase 2) que recebe `org` como **1º argumento posicional obrigatório** e injeta
`org_id=?` em **todo** `SELECT/UPDATE/DELETE`. O caller nunca monta SQL.
Esquecer o `org` é **`TypeError` em tempo de chamada**, não um `SELECT`
silenciosamente amplo. O `org_id` **nunca** vem do corpo da requisição — vem do
ctx autenticado, via `_actor_org(request)`. Um cliente não consegue forjá-lo.

`prodchain.ChainStore` (`prodchain.py:475-531`) é o template: hoje filtra
`WHERE owner_user_id=?`; passa a `WHERE org_id=? AND owner_user_id=?`. As
assinaturas ganham `org` antes de `owner` (`list(org, owner)`,
`get(org, owner, cid)`, `save(org, owner, ...)`, `delete(org, owner, cid)`); o
upsert-por-nome (`prodchain.py:513-515`) e o `UNIQUE` viram
`(org_id, owner_user_id, name)`. Os quatro endpoints (`app.py:1685-1714`) passam
`_actor_org(request)` e `_chain_owner(request)`. Contrato HTTP inalterado.

> **Regra adversarial:** um `cid` válido **nunca** é confiado sem `(org, owner)`
> no `WHERE`. `get`/`delete`/`save(cid=...)` filtram `WHERE id=? AND org_id=? AND
> owner_user_id=?` → `cid` de outra org devolve **404**, não o recurso.

### 5.3 Gate por handler

Dois helpers, **antes** do `_require_role`:

- `_require_entitlement(request, 'analitico')`: superfície compartilhada de
  leitura. Barato; qualquer conta ativa. **Não** inclui grafo/cadeia — só o
  mercado verdadeiramente compartilhado (`prices`/`history`/`gold`/
  `kill_demand_daily`), que não tem dono.
- `_require_entitlement(request, 'operacao')`: superfície operacional. Exige
  `_actor_org(request) == org do recurso` **E** `org_entitlements('operacao')`
  ativo e não-vencido (lido por `cutoff_iso(0)`/`>=`, §3.4). Modo local libera.

> **Correção (cadeia-grafo não é compartilhada).** As cadeias salvas
> (`/api/prodchain/chains*`) são **operacionais** (por org + owner), não
> analíticas. Só o **grafo de cálculo** (`/api/prodchain`, `app.py:1640`, lê
> preço) é analítico. O gate `'analitico'` **não** libera cadeias salvas.

### 5.4 Rede de segurança e teste

`SHARED_ANALYTIC_PATHS` e `ORG_SCOPED_PATHS` declarados no nível do módulo (ao
lado de `PUBLIC_AUTH_PATHS`, `app.py:190`). São **rede de segurança/teste**, não
a única defesa. O enforcement real é o gate por handler + o `Store`
centralizado. Como `check_pg.py` valida **dialeto, não isolamento**, é
**obrigatório** um teste de isolamento dedicado (§8) antes de a Fase 1 ser
considerada pronta:

1. Cria org A e org B; cadeia em cada uma.
2. Conta de B recebe **404** em `GET/PUT/DELETE` do recurso de A (e no
   upsert-por-nome `prodchain.py:513`).
3. Modo local (`AUTH` off): `_require_role`/`_chain_owner`/`_actor_org` devolvem
   `org=1`/`owner=0` e **nada quebra** (regressão mais provável).
4. (Fase 2) `audit_log` de B não retorna eventos de A (§9).
5. `last_super_admin` global e `last_admin` por-org barram o auto-lockout.

### 5.5 Cron global como prova viva do isolamento

`/api/sweep` e `/api/intel-sweep` são isentos de sessão (`PUBLIC_AUTH_PATHS`),
protegidos por `ALBION_SWEEP_TOKEN` (`_check_sweep_token`, fail-closed em prod,
`compare_digest` por bytes), e tocam **só** as tabelas compartilhadas. Eles rodam
sem contexto de inquilino — se uma tabela compartilhada ganhasse `org_id` por
engano, o cron quebraria. Esse acoplamento é proposital: é a garantia de que o
mercado não vira por-inquilino.

---

## 6. Roteamento multi-inquilino do Discord + espaço do assinante individual

> Fase 2. O schema entra na Fase 1; o roteamento opera com 1 linha
> (servidor do dono → org nº 1).

### 6.1 Um webhook, resolução em duas etapas

Há **um** `POST /api/discord/interactions`, compartilhado por todos os servidores
Discord. Ele **não** entra no guard de sessão (não há cookie/IP/CSRF num POST do
Discord); fica em `PUBLIC_AUTH_PATHS` com porteiro próprio: **Ed25519** com
`DISCORD_PUBLIC_KEY` validada **antes** de ler o corpo (plano Discord §2.2). A
assinatura autentica o **canal**, não o inquilino. O inquilino vem de dentro do
corpo já validado (`data.guild_id`), resolvido assim:

1. **`discord_guild_id` (snowflake) → `org_id`** via `orgs.discord_guild_id`
   (`UNIQUE`, N:1 — vários servers podem apontar para a mesma org). Sem org ativa:
   `defer` + "servidor não registrado" e **para**; nunca cria org implícita.
2. **`(discord_user_id, org_id) → account_id`** via `auth_discord_links`. O par
   da etapa 1 desambigua qual conta usar. **Asserção obrigatória** (selo do
   IDOR): `account.org_id == org_resolvido_na_etapa_1` antes de qualquer escrita.

> **`auth_discord_links.discord_user_id` é `UNIQUE` GLOBAL na Fase 1** (como o
> plano Discord define hoje). Trocar para `UNIQUE(discord_user_id, org_id)` exige
> **rebuild de tabela** no SQLite (não há `ADD CONSTRAINT`/`ALTER` de UNIQUE) —
> isso é **Fase 2**, com rebuild explícito dual (criar nova, copiar, dropar,
> renomear), **não** um "ADD COLUMN idempotente". Na Fase 1, vínculo global basta
> (1 org).

O `org_id` resolvido é o escopo de **toda** escrita operacional do comando. A
resolução fica **num único lugar** (o roteador), não espalhada por handler.

### 6.2 Token de serviço do bot, por org

O webhook **não** usa `X-Service-Token` (é Ed25519). Quando o **bot chama a API
de volta** (pós-`defer`, agregação), usa `X-Service-Token`
(`authenticate_service`, plano Discord §4.2), sem IP/CSRF. Para o token não
escrever na org errada, `auth_service_tokens` ganha `org_id` (NULL = token
"platform" do dono, cross-org; preenchido = token de **uma** org).
`authenticate_service` devolve `service_org_id`; o `GuildStore` filtra por ele.
`authenticate_service` continua devolvendo `account: None`, então `_require_role`
barra o bot da console (`service_forbidden`).

### 6.3 Tabelas operacionais do Discord precisam de `org_id` (pré-condição)

As tabelas do `PLANO_DISCORD_GUILD.md` (`weekly_assignments`, `member_reports`,
`member_clock`, `guild_audit_log`) são chaveadas por `guild_id` (**snowflake**).
O gate operacional compara `org do recurso`, que precisa de uma coluna `org_id`.
**Decisão (pré-condição da Fase 2):** essas tabelas ganham `org_id INTEGER` como
coluna de partição (mantendo `guild_id` só para a REST de cargo), **ou** o
`GuildStore` deriva o org via `JOIN auth_accounts` por `account_id`. `member_clock`
mantém sua PK por `(guild_id, account_id)` — o `org_id` entra como coluna
adicional, não como nova PK.

### 6.4 Espaço do assinante individual

Vive na **superfície analítica**: servidor público de análise (ou DM, onde
`guild_id` é nulo) mapeado a uma **org-sentinela "analítica pública"** (sem
operação, só `org_entitlements('analitico')`). Comandos operacionais num contexto
sem org operacional retornam "este comando exige uma guilda registrada" —
explícito, nunca `org=None → 0` implícito. A org pública usa um `id` dedicado,
**distinto de 1** (que é a guilda do dono), para não confundir os significados de
id baixo.

---

## 7. Cobrança/planos e livro-razão de contribuição com procedência (Fase 2)

> O **gancho de dado** entra na Fase 1 (tabelas + `org_id`); a **cobrança ativa**
> liga na Fase 2 por flag (`ALBION_BILLING_ENFORCED`), sem migração nova.

### 7.1 Dois portões

- **Portão 1 — pedágio do membro.** É o relógio de 14d do `member_clock`. O efeito
  de "desligado" passa a derrubar também o `account_entitlements` do membro, não
  só o cargo Discord. Renova por baixa humana (§3.3).
- **Portão 2 — plano da guilda.** Trato adiantado: "líder paga X (prata/ouro/
  serviço, ex.: erguer uma casa T8 nas ilhas do dono em 30d)" e isso renova
  `org_entitlements('operacao')` da **org inteira** por um período (`expires_at`).
  Vencido, a operação congela; a leitura analítica compartilhada continua
  liberada (dado público da AODP, custo zero — decisão do dono em §9). A ativação
  do plano é, ela própria, uma baixa de auditor sobre a contribuição-entregável —
  fecha o ciclo com a mesma máquina humana, **sem inventar gateway de pagamento**.

### 7.2 Livro-razão com procedência

Tabela append-only `contribution_ledger` (DDL dual; cópia em `store.py` **e** no
módulo `albion/contribution.py`, no molde de `production_chains`, mantidas
idênticas; **fixar a ordem de criação** para o `IF NOT EXISTS` de um não mascarar
o tipo do outro). Cada linha é uma **alegação** (não o fato):

```sql
CREATE TABLE IF NOT EXISTS contribution_ledger (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- PG: SERIAL
  org_id       INTEGER NOT NULL DEFAULT 1,
  account_id   INTEGER NOT NULL,         -- quem reportou (membro ou líder)
  target_scope TEXT NOT NULL DEFAULT 'member',  -- 'member' (pedágio) | 'org' (plano)
  item_id      TEXT,                     -- NULL p/ serviço/prata
  qty          INTEGER NOT NULL DEFAULT 0,
  origin       TEXT NOT NULL,            -- comprado|coletado|plantado|refinado|
                                         --   fabricado|servico|prata|ouro
  provider     TEXT,                     -- de quem veio (nick/account)
  sector       TEXT,                     -- raw|refine|craft|farm|buy (prodchain._sector)
  deal_kind    TEXT,                     -- entregável do líder, ex 'casa_t8_30d'
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
  reported_at  REAL NOT NULL,           -- PAUSA o member_clock
  auditor_account_id INTEGER,
  audit_note   TEXT,
  processed_at REAL,                    -- baixa humana: aprova RENOVA / rejeita CORRE
  from_chain_id INTEGER,                -- FK lógica production_chains.id
  created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contrib_pending
  ON contribution_ledger (org_id, status, reported_at);
CREATE INDEX IF NOT EXISTS idx_contrib_member
  ON contribution_ledger (account_id, status);
```

`origin`/`provider` são a **procedência declarada** pelo membro; `sector` (de
`prodchain._sector`) é só pista para casar com a meta da cadeia. O auditor confere
as duas no jogo.

### 7.3 Baixa do auditor (sempre humana, atômica)

`approve(org, ledger_id, auditor_id, note)`: `status='approved'`,
`processed_at=now`, e **na mesma `_tx`** (con compartilhada) renova o entitlement
do alvo (`account_entitlements` se `target_scope='member'`; `org_entitlements` se
`'org'`), zera o `member_clock` e audita em `guild_audit_log`. `reject` faz o
relógio correr e **não** renova. **A máquina nunca aprova** — o estado `pending`
existe porque ela não sabe se a entrega é verdadeira (Albion não expõe baú).

Gate da baixa: `OPERATOR_ROLES` (console) + cargo Discord "Auditoria" (bot).
`renew_entitlement`/`expire_entitlement` só são alcançáveis de **dentro** de um
`approve_report`/tick já autorizado — sem endpoint público próprio (senão vira
escalada: renovar o próprio direito).

---

## 8. Migração e faseamento

### 8.1 Princípio

`org_id` é camada **adicional**, nunca rename. A âncora é `auth_accounts.org_id`;
o dado sensível herda dela. `CREATE TABLE IF NOT EXISTS` **não** altera tabela
viva — Supabase do piloto e SQLite local já têm `auth_accounts`/
`production_chains`/`positions` **sem** `org_id`. É preciso migração explícita.

### 8.2 Helper de migração dual (corrige o `except` cego)

`store.add_column(conn, table, coldef)`:

- **SQLite:** `ALTER TABLE {table} ADD COLUMN {coldef}`; captura **só** o erro
  "duplicate column name" (idempotência), repropaga o resto.
- **PG:** `ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {coldef}` (Supabase
  suporta); no `except`, **rollback** da conexão (espelha o `_tx` do
  `AuthManager`) para um `ALTER` que falhe não envenenar a transação seguinte no
  pooler.

> **Não** usar o molde `_revoke_legacy_devices` (`except Exception: pass` cego)
> para coluna crítica. Após o `ALTER`, **verificar a pós-condição** (PRAGMA
> `table_info` no SQLite / `information_schema.columns` no PG); se a coluna não
> existir, **abortar o boot com mensagem clara** — falhar alto, porque o código
> novo depende da coluna. `ADD COLUMN NOT NULL` em tabela populada **exige**
> `DEFAULT` constante (`DEFAULT 1`), nunca `NOT NULL` sem default.

### 8.3 Semeadura da org nº 1 — desacoplada do bootstrap (correção bloqueante)

`bootstrap_admin` (`auth.py:393`) **aborta** se já existe qualquer conta, e
`_maybe_bootstrap_admin` (`app.py:59`) faz early-return se `has_admin()`. O
Supabase do piloto **já tem admin** → o bootstrap **nunca mais roda**. Se a
semeadura de `orgs(id=1)`/`org_entitlements` dependesse do bootstrap, ela
**nunca** aconteceria na nuvem viva, e `_require_entitlement('operacao')`
referenciaria uma org inexistente.

> **Decisão:** semeadura em passo idempotente próprio — `store.ensure_default_org`
> (ou dentro de `init_schema`, após `add_column`), chamado em **toda** subida:
> `INSERT` explícito de `orgs(id=1, name, plan='pilot', active=1, created_at)` com
> `ON CONFLICT DO NOTHING`; `org_entitlements(1,'operacao',active=1,
> expires_at=NULL)` e `(1,'analitico',1,NULL)` idem. **Forçar `id=1` explícito**
> (não deixar o `SERIAL` escolher) e, no PG, `setval` da sequência de `orgs` para
> `MAX(id)`, para um `INSERT` futuro não consumir o id 1. O bootstrap apenas
> garante que o admin nasça `org_id=1, is_super=1` quando criar uma base nova.

### 8.4 Fase 1 — guilda do dono = inquilino nº 1 (validar PRIMEIRO)

Passo a passo (cada passo valida com `check_pg.py`):

1. **`store.py`:** tabelas `orgs`, `org_entitlements`, `account_entitlements` nos
   dois dialetos; helper `add_column`; `ensure_default_org`.
2. **`production_chains`:** `org_id` + `UNIQUE(org_id,owner_user_id,name)` +
   índice nas **cinco** cópias (ou fonte única importada). `add_column` para a
   base viva.
3. **`positions`:** `org_id` via `add_column` nos quatro caminhos
   (incl. `client.py:126`); **sem** consumidor escopado (§9).
4. **`auth.py`:** `org_id`/`is_super` em `auth_accounts` (três cópias);
   `add_column` na migração; `org_id`+`is_super` em `_account_select`/keys (mesma
   posição, teste de posição). `account_entitlements`/`org_entitlements` lidos no
   ctx.
5. **`auth.py`:** `bootstrap_admin` carimba `org_id=1, is_super=1`; emissão de
   `is_super` restrita a actor super; guarda `last_super_admin`; documentar
   `last_admin` por-org na Fase 2.
6. **`app.py`:** `_actor_org`, `_require_entitlement` (no-op para org 1 e local),
   `_require_role(..., org=)`; `SHARED_ANALYTIC_PATHS`/`ORG_SCOPED_PATHS`.
7. **`ChainStore`:** `org` 1º arg posicional, `WHERE org_id=? AND owner_user_id=?`
   em tudo; os 4 endpoints passam `_actor_org`+`_chain_owner` e chamam
   `_require_entitlement('operacao')`. Gatear **escrita** (`save`/`delete`) por
   `OPERATOR_ROLES` — senão um `member` edita a cadeia da guilda inteira.
8. **Validar:** `tools/check_pg.py` com `DATABASE_URL` (schema sobe; `/api/
   prodchain/chains` 200; `auth.login` exercita o PG sem erro de dialeto) **+ o
   teste de isolamento de §5.4** (que `check_pg.py` não cobre).

Nesta fase há **1 org e 1 super**: o gate `(role E org_id)` é sempre satisfeito;
`org_entitlements('operacao')` é `active=1/expires_at=NULL` (no-op permissivo). O
relógio do membro já valida com a guilda do dono.

### 8.5 Fase 2 — abrir para outras guildas

- `org_entitlements('operacao')` vira gate **ativo** (org nova nasce
  `inactive` até a baixa do entregável do líder); `ALBION_BILLING_ENFORCED=1`.
- `discord_guilds`/`orgs.discord_guild_id` resolvem N:1 server→org; multi-Discord
  com `DISCORD_PUBLIC_KEY`/bot-token/role-id **por org**; o `clock-tick` deixa de
  usar role-id/token global e usa o da org de cada membro (remover cargo na
  guilda errada é bug grave).
- `org_id` nas tabelas operacionais do Discord (§6.3); `auth_service_tokens.org_id`.
- **Decidir `username`/throttle ANTES de popular muitas orgs:** `username_norm` é
  `UNIQUE` **global** hoje (`auth.py:172`), e `auth_login_throttle` é PK por
  `username_norm` (global). Enquanto global, dois inquilinos não podem reusar
  nick **e** o lockout é cross-tenant (um trava o nick do outro). Se for por-org,
  migrar para `UNIQUE(org_id, username_norm)` e PK `(org_id, username_norm)` —
  **migração de PK com base populada**, cara; por isso a decisão vem antes da
  abertura, com plano de backfill. Recomendação Fase 1: manter global,
  documentar o acoplamento.
- `last_admin` passa a contar admins **dentro da org**.

---

## 9. Riscos honestos

- **Baú não-legível.** A verificação de entrega/contribuição é **sempre humana**.
  O estado `pending` existe porque a máquina não sabe. Risco: pressão para
  "automatizar" a baixa inventando um endpoint de inventário — **proibido**, a
  API do Albion não o expõe. O único efeito automático tolerado é a **suspensão
  por tempo** (tick), que é decisão de tempo, não de verdade de entrega; isso
  precisa do aval explícito do dono por atravessar a fronteira "a máquina não
  decide".
- **Cross-tenant (IDOR).** Um handler operacional novo pode nascer sem
  `org_id=?`. Mitigação por construção (§5.2) + teste de isolamento obrigatório
  (§5.4). `check_pg.py` valida **dialeto, não isolamento** — sem o teste, o filtro
  é só promessa.
- **Confusão `guild_id` (snowflake) × `org_id` (int4).** Duas larguras, dois
  namespaces. `member_clock` permanece por `guild_id`; entitlements/`org_id` por
  int4; ponte via `orgs.discord_guild_id`. Reusar a coluna errada quebra o PG e o
  isolamento.
- **`auth_audit` vaza na Fase 2.** `audit_log` (`auth.py:745-759`) faz `SELECT`
  sem filtro de org e `auth_audit` não tem `org_id`. Na Fase 2, admin-de-guilda de
  B veria eventos (ações, IPs em `details_json`) de A. Correção: `auth_audit`
  ganha `org_id` (NOT NULL DEFAULT 1) e `audit_log` filtra pelo org do actor
  (super-admin vê todos); `/api/admin/audit` passa `org=_actor_org(request)`. **A
  Fase 1 com 1 org não vaza; a abertura sem isso vaza.**
- **`positions` é schema morto se ganhar `org_id` sem consumidor.** Hoje é
  single-user sem dono e CLI-only (sem endpoint). Adicionar `org_id` sem caminho
  de leitura escopado cria **falso** isolamento. Decisão: adicionar a coluna
  (DEFAULT 1) para não migrar PK depois, mas **não** expor leitura escopada na
  Fase 1; **negar** qualquer rota de `positions` em multi-org até decidir
  por-conta vs por-guild. *Open question:* `positions` é portfólio por-conta ou
  por-guild?
- **Complexidade / over-gating.** Ligar `_require_entitlement('operacao')` como
  gate **ativo** cedo demais trancaria o próprio inquilino nº 1. Por isso a Fase 1
  é permissiva (`expires_at=NULL`, `BILLING_ENFORCED=0`); a trava morde só na
  Fase 2. Risco simétrico: a Fase 1 carregar gate/`Store` por org **sem** teste
  de isolamento — o enforcement vira promessa.
- **Modo local.** `org=1`/`owner=0`/`is_super=1` no caminho `not AUTH_REQUIRED`,
  **antes** de qualquer checagem; `ensure_default_org` semeia a org 1 também
  localmente; `_require_entitlement` libera. Sem isso, o single-user sem login
  regride (cadeias somem, ou o gate operacional nega). Coberto pelo item (3) do
  teste de isolamento.
- **Cópias de DDL.** Cinco de `production_chains`, três de `auth_accounts`, quatro
  caminhos de `positions`. `IF NOT EXISTS` mascara divergência (quem cria primeiro
  vence). Editar todas idênticas, ou centralizar `production_chains` numa fonte
  única importada — a opção que elimina a classe de bug.

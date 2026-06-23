# Plano Técnico — Discord + Gestão de Tributo e Metas da Guild

Plataforma: Mercado Albion (FastAPI + `albion/store.py` dual SQLite/Postgres + `albion/auth.py` por sessão/IP + `albion/prodchain.py`). Branch base: `pilot-sem-coleta`. Hospedagem-alvo: Render free + Supabase + cron externo (cron-job.org), como em `DEPLOY.md`.

Este documento reúne cinco desenhos (modelo de dados, identidade/auth de bot, máquina de estados do relógio, comandos/permissões, gráficos/Linha de Produção) num plano único e acionável. Onde os desenhos divergiam do código real, prevalece o código. Os pontos corrigidos estão marcados ao longo do texto.

---

## 1. Visão e princípios

O dono decidiu separar quem opera de quem executa. O **site continua sendo o console do admin**: criar contas, atribuir metas, montar a Linha de Produção, ver a fila de auditoria, ler relatórios. Membro comum não abre o site. Ele vive no **Discord**, onde reporta entrega, consulta preço e vê sua própria pendência.

Três papéis de pessoa, um papel de máquina:

- **Admin/operador** (web): usa a console autenticada por sessão+IP, como hoje. Mapeia para os roles reais de `auth.py`: `OPERATOR_ROLES = {admin, guild_leader, economic_officer, treasurer}` e `ADMIN_ROLES = {admin}`.
- **Auditor** (Discord e/ou web): o dono fixou que auditor é "cargo alto + cargo auditor". Como `auth.py` **não tem** role `auditor`, o gate de auditoria no backend usa `OPERATOR_ROLES` para a conta web do auditor; no Discord, o gate adicional é por cargo do servidor (um cargo "Auditoria"). Os dois caminhos chamam o mesmo endpoint.
- **Membro** (Discord): cargo "Ferramentas" no servidor. Esse cargo é a chave de acesso ao bot e às ferramentas; perdê-lo é o desligamento.
- **Bot** (máquina): autentica na API por **token de serviço**, não por sessão de humano (detalhe na seção 4), para não colidir com o vínculo por IP.

O que roda sozinho e o que espera o clique do auditor é a distinção que organiza o resto:

| Automático (cron/bot/máquina de estados) | Humano (clique do auditor/admin) |
|---|---|
| Avançar o relógio de cada membro a cada dia | Aprovar ou rejeitar um reporte |
| Pausar o relógio quando chega um reporte | Atribuir metas semanais (incl. a partir da cadeia) |
| Desligar ferramentas aos 14 dias (remover cargo) | Verificar no jogo se o baú recebeu o que foi reportado |
| Devolver o cargo quando uma aprovação chega | Resetar relógio manualmente (caso excepcional) |
| Renderizar gráficos e responder consultas | Decidir distribuição de carga entre membros |

A regra que não pode ser violada: **a máquina nunca decide se uma entrega é verdadeira**. O baú do Albion não é legível por API. O bot registra a alegação do membro e congela o relógio; só o olho do auditor no jogo converte alegação em fato. Isso é limite de produto, não falha de engenharia (seção 9).

Decisões já fechadas pelo dono, que o plano respeita literalmente:

- Reporte **pausa** o relógio.
- Aprovação **zera** o relógio.
- Rejeição faz o relógio **voltar a correr**.
- Metas são **semanais** (semana ISO, segunda a domingo).
- **14 dias** sem entrega aprovada **desliga as ferramentas** (remove o cargo Discord). Não expulsa, não bane. O membro fica na comunidade e no chat.

---

## 2. Arquitetura geral

### 2.1 Componentes

```
                         cron-job.org
                    (tick sweep + tick relógio + keep-alive)
                              |
                              v
  Discord  --interação-->  FastAPI (app.py em Render free)  <--SQL-->  Supabase Postgres
  (membros)  HTTP/Ed25519    |  _auth_guard (sessão+IP)                 (store.py dual)
                             |  /api/discord/interactions
  Navegador --sessão-->      |  /api/guild/* (token de serviço/cron)
  (admins)   cookie+CSRF     |  /api/prodchain (já existe)
                             |
                             +--> Discord REST (adicionar/remover cargo)
```

Tudo vive **dentro do mesmo processo FastAPI** que já existe. Não há serviço novo de bot 24h. O endpoint `/api/discord/interactions` é mais uma rota de `app.py`. As tabelas novas entram pelo mesmo `store.init_schema` (ou por um `GuildStore` que cria o próprio schema, espelhando `ChainStore`).

### 2.2 Interactions HTTP, não gateway

O bot do Discord pode operar de dois modos. O **gateway** mantém um WebSocket vivo o tempo todo; exige um processo sempre ligado. O **Interactions HTTP** recebe cada slash-command como um POST assinado e responde na mesma requisição.

Para Render free, gateway é inviável: o serviço hiberna após 15 min de inatividade e mataria a conexão. Interactions HTTP casa com a hospedagem porque o webhook só precisa estar de pé quando há comando, e o cron de keep-alive (seção 2.4) cobre o cold start. Esse é o mesmo raciocínio que já justifica o sweep por cron em vez de coletor sempre-ligado, descrito no CLAUDE.md.

A consequência prática é a verificação de assinatura. Todo POST em `/api/discord/interactions` chega com `X-Signature-Ed25519` e `X-Signature-Timestamp`. O backend valida com a chave pública do app (`DISCORD_PUBLIC_KEY`) antes de qualquer leitura de corpo. Sem assinatura válida, 401. Um `PING` (type 1) responde `{"type": 1}`; um comando (type 2) entra no roteador.

### 2.3 Fluxo de dados de um reporte

```
1. Membro: /reportar item:T5_ORE qtd:50
2. Discord -> POST /api/discord/interactions  (Ed25519)
3. app.py valida assinatura; resolve discord_user_id -> account_id (auth_discord_links)
4. Handler chama GuildStore.report_delivery():
     INSERT member_reports (status='pending')
     UPDATE member_clock  (status='paused', clock_paused_at=now)
5. Resposta imediata (<3s): "Reporte registrado, aguardando auditoria."
6. (opcional) bot posta no canal de auditores que há pendência
7. Auditor (Discord ou web): /aprovar ou clique na console
8. app.py: GuildStore.approve_report() zera relógio; se cargo removido, REST add_role
```

### 2.4 Hospedagem e cron

Três tarefas agendadas no cron-job.org, todas protegidas por token e isentas de sessão, pelo mesmo padrão do `/api/sweep`:

- **Tick do relógio** (`/api/guild/clock-tick`, 1×/dia logo após 00:00 UTC): reavalia todos os membros, faz transições de tempo e dispara remoção de cargo aos 14 dias. Idempotente.
- **Sweep de mercado** (`/api/sweep`): já existe, não muda.
- **Keep-alive** (qualquer GET barato, a cada ~14 min): mantém o Render acordado para o webhook responder dentro do limite de 3s do Discord.

O gate de token reusa exatamente a lógica de `_check_sweep_token`: fail-closed em Postgres se o token não estiver definido, comparação em tempo constante por bytes. Um token próprio (`ALBION_GUILD_TOKEN`) separa o relógio do sweep, para poder rotacionar um sem o outro.

---

## 3. Modelo de dados

### 3.1 Decisões de modelagem (correções sobre os desenhos)

Os cinco desenhos propuseram nomes de tabela conflitantes (`guild_members`/`deliveries`/`delivery_clock` num; `tribute_*` noutro; `weekly_assignments`/`member_reports`/`member_clock` noutro). O plano consolida em **um** conjunto, escolhido pela aderência ao código real:

- **A identidade do membro reusa `auth_accounts`.** Não se cria tabela paralela de membro. A coluna `discord_nick` já existe em `auth_accounts` (decorativa). O vínculo forte Discord↔conta vai numa tabela própria, `auth_discord_links`, porque o snowflake do Discord precisa de chave única e de timestamp de vínculo/desvínculo.
- **`account_id INTEGER`**, não `BIGINT`, para casar com `auth_accounts.id` (SERIAL/AUTOINCREMENT). Só `discord_user_id` é `BIGINT`/`INTEGER` largo (snowflake 64-bit).
- **`guild_id` é o `server_id` do Discord** (`BIGINT`/`TEXT`), guardado para multi-servidor futuro; o piloto opera com um único servidor.
- **Timestamps**: `REAL` no SQLite, `DOUBLE PRECISION` no Postgres, UTC unix, exatamente como `auth_accounts`/`production_chains`. Datas de semana e de corte vão como **texto ISO** comparado com `>=`, nunca via `date('now')` — a regra de ouro do `store.py`.
- **IDs autogerados** via o padrão `_insert_id` (lastrowid no SQLite, `RETURNING id` no Postgres), idêntico a `AuthManager` e `ChainStore`.

### 3.2 DDL — SQLite

```sql
-- Vínculo Discord <-> conta (1:1 forte). Reusa auth_accounts; não duplica membro.
CREATE TABLE IF NOT EXISTS auth_discord_links (
  account_id      INTEGER PRIMARY KEY,            -- FK auth_accounts.id
  discord_user_id INTEGER NOT NULL UNIQUE,        -- snowflake (largo)
  guild_id        INTEGER,                        -- server_id do Discord
  linked_at       REAL NOT NULL,
  unlinked_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_discord_links_user
  ON auth_discord_links (discord_user_id);

-- Códigos de vínculo (gerados na web, consumidos no Discord; 15 min, 1 uso)
CREATE TABLE IF NOT EXISTS auth_discord_link_codes (
  code        TEXT PRIMARY KEY,                   -- 8 chars, base32 sem ambíguos
  account_id  INTEGER NOT NULL,                   -- FK auth_accounts.id
  expires_at  REAL NOT NULL,
  used_at     REAL,
  created_at  REAL NOT NULL
);

-- Tokens de serviço (bot -> API). Não é sessão; não tem IP nem CSRF.
CREATE TABLE IF NOT EXISTS auth_service_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  label        TEXT NOT NULL UNIQUE,              -- "discord_bot"
  token_hash   TEXT NOT NULL UNIQUE,              -- sha-256 do segredo
  scopes_json  TEXT NOT NULL,                     -- '["search","guild_report",...]'
  created_at   REAL NOT NULL,
  last_used_at REAL,
  revoked_at   REAL
);

-- Metas semanais por membro (uma linha por item atribuído)
CREATE TABLE IF NOT EXISTS weekly_assignments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id      INTEGER NOT NULL,
  account_id    INTEGER NOT NULL,                 -- FK auth_accounts.id
  week_start    TEXT NOT NULL,                    -- ISO da segunda, ex "2026-06-22"
  item_id       TEXT NOT NULL,
  qty_target    INTEGER NOT NULL,
  sector        TEXT,                             -- raw|refine|craft|farm|buy (de prodchain._sector)
  from_chain_id INTEGER,                          -- FK production_chains.id (origem, se houver)
  note          TEXT,
  created_at    REAL NOT NULL,
  created_by    INTEGER NOT NULL,                 -- FK auth_accounts.id (admin)
  UNIQUE (guild_id, account_id, week_start, item_id)
);
CREATE INDEX IF NOT EXISTS idx_assign_week
  ON weekly_assignments (guild_id, week_start);
CREATE INDEX IF NOT EXISTS idx_assign_member
  ON weekly_assignments (account_id, week_start);

-- Reportes de entrega (alegação do membro; auditável)
CREATE TABLE IF NOT EXISTS member_reports (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id           INTEGER NOT NULL,
  account_id         INTEGER NOT NULL,
  assignment_id      INTEGER,                     -- FK weekly_assignments.id (NULL=bônus)
  item_id            TEXT NOT NULL,
  qty_reported       INTEGER NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
  reported_at        REAL NOT NULL,               -- PAUSA o relógio aqui
  auditor_account_id INTEGER,                      -- FK auth_accounts.id
  audit_note         TEXT,
  processed_at       REAL,                         -- aprovação (ZERA) ou rejeição (CORRE)
  discord_message_id INTEGER,
  created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_pending
  ON member_reports (guild_id, status, reported_at);
CREATE INDEX IF NOT EXISTS idx_reports_member
  ON member_reports (account_id, status);

-- Relógio de 14 dias: 1 linha viva por membro (estado corrente da máquina)
CREATE TABLE IF NOT EXISTS member_clock (
  guild_id          INTEGER NOT NULL,
  account_id        INTEGER NOT NULL,
  state             TEXT NOT NULL DEFAULT 'em_dia',  -- em_dia|pendente|atrasado|desligado
  anchor_ts         REAL NOT NULL,                   -- de onde o relógio conta (última aprovação/entrada)
  paused_ts         REAL,                            -- preenchido => relógio congelado
  clock_days        INTEGER NOT NULL DEFAULT 0,      -- dias materializados pelo tick (p/ consulta barata)
  tools_revoked     INTEGER NOT NULL DEFAULT 0,      -- 1 => cargo removido
  dismissed_at      REAL,
  reactivated_at    REAL,
  updated_at        REAL NOT NULL,
  PRIMARY KEY (guild_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_clock_state
  ON member_clock (guild_id, state);

-- Pista de auditoria das ações da guild (separada de auth_audit)
CREATE TABLE IF NOT EXISTS guild_audit_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id          INTEGER NOT NULL,
  actor_account_id  INTEGER,                       -- NULL => bot/cron
  action            TEXT NOT NULL,                 -- assign|report|approve|reject|revoke|restore|reset
  target_account_id INTEGER,
  details_json      TEXT,
  created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_audit_time
  ON guild_audit_log (guild_id, created_at DESC);
```

### 3.3 Notas SQLite ↔ Postgres

A tradução segue o que `_AUTH_SCHEMA_PG` e `_PG_SCHEMA` já fazem. Resumo das trocas:

- `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`.
- `REAL` → `DOUBLE PRECISION` em todos os timestamps.
- `discord_user_id INTEGER` (SQLite guarda inteiro de 64 bits sem dor) → `BIGINT` no Postgres, porque o snowflake estoura `int4`. Mesma escolha para `guild_id` e `discord_message_id`.
- `account_id`, `assignment_id`, `from_chain_id`, `*_account_id` permanecem `INTEGER` nos dois bancos (referenciam `SERIAL`, que é `int4`).
- Sem `FOREIGN KEY` declarada no DDL: o `store.py` não as usa (o Postgres do Supabase em modo transação e o SQLite com `PRAGMA` ligado divergem no enforcement). A integridade fica no código, como já ocorre em `production_chains` (que carrega `owner_user_id` solto). Os comentários `-- FK` documentam a intenção.
- `UNIQUE(...)` e índices têm a mesma sintaxe nos dois; mantêm-se idênticos.
- Escritas usam `store.upsert`/`upsert_ignore` quando há conflito de chave. O reporte e a aprovação não somam, então não usam `upsert_add` (esse fica reservado a agregados como `kill_demand_daily`).

O DDL Postgres entra no mesmo formato statement-a-statement de `init_schema` (Postgres não tem `executescript`):

```python
for stmt in _GUILD_SCHEMA_PG.split(";"):
    if stmt.strip():
        conn.execute(stmt)
conn.commit()
```

### 3.4 Justificativa do desenho do relógio

O ponto sutil é onde guardar o tempo. Materializar `clock_days` por linha num cron diário é barato de consultar (o `/meu-status` lê um inteiro pronto), mas o número de verdade vem de `anchor_ts` e `paused_ts`, calculados em Python:

```python
def clock_days(now: float, anchor_ts: float, paused_ts: float | None) -> int:
    ref = paused_ts if paused_ts is not None else now   # pausado => congela
    return max(0, int((ref - anchor_ts) // 86400))
```

`anchor_ts` é o marco zero: data de entrada do membro, ou a última aprovação. `paused_ts` preenchido significa relógio congelado no instante do reporte. A aprovação reescreve `anchor_ts = now` e limpa `paused_ts`. A rejeição apenas limpa `paused_ts`, então o relógio retoma a contagem a partir do mesmo `anchor_ts` antigo, exatamente como o dono pediu. `clock_days` é só um espelho para leitura; a fonte da verdade são os dois timestamps.

---

## 4. Identidade e auth de bot

### 4.1 Vínculo Discord ↔ conta: código gerado, não OAuth2

O admin cria a conta web do membro (já é o fluxo de hoje). O vínculo com o Discord usa um **código de uso único**, não o OAuth2 do Discord. A razão é operacional: OAuth2 exige client secret, callback HTTPS público e configuração que pesa num Render de domínio aleatório, e ainda assim entrega só o `discord_id` que o código já entrega. O código é instantâneo e sobrevive a IP interno de LAN.

Fluxo:

1. Na console, o admin abre a conta do membro e clica "Gerar código Discord". O backend gera 8 caracteres (base32 sem caracteres ambíguos), grava em `auth_discord_link_codes` com `expires_at = now + 900` (15 min) e devolve o código.
2. O membro digita `/vincular ABC23XYZ` no Discord.
3. O bot chama o endpoint de vínculo. O backend valida: código existe, não expirou, não foi usado, e o `discord_user_id` ainda não está vinculado a outra conta. Em sucesso, grava `auth_discord_links` e marca o código `used_at = now`.
4. O bot adiciona o cargo "Ferramentas" e inicializa `member_clock` com `state='em_dia'`, `anchor_ts=now`.

Métodos novos em `AuthManager`, seguindo o estilo existente (`_tx`, `_read`, `_insert_id`):

```python
def gen_link_code(self, account_id: int) -> str           # 8 chars, 15 min, 1 uso
def link_discord(self, code: str, discord_user_id: int)   # valida + grava + audita
def get_account_by_discord_id(self, discord_user_id: int) # -> account_id | None
```

### 4.2 Token de serviço para o bot

O bot não pode usar cookie de sessão. A sessão de humano é amarrada a IP (`MAX_IPS_PER_ACCOUNT = 2`) e a CSRF; um bot atrás de Render compartilharia IP com o proxy e estouraria o limite, além de não ter token CSRF. A saída é um caminho de autenticação paralelo: **token de serviço** em header.

A tabela `auth_service_tokens` guarda o hash do segredo e uma lista de escopos. O segredo aparece **uma vez** quando criado por CLI (`manage_accounts.py create-service-token --label discord_bot --scopes ...`) e nunca mais. Formato `svc_<base64url>`. Sem expiração; revoga-se por label.

Métodos novos em `AuthManager`:

```python
def create_service_token(self, label, scopes) -> str   # devolve o segredo (uma vez)
def authenticate_service(self, token, path) -> dict     # valida + checa escopo do path
def revoke_service_token(self, label) -> bool
```

`authenticate_service` devolve um contexto com forma diferente do de humano: `{"service_label": ..., "service_scopes": [...], "account": None}`. A ausência de `account` é o que o gate de papéis usa para barrar o bot da console (seção 4.4).

### 4.3 Onde o middleware muda

`_auth_guard` hoje só conhece cookie. A adaptação é mínima e ordenada: tenta sessão; se não houver cookie mas houver header de serviço, tenta serviço; se nenhum, erro.

```python
# dentro de _auth_guard, no ramo protected and not public:
session = request.cookies.get(config.AUTH_SESSION_COOKIE)
service = request.headers.get("X-Service-Token")
if session:
    ctx = auth_manager.authenticate(session, ip=_client_ip(request))
    # ... fluxo atual: CSRF, must_change_password, etc. ...
elif service:
    ctx = auth_manager.authenticate_service(service, path)   # sem IP, sem CSRF
else:
    raise AuthError("Autenticacao ausente.", "auth_missing", 401)
request.state.auth = ctx
```

Os endpoints de Discord ficam sob esse guard como qualquer `/api/*`. Os endpoints tocados por cron (`/api/guild/clock-tick`) entram em `PUBLIC_AUTH_PATHS`, isentos do guard e protegidos pelo token de cron, copiando a isenção que `/api/sweep` já tem.

### 4.4 Gate de papéis no site

`_require_role` precisa de uma linha a mais: um contexto de serviço nunca tem `account`, então cai no `forbidden` atual sem mudança. Para deixar explícito e evitar `KeyError`:

```python
def _require_role(request, allowed):
    if not config.AUTH_REQUIRED:
        return {"id": 0, "username": "local", "role": "admin"}
    ctx = getattr(request.state, "auth", {})
    if ctx.get("service_label"):          # bot nunca acessa console de role
        raise AuthError("Servicos nao acessam o painel.", "service_forbidden", 403)
    account = ctx.get("account")
    if not account or account.get("role") not in allowed:
        raise AuthError("Voce nao tem permissao para esta operacao.", "forbidden", 403)
    return account
```

Resultado: `/api/admin/*` e `/api/prodchain/chains*` exigem humano com role; os endpoints de leitura para o bot (busca, preço, status) exigem o escopo correto do token; os endpoints de auditoria aceitam **ou** o humano com `OPERATOR_ROLES` **ou** o token de serviço com escopo `guild_audit` (o gate do cargo Discord roda no lado do bot, antes de chamar).

---

## 5. Máquina de estados do relógio e automação de cargos

### 5.1 Estados

Quatro estados em `member_clock.state`:

| Estado | Relógio | Cargo "Ferramentas" | Significado |
|---|---|---|---|
| `em_dia` | conta (0–13 dias) | tem | cumpriu a última meta; tudo normal |
| `pendente` | **congelado** | tem | reportou; espera o auditor |
| `atrasado` | conta (1–13 dias) | tem | passou da semana sem entrega aprovada |
| `desligado` | congelado em 14+ | **removido** | estourou 14 dias; segue na comunidade |

### 5.2 Tabela de transições

| Estado atual | Evento | Estado novo | Efeito no relógio | Efeito no cargo |
|---|---|---|---|---|
| `em_dia` | membro reporta | `pendente` | `paused_ts = now` | — |
| `em_dia` | tick: passou a semana sem aprovação | `atrasado` | continua de `anchor_ts` | — |
| `pendente` | auditor **aprova** | `em_dia` | `anchor_ts = now`, `paused_ts = NULL` | — |
| `pendente` | auditor **rejeita** | `atrasado` | `paused_ts = NULL` (retoma) | — |
| `atrasado` | membro reporta | `pendente` | `paused_ts = now` | — |
| `atrasado` | tick: `clock_days >= 14` | `desligado` | congela | **remove cargo** |
| `desligado` | auditor **aprova** entrega | `em_dia` | `anchor_ts = now`, `paused_ts = NULL` | **devolve cargo** |

Observações que fecham os casos de borda:

- **Reporte de última hora.** Membro reporta a 13d23h; o tick roda 1h depois. Como o reporte já moveu o estado para `pendente`, o tick pula quem está `pendente`. O reporte "venceu" o relógio. Sem corrida, porque a transição de reporte e a leitura do tick passam pela mesma `member_clock` sob `lock`/transação.
- **Aprovação após desligado.** O dono escolheu que aprovar reativa. De `desligado`, uma aprovação volta a `em_dia` e devolve o cargo. O membro que ficou de fora volta com uma entrega aceita.
- **Membro saiu do servidor.** A chamada REST de remoção de cargo devolve 404. O backend registra o erro na resposta do tick, mas grava o estado no banco mesmo assim. Se a pessoa voltar, o estado a espera.

### 5.3 O job do cron

`POST /api/guild/clock-tick`, protegido por `ALBION_GUILD_TOKEN` (mesmo gate de bytes do sweep), 1×/dia logo após 00:00 UTC. Idempotente: reprocessar não muda resultado.

```python
@app.api_route("/api/guild/clock-tick", methods=["GET", "POST"])
def guild_clock_tick(token: str = ""):
    _check_guild_token(token)                 # fail-closed em PG, compare_digest por bytes
    now = time.time()
    res = {"para_atrasado": 0, "desligados": 0, "erros": []}
    with aodp.db_lock:
        rows = aodp.db.execute(
            "SELECT guild_id, account_id, state, anchor_ts, paused_ts "
            "FROM member_clock WHERE state IN ('em_dia','atrasado')").fetchall()
        for r in rows:
            if r["paused_ts"] is not None:    # pendente disfarçado: nunca avança
                continue
            days = clock_days(now, r["anchor_ts"], None)
            if r["state"] == "em_dia" and days >= 7:
                _set_state(r, "atrasado", days, now); res["para_atrasado"] += 1
            elif r["state"] == "atrasado" and days >= 14:
                _set_state(r, "desligado", days, now, tools_revoked=1)
                try:
                    discord_remove_role(r["guild_id"], _discord_id(r["account_id"]),
                                        config.DISCORD_TOOLS_ROLE_ID)
                except Exception as e:
                    res["erros"].append({"account_id": r["account_id"], "err": str(e)[:120]})
                res["desligados"] += 1
        aodp.db.commit()
    return res
```

O corte de 7 dias para `em_dia → atrasado` materializa a "semana": passada a semana sem aprovação, o relógio dos 14 já está correndo de fato (o `anchor_ts` é o mesmo), e o estado só reflete que a janela semanal fechou. A remoção de cargo usa a REST do Discord com `Authorization: Bot <token>`, `PUT`/`DELETE` em `/guilds/{guild}/members/{user}/roles/{role}`, idempotente (204 mesmo se já tinha/já não tinha o cargo).

### 5.4 Sincronia banco ↔ Discord

A regra: **o banco é a verdade do estado; o Discord é um espelho**. A operação grava primeiro no banco (transação fecha), depois chama a REST. Se a REST falhar, o estado já está correto e um próximo tick (ou a próxima aprovação) reconcilia o cargo. Nunca o contrário, para não deixar o cargo dizer uma coisa e o banco outra após uma falha de rede.

---

## 6. Comandos do bot e permissões

### 6.1 Cargos do servidor → gate

| Cargo Discord | Papel | Gate no backend |
|---|---|---|
| "Ferramentas" | membro | vínculo ativo em `auth_discord_links` + escopo do token |
| "Auditoria" | auditor | cargo Discord no lado do bot + conta `OPERATOR_ROLES` no endpoint |
| "Organização" | admin/operador | cargo Discord + conta `OPERATOR_ROLES`/`ADMIN_ROLES` |
| (consulta pública) | leitura | só comandos de consulta, sem escrita |

O cargo do Discord é o primeiro filtro (o handler lê `data.member.roles`); o role da conta web vinculada é o segundo, no endpoint. Os dois precisam bater para escrita. Para consulta de preço, basta "Ferramentas".

### 6.2 Comandos e mapeamento

| Comando | Papel | Endpoint | Escopo token | Resposta |
|---|---|---|---|---|
| `/vincular <codigo>` | qualquer | `POST /api/discord/link` | `link` | imediata |
| `/reportar <item> <qtd> [nota]` | membro | `POST /api/guild/report` | `guild_report` | imediata |
| `/bonus <item> <qtd>` | membro | `POST /api/guild/report` (`assignment_id=NULL`) | `guild_report` | imediata |
| `/meu-status` | membro | `GET /api/guild/member-status` | `guild_read` | imediata |
| `/minhas-metas` | membro | `GET /api/guild/assignments` | `guild_read` | imediata |
| `/preco <item> [cidade]` | membro | `GET /api/discord/price` | `search` | defer (gráfico) |
| `/buscar <termo>` | membro | `GET /api/discord/search` | `search` | imediata |
| `/cadeia <item> [qtd]` | membro | `GET /api/discord/prodchain` | `search` | defer (PNG) |
| `/fila` | auditor | `GET /api/guild/pending` | `guild_audit` | imediata |
| `/aprovar <id> [nota]` | auditor | `POST /api/guild/approve` | `guild_audit` | imediata |
| `/rejeitar <id> <motivo>` | auditor | `POST /api/guild/reject` | `guild_audit` | imediata |
| `/atribuir <@membro> <item> <qtd>` | admin | `POST /api/guild/assign` | `guild_admin` | imediata |
| `/relatorio-semana` | admin | `GET /api/guild/weekly-report` | `guild_admin` | defer (PNG) |
| `/resetar-relogio <@membro> <motivo>` | admin | `POST /api/guild/reset-clock` | `guild_admin` | imediata |

### 6.3 Limite de 3 segundos

O Discord exige ACK em 3s. Comandos rápidos respondem `type: 4` (mensagem imediata). Comandos que rendem PNG ou agregação (`/preco`, `/cadeia`, `/relatorio-semana`) respondem `type: 5` (deferred) na hora e, terminado o trabalho, postam o resultado no webhook `POST /webhooks/{app_id}/{interaction_token}`. O defer dá até ~15 min, o que absorve o cold start do Render.

### 6.4 Endpoints novos no backend

Os endpoints de escrita da guild reusam `GuildStore` (espelho de `ChainStore`): `report_delivery`, `approve_report`, `reject_report`, `create_assignment`, `reset_clock`, cada um abrindo `_tx`, gravando e auditando em `guild_audit_log`. A aprovação e a rejeição carregam a transição da seção 5 e, em aprovação que sai de `desligado`, sinalizam ao chamador (bot) que precisa devolver o cargo.

---

## 7. Gráficos no servidor e ligação com a Linha de Produção

### 7.1 Gráficos viáveis no Render free

Matplotlib gera PNG em ~50–120 ms por gráfico, dentro do orçamento de 0,5 vCPU. Pillow é o plano B para imagem de texto puro; se ambos faltarem, o bot cai em embed textual. Um módulo `albion/discord_graphs.py` encapsula isso e nunca levanta exceção para fora: devolve `(png_bytes, None)` ou `(None, texto_fallback)`.

| Gráfico | Origem dos dados | Tamanho |
|---|---|---|
| Preço 7/30d (linha + VWAP) | tabela `history` (`time_scale=24`) | 800×400 |
| Volume 7d (barras) | `history.item_count` | 600×300 |
| Demanda destruída por item da cadeia | `kill_demand_daily` (slot != Inventory) | 700×500 |
| Progresso semanal da guild | `weekly_assignments` + `member_reports` | 1000×600 |

A leitura de `history` segue a regra do `store.py`: corte por `store.cutoff_iso(7)` comparado com `ts >= ?`, sem função de data no SQL. Preços passam por `microstructure.clean_price_rows` antes de virar gráfico, para zerar ordens-isca, como o resto das análises.

### 7.2 Atribuir metas a partir das folhas da cadeia

Esta é a ponte com a Linha de Produção. O admin já monta e salva a cadeia (`ChainStore`, tabela `production_chains`). O endpoint novo transforma essa cadeia salva em metas semanais por membro.

```python
class AssignFromChainBody(BaseModel):
    chain_id: int
    guild_id: int
    week_start: str | None = None                 # default: segunda desta semana (ISO, Python)
    factors: dict[int, int]                       # {account_id: multiplicador}

@app.post("/api/guild/assign-from-chain")
def assign_from_chain(body: AssignFromChainBody, request: Request):
    actor = _require_role(request, OPERATOR_ROLES)
    chain = chain_store.get(actor["id"], body.chain_id)   # escopado por dono
    if not chain:
        raise HTTPException(404, "Cadeia nao encontrada")
    graph = chain["payload"]                              # {roots, nodes, ...}
    week = body.week_start or _week_start_iso()
    created = []
    for account_id, factor in body.factors.items():
        targets = {r: factor for r in graph.get("roots", [])}
        res = _prodchain.solve(graph, targets)            # propagação real
        for item_id, info in res["nodes"].items():
            if info["leaf"]:                              # folha = comprar, não atribui
                continue
            sector = graph["nodes"][item_id].get("sector")
            aid = guild_store.create_assignment(
                body.guild_id, week, account_id, item_id,
                qty=info["buy_qty"], sector=sector,
                from_chain_id=body.chain_id, created_by=actor["id"])
            created.append(aid)
    return {"ok": True, "week_start": week, "assignments": len(created)}
```

O critério de "folha" vem direto de `solve()`: `nodes[item]["leaf"]` é `True` quando `_is_buy` decide que o nó se compra (bruto, sem receita, ou marcado `mode='buy'`). Esses nós não viram meta de fabricação; o que vira meta é o que tem `leaf=False`, com a quantidade já propagada em `buy_qty` (teto inteiro da demanda) e o setor de `_sector` (`raw|refine|craft|farm|buy`) gravado para a UI agrupar por tipo de trabalho. A venda das raízes na cadeia usa só `ROYAL_CITIES`; o Mercado Negro não entra, como já documentado para a Linha de Produção.

Há uma decisão de produto aqui que o desenho original tocou de leve: quem recebe a meta de uma **folha bruta** (minério, couro) é o coletor, mas `solve()` marca bruto como `leaf` e não o atribui. Para atribuir coleta de bruto, a UI de distribuição precisa oferecer explicitamente os nós `sector='raw'`/`'farm'` como metas opcionais, fora do laço acima. O plano deixa isso como escolha do admin na tela, não automático, porque misturar "comprar a folha" com "mandar alguém coletar a folha" é exatamente a decisão humana que o sistema não deve presumir.

---

## 8. Fases de implementação

Cada fase entrega algo testável e roda no SQLite local (auth off via `tools/run_local.py`) antes de tocar a nuvem.

**Fase 0 — Esquema e store.** Adicionar as seis tabelas (`auth_discord_links`, `auth_discord_link_codes`, `auth_service_tokens`, `weekly_assignments`, `member_reports`, `member_clock`, `guild_audit_log`) em DDL dual. Criar `GuildStore` espelhando `ChainStore`. Rodar `tools/check_pg.py` com `DATABASE_URL` para validar o dialeto. Teste: schema sobe nos dois backends, inserts e selects passam.

**Fase 1 — Identidade e token de serviço.** `gen_link_code`, `link_discord`, `get_account_by_discord_id`, `create_service_token`, `authenticate_service`, `revoke_service_token` em `AuthManager`. CLI em `manage_accounts.py`. Adaptar `_auth_guard` e `_require_role`. Teste: token de serviço entra em endpoint de leitura e é barrado em `/api/admin/*`; sessão de humano segue intacta.

**Fase 2 — Webhook e ping.** Rota `/api/discord/interactions` com verificação Ed25519, respondendo `PING` e um `/vincular` que fecha o ciclo de vínculo. Teste: payload de exemplo do Discord valida; assinatura adulterada dá 401; `/vincular` liga conta e dá o cargo.

**Fase 3 — Reporte e máquina de estados.** `report_delivery`, `approve_report`, `reject_report` + `member_clock`. Comandos `/reportar`, `/fila`, `/aprovar`, `/rejeitar`. Teste unitário das sete transições da seção 5.2, incluindo reporte de última hora e aprovação a partir de `desligado`.

**Fase 4 — Tick do cron e cargos.** `/api/guild/clock-tick` idempotente + REST de cargo (add/remove). Cron no cron-job.org. Keep-alive. Teste: membro a 15 dias perde o cargo num tick; aprovação devolve; membro fora do servidor não quebra o tick.

**Fase 5 — Metas e cadeia.** `/api/guild/assign`, `/api/guild/assignments`, `/api/guild/member-status`, `/api/guild/assign-from-chain`. Comandos `/atribuir`, `/minhas-metas`, `/meu-status`. Teste: cadeia salva vira metas com `buy_qty` e `sector` corretos; folhas não viram meta de fabricação.

**Fase 6 — Consultas e gráficos.** `albion/discord_graphs.py`, endpoints `/api/discord/price|search|prodchain`, defer + webhook follow-up. Comandos `/preco`, `/buscar`, `/cadeia`, `/relatorio-semana`. Teste: PNG chega no Discord; sem matplotlib, cai em texto.

**Fase 7 — Endurecimento.** Rate-limit por token, rotação de tokens, `DISCORD.md` para operadores, alpha com admin e auditores, depois abertura gradual aos membros.

---

## 9. Riscos e limites honestos

**O baú não é legível.** A API do Albion não expõe o conteúdo do baú da guild. O sistema registra a alegação do membro e congela o relógio, mas a verdade da entrega depende do auditor abrir o jogo e conferir. Toda a arquitetura assume isso: o estado `pendente` existe justamente porque a máquina não sabe, e não deve fingir que sabe. Quem prometer auditoria automática aqui está prometendo o que o jogo não entrega.

**Cold start contra o limite de 3s.** O Render free hiberna; a primeira requisição depois da soneca leva ~30s para acordar, e o Discord corta interações sem ACK em 3s. Duas defesas em camadas: o keep-alive de 14 min reduz a chance de pegar o app dormindo, e o defer (`type: 5`) salva os comandos que mesmo acordados passariam de 3s. O risco residual é a janela entre uma hibernação e o próximo keep-alive; nessa fresta, um comando pode falhar e o membro repete. Aceitável para o piloto, custo zero. Subir para o plano pago do Render elimina a hibernação, se a guild crescer.

**Custo do free tier.** Render free dá 0,5 vCPU e 512 MB; matplotlib carregado custa RAM. Supabase free limita conexões e pausa o projeto após inatividade longa. O pooler em modo transação (porta 6543) já exige `prepare_threshold=None`, como `store.connect` faz. Gráficos pesados em rajada podem encostar no teto de CPU; por isso o orçamento da seção 7.1 fica em gráficos simples e o defer serializa o trabalho.

**Colisão de IP do bot com humanos.** Resolvida pelo token de serviço, que não passa pelo limite de 2 IPs. O risco seria deixar o bot autenticar por sessão e queimar uma das duas vagas de IP de uma conta. O plano evita isso por construção: bot só fala por header de serviço, nunca por cookie.

**Estado dividido entre dois sistemas.** O cargo no Discord e o estado no banco podem divergir após uma falha de rede na REST. A mitigação é a ordem fixa (banco primeiro, Discord depois) e a reconciliação no próximo tick. Não há transação distribuída; há convergência eventual, com o banco como autoridade.

**Role `auditor` não existe em `auth.py`.** Os desenhos assumiram um role `auditor` que o código não tem. O plano não inventa o role: usa `OPERATOR_ROLES` para a conta web do auditor e o cargo "Auditoria" do Discord como segundo fator. Se o dono quiser um role dedicado depois, é um item em `ROLES` e nada mais; até lá, não se cita API que não existe.

---

Arquivos do código real citados:

- `albion/store.py` — backend dual, `connect/upsert/cutoff_iso/init_schema`, `Row`, `_insert_id`.
- `albion/auth.py` — `ROLES`/`OPERATOR_ROLES`/`ADMIN_ROLES`, `MAX_IPS_PER_ACCOUNT`, `auth_accounts.discord_nick`, `AuthManager._tx/_read/_insert_id/authenticate`.
- `app.py` — `_auth_guard`, `_require_role`, `_client_ip` (XFF-direita), `_check_sweep_token`, `PUBLIC_AUTH_PATHS`, `/api/prodchain*`, startup.
- `albion/prodchain.py` — `build_graph/solve/make_or_buy`, `_sector` (`raw|refine|craft|farm|buy`), `solve().nodes[item].leaf/buy_qty`, `ChainStore`.

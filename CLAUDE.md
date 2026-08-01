# Mercado Albion — Américas (app local)

App local de consulta de preços e flips do Albion Online (servidor das Américas),
dados do Albion Online Data Project (AODP). Interface PT-BR + CLI de análise.

## Piloto na nuvem (branch pilot-sem-coleta) — IMPORTANTE p/ contexto

O app ganhou um modo de PILOTO gratuito (Render + Supabase, sem cartão; guia em
DEPLOY.md). Mudanças estruturais em relação ao app local:
- **Camada de dados dual** (albion/store.py): SQLite local OU Postgres na nuvem
  (escolhido pela env DATABASE_URL). Regra de ouro: NADA de funções de data do
  SQL (date('now'), strftime, datetime) — use store.cutoff_iso e compare `ts`
  (texto ISO) com `>=`. Escritas com conflito via store.upsert/upsert_ignore/
  upsert_add (este SOMA, p/ agregados). store.Row imita sqlite3.Row.
  2ª regra de ouro (tipo): no Postgres, SUM/AVG de coluna INTEIRA (BIGINT como
  item_count, victim_units, sell_price_min, gold.price) volta **Decimal**;
  misturar Decimal com float (colunas DOUBLE: avg_price, fetched_at) em / * + -
  estoura TypeError. No SQLite tudo é float, então o bug NÃO aparece local nem
  nos testes. Sempre coaja o agregado com float()/int() NA LEITURA. Regressão:
  HistoryStatsPgTypeTests/LiquidityPgTypeTests injetam Decimal (o bug do 500 do
  /recomendar, jul/2026).
- **Coleta = sweep fatiado por cron** na NUVEM (não mais coletor sempre-ligado);
  no LOCAL (SQLite) app.py._start_auto_collector religa um coletor em background
  (startup event): semeia a watchlist com itens líquidos no 1º uso e coleta a cada
  AUTO_COLLECT_INTERVAL_MIN, para "abrir → atualiza sozinho". Desligável com
  ALBION_NO_AUTOCOLLECT=1; nunca roda em Postgres nem sob testes. /api/sweep
  varre TODO o mercado (~10.4k itens, _market_universe, cursor em sweep_state) e
  /api/intel-sweep agrega o killboard. Ambos protegidos por ALBION_SWEEP_TOKEN
  (fail-closed em prod), isentos de auth, tocados por cron-job.org.
- **Killboard MAGRO**: não guarda o firehose cru no piloto — só o agregado diário
  kill_demand_daily (server,day,item_id,slot,quality→victim_units). Alimenta
  Guild (make_or_buy/watch/kit) e Logística restock e Demanda (burn/quality).
  PvP puro (correlação intra-evento) fica fora. As análises de killboard somam
  victim_units com `slot != 'Inventory'` (só gear equipado, não carga).
- **Auth** portado p/ Postgres (SERIAL/RETURNING; _read encerra transações de
  leitura). Bootstrap por env ALBION_BOOTSTRAP_ADMIN (mostra a senha no log).
- Validador do dialeto Postgres: tools/check_pg.py (rodar com DATABASE_URL).
- Módulos pvp/backtest/survival/orders permanecem no repo mas estão FORA do
  piloto (não rodam na nuvem); CLI/testes ainda os exercitam no SQLite local.

## Multi-inquilino Fase 1 (branch multi-inquilino-fase1) — APLICADA

A plataforma virou multi-org (SaaS p/ outras guilds + assinantes analytics-only):
- **Tabelas** (store.py, dual): `orgs` (id=1 = org padrão, criada por
  ensure_default_org; `discord_guild_id` BIGINT único p/ mapear servidor Discord→org),
  `org_entitlements`, `account_entitlements`. Colunas novas SEMPRE no FIM
  (store.add_column faz a migração; índice de org criado APÓS o add_column).
- **Contas** (auth.py): `org_id` + `is_super` no fim de auth_accounts; o admin mais
  antigo vira super no boot (_migrate_org_columns); create_account herda a org do ator.
  `is_super` = dono da plataforma (cross-org); admin comum só manda na própria org.
- **Guardas** (app.py): `_actor_org(request)` (org do ator), `_require_role(...,
  org=...)` (papel + escopo de org), `_require_entitlement` (gate de plano;
  account_entitlements ainda sem consumidor ativo). ChainStore/production_chains
  são org-scoped (org_id + owner).
- **PENDÊNCIA ALTA-1 (deliberada)**: /api/admin/* ainda NÃO é org-scoped — corrigir
  ANTES de aceitar a 2ª guild (junto: username_norm global vs por-org; org nova
  nasce inactive). Testes: MultiTenantIsolationTests em tests/test_core.py.

## Doutrina LOCAL vs PLATAFORMA (decisão do usuário, jul/2026)

O bot NÃO deve morrer junto com a plataforma. O critério é o teto da AODP
(180/min, 300/5min, URL<=4096 => ~100 itens por chamada):
- **Precisa do banco acumulado** (varredura do mercado inteiro / série histórica
  / estado): /recomendar, /flip, /escanear, /micro, /risco, /logistica, /guild,
  /demanda, /sinais, /lab, /ilha, /plano, /foco, /produzir + tributo, Mural,
  vínculo membro→nick. Isso é o núcleo irredutível da plataforma.
- **NÃO precisa de plataforma** (repasse puro ou dado estático) => roda LOCAL no
  processo do bot, em `tools/discord_bot.py` seção "SERVIÇOS LOCAIS":
  `local_search_rows` (items_db do disco), `local_origem_data`
  (data/supply_data.json), `local_gold_pts` (AODP direto), `local_fama_data`
  (gameinfo direto, via asyncio.to_thread p/ não travar o event loop);
  `local_prices_rows` e `local_history_series` (AODP direto, mesma forma de
  /api/prices e /api/history: descarta city "0", enriquece name_pt/tier/ench e
  calcula idade — `_age_min` trata a sentinela '0001-01-01' como None; o history
  normaliza location->city e timestamp->ts); `_resolve_item` resolve pelo
  items_db do disco.
  `local_craft_data` e `local_refine_rows` (produção de UM item / ranking de
  refino: reusam albion.craft e albion.production — os MESMOS módulos da
  plataforma, nada de matemática duplicada — com preços da AODP; o refino busca
  os ~230 ids das 115 receitas em blocos de 90 = 3 chamadas, e aplica o mesmo
  clean_price_rows + corte de frescor de 3d. Única perda vs plataforma: a
  mediana histórica como 2ª âncora anti-isca, que mora no banco).
  Handlers religados: /buscar, /origem, /ouro, /fama (COM nick), **/preco,
  /comparar, /vender, /historico**, **/craftar, /refinar**. Sem nick, /fama ainda
  consulta a plataforma p/ achar o personagem vinculado (ESTADO, mora no banco).
Efeito: o Discord segue útil com a plataforma fora, E a plataforma recebe menos
carga (menos chance de cair). Regressão: `SemPlataformaTests` injeta uma API que
EXPLODE em qualquer chamada — se alguém reacoplar um handler, o teste quebra.
CUIDADO ao mexer nesses handlers: o teste TEM de injetar a via LOCAL; injetando
só uma API falsa, o teste passa a bater na REDE de verdade (a suíte pulou de 1s
p/ 71s quando isso aconteceu, com socket SSL aberto no relatório). Isso REINCIDIU
ao desacoplar /craftar e /refinar (CraftRefinarHandlerTests tinha API falsa e
virou teste de rede) — reescrito p/ injetar local_craft_data/local_refine_rows.
Restam na plataforma (precisam do banco): /recomendar, /flip, /escanear, /micro,
/risco, /logistica, /guild, /demanda, /sinais, /lab, /ilha, /plano, /foco,
/produzir, tributo, Mural e o vínculo membro→nick.

## Estúdio de Refino (ferramenta 2) — albion/refining.py, ago/2026

Ferramenta independente (como as builds): **dado estático do dump + preços ao
vivo, ZERO leitura das tabelas do banco**. Um motor puro, três consumidores —
aba **Refino** na web, 3 comandos LOCAIS no Discord e o endpoint `/api/refino`.

O que ela modela e as calculadoras de refino do mercado NÃO (verificado):
- **O foco é ESTOQUE, não interruptor** (10.000/dia com Premium, teto 30.000).
  Todo plano sai em 2 fases — com foco (RRR alto) e sem foco (RRR baixo) — e
  `plan(allow_no_focus="auto")` **não executa** a fase sem foco quando ela é
  negativa: o erro que mais custa prata é seguir no automático depois que o foco
  acaba. `advice` diz isso em PT; `break_even_raw_price` dá o teto de preço do
  bruto com e sem foco.
- **O RRR cria operações EXTRAS que gastam foco.** O que volta é insumo e é
  refinado de novo: estoque E rende `E/(c·(1-RRR))` unidades (com RRR 53,9% é
  +117%), e o foco escala junto. Contar foco por "unidade final" subestima pela
  metade.
- **Cascata de tiers** (`cascade`): refinar T5 exige 1 refinado T4 por operação,
  que exige T3… A árvore inteira até o T2, com bruto e foco por degrau.
- **Modo coletor** (`from_stock`): estoque bruto por tier → produção, gargalo
  (bruto / refinado de baixo / prata / foco) e sobras. O que falta do tier de
  baixo é COMPRADO (senão o coletor trava em zero, que era a dor real). O foco é
  alocado por **ganho incremental** (`focus_gain_per_point` = quanto o foco
  ADICIONA por ponto), não por margem/foco — ordenar pelo ingênuo manda o foco
  pro degrau errado. `products`/`kept` separam produto final do que virou insumo.
- **Taxa de uso da estação**: `itemvalue × 0,1125 × taxa/100` por operação
  (nutrição do dump). `data/refine_data.json` (gerado por
  `scripts/build_refine_data.py`) traz itemvalue/peso/nutrição dos 248 itens.
Consumidores: `/api/refino?view=plano|estoque|ranking` (app.py, preços via AODP
com `_refino_prices`, sem tocar `prices`/`history`); aba **Refino** (web, 3
subabas); **/refino**, **/refino-estoque**, **/refino-ranking** no bot (seção
SERVIÇOS LOCAIS: `local_refine_plan/stock/ranking`, 1 chamada da AODP cada, e o
plano cai pra melhor cidade cotada quando a cidade-bônus está sem cotação).
Testes: `tests/test_refining.py` (18) + 4 em `SemPlataformaTests`. A ajuda do bot
ganhou seção própria "⚙️ Refino" — o embed corta em 1024 chars POR CAMPO e
juntar com Ilha & Produção comia o /guild.
Correção junto: `/api/auth/bootstrap-status` agora devolve `ready` quando a auth
está DESLIGADA — sem isso `tools/run_local.py` travava na tela de acesso pedindo
um admin que o modo local nem usa.
Limite honesto: o ranking não conhece LIQUIDEZ (o total é teto teórico; o mercado
pode não absorver) e a venda assume ordem na cidade do refino.

## Auditoria de CAPACIDADE 2026-07-31 (20 usuários / 10 simultâneos) — correções

Auditoria multiagente de capacidade/sobrecarga (6 frentes, verificação adversarial)
contra os tetos do free sob a carga real da guild. FOLGADO: memória (~180-210 MB de
512), disco Supabase (~176 de 500, autolimite fail-closed), banda Render (~1,1 GB).
O que MORDIA foi corrigido (suíte 311):
- **EL-1/EL-2 (CRÍTICO): auth SÍNCRONA no event loop.** `_auth_guard` (async) fazia
  ~5-7 SELECTs por /api NO loop, segurando o db_lock — reabria o freeze de 15/jul,
  e PERIODICAMENTE (o /api/sweep do cron segura o mesmo lock a cada 15min). Fix:
  (a) CACHE DE SESSÃO em memória no AuthManager (chave=(hash do token, ip), TTL
  `ALBION_SESSION_CACHE_TTL_S`=30s; SÓ cacheia sucesso; invalidação COARSE em toda
  mutação de sessão — login/logout/troca/reset/update — e CIRÚRGICA no rotate_csrf;
  has_admin e authenticate_service também cacheados); (b) a auth saiu do loop via
  `run_in_threadpool(_resolve_auth_blocking, ...)`. NUNCA reverter p/ auth no loop.
- **EGR-1 (ALTA): egress de leitura do Supabase.** `_cached_price_rows` fazia
  `SELECT *` (~3-4 MB/page-load do dashboard) e filtrava cat/tier em Python. Fix:
  push-down de `item_id IN (...)` (resolve ids do catálogo; ≤1500) + cache de
  resultado server-side (`ALBION_PRICE_CACHE_TTL_S`=600s) — **só no Postgres**
  (`_read_cache_on`); no SQLite/testes fica off (egress grátis, não contamina).
- **DEP-1 (MÉDIA): conexão nova por request.** `_cache_connection` agora REUSA uma
  conexão readonly POR THREAD no Postgres (`_ReusedConn`: close()→reset, não fecha;
  gate `ALBION_RO_CONN_REUSE`); `_verify_timeout` roda 1x/processo (store.py
  `_STMT_TIMEOUT_CHECKED`), não por conexão.
- **EL-3/EL-4:** loops de fundo do bot (keepalive/killfeed/board) guardados em
  `self._bg_tasks` e cancelados em `GuildBot.close()` (não vazam por restart do
  supervisor); teto do BoundedLock 30s→`ALBION_DB_LOCK_TIMEOUT_S`=10s.
- **DEP-3/DEP-4:** cap de `_MAX_LIVE_ITEMS`=100 em /api/prices,/flips,/sell;
  `_aodp_get` do bot com retry de 429 (backoff). rank 10: timeout AODP 40s→25s
  (`ALBION_AODP_TIMEOUT_S`) contra thread starvation.
- **Higiene:** keep-alive pinga /api/health (não index.html 54KB → -577 MB/mês);
  GZipMiddleware; erro de rede vira PT (`_fetch` no web); keepalive.yml semanal.
- **MEM-2 (OOM real):** `wiki._DETAILS` usava check-then-set SEM lock — N threads
  num processo FRIO parseariam items_raw.json (17 MB, ~47 MB de pico) ao MESMO
  tempo (10x47 MB estoura os 512 MB = crash duro = queda de web E bot). Agora tem
  `_DETAILS_LOCK` com dupla checagem (provado: 10 threads → 1 parse).

### Revisão de PRONTIDÃO DE DEPLOY (ago/2026) — o que a suíte SQLite não pega

Antes do deploy rodou uma 2ª revisão adversarial focada no que só ATIVA no
Postgres (a suíte roda em SQLite e nunca executa esses caminhos). Ela BLOQUEOU o
deploy e o culpado era o próprio fix EGR-1 acima — lição a não repetir:
- **PG-1 (bloqueava):** o cache de leitura tinha teto por CONTAGEM (256 entradas)
  e NENHUM por tamanho. A consulta padrão do painel (sem filtro) cacheia a fatia
  INTEIRA de `prices`: ~46 MB/entrada na nuvem + ~17 MB do histórico; meia dúzia
  de filtros passava de 300 MB em 512 MB = OOM = web e bot mortos juntos. Agora
  `_READ_CACHE_MAX_ROWS` (4000) + máx. 8 entradas + NUNCA vazio (REG-2) + TTL
  180s. **Regra: todo cache novo precisa de teto em BYTES/LINHAS, não em chaves.**
- **PG-2/REG-3:** exceção dentro de `_auth_guard` virava HTTP **500 cru** — o
  `@app.exception_handler` NÃO alcança middleware (o ExceptionMiddleware fica
  DENTRO da pilha do usuário). O middleware agora devolve `_db_busy_json()`.
- **PG-3:** `/api/health` chamava a medição de disco (que pega o db_lock) fora de
  try/except: engasgo virava 503 no endpoint PÚBLICO que o vigia externo
  monitora = alarme falso no Discord. Agora falha vira None + `db_measure_ok`.
- **CFG-1:** o limiar de "coletando" era 300s, do cron ANTIGO de 1 min → com o
  coletor a cada 15 min marcaria "parada" quase sempre. Agora `_COLETA_FRESH_S`.
- **CFG-3 (alarme cego):** `coletor_externo_ok` vinha SÓ do estado do vigia, e a
  postura primária é `ALBION_NO_WATCHDOG=1` → era SEMPRE True. Sem vigia, quem
  denuncia a morte do coletor é a IDADE da coleta (`_CRON_LATE_S`).
- **CONN-1/PG-5:** teto de conexões reusadas (`ALBION_RO_CONN_MAX`=10; o close de
  conexão morta devolve a vaga) e aviso ALTO no boot se o `DATABASE_URL` não for
  o pooler de TRANSAÇÃO (:6543), do qual o reuso depende.
- **SCHEMA-1:** `add_column` levantava RuntimeError que não casava
  `_is_readonly_error` → crash-loop em banco somente-leitura. Agora
  `store.ReadOnlyMigration` (subclasse), reconhecida: o app SOBE degradado.
- **REG-1/REG-4:** o botão "Adicionar visíveis" mandava 150 itens contra o cap de
  100 (agora para no limite e avisa em PT); `/craftar` e `/refinar` diziam "sem
  receita" quando o problema era a AODP fora (agora separam os casos).
- **REG-5:** teto do BoundedLock 10s→**25s**: 10s ficava ABAIXO do
  statement_timeout (20s) e estourava ANTES de o dono soltar — invertia a defesa.
- **BOOT-1:** `store.connect` roda no IMPORT; Postgres fora por segundos matava o
  uvicorn em crash-loop. Agora `AODP._connect_com_retry` (5x, 3s, só no PG).
- **GZIP-1:** aceito e NÃO corrigido (recomprimir PNG é cosmético; um middleware
  ASGI caseiro custaria mais que o desperdício).
Ferramenta nova: **`python tools/verificar_nuvem.py [url]`** — lê o /api/health
público e explica o estado em PT (banco, coleta, killboard, disco, bot) com o que
fazer; exit 0/1/2 p/ monitor. É o "sabe antes da guilda" na mão do usuário.

RESIDUAIS ESTRUTURAIS (o free tier não deixa eliminar, decisão do usuário):
host único (bot in-process = queda dupla no OOM/suspensão; separar exige 2º host),
event loop único (NÃO subir --workers>1: cada worker sobe outro bot Discord),
keeper externo (falta cron-job.org/UptimeRobot <=5min em /api/health), egress
Supabase 5 GB é apertado por natureza. PENDENTE do usuário (segredos/config, não
código): repo PÚBLICO, secret DATABASE_URL, vars COLETOR_ATIVO=1 e PUBLIC_URL,
secret ALBION_ALERT_WEBHOOK — e conferir se o projeto do Supabase PAUSOU (o free
pausa após 7 dias sem atividade; foi o caso durante a suspensão do Render).
Testes novos: session cache evict, pushdown==filtro-Python, reuso de conexão PG,
coletor_externo_ok denuncia coletor morto sem vigia, limiar de coleta vs cadência.

**LIÇÃO DE PROCESSO (ago/2026):** duas sessões de agente no MESMO diretório
custaram caro — um descarte do working tree apagou trabalho não commitado das
duas, e um teste chegou a ler um arquivo NO MEIO da gravação da outra sessão
(diagnóstico falso de comando faltando no /ajuda). Se houver 2 agentes: um por
vez, ou worktrees separados; e **commitar em lotes pequenos, na hora** — commit
sobrevive a `git checkout/restore`, diretório não.

## Auditoria de estabilidade 2026-07-19 (pós-suspensão por banda) — correções

Suspensão por BANDA no Render (coletor baixava 36 GB > 5 GB free). Coletor movido
p/ GitHub Actions (tools/collector_tick.py, .github/workflows/coletor*.yml). Depois,
auditoria multiagente (8 frentes, verificação adversarial) achou os riscos LATENTES
que fariam a falha VOLTAR — corrigidos:
- **Vigia re-coletava no Render** (recriava a banda): `_CRON_LATE_S` 150s→**1800s**
  (env ALBION_CRON_LATE_S) — só assume se o Actions morrer >30min, não no vão
  normal de 15min; o intel do vigia agora só roda DENTRO do takeover (não a cada
  laço). `ALBION_NO_WATCHDOG=1` no render.yaml = postura primária (Render só serve).
- **/api/health de-mascara a morte do coletor externo**: campos novos
  `coletor_externo_ok` (vigia ativo >25min = Actions morto), `intel_ok`,
  `db_measure_ok` (None→db_mode='unknown', não 'ok' silencioso), `bot_ok`/
  `bot_restarts`. `coletor_externo_ok` entra no `ok` global.
- **Dead-man-switch**: `.github/workflows/vigia-externo.yml` bate no /api/health
  a cada 15min DE FORA e posta num webhook do Discord se cair (gate vars.PUBLIC_URL
  + secret ALBION_ALERT_WEBHOOK). "Sabe antes da guilda."
- **Bot supervisor**: _run_discord_bot_inproc agora re-sobe o bot com backoff
  (recria o Client) em vez de morrer no 1º erro fatal; _BOT_STATE no /api/health.
- **statement_timeout (57014) NÃO é morte de conexão** (store._is_dead): antes
  reconectava+re-executava, dobrando o tempo com o db_lock preso; agora faz
  rollback e sobe rápido. Retry automático só quando seguro (flag `_dirty`) — não
  perde statement de transação multi-linha (buraco no agregado). connect() agora
  VERIFICA o statement_timeout pós-conexão (o pooler de transação pode ignorar
  `options` em silêncio) e loga ALTO se não pegou.
- **Autolimite FAIL-CLOSED**: sweep_mode(None)→'soft' (era 'ok'): se a medição de
  disco falhar, corta histórico em vez de encher às cegas até o read-only.
- Testes: WatchdogHealthTests (vigia não assume no vão de 15min), PgConnResilienceTests
  (57014 não reconecta; retry não perde 2º statement). Suíte 300.
PENDÊNCIAS conhecidas (não-iminentes, documentadas): sweep_mode preso em 'hard'
se pg_database_size não encolher pós-poda (precisa VACUUM/medida lógica; disco hoje
~176MB, longe dos 420); advisory lock p/ 2 instâncias do bot (HF fora, 1 instância);
contador de banda no app (coberto por alertas nativos Render/Supabase).

## Incidente 2026-07-15 (app congelou) — defesas permanentes

Query pendurada no pooler (sem timeout) segurava o db_lock e congelava o app
INTEIRO (site+bot+autocomplete) até reinício manual. Defesas em vigor:
- store.connect (PG): connect_timeout=10s, TCP keepalives (~60s p/ rede morta),
  statement_timeout=20s via `options` com fallback se o pooler recusar;
  _PgConn reconecta e re-tenta 1x quando a conexão morre (upserts idempotentes).
- **AUTOLIMITE de disco**: /api/sweep mede o banco (store.db_size_mb, cache
  5min) e decide por store.sweep_mode (puro): >=ALBION_DB_SOFT_MB (340) corta
  o HISTÓRICO e poda a cada tick; >=ALBION_DB_HARD_MB (420) pausa a coleta
  (leituras seguem). Nunca mais encher até o Supabase virar read-only.
- Autocomplete do bot é LOCAL (local_item_search sobre data/items_db.json em
  memória; t4/@2/4.2 filtram; sem @N colapsa no base) — o picker funciona
  mesmo com a API fora; HTTP é só fallback. Testes: DbSelfLimitTests,
  LocalItemSearchTests.
- **Keep-alive próprio** (_start_keepalive_task no bot): pinga a URL pública
  (RENDER_EXTERNAL_URL) a cada 4min — slash command NÃO conta como tráfego no
  Render e o free tier dormia NO MEIO do uso quando o cron externo morria.
- **VIGIA da coleta** (_start_cloud_watchdog, app.py; só PG): se o cron externo
  atrasar >2,5min (_cron_late), assume a coleta interna com o mesmo
  _sweep_tick_core (sweep_reserve atômico = zero duplicação) e volta a standby
  quando o cron retorna; roda o intel (killboard) a cada 10min — nunca houve 2º
  cron. **/api/health é PÚBLICO** (PUBLIC_AUTH_PATHS): db/coleta/disco/vigia/
  uptime sem login — é como verificar a prod de fora. **/saude** no bot mostra
  isso em PT. Doutrina: o app se mantém acordado, se coleta, se limita, se
  cura e se explica — o cron-job.org é redundância, não dependência. Testes:
  WatchdogHealthTests, SaudeHandlerTests.

## Auditoria 2026-07-02

Relatório completo (22 achados verificados + roadmap produto/Discord) em
docs/AUDITORIA_2026-07-02.md. Higiene conhecida: retenção do cache automatizada no
coletor, backup via tools/backup_db.py, CI em .github/workflows/tests.yml.

## Tributo da guild (núcleo web-first) + Discord Fase 0

- **Tributo** (albion/tribute.py + tabelas duais em store.py: weekly_assignments,
  member_reports, member_clock, guild_audit_log — org_id INTEGER, nunca snowflake):
  máquina de estados do PLANO_DISCORD_GUILD.md §5.2 (reporte PAUSA o relógio,
  aprovação ZERA, rejeição RETOMA — mas só se não houver OUTRO pendente —, 14d
  desliga; relógio 100% injetável via `now`). Endpoints /api/guild/assign|
  assignments|report|pending|approve|reject|member-status|members (todos com
  _require_entitlement("operacao") + papel + org) e /api/guild/clock-tick
  (token ALBION_GUILD_TOKEN, fail-closed fora do sqlite/localhost). week_start
  normaliza p/ SEGUNDA-FEIRA no backend E no cliente. Aba **Guild** na web
  (operador/admin; guildTabButton). Testes: tests/test_tribute.py (20).
- **Discord Fase 0** (sem bot): `analyze.py report --discord` e
  `analyze.py digest --view report|ilha|laborers|advisor [--budget N] --discord`
  postam no webhook (env ALBION_DISCORD_WEBHOOK; helper _post_discord fatia em
  1900 chars). Agendamento Windows: tools/discord_daily.bat (schtasks;
  PYTHONIOENCODING=utf-8 obrigatório — stdout cp1252 estoura em ▲/▼).
  Próximas fases (bot gateway, vínculo membro→conta, tributo via Discord):
  docs/AUDITORIA_2026-07-02.md seção Discord.

## Mural de Conquistas (killfeed SÓ de vitórias) — Discord

Feed que celebra SÓ quando um MEMBRO dá o golpe final em outro jogador (nunca
mortes — pra ninguém passar vergonha). Tabelas duais em store.py: `killfeed_settings`
(org_id PK, channel_id, min_fame, active) e `killfeed_watch` (org_id, guild_name,
guild_name_norm, **guild_id**). Config por org via comandos **/mural-canal**
(usa o canal atual), **/mural-guilda** (resolve o NOME→Id no gameinfo /search e
confirma a grafia; guarda o Id, não o nome), **/mural-status**, **/mural-remover** —
todos gate duplo (escopo guild_audit + papel operador via _discord_operator).
Fonte: `GET /events?guildId=` (guild-scoped, só kills; SEM firehose global), com
checkpoint POR GUILDA em public_ingest_checkpoints (source `killfeed:<guild_id>`;
1ª rodada só PRIMA, não despeja histórico). O bot roda um laço (_start_killfeed_task,
~2,5 min) que chama `/api/killfeed/poll` (escopo discord_read; enriquece com
nome/ícone PT da arma do killer) e posta embeds (killfeed_embed_data, puro). O
poll avança o checkpoint no servidor: se o post falhar, o abate é perdido
(aceitável p/ feed comemorativo). albion/gameinfo.py: poll_killfeed, resolve_guild,
events_by_guild, search, norm_guild. Testes: KillfeedPollTests (test_core),
KillfeedBotTests (test_builds_bot). Config é single-org por ora (mesma pendência
ALTA-1 do /api/admin). Fama por atividade (LifetimeStatistics do /players/{id})
fica p/ próxima leva (/fama; Albion 2D é só UI sobre esse mesmo endpoint).

## Camadas do servidor Discord (organização)

Três ANÉIS de acesso, decididos com o usuário: **Visitante** (entrou no Discord
mas NÃO é do projeto — só a área pública) → **Aprendiz** (1º nível DENTRO da
guild) → **Oficial** (responsável por área/mester) → **Mestre** (liderança). A
linha de progressão é a dos mesteres (aprendiz→oficial→mestre); o Visitante fica
fora do portão. Dados em tools/discord_bot.py: `SERVER_ROLES`, `RING_INTERNO`
(Aprendiz+), `RING_STAFF` (Oficial/Mestre), `SERVER_PLAN` (📢 ENTRADA público /
🛡️ GUILDA interno / ⚙️ COMANDO staff). `server_plan_summary()` é puro (testável);
`apply_server_plan(guild, discord)` cria só o que FALTA — nunca apaga nem move o
que já existe — e configura a permissão na CATEGORIA (canais herdam; ajustar
canal a canal gera buraco de acesso). O bot SEMPRE se inclui (guild.me
view=True) em toda categoria que tranca — sem isso ele se tranca pra fora do
que criou e não consegue nem editar nem postar (Mural) depois; erro numa
categoria não derruba as outras e o relatório traz o erro EXATO da API
(exc.text), não mensagem genérica. Comandos: **/camadas** (mostra o plano) e
**/organizar-servidor** (aplica; gate = dono do servidor ou admin do Discord).
Vínculo Discord→nick do Albion é ATO DO RECRUTADOR (decisão do usuário, o
recrutamento confere o nick por share de tela): **/registrar-membro** (gate
`is_recruiter` = dono/admin/Oficial/Mestre) grava via POST /api/discord/
character; **/personagem** é SÓ consulta ("quem sou eu"), sem autosserviço.
Exige que o bot tenha Gerenciar Cargos + Gerenciar Canais (o convite original só
deu Send Messages=2048) — o comando devolve instrução PT se levar Forbidden.
Testes: ServerLayersTests (test_builds_bot). Onboarding/Regras nativos do Discord
(pré-requisito: ativar "Community") ficam por conta do usuário na UI.

## Para fazer análises mercadológicas (pedido comum do usuário)

Use a CLI — ela cuida de cache, throttle e taxas:

```
python analyze.py search <termo>                      # achar o id do item
python analyze.py prices <itens> [--qualities 1,2]    # preços atuais por cidade
python analyze.py flips <itens> [--min-profit N]      # rotas de lucro líquido
python analyze.py scan --cat <categoria> --tier-min N --volume   # varrer categoria
python analyze.py sell <item>                         # melhor cidade p/ vender
python analyze.py history <itens> --days 30           # preço médio + volume/dia
python analyze.py recommend [--cat ...]               # recomendações (cache) c/ score absoluto
python analyze.py lab <item> [--fetch]                # estatística: VWAP, z residual, momentum
python analyze.py watch add|rm|list [itens]           # watchlist de coleta
python analyze.py collect [--cat ...]                 # coleta preços+histórico (watchlist/filtros)
python analyze.py survival [--item X]                 # persistência de ordens (flip fantasma)
python analyze.py backtest                            # valida sinal: prometido vs realizado
python analyze.py report [--discord]                  # relatório do dia (webhook opcional)
python analyze.py refine <familia> --tier N [--ench E] # margem de refino (receitas do dump)
python analyze.py craft <item> [--focus]              # margem de craft (RRR real do dump)
python analyze.py origin <item>                       # de onde o item nasce (mobs que dropam)
python analyze.py journals [--family ORE]             # margem de diários vazio->cheio
python analyze.py pos add|sell|list|rm                # portfolio (PnL real)
python analyze.py indexes --cat X --sub Y             # índice de preço da cesta
python analyze.py intel collect|top|status            # killboard: destruição/demanda
python analyze.py micro spread|book|traps|capital|hours  # microestrutura/market-making
python analyze.py prod focus|chain|refine|quality     # produção: foco/cadeia/refino/EV qualidade
python analyze.py prodchain <item> [--qty N] [--focus] # linha de produção: BOM auto + lista de compras
python analyze.py logi cargo|restock|ladder|bm        # logística: carga/reposição/qualidade/BM
python analyze.py risk profile|size|corr              # risco: VaR/drawdown, sizing, correlação
python analyze.py fc revert|pair|predict              # previsão: reversão, par trading, previsibilidade
python analyze.py demand burn|quality|meta            # demanda killboard: consumíveis/qualidade/meta
python analyze.py guild watch|kit|makeorbuy           # guild: ROI coleta/cesta regear/make-or-buy
python analyze.py prod chaincity|farm                 # cidade-bônus por cadeia; economia de fazenda
python analyze.py fc regime|backtest                  # quebra de regime; backtest do sinal de reversão
python analyze.py recommend --fused                   # escore COMPOSTO (lucro+risco+reversão+divergência)
python analyze.py watch rebuild [--replace]           # prioriza a coleta por ROI-de-informação
python analyze.py collect --deep                      # recarga de 365d (séries longas; semanal)
python analyze.py status --detail                     # cobertura local do cache
python analyze.py prune                               # agrega snapshots antigos
python analyze.py sql "SELECT ..."                    # SQL somente-leitura no cache
python analyze.py --format json ...                   # saída para parsing
```

Cache SQLite em `data/cache.db` (WAL; tabelas: prices, price_snapshots,
price_snapshots_daily, history, gold, fetch_log, watchlist). Antes de analisar,
cheque a cobertura com `status --detail` — recomendações/lab só enxergam o que
foi coletado. Metodologia: z-score é sobre RESÍDUOS da tendência (não níveis);
score usa âncoras absolutas (config.SCORE_ANCHORS); volume da AODP é piso
censurado; Pot./dia aplica CAPTURE_RATE (20%); persistência de ordens vem de
albion/survival.py sobre price_snapshots. O servidor coleta a watchlist
automaticamente a cada AUTO_COLLECT_INTERVAL_MIN (30 min), registrando em
collection_runs. Detalhes e pendências: AUDITORIA.md.

Camada analítica avançada (módulos albion/microstructure, production, logistics,
risk, forecast, demand, guild) — ~22 análises derivadas de um workflow
multiagêntico validado; backlog e ressalvas de dado em docs/ANALISES_POSSIVEIS.md.
Todas operam somente-leitura sobre o cache. Defesa comum contra ordens-isca:
microstructure.clean_price_rows zera preços-âncora (outlier z robusto entre
cidades) — use-a sempre que uma análise pegar o "melhor/maior" preço. Recurso
BRUTO é folha na cadeia de produção (a receita do dump é transmutação, sem RRR).
Venda no Mercado Negro não leva taxa de anúncio (venda instantânea na ordem do
sistema). Onde a série fina (~5-7 dias) ou positions (vazia) limitam, a análise
existe mas sai "provisória" e melhora conforme a coleta acumula.
Categorias: weapons, armors, head, shoes, offhands, capes, bags, mounts,
consumables, gathering, crafting (recursos ficam em crafting/resources), artefacts.

Killboard (gameinfo): schema real em docs/SCHEMA_GAMEINFO.md — Location vem
nulo, offset máx 1000, ~50 eventos/min em pico; o servidor ingere a cada 10 min
(AUTO_INTEL_INTERVAL_MIN). Plano completo: docs/PLANO_DADOS_PUBLICOS_ECONOMIA_AVANCADA.md.

Ilha (aba web + albion/island.py, dados em data/island_data.json via
scripts/build_island_data.py): 3 motores sobre price_q1. AGRICULTURA — a colheita
é FIXA (~4.5, igual p/ toda cultura; o @activefarmbonus do crop é só 2*(1-rebrota),
NÃO multiplicador). O FOCO não aumenta a colheita: GARANTE a volta da semente
(sem foco ela volta com seed_regrow_chance), então ganho do foco = semente
economizada. PECUÁRIA — ração (nutrição×prata/nutrição da planta mais barata) NÃO
cai com foco; o foco vira PROLE EXTRA estimada (@activefarmbonus por animal),
rotulada como estimativa e fora do ranking (ordena pelo lucro firme sem foco).
TRABALHADORES — margem do diário vazio→cheio; só famílias de COLETA
(WOOD/ORE/HIDE/FIBER/STONE→ROCK) entregam recurso bruto, fabricantes/pesca = None.

Linha de Produção (aba web + albion/prodchain.py; /api/prodchain): editor visual de
nós (grafo DAG) da cadeia da guild. Escolhe-se 1+ produtos finais, a árvore de
receita (recipe_for, recursiva) se expande até o BRUTO (production.is_raw_resource
trava bruto como folha — NÃO desce na transmutação T5_ORE→T4_ORE), e a quantidade
propaga do alvo p/ trás. RRR abate a QUANTIDADE de insumo (não o custo — senão
dupla contagem); foco por nó é um recurso à parte (pontos; só vira prata com
"prata/foco"); make-or-buy por nó (comprar poda a árvore ali). O servidor entrega o
grafo estático (RRR por cidade + preços saneados); a propagação/custo roda no
cliente ao vivo, espelhando prodchain.solve()/make_or_buy(). Cadeias salvas POR
CONTA (prodchain.ChainStore, tabela production_chains dual; /api/prodchain/chains).
Limitações: venda das raízes usa só ROYAL_CITIES (Mercado Negro não entra — é
venda-only de equip.; p/ confirmar preço de BM use /api/flips). Isolamento por
conta só vale na nuvem (AUTH on); em modo local (auth off) owner=0 é
single-user. solve() devolve missing_sell (produto final sem cotação → receita 0,
o front avisa) e missing_prices (insumo sem cotação de compra).

## Fatos que não mudam (verificados jun/2026)

- Taxas: imposto de venda 4% premium / 8% sem; taxa de anúncio 2,5% em ordens
  (compra e venda), não reembolsável; compra instantânea sem taxa.
- Mercado Negro: só Caerleon, só vende (equip. de combate), qualidade do item
  pode ser >= à da ordem.
- API AODP: 180 req/min E 300/5min; URL <= 4096 chars; preço 0 = sem dado;
  city "0" = localização inválida; prices usa campo "city", history usa "location".
- Serviço de ícones (render.albiononline.com) bloqueia clientes não-navegador
  via Cloudflare de forma intermitente — por isso existe o proxy /icon com cache
  em disco + fallback no frontend.

## Rodar / desenvolver

- Servidor: `python app.py` (porta 8528, abre o navegador) ou preview `mercado-albion`.
  Para testar SEM login na própria máquina: `python tools/run_local.py` (liga
  ALBION_AUTH_DISABLED=1) ou o preview `mercado-albion-dev`. Nesse modo /api/auth/me
  devolve um admin "local" p/ o frontend não travar na tela de acesso. A nuvem
  sempre roda com auth ligada.
- Itens novos após patch: `python scripts/build_items_db.py --refresh`,
  depois `build_recipes.py`, `build_craft_recipes.py`, `build_craft_data.py`,
  `build_supply_data.py` (refino/craft/oferta).
- RRR de refino/craft é DERIVADO de data/craft_data.json (craftingmodifiers do
  dump), não mais hardcoded; oferta (mobs->itens) em data/supply_data.json.
- O frontend é vanilla JS servido de `web/` — sem build step.
- Console Windows usa cp1252: nunca dê print de bytes da API sem tratar encoding.

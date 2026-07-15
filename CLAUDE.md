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
canal a canal gera buraco de acesso). Comandos: **/camadas** (mostra o plano) e
**/organizar-servidor** (aplica; gate = dono do servidor ou admin do Discord).
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

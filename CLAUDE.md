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
- **Coleta = sweep fatiado por cron** (não mais coletor sempre-ligado): /api/sweep
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
- Itens novos após patch: `python scripts/build_items_db.py --refresh`,
  depois `build_recipes.py`, `build_craft_recipes.py`, `build_craft_data.py`,
  `build_supply_data.py` (refino/craft/oferta).
- RRR de refino/craft é DERIVADO de data/craft_data.json (craftingmodifiers do
  dump), não mais hardcoded; oferta (mobs->itens) em data/supply_data.json.
- O frontend é vanilla JS servido de `web/` — sem build step.
- Console Windows usa cp1252: nunca dê print de bytes da API sem tratar encoding.

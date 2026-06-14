# Registro de trabalho dos agentes

## 2026-06-10 - Correcoes de execucao e CLI

Contexto: a auditoria inicial mostrou que `python` nao estava no PATH, `py -3.13`
apontava para uma instalacao Windows Store que nao iniciava, e o Python do Codex
nao tinha `fastapi`, `uvicorn` nem `httpx`. Isso impedia rodar `run.bat`,
`app.py` e ate comandos locais do `analyze.py`, porque `httpx` era importado no
topo da CLI.

Alteracoes feitas:

- Criado `.venv` local no projeto e instaladas as dependencias de
  `requirements.txt` nele.
- Refeito `run.bat` para preferir `.venv\Scripts\python.exe`, depois `python`,
  depois `py -3`. O script agora checa `fastapi`, `uvicorn` e `httpx` antes de
  iniciar e mostra o comando de instalacao se faltar dependencia.
- Ajustado `analyze.py` para importar `albion.client.AODP` e `httpx` de forma
  preguiçosa, somente nos comandos que precisam da API. Com isso, `search` e
  `sql` funcionam sem `httpx`.
- Adicionado `--sub` ao comando `search`.
- Adicionados filtros avancados da UI na CLI para `flips` e `scan`:
  `--buy-cities`, `--sell-cities`, `--max-age-buy`, `--max-age-sell`.
- Adicionado `--max-age` em `prices`, `flips`, `scan`, `sell` e `history`,
  permitindo usar cache antigo de forma explicita sem forcar rede.
- Criada `.gitignore` basica para evitar versionar `.venv`, `__pycache__` e
  caches temporarios.

Validacoes executadas:

- `.venv\Scripts\python.exe -B -c "import fastapi, uvicorn, httpx"`
- `.venv\Scripts\python.exe -B -c "import app; print(app.app.title); print(len(app.db.items))"`
- `.venv\Scripts\python.exe -B analyze.py search bolsa --limit 3`
- `.venv\Scripts\python.exe -B analyze.py sql "SELECT COUNT(*) AS prices FROM prices"`
- `.venv\Scripts\python.exe -B analyze.py prices T5_BAG --cities Martlock --qualities 1 --max-age 9999999999`
- `.venv\Scripts\python.exe -B analyze.py history T4_BAG --cities Martlock --quality 1 --days 7 --max-age 9999999999`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py`
- Servidor iniciado via `uvicorn app:app` em `127.0.0.1:8528`, com respostas
  verificadas para `/api/meta`, `/api/search` e `/api/prices` usando cache.

Pendencias importantes:

- Nao foi feito teste visual completo no navegador.
- `run.bat` nao foi executado diretamente para evitar abrir navegador e manter
  processo rodando; o mesmo caminho Python/dependencias foi validado via
  `uvicorn`.
- Ainda nao ha testes automatizados formais.
- A CLI agora tem mais filtros, mas ainda pode evoluir para relatorios de
  confianca, backtesting, snapshots historicos append-only e craft/refino.

## 2026-06-10 - UI, status e testes basicos

Objetivo: continuar a estabilizacao apos a primeira rodada, com foco em
melhorias pequenas de produto e em testes que protejam a logica critica.

Alteracoes feitas:

- Adicionado endpoint `GET /api/status` em `app.py` com resumo local do cache:
  existencia/tamanho do banco e contagem/ultimo timestamp de `prices`,
  `history`, `gold` e `fetch_log`.
- Adicionado indicador `cache: N precos` no cabecalho do frontend, alimentado
  por `/api/status`.
- Adicionado botao `Restaurar ocultas` no Scanner para limpar os flips ocultos
  salvos em `localStorage`.
- Separado no estado do frontend o resultado bruto do scanner (`scanOpps`) das
  linhas visiveis (`scanRows`), permitindo restaurar ocultas sem reexecutar a
  busca.
- Versionados `style.css` e `app.js` no `index.html` com query string simples
  para reduzir cache velho durante atualizacoes locais.
- Criado `tests/test_core.py` com `unittest` cobrindo formulas de taxas,
  flip basico entre cidades, compatibilidade de qualidade no Mercado Negro,
  busca de item e endpoints `/api/meta` + `/api/status`.
- Corrigido bug em `albion/flips.py`: quando o usuario filtrava uma qualidade
  especifica, o motor descartava ordens do Mercado Negro de qualidades menores
  antes de calcular se uma peca de qualidade maior poderia preenche-las. Agora
  o motor monta o mapa do Mercado Negro com todas as qualidades e aplica o
  filtro apenas na qualidade do item analisado. A mesma correcao foi aplicada
  em `where_to_sell`.

Validacoes executadas:

- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile app.py analyze.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- Smoke test de servidor em `127.0.0.1:8528` dentro do mesmo comando:
  `/`, `/api/status` e `/app.js?v=20260610-2`.
- Navegador integrado abriu a primeira tela e confirmou sem banner de erro,
  com abas e controles principais carregados. Observacao: o navegador estava
  com HTML/JS cacheado durante parte da verificacao; por isso os assets foram
  versionados e a confirmacao final dos assets foi feita via HTTP local.

Pendencias:

- Fazer QA visual/manual completo em navegador normal depois de abrir pelo
  `run.bat`.
- Expandir os testes para CLI com mocks de rede/cache e para endpoints de
  `prices`, `scan`, `sell` e `history`.
- Criar historico append-only de snapshots de preco e camada de confianca do
  flip.

## 2026-06-10 - Guia economico e status CLI

Objetivo: documentar o universo de analises economicas possiveis e adicionar
uma forma rapida de checar a cobertura local antes de analisar.

Alteracoes feitas:

- Criado `docs/GUIA_ANALISES_ECONOMICAS.md` com uma matriz de analises:
  preco/spread, arbitragem, Mercado Negro, market making, onde vender,
  historico, ouro, livro de ordens, sobrevivencia de ordens, backtesting,
  craft/refino, cadeias produtivas, farming, artefatos, qualidade/encanto,
  logistica, portfolio, outliers, patches/eventos e cross-server.
- Adicionado comando `python analyze.py status --detail`, somente-leitura,
  para resumir itens locais, tamanho do cache, linhas/ultimos timestamps por
  tabela e cobertura de precos por cidade.
- Atualizado `README.md` com o comando `status` e link para o guia.

Validacoes executadas:

- `.venv\Scripts\python.exe -B analyze.py status --detail`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- Confirmado que `docs/GUIA_ANALISES_ECONOMICAS.md` existe e tem 402 linhas.

Observacoes:

- `status --detail` mostrou 12.066 itens no banco local, 13.660 linhas de
  precos, 379 itens cobertos em `prices`, 333 linhas de historico e 8 itens
  cobertos em `history`.
- Em 2026-06-10 16h, havia um servidor local escutando em `127.0.0.1:8528`
  no PID 26136.

## 2026-06-10 - Snapshots append-only e confianca de flips

Objetivo: iniciar a camada de inteligencia historica prevista no guia, sem
quebrar a tabela rapida `prices`.

Alteracoes feitas:

- `albion/client.py` agora cria a tabela `price_snapshots`, append-only, com a
  mesma estrutura de preco de `prices` e chave incluindo `fetched_at`.
- Quando `get_prices()` busca dados novos na API, grava tanto em `prices`
  quanto em `price_snapshots`.
- `/api/status` e `python analyze.py status --detail` agora reportam
  `price_snapshots`.
- `albion/flips.py` agora calcula `confidence_score`, `confidence_label` e
  `confidence_notes` com base no lado mais velho da rota de flip.
- CLI e frontend passaram a exibir a coluna `Conf.` nas tabelas de flips.
- Exportacao CSV do scanner inclui os campos de confianca.
- Assets do frontend versionados para `v=20260610-3`.
- `docs/GUIA_ANALISES_ECONOMICAS.md` e `README.md` atualizados para mencionar
  snapshots e a camada de confianca.
- Testes atualizados para cobrir confianca e criacao de `price_snapshots`.

Validacoes executadas:

- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- `.venv\Scripts\python.exe -B analyze.py prices T5_BAG --cities Martlock --qualities 1 --fresh`
- `.venv\Scripts\python.exe -B analyze.py status --detail`
- `.venv\Scripts\python.exe -B analyze.py sql "SELECT COUNT(*) AS snapshots, COUNT(DISTINCT item_id) AS items, MAX(datetime(fetched_at,'unixepoch')) AS latest FROM price_snapshots"`
- Smoke HTTP em `127.0.0.1:8528`: `/`, `/app.js?v=20260610-3` e
  `/api/status`.

Resultado observado:

- `price_snapshots` existe e recebeu 5 linhas para `T5_BAG` em Martlock apos
  uma consulta fresca autorizada.
- `prices` foi atualizado para Martlock em `2026-06-10 19:17:53`.
- Servidor local reiniciado na porta 8528 com PID 3396.

Pendencias:

- Criar comandos/visoes que usem `price_snapshots` para recorrencia,
  volatilidade e backtesting real.
- Refinar `confidence_score` com volume, recorrencia historica e profundidade
  de livro quando esses dados forem coletados.

## 2026-06-10 - Busca estruturada de itens no frontend

Objetivo: aproximar a ergonomia de busca/filtro do app ao mercado do Albion,
mantendo Vanilla JS e a estrutura de `web/app.js` em arquivo unico.

Alteracoes feitas:

- Expandido o componente `makeItemPicker()` em `web/app.js` para renderizar um
  navegador de itens com filtros estruturais:
  categoria, subcategoria dependente, tier, encantamento e busca por texto.
- Os filtros consultam `/api/search` com `cat`, `sub`, `tier_min`,
  `tier_max`, `ench`, `q` e `limit`, usando o `meta` carregado no init para
  popular dinamicamente categorias/subcategorias.
- Mantido o autocomplete por nome/id e atalhos existentes; agora ele atua em
  conjunto com os filtros.
- Adicionado no picker de Flips o botao `Adicionar visiveis`, que inclui todos
  os resultados filtrados ainda nao selecionados.
- Marcados os quatro pickers de item em `web/index.html` como
  `item-search-field` e versionados assets para `v=20260610-4`.
- Ajustado `web/style.css` para layout responsivo, dropdowns escuros e grade
  de filtros em 4/2/1 colunas conforme viewport.

Validacoes executadas:

- `node --check web\app.js`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- Smoke de `/api/search` via FastAPI TestClient com filtros
  `cat=weapons`, `tier_min=4`, `tier_max=4`, `ench=0`, `limit=5`.
- QA no navegador integrado em `http://127.0.0.1:8528/`: assets
  `v=20260610-4` carregados, quatro pickers com filtros renderizados,
  cascata `Armas -> subcategorias` funcionando, busca `arco` T4.0 retornando
  resultados e botao `Adicionar visiveis` do Flips adicionando Bolsa T4.0.
- Console do navegador sem erros/warnings apos a interacao.

Observacoes:

- A pasta atual nao esta inicializada como repositorio Git para `git diff`;
  a revisao foi feita por busca textual com `rg` e leitura dos trechos editados.
- Esta entrega e a base para reutilizar o mesmo componente em futuras telas de
  dashboard, watchlist e rotas seguras.

## 2026-06-10 - Recomendacoes automaticas e descoberta de flips

Objetivo: evitar tela inicial vazia e permitir encontrar flips por
caracteristicas de mercado, sem selecionar item manualmente.

Alteracoes feitas:

- Criado endpoint `GET /api/recommendations` em `app.py`, cache-only, sem
  fetch externo no carregamento da tela.
- O endpoint cruza:
  `prices` para spread/frescor atual,
  `history` para volume medio diario e dias ativos,
  metadados locais de item para categoria/sub/tier/encanto,
  taxas e modos de compra/venda para lucro liquido.
- Padrao do endpoint: rotas reais seguras, excluindo Caerleon, com compra
  instantanea e venda por ordem.
- Adicionados filtros no endpoint:
  `cat`, `sub`, `tier_min`, `tier_max`, `ench`, `qualities`,
  `min_daily_volume`, `min_active_days`, `history_days`, `max_age_buy`,
  `max_age_sell`, `min_profit`, `min_roi`, `buy_cities`, `sell_cities`,
  `same_city`, `exclude_outliers` e `limit`.
- Criado score multifator `opportunity_score`, combinando potencial/dia,
  frescor, ROI, liquidez e lucro por unidade; labels: `executar`,
  `monitorar`, `cautela`.
- Adicionada aba inicial `Inicio` em `web/index.html`, ativa ao abrir o app,
  com tabela `Recomendacoes agora`.
- Adicionado bloco `Descobrir flips` no topo da aba Flips, com filtros de
  categoria/subcategoria, tier, encanto, qualidade, volume/dia minimo, dias
  ativos, idade maxima de compra/venda, lucro minimo, ROI minimo, periodo de
  volume e quantidade de resultados.
- `web/app.js` ganhou renderizacao de tabelas de recomendacao, chamada
  automatica ao abrir o app e busca manual pelo botao `Pesquisar`.
- `web/style.css` ganhou estilos para `section-head`, controles de descoberta
  e selos de score.
- Assets versionados para `v=20260610-5`.
- Teste de contrato adicionado em `tests/test_core.py` para
  `/api/recommendations`.

Validacoes executadas:

- `node --check web\app.js`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- FastAPI TestClient:
  `/`, `/app.js?v=20260610-5`, `/style.css?v=20260610-5`,
  `/api/recommendations?limit=3`.
- Confirmado via TestClient que o HTML contem `Recomendacoes agora` e
  `Descobrir flips`.

Observacoes:

- O ambiente desta sessao nao manteve processo background persistente via
  `Start-Process`/`Start-Job` para QA visual no navegador; a validacao foi
  feita por TestClient e checagem de sintaxe.
- Proximo passo natural: adicionar seletor de cidades na descoberta, salvar
  presets de filtros e ligar as recomendacoes a uma Watchlist/collect_snapshots.

## 2026-06-10 - Total vendido nas recomendacoes

Objetivo: complementar `Freq.` com quantidade efetiva de itens vendidos no
periodo historico analisado.

Alteracoes feitas:

- `/api/recommendations` agora inclui `vol_buy_total`, `vol_sell_total` e
  `volume_min_total`, calculados a partir de `history.item_count`.
- A tabela de recomendacoes em `web/app.js` ganhou a coluna `Vendidos`, usando
  `volume_min_total` para representar o lado limitante da rota; o tooltip mostra
  totais da origem e do destino.

Validacoes executadas:

- `node --check web\app.js`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- FastAPI TestClient em `/api/recommendations?limit=1`, confirmando os novos
  campos de volume total.

## 2026-06-10 - Graficos historicos a partir das recomendacoes

Objetivo: aproximar a experiencia do grafico de mercado do Albion, permitindo
abrir rapidamente preco medio e volume historico de cada item recomendado.

Alteracoes feitas:

- Adicionada coluna `Grafico` nas tabelas de recomendacao.
- O botao `abrir` chama `openHistoryForOpportunity()`, muda para a aba
  Historico e preenche automaticamente item, qualidade e cidades da rota
  compra/venda.
- Adicionado periodo `24 horas` no seletor de historico.
- Quando o usuario seleciona `24 horas`, a escala muda automaticamente para
  `por hora`.
- Assets versionados para `v=20260610-6`.
- Adicionado estilo `.mini-btn` para a acao compacta nas tabelas.

Validacoes executadas:

- `node --check web\app.js`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- FastAPI TestClient: `/`, `/app.js?v=20260610-6`,
  `/style.css?v=20260610-6`; confirmado HTML com `24 horas` e assets novos.

## 2026-06-10 - Objetivos declarados e Item Lab estatistico

Objetivo estrategico discutido:

- Evoluir a plataforma de um scanner de flips para uma central local de
  inteligencia de mercado do Albion Online.
- Priorizar registro historico de longo prazo, ferramentas graficas,
  interpretacao estatistica, analise economica, inferencia e predicao.
- Tratar o mercado como uma microestrutura analisavel: preco, volume,
  liquidez, tendencia, anomalia, risco, profundidade e validade historica.
- Manter duas camadas de uso: leitura simples para membros leigos e metricas
  tecnicas auditaveis para analistas/traders avancados.
- Usar metodo cientifico: dado observado, qualidade da amostra, hipotese,
  backtest, decisao e revisao.

Registro criado:

- Adicionado `docs/OBJETIVOS_INTELIGENCIA_MERCADO.md` com:
  objetivo maior, principios, dados necessarios, ferramentas analiticas
  desejadas, ordem cientifica recomendada e estado atual da plataforma.

Alteracoes tecnicas feitas:

- Criado endpoint `GET /api/item-analysis` em `app.py`.
- O endpoint calcula, por item/cidade/qualidade:
  - pontos historicos disponiveis;
  - preco inicial, ultimo preco e variacoes;
  - media, proxy de mediana, VWAP, minimo, maximo e desvio;
  - z-score do preco;
  - percentil do preco atual na janela;
  - medias moveis rapida/lenta;
  - momentum;
  - inclinacao linear da tendencia;
  - volatilidade de retornos logaritmicos;
  - volume total, volume medio diario e z-score de volume;
  - dias ativos, razao de atividade e score de qualidade do dado;
  - previsao simples do proximo ponto por tendencia linear +/- 1 desvio;
  - interpretacao automatica com stance e notas.
- Criada comparacao intercidades dentro do endpoint:
  cidade mais barata por VWAP, mais cara, spread de VWAP, cidade mais liquida
  e cidade com melhor qualidade de dado.
- Adicionado modo `cache_only` a `/api/history`, permitindo renderizar graficos
  a partir do historico local sem tentar rede externa.
- O `Item Lab` no frontend agora usa `/api/item-analysis` e `/api/history`
  cache-only para exibir analise reprodutivel.
- Renomeada a aba visual de `Historico` para `Item Lab`.
- Adicionados ao HTML:
  `histLabPanel`, `histLabSummary` e `histLabCards`.
- `web/app.js` ganhou:
  `fmtDec`, `fmtPct`, `renderItemLab()` e chamada analitica dentro de
  `runHist()`.
- `web/style.css` ganhou painel, pills e cards analiticos para o Item Lab.
- Assets versionados para `v=20260610-7`.
- Teste de contrato em `tests/test_core.py` passou a cobrir
  `/api/item-analysis`.

Validacoes executadas:

- `node --check web\app.js`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- FastAPI TestClient:
  `/`, `/app.js?v=20260610-7`, `/style.css?v=20260610-7`,
  `/api/item-analysis?item=T4_FIBER&cities=Bridgewatch,Lymhurst&quality=1&days=7&time_scale=24`,
  `/api/history?items=T4_FIBER&cities=Bridgewatch,Lymhurst&quality=1&days=7&time_scale=24&cache_only=true`.
- Confirmado que o HTML contem `Item Lab`, `histLabPanel` e assets
  `v=20260610-7`.

Decisoes importantes:

- `Item Lab` usa cache local por padrao para evitar contaminar analises com
  fetches acidentais e para funcionar mesmo sem rede.
- A previsao adicionada e deliberadamente simples e auditavel; nao deve ser
  apresentada como oraculo, mas como baseline probabilistico inicial.
- Z-score e momentum sao sinais de pesquisa, nao recomendacoes finais; devem
  ser combinados com volume, frequencia, frescor, profundidade e backtesting.

Proximos passos recomendados:

1. Adicionar linhas de media movel e/ou bandas no grafico de preco.
2. Criar aba `Tendencias` agregando z-score, momentum e volume por watchlist.
3. Criar `collection_runs` e `watchlist_items` para auditar coleta de longo prazo.
4. Criar tabelas derivadas diarias para acelerar analises de 30/90/180 dias.
5. Criar backtesting de estrategias simples antes de adicionar modelos
   preditivos mais complexos.

## 2026-06-10 - Glossario economico e cobertura das recomendacoes

Contexto:

- Usuario perguntou se era estranho haver apenas 2 recomendacoes mesmo apos
  atualizar, e pediu um botao no canto da tela explicando siglas do economes.

Diagnostico:

- O catalogo local tem 12.066 itens.
- O cache de precos tem 423 itens.
- O historico tem 68 itens.
- Portanto `/api/recommendations` nao avalia todos os itens do jogo; avalia
  somente itens com precos cacheados e, para passar pelos filtros de liquidez,
  precisa tambem de historico.
- O filtro padrao anterior de idade maxima era 360 min. No cache atual ele
  retornava apenas 2 oportunidades.
- Com 720 min, o endpoint retornou 62 oportunidades no smoke test.

Alteracoes feitas:

- Adicionado helper `_cache_coverage()` em `app.py`.
- `/api/recommendations` agora retorna `coverage` com:
  `catalog_items`, `price_items`, `history_items`, `snapshot_items`.
- Padrao de `max_age_buy` e `max_age_sell` em `/api/recommendations` alterado
  de 360 para 720 minutos.
- Dashboard de recomendacoes em `web/app.js` tambem passou a consultar com
  720 min.
- Campos da descoberta de flips em `web/index.html` alterados para 720 min.
- Status das tabelas de recomendacao passou a exibir cobertura:
  itens avaliados, itens com preco no cache e itens com historico.
- Adicionado botao flutuante `?` no canto inferior direito.
- Adicionado modal `Glossario do economes` explicando ROI, PnL, Spread, VWAP,
  Z-score, Momentum, Volatilidade, Liquidez, Freq., Pot./dia, Slippage,
  Drawdown, Backtest, Livro de ordens, Profundidade e Conf.
- Assets versionados para `v=20260610-8`.

Validacoes executadas:

- `node --check web\app.js`
- `.venv\Scripts\python.exe -B -m unittest discover -s tests`
- `.venv\Scripts\python.exe -B -m py_compile analyze.py app.py albion\client.py albion\flips.py albion\items.py tests\test_core.py`
- FastAPI TestClient:
  `/api/recommendations?limit=200` retornou 62 oportunidades,
  `items_considered=423`,
  `coverage={'catalog_items': 12066, 'price_items': 423, 'history_items': 68, 'snapshot_items': 157}`.
- Confirmado HTML com `Glossario do economes` e assets `v=20260610-8`.

## 2026-06-11 - Correcoes da auditoria (Claude)

Contexto: AUDITORIA.md (raiz) listou ~30 achados tecnicos e metodologicos.
Esta rodada aplicou as fases 0-2 do roadmap.

Alteracoes feitas:

- albion/client.py: PRAGMA journal_mode=WAL + busy_timeout=5000 (servidor e
  CLI compartilham o cache sem 'database is locked'); retry para 5xx;
  datetime.utcnow() -> now(timezone.utc); get_gold com TTL e leitura do cache;
  get_history com janelas canonicas (HISTORY_FETCH_WINDOWS) — pedir 7 dias
  depois de 30 nao re-busca; tabelas novas: watchlist e price_snapshots_daily;
  metodos watch_add/watch_remove/watch_list, collect() e snapshot_prune()
  (agrega snapshots antigos em daily e compacta com VACUUM).
- albion/stats.py (novo): camada estatistica extraida de app.py e corrigida —
  z-score sobre RESIDUOS da tendencia linear (nao niveis), z robusto via
  mediana+MAD, mediana verdadeira, janelas de MA/momentum definidas em TEMPO
  (dias) e convertidas por escala, intervalo da previsao ingenua usando desvio
  dos residuos com cobertura declarada (~68%), abs_score() em escala log.
- app.py: importa albion.stats (de 950 para ~760 linhas); opportunity_score
  com ancoras ABSOLUTAS (config.SCORE_ANCHORS) — comparavel entre consultas;
  confianca limitada pela liquidez (item <5/dia nao passa de 'media');
  daily_potential distingue volume 0 de sem-dado; print() do proxy de icones
  trocado por log em arquivo (crash cp1252); endpoints novos: GET/POST/DELETE
  /api/watchlist e POST /api/collect; /api/status reporta watchlist.
- analyze.py: comandos novos recommend, lab (--fetch), watch add/rm/list,
  collect (watchlist ou --cat/--itens) e prune; search --ench; potencial_dia
  corrigido (0 vs None).
- web/: aba Inicio com botao 'Coletar watchlist (N)'; Item Lab com checkbox
  'buscar da API' (era so cache, sem caminho de coleta) e botao '+ watchlist';
  glossario com entradas Score (regua absoluta) e Z-score (residuos);
  favicon via /icon; assets v=20260611-1.
- tests/test_core.py: +8 testes numericos da camada estatistica (slope,
  mediana, z residual vs z de niveis, anomalia via MAD, VWAP, banda da
  previsao por residuos, ancoras do score, momentum por escala) e tabelas
  novas do AODP. Total: 15 testes.
- Limpeza da raiz: removidos sw.json (HTML 404 salvo por engano),
  _tmp_scale_bench.py, _tmp_status_bench.py, server_stdout.log,
  server_stderr.log.
- README.md: abas Inicio/Item Lab, secao 'Como ler o score e a confianca',
  comandos novos da CLI. CLAUDE.md/AGENTS.md sincronizados.

Validacoes executadas:

- .venv unittest: 15/15 OK; node --check web/app.js OK; py_compile OK.
- Servidor ao vivo (porta 8528): coleta da watchlist via botao (2 itens,
  precos+historico), recomendacoes recalculadas com score absoluto
  (cobertura 794 itens com preco / 85 com historico), Item Lab buscando
  dados novos da API para item sem cache (29 pontos) — console sem erros.
- CLI ao vivo: watch add/list, lab T4_BAG (z residual + z robusto + VWAP).

Pendencias (fases 3-6 da AUDITORIA.md):

- Sobrevivencia de ordens (flip fantasma) a partir de price_snapshots.
- Haircut de captura no Pot./dia; sazonalidade dia-da-semana.
- Backtesting walk-forward com taxas/re-listagens; predicao probabilistica.
- Testes hermeticos (hoje dependem do cache real) e agendador de coleta.

## 2026-06-11 - Coleta automatica, persistencia de ordens e haircut (Claude)

Fase 3 (parcial) da AUDITORIA.md: itens 1-2 do plano combinado com o usuario.

Alteracoes feitas:

- albion/config.py: AUTO_COLLECT_INTERVAL_MIN=30, CAPTURE_RATE=0.20.
- albion/client.py: tabela collection_runs (auditoria de coleta: inicio, fim,
  origem auto/manual/cli, contagens, ok/erro); collect() registra cada rodada.
- app.py: thread daemon de coleta automatica da watchlist no startup (dorme o
  intervalo antes da 1a rodada; erros ficam em collection_runs); endpoint
  GET /api/survival; /api/recommendations ganhou capture_rate e o campo
  daily_realistic (score passa a usar o realista com ancora escalada — valores
  estaveis); /api/scan tambem emite daily_realistic; /api/status reporta
  collection_runs.
- albion/survival.py (novo): persistencia da ordem do topo por faixa de idade,
  medida em pares consecutivos de price_snapshots (gap 10min-12h), lados venda
  e compra — proxy honesto de flip fantasma, precursor de Kaplan-Meier.
- albion/stats.py: perfil dia-da-semana (>=14 pontos diarios), razao de volume
  fim-de-semana/dias-uteis e notas automaticas de sazonalidade.
- analyze.py: comando survival (--item/--city/--quality); collect marca
  source='cli'; status mostra watchlist e collection_runs.
- web/: painel 'Persistencia das ordens' na aba Inicio (recarrega apos
  coleta); coluna Pot./dia exibe o valor realista com teto teorico no tooltip;
  CSV exporta daily_realistic/capture_rate; glossario com Persistencia e
  Pot./dia atualizado; assets v=20260611-2.
- tests/test_core.py: +3 testes (buckets de persistencia com sobrevivencia
  conhecida em AODP temporario; razao de fim de semana 3.0 + nota; score
  maximo com ancoras nas referencias). Total: 18 testes, todos OK.

Validacoes executadas:

- unittest 18/18; node --check; py_compile de todos os modulos.
- Servidor ao vivo: painel de persistencia com 3.175 pares reais
  (venda 0-30min: 35,9%; 120-240min: 98,1%; 1440+: 0% — curva informativa),
  tooltip de captura no Pot./dia, console sem erros.
- CLI: survival (tabela por faixa), status com watchlist/collection_runs;
  rodada manual de coleta registrada em collection_runs.

Pendencias seguintes (fases 4-6):

- Backtesting walk-forward com taxas/re-listagens; calibrar CAPTURE_RATE e a
  cobertura dos intervalos de previsao com dados acumulados.
- Usar a curva de persistencia para ponderar a confianca dos flips.
- Predicao probabilistica e camada de guild (relatorios, alertas, alocacao).

## 2026-06-11 - Watchlist de recursos (Claude)

- Adicionados 291 itens a watchlist: crafting/resources (133 brutos: minerio,
  madeira, fibra, couro, pedra com encantos), crafting/refinedresources (115)
  e crafting/fish (42 peixes). Total: 293 itens vigiados.
- COLLECT_MAX_ITEMS=400 em config (collect, /api/collect e CLI usam o mesmo
  teto; a coleta automatica cobre a watchlist inteira em ~12 requisicoes).
- Primeira coleta completa executada: 11.720 linhas de preco + 2.015 series
  de historico; cobertura subiu para 993 itens com preco / 326 com historico.
- 18/18 testes OK; servidor reiniciado com o teto novo.

## 2026-06-11 - Watchlist expandida para 2.157 itens (Claude)

- Adicionados a pedido do usuario: farming completo (141: animais, derivados,
  colheitas, ervas, sementes), consumables food+potions (387), gathering
  completo (686: equip. de coleta de todas as profissoes), bags (52), bestas
  (weapons/crossbow, 201), todos os JOURNAL_ cheios e vazios (399), algas.
  Encantos = ids proprios; qualidades sempre coletadas integralmente.
- COLLECT_MAX_ITEMS elevado para 2500 (rodada de ~88 requisicoes, ~40 s).
- Coleta-base executada: 86.280 linhas de preco + 10.274 series de historico.
  Cobertura: 2.475 itens com preco / 1.664 com historico. cache.db ~164 MB
  (o historico de 90 dias x 8 cidades e o grosso; prune disponivel).
- Coleta automatica a cada 30 min cobre a watchlist inteira.

## 2026-06-11 - Profissionalizacao: git, backup, backtest, report, diarios (Claude)

- Git inicializado (main; data/, backups/, .venv ignorados) com commits por fase.
- scripts/backup_cache.py: backup consistente via API do SQLite + zip (retem
  14); scripts/instalar_tarefas.cmd registra (opt-in do usuario, exigencia do
  classificador de seguranca) as tarefas MercadoAlbion-Coleta (30 min, janela
  oculta via run_hidden.vbs) e MercadoAlbion-Backup (diaria 04:00).
- analyze.py collect --skip-if-recent N: nao duplica com o servidor aberto.
- albion/backtest.py + CLI backtest + GET /api/backtest: backtest de sinal
  (lucro prometido em R1 vs realizado em R2, por ROI/rota/idade; contraparte
  sumida contada a parte). Numeros reais da 1a rodada: captura geral 10%,
  entre-cidades 100%, Mercado Negro com dado velho negativo, idade 480+ ruim.
- analyze.py report [--discord]: top oportunidades, movers ~24h da watchlist,
  resumo do backtest e cobertura; publica via DISCORD_WEBHOOK_URL (config).
- analyze.py journals: margem vazio->cheio por familia/tier com aviso sobre
  anuncios absurdos (margem instantanea e a executavel).
- Corrigido: chamadas diretas a funcoes FastAPI a partir da CLI agora passam
  todos os defaults Query() explicitamente (recommend/lab/report).
- 19 testes OK (novo teste sintetico do backtest com captura conhecida).

Pendencias seguintes: refino/cozinha com receitas do dump, heatmap
intercidades, indices de mercado, acesso LAN, portfolio tracker.

## 2026-06-11 - Refino, heatmap, LAN, portfolio e indices (Claude)

- scripts/build_recipes.py: extrai as 115 receitas de refino do dump bruto
  (incl. encantadas: no dump sao entradas X_LEVELn sem o @n do mercado) ->
  data/recipes_refining.json. Nada de constantes chutadas.
- analyze.py refine <familia> --tier --ench [--rrr 36.7] [--fee]: margem de
  refino por cidade (custo efetivo = insumos x (1-RRR)), validado com dados
  reais (couro T6.2: so Caerleon positivo, +12,8%).
- analyze.py pos add|sell|list|rm + tabela positions no cache: portfolio com
  PnL aberto marcado a mercado (venda instantanea, premium) e PnL realizado.
- analyze.py indexes: indice de preco base-100 ponderado por volume (VWAP
  diario q1) para uma cesta cat/sub/tier — recursos brutos caíram ~5% na
  semana com 22M itens/dia de volume.
- Mapa de rotas na aba Inicio: matriz compra x venda com potencial realista
  somado e item lider no tooltip (agregado client-side de /api/recommendations).
- Acesso LAN opcional: SERVE_LAN + ACCESS_TOKEN (middleware token->cookie).
- Assets v=20260611-3; 19 testes OK; console limpo; persistencia ja com
  17 mil pares apos a coleta automatica rodar o dia.

Pendencias: cozinha/alquimia com receitas do dump (mesma infra do refino),
indices/portfolio na UI, alertas de preco no Discord, calibracao continua do
CAPTURE_RATE pelo backtest.

## 2026-06-11 - Plano de dados publicos, killboard e mapa

- Criado `docs/PLANO_DADOS_PUBLICOS_ECONOMIA_AVANCADA.md`.
- Documento registra os objetivos discutidos com o usuario: evoluir a
  plataforma para inteligencia economica avancada, cruzando mercado, kills,
  batalhas, metadados estaticos, mapa, risco, guerra, builds e cadeias
  produtivas.
- Fontes pesquisadas e classificadas: AODP, `gameinfo.albiononline.com`,
  `ao-bin-dumps`, Portaler, Albion Mapper, ao-avalonian-roads, Avalon Atlas e
  AO-Noki Avalon Roads.
- O documento diferencia dados publicos fortes, zona cinzenta (OCR e leitura
  passiva fora do AODP) e praticas que nao devem ser implementadas
  (scanner/radar, memoria do cliente, automacao e scraping privado).
- Nenhum codigo da aplicacao foi alterado nesta etapa.

## 2026-06-11 - Ordens de servico integradas e preparacao para coleta aberta (Codex)

- Continuado o trabalho deixado pelo Claude em `albion/orders.py`, sem reverter
  alteracoes existentes.
- Integrado o motor de ordens de servico no backend:
  `GET /api/service-orders` e `POST /api/service-orders/refresh`.
- O gerador traduz sinais cacheados em missoes simples para trader, coletor,
  crafter, refinador e aviso de risco, usando recomendacoes, killboard,
  divergencia demanda-preco, refino e classificacao recente de mortes.
- Adicionado painel "Ordens de servico" na aba Inicio do frontend, com botao
  Atualizar, item, rota/cidade, quantidade, lucro estimado, risco/confianca e
  explicacao curta.
- Validacoes executadas: `py_compile` em `app.py`, `analyze.py`,
  `albion/gameinfo.py`, `albion/orders.py`, `albion/client.py`; 22 testes OK;
  chamada local a `/api/service-orders?regenerate=true` retornou 200 e 16
  ordens abertas.
- Proximo passo operacional desta sessao: deixar o servidor local rodando para
  manter a coleta automatica de killboard e mercado.

## 2026-06-11 - Correção operacional: ordens sem Caerleon (Codex)

- Corrigido `albion/orders.py` para que ordens simples de coletor/refinador
  nao recomendem Caerleon. A camada leiga agora respeita a diretriz operacional
  de nao usar rotas que exigem passar por zonas vermelhas.
- Adicionado teste sintetico em `tests/test_core.py` garantindo que uma ordem
  de coletor nao escolhe Caerleon mesmo quando Caerleon teria o melhor preco.
- Validacoes: `py_compile` OK; 23 testes OK.
- Tarefa `MercadoAlbion-Servidor` reiniciada e ordens recalculadas:
  `/api/service-orders/refresh` retornou 16 ordens e 0 ordens com Caerleon.

## 2026-06-11 - Complemento do plano: biomas, contas e teoria economica

- Ampliado `docs/PLANO_DADOS_PUBLICOS_ECONOMIA_AVANCADA.md` com auditoria de
  completude das fontes possiveis.
- Adicionada distincao entre oferta estrutural de bioma/tier, oferta observada
  via mercado e estado dinamico real do mapa. Foi registrado que quantidade
  exata de recursos encantados vivos "agora" nao deve ser prometida como dado
  publico limpo.
- Adicionados metodos economicos aplicaveis: microestrutura, arbitragem
  espacial, economia da producao, demanda derivada por destruicao, estudos de
  evento, series temporais, estoque e risco.
- Adicionado desenho de contas/perfis: identidade pseudonima, papeis
  administrativos, perfis economicos, interfaces por funcao, controle de
  dispositivo, auditoria e privacidade.
- Codigo da aplicacao continua intocado; mudanca apenas documental.

## 2026-06-11 - Complemento do plano: Trash to Cash e ordens de servico

- Ampliado `docs/PLANO_DADOS_PUBLICOS_ECONOMIA_AVANCADA.md` para cobrir as
  lacunas apontadas no trecho do usuario.
- Adicionado modelo "Trash to Cash": morte como evento economico de destruicao,
  loot, dano, reposicao e previsao de demanda. O documento orienta nao
  hardcodar o trash rate sem validacao e usar assumptions configuraveis.
- Adicionadas tabelas propostas: `asset_destruction_events`,
  `regear_demand_forecasts`, `demand_vacuum_alerts`, `death_clusters`,
  `death_cluster_items`, `death_cluster_entities`, `item_combat_tags`,
  `service_orders`, `service_order_feedback`, checkpoints/fila/jobs de
  ingestao publica.
- Detalhados detector de clusters de morte/ZvZ, classificador por arsenal,
  janelas de regear, backtesting de alertas, motor de ordens de servico,
  arquitetura para alto volume de JSON e politica comunitaria/RMT.
- Nenhum codigo da aplicacao foi alterado nesta etapa.

## 2026-06-11 - Killboard: ingestao publica e indice de destruicao (Claude)

Fases 0-2 do PLANO_DADOS_PUBLICOS_ECONOMIA_AVANCADA executadas.

- Fase 0: schema real do gameinfo validado ao vivo e documentado em
  docs/SCHEMA_GAMEINFO.md. Descobertas: Location 100% nulo na amostra (risco
  por zona deve usar KillArea/clusterName), limit<=51, offset<=1000 (~1.050
  eventos por varredura), inventario completo da vitima presente, killboard
  das Americas gera ~50 eventos/min em pico.
- albion/gameinfo.py: cliente com backoff, ingestao incremental idempotente
  por checkpoint de EventId/battle id (para quando a pagina so tem ids
  conhecidos), normalizacao em kill_events / kill_event_actors /
  kill_event_equipment (killer+vitima; inventario so da vitima; participantes
  sem equipamento para conter volume) / battle_summaries / battle_entities,
  auditoria em public_data_runs + public_ingest_checkpoints.
- destruction_top(): itens mais perdidos/usados por janela, com valor =
  unidades x menor venda q1 nas cidades reais (SEM ajuste de trash,
  documentado). Cuidado de corretude: cutoff com strftime %Y-%m-%dT%H:%M:%S
  porque o ts da API usa 'T' e datetime('now') usa espaco.
- CLI: analyze.py intel collect|top|status (--days fracionario, --role,
  --inventory). Endpoint GET /api/intel/top.
- Servidor: loop de cadencia dupla — killboard a cada 10 min
  (AUTO_INTEL_INTERVAL_MIN), mercado a cada 30 (AUTO_COLLECT_INTERVAL_MIN).
- UI: painel 'Demanda por destruicao' na aba Inicio (janela 6h/24h/7d,
  toggle inventario, caveat de amostra publica). Assets v=20260611-4.
- Teste sintetico de ingestao: normalizacao, dedup idempotente via checkpoint
  e ranking com preco conhecido (20 testes OK).
- Primeira coleta real: 228 kills / 6.257 linhas de equipamento / 102
  batalhas em ~4 min de janela; top: pocoes de crescimento, comida de guerra,
  bolsas — demanda derivada visivel imediatamente.

Proximos passos (fases 3+ do plano): agregacao diaria item_demand_daily com
z-score de destruicao, sinal divergencia demanda-preco na tela inicial,
clusters de morte (zvz/gank) com item_combat_tags, assumptions economicas
configuraveis (trash rate) e regear forecasts com backtest de alertas.

## 2026-06-11 - Fase 3: divergencia demanda x preco (Claude)

- Tabelas novas: item_demand_daily (agregado diario de destruicao por item,
  separando vitima/killer/inventario) e economic_assumptions (parametros
  configuraveis: trash_rate_base=0.30 e regear_capture_rate=0.25 gravados como
  'regra comunitaria nao validada', confianca baixa — NUNCA hardcoded como
  verdade, conforme o plano Trash to Cash).
- albion/gameinfo.py: aggregate_demand_daily() (reagrega ultimos 3 dias,
  idempotente, dias antigos congelados) e demand_price_divergence() — o
  sinal-chefe do plano: destruicao/dia recente vs base (5d) cruzada com VWAP
  diario q1 das cidades reais; flags: demanda >= 1.3x e preco <= 1.05x;
  demanda sem base marcada como 'demanda nova'.
- CLI: intel signals; intel collect agora reagrega e garante assumptions.
- Servidor: loop agrega apos cada ingestao; GET /api/intel/signals; painel
  'Demanda subindo, preco atrasado' na aba Inicio com estado vazio honesto
  (mostra quantos dias de historico ja existem). Assets v=20260611-5.
- Teste sintetico de ponta a ponta: 8 dias de kills (10/dia -> 30/dia) +
  preco flat => ratio 3.0 detectado, preco 1.0, volume 50/dia (21 testes OK).
- Validacao real: 534 kills ja no banco (ingestao automatica de 10 min
  funcionando sozinha), 2.486 linhas item-dia agregadas, primeiro sinal
  emitido com ressalva de base curta.

Proximos passos: clusters de morte (zvz/gank) com item_combat_tags, aplicar
trash_rate/regear nos valores do painel, z-score de destruicao com mais dias,
backtest de alertas de divergencia (mesma infra do backtest de flips).

## 2026-06-11 - Trash rate aplicado, log de sinais, validacao e risco (Claude)

- destruction_top() agora retorna valor_trash_estimado (valor x
  trash_rate_base lido de economic_assumptions; padrao 0.30 marcado como nao
  validado). CLI 'intel top' ganhou a coluna Pos-trash; tooltip na UI explica
  a taxa configuravel.
- Tabela demand_signal_log: o servidor persiste os sinais de divergencia
  1x/hora (dentro do loop, com lock para uso seguro da conexao) — e o
  pre-requisito do backtest de alertas prometido-vs-realizado.
- validate_signals(): retorno do preco apos o sinal (VWAP no dia D+1/D+3),
  hit rate, media/mediana/extremos. CLI 'intel validate' (N=0 ate os sinais
  envelhecerem — estrutura testada com sintetico: +10% detectado).
- risk_summary(): risco estrutural com o que e publico hoje — mortes por
  KillArea x hora UTC + batalhas grandes (>=15 kills) como proxy de ZvZ com
  guildas/jogadores/fama. CLI 'intel risk'. Validacao real: 782 mortes na
  hora 17 UTC (~70M de fama), 2 ZvZ detectadas (44 players/4 guildas;
  27 players/22 guildas).
- 22 testes OK (validate_signals sintetico +10%; trash 30% no top).
  Assets v=20260611-6.

Proximos: clusters formais com item_combat_tags e classificacao gank/zvz por
arsenal (tabela editavel, preenchimento conservador), paineis de risco e
validacao na UI, calibracao do trash_rate e do regear pelo proprio log.

## 2026-06-11 - Classificacao de mortes, tags de combate e paineis (Claude)

- Tabelas: static_items (catalogo espelhado no SQLite p/ joins do killboard;
  sync no startup e no intel collect) e item_combat_tags (editavel; seed
  conservador SO de montarias de carga por padrao de id — mula/boi/mamute;
  classificacao por arsenal fica para curadoria manual, como o plano exige).
- classification_summary(): classes estruturais por porte (gank_provavel
  <=2 participantes, small_scale 3-14, zvz >=15) + flags economicas: vitima
  com traje de coleta vestido (join static_items cat=gathering), com montaria
  de carga (tag) e com inventario 10+ unidades (interdicao de transporte).
- CLI 'intel risk' agora abre com a classificacao; endpoints novos
  GET /api/intel/risk e GET /api/intel/validate; paineis 'Guerra e risco'
  (classes + vitimas economicas + tabela de ZvZ) e linha de backtest dos
  alertas anexada ao painel de divergencia quando N>0.
- Validacao real (1.020 mortes/24h): 66,5% gank_provavel, 33,5% small_scale,
  40 vitimas coletoras, 39 com montaria de carga, 593 com inventario 10+ —
  o canal de interdicao de transporte e muito maior do que se imaginava.
- 22 testes OK; assets v=20260611-7; console limpo.

Proximos: curadoria manual de item_combat_tags por arma (gank/zvz por
arsenal), alertas de rota para coletores ('evite X por 2h'), calibracao do
trash_rate/regear quando validate acumular N, ordens de servico (service
orders) por perfil.

## 2026-06-12 - Correcoes da auditoria das ordens de servico (Claude)

Auditoria read-only identificou 2 achados altos + 6 medios no motor de ordens
integrado pelo Codex; todos os relevantes corrigidos nesta rodada:

- A1 (alto): gatherer_sell_orders agora usa buy_price_max (venda instantanea,
  ordem de compra real) em vez do menor anuncio — o anuncio absurdo de 999.999
  nao dispara mais missao inexecutavel para a camada leiga. Lucro reportado e
  liquido (pos-imposto). Teste atualizado cobre exatamente esse cenario.
- A2 (alto): refiner_orders aplica RRR de bonus (36,7%, assumption
  refining_rrr) SO na cidade com bonus da familia
  (config.REFINING_BONUS_CITY: FS/madeira, Lymhurst/fibra, BW/pedra,
  Martlock/couro, Thetford/minerio) e RRR base (15,2%) nas demais; a
  explicacao marca 'com bonus'/'SEM bonus'. Validado ao vivo: Lymhurst com
  bonus 12% vs SEM bonus 5%.
- B6: trader_orders com teto de capital (config.ORDER_MAX_CAPITAL=2M,
  configuravel) — qty limitada ao caixa; custo unitario acima do teto nao
  vira missao.
- B2: persist() transacional com rollback (UPDATE+DELETE+INSERT atomicos).
- B1: retencao — expiradas com 7+ dias sao apagadas no proprio persist().
- B4: GET /api/service-orders virou somente leitura; gerar so no POST
  /refresh (frontend ja usava POST no botao).
- B5: geracao automatica movida do tick de 10 min para a cadencia do mercado
  (30 min) — TTL de 4h volta a fazer sentido e a tabela nao infla.
- B3: divisao inteira no vol_dia corrigida (* 1.0).
- UX: aviso de risco emitido tambem para 'transportador'; painel ordena
  avisos de risco no topo, depois maior lucro; mensagem de vazio aponta o
  botao Atualizar. Assets v=20260612-1. .gitattributes para acabar com o
  ruido de CRLF nos diffs.
- Testes: 25 OK (3 novos: anuncio absurdo nao dispara coletor; teto de
  capital; RRR de bonus so na cidade certa).

ATENCAO operacional: o servidor da tarefa MercadoAlbion-Servidor continua
com o codigo antigo ate ser reiniciado (reiniciar a tarefa ou o run.bat).

## 2026-06-12 - Verificacao completa "sem erros" (Claude)

Varredura integral a pedido do usuario; 2 bugs reais encontrados e corrigidos:

- /api/backtest retornava 500 no servidor: a conexao do app usa
  row_factory=Row e sorted() nao ordena objetos Row (a CLI usava tuplas e
  funcionava). Fix em backtest._runs (tuple()) + teste de regressao com Row.
- /api/collect SEMPRE falhava com 422 nos defaults: max_items default 2500
  vs limite le=600 esquecido quando o teto subiu — o botao 'Coletar
  watchlist' estava quebrado. Fix: le=COLLECT_MAX_ITEMS + teste de regressao
  via OpenAPI que valida default<=maximum em TODOS os parametros de TODOS os
  endpoints (pega a classe inteira de bug).

Resultado da varredura: py_compile 13 modulos OK; node --check OK; 25 testes
OK; 24/24 comandos da CLI OK; 21/21 endpoints GET 200 + POST refresh/collect
OK; coleta fresca de 2.157 itens (86.280 precos, 10.298 series); os 7 paineis
do Inicio populados com dados reais; console do navegador sem erros; git
limpo. Tarefa MercadoAlbion-Servidor reiniciada com o codigo novo (porta
8528 escutando, /api/backtest 200 em producao).

## 2026-06-12 - Consolidação de abas: busca agrupada + inspetor de item (Claude)

Análise pedida pelo usuário: redundância entre abas + busca não alcançava
todos os itens. Confirmado e corrigido.

Bug da busca (grave): cada item virava 5 entradas (@0-@4) e o limite de 80 as
contava, escondendo a maioria — 'arco' só alcançava 19 de 49 itens-base.
- ItemDB.search(group=True) colapsa por item-base e aplica o limite a bases;
  /api/search?group=. Picker mostra 1 linha/item com chips .0-.4 que escolhem
  o encanto exato. (commit e28b044)

Consolidação A (commit eba997d): 'Descobrir flips' duplicava /api/recommendations
com 'Recomendações' do Início. Virou um <details> de filtros no próprio painel
do Início; aba Flips ficou só com a calculadora manual.

Consolidação B (este commit): Preços + Onde Vender + Item Lab eram 3 abas com o
mesmo gesto 'escolha 1 item'. Unidas numa aba 'Item' com 1 picker compartilhado
e 3 sub-abas (Preços por cidade / Onde vender / Item Lab); escolher o item uma
vez popula as três, carregando sob demanda (state.itemLoadedFor). Pickers
precos/vender/hist removidos; selectItem/showItemSub/loadItemSub novos;
selectPrecosItem (favoritos) e openHistoryForOpportunity repontados.
Resultado: 6 abas -> 4 (Início · Item · Flips · Scanner).

Verificado ao vivo: busca 'arco' alcança 49 bases; chip .2 seleciona
T4_2H_BOW@2; inspetor carrega as 3 facetas do mesmo item; console limpo;
28 testes OK; assets v=20260612-4.

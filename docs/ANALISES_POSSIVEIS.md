# Análises possíveis com os dados atuais — backlog validado

> **STATUS (16/06/2026): ~22 destas análises já foram CONSTRUÍDAS** na CLI
> (`analyze.py`), em 7 levas commitadas, nos módulos `albion/microstructure.py`,
> `production.py`, `logistics.py`, `risk.py`, `forecast.py`, `demand.py`,
> `guild.py` — cada uma com testes (46 no total). Comandos novos: `micro`,
> `prod`, `logi`, `risk`, `fc`, `demand`, `guild`. Ficaram DOCUMENTADAS para
> depois as que precisam de mais histórico fino (meia-vida de spread, nowcast,
> elasticidade, timing intradiário), de `positions` preenchida (atribuição de
> PnL), de modelo de transporte (cidade-bônus por cadeia, pegada logística) ou
> são pesadas (regime estrutural, lista-de-guerra recursiva). Próximo passo:
> expor as melhores na interface web (hoje são CLI / `--format json`).

> Gerado por workflow multiagêntico (15/06/2026): 7 lentes econômicas geraram 40 ideias; 7 validadores céticos aterraram cada uma nas tabelas/arquivos reais. **39 viáveis e novas, 0 vaporware, 1 já existia.** Cada item traz método concreto (tabela.campo + SQL/fórmula), dificuldade de implementar e valor pro jogador.

## ⭐ Ganhos rápidos (alto valor, fácil)

- **Spread efetivo bid-ask por cidade e o cardápio de market-making** — Lista onde postar ordem de compra E de venda no MESMO mercado e embolsar o spread liquido, sem viajar nem pagar transporte.
- **Eficiencia de foco: ranking de prata-por-foco da cadeia inteira (refino + craft)** — Unifica refino e craft num so ranking prata/foco para decidir onde gastar o foco escasso do dia.
- **Consumo de consumíveis por kill → giro real de poções/comida** — Estima a queima diária de cada poção/comida em unidades e prata e sinaliza onde o consumo supera o volume de mercado (poção subofertada, preço pronto pra subir).
- **Priorização de coleta da watchlist por ROI de informação (destruição não-monitorada)** — Ranqueia itens de alta destruição real que estão FORA da watchlist para `watch add`, fechando o ponto cego da coleta — gap confirmado em 5.818 itens.

## Arbitragem & Logística

### Carga ótima por viagem (knapsack peso×lucro por rota)  
`high` · `medium`

Transforma a lista de flips numa lista de compras concreta: o que e quanto enfiar na mula para encher os kg sem estourar a liquidez do destino.

- **Método:** 1) Reusar albion/flips.compute_flips(buy_city,sell_city) -> candidatos com profit/un e weight (já vem de items_db campo w, presente em 11633/12066 itens; T4_MAIN_SWORD w=5.1). 2) Teto de qty por item = volume diário de history (time_scale=24, SUM(item_count)/dias dos últimos 7d) × CAPTURE_RATE 0.20 (mesmo piso de scan). 3) Knapsack: greedy fracionário por profit_per_kg OU 0/1 via DP discretizando peso em passos de 1kg sujeito a W_max. 4) W_max é PARÂMETRO do usuário (não está nos dumps — items_raw não tem carryweight/carrycapacity; item_combat_tags só marca a montaria 'transport', não a capacidade em kg). Saída: cesta {item,qty,peso,lucro} + lucro/viagem + preço-sombra do último kg.
- **⚠ Limite de dado:** Capacidade de carga (kg) das montarias não existe nos dumps — precisa ser parâmetro livre do usuário (default plausível, ex 1500kg do boi, hardcoded ou editável)
- **Nota:** compute_flips já calcula profit, weight e profit_per_kg por rota (albion/flips.py:209-229) — toda a matéria-prima existe. O que NÃO existe: a otimização de cesta (knapsack) sujeita a teto de peso E a teto de liquidez por item simultaneamente; hoje o app só ordena flips por profit ou profit/kg em colunas. É a decisão real do trader e está genuinamente faltando. Implementação isolada (CLI nova + função de DP), não toca o motor existente. difficulty=medium pelo DP e pela integração de volume.

### Janela de oportunidade por idade do dado (frescor desbalanceado entre cidades)  
`high` · `medium`

Desconta o lucro prometido pela probabilidade real de a ordem ainda existir (curva de sobrevivência do survival vira multiplicador), separando flip real de flip-fantasma antes de viajar.

- **Método:** albion/survival.persistence() já produz a curva: P(sobrevive) por bucket de idade (AGE_BUCKETS) e por lado (sell/buy), pareando coletas consecutivas em price_snapshots e checando se preço-topo + *_date persistiram. flips.py já calcula buy_age_min/sell_age_min e confidence (flips.py:185-210). O novo: pegar rate_pct da curva survival por bucket de idade de cada lado, e profit_ajustado = profit × P_buy × P_sell; expor gap_frescor=|buy_age-sell_age|. price_snapshots tem ~5 dias / 2777 buckets — base suficiente para a curva (já usada hoje em produção pelo comando survival).
- **Nota:** Genuinamente novo: survival.py mede persistência mas NUNCA realimenta o motor de flips — flips usa só uma confidence_label categórica por _age_score (flips.py:39-73), não a probabilidade empírica. A ideia transforma a curva descritiva em multiplicador de lucro-esperado por rota. Toda a mecânica (curva + idade por lado) já existe em peças separadas; falta só o casamento. difficulty=medium pela necessidade de cruzar a curva (que é global por bucket) com cada rota e de definir granularidade do bucket (idade × tier/valor amplia muito). value=high: ataca o risco central da arbitragem (ordem velha que já sumiu).

### Mapa de demanda de reposição por rota de transporte (destruição → onde abastecer)  
`high` · `medium`

Funde o índice de destruição do killboard com spread inter-cidade + peso: uma fila priorizada de 'compre X em A, leve pra B, o servidor vai precisar repor porque morreu em massa', com lucro/kg.

- **Método:** item_demand_daily existe e é denso (23090 linhas, 7005 itens; colunas victim_units/inventory_units verificadas) — fonte de demanda de reposição. 1) Top itens por victim_units recente. 2) Por item: cidade mais barata (min sell_price_min em prices) + melhor venda (reusar albion/flips.where_to_sell). 3) lucro líquido/un (taxas reais) e lucro/kg (items_db w). 4) Ordenar por demanda_recente × lucro_por_kg. SQL join item_demand_daily × prices + Python ranking.
- **Nota:** Verifiquei a sobreposição com o que existe: albion/gameinfo.demand_price_divergence (endpoint /api/intel/signals) detecta 'destruição subindo + preço atrasado' MAS é item-a-item e NÃO tem camada logística — não diz de qual cidade comprar nem lucro/kg de transporte (confirmado lendo a função, linhas 350-423: só compara demanda vs VWAP, sem cidade de origem nem peso). where_to_sell existe mas não cruza com demanda de killboard. A fusão demanda-PvP -> rota logística com peso é genuinamente nova. value=high: conecta duas telas que hoje vivem separadas e responde 'o que carregar E de onde'. difficulty=medium pela integração entre killboard e flips. Caveat menor: victim_units é piso (killboard ~50 ev/min em pico, ingestão a cada 10min) — é sinal de direção, não contagem exata.

### Arbitragem por qualidade dentro da mesma cidade (upgrade ladder)  
`medium` · `easy`

Mede o prêmio de prata por degrau de qualidade (q1->q5) e revela quando comprar a qualidade barata que ainda satisfaz a ordem-alvo (regra BM q_item>=q_pedido).

- **Método:** prices tem coluna quality (1-5) e sell_price_min/buy_price_max por (item,city,quality). Self-join/pivot por quality: 1782 pares (item,city) já têm >=2 qualidades com sell_price_min>0 (SQL verificado). premio_abs[q]=sell_min[q]-sell_min[q-1], premio_pct[q]=premio_abs/sell_min[q-1]; z-score do prêmio entre itens da mesma sub/tier (items_db cat/sub/tier/maxq). Estabilidade do prêmio via price_snapshots (variância sobre ~2777 buckets). Caso BM: flips.py:150 já implementa que item q_item preenche ordem q<=q_item — comparar custo da q-baixa real vs entrega na ordem BM.
- **Nota:** Confirmado: o motor de flips/where_to_sell trata cada (item,quality) como universo separado e NUNCA compara entre qualidades do mesmo item (flips.py agrupa por (item_id,quality)). Esta é a dimensão vertical inexplorada. Dado denso e suficiente. difficulty=easy: é essencialmente um SQL de pivot + z-score, sem motor novo. value=medium porque o ganho prático depende de haver ordens BM/compradores aceitando q>=pedido com spread real — útil mas nicho frente ao flip inter-cidade.

### Prêmio de contrabando do Mercado Negro vs custo de rota (Caerleon)  
`medium` · `medium`

Isola o prêmio real do Mercado Negro sobre a melhor venda nas cidades, aplicando a taxa de contrabando correta (smugglersetupfee 1,5%) que o flips hoje ignora, e ordena por prata/kg.

- **Método:** prices tem city='Black Market' com buy_price_max>0 em 3164 linhas (verificado) — dado real existe. premio_bm_pct = net_BM / max(net_venda 5 cidades) - 1, com net_BM = price×(1 - 0.08 - 0.015). gamedata.json:2874 confirma smugglersetupfee=0.015 e smugglertransactiontax=0.08 (vs flips.py que usa SETUP_FEE genérico 0.025 — config.py:40). Filtrar combate via config.BLACK_MARKET_CATEGORIES; peso de items_db w. Risco temporal: kill_events.ts agregado por hora UTC (32842 eventos, ~5 dias) como proxy — porém kill_area é 100% OPEN_WORLD e location é nulo (verificado), então NÃO dá granularidade espacial Caerleon: só intensidade global de PvP por hora.
- **⚠ Limite de dado:** Risco espacial específico de Caerleon não é mensurável: kill_area só tem OPEN_WORLD e location vem nulo — o 'risco da rota a Caerleon' fica reduzido a um proxy de hora-do-dia global de PvP, não da zona vermelha de Caerleon
- **Nota:** Achado de bug real: flips.py aplica setup_fee genérico 2,5% no lado BM, mas o jogo cobra smugglersetupfee 1,5% — gamedata.json já tem o valor (linha 2874) e nenhum código o lê (grep 'smuggler' em *.py = 0 hits). Corrigir isso é correção de fidelidade independente da análise. O 'prêmio BM por categoria/tier' não é medido em lugar nenhum hoje. value=medium (não high) porque a parte logística de risco é o ponto fraco — o único proxy é hora-do-dia global. A correção da taxa é a peça mais sólida/acionável.

### Convergência de preços pós-arbitragem (meia-vida do spread entre cidades)  
`medium` · `medium`

Mede em quantas horas o spread entre duas cidades tipicamente fecha, separando rota repetível de oportunidade-relâmpago que evapora antes de você chegar.

- **Método:** price_snapshots é série de 30 em 30 min (2777 buckets, ~5 dias). Verificado: 2721 (item,quality) já têm >=2 cidades com sell_price_min>0 E >=10 buckets distintos — base existe. Por (item,quality,cidadeA,cidadeB): série spread = buy_price_max[B]-sell_price_min[A] sobre fetched_at; detectar eventos (spread > limiar líquido de taxas) e contar buckets de 30min até cair abaixo do limiar -> meia-vida (mediana). Classificar persistente vs transitório. items_db sub/tier para agregar; history item_count para filtrar liquidez mínima.
- **⚠ Limite de dado:** Janela curta: só ~5 dias de snapshots (SNAPSHOT_RETENTION_DAYS=7 e price_snapshots_daily ainda VAZIA) — meia-vida de spreads lentos (>1-2 dias) fica mal estimada; estatística robusta de meia-vida pede mais histórico fino do que existe hoje
- **Nota:** Distinto de survival (mede ordem individual) e backtest (lucro realizado de um sinal): aqui o objeto é a vida do SPREAD cidade-a-cidade, que ninguém mede. Conceitualmente sólido e dado existe. value=medium (não high) por duas razões: (1) só 5 dias de série fina limita a confiança da meia-vida; (2) com cadência de 30min, spreads que fecham em <30min são invisíveis. Honesto rotular como heurística de curto prazo até price_snapshots_daily acumular. difficulty=medium pelo pareamento de séries por cidade + detecção de eventos.

## Microestrutura & Market-making

### Spread efetivo bid-ask por cidade e o cardápio de market-making  
`high` · `easy`

Lista onde postar ordem de compra E de venda no MESMO mercado e embolsar o spread liquido, sem viajar nem pagar transporte.

- **Método:** Por (item,city,quality) usando o ULTIMO snapshot de cada chave (price_snapshots JOIN subquery 'SELECT item_id,city,quality,MAX(fetched_at) GROUP BY ...' das ultimas 24h): spread_liquido = sell_price_min*(1-SALES_TAX-SETUP_FEE) - buy_price_max*(1+SETUP_FEE), com SALES_TAX_PREMIUM=0.04/NO_PREMIUM=0.08 e SETUP_FEE=0.025 (albion/config.py:38-40). spread_pct = spread_liquido/buy_price_max. Anexa giro = mediana de history.item_count (time_scale=24, mesma city, 7d) e tempo de fila via survival.persistence(con,item,city,quality). Ordena por spread_liquido*liquidez_dia.
- **Nota:** Inedito: flips/sell/craft sempre cruzam cidades ou usam instantanea; nenhum endpoint (app.py) nem aba (web) faz market-making intra-cidade. As entradas 'Spread' em web/index.html:486 sao so glossario. Dados aterrados: 24h tem 84.032 linhas com ambas as pontas, das quais 63.999 (76%) tem spread NET positivo; pelo join 'ultimo snapshot por chave' restam 2.237 combos com spread liquido>0 AGORA = cardapio real, porem moderado. ATENCAO HONESTA: o spread liquido por unidade e fino na maioria dos itens liquidos (imposto+setup comem ~9% round-trip), entao o valor vem do GIRO, nao da margem; o risco de fila (survival ja existe) domina. SQL com CTE (WITH) e bloqueado pelo guard do comando 'sql' (somente SELECT simples), mas o codigo real (app/CLI) usa conexao crua sem esse guard, entao nao e blocker. difficulty=easy: reusa flips taxes + survival + history.

### Capital de giro ótimo e velocidade de capital por oportunidade  
`high` · `hard`

Dado X de prata, diz quais N ordens postar para maximizar lucro/DIA (ROI sobre capital), nao lucro/unidade — a metrica que um market-maker realmente otimiza.

- **Método:** Sobre o cardapio da ideia 1: capital_por_lote = buy_price_max*(1+SETUP_FEE)*lote; giro_capturavel = min(item_count_dia compra, item_count_dia venda)*CAPTURE_RATE(0.20, config.py:168); tempo_ciclo = persistencia (survival) + gap mediano de coleta (collection_runs: auto a ~30min, avg_dur 75s). velocidade_capital = lucro_liquido_ciclo / capital_lote / tempo_ciclo_dias. Alocacao: guloso/fracional (mochila) ate esgotar ORDER_MAX_CAPITAL (=2.000.000, config.py:163) ou input, 1 ordem por item. Reporta ROI/dia marginal do proximo item fora do orcamento.
- **Nota:** Inedito e e a pergunta de MM que recommend NAO responde (recommend usa CAPTURE_RATE fixo e ancoras de liquidez, nao resolve alocacao de capital escasso nem turnover). Todos os insumos existem: config (ORDER_MAX_CAPITAL, CAPTURE_RATE, SETUP_FEE), history (giro), survival (tempo de fila), collection_runs (cadencia confirmada: auto=60 rodadas ~30min). difficulty=HARD: depende de construir antes a ideia 1 (cardapio MM) E de estimar tempo_de_ciclo combinando persistencia + gap, que sao estimativas soft/ruidosas com so ~5-6 dias de serie; a mochila em si e trivial, o risco esta na qualidade dos inputs de giro/fila. value=high porque transforma margem bruta em retorno sobre capital, exatamente o que falta. Recomendo entregar 1 antes de 5.

### Profundidade e largura do livro a partir de min/max das duas pontas  
`medium` · `easy`

Usa sell_price_max/buy_price_min (hoje armazenados e ignorados) para dizer se o topo do livro e denso ou casca fina e quanto da pra escoar antes de mover o preco.

- **Método:** Por (item,city,quality) no ultimo snapshot: largura_venda_pct=(sell_price_max-sell_price_min)/sell_price_min; largura_compra_pct=(buy_price_max-buy_price_min)/buy_price_max. Volatilidade do topo = STDDEV de sell_price_min sobre os snapshots das ultimas 24h (indice idx_price_snapshots_time em server,fetched_at). Fundura = history.item_count_dia/(1+largura_venda_pct). Tudo agregacao SQL.
- **Nota:** Campos confirmados POPULADOS em 24h: 151.884 linhas com sell_price_max>sell_price_min e 77.931 com buy_price_max>buy_price_min — dado suficiente. Nenhuma analise atual usa esses campos (so sell_price_min/buy_price_max sao usados em flips/recommend); 'Profundidade'/'Slippage' em web/index.html:500/496 sao glossario, nao analise. RESSALVA METODOLOGICA: a 'largura' min/max NAO e profundidade real do livro (a AODP nao expoe quantidades por nivel) — e so um PROXY de dispersao do topo; a ideia ja chama isso de 'proxy', mas o valor depende de o usuario entender que nao e ordersize verdadeiro. difficulty=easy (SQL puro). value=medium: ajuda a dimensionar lote, util mas secundario.

### Detector de ordens-armadilha: livros cruzados, travados e isca de preço  
`medium` · `medium`

Marca cada ordem do topo como executavel/suspeita/travada/fantasma para flips e recommend nao apontarem para precos que somem antes de voce chegar.

- **Método:** Tres sinais sobre price_snapshots: (1) CRUZADO: sell_price_min<=buy_price_max na mesma (item,city,quality) no ultimo bucket -> confirmado 1.154/84.032 ~1,4% em 24h. (2) OUTLIER: z-robusto (mediana+MAD) de sell_price_min do item/quality contra as outras 7 cidades no mesmo fetched_at >3.5. (3) FANTASMA: taxa de sobrevivencia condicional reusando o pareamento de albion/survival.py (_epoch, MIN_GAP_MIN=10/MAX_GAP_MIN=720, compara (preco,*_date) entre t e t+1). Score_armadilha = combinacao; anexavel a flips/scan/recommend.
- **Nota:** O survival ATUAL (albion/survival.py) so agrega persistencia por FAIXA DE IDADE da ordem, em massa — nao classifica a ordem INDIVIDUAL no momento da decisao, exatamente o gap que a ideia preenche. Inedito (sem endpoint/aba). Dados confirmam: 1.154 livros cruzados/24h, 7 cidades para o cross-section de outlier. difficulty=medium: o z-robusto cross-city e o pareamento t->t+1 por ordem exigem codigo novo (nao e so SQL agregado); o sinal fantasma reutiliza a logica de survival mas precisa rodar por-ordem. value=medium: melhora confiabilidade de telas existentes (camada de saneamento), nao gera oportunidade nova por si.

### Janela de spread por hora do dia (microestrutura intradiária dos snapshots)  
`low` · `easy`

Heatmap hora-UTC do spread liquido e da idade da contraparte para saber QUANDO postar ordem barata e vender caro — janela que o jogo nunca expoe.

- **Método:** Agrupa price_snapshots por hora UTC: strftime('%H', datetime(fetched_at,'unixepoch')). Por (item,city,hora): spread_liquido_medio e idade_mediana_das_pontas = (fetched_at - epoch(*_date))/60 reusando survival._epoch. history nao serve (time_scale=1 tem 1 item de teste, 6h vazio) — a UNICA fonte intradiaria e price_snapshots (cadencia ~30min). Reportar nº de buckets por hora (cobertura honesta).
- **⚠ Limite de dado:** Cobertura intradiaria insuficiente: so 17 das 24 horas UTC tem buckets e apenas 6 dias de serie (2.777 buckets) — coleta comecou no meio do dia; horas 'mortas' (madrugada UTC) tem zero ou poucos buckets, e 6 dias e pouco para isolar ciclo horario de ruido. Precisa a serie de snapshots amadurecer (retencao e so 7 dias em price_snapshots; o roll-up price_snapshots_daily nao guarda hora) — sazonalidade horaria robusta exige semanas de cobertura das 24h.
- **Nota:** Feasivel de IMPLEMENTAR (SQL trivial, difficulty=easy) e inedito, mas data_sufficient=false HOJE: a propria ideia admite '17 das 24 horas'. Pior: a retencao de 7 dias de price_snapshots significa que a serie horaria NUNCA acumula muito alem de 1 semana a menos que se crie persistencia horaria nova (price_snapshots_daily so guarda min/avg/max diario, perde a hora). value=low por enquanto — vira medium so se a coleta cobrir as 24h por varias semanas e/ou um agregado horario permanente for adicionado. Sem isso, o heatmap mostra padrao de COLETA (quando o coletor roda), nao padrao de MERCADO.

## Economia de Produção

### Eficiencia de foco: ranking de prata-por-foco da cadeia inteira (refino + craft)  
`high` · `easy`

Unifica refino e craft num so ranking prata/foco para decidir onde gastar o foco escasso do dia.

- **Método:** Para cada receita em recipes_craft.json E recipes_refining.json (ambas tem campo focus): margem_foco=sell_revenue(produto)-custo_insumos*(1-craft_rrr(cat,city,True))-fee; prata_por_foco=margem_foco/recipe.focus. ganho_do_foco=margem_foco-margem_sem_foco (RRR 53.9% vs 36.7% confirmado em craft_data.refining). Filtra por liquidity_day=min(history.item_count 7d insumo, produto). Agrega mediana por familia/categoria.
- **Nota:** Confirmado que cmd_refine NAO computa silver_per_focus nem usa foco (so cmd_craft em craft.py:87, e so 1 degrau). recipes_refining.json tem focus por degrau (T5_METALBAR focus=94). Baixa dificuldade: a formula de margem ja existe, falta unificar as duas fontes de receita e ordenar. Maior valor + menor custo da lente.

### PnL de cadeia vertical end-to-end (raw -> refino -> item) num unico extrato  
`high` · `medium`

Desce a arvore inteira de craft+refino e diz em cada degrau se compensa comprar pronto ou fazer, com um unico ROI raw->item.

- **Método:** Expansao recursiva: recipes_craft.json[id].inputs -> para cada input testa recipes_refining.json[input] (confirmado: T4_MAIN_SWORD->T4_METALBAR->T3_METALBAR->...->T2_ORE existe). DP bottom-up: custo_make(no)=sum(inputs: min(prices.sell_price_min, custo_make)*count)*(1-craft.craft_rrr(cat,city,focus)) onde craft_rrr=1-1/(1+0.18+craft_data.crafting[cat][city]+0.59*foco). custo_buy=prices.sell_price_min q1. Folha=raw sem receita. PnL=flips.sell_revenue(item_final,4%/8%+2.5%)-custo_raiz. Marca o degrau onde delta=custo_buy-custo_make<=0.
- **Nota:** Reusa albion/craft.craft_rrr e flips.sell_revenue ja existentes; recipes_*.json e craft_data.json tem tudo. /api/craft e cmd_craft hoje sao SO 1 degrau (linhas 52-90 craft.py, q1, mesma cidade) — esta ideia e a generalizacao recursiva que nao existe. Risco: precos q1 sparsos em alguns degraus intermediarios derrubam nos da arvore (tratar com 'cobertura insuficiente').

### Custo de oportunidade do coletor/refinador: vender o bruto vs subir o degrau  
`high` · `medium`

Diz se vende o minerio agora ou refina pra barra, com o premio de refino atual vs seu percentil historico de 90-180 dias.

- **Método:** ratio do recipes_refining.json (T5_METALBAR=3x T5_ORE+1x T4_METALBAR, confirmado). premio_pct=[sell_revenue(refinado)-custo_raw*(1-RRR)-fee]/custo_raw. Instantaneo via prices; serie via history.avg_price time_scale=24 (confirmado 187 dias p/ T5_ORE e T5_METALBAR, ts ISO text). Constroi serie DERIVADA do premio diario e roda albion/stats.analyze_history_series (linear_fit+residuals existem) -> z sobre residuos do spread. Sinaliza percentil/z + veredito 'refine'/'venda bruto'.
- **Nota:** Dado historico solido (6 meses, 187 dias para ambos lados). Reusa stats.analyze_history_series igual ao lab, mas sobre serie derivada — angulo genuinamente novo (ninguem cruza raw vs refinado no tempo). cmd_journals existe (mede salario de fama, nao spread de refino) entao a comparacao da ideia procede. Otimo custo-beneficio.

### Valor esperado do craft ajustado pela distribuicao de qualidade (nao so q1)  
`medium` · `medium`

Pondera o sell por q1..q5 pelas chances reais do jogo pra mostrar quanto o craft de itens caros era subestimado ao olhar so q1.

- **Método:** gamedata.CraftingQualityChances.QualityLevel weights=689/250/50/10/1 e ActionFocus.CraftingQuality.@bonus=50 CONFIRMADOS exatos. E[sell]=sum_q(weight_q/sum * sell_revenue(prices.sell_price_min[q])). prices tem quality 1-5. Margem_esp=E[sell]-custo*(1-RRR)-fee vs margem_q1 (atual, craft.py:69 usa so q1). Foco desloca distribuicao (+50 ao pool).
- **⚠ Limite de dado:** cobertura de preco em q4/q5 e rala: prices tem 2340 linhas q4 e 581 q5 com sell>0 (vs 4886 q1) — muitos itens nao terao preco em >=3 qualidades, exigindo gate de cobertura regra exata de como o bonus +50 de foco realoca os pesos de qualidade nao esta documentada no dump (so o @bonus=50 bruto) — vira aproximacao
- **Nota:** Premissa correta: craft.py compara so contra q1. Mas o ganho pratico depende de q4/q5 terem preco, e a cobertura e fina; roda so onde ha >=3 qualidades precificadas (a propria ideia admite). O efeito do foco na qualidade fica aproximado. Valor medio por isso.

### Economia de fazenda: insumo (semente/cria + racao) -> produto, em prata/dia de terreno  
`medium` · `medium`

Ranking de cultivos/criacoes por prata/dia de canteiro e prata/foco, com cria esperada (offspring) e racao ja descontadas.

- **Método:** items_raw.farmableitem (107 itens) CONFIRMADO: T3_FARM_OX_BABY.grownitem={@uniquename:T3_FARM_OX_GROWN,@growtime:158400,offspring:{@chance:0.84,@amount:1}}, consumption.food.@nutritionmax=480/@secondspernutrition=330/acceptedfood.@foodcategory=plants, @activefarmfocuscost=1000. yield=1+amount*chance; custo=preco_cria+racao(nutricao no growtime*preco/nutricao do food); receita=yield*sell_revenue(grown); prata_dia=(rec-custo)/(growtime/86400); prata_foco=/focuscost. prices tem os ids de farm (no watchlist).
- **⚠ Limite de dado:** cobertura de preco dos itens de fazenda e sparsa: prices mostra 2-7 de 40 linhas com sell>0 por item (so 1-3 cidades com cotacao viva) — ranking sai com muitos buracos mapear food consumido por @foodcategory ('plants') ao item de racao mais barato exige um cruzamento extra (foodcategory -> ids de plants) ainda nao derivado
- **Nota:** Ramo inteiro (farming) realmente inexplorado — nenhum comando/endpoint/tab toca (confirmado: 'farm' ausente de analyze.py). Estrutura do dump bate 100% com a ideia. Limitante e a liquidez/cobertura de preco dos produtos de fazenda no cache, que e baixa; e o nicho de jogadores que farmeia e menor. Feasible e original, valor medio pela cobertura.

### Cidade-bonus otima por cadeia completa, contando logistica de insumos entre cidades  
`medium` · `hard`

Matriz cidade-de-refino x cidade-de-craft mostrando ganho de refinar a barra em Thetford e craftar a espada em Lymhurst vs tudo num lugar so.

- **Método:** craft_data.refining[fam].city (ore->Thetford 0.40 especialidade) vs craft_data.crafting[cat][city] (sword->Lymhurst 0.15) — mapas em cidades distintas, confirmado. Para cada par (city_ref,city_craft): custo_barra=min(prices.sell_price_min[barra][city_craft], custo_refino em city_ref com craft_rrr da familia)+transporte; margem=sell_revenue(produto em city_craft com RRR de craft local)-custo_barra. prices tem as 8 cidades. Transporte = items_db.w (T4_METALBAR=0.51) como PROXY.
- **⚠ Limite de dado:** custo/risco de transporte real entre cidades (so existe peso items_db.w como proxy; nenhum modelo de frete/risco no codigo — flips.py menciona 'transport' so como combat_tag, nao custo de rota)
- **Nota:** Feasible mas a 'logistica' fica num proxy fraco (peso, sem risco de zona vermelha nem custo de viagem) — o numero de ganho da estrategia hibrida e indicativo, nao preciso. Valor medio: poucos crafters movem barras entre cidades na pratica. Complexidade O(cidades^2) por item + matriz de UI.

## Inteligência de Demanda (killboard)

### Consumo de consumíveis por kill → giro real de poções/comida  
`high` · `easy`

Estima a queima diária de cada poção/comida em unidades e prata e sinaliza onde o consumo supera o volume de mercado (poção subofertada, preço pronto pra subir).

- **Método:** kill_event_equipment WHERE slot IN ('Potion','Food') tem stack real: SUM(count) por item. Verificado: Potion 24.609 linhas/91.483 un, Food 19.047/45.605; topo T7_POTION_REVIVE 20.601 un/5.816 eventos, T8_MEAL_STEW 13.084 un. consumo_por_kill = SUM(count)/n_kills; consumo_diario = ×kills_dia; cruzar com history.item_count(time_scale=24) p/ taxa_cobertura<1 = subabastecido; ×avg_price = prata/dia.
- **Nota:** Único slot com stack>1 (junto de Inventory), então count importa de verdade aqui. O 'intel top' mistura tudo num índice único e usa quality=1 fixo; isolar Potion/Food com taxa de consumo recorrente é novo e direto (uma query + painel). Maior valor prático da lente: piso de demanda recorrente p/ crafter. Easy porque reusa history e o padrão de destruction_top.

### Qualidade do equipamento destruído vs prêmio de qualidade no mercado  
`high` · `medium`

Mostra que a demanda real de reposição é majoritariamente Q4+ e cruza com o prêmio de preço por qualidade, revelando quando vale craftar/estocar qualidade alta em vez de Q1.

- **Método:** kill_event_equipment.quality (1-5) está cheio e bem distribuído: verificado Armor Q4=14.439 un vs Q1=3.020 (Q4 domina em todos os slots de equip). Cruzar com prices(quality, sell_price_min) — 5 níveis armazenados, 5.953 linhas Q>1 com preço real vs 4.886 em Q1. premio_Qk = sell_price_min(Qk)/sell_price_min(Q1); indicador = share_destruicao_Qk × premio_Qk por família.
- **Nota:** Inédito e bem fundamentado: destruction_top usa quality=1 FIXO (verificado no SQL de gameinfo.py) e craft/refino só olham Q1 — a qualidade do que é destruído nunca foi cruzada com preço por qualidade. Ambos os lados do dado confirmados. Aproveita prices.quality, hoje subexplorado. Dificuldade média só pela nova UI/coluna; a query é direta. Alto valor: aponta onde craftar qualidade tem demanda garantida.

### Elasticidade demanda-destruição → preço (β por item)  
`medium` · `medium`

Mede quanto o preço de cada item reage à destruição, virando o sinal binário de divergência num ranking de 'o que estocar quando a destruição acelera'.

- **Método:** JOIN item_demand_daily(day, item_id, victim_units) com history(ts, avg_price, item_count WHERE time_scale=24, quality=1). Confirmado: 1.109 itens joináveis (item_demand_daily tem 7.005 itens; history time_scale=24 tem 1.668). Série diária log(victim_units) vs log(avg_price); OLS de Δlog(preço)_{t+1} ~ Δlog(demanda)_t → β,R². Com <=5 dias, usar Spearman e persistir β diário (tabela nova tipo demand_signal_log) p/ amadurecer.
- **⚠ Limite de dado:** Apenas 5 dias distintos em item_demand_daily (2026-06-11 a 06-16) — OLS de elasticidade é estatisticamente fraco; β será 'provisório'/relativo até a série passar de ~3-4 semanas
- **Nota:** Data REALMENTE existe e o join foi verificado. O 'intel signals' (demand_price_divergence em gameinfo.py) hoje só dá flag binário rising+lagging — quantificar β é genuinamente novo. Honestidade do método (Spearman, provisório, persistir) é apropriada dada a profundidade de 5 dias. Valor cai p/ 'ranking relativo' enquanto a série não cresce.

### Detector de mudança de meta (builds que sobem)  
`medium` · `medium`

Detecta qual build (arma+armadura) está ganhando participação nas mortes antes do preço dos insumos reagir, gerando lista de compra do conjunto emergente.

- **Método:** Self-join de kill_event_equipment (role='victim') por event_id: slot='MainHand' x slot='Armor'. Verificado: combo nº1 T4_MAIN_AXE@1 + T4_ARMOR_LEATHER_SET1@1 = 266 ocorrências (idea citava 227). share_familia = SUM(count)/total por slot/dia (normalizar item_id removendo tier/@ench via items_db.json). Δshare e z-score do share por dia.
- **⚠ Limite de dado:** 5 dias distintos e MAL distribuídos (dow=1/segunda tem 20.325 kills vs 1.6k-4.2k nos outros; quarta/sábado ausentes) — 'share subindo semana a semana' não é mensurável ainda; só dá foto do mix atual
- **Nota:** Co-ocorrência de equipamento NUNCA foi lida em nenhuma análise (destruction_top só conta itens isolados) — inédito de verdade. O self-join é barato e o ranking de combos atuais já é útil hoje. A parte 'meta em ASCENSÃO' (tendência) é que sofre com a janela curta. Dificuldade média pela normalização família + frontend.

### Composição de meta por papel (DPS/Healer/Tank) via dano dos participantes  
`low` · `medium`

Indica se a meta pende pra cura ou dano pelo healing/damage dos participantes, tentando antecipar demanda de cajados de cura.

- **Método:** kill_event_actors.damage_done/healing_done existem e SÓ estão populados em role='participant' (verificado: 78.523 participants, 65.464 com dano>0, 15.449 com cura>0; killer/victim têm 0). Classificar participant: healing_done>damage_done → healer, senão DPS; share diário healer vs DPS. Cruzar tendência de cura com destruição de kill_event_equipment família HOLYSTAFF/NATURESTAFF (de killer/victim).
- **⚠ Limite de dado:** Participants NÃO guardam equipamento (PK e ingestão confirmam: equip só p/ killer/victim) — a arma do participante healer/DPS não é joinável. O elo 'papel -> arma específica -> demanda' depende de proxy frouxo (assumir arsenal do killer). Só 5 dias p/ tendência de composição
- **Nota:** A própria ideia admite a limitação honestamente. O split healer/DPS por participante é calculável, mas o passo que dá VALOR (qual cajado comprar) repousa num proxy não verificável (killer ≠ participant). Sem ligar papel à arma, vira contexto de meta vago, redundante com o share de destruição de cajados (que a ideia 2/5 já capturam melhor e diretamente). Valor baixo pelo proxy frouxo, não por falta de dado bruto.

## Risco, Portfólio & Timing

### Perfil de risco do item: volatilidade, drawdown e VaR a partir do histórico de 6 meses  
`high` · `medium`

Da uma regua de RISCO absoluto (vol anualizada, max drawdown, VaR-1d) por item, transformando 'quanto lucro' em 'quanto posso perder se travar no bau'.

- **Método:** Sobre history.avg_price (time_scale=24, quality=1) por item_id x city com COUNT(DISTINCT substr(ts,1,10))>=120 (verificado: 268 series qualificam, NAO 53 como a ideia diz). retornos log r_t=ln(P_t/P_{t-1}); vol=pstdev(r_t)*sqrt(365); maxDD=min(P_t/max-ate-t - 1) (verificado T6_BAG history de 32.954 a 212.389 em 96 dias = drawdown real grande); VaR5%=percentil 5 dos r_t; downside dev para Sortino. Faixas absolutas de vol -> selo seguro/medio/especulativo. Nova coluna em cmd_recommend/cmd_scan e selo no Item Lab (renderItemLab/cmd_lab).
- **Nota:** CLAIM PARCIALMENTE FALSO: a ideia afirma 'nenhuma metrica dessas existe em stats.py (so linear_fit/residuals/z-score, confirmado)'. FALSO — albion/stats.py:208-213,274 JA computa return_volatility (pstdev dos retornos log diarios). POReM: (a) e so a vol diaria, NAO anualizada; (b) NAO ha drawdown, VaR, Sortino, nem selo de risco; (c) o valor return_volatility nem aparece para o usuario — cmd_lab (analyze.py:746-758) e o Item Lab nao mostram esse campo; web/index.html so tem 'Volatilidade' no glossario (linha 491), nada renderizado. Ou seja: o motor de vol diaria existe mas esta enterrado; drawdown/VaR/anualizada/selo sao genuinamente novos. Profundidade real do history: 187 dias (2025-12-11 a 2026-06-15), suporta a janela de 6 meses.

### Dimensionamento de posição ajustado a risco: quanto comprar de cada flip dado meu capital  
`high` · `medium`

Junta lucro, liquidez, persistencia de ordem e volatilidade num numero acionavel 'compre ate N unidades', reordenando o ranking por retorno por unidade de risco.

- **Método:** Reaproveita motores existentes: (1) liquidity_day = AVG(history.item_count 7d) ja usado em cmd_scan; Q_liq = liquidity_day * config.CAPTURE_RATE (0.2). (2) albion/survival.py:44 persistence(con,...) ja retorna rate_pct por faixa de idade (AGE_BUCKETS) para sell/buy — multiplicar Q_liq pela rate_pct do bucket tipico. (3) vol diaria da ideia 1 (stats.return_volatility). (4) profit_liq_un de albion/flips.compute_flips. Fracao tipo Kelly: edge_por_risco = profit_liq_un/(vol*preco); N = min(Q_liq*persistencia, capital*frac_kelly/preco_compra). Ordenar por profit*N. Campo de capital novo no scan/recommend.
- **Nota:** Todas as pecas existem e foram verificadas: survival.persistence (rate_pct por bucket), liquidity_day em scan, CAPTURE_RATE em config, flips.compute_flips. So a formula de sizing e nova. Depende da vol da ideia 1 (encadeamento), por isso medium e nao easy. Risco de implementacao: persistence varre price_snapshots (~5.4M linhas) — ja limitado por days e indice (server,fetched_at), mas chamar por item no loop do scanner pode ficar pesado; precisa agregar persistencia uma vez e reusar.

### Correlação e diversificação de carteira: quais itens andam juntos (e quais protegem)  
`medium` · `medium`

Heatmap de correlacao de retornos + alerta de concentracao do estoque + sugestao de itens descorrelacionados para baixar o risco total da cesta.

- **Método:** Pearson sobre RETORNOS log diarios (nao niveis) de pares de history.avg_price (q1, time_scale=24), alinhados por substr(ts,1,10), exigindo >=60 dias comuns (verificado: 1.097 itens tem >=60 dias; par T4_CLOTH x T4_ORE Caerleon = 93 dias comuns). Risco da cesta sqrt(w'Sigma w) usando vol da ideia 1 + matriz de corr. Indice de concentracao (pares >0.6) e candidatos a hedge (corr baixa/negativa). items_db.json para nomear/agrupar por cat/sub/tier. Heatmap novo na UI.
- **Nota:** Dado suficiente e verificado. indexes (analyze.py:1424) so faz VWAP de cesta base 100 — NAO faz covariancia, confirmado, entao nao existe. value=MEDIUM e nao high: e analise correta e diferenciada, mas o ganho pratico depende do usuario montar estoque grande e diversificado (publico de nicho); para o flipper medio que gira poucas unidades, e mais educativo que acionavel. Depende da vol da ideia 1 para o risco da cesta. Cuidado metodologico: correlacao de retornos AODP sofre com volume censurado e dias sem trade (NULLs) — filtrar so itens liquidos como a propria ideia diz.

### PnL e atribuição do portfólio: de onde vem (e some) meu lucro real, marcado a risco  
`medium` · `medium`

Painel de portfolio: PnL realizado vs aberto, atribuicao timing-vs-spread, ranking de posicoes por VaR e taxa de acerto por sinal originador.

- **Método:** positions (schema verificado em client.py:115: item_id,quality,qty,buy_price,buy_city,opened_at,sell_price,sell_city,closed_at,note) — o cmd_pos (analyze.py:1351) ja marca a mercado (buy_price_max liquido de imposto via sell_revenue) e da PnL aberto+realizado. NOVO: (1) atribuicao = decompor PnL aberto em 'movimento de preco desde a compra' (history.avg_price hoje vs no opened_at) vs 'spread capturado'; (2) VaR por posicao = qty*preco*VaR5% (ideia 1); (3) agrupar por source_signal cruzando com service_orders (item_id, expected_profit, source_signal, created_at — schema verificado em client.py:196).
- **⚠ Limite de dado:** positions esta VAZIA (0 linhas verificado) — todo o painel fica latente ate o usuario logar trades manualmente service_order_feedback esta VAZIA (0 linhas) — sem o realized_profit logado, a atribuicao 'qual sinal pagou' fica sem ground truth; ha 826 service_orders mas zero feedback Nao ha FK trade<->ordem: ligar positions a service_orders so por heuristica item_id+janela (a propria ideia admite 'se houver')
- **Nota:** feasible=TRUE mas data_sufficient=FALSE hoje: depende inteiramente do usuario alimentar positions (vazia) e idealmente service_order_feedback (vazia). cmd_pos ja faz mark-to-market e PnL realizado/aberto, mas SEM atribuicao, SEM VaR por posicao, SEM ranking de risco, SEM cruzamento com sinal e SEM UI (so tabela CLI) — confirmado, esses 4 sao genuinamente novos. value=MEDIUM: fecha o loop recomendacao->resultado (alto valor conceitual) mas so vale para o usuario disciplinado que registra trades; e o unico lugar com o trade REAL dele. Depende da vol/VaR da ideia 1.

### Timing intradiário: melhor hora UTC para comprar e para vender cada item  
`low` · `medium`

Curva de preco por hora UTC no Item Lab + badge 'comprar ~03h / vender ~14h' — timing tatico que a analise diaria nao enxerga.

- **Método:** price_snapshots.sell_price_min/buy_price_max, hora-UTC de strftime('%H',datetime(fetched_at,'unixepoch')). Agrupar por hora, mediana robusta, normalizar pela mediana do dia para empilhar os dias. Exigir n>=3/hora e amplitude>X%. Mini-grafico no renderItemLab. price_snapshots tem retencao de 7 dias (SNAPSHOT_RETENTION_DAYS) — analise de regime CURTO, rotular assim.
- **⚠ Limite de dado:** Cobertura horaria completa: price_snapshots so tem 17 de 24 horas UTC distintas (verificado) — o coletor automatico nao roda 24/7 (maquina do usuario nem sempre ligada), entao 7 horas do dia tem ZERO observacao n por hora x item e raríssimo: 3-6 obs/hora num item liquido (T6_BAG), insuficiente para significancia Janela de so ~5 dias confunde TENDENCIA com ciclo intradiario (T6_BAG 'sobe' de 51k as 12h para 229k as 21h, mas e drift dos 5 dias, nao ciclo repetido)
- **Nota:** FEASIBLE tecnicamente (dado existe a 30 min, 2.777 buckets / 5.4M linhas verificado — a ideia diz 2.469, ordem certa), mas data_sufficient=FALSE e value=LOW. A propria ideia admite '5 dias e pouco, confianca baixa/media', mas SUBESTIMA: alem de 5 dias curtos, faltam 7h de cobertura no dia e o sinal observado e mais drift que ciclo. Como nada disso se acumula (retencao 7 dias mata a serie fina), nunca vai amadurecer sem mudar a retencao. Honestamente: feature bonita mas fragil; entregar so com selo de confianca explicito e poucos itens passariam o filtro de robustez.

## Previsão & Econometria

### Reversão à média acionável: meia-vida + banda de equilíbrio por item/cidade  
`high` · `medium`

Transforma o z residual estático do lab num sinal temporizado: alvo de preço + em quantos dias a reversão paga.

- **Método:** Sobre history(time_scale=24, avg_price>0, item_count) por item_id×city×quality. Reusa albion/stats.linear_fit(prices) e residuals() (já existem, puro Python, sem numpy) para r_t = log(avg_price) destendência. AR(1) dos resíduos por OLS manual (mesma fórmula de linear_fit, x=r_{t-1} y=r_t): b = cov/var; meia_vida = -ln2/ln(b) válido se 0<b<1. alvo = exp(tendência_no_dia); banda = ±std(resid) (std() já existe). Confiança = history_quality_score (já existe) + nº de dias. Gate: dispara só se meia_vida<=7 E |z_score residual atual (já calculado em analyze_history_series)|>=1.5.
- **Nota:** Base de dados forte: 268 séries com 120+ dias e 4978 com 60-119 dias (history time_scale=24, 186 dias). A infra estatística JÁ existe — linear_fit/residuals/std/history_quality_score em albion/stats.py são exatamente o que o método pede, e o lab já expõe naive_forecast_next (latest+slope ± std_resid), que é o embrião disso. O método cita 'numpy.polyfit' mas numpy NÃO está instalado; o AR(1) tem que ser OLS manual como o resto do stats.py (trivial, 5 linhas). O que falta vs. hoje é só a meia-vida e o alvo/horizonte — o lab atual rotula caro/barato mas não dá VELOCIDADE de reversão. Extensão honesta e de alto valor. Difficulty=medium porque é novo cmd/endpoint + UI, não por dado.

### Par trading entre cidades: cointegração e desvio do spread normal  
`high` · `medium`

Mostra arbitragens de REVERSÃO (spread anormalmente largo que tende a fechar), não só de nível — o que flips/scan não distinguem.

- **Método:** Para cada item e par (cidadeA,cidadeB) com >=60 dias comuns em history(time_scale=24, quality=1): spread_t = log(avg_price_A)-log(avg_price_B) alinhado por substr(ts,1,10). OLS log(P_A)~log(P_B) por fórmula manual (lstsq não disponível: numpy ausente, mas regressão simples = a mesma de linear_fit); resíduo do hedge -> meia-vida AR(1) (b<1) p/ estacionariedade. z_spread=(spread_hoje-mean)/std. Gate: cointegrado (meia-vida finita curta) E |z_spread|>=2 E desvio bruto cobre 8% imposto + 2,5%×2 (config.SALES_TAX_NO_PREMIUM, SETUP_FEE) com folga. Ranqueia por z_spread × min(item_count das 2 pontas).
- **Nota:** Dado suficiente: 5246 séries item×cidade×qualidade têm >=60 dias; pares líquidos comuns existem (T4_ORE, T4_BAG têm 186 dias em 8 cidades). flips.py é 100% instantâneo (nenhuma referência a history/spread/cointegração — grep vazio), então isto é genuinamente novo e complementar. O 'teste de cointegração pobre-mas-honesto' (meia-vida AR(1) + razão de variância em vez de ADF) é apropriado para o nível de dado e dispensa statsmodels. numpy.linalg.lstsq citado não está instalado, mas a regressão bivariada é trivial em puro Python. Custo computacional: pares × itens pode explodir — precisa pré-filtrar por liquidez (já há base p/ isso). Alto valor: ataca o flip-fantasma na origem.

### Score de previsibilidade e confiança da previsão por item  
`high` · `medium`

Selo 'aqui a estatística ajuda / aqui é loteria' que serve de porteiro para os outros 5 ângulos — calar onde não há sinal.

- **Método:** Por item×city×quality combinar 4 medidas em [0,1] sobre history(time_scale=24): (1) autocorrelação lag-1 dos resíduos (residuals já existe) — alto=previsível, ~0=random walk; (2) R² da tendência linear (de linear_fit: 1 - SSR/SST); (3) history_quality_score (já existe, dias ativos+frescor+pontos); (4) estabilidade de volume = 1-CV(item_count). Score = média ponderada -> rótulo modelável/cautela/ruído. Backtest do gate: erro de forecast naive-drift (latest+slope, já em stats) vs persistência (latest) nos últimos dias.
- **Nota:** Dado e infra suficientes: as 4 componentes saem de funções que JÁ existem em albion/stats.py (residuals, linear_fit, std, history_quality_score) + history. Valor alto e meta: é a camada que falta em recommend/lab, que dão sinal sem dizer se o item é sequer previsível (séries rasas <30 dias = 6397 delas geram falso sinal). Funciona como gate barato dos ângulos 1-4. Difficulty=medium: a autocorrelação de resíduos e o mini-backtest naive-drift vs persistência são novos, e idealmente o score precisa ser plugado como filtro no recommend/lab (vários pontos). É o de melhor relação valor/risco da lente, junto com reversão e regime, porque honra a granularidade real do cache em vez de fingir previsão onde não há.

### Detecção de quebra estrutural / mudança de regime de preço  
`high` · `hard`

Protege todos os sinais de envenenamento pós-patch: marca quando a média/VWAP virou referência morta e qual janela ainda é confiável.

- **Método:** Por item×city×quality(q1) sobre history(time_scale=24, avg_price): varredura de breakpoint tipo Chow/CUSUM caseiro — para cada t candidato, ajustar OLS antes/depois (reusando linear_fit) e comparar SSR segmentado vs SSR único (estatística F = ganho de SSR). t* = argmax. Significância por permutação (embaralhar série N vezes, p-valor empírico do F máx — puro Python, sem scipy). Classificar quebra de NÍVEL (salto de mean), VOLATILIDADE (mudança de std de resíduos/item_count) ou TENDÊNCIA (mudança de slope). Janela válida = pós-última-quebra; flag para lab/recommend usarem só ela.
- **Nota:** Dado suficiente para as séries profundas: 268 itens com 120+ dias e ~5000 com 60-119 dias em history. O problema que ataca é real e estrutural — VWAP/z residual/SCORE_ANCHORS do recommend assumem que o passado recente representa o presente, e nada hoje detecta regime (grep por regime/breakpoint/quebra só achou .venv). Difficulty=hard: o teste de permutação é O(N²×perm) por série e precisa rodar sobre milhares de séries — exige caching/limitar a itens líquidos; classificar 3 tipos de quebra e religar o flag em lab+recommend é mexer em vários pontos. Sem numpy/scipy o p-valor por permutação é puro Python (mais lento, mas viável). Alto valor: é o 'porteiro temporal' que falta. Funciona melhor nos 268 itens fundos; nos rasos será inconclusivo (correto, não falha).

### Nowcast de curtíssimo prazo e lead-lag entre cidades (qual cidade move primeiro)  
`medium` · `medium`

Único uso preditivo da série fina de 30min: avisa 'a perna de venda vai cair, execute já' e qual cidade lidera.

- **Método:** Por item×quality alinhar log(sell_price_min) das cidades em buckets de 30min via CAST(ROUND(fetched_at/1800.0) AS INT) sobre price_snapshots (necessário porque fetched_at NÃO é grade limpa: ~88-91 timestamps distintos por coleta, um por batch). Cross-corr defasada (corrcoef manual) cidade-líder(t) vs seguidora(t+k), k=1,2,3. Nowcast: linear_fit (já existe) sobre últimos ~12 buckets, afirma direção só se |slope|>1.5×erro-padrão. Confiança degradada explícita.
- **⚠ Limite de dado:** price_snapshots só tem ~5 dias e o dia 2026-06-13 está AUSENTE (buraco confirmado em snapshots E em item_demand_daily) buckets irregulares: 12-1282 buckets/dia, não os ~48 esperados; após arredondar sobram ~63 buckets úteis p/ itens líquidos (T4_BAG/T4_ORE), insuficiente p/ lead-lag estatisticamente robusto SNAPSHOT_RETENTION_DAYS=7 corta a série fina — a janela nunca passa de ~1 semana
- **Nota:** feasible=true mas data_sufficient=false: a própria ideia admite a janela curta e o buraco do dia 13. Tecnicamente roda (T4_BAG tem 63 buckets×8 cidades alinháveis), mas 63 pontos para cross-corr em 3 lags + nowcast de 12 pontos é frágil — vira mais 'sinal exploratório' do que decisão. price_snapshots hoje só alimenta survival/backtest, então o uso é inédito. Valor cai para medium pela fragilidade estatística e pela retenção de 7 dias que impede acumular série. Honesto, mas o jogador deve tratar como heurística, não previsão.

### Demanda por destruição como indicador antecedente do preço (elasticidade defasada kills->preço)  
`low` · `medium`

Estima 'quanto a destruição de hoje antecipa o preço de amanhã' e ranqueia onde o killboard realmente lidera.

- **Método:** Painel diário por item: [victim_units_t de item_demand_daily, Δlog(avg_price)_{t+1} e Δlog(item_count)_{t+1} de history time_scale=24]. Regressão defasada Δlog(preço)_{t+1}=a+b·log(1+victim_units_t) por OLS manual (numpy.linalg.lstsq citado mas ausente). Como a série é curtíssima, usar pooling cross-section (regressão ENTRE itens no mesmo dia: itens mais destruídos subiram mais?) p/ ganhar graus de liberdade. Ranqueia itens por força do sinal. Confiança baixa explícita.
- **⚠ Limite de dado:** item_demand_daily tem apenas 5 dias distintos (2026-06-11,12,14,15,16; dia 13 AUSENTE) — série temporal por item praticamente inexistente p/ regressão defasada alinhamento temporal frágil: history e item_demand_daily compartilham o mesmo buraco do dia 13 victim_units extremamente concentrado num único dia (222k em 06-15 vs 16-46k nos outros) — distribuição instável domina qualquer ajuste
- **Nota:** feasible=true só na forma cross-section/pooling que a própria ideia propõe como plano B; a regressão temporal defasada por item é vaporware com 5 dias (4 pares t->t+1, e ainda com lacuna). O sinal 'intel signals' JÁ cruza demanda×preço — validate_signals (albion/gameinfo.py:444) faz hit-rate do alerta — mas é ratio + acerto/erro, NÃO elasticidade nem ranking por força preditiva, então a EXTENSÃO é legítima e nova. Valor=low por enquanto: com 5 dias qualquer 'elasticidade' é ruído; vira útil só após semanas de coleta diária. A própria ideia rotula como 'hipótese a acumular', o que é honesto. Difficulty=medium (dado já existe, é montar painel + OLS), mas o retorno hoje é quase nulo.

## Guild & Estratégico

### Priorização de coleta da watchlist por ROI de informação (destruição não-monitorada)  
`high` · `easy`

Ranqueia itens de alta destruição real que estão FORA da watchlist para `watch add`, fechando o ponto cego da coleta — gap confirmado em 5.818 itens.

- **Método:** LEFT JOIN item_demand_daily (victim_units/killer_units/inventory_units, 7.013 itens, 5 dias) contra watchlist por item_id -> flag on_watchlist. Score = SUM(victim_units)*sell_price_min * dias_ativos. WHERE on_watchlist IS NULL ORDER BY score DESC = lista ADD. Inverso (na watchlist, destruição~0 e volume~0) = lista DROP. Confirmado: 5.818/7.013 itens destruídos NÃO estão na watchlist; T3_MOUNT_HORSE (8.818 vu) e T4_MOUNT_HORSE estão FORA (T7_POTION_REVIVE está dentro).
- **Nota:** Premissa empiricamente confirmada e até MAIOR que o pitch (5.818 vs ~4.670 alegado). Todos os campos existem e estão preenchidos. 'intel top' lista destruição mas não cruza com watchlist nem produz ação de coleta — esta fecha o loop operacional (orçamento COLLECT_MAX_ITEMS=2500). Alto valor: melhora diretamente a cobertura que limita recommend/lab. Easy: 2 queries + 'watch add'. A melhor candidata da lente.

### Taxa de queima de consumíveis (poções/comida): demanda de reposição por hora-pico  
`medium` · `easy`

Curva horária de queima de poções/comida (pico real 20-23h UTC) cruzada com volume de mercado — cronograma de produção para o crafter de consumível.

- **Método:** JOIN kill_event_equipment (slot IN ('Potion','Food')) com kill_events por (server,event_id); strftime('%H', ke.ts) -> hora UTC; GROUP BY item_id, hora SUM(count). Confirmado: pico em 20-23h UTC (22h=22.754 un de poção). Cruzar com history.item_count (time_scale=24) e valorar com prices.sell_price_min q1. Cobertura ótima: Potion 89/101 e Food 114/123 itens com preço.
- **⚠ Limite de dado:** Profundidade temporal curta: killboard tem só 5 dias (2026-06-11 a 2026-06-16). A 'curva horária' agrega poucos dias/hora -> IC largo; o IC de Poisson da ideia é honesto mas o N é pequeno. Não invalida, mas rebaixa confiança. Unidades reais divergem do pitch: Potion=224.232 un (não 46k) e Food=98.377 — a ideia subestima; bom para o valor, mau para a checagem.
- **Nota:** A mais limpa da lente: join trivial, cobertura de preço alta justo onde importa (consumíveis), e demanda 100% recorrente. 'intel top --role killer' já lista consumíveis usados, mas SEM perfil horário nem razão queima/mercado — o ângulo horário é novo. Easy: é essencialmente uma query + uma view.

### Índice de cesta de regear (Soldier's Kit Index): custo de re-equipar a guild ao longo do tempo  
`medium` · `medium`

Generaliza 'indexes' para uma cesta COMPOSTA ponderada pela frequência real de uso no killboard — um CPI de guerra com attribution por sub-cesta.

- **Método:** Pesos w_i = count_i/total_slot de kill_event_equipment (top-K por slot). Série diária = SUM(w_i * avg_price_i) sobre history (time_scale=24, q1, ~6 meses desde 2025-12-11, já consumido por cmd_indexes:1438). prices guarda variantes @N (ex T7_POTION_REVIVE@1..@3) então a cesta enchanted casa. Normalizar base 100; attribution por cat de items_db.
- **⚠ Limite de dado:** price_snapshots_daily está VAZIA (poda não rodou; cache < 7d) — o 'tracking de curto prazo' citado não tem dado. Só o caminho via history funciona. Buraco de cobertura: muitas peças de arma/armadura frequentes no killboard não têm série em history (mesma limitação dos 901/5.364 com preço). A cesta tende a pender para consumíveis (onde há dado), enviesando o índice. Pesos vêm de janela de só 5 dias de killboard — estável o bastante para pesos, mas pouco para sazonalidade.
- **Nota:** cmd_indexes (analyze.py:1424) já faz índice base-100 VWAP, MAS com pesos uniformes por volume de mercado, não por frequência de killboard nem cesta composta multi-categoria com attribution. A generalização é real e não trivial (ponderação + decomposição). Medium.

### Contratos internos de fornecimento: preço justo guild vs mercado (make-or-buy)  
`medium` · `medium`

Define a banda de preço de transferência interna (piso=custo do produtor, teto=mercado) por item consumido, dimensionada pela queima do killboard — make-or-buy com números defensáveis.

- **Método:** Custo de mercado = menor prices.sell_price_min q1 nas cidades. Custo interno = SUM(insumos a sell_price_min)*(1-RRR) + fee, via craft_data.json (rrr_bonus_pct, focus_bonus_sum 0.59, specialty_bonus) e recipes_craft/refining — reusa albion/craft.py margins()/craft_rrr(). Banda = [interno, mercado]; tamanho do contrato = item_demand_daily.victim_units/dia.
- **⚠ Limite de dado:** Cobertura de preço dos insumos e do produto: para gear, só ~17% têm preço; muitos insumos folha (herbs/raw) fora da watchlist. A banda fica indefinida onde falta preço — funciona bem só para consumíveis/refinados bem cobertos. item_demand_daily mistura victim+killer+inventory; 'demanda da guild' é uma escolha de definição, não um dado direto (proxy de toda a região, não da guild específica).
- **Nota:** O motor de margem (craft/refine) já existe; o que falta é a lente de transferência (piso/teto/excedente repartido), que é nova. Conceito de preço de transferência é sólido. Medium: matemática simples sobre infra existente, mas depende de cobertura de preço que hoje é parcial. Valor médio — útil para guilds organizadas, nicho menor que a ideia 4.

### Lista de Compras de Guerra: cadeia de suprimento por composição de raide  
`medium` · `hard`

Vira a destruição observada no killboard numa ordem de compra de matéria-prima pronta para o quartel-mestre — mas o filtro de ZvZ proposto não funciona como descrito e o custeio tem buracos.

- **Método:** Agregar SUM(count) por kill_event_equipment.item_id (slot IN MainHand/OffHand/Head/Armor/Shoes/Cape) e expandir via recipes_craft.json -> recipes_refining.json (BFS recursiva até raw). Recipes já têm chaves com sufixo @N (6.001) e variantes _CRYSTAL/_HELL/_AVALON, então o item_id do killboard (ex T4_2H_FROSTSTAFF_CRYSTAL@3) casa direto. Custear com prices.sell_price_min (q1). craft_rrr via albion/craft.py:38.
- **⚠ Limite de dado:** Filtro ZvZ por n_participants é INVÁLIDO: kill_events.n_participants é CAPADO em 4 (max=4, 100% < 5 buckets). O único proxy de escala é group_members (groupMemberCount, max=20; só 631 eventos têm >=15). A ideia precisa trocar o campo. Cobertura de preço da gear destruída é fraca: só 901 de 5.364 itens distintos de equipamento têm sell_price_min>0 no cache (~17%). O custo da cesta de armas/armaduras sai cheio de zeros (mounts: 0/72 com preço). Recursos brutos folha (T7_MULLEIN, T8_MEAT etc.) frequentemente fora da watchlist — sem preço para somar a raiz.
- **Nota:** O motor de BFS de receita não existe (craft/refine atuais só descem 1 degrau). 'intel top' já dá o ranking de destruição mas NÃO expande BOM nem chega a raw. Premissa central da ideia (n_participants alto = ZvZ) é factualmente errada contra os dados — recuperável só com group_members, que limita a amostra ZvZ a ~631 eventos em 5 dias. Hard por exigir BFS recursiva + correção do proxy + lidar com cobertura esparsa de preço.

### Pegada logística da reposição: peso x valor da cesta de regear por rota e cidade  
`low` · `medium`

Cruza a cesta de reposição (killboard) com peso (items_db.w) e bônus de cidade (craft_data) para sugerir onde centralizar produção e nº de viagens.

- **Método:** Cesta = top itens por slot de kill_event_equipment. Peso = SUM(count_i * items_db.w) (campo w confirmado, ex T7_POTION_REVIVE). Densidade valor/peso via prices.sell_price_min. Origem por família = cidade de maior specialty_bonus em craft_data.json (refining.<fam>.city / crafting.<cat>.<cidade>). Viagens = ceil(peso/capacidade informada).
- **⚠ Limite de dado:** Cobertura de preço para o componente 'valor': mounts 0/72 com preço; gear ~17%. A densidade de valor sai parcial. Nenhum dado geográfico de rota: world.json é só índice nome<->id, SEM coordenadas nem conexões; KillArea/Location/cluster_name vêm nulos (100%). 'por rota' é impossível — só dá 'cidade-origem por bônus', não rota/distância real. Capacidade de montaria é input manual, não dado.
- **Nota:** Peso (items_db.w) e bônus de cidade (craft_data) existem e funcionam, mas o ângulo 'por rota' do título não tem lastro (sem geografia no dump nem no killboard). Vira essencialmente 'peso+densidade da cesta + cidade-bônus sugerida' — derivável, mas baixo valor incremental sobre ideias 1/3/5 (a cesta é a mesma) e muito dependente de input do usuário. Value low por ser o ângulo mais marginal e mais bloqueado por dados faltantes.

## Já existe na plataforma

- **Sazonalidade intradiária de destruição (hora UTC × dia da semana)** (Inteligência de Demanda (killboard)) — Parcialmente JÁ EXISTE: o perfil mortes×hora UTC já está calculado e na UI (intel risk), só rotulado como 'risco', não como timing. O reframe p/ multiplicador de timing é leve mas o elo causal 'pico de destruição -> melhor hora de listar' é fraco sem cruzar com price_snapshots por hora (que é a ideia da lente risco-portfolio). Valor baixo: o jogador já vê o pico; o passo acionável extra é pequeno e não validado.
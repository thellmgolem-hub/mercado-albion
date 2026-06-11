# Plano de dados publicos e economia avancada - Albion Online

Data: 2026-06-11

Objetivo deste documento: orientar a proxima IA/agente no desenvolvimento da
plataforma como um centro de inteligencia economica de alto nivel para Albion
Online, sem depender inicialmente de muitos usuarios inserindo dados manuais.
O foco imediato deve ser extrair o maximo de valor de dados publicos,
metadados estaticos e series historicas locais.

## Visao central discutida

A plataforma nao deve ser apenas uma planilha de precos ou scanner de flips.
Ela deve cruzar mercado, destruicao de itens, metadados de mapa, cadeias
produtivas, volume, risco, guerra, popularidade de builds e tendencias
temporais.

O usuario quer duas camadas de produto:

- Camada simples: recomendacoes claras para jogadores leigos, com conclusoes
  diretas do tipo "comprar", "vender", "produzir", "guardar" ou "evitar".
- Camada tecnica: ferramentas para traders e analistas, com series temporais,
  z-score, momentum, volatilidade, correlacao, eventos de guerra, demanda
  derivada, liquidez e backtesting.

O diferencial estrategico sugerido nesta conversa e usar dados publicos de
kills/batalhas como proxy de demanda real. Se um item aparece cada vez mais em
mortes, batalhas e builds populares, isso pode antecipar pressao de compra
antes do preco reagir totalmente.

## Principio de seguranca e legitimidade

Nao construir scanner/radar, nao ler memoria do cliente, nao injetar codigo,
nao automatizar input, nao revelar informacao que o jogador nao observou
normalmente e nao fazer scraping de ferramenta privada sem permissao.

Priorizar:

- APIs publicas acessiveis por HTTP.
- Dados estaticos open source.
- Dados de mercado AODP.
- Agregacoes estatisticas.
- Confirmacao humana quando houver qualquer captura local de tela no futuro.

O fato de a plataforma ser comunitaria e nao comercial ajuda na intencao, mas
nao muda a avaliacao tecnica: o metodo de coleta precisa continuar limpo.

## Fontes confirmadas e relevantes

### 1. Albion Online Data Project - AODP

Fonte: https://www.albion-online-data.com/api/

Dados publicos documentados:

- Precos atuais por item, cidade e qualidade.
- Historico agregado de vendas, com escalas 1h, 6h e 24h.
- Charts historicos.
- Ouro.

Limites documentados:

- 180 requisicoes por 1 minuto.
- 300 requisicoes por 5 minutos.
- URL com limite pratico de 4096 caracteres.

Uso economico:

- Preco atual.
- Spread compra/venda.
- Volume historico.
- Liquidez aproximada.
- Tendencia de preco.
- Inflacao via ouro.
- Backtesting de flips e producao.

Observacao: o AODP diz que o cliente so envia ordens que o jogador carrega no
mercado dentro do jogo. Logo, dado fresco depende de cobertura real dos itens e
cidades.

### 2. API publica gameinfo.albiononline.com

Endpoints verificados em 2026-06-11:

- https://gameinfo.albiononline.com/api/gameinfo/events?limit=1
- https://gameinfo.albiononline.com/api/gameinfo/battles?limit=1
- https://gameinfo.albiononline.com/api/gameinfo/search?q=Bridgewatch

O endpoint de eventos respondeu com:

- `EventId`
- `TimeStamp`
- `Killer`
- `Victim`
- `Participants`
- `GroupMembers`
- equipamento completo por slot
- comida, pocao e montaria
- item power medio
- guilda e alianca
- `KillArea`
- `Type`
- `Location` quando disponivel

O endpoint de batalhas respondeu com:

- id da batalha
- inicio, fim e timeout
- fama total
- total de kills
- jogadores agregados
- guildas agregadas
- aliancas agregadas

Uso economico:

- Medir destruicao e consumo de itens.
- Detectar builds populares.
- Detectar meta emergente.
- Medir intensidade de guerras.
- Inferir demanda futura por sets, comidas, pocoes e montarias.
- Criar indices de risco por zona/area quando a localizacao estiver presente.
- Cruzar aumento de uso com preco ainda atrasado.

Importante: a API e publica, mas o agente deve validar schema, paginacao,
limites e estabilidade antes de implementar coletor pesado. Usar cache local,
backoff e coleta incremental.

### 3. ao-bin-dumps

Fonte: https://github.com/ao-data/ao-bin-dumps

Contem dumps de arquivos binarios do jogo em formatos estruturados. Exemplos
visiveis no repositorio:

- `formatted/items.json`
- `formatted/items.txt`
- `formatted/world.json`
- `formatted/world.txt`
- crafting modifiers
- buildings
- varias tabelas estaticas do jogo

Uso economico:

- Catalogo robusto de itens.
- Categorias, tiers, encantamentos e familias.
- Mapeamento de nomes internos para nomes amigaveis.
- Receitas e cadeias produtivas, quando disponiveis.
- Metadados de mundo e zonas.
- Base para filtros parecidos com o mercado do jogo.
- Base para calculadoras de refino, craft, comida, pocoes e ilhas.

### 4. Ferramentas comunitarias de rotas Avalon

Referencias:

- Portaler: https://github.com/mawburn/portaler-core
- Albion Mapper: https://github.com/dignityofwar/albion-mapper
- ao-avalonian-roads: https://github.com/Tyrben/ao-avalonian-roads
- Avalon Atlas: https://github.com/Pililink/Avalon-Atlas
- AO-Noki Avalon Roads: https://github.com/AO-Noki/avalon-roads

Conclusao importante:

Rotas Avalon vivas parecem depender de contribuicao manual ou captura local do
que o jogador esta vendo. Nao encontrei uma fonte publica limpa e completa que
entregue "rotas abertas agora" sem alguem reportar.

Uso imediato sem manual:

- Usar atlas e metadados estaticos dos mapas.
- Classificar mapas por tier, recursos, tipo e potencial economico.
- Cruzar atividade de kills com regioes/areas quando a API publicar local.
- Criar heuristicas logisticas e de risco, mesmo sem rota viva.

Uso futuro com comunidade:

- Sala privada estilo Portaler/Albion Mapper.
- Registro manual de conexoes.
- OCR de screenshot com confirmacao humana.
- Importacao CSV/JSON voluntaria.

## Zona cinzenta: avaliar com cuidado

### OCR de screenshot

Ideia: o usuario tira screenshot ou usa captura local da tela; a ferramenta le
nomes de mapa/portal por OCR; o usuario confirma antes de salvar.

Potencial:

- Reduz digitacao.
- Facilita mapear Avalon sem scanner.
- Mantem decisao humana.

Risco:

- Moderado.
- Deve ser local, transparente, com opt-in e sem automacao de input.
- Nao deve ler memoria, pacotes ou dados nao visiveis.

Recomendacao: deixar para fase posterior. Se fizer, adicionar aviso de uso
responsavel e exigir confirmacao humana antes de persistir qualquer dado.

### Leitura passiva de trafego fora do AODP

O AODP cita entendimento historico de que "olhar e analisar" trafego pode ser
aceitavel quando nao ha modificacao/interferencia. Ainda assim, expandir isso
para mapa, zonas, entidades ou rotas e muito mais sensivel.

Risco:

- Alto.
- Pode ser interpretado como scanner/radar dependendo do dado extraido.

Recomendacao: nao implementar agora. A plataforma pode ficar muito poderosa
sem isso usando AODP, gameinfo e metadados publicos.

### Scraping de sites terceiros

Usar ferramentas publicas de terceiros sem API documentada pode ser fragil e
eticamente ruim.

Recomendacao:

- So integrar se houver API publica, permissao clara ou licenca compativel.
- Respeitar robots/termos/rate limit.
- Nao copiar dados privados de guildas ou comunidades.

## Dados de mapa que podem influenciar mercado

Mesmo sem rotas vivas, ha varias camadas uteis:

### 1. Biomas e abundancia de recursos

Se o catalogo de mundo permitir mapear biomas e recursos, podemos estimar
pressao estrutural de oferta:

- regioes com madeira tendem a alimentar madeira bruta/refinada;
- regioes com ore tendem a alimentar metal;
- regioes com fiber tendem a alimentar tecido;
- regioes com hide tendem a alimentar couro;
- regioes com rock tendem a alimentar pedra.

Isso pode ser cruzado com bonus das cidades reais e precos locais.

Pergunta economica:

"O bonus de uma cidade realmente se traduz em preco menor do produto refinado
ou em maior liquidez?"

### 2. Tipo de zona e risco

Com dados de kills:

- contar mortes por zona/area;
- contar mortes por horario;
- separar open world, mists, corrupted, Avalon, black zone, se o campo permitir;
- medir IP medio dos mortos;
- medir grupos/guildas envolvidas;
- estimar risco logistico por janela temporal.

Pergunta economica:

"Vale transportar hoje, neste horario, ou o premio de spread nao paga o risco?"

### 3. Guerra e pressao de demanda

Grandes batalhas consomem equipamentos, montarias, comidas e pocoes.

Indices possiveis:

- kills por hora;
- fama destruida por hora;
- guildas/aliancas mais ativas;
- regioes mais violentas;
- itens mais usados em batalhas grandes;
- itens mais perdidos por vitimas;
- composicoes de build vencedoras e perdedoras.

Pergunta economica:

"Quais familias de itens sobem em uso quando ha guerra intensa?"

### 4. Logistica estrutural

Mesmo sem Avalon vivo:

- distancia conceitual entre cidades;
- cidade de bonus de refino/producao;
- peso do item;
- valor por peso;
- spread por rota;
- risco aproximado por areas intermediarias;
- liquidez no destino.

Pergunta economica:

"Qual rota tem melhor lucro esperado por peso e por minuto, ajustado ao risco?"

## Dados de faccao, guildas e black zone

Nem todos os dados de faccao parecem ter endpoint publico confirmado. O agente
nao deve inventar endpoints. O caminho correto e:

1. Usar `gameinfo/search` para encontrar entidades publicas.
2. Validar endpoints de guild/player/battle/event um por um.
3. Persistir apenas campos necessarios.
4. Agregar por guilda, alianca, periodo e area.

Analises possiveis com dados confirmados:

- ranking local de guildas/aliancas por atividade de kill;
- intensidade de guerra por periodo;
- frequencia de batalhas grandes;
- itens de guerra mais usados;
- itens mais destruidos por guildas/aliancas ativas;
- demanda derivada por consumiveis;
- mudanca de meta em resposta a conflitos.

Analises desejadas, mas dependentes de validacao:

- estatisticas especificas de faccao;
- localizacao completa de todos os eventos;
- controle territorial da black;
- timers de objetivos;
- relacao oficial entre zonas, territorios e donos atuais.

Se nao houver API publica estavel para isso, tratar como futuro opcional ou
entrada manual/comunitaria.

## Modelos analiticos recomendados

### 1. Indice de destruicao por item

Objetivo: medir consumo real via mortes.

Entrada:

- eventos de kill;
- equipamentos da vitima;
- inventario da vitima quando disponivel;
- comida/pocao/montaria;
- preco atual e historico.

Metricas:

- aparicoes em mortes por dia;
- quantidade estimada perdida;
- valor estimado destruido;
- crescimento 24h/7d/30d;
- z-score de destruicao;
- destruicao por tier/encanto/qualidade.

Uso:

- detectar demanda real;
- antecipar reposicao de mercado;
- priorizar craft/refino de itens consumidos.

### 2. Indice de meta de builds

Objetivo: descobrir o que esta sendo usado com sucesso.

Entrada:

- equipamento de killers;
- equipamento de vitimas;
- participantes;
- resultado da batalha;
- IP medio;
- contexto de grupo.

Metricas:

- frequencia de item em killers;
- frequencia de item em vitimas;
- razao killer/vitima;
- win proxy por composicao;
- crescimento de uso;
- diversidade de builds.

Cuidados:

- Kill nao prova causalidade.
- Um item popular pode estar popular porque e barato, nao porque e forte.
- Grandes guildas podem enviesar a amostra.

### 3. Indice de pressao de guerra

Objetivo: medir quando o servidor esta consumindo equipamentos em escala.

Entrada:

- battles;
- events;
- guilds/alliances agregadas;
- total fame;
- total kills.

Metricas:

- kills por hora;
- kill fame por hora;
- batalhas grandes por dia;
- concentracao por alianca;
- crescimento de conflito por janela.

Uso:

- prever aumento de demanda de consumiveis;
- detectar oportunidades em equipamentos de guerra;
- ajustar estoques de guilda.

### 4. Indice de risco logistico

Objetivo: precificar risco de transporte.

Entrada:

- kills por area/zona quando disponivel;
- horario;
- tipo de zona;
- fama media das mortes;
- tamanho medio de grupo;
- rota escolhida ou heuristica de rota.

Metricas:

- mortes/hora;
- mortes por janela de horario;
- risco relativo contra media historica;
- risco por valor transportado;
- spread minimo exigido.

Uso:

- filtrar flips perigosos;
- calcular ROI ajustado ao risco;
- recomendar transporte em janelas mais seguras.

### 5. Divergencia demanda-preco

Objetivo: achar item em que a demanda publica subiu mas o preco ainda nao.

Entrada:

- indice de destruicao;
- indice de meta;
- preco atual;
- preco historico;
- volume vendido;
- spread e frescor.

Sinal:

- demanda subindo;
- preco lateral ou atrasado;
- volume suficiente;
- dado fresco;
- estoque de venda ainda barato.

Esse pode ser um dos sinais mais valiosos da plataforma.

### 6. Estudos de evento

Objetivo: medir impacto de guerras, patches, eventos e mudancas de meta.

Entrada:

- data do evento;
- janela antes/depois;
- precos;
- volumes;
- kills;
- batalhas.

Metricas:

- retorno percentual pos-evento;
- volume anormal;
- destruicao anormal;
- diferenca contra grupo de controle.

Uso:

- entender efeitos de patch;
- descobrir itens sensiveis a guerras;
- calibrar previsoes.

## Banco de dados recomendado

Nao substituir as tabelas atuais. Adicionar novas tabelas append-only e tabelas
derivadas.

### Tabelas de ingestao

`public_data_runs`

- id
- source
- started_at
- finished_at
- status
- rows_seen
- rows_inserted
- newest_remote_timestamp
- error

`raw_public_payloads`

- id
- source
- remote_id
- fetched_at
- payload_hash
- payload_json

Opcional. Util para auditoria, mas pode crescer muito. Se usar, aplicar
retencao ou compactacao.

`kill_events`

- event_id
- timestamp
- battle_id
- type
- kill_area
- location
- total_victim_kill_fame
- number_of_participants
- group_member_count
- killer_id
- victim_id

`kill_event_actors`

- event_id
- actor_role: killer, victim, participant, group_member
- player_id
- player_name
- guild_id
- guild_name
- alliance_id
- alliance_name
- average_item_power
- kill_fame
- death_fame
- damage_done
- support_healing_done

`kill_event_equipment`

- event_id
- actor_role
- player_id
- slot
- item_id
- count
- quality

Slots: MainHand, OffHand, Head, Armor, Shoes, Bag, Cape, Mount, Potion, Food,
Inventory quando aplicavel.

`battle_summaries`

- battle_id
- start_time
- end_time
- timeout
- total_fame
- total_kills
- cluster_name

`battle_guilds`

- battle_id
- guild_id
- guild_name
- alliance_id
- alliance_name
- kills
- deaths
- kill_fame

`battle_alliances`

- battle_id
- alliance_id
- alliance_name
- kills
- deaths
- kill_fame

### Tabelas de metadados

`static_items`

- item_id
- localized_name
- category
- subcategory
- tier
- enchantment
- weight
- max_quality
- raw_json_version

`static_zones`

- zone_id
- zone_name
- zone_type
- biome
- tier
- continent
- raw_json_version

Campos reais dependem do schema validado em `world.json`.

`static_recipes`

- output_item_id
- input_item_id
- input_count
- craft_type
- focus_related
- source_version

### Tabelas derivadas

`item_demand_daily`

- date
- item_id
- appearances_killer
- appearances_victim
- appearances_participant
- estimated_destroyed_count
- estimated_destroyed_value
- battle_presence_count
- zscore_7d
- zscore_30d
- momentum_7d

`build_signature_daily`

- date
- signature_hash
- mainhand
- offhand
- head
- armor
- shoes
- cape
- food
- potion
- mount
- appearances
- killer_appearances
- victim_appearances
- avg_ip
- estimated_success_ratio

`zone_risk_daily`

- date
- location
- kill_area
- deaths
- kill_fame
- avg_group_size
- avg_ip
- risk_zscore

`war_pressure_daily`

- date
- battles
- kills
- total_fame
- active_guilds
- active_alliances
- war_pressure_index

`item_market_signal_daily`

- date
- item_id
- city
- quality
- price
- volume
- spread
- freshness
- demand_index
- destruction_index
- meta_index
- liquidity_index
- opportunity_score
- confidence_score

## Coleta recomendada

### Coletor de gameinfo

Criar script separado, por exemplo:

```bash
python analyze.py public collect --source events --limit 100
python analyze.py public collect --source battles --limit 50
```

Ou script:

```bash
python scripts/collect_public_intel.py
```

Regras:

- Incremental por `EventId` e/ou `TimeStamp`.
- Nao duplicar eventos.
- Respeitar cache headers.
- Usar backoff em erro.
- Registrar `public_data_runs`.
- Salvar raw payload opcionalmente.
- Normalizar equipamentos em tabela propria.

### Frequencia inicial

Para eventos:

- A cada 5 a 15 minutos e suficiente no inicio.
- Nao ha necessidade de bater a cada poucos segundos.

Para batalhas:

- A cada 15 a 60 minutos.

Para metadados estaticos:

- Manualmente apos patch ou 1 vez por dia/semana.

## Interface recomendada

### Modo simples

Tela inicial deve responder:

- "O que vale comprar agora?"
- "O que esta com demanda subindo?"
- "O que esta perigoso/instavel?"
- "Qual item tem dado fresco e volume real?"
- "Qual oportunidade tem maior confianca?"

Cartoes simples:

- Top oportunidades seguras.
- Top itens com demanda subindo.
- Top consumiveis de guerra.
- Top itens de meta emergente.
- Alertas de dados ruins.

Linguagem:

- "Demanda subindo"
- "Preco ainda atrasado"
- "Volume bom"
- "Risco alto"
- "Evite por enquanto"

Evitar economes na camada simples. O botao de glossario existente deve explicar
as siglas para quem quiser.

### Modo especialista

Abas recomendadas:

1. Mercado
2. Item Lab
3. Meta e Builds
4. Guerra e Batalhas
5. Risco de Mapa
6. Cadeias Produtivas
7. Backtesting
8. Dados e Cobertura

#### Item Lab

Ao clicar em um item:

- preco atual por cidade;
- historico de preco e volume;
- spread e ROI;
- volume vendido;
- destruicao por dia;
- uso em builds;
- aparicoes em killers/vitimas;
- relacao com receitas;
- score de demanda;
- score de risco;
- interpretacao automatica.

#### Meta e Builds

Recursos:

- ranking de armas mais usadas;
- ranking de armaduras;
- combinacoes de build;
- filtros por tier/encanto/IP;
- killer vs victim;
- tendencia 24h/7d/30d;
- consumiveis mais usados;
- montarias mais perdidas.

#### Guerra e Batalhas

Recursos:

- batalhas grandes recentes;
- aliancas mais ativas;
- fame destruida por dia;
- itens mais consumidos em guerra;
- efeito sobre precos;
- alertas de demanda por consumiveis.

#### Risco de Mapa

Recursos:

- kills por area/zona quando disponivel;
- horario mais perigoso;
- risco relativo;
- risco ajustado ao valor transportado;
- spread minimo recomendado.

Se a localizacao vier nula com frequencia, a UI deve deixar isso claro e usar
apenas agregados disponiveis.

## Fases de implementacao sugeridas

### Fase 0 - Validacao de fontes

Objetivo: confirmar schemas e limites.

Tarefas:

- Testar endpoints `events`, `battles`, `search`.
- Descobrir se ha paginacao por offset/range nos endpoints publicos.
- Verificar endpoints de player/guild/battle especifico sem assumir.
- Criar pequeno script de prova que salva 10 eventos e 3 batalhas.
- Documentar campos nulos frequentes, principalmente `Location`.

Resultado esperado:

- Documento curto de schema real.
- Decisao sobre frequencia de coleta.

### Fase 1 - Ingestao publica basica

Objetivo: guardar kills e batalhas.

Tarefas:

- Criar tabelas `public_data_runs`, `kill_events`,
  `kill_event_actors`, `kill_event_equipment`, `battle_summaries`,
  `battle_guilds`, `battle_alliances`.
- Criar coletor incremental.
- Criar comando CLI de status.
- Criar testes de normalizacao com payload sintetico.

Resultado esperado:

- Banco local acumulando eventos publicos sem duplicacao.

### Fase 2 - Indice de destruicao e demanda

Objetivo: transformar kills em sinais economicos.

Tarefas:

- Agregar equipamentos por item/dia.
- Separar killer/victim/participant.
- Calcular destruicao estimada.
- Cruzar com preco atual e historico.
- Criar `item_demand_daily`.

Resultado esperado:

- Ranking de itens com demanda/destruicao subindo.

### Fase 3 - Cruzamento mercado + demanda

Objetivo: criar sinais que nenhuma planilha simples oferece.

Tarefas:

- Criar `item_market_signal_daily`.
- Calcular divergencia demanda-preco.
- Adicionar score de confianca.
- Adicionar recomendacoes na tela inicial.

Resultado esperado:

- "Itens com demanda subindo e preco atrasado".

### Fase 4 - Meta e builds

Objetivo: estudar builds como fenomeno economico.

Tarefas:

- Criar assinatura de build.
- Agregar frequencia por janela.
- Comparar killers vs vitimas.
- Criar pagina "Meta e Builds".

Resultado esperado:

- Ranking de builds e familias de itens em ascensao.

### Fase 5 - Risco e mapa

Objetivo: mapear risco e logistica com dados publicos.

Tarefas:

- Importar/validar `world.json`.
- Criar `static_zones`.
- Agregar kills por location/kill_area.
- Criar `zone_risk_daily`.
- Exibir heatmap se houver localizacao suficiente.

Resultado esperado:

- Risco historico por area, horario e tipo de zona.

### Fase 6 - Guerra, guildas e aliancas

Objetivo: estudar conflitos como motor de mercado.

Tarefas:

- Agregar batalhas por guilda/alianca.
- Criar `war_pressure_daily`.
- Cruzar guerra com demanda por itens.
- Criar painel de guerra.

Resultado esperado:

- Indicador de "pressao de guerra" e seus impactos economicos.

### Fase 7 - Rotas Avalon e OCR opcional

Objetivo: adicionar dados logisticos temporarios sem scanner.

Tarefas futuras:

- Primeiro importar atlas estatico.
- Depois criar modulo manual se houver comunidade.
- Depois avaliar OCR de screenshot com confirmacao humana.

Resultado esperado:

- Mapa logistico limpo, sem packet scanner.

## Criterios de qualidade

O agente deve sempre diferenciar:

- dado observado;
- dado inferido;
- dado estimado;
- hipotese nao validada.

Todo score deve ser decomponivel. O usuario deve conseguir ver por que um item
foi recomendado.

Exemplo de decomposicao:

- lucro esperado: 35%
- volume: bom
- frescor: excelente
- destruicao 7d: subindo
- preco 7d: lateral
- risco logistico: medio
- confianca: 78/100

## Cuidados estatisticos

- Correlação nao e causalidade.
- Killboard representa uma amostra enviesada do jogo.
- Itens populares podem estar populares por preco, patch, guerra ou moda.
- Eventos grandes podem distorcer janelas curtas.
- Dados de mercado podem estar velhos ou manipulados.
- Volume historico da AODP e agregado e deve ser interpretado com cuidado.
- O modelo deve usar intervalos, confianca e backtesting.

Tecnicas recomendadas:

- medias moveis;
- EWMA;
- volatilidade rolling;
- z-score e robust z-score;
- momentum;
- estudos de evento;
- regressao simples para tendencia;
- correlacao com defasagem;
- backtesting walk-forward;
- deteccao de outliers;
- score bayesiano ou shrinkage para itens com pouca amostra.

## Produto final desejado

A plataforma deve parecer profissional e integrada ao universo do Albion:

- filtros parecidos com mercado do jogo;
- itens clicaveis com icone e detalhes;
- cadeias produtivas visuais;
- mapa/risco em paineis limpos;
- graficos claros;
- linguagem simples na camada leiga;
- metricas profundas na camada especialista;
- dados auditaveis e historicos.

O objetivo nao e vender uma promessa magica. O objetivo e criar uma ferramenta
que ajuda chefes de guilda, traders, coletores, refinadores e crafters a tomar
decisoes melhores com base em evidencia.

## Auditoria de completude das fontes possiveis

Esta secao responde a uma pergunta importante do usuario: "levantamos todas as
possibilidades de dados que podemos aproveitar?"

Resposta curta: nao existe garantia de completude absoluta, porque endpoints,
ferramentas comunitarias e dados publicos mudam. Mas o universo util pode ser
organizado em camadas. O agente deve trabalhar com esta taxonomia e atualizar
o documento quando uma fonte nova for validada.

### Camada A - Dados publicos fortes e diretamente coletaveis

Sao dados que podem entrar no produto sem depender de varios usuarios fazendo
registro manual.

- AODP precos atuais.
- AODP historico de vendas.
- AODP ouro.
- Snapshots locais coletados pelo app.
- API publica de eventos de kill.
- API publica de batalhas.
- Busca publica de jogadores/guildas, se usada com parcimonia.
- Dumps estaticos de itens e mundo.
- Noticias/patch notes publicas, preferencialmente coletadas em baixa
  frequencia ou registradas manualmente como eventos economicos.

Valor economico:

- microestrutura de mercado;
- liquidez;
- preco e volume;
- destruicao de itens;
- popularidade de builds;
- intensidade de guerra;
- mudancas de regras via patch;
- inflacao via ouro.

### Camada B - Dados publicos fortes, mas com representatividade imperfeita

Sao dados uteis, mas enviesados.

- Killboard publica: representa mortes registradas/publicadas, nao todo o
  consumo economico do servidor.
- Steam charts e sinais externos de popularidade: Albion tambem tem launcher
  proprio e mobile, entao Steam nao representa a populacao inteira.
- Ferramentas terceiras publicas: podem ter Cloudflare, termos proprios,
  licencas ou dados incompletos.

Uso correto:

- tratar como proxy;
- nunca como verdade total;
- combinar com preco, volume e historico;
- exibir confianca.

### Camada C - Dados estaticos de mapa e bioma

Provavelmente coletaveis por dumps ou atlas comunitarios:

- nome/id de zonas;
- tipo de zona;
- tier/cluster quando disponivel;
- bioma;
- recursos esperados do bioma;
- conexoes estaticas do continente real;
- cidades, rests e regioes;
- informacoes estaticas de mapas Avalon em atlas publicos.

Uso economico:

- estimar oferta estrutural de recursos;
- explicar por que uma cidade tende a receber mais de determinado insumo;
- estudar se bonus de cidade se converte em preco menor ou volume maior;
- precificar custo logistico estrutural;
- criar filtros e mapas economicos.

Regra importante: nao hardcodar biomas e recursos sem validar fonte. O agente
deve importar de dumps/atlas quando possivel e deixar versao/fonte registrada.

### Camada D - Dados dinamicos de mapa que parecem nao ter fonte publica limpa

Exemplos:

- quantidade exata de recursos vivos em uma zona agora;
- quantidade exata de recursos encantados vivos em uma zona agora;
- estado atual de cada no de recurso;
- rotas Avalon temporarias abertas agora;
- jogadores vivos no mapa;
- posicao de grupos, gankers ou transportadores.

Conclusao tecnica:

Esses dados nao devem ser tratados como publicamente disponiveis. Para obter
estado dinamico real do mapa, normalmente seria necessario:

- scout humano;
- registro manual;
- screenshot/OCR com confirmacao humana;
- ou tecnicas proibidas/arriscadas como scanner, radar, memoria ou pacotes.

O app deve evitar prometer "materiais encantados naquele momento". O que pode
ser feito de forma limpa e:

- estimar probabilidade estrutural por zona/bioma/tier;
- inferir pressao de oferta por preco e volume;
- inferir atividade de coleta por mortes de gatherers e inventario quando
  presente em eventos publicos;
- registrar observacoes voluntarias de scouts no futuro;
- usar OCR apenas como ferramenta auxiliar e confirmada pelo usuario, se for
  aprovado numa fase posterior.

### Camada E - Dados internos voluntarios da comunidade/guilda

Mesmo que o foco inicial seja dados publicos, no futuro a plataforma pode
aceitar dados voluntarios:

- perfil economico do membro;
- foco disponivel;
- especializacoes;
- capacidade de transporte;
- ilhas e producao;
- contribuicoes de materiais;
- metas de guilda;
- observacoes de rotas;
- estoques locais.

Esses dados nao sao publicos. Devem ser minimizados, pseudonimos e com
permissoes claras.

## Sobre biomas, recursos e materiais encantados

Biomas e recursos sao uma frente essencial para a plataforma, mas precisam ser
separados em tres niveis.

### Nivel 1 - Oferta estrutural

Pergunta:

"Esta regiao e naturalmente boa para quais recursos?"

Dados necessarios:

- bioma;
- tier da zona;
- tipo de recurso esperado;
- proximidade de cidade;
- bonus de refino/producao da cidade;
- distancia/custo logistico.

Analises:

- preco relativo de recurso bruto por cidade;
- preco relativo de refinado por cidade;
- volume por cidade;
- elasticidade entre bruto e refinado;
- diferenca persistente entre cidade de bonus e demais cidades;
- efeito da geografia sobre arbitragem.

Resultado para leigo:

"Venda fibra aqui", "refine couro ali", "nao transporte pedra nessa rota hoje".

Resultado para especialista:

Heatmap de oferta estrutural, spread liquido, valor por peso, liquidez,
persistencia historica e intervalo de confianca.

### Nivel 2 - Oferta observada via mercado

Pergunta:

"A oferta parece alta ou baixa hoje?"

Dados:

- preco atual;
- profundidade/topo de livro quando disponivel;
- volume historico;
- snapshots diarios;
- variacao contra media 7d/30d;
- frescor por cidade.

Analises:

- z-score de preco;
- volume anormal;
- compressao/abertura de spread;
- queda de preco em cidade produtora;
- diferenca entre bruto e refinado;
- oportunidade de refino ou transporte.

### Nivel 3 - Estado dinamico real do mapa

Pergunta:

"Quantos recursos encantados existem nesta zona neste momento?"

Resposta:

Sem scout/OCR/manual/scanner, provavelmente nao ha dado publico limpo para
isso. O app deve tratar esse tipo de dado como "nao disponivel publicamente".

Alternativas legitimas:

- criar um painel de probabilidade/expectativa, nao de certeza;
- deixar scouts registrarem observacoes no futuro;
- estudar o mercado para inferir abundancia;
- usar mortes de gatherers com inventario como amostra enviesada de coleta.

## Metodos economicos e estatisticos aplicaveis

A economia do Albion e especial porque combina mercado player-driven, producao,
destruicao por PvP, geografia, risco logistico, bonus de cidades, foco e
choques de patch. Isso permite aplicar varios ramos de analise economica.

### Microestrutura de mercado

Perguntas:

- O spread esta largo por falta de liquidez ou por risco real?
- O topo do livro e confiavel ou e fake/stale?
- Quanto slippage existe para comprar/vender uma quantidade?
- Qual a probabilidade de uma ordem executar?

Indicadores:

- bid-ask spread;
- spread percentual;
- idade das ordens;
- volume historico;
- persistencia de oportunidade;
- profundidade acumulada;
- preco medio de execucao;
- fill probability estimada;
- adverse selection: quando so sobra ordem ruim.

Uso:

- filtrar ghost flips;
- calcular lucro executavel;
- ranquear flips por risco real;
- decidir entre comprar instantaneo, colocar ordem ou ignorar.

### Arbitragem espacial

Perguntas:

- A lei do preco unico falha entre cidades?
- O spread paga taxa, peso, tempo e risco?
- A cidade com bonus realmente fica mais barata no produto esperado?

Modelo:

Preco_destino - Preco_origem - taxas - custo_transporte - premio_risco >
lucro_minimo

Indicadores:

- lucro liquido;
- ROI;
- lucro por peso;
- lucro por minuto;
- lucro por slot de inventario;
- risco por rota;
- persistencia historica do spread.

### Economia da producao

Perguntas:

- Vale vender bruto, refinar ou craftar?
- O foco deve ser gasto em qual item?
- Qual cadeia produtiva agrega mais valor?

Indicadores:

- margem sem foco;
- margem com foco;
- prata por foco;
- gargalo de insumo;
- custo de oportunidade do foco;
- sensibilidade a taxa de estacao;
- retorno esperado por capital empregado.

Principio:

Foco e um recurso escasso. Deve ser tratado como capital produtivo diario, nao
como bonus gratuito.

### Demanda derivada por destruicao

Perguntas:

- Quais itens estao sendo destruidos?
- Quais builds estao crescendo?
- Quais consumiveis sao usados em guerras?
- O mercado ja precificou essa demanda?

Fontes:

- eventos de kill;
- batalhas;
- equipamentos de killers/vitimas;
- consumiveis;
- montarias;
- preco/volume.

Indicadores:

- destruicao por item;
- uso por killers;
- uso por vitimas;
- crescimento 24h/7d/30d;
- divergencia demanda-preco;
- pressao de reposicao.

### Estudos de evento e causalidade

Eventos:

- patches;
- nerfs/buffs;
- guerras grandes;
- inicio/fim de temporada;
- mudancas de meta;
- eventos oficiais;
- grandes campanhas de guildas.

Metodos:

- janela antes/depois;
- grupo de controle;
- diferenca-em-diferencas quando houver comparavel;
- retorno anormal;
- volume anormal;
- demanda anormal por killboard;
- defasagem entre demanda e preco.

### Series temporais

Metodos recomendados:

- medias moveis;
- EWMA;
- tendencia linear robusta;
- sazonalidade por dia da semana/hora;
- volatilidade rolling;
- z-score e robust z-score;
- momentum e aceleracao;
- forecast baseline com intervalo;
- backtesting walk-forward.

Evitar no inicio:

- modelos complexos sem dados suficientes;
- promessas de previsao exata;
- machine learning opaco antes de construir bons indicadores.

### Gestao de estoque e risco

Perguntas:

- Quanto estoque manter?
- Quando vender?
- Quando segurar?
- Qual drawdown maximo aceitavel?

Indicadores:

- giro de estoque;
- dias de cobertura;
- capital travado;
- VaR/CVaR simples;
- stop-loss operacional;
- exposicao por item/familia;
- diversificacao;
- concentracao de risco.

## Produto: duas experiencias no mesmo sistema

### Experiencia leiga - "me diga o que fazer"

Objetivo: reduzir decisao complexa a acoes seguras.

Componentes:

- painel inicial com top oportunidades confiaveis;
- semaforo de dado fresco/confiavel;
- explicacao curta do motivo;
- filtros simples: capital, cidade, peso, risco, atividade;
- botoes de acao: vender, comprar, refinar, craftar, guardar, evitar;
- presets: coletor, refinador, crafter, trader, ilha, guerra.

Exemplo de card:

Item: T6 couro refinado

- Acao: vender em Martlock
- Motivo: preco acima da media, volume bom, dado fresco
- Risco: baixo
- Confianca: alta

### Experiencia especialista - "me mostre a evidencia"

Objetivo: permitir investigacao profunda.

Componentes:

- Item Lab;
- graficos multijanelas;
- tabela de ordens/profundidade;
- comparador intercidades;
- heatmaps;
- estudos de evento;
- painel de guerra;
- painel de builds;
- painel de risco logistico;
- export CSV/JSON;
- SQL somente leitura;
- decomposicao de score.

Todo card simples deve ter caminho para drill-down tecnico.

## Killboard como motor economico: Trash to Cash

Esta secao detalha uma camada essencial que ainda precisava ficar mais
minuciosa: tratar cada morte como um evento economico de destruicao, dano,
reposicao e possivel reprecificacao futura.

### Principio

No Albion, a morte nao e apenas estatistica de PvP. Ela e um mecanismo de
destruicao de capital produtivo e consumo forcado de estoque. Quando jogadores
morrem, parte dos itens pode ser destruida, parte pode ser saqueada, parte
volta ao mercado direta ou indiretamente, e parte gera demanda de regear.

O app deve transformar killboard em um livro economico paralelo:

- o que foi usado;
- o que morreu;
- o que provavelmente foi destruido;
- o que provavelmente precisara ser recomprado;
- em qual janela de tempo a recompra tende a acontecer;
- se o mercado ja reagiu ou ainda esta atrasado.

### Nao hardcodar Trash Rate sem validacao

O usuario citou a regra comum de aproximadamente 30% de trash. O agente nao
deve gravar esse numero como verdade imutavel sem validar a regra atual do
jogo e eventuais diferencas por tipo de item/conteudo.

Criar parametros configuraveis:

`economic_assumptions`

- key
- value
- source
- confidence
- valid_from
- valid_to
- notes

Assumptions iniciais sugeridas:

- `trash_rate_base`
- `durability_loss_rate_on_death`
- `regear_delay_hours_low`
- `regear_delay_hours_base`
- `regear_delay_hours_high`
- `market_replacement_capture_rate`
- `loot_recirc_rate`
- `guild_regear_share`

Todo relatorio deve deixar claro quando um numero vem de regra validada,
estimativa do usuario ou calibracao empirica do app.

### Modelo contabil de destruicao

Para cada item presente em uma morte, estimar quatro destinos:

- `trashed`: destruido permanentemente;
- `looted`: dropou e pode voltar a circular;
- `damaged`: sofreu perda de durabilidade/reparo;
- `replaced`: gerou necessidade de recompra.

Modelo simples:

```text
expected_trashed_units = count * trash_rate
expected_looted_units = count * (1 - trash_rate)
replacement_pressure = expected_trashed_units + guild_regear_share * expected_looted_units_adjustment
expected_destroyed_value = expected_trashed_units * reference_price
```

O `reference_price` deve ser escolhido com cuidado:

- preco de venda minimo fresco na cidade relevante;
- VWAP historico;
- mediana 7d;
- preco medio de execucao estimado;
- fallback global quando a cidade nao tiver dado.

### Tabelas recomendadas

`asset_destruction_events`

- event_id
- timestamp
- battle_id
- item_id
- slot
- quality
- count_seen
- actor_role
- victim_player_id
- victim_guild_id
- victim_alliance_id
- kill_area
- location
- reference_city
- reference_price
- expected_trashed_units
- expected_looted_units
- expected_destroyed_value
- replacement_pressure
- assumption_version
- confidence_score

`regear_demand_forecasts`

- forecast_id
- generated_at
- source_window_start
- source_window_end
- item_id
- city
- quality
- expected_units_low
- expected_units_base
- expected_units_high
- expected_demand_value
- expected_peak_start
- expected_peak_end
- source_cluster_id
- confidence_score
- explanation

`demand_vacuum_alerts`

- alert_id
- generated_at
- item_id
- city
- signal_type
- destruction_signal
- price_signal
- volume_signal
- spread_signal
- freshness_signal
- expected_action
- urgency
- confidence_score
- expires_at

### Interpretacao economica

A morte cria um "vacuo de demanda" quando:

- houve destruicao relevante;
- os itens destruidos sao usados em builds recorrentes;
- a guilda/alianca provavelmente fara regear;
- o mercado local ainda tem preco antigo;
- o estoque visivel e limitado;
- o volume historico suporta execucao;
- ha dados frescos.

Sinal forte:

```text
destruction_zscore alto
+ demanda de build subindo
+ preco lateral/baixo
+ volume suficiente
+ dado fresco
+ evento recente
= oportunidade de reposicao/revenda/craft
```

### Calibracao empirica

O app deve aprender com o proprio historico:

- depois de grandes clusters de morte, medir preco 1h/6h/12h/24h/72h;
- medir volume anormal;
- medir se o item realmente subiu;
- ajustar `market_replacement_capture_rate`;
- separar por item, cidade, conteudo e tipo de evento.

Nunca prometer que toda morte gera compra imediata. Algumas guildas ja tem
estoque, algumas compram em outra cidade, algumas usam regears internos, e
parte dos itens saqueados volta ao mercado.

## Detector de clusters de morte e ZvZ

Eventos individuais sao uteis, mas o valor economico maior vem de agrupar
mortes em clusters. Um cluster representa uma concentracao anormal de mortes
em tempo, local, guildas/aliancas e/ou batalha.

### Fontes de agrupamento

Usar, em ordem de confianca:

1. `battle_id`, quando presente e confiavel.
2. `location`/`clusterName`, quando presente.
3. janela temporal curta.
4. guildas/aliancas repetidas.
5. composicao de participantes.
6. proximidade de kill_area/tipo.

### Tabelas recomendadas

`death_clusters`

- cluster_id
- started_at
- ended_at
- detection_method
- battle_id
- location
- kill_area
- total_events
- total_deaths
- total_kill_fame
- unique_players
- unique_guilds
- unique_alliances
- avg_item_power
- dominant_event_type
- classification
- confidence_score

`death_cluster_items`

- cluster_id
- item_id
- slot
- quality
- victim_appearances
- killer_appearances
- participant_appearances
- expected_trashed_units
- expected_destroyed_value
- replacement_pressure

`death_cluster_entities`

- cluster_id
- entity_type: player, guild, alliance
- entity_id
- entity_name
- kills
- deaths
- kill_fame
- side_hint

### Features para detectar cluster relevante

- mortes por minuto;
- fama destruida por minuto;
- numero de guildas/aliancas;
- concentracao de duas aliancas dominantes;
- IP medio;
- diversidade de builds;
- repeticao de armas AoE/suporte;
- numero de healers/tanks;
- tamanho medio de grupo;
- presenca de montarias de transporte;
- horario do servidor;
- historico daquele local/horario.

### Classificacao inicial de eventos

Criar classificacao baseada em regras simples antes de qualquer ML:

- `zvz`: muitas mortes, muitas guildas/aliancas, alto IP medio, armas AoE,
  suportes/healers/tanks, battle_id forte.
- `gank`: poucas vitimas por evento, armas de burst/mobilidade, vitimas com
  montarias/cargas, grupos pequenos ou medio-pequenos.
- `small_scale`: grupos menores, composicoes completas, IP medio alto, mortes
  distribuidas.
- `transport_interdiction`: vitimas com montarias de carga, bags, recursos ou
  inventario valioso quando disponivel.
- `faction_or_open_world`: usar apenas se `kill_area`, location ou outro campo
  validado sustentar.
- `unknown`: quando o dado nao sustentar uma conclusao.

### Classificador por arsenal

Criar uma tabela manual/editavel de tags de item:

`item_combat_tags`

- item_id
- weapon_family
- role_tags: gank, zvz, healer, tank, support, clap, purge, mobility,
  catch, escape, transport, gatherer
- confidence
- source
- notes

Uso:

- diferenciar "zona de gank" de "zona de guerra";
- explicar por que o app classificou o risco;
- gerar alertas mais uteis para coletores e transportadores.

Exemplo:

- Muitas mortes causadas por armas de catch/burst/mobilidade: alerta de gank.
- Muitas mortes com armas AoE, healers e tanks: alerta de ZvZ.
- Muitas mortes de montarias de carga: alerta logistico.

O agente deve preencher tags de forma conservadora e permitir revisao manual.

## Janelas de regear e previsao de demanda

Nem toda demanda aparece imediatamente. O app deve modelar defasagens.

### Horizontes padrao

Calcular efeitos em janelas:

- 1h;
- 3h;
- 6h;
- 12h;
- 24h;
- 72h;
- 7d.

### Hipoteses por tipo de evento

- Gank: reposicao pequena, dispersa e muitas vezes imediata.
- Small scale: reposicao moderada, possivelmente em 1h a 24h.
- ZvZ: reposicao grande, pode ocorrer imediatamente se houver CTA continuo,
  ou em lote antes do proximo timer.
- Transporte morto: pode gerar choque no item transportado, mas depende do
  inventario estar visivel e da relevancia do valor.
- Guerra de aliancas: pode aumentar demanda recorrente por consumiveis,
  montarias e sets padronizados.

### Metodo estatistico recomendado

Usar modelo de defasagem distribuida:

```text
price_return(item, t+h) ~ destruction_signal(t)
                       + war_pressure(t)
                       + market_liquidity(t)
                       + item_fixed_effect
                       + city_fixed_effect
```

No inicio, nao precisa implementar regressao complexa. Basta calcular:

- retorno apos evento;
- volume anormal apos evento;
- max drawup em 24h;
- tempo ate pico;
- taxa de acerto do sinal.

### Backtesting especifico

Para cada alerta gerado:

- preco no momento do alerta;
- preco 1h/6h/12h/24h depois;
- volume observado;
- se havia dado fresco;
- se o item ficou sem estoque;
- lucro teorico com taxas;
- lucro executavel com slippage;
- se o sinal expirou.

Isso deve alimentar a confianca dos alertas futuros.

## Motor de ordens de servico

A camada leiga nao deve apenas mostrar graficos. Ela deve converter sinais em
tarefas operacionais simples, como missoes.

### Conceito

Um sinal tecnico vira uma ordem de servico quando:

- tem acao clara;
- tem perfil responsavel;
- tem prazo;
- tem cidade;
- tem quantidade sugerida;
- tem risco aceitavel;
- tem confianca minima;
- tem explicacao curta.

### Tabelas recomendadas

`service_orders`

- order_id
- created_at
- expires_at
- profile_target: coletor, refinador, crafter, trader, transportador,
  tesoureiro
- action_type: collect, sell, buy, refine, craft, transport, stock,
  avoid_route, watch_market
- item_id
- city_from
- city_to
- quantity_low
- quantity_base
- quantity_high
- capital_required
- expected_profit
- expected_roi
- risk_level
- confidence_score
- source_signal_id
- explanation_short
- explanation_technical
- status

`service_order_feedback`

- order_id
- account_id
- feedback_at
- accepted
- completed
- skipped_reason
- realized_quantity
- realized_profit
- notes

### Exemplos de ordens

Coletor:

- "Venda fibra T6 em Lymhurst hoje. Preco 12% acima da media 7d e volume bom."
- "Evite a regiao X por 2h. Mortes/hora subiram 400% e padrao parece gank."

Crafter:

- "Produza 50 Capotes de Clerigo T7. Demanda por destruicao subiu e preco
  ainda nao reagiu."

Refinador:

- "Use ate 8.000 foco em couro T6.2. Prata/foco acima da sua media semanal."

Transportador:

- "Rota Fort Sterling -> Martlock tem spread positivo, mas risco alto. Exigir
  lucro minimo maior ou esperar."

Tesoureiro/chefe:

- "Estoque de sets ZvZ deve ser reforcado: guerra elevou consumo de comida,
  pocao e capes."

### Regras de UX

Cada ordem deve ter:

- acao;
- motivo;
- prazo;
- risco;
- confianca;
- botao para ver evidencia.

Nao mostrar jargoes na versao simples. O especialista pode abrir o drill-down.

## Arquitetura para alto volume de JSON da killboard

O trecho do usuario levanta uma pergunta critica: como lidar com volume de
eventos em horario de pico sem travar o PC local?

### Principio

Separar ingestao, normalizacao, agregacao e interface.

O app nao deve processar tudo dentro da request da UI. A coleta publica deve
rodar como job local, incremental e idempotente.

### Pipeline recomendado

1. Fetch leve da API.
2. Validacao do payload.
3. Deduplicacao por id remoto.
4. Gravacao raw opcional.
5. Normalizacao em tabelas relacionais.
6. Marcacao de eventos pendentes de agregacao.
7. Agregacao assincorna por janelas.
8. Atualizacao de tabelas derivadas.
9. UI le apenas tabelas derivadas/cacheadas.

### Tabelas de controle

`public_ingest_checkpoints`

- source
- cursor_type
- cursor_value
- last_success_at
- last_event_timestamp
- last_event_id
- consecutive_errors

`public_ingest_queue`

- queue_id
- source
- remote_id
- fetched_at
- priority
- status
- attempts
- next_attempt_at
- error

`public_aggregation_jobs`

- job_id
- job_type
- window_start
- window_end
- status
- started_at
- finished_at
- rows_processed
- error

### Regras de performance

- Usar inserts em lote.
- Usar indices em `event_id`, `timestamp`, `battle_id`, `item_id`,
  `location`, `guild_id`, `alliance_id`.
- Usar WAL no SQLite.
- Manter raw JSON com retencao curta ou compactado.
- Nunca recalcular todo o historico a cada coleta.
- Materializar agregados diarios/horarios.
- Fazer vacuum/prune planejado.
- Expor status de fila/coleta na UI.

### Controle de carga

Durante pico:

- reduzir frequencia de polling se houver erro;
- aumentar tamanho de lote apenas ate limite seguro;
- priorizar eventos recentes;
- adiar agregacoes pesadas;
- usar janelas fechadas para calculos definitivos;
- usar janelas abertas com etiqueta "provisorio".

### Estados dos dados

Todo dado derivado deve ter estado:

- `provisional`: janela ainda aberta, pode mudar;
- `final`: janela fechada;
- `stale`: fonte sem atualizacao recente;
- `partial`: coleta incompleta;
- `failed`: erro de coleta/processamento.

Isso protege a UI contra conclusoes falsas em horario de pico.

### Retencao sugerida

- Eventos normalizados: manter longo prazo se o banco suportar.
- Raw JSON: 7 a 30 dias ou compactado.
- Agregados horarios: 90 a 180 dias.
- Agregados diarios: manter indefinidamente.
- Alertas expirados: manter para backtesting.

## Politica comunitaria, doacoes e RMT

Registrar explicitamente a intencao do projeto para orientar proximos agentes.

### Principios

- A plataforma e uma ferramenta comunitaria e analitica.
- Nao deve vender prata, itens ou vantagem direta por dinheiro real.
- Doacoes devem ser voluntarias.
- Acesso ao Discord/comunidade nao deve ser apresentado como compra de prata
  ou item in-game.
- Contribuicoes in-game, se existirem, devem ser tratadas como apoio
  comunitario/guilda, nao transacao comercial obrigatoria.
- Nao prometer retorno financeiro real.
- Nao usar linguagem de investimento real.

### Cuidados de produto

- Evitar "lucro garantido" na interface; usar "lucro estimado" ou
  "oportunidade com alta confianca".
- Mostrar riscos, taxas, frescor e liquidez.
- Permitir que guildas sem doacao usem a comunidade quando esse for o
  objetivo do projeto.
- Separar comunidade, governanca e acesso tecnico.

### Registro/auditoria

Se houver modulo de contribuicoes:

- registrar item/material;
- quantidade;
- data;
- objetivo comunitario;
- responsavel pelo registro;
- observacao;
- nunca transformar isso em compra/venda de moeda real.

## Complemento na ordem de implementacao: Trash to Cash

Inserir antes de dashboards complexos:

1. Criar assumptions economicas configuraveis.
2. Normalizar eventos de kill.
3. Criar `asset_destruction_events`.
4. Criar agregacao por item/janela.
5. Criar detector simples de clusters.
6. Criar classificacao inicial: gank, small scale, ZvZ, transporte, unknown.
7. Criar primeiro `regear_demand_forecasts`.
8. Criar backtest de alertas.
9. Criar `service_orders` simples.
10. So entao sofisticar UI/ML.

## Contas, perfis e interfaces por funcao

Mesmo que hospedagem fique para depois, o desenho local deve facilitar migrar
para Supabase/Vercel ou outro backend no futuro.

### Identidade minima

O usuario nao quer coletar email real ou dado sensivel. Entao:

- conta pseudonima;
- username interno;
- nick Albion/Discord opcional;
- senha com hash forte;
- admin pode resetar senha, desativar e alterar papeis;
- admin nao deve guardar senha em texto;
- identidade real, se existir, fica em controle local separado do dono.

### Papeis administrativos

- admin global;
- chefe de guilda;
- tesoureiro;
- oficial economico;
- membro;
- convidado/leitura;
- colaborador externo.

### Perfis economicos

Uma conta pode ter varios perfis:

- coletor;
- pescador;
- refinador;
- crafter;
- agricultor/ilha;
- criador de animais;
- cozinheiro/alquimista;
- trader/flipper;
- transportador;
- scout;
- PVE;
- PVP/ZvZ.

Esses perfis controlam a interface padrao, nao necessariamente permissao de
seguranca. Permissao e perfil economico devem ser separados.

### Interfaces por perfil

Coletor:

- onde vender bruto;
- onde refinar;
- bruto vs refinado;
- cidade recomendada;
- risco de transporte;
- preco por peso;
- historico do recurso.

Refinador:

- margem por cidade;
- foco disponivel;
- prata por foco;
- taxa de estacao;
- retorno de recursos;
- sensibilidade de preco.

Crafter:

- custo dos insumos;
- margem por qualidade;
- Mercado Negro quando aplicavel;
- demanda por killboard;
- diarios relacionados;
- prata por foco.

Ilhas/agro:

- sementes, ervas, animais e derivados;
- ciclo de producao;
- premium/no premium;
- custo de oportunidade;
- retorno por dia;
- demanda por comida/pocoes.

Trader:

- flips;
- persistencia;
- profundidade;
- backtest;
- z-score;
- score multifator;
- risco logistico.

Chefe de guilda/tesoureiro:

- metas de doacao;
- estoque;
- necessidades produtivas;
- contribuicoes;
- valor de inventario;
- relatorios;
- gargalos;
- planejamento por cadeia.

### Controle de dispositivo

Para reduzir compartilhamento casual de contas:

- gerar `device_id` aleatorio no primeiro login;
- salvar token/cookie seguro;
- guardar hash do dispositivo no servidor;
- novo dispositivo exige liberacao do admin;
- permitir reset manual quando o usuario trocar PC.

Isso nao e seguranca perfeita contra alguem tecnico, mas e suficiente para
controle comunitario basico.

### Auditoria e privacidade

Registrar:

- login;
- logout;
- mudanca de senha;
- mudanca de papel;
- geracao de relatorio;
- contribuicao registrada;
- meta criada/alterada;
- dispositivo liberado.

Evitar:

- IP detalhado salvo indefinidamente;
- email real;
- nome real;
- fingerprint agressivo;
- coleta invisivel de dados pessoais.

## Novas perguntas de pesquisa que a plataforma deve responder

### Mercado e cidades

- A cidade com bonus realmente tem preco menor no produto beneficiado?
- Esse efeito e consistente por tier/encanto?
- O efeito aparece no preco, no volume ou nos dois?
- O spread entre cidades e persistente ou episodico?
- Quais familias respeitam melhor a lei do preco unico?

### Recursos e biomas

- Recursos abundantes no bioma sao mais baratos perto da regiao?
- Recursos encantados apresentam premio estavel?
- O premio de encantamento muda com guerras?
- O preco do bruto antecipa o preco do refinado?
- O refinado antecipa o custo de craft?

### Guerra e destruicao

- Quais itens sobem apos batalhas grandes?
- Quais consumiveis reagem mais rapido a guerra?
- Quais builds geram maior demanda derivada?
- Existe defasagem entre aumento de kills e aumento de preco?
- Itens de meta emergente podem ser comprados antes da massa perceber?

### Producao e foco

- Qual melhor uso diario do foco por perfil?
- Quando vender insumo e melhor que craftar?
- Quando craftar para Mercado Negro supera vender em cidade real?
- Quais cadeias tem menor volatilidade e melhor retorno ajustado ao risco?

### Guilda e planejamento

- O que produzir internamente versus comprar no mercado?
- Qual infraestrutura de ilha/guild hall se paga?
- Quais diarios compensam?
- Qual estoque minimo manter para ZvZ/HCE/PVE?
- Quais membros/perfis reduzem gargalos produtivos?

## Ajuste na ordem de implementacao

O plano anterior recomenda comecar por `gameinfo`. Mantem-se essa prioridade,
mas com uma fase paralela de metadados estaticos.

Nova ordem recomendada:

1. Validar schema real de `gameinfo` e limites.
2. Criar ingestao incremental de kills/batalhas.
3. Importar/normalizar metadados estaticos de itens, receitas e mundo.
4. Criar indice de destruicao por item.
5. Cruzar indice de destruicao com preco/volume/frescor.
6. Criar primeira tela simples de "demanda subindo".
7. Criar Item Lab com aba "mercado", "demanda", "builds" e "cadeia".
8. Criar perfis de conta locais/pseudonimos, ainda sem hospedagem.
9. Criar modulos por perfil economico.
10. So depois avaliar mapa dinamico, OCR ou rotas Avalon colaborativas.

## Proxima acao recomendada

A proxima IA deve comecar pela Fase 0:

1. Validar schemas reais da API `gameinfo`.
2. Criar um pequeno coletor experimental somente-leitura.
3. Criar tabelas de ingestao publica.
4. Inserir poucos eventos/batalhas.
5. Criar uma primeira agregacao: top itens em kills nas ultimas 24h/7d.
6. Cruzar esse ranking com precos atuais e historicos ja existentes.

Isso entrega o primeiro diferencial real da plataforma: mercado + demanda
publica observada, sem depender de registro manual de jogadores.

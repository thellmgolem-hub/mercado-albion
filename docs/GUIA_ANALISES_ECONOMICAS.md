# Guia de analises economicas - Mercado Albion

Este guia lista as principais analises economicas possiveis para Albion Online
usando a plataforma local, o cache atual e dados que podem ser coletados
automaticamente no futuro. A ideia e separar o que ja e possivel hoje do que
exige novas tabelas/coletas.

Fontes de dados confirmadas em 2026-06-10:

- AODP REST: precos atuais, historico agregado de vendas e ouro.
- AODP NATS: ordens de mercado, historicos e ouro em tempo real, com topicos
  `marketorders.deduped`, `markethistories.deduped` e `goldprices.deduped`.
- AODP dumps: backups diarios do banco e historicos mensais.
- `ao-bin-dumps`: metadados de itens, categorias, pesos, qualidade maxima,
  receitas, foco, tempo de craft, valor de item e outros atributos.
- Inputs locais futuros: tempo de rota, risco de transporte, taxa de estacao,
  bonus de cidade, foco disponivel, capital, peso maximo e regras pessoais de
  risco.

## Antes de analisar: qualidade dos dados

Use:

```bash
python analyze.py status --detail
```

Perguntas obrigatorias antes de confiar em qualquer resultado:

- Quantos itens e cidades estao cobertos no cache?
- O dado de compra e venda esta fresco?
- O volume historico existe para o item e para a qualidade analisada?
- O preco e uma ordem realista ou um outlier?
- A oportunidade depende de vender por ordem ou de vender instantaneamente?
- O lucro compensa tempo, peso, capital parado e risco de rota?

## Analises que ja sao possiveis hoje

### 1. Consulta de preco e spread interno

Objetivo: entender compra instantanea, venda instantanea e spread dentro de uma
cidade.

Dados: `prices`.

Comandos:

```bash
python analyze.py prices T5_BAG --qualities 1
python analyze.py prices T5_BAG --cities Martlock,Lymhurst --max-age 3600
```

Indicadores:

- `sell_price_min`: menor anuncio de venda.
- `buy_price_max`: maior ordem de compra.
- spread bruto: `sell_price_min - buy_price_max`.
- idade dos dois lados.
- disponibilidade por qualidade.

Uso economico:

- detectar mercados vazios;
- identificar itens com spread largo;
- escolher entre compra/venda instantanea e ordem;
- filtrar itens com dados velhos.

### 2. Arbitragem entre cidades

Objetivo: comprar barato em uma cidade e vender caro em outra.

Dados: `prices`, taxas, peso.

Comandos:

```bash
python analyze.py flips T5_BAG --buy-cities Martlock --sell-cities Caerleon
python analyze.py scan --cat bags --tier-min 4 --buy-cities Martlock --sell-cities Lymhurst --volume
```

Indicadores:

- lucro liquido por unidade;
- ROI;
- lucro por kg;
- idade do preco na origem e destino;
- volume/dia no historico;
- potencial/dia aproximado.

Evolucao futura:

- tempo de transporte;
- risco por rota;
- lucro/hora;
- capacidade por viagem;
- limite por capital.

### 3. Mercado Negro

Objetivo: comprar equipamentos nas cidades reais e vender para ordens do
Mercado Negro em Caerleon.

Dados: `prices`, categorias elegiveis, qualidade da ordem.

Comandos:

```bash
python analyze.py scan --cat weapons --tier-min 4 --sell-cities "Mercado Negro" --volume
python analyze.py flips T6_HEAD_PLATE_SET1 --sell-cities "Mercado Negro" --qualities 1,2,3
```

Indicadores:

- lucro liquido contra ordem de compra do Mercado Negro;
- qualidade do item versus qualidade da ordem;
- volume historico;
- idade da ordem;
- lucro por kg;
- recorrencia da oportunidade.

Risco:

- dado velho vira flip fantasma;
- Mercado Negro pode consumir ordens rapidamente;
- qualidade alta pode preencher ordem menor, mas talvez destrua margem se o
  item de qualidade alta custou caro.

### 4. Market making

Objetivo: colocar ordem de compra e ordem de venda no mesmo mercado ou em
mercados diferentes.

Dados: `prices`, taxas de anuncio, imposto, volume.

Comandos:

```bash
python analyze.py flips T5_BAG --buy-mode order --sell-mode order --same-city
python analyze.py scan --cat weapons --buy-mode order --sell-mode order --same-city --volume
```

Indicadores:

- spread liquido apos taxa de anuncio dos dois lados;
- capital travado;
- volume/dia;
- concorrencia implicita pelo spread;
- risco de editar ordem e pagar taxa novamente.

Evolucao futura:

- tempo medio para executar a ordem;
- sobrevivencia da ordem via NATS;
- custo esperado de repricing;
- lucro esperado por dia por milhao investido.

### 5. Onde vender inventario

Objetivo: dado um item que voce ja possui, ranquear o melhor destino e metodo.

Dados: `prices`.

Comandos:

```bash
python analyze.py sell T6_HEAD_PLATE_SET1 --qualities 1
```

Indicadores:

- melhor liquido instantaneo;
- melhor liquido por ordem;
- diferenca entre vender agora e esperar;
- idade da ordem/anuncio.

Uso economico:

- liquidar loot;
- decidir destino de transporte;
- precificar saida de craft.

### 6. Historico de preco e volume

Objetivo: entender tendencia, liquidez e sazonalidade.

Dados: `history`.

Comandos:

```bash
python analyze.py history T4_BAG --cities Martlock --quality 1 --days 30
python analyze.py history T4_BAG --cities Caerleon,Lymhurst --scale 6 --days 7
```

Indicadores:

- preco medio;
- volume vendido;
- media movel;
- variacao percentual;
- dias sem liquidez;
- dispersao entre cidades.

Limitacao atual:

- o historico REST e agregado e representa vendas/historico de mercado, nao uma
  fotografia completa do livro de ordens.

### 7. Ouro e inflacao

Objetivo: acompanhar relacao prata/ouro e regime macroeconomico.

Dados: `gold`.

Comandos:

```bash
python analyze.py gold --count 72
```

Indicadores:

- variacao horaria;
- tendencia de curto prazo;
- choque de preco;
- correlacao futura com itens de consumo, recursos e equipamentos.

Uso economico:

- medir poder de compra da prata;
- decidir manter caixa em prata, itens ou ouro;
- detectar periodos de inflacao/deflacao relativa.

## Analises avancadas que exigem novas coletas

### 8. Livro de ordens e profundidade real

Dados necessarios:

- AODP NATS `marketorders.deduped` ou dumps completos.

Perguntas:

- Quanto posso comprar/vender antes de mover o preco?
- Qual e o slippage para 10, 50, 100 unidades?
- Quantas ordens existem por nivel de preco?
- Qual e o tamanho medio das ordens?
- Qual e a concentracao de liquidez por cidade?

Indicadores:

- profundidade acumulada;
- spread ponderado por quantidade;
- preco medio de execucao;
- slope do livro;
- liquidez a 1%, 5% e 10% do melhor preco.

### 9. Sobrevivencia de ordem e probabilidade de flip fantasma

Dados necessarios:

- stream de ordens do NATS;
- ultimo momento em que cada ordem foi vista;
- snapshots append-only.

Perguntas:

- Quanto tempo uma ordem lucrativa costuma sobreviver?
- Qual probabilidade de a ordem ainda existir apos X minutos?
- Quais cidades e categorias ficam stale mais rapido?

Indicadores:

- idade desde ultimo visto;
- meia-vida de oportunidade;
- taxa de desaparecimento;
- confianca do flip.

### 10. Backtesting de estrategias

Dados necessarios:

- snapshots historicos append-only de precos;
- historico de volume;
- regras de execucao.

Perguntas:

- A estrategia teria dado lucro nos ultimos 7/30/90 dias?
- Qual foi drawdown maximo?
- Quantas oportunidades eram executaveis?
- O lucro veio de poucos outliers ou de muitas rotas pequenas?

Indicadores:

- PnL simulado;
- taxa de acerto;
- lucro medio;
- mediana do lucro;
- drawdown;
- Sharpe simplificado;
- turnover de capital.

### 11. Craft e refino

Dados necessarios:

- `craftingrequirements` do dump;
- precos dos insumos;
- preco do produto final;
- taxa de estacao;
- bonus de cidade;
- retorno de recurso;
- foco disponivel e custo de foco.

Perguntas:

- E melhor vender recurso bruto, refinado ou item craftado?
- Qual cidade maximiza margem apos bonus?
- Quanto vale cada ponto de foco?
- Qual receita tem melhor lucro por foco?

Indicadores:

- custo total da receita;
- custo liquido com retorno;
- lucro sem foco;
- lucro com foco;
- prata por foco;
- ROI por craft;
- volume vendavel do produto final.

### 12. Cadeias produtivas completas

Objetivo: modelar do recurso bruto ao item final.

Exemplos:

- madeira -> tabuas -> arma;
- couro -> armadura -> Mercado Negro;
- fibra -> tecido -> bolsa/capa;
- peixe/ingredientes -> comida.

Indicadores:

- margem por etapa;
- gargalo de liquidez;
- etapa com maior valor agregado;
- sensibilidade a preco de insumo;
- ponto de equilibrio.

### 13. Farming, animais, comida e pocoes

Dados necessarios:

- ciclo de producao;
- sementes/filhotes;
- retorno com/sem foco;
- custos de ingredientes;
- preco final e volume.

Perguntas:

- Qual cultivo gera melhor prata/dia por ilha?
- Vale usar foco aqui ou em refino/craft?
- Comprar ingrediente pronto ou produzir?

Indicadores:

- lucro por ciclo;
- lucro por parcela;
- lucro por dia;
- lucro por foco;
- risco de baixa liquidez.

### 14. Artefatos, runas, almas, reliquias e energia

Dados necessarios:

- precos dos artefatos e fragmentos;
- receitas de artefato;
- preco do item final.

Perguntas:

- Vale converter fragmentos em artefatos?
- Qual artefato esta barato frente ao item final?
- O lucro esta no artefato, no craft ou no Mercado Negro?

Indicadores:

- spread artefato -> item;
- margem apos craft;
- liquidez do item final;
- volatilidade do insumo raro.

### 15. Qualidade e encantamento

Perguntas:

- Qual qualidade tem melhor ROI real?
- O mercado paga premio suficiente por qualidade alta?
- Itens encantados tem liquidez proporcional ao custo?

Indicadores:

- premio de qualidade;
- curva de preco por qualidade;
- spread entre `.0`, `.1`, `.2`, `.3`, `.4`;
- volume por qualidade;
- lucro de vender qualidade alta em ordem menor do Mercado Negro.

### 16. Logistica e transporte

Dados necessarios:

- peso;
- capacidade de montaria;
- tempo medio de rota;
- risco de morte;
- custo esperado de perda;
- taxa de retorno por viagem.

Perguntas:

- Qual rota paga melhor por minuto?
- Qual item maximiza lucro por kg?
- Quanto capital levar por viagem?
- O risco compensa?

Indicadores:

- lucro por kg;
- lucro por viagem;
- lucro por hora;
- valor em carga;
- perda esperada;
- margem ajustada a risco.

### 17. Portfolio e alocacao de capital

Perguntas:

- Como distribuir capital entre flips, craft, refino e caixa?
- Quais estrategias tem menor correlacao?
- Qual limite de exposicao por item/cidade?

Indicadores:

- ROI esperado;
- giro;
- capital travado;
- liquidez;
- volatilidade;
- drawdown;
- VaR simples;
- concentracao por categoria.

### 18. Deteccao de outliers e manipulacao

Perguntas:

- O preco e real ou anuncio absurdo?
- O volume suporta a oportunidade?
- A cidade esta sem dados recentes?

Indicadores:

- desvio contra media/mediana historica;
- z-score robusto;
- preco fora de banda;
- volume nulo;
- idade alta;
- spread incompatível com historico.

### 19. Choques de patch, meta e eventos

Dados necessarios:

- datas de patch;
- alteracoes de balanceamento;
- eventos sazonais;
- dados de kill/meta se adicionados.

Perguntas:

- Qual item subiu depois de patch?
- Quais recursos antecipam demanda futura?
- Quais equipamentos entram em tendencia?

Indicadores:

- retorno antes/depois;
- volume relativo;
- breakouts;
- mudanca de regime;
- demanda por categoria.

### 20. Cross-server e arbitragem informacional

Dados necessarios:

- precos Americas, Europe e Asia.

Perguntas:

- Que tendencias aparecem primeiro em um servidor?
- Ha diferencas estruturais de preco por regiao?
- Qual item e barato/caro no servidor atual em relacao aos demais?

Observacao:

- Nao existe transporte entre servidores; isso e analise informacional, nao
  arbitragem fisica.

## Score recomendado para oportunidades

Um score robusto deveria combinar:

- lucro liquido por unidade;
- ROI;
- lucro por kg;
- volume/dia;
- idade do dado de compra;
- idade do dado de venda;
- recorrencia historica;
- profundidade do livro;
- risco de rota;
- capital necessario;
- tempo estimado;
- estabilidade do preco;
- penalidade para outliers.

Exemplo conceitual:

```text
score = lucro_esperado
      * confianca_dado
      * fator_liquidez
      * fator_recorrencia
      / (capital_travado * tempo_estimado * risco_rota)
```

## Tabelas futuras recomendadas

Para evoluir a plataforma:

- `price_snapshots`: snapshots append-only de precos atuais.
- `market_orders`: ordens individuais do NATS/dumps.
- `order_seen`: primeira/ultima vez que cada ordem foi vista.
- `craft_recipes`: receitas normalizadas do dump.
- `craft_costs`: custo calculado por cidade e data.
- `routes`: tempo, risco e capacidade por rota.
- `stations`: taxa e bonus de craft/refino por cidade.
- `strategy_runs`: resultados de backtests.
- `opportunity_scores`: oportunidades ranqueadas com score e explicacao.

## Roteiro de implementacao

1. Consolidar cache/status e testes.
2. Expandir o uso dos snapshots append-only de `prices`.
3. Refinar a nota de confianca do flip com liquidez e recorrencia.
4. Normalizar receitas de craft/refino.
5. Adicionar calculadora de craft/refino.
6. Adicionar lucro/hora, capital travado e risco de transporte.
7. Ingerir dumps historicos do AODP.
8. Ingerir NATS para livro de ordens e sobrevivencia.
9. Criar backtesting de estrategias.
10. Criar ranking multiobjetivo de oportunidades.

## Implementado na plataforma local

Ja existem as primeiras pecas de maturidade economica:

- `price_snapshots`: tabela append-only criada pelo cliente AODP para registrar
  cada lote novo de precos buscado na API.
- `confidence_score`, `confidence_label` e `confidence_notes`: campos gerados
  no motor de flips com base na idade dos dados de compra e venda.
- `python analyze.py status --detail`: diagnostico da cobertura local antes da
  analise.

Proximo passo recomendado: usar `price_snapshots` para construir backtests e
recorrencia de oportunidades, em vez de depender apenas do ultimo preco em
`prices`.

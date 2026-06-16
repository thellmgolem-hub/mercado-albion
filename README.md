# Mercado Albion — Américas

App **local** de consulta de preços e cálculo de flips do mercado do Albion Online
(servidor das **Américas**), com interface em português e uma CLI para análises
mercadológicas. Os dados vêm do [Albion Online Data Project](https://www.albion-online-data.com/)
(crowdsourced: os preços só atualizam quando algum jogador com o cliente de
coleta abre aquele mercado no jogo).

## Como rodar

Requisitos: Python 3.11+ e internet.

```
pip install -r requirements.txt   (só na primeira vez)
```

Depois é só dar dois cliques em **`run.bat`** (ou rodar `python app.py`).
O navegador abre sozinho em `http://127.0.0.1:8528`.

## As abas

São quatro abas no topo. A aba **Item** reúne, num inspetor único com um só
campo de busca, cinco sub-abas para o item selecionado.

| Aba | O que faz |
|---|---|
| **Início** | Recomendações automáticas (só cache local) com score 0–100 de régua **absoluta** e selos executar/monitorar/cautela; botão **Coletar watchlist** busca preços + histórico frescos dos itens vigiados. Abaixo: persistência das ordens, mapa de rotas, demanda por destruição (killboard), divergência demanda×preço, guerra & risco e ordens de serviço. |
| **Item** | Inspetor de um item com sub-abas (ver tabela abaixo). |
| **Flips** | "Descobrir flips" por características do mercado + cálculo para itens específicos, com os 4 modos de compra/venda e lucro líquido já descontadas as taxas. |
| **Scanner** | Varre uma categoria inteira (até 800 itens) atrás de oportunidades, com presets de um clique: **Reais → Mercado Negro**, **Entre cidades reais** e **Market making** (ordem → ordem na mesma cidade). Mostra volume diário, potencial de lucro/dia e lucro por kg. Exporta CSV. |

### Sub-abas do inspetor de Item

| Sub-aba | O que faz |
|---|---|
| **Preços por cidade** | Preço de venda mín. e ordem de compra máx. em todas as cidades, por qualidade, com idade dos dados em cores (verde < 30 min, amarelo < 2 h, vermelho mais velho). |
| **Onde vender** | Você já tem o item? Mostra a melhor cidade e método (venda instantânea × ordem) pelo valor **líquido**. |
| **Craft / Refino** | Margem de fabricar/refinar o item por cidade, usando o **retorno de recursos (RRR) real do jogo** derivado do dump — marca a cidade-bônus da categoria (★), opção **usar foco** (RRR maior + prata por foco) e escolha do modo de venda. |
| **De onde vem** | Fontes de drop do item (mobs/baús) ordenadas por fama, do grafo mob→loot do dump — para saber onde farmar a oferta. |
| **Item Lab** | Estatística por item/cidade: VWAP, mediana, z-score **sobre resíduos da tendência**, z robusto (MAD), momentum, volatilidade, qualidade do dado e previsão baseline com intervalo (~68%). Use "buscar da API" para coletar dados novos e **+ watchlist** para vigiar o item. |

### Como ler o score e a confiança

- **Score (0–100, régua absoluta)**: 30% potencial/dia + 25% frescor + 20% ROI +
  15% liquidez + 10% lucro/unidade, com âncoras fixas em escala log (lucro 50k,
  potencial 1M/dia, liquidez 200/dia ⇒ 100 pontos). O score de uma oportunidade
  **não depende** das outras da lista — dá para comparar entre dias e consultas.
- **Conf.** = frescor das duas pontas, **limitado pela liquidez** (item que vende
  < 5/dia nunca passa de "média"; < 1/dia, "baixa").
- **Pot./dia já vem com haircut**: lucro × liquidez × taxa de captura de 20%
  (o teto teórico fica no tooltip). O volume da AODP é um piso — só conta
  quando alguém abre o mercado com o cliente de coleta.
- **Persistência das ordens** (aba Início): % de vezes que a ordem do topo
  ainda existia na coleta seguinte, por idade da ordem — medida nos seus
  próprios snapshots. É a régua empírica do "flip fantasma".
- **Coleta automática**: com o servidor aberto, a watchlist é coletada a cada
  30 min (configurável em `albion/config.py`, registrada em `collection_runs`).
  Quanto mais itens vigiados e mais tempo de coleta, melhores as análises.

Dicas da interface:
- Busque por nome em português, inglês ou id. Atalhos: `t6` (tier), `4.1`
  (tier 4 encanto 1), `@2` (encanto 2). Ex.: `bolsa 5.0`, `elmo soldado t6`.
- Clique numa linha de flip para **copiar o nome do item** e colar na busca do
  mercado dentro do jogo.
- O **✕** oculta um flip que você já executou.
- A **☆** no card do item salva favoritos.
- O toggle **Premium** muda o imposto entre 4% e 8% em todos os cálculos.

## As taxas (verificadas no wiki oficial)

- **Imposto de venda**: 4% com premium / 8% sem — cobrado do vendedor em toda
  venda, inclusive no Mercado Negro.
- **Taxa de anúncio**: 2,5% — cobrada ao criar (e a cada edição de) ordem de
  venda **ou** de compra; não é reembolsada nem se a ordem expirar.
- **Compra instantânea**: zero taxas para o comprador.

| Modo | Fórmula do líquido |
|---|---|
| Compra instantânea | custo = preço |
| Ordem de compra | custo = preço × 1,025 |
| Venda instantânea | receita = preço × (1 − imposto) |
| Ordem de venda | receita = preço × (1 − imposto − 0,025) |

**Mercado Negro** (Caerleon): só compra equipamento de combate, só por ordens do
sistema (você vende instantâneo nelas) e aceita item de qualidade **igual ou
maior** que a da ordem — o app já explora isso automaticamente.

## CLI de análise (`analyze.py`)

Para análises rápidas no terminal — e é por aqui que o Claude consulta os dados
quando você pedir uma análise de mercado:

```
python analyze.py search bolsa --limit 5
python analyze.py prices T4_BAG --qualities 1
python analyze.py flips T5_BAG --min-profit 1000
python analyze.py scan --cat bags --tier-min 4 --min-profit 5000 --volume
python analyze.py sell "Elmo de Soldado do Mestre" --qualities 1
python analyze.py history T4_BAG --cities Caerleon --days 30
python analyze.py gold --count 24
python analyze.py status --detail
python analyze.py recommend --cat weapons --min-volume 10        # score absoluto
python analyze.py lab T4_BAG --cities Caerleon,Lymhurst --fetch  # Item Lab
python analyze.py watch add "Bolsa do Adepto,T5_BAG"             # vigiar itens
python analyze.py collect                                        # coleta a watchlist
python analyze.py collect --cat crafting --sub resources --tier-min 4
python analyze.py survival                                       # persistência das ordens (flip fantasma)
python analyze.py backtest                                       # lucro prometido vs realizado
python analyze.py journals --tier-min 5                          # margem de diários vazio->cheio
python analyze.py report --discord                               # relatório do dia no Discord
python analyze.py refine hide --tier 6 --ench 2 --rrr 53.9       # margem de refino por cidade
python analyze.py craft "Arco do Adepto" --focus                 # margem de craft (RRR real)
python analyze.py origin "Arco do Adepto"                        # de onde o item dropa
python analyze.py micro spread --min-volume 20                   # market-making intra-cidade
python analyze.py micro capital --capital 2000000               # alocar capital por velocidade
python analyze.py prod focus                                     # ranking prata/foco (refino+craft)
python analyze.py prod chain "T5_METALBAR"                       # PnL make-vs-buy da cadeia
python analyze.py logi cargo --buy-city Caerleon --sell-city Martlock --kg 1500  # carga ótima
python analyze.py logi restock --days 7                          # reposição (killboard × mercado)
python analyze.py risk profile --cat bags                        # vol/drawdown/VaR por item
python analyze.py risk size T5_BAG --capital 1000000             # quanto comprar (ajustado a risco)
python analyze.py fc revert --cat crafting --sub refinedresources --signals  # reversão à média
python analyze.py fc pair T5_METALBAR                            # par trading entre cidades
python analyze.py demand burn                                    # giro de consumíveis (killboard)
python analyze.py guild watch                                    # o que adicionar à watchlist
python analyze.py guild makeorbuy                                # fazer vs comprar (guild)
python analyze.py pos add T4_BAG --qty 10 --price 4000           # portfolio: PnL real da guild
python analyze.py indexes --cat crafting --sub resources         # índice de preço (base 100)
python analyze.py intel collect                                  # ingere killboard público
python analyze.py intel top --days 1 --inventory                 # demanda por destruição
python analyze.py prune                                          # compacta snapshots antigos
python analyze.py sql "SELECT COUNT(*) FROM prices"
python analyze.py --format json scan --cat weapons --tier-min 6   (json/csv/table)
```

Todos os preços consultados ficam cacheados em `data/cache.db` (SQLite) — o
comando `sql` permite qualquer consulta somente-leitura sobre esse histórico.
Além da tabela rápida `prices`, novas consultas de preço também alimentam
`price_snapshots`, uma tabela append-only para análises futuras e backtesting.

## Acesso pela rede local (guild)

Em `albion/config.py`, defina `SERVE_LAN = True` para o servidor atender a rede
local (`http://SEU_IP:8528`) e, opcionalmente, `ACCESS_TOKEN = "umasenha"` para
exigir `?token=umasenha` na primeira visita (vira cookie por 30 dias).

## Guia de análises econômicas

O arquivo [`docs/GUIA_ANALISES_ECONOMICAS.md`](docs/GUIA_ANALISES_ECONOMICAS.md)
lista as análises possíveis hoje e as próximas camadas de inteligência:
arbitragem, Mercado Negro, market making, craft/refino, logística, risco,
backtesting, livro de ordens e score de oportunidades.

## Atualizar o banco de itens

Quando o jogo ganhar itens novos (patch grande):

```
python scripts/build_items_db.py --refresh
```

## Estrutura

```
app.py                  servidor FastAPI (porta 8528) + proxy de ícones
albion/config.py        cidades, taxas, limites da API
albion/client.py        cliente AODP com throttle (180/min e 300/5min) e cache SQLite
albion/flips.py         motor de flips e "onde vender"
albion/items.py         busca de itens PT-BR/EN
analyze.py              CLI de análise
web/                    interface (HTML/CSS/JS + Chart.js local)
data/items_db.json      12 mil itens com nomes PT-BR, categoria, peso
data/cache.db           cache de preços/histórico (SQLite)
```

## Limitações conhecidas

- Os dados são da comunidade: uma "oportunidade" com idade alta provavelmente é
  um **flip fantasma** (a ordem já foi consumida). Use os filtros de idade máxima.
- Ordens de venda absurdas (ex.: 999.999 numa bolsa T5) aparecem como dado real
  da API; o volume diário ajuda a identificar essas armadilhas.
- A API é limitada a 180 req/min — escanear 800 itens leva ~30 s na primeira vez
  (depois o cache de 5 min responde na hora).
- Os ícones vêm do serviço oficial de render; se o Cloudflare bloquear, o app
  tenta um proxy local e, em último caso, esconde o ícone (nada quebra).

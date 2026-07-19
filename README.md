<!-- Metadados do Hugging Face Spaces (deploy grátis via SDK Docker). Inócuo no
     GitHub; o HF lê este bloco pra subir o container na porta 7860. -->
---
title: Mercado Albion
emoji: 🐳
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Mercado Albion — Américas

Plataforma de inteligência de mercado do Albion Online (servidor das **Américas**),
com interface web em português e CLI de análise. Cobre consulta de preços e flips,
consultor por orçamento, scanner de categorias, análises avançadas (microestrutura,
produção, logística, risco, demanda por killboard), economia da ilha (trabalhadores,
agricultura, pecuária), planejamento de linha de produção da guild e gestão do
tributo semanal. Roda **local** (SQLite, coleta automática) ou como **piloto na
nuvem** (Render + Supabase, multi-organização). Os preços vêm do
[Albion Online Data Project](https://www.albion-online-data.com/) — dados
crowdsourced: só atualizam quando algum jogador com o cliente de coleta abre
aquele mercado no jogo.

## Como rodar

Requisitos: Python 3.11+ e internet.

```
pip install -r requirements.txt        (só na primeira vez)
python app.py                          (ou dois cliques em run.bat)
```

O navegador abre sozinho em `http://127.0.0.1:8528`. Com o servidor aberto, a
watchlist é coletada automaticamente a cada 30 min (semeada com itens líquidos
no primeiro uso) — quanto mais tempo aberto, melhores as análises.

O app exige conta por padrão. Primeiro administrador:

```powershell
.\.venv\Scripts\python.exe -B manage_accounts.py bootstrap --username admin
```

Para desenvolver **sem login** na própria máquina: `python tools/run_local.py`
(liga `ALBION_AUTH_DISABLED=1`; nunca use na nuvem).

## As abas

| Aba | O que faz |
|---|---|
| **Início** | Recomendações automáticas de flip (score 0–100 em régua absoluta, selos executar/monitorar/cautela), com filtros de descoberta (categoria, tier, volume, ROI…) e o **escore composto** opcional, que funde risco, reversão à média e divergência de demanda ao ranking. |
| **Moedas** | Cotação do ouro (prata por 1 ouro) e o valor da prata em ouro, em janelas de 48 h a 30 dias, com tendência, volatilidade e extremos do período. |
| **Item** | Inspetor de um item com 7 sub-abas: preços por cidade (idade dos dados em cores), onde vender (líquido), craft/refino (RRR real do dump, cidade-bônus, foco), cadeia/wiki, de onde vem (mobs que dropam), risco & previsão e Item Lab (VWAP, z-score sobre resíduos, momentum). |
| **Flips** | Cálculo de rotas de lucro líquido para itens específicos, nos 4 modos de compra/venda, taxas já descontadas. |
| **Consultor** | Diga quanta prata você tem e em que cidade está — devolve a melhor combinação de compras para revender que cabe no orçamento, limitada pela liquidez real (Mercado Negro opcional). |
| **Scanner** | Varre uma categoria inteira (até 800 itens) com presets de um clique: Reais → Mercado Negro, entre cidades reais e market making. Volume diário, potencial/dia e lucro por kg; exporta CSV. |
| **Avançado** | 6 painéis analíticos sobre o cache: **Microestrutura** (spread intra-cidade, alocação de capital), **Produção** (prata por foco, refinar vs vender), **Guild** (fazer vs comprar, ranking de destruição, cesta de regear), **Logística** (prêmio do Mercado Negro, escada de qualidade, mapa de reposição), **Demanda** (giro de consumíveis e qualidade destruída, via killboard) e **Risco** (volatilidade, VaR, correlação). |
| **Ilha** | Economia da ilha pessoal: **Trabalhadores** (margem do diário vazio→cheio + calculadora de felicidade/rendimento), **Agricultura** (lucro por ciclo; o foco vale a semente economizada) e **Pecuária** (lucro firme sem foco; prole extra do foco sai como estimativa). |
| **Linha de Produção** | Planejador da cadeia de produção da guild: escolha os produtos finais e a árvore de receita se expande até o recurso bruto, com quantidades propagadas, RRR por cidade, fazer-vs-comprar por etapa, foco por nó, diagrama arrastável e **lista de compras**. Cadeias salvas por conta. |
| **Guild** | Tributo semanal (operador/admin): metas de entrega por membro, fila de auditoria e relógio de cobrança — reporte pendente pausa, aprovação zera, 14 dias sem cumprir desliga. |
| **Contas** | Administração de usuários pseudônimos: papéis, perfis econômicos, senha temporária exibida uma única vez, limite de 2 IPs por conta e trilha de auditoria de segurança. Sem e-mail nem identidade real. |

### Taxas usadas em todos os cálculos (verificadas no wiki oficial)

Imposto de venda **4%** com premium / **8%** sem (toggle na interface); taxa de
anúncio **2,5%** ao criar ordem de compra ou de venda (não reembolsável); compra
instantânea sem taxa. **Mercado Negro** (só Caerleon): venda instantânea contra
ordens do sistema, sem taxa de anúncio, e aceita qualidade igual ou maior que a
da ordem — o app explora isso automaticamente.

## CLI de análise (`analyze.py`)

Para análises no terminal — é por aqui que o Claude consulta os dados. Os 10
comandos mais usados:

```
python analyze.py search bolsa                    # achar o id do item
python analyze.py prices T4_BAG --qualities 1     # preços atuais por cidade
python analyze.py flips T5_BAG --min-profit 1000  # rotas de lucro líquido
python analyze.py sell T5_BAG                     # melhor cidade/método p/ vender
python analyze.py scan --cat bags --tier-min 4 --volume   # varrer categoria
python analyze.py history T4_BAG --days 30        # preço médio + volume/dia
python analyze.py recommend --cat weapons         # recomendações (score absoluto)
python analyze.py lab T4_BAG --fetch              # VWAP, z residual, momentum
python analyze.py collect                         # coleta a watchlist agora
python analyze.py status --detail                 # cobertura do cache local
```

A lista completa (~40 comandos: refino/craft, microestrutura, produção,
logística, risco, previsão, demanda, guild, portfolio, killboard, SQL
somente-leitura…) está no [`CLAUDE.md`](CLAUDE.md). Tudo opera sobre o cache
em `data/cache.db` — cheque a cobertura com `status --detail` antes de analisar.

## Discord — Fase 0 (webhook, sem bot)

Relatórios diários direto num canal do Discord, sem hospedar nada:

1. No canal, crie um webhook e exporte `ALBION_DISCORD_WEBHOOK=<url>`.
2. `python analyze.py report --discord` posta o relatório do dia;
   `python analyze.py digest --view report|ilha|laborers|advisor [--budget N] --discord`
   posta a visão escolhida (mensagens fatiadas em 1900 chars).
3. Agendamento no Windows: `tools/discord_daily.bat` (usa `schtasks`;
   `PYTHONIOENCODING=utf-8` é obrigatório — o console cp1252 quebra em ▲/▼).

Bot de gateway, vínculo membro→conta e tributo via Discord são fases futuras
(plano em `docs/AUDITORIA_2026-07-02.md`, seção Discord).

## Piloto na nuvem

Guia completo em [`DEPLOY.md`](DEPLOY.md): Render (app) + Supabase (Postgres) +
cron-job.org (sweep do mercado a cada 1 min e killboard a cada 10 min), tudo
grátis e sem cartão. A camada de dados é dual (`albion/store.py`): SQLite local
ou Postgres na nuvem, escolhido pela env `DATABASE_URL`. Na nuvem não há coletor
sempre-ligado — o cron toca `/api/sweep` (varre os ~10,4 mil itens em ~2 h) e
`/api/intel-sweep` (agregado diário de destruição), ambos protegidos por
`ALBION_SWEEP_TOKEN`.

A plataforma é **multi-organização** (Fase 1 aplicada): tabelas `orgs` e
entitlements por org/conta, papéis com escopo de org e cadeias de produção
isoladas por org. Pendência conhecida e deliberada: `/api/admin/*` ainda não é
org-scoped — corrigir antes de aceitar uma segunda guild
(`docs/AUDITORIA_2026-07-02.md`).

## Testes e CI

```
python -m unittest tests.test_core tests.test_laborer tests.test_advisor tests.test_tribute
```

Sem rede externa nos testes (SQLite temporário e fixtures locais). No Windows,
rode com `PYTHONIOENCODING=utf-8` e `ALBION_NO_AUTOCOLLECT=1`. O CI
(`.github/workflows/tests.yml`) roda a suíte a cada push/PR. O dialeto Postgres
tem validador próprio: `python tools/check_pg.py` (exige `DATABASE_URL`).

## Estrutura

```
app.py                  servidor FastAPI (porta 8528) + API web + proxy de ícones
analyze.py              CLI de análise
manage_accounts.py      contas via terminal (bootstrap, reset, recuperação)
albion/                 módulos: client (AODP c/ throttle), store (SQLite/Postgres),
                        flips, advisor, island, prodchain, tribute, auth,
                        microstructure, production, logistics, risk, forecast,
                        demand, guild, gameinfo (killboard)…
web/                    frontend vanilla JS (sem build step)
data/                   items_db.json (~12 mil itens PT-BR), cache.db (SQLite),
                        island_data.json, craft/supply do dump oficial
db/schema_pg.sql        schema do Postgres (piloto)
scripts/                geradores do banco de itens/receitas (rodar após patch)
tools/                  run_local.py (dev sem login), check_pg.py, backup_db.py,
                        discord_daily.bat
tests/                  suíte unittest
docs/                   auditoria, planos e guia de análises econômicas
```

Itens novos após patch grande: `python scripts/build_items_db.py --refresh`,
depois os demais `scripts/build_*.py` (receitas, craft, oferta).

## Limitações conhecidas

- Os dados são da comunidade: uma "oportunidade" com idade alta provavelmente é
  um **flip fantasma** (a ordem já foi consumida). Use os filtros de idade e a
  persistência de ordens medida nos próprios snapshots.
- Ordens-isca (preços absurdos) existem; as análises avançadas saneiam
  preços-âncora automaticamente, mas confira o volume diário antes de agir.
- O volume da AODP é um **piso censurado** — só conta quando alguém abre o
  mercado com o cliente de coleta; o potencial/dia já aplica haircut de 20%.
- A API é limitada a 180 req/min e 300/5 min — escanear 800 itens leva ~30 s na
  primeira vez (depois o cache de 5 min responde na hora).
- Análises que dependem de série longa ou de posições saem como "provisórias"
  até a coleta acumular.
- Os ícones vêm do serviço oficial de render; se o Cloudflare bloquear, o app
  usa proxy local com cache e, em último caso, esconde o ícone (nada quebra).

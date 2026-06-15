# Auditoria Multiagêntica — Mercado Albion (Américas)

**Data:** 15/06/2026 · **Método:** 5 revisores especializados em paralelo (busca/cobertura, backend, frontend, finanças/estatística, SQL/dados), cada achado submetido a um agente **cético** que tentou refutá-lo lendo o código real e rodando comandos. 26 agentes no total.
**Resultado:** 21 achados brutos → **16 confirmados, 5 refutados**. Todos os confirmados de severidade Alta/Média e os Baixos de correção barata foram **corrigidos nesta rodada** (commit `b3dcf59`); os demais Baixos estão documentados com decisão.

Foco especial na sua preocupação recorrente — *"nem todos os itens podem ser pesquisados"*: a causa foi **isolada, quantificada e corrigida** (ver SEARCH-1, SEARCH-2, SEARCH-4).

---

## 1. Achados confirmados e corrigidos

### 🔴 Alta — performance (a raiz da lentidão)

**BACKTEST-1 — `/api/backtest` travava (timeout > 90 s).**
`price_snapshots` (4,6 M linhas, 1,6 GB) só tinha índice com `fetched_at` na última coluna; as consultas por janela de tempo varriam a tabela inteira (110 s só de SQL). **Corrigido:** novo índice `idx_price_snapshots_time (server, fetched_at)` → o plano virou *range seek* (confirmado por `EXPLAIN`), custo caiu para **~0,8 s por par de rodadas**; default do endpoint reduzido para `max_runs=24` (~18 s). Teste de regressão valida que o plano usa o índice.

**RETENTION-1 — `price_snapshots` crescia sem limite; o prune nunca rodava sozinho.**
O `snapshot_prune` só existia no CLI; o servidor acumulava ~1 M linhas/dia indefinidamente — causa-raiz da lentidão de backtest e survival. **Corrigido:** poda automática diária no loop do servidor (`AUTO_PRUNE_INTERVAL_H=24`), **sem VACUUM** (para não segurar o lock); retenção de snapshots brutos baixada de 180 → **7 dias** (o histórico de longo prazo permanece em `price_snapshots_daily`, agregado). Isso estabiliza a tabela num tamanho em que as análises rodam em segundos.

**SURVIVAL-1 — `/api/survival` varria a tabela inteira (~20 s, ~2 GB de RAM) a cada carregamento do dashboard.**
**Corrigido em três frentes:** (a) janela de tempo na consulta (`days`, padrão 3) usando o índice novo; (b) o painel "Persistência das ordens" deixou de **auto-carregar** — agora é **sob demanda** (botão "Carregar persistência"), então o dashboard abre instantâneo; (c) a retenção de 7 dias limita o volume.

### 🟡 Média — cobertura de busca (sua preocupação central)

**SEARCH-2 — o picker truncava em 80 itens-base, em silêncio.**
Buscar uma palavra comum sozinha (`cajado`=271 bases, `capa`=173, `botas`=151) mostrava só 80, sem indicar que havia mais. **Corrigido:** limite do picker 80 → **150**, e quando ainda trunca aparece a linha **"há mais itens — refine a busca"**. Verificado ao vivo: `cajado` agora lista 150 + aviso.

**SEARCH-1 — o Scanner cobria só os primeiros N itens em ORDEM DE ARQUIVO, sem avisar.**
Com o default (Armas, T4-8, máx 300) escaneava 300 de 3.421 armas; famílias inteiras (cajados de fogo/gelo) nunca eram consultadas, e o usuário concluía falsamente "não há flips aqui". **Corrigido:** `/api/scan` agora retorna `items_total`; quando `total > escaneados` o Scanner mostra **aviso em amarelo** ("a categoria tem N itens — só os de maior tier foram escaneados; aumente Máx. itens ou refine"); a seleção passou a ser **ordenada por tier** (valor) em vez de ordem de arquivo; teto de itens elevado para 1500.

**SEARCH-4 — busca AND estrita sem tolerância: query com palavra a mais/divergente dava ZERO.**
`manto de thetford` → 0 (o item é "Capa de Thetford"); `botas de placas do soldado` → 0. **Corrigido:** quando o AND estrito não acha nada e há 2+ palavras, um **fallback parcial** aceita itens que casam a maioria dos tokens, penalizando os que faltam. Verificado: `manto de thetford` agora retorna 28 resultados relevantes; buscas exatas (`bolsa do adepto` → T4_BAG) **não** são degradadas.

### 🟡🔵 Média/Baixa — correção e clareza

**ITEM-1 (média) — o toggle Premium não invalidava o cache do inspetor:** a sub-aba "Onde vender" continuava mostrando o líquido com o imposto antigo (4% vs 8%). **Corrigido:** alternar Premium limpa `itemLoadedFor` e recarrega a sub-aba ativa.

**ITEM-2 (baixa) — trocar de item deixava as sub-abas inativas com dados do item anterior** (flash transitório). **Corrigido:** `selectItem` limpa tabelas e destrói os gráficos das sub-abas antes de recarregar.

**SQL-2 (baixa) — `volume_dia` na divergência usava divisão inteira** (`SUM/COUNT` sem `*1.0`). **Corrigido** (`* 1.0`).

**SQL-3 (baixa) — a janela "recente" da divergência pegava 3 dias-calendário em vez de `recent_days=2`** (off-by-one inclusivo). **Corrigido:** `-(recent_days-1)` alinha à semântica documentada; fixtura de teste ajustada.

**GATHER-2 (baixa) — o "lucro" da ordem de coletor comparava líquido (com imposto) vs VWAP bruto.** **Corrigido:** compara líquido vs líquido (prêmio sobre a média 7d, mesma base tributária).

---

## 2. Confirmados de baixo impacto — decisão de não alterar agora

**SEARCH-3 — ~852 itens-base sem nome PT-BR** (texto cru em maiúsculas: `QUESTITEM_*`, `UNIQUE_*`, `DEBUG_*`, `_NONTRADABLE`). Só achráveis digitando o id. **Decisão:** não corrigir — o cético confirmou que **todos** são itens não-comercializáveis (quest, vaidade, protótipos de dev, fichas), sem preço na AODP e nunca alvo de consulta de mercado. Re-rodar `build_items_db.py --refresh` após um patch grande preenche o que tiver tradução nova.

**GATHER-1 (baixa) — `vol_dia` do coletor usa dias-ativos como denominador**, divergindo do Item Lab (que usa a janela). O cético notou que o **Scanner usa a mesma convenção** do coletor, então não é uma inconsistência universal — é cosmético e não emitiu nenhuma ordem enganosa no cache atual. Documentado; padronização fica para uma rodada de unificação de métricas.

**STATS-1 (baixa) — `momentum_window_days` reporta 7 fixo em séries curtas.** O cético confirmou que o campo **não é exibido em nenhuma tela** (só no JSON cru); o `momentum_pct` em si está correto para a janela usada. Sem impacto de decisão.

**SQL-4 (baixa) — dia de fronteira do prune podia ser subcontado** em execuções repetidas. **Já corrigido** de fato ao reescrever `snapshot_prune` para cortar na **meia-noite UTC** (dias completos) em vez do instante atual — então este achado deixou de existir.

---

## 3. Achados refutados pelos céticos (não eram bugs reais)

| ID | Alegação | Por que foi refutado |
|---|---|---|
| **REFINE-1** | RRR de refino inflaria a margem ~2× (aplicado à barra de tier inferior) | **Premissa errada sobre o jogo.** No Albion o Resource Return Rate incide sobre **todos** os insumos, inclusive a barra refinada — `eff = cost*(1-rrr)` está correto. O exemplo numérico do achado nem batia com o código. |
| **ICON-1** | Injeção no proxy `/icon` via f-string no PowerShell | A regex `^[A-Za-z0-9_@\-\.]+$` rejeita todos os metacaracteres e `quote()` escapa o resto; o próprio achado admitia "não explorável hoje". Hardening hipotético, não bug. |
| **ITEM-3** | `openHistoryForOpportunity` perderia `en`/`weight` (undefined) | O botão só existe na tabela de recomendações, cujas linhas sempre têm `name_en`/`weight` via `compute_flips`. Nunca undefined. |
| **PRUNE-1 / SQL-1** | `aggregated_days` inflado por `total_changes` cumulativo | Verdadeiro em tese, mas o único caminho real (`analyze.py prune`) usa conexão nova e limpa; o número está correto na prática. Corrigido mesmo assim por robustez (uso de `rowcount`). |

---

## 4. Cobertura de busca — veredito final (sua preocupação)

Medição empírica do cético, cruzada com a minha:
- **Por nome completo ou id: 100% dos 6.222 itens-base são alcançáveis** (rank máximo 23). Nunca houve inalcançabilidade absoluta.
- A sensação de "não acho" vinha de três pontos, **todos corrigidos**: truncamento silencioso do picker (SEARCH-2), Scanner enviesado por ordem de arquivo (SEARCH-1) e AND estrito sem fallback (SEARCH-4).
- O que resta é intencional e honesto: itens sem tradução PT (não-comercializáveis) e o aviso "refine a busca" quando o termo é amplo demais.

---

## 5. Verificação

- **28 testes** (unittest) passando, incluindo regressões novas: índice de tempo no plano de consulta, agrupamento de encantos, fallback parcial, e o `default ≤ maximum` de todos os parâmetros de endpoint (que pegou o bug do `/api/collect` na sessão anterior).
- Verificado ao vivo no navegador: busca ampla com aviso, fallback parcial, Scanner com aviso de truncamento, inspetor de item, persistência sob demanda — **console limpo**.
- `node --check` + `py_compile` de todos os módulos OK.

## 6. Próximos passos sugeridos (não-bloqueantes)

1. **Pré-agregar a persistência** numa tabela diária (como `item_demand_daily`) para torná-la instantânea em vez de sob demanda.
2. **Unificar a métrica de volume/dia** entre coletor, Scanner e Item Lab (GATHER-1).
3. **Backtest assíncrono** (job em background) se a guild quiser janelas grandes sem esperar.
4. Fuzzy leve por distância de edição na busca (typos como "bosa"→"bolsa"), além do fallback parcial já implementado.

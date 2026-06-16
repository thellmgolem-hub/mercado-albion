# Melhorias analíticas — roadmap aterrado

> Workflow multiagêntico (16/06/2026) auditou o **código e os dados reais** das ~22
> análises já construídas. 2 das 6 dimensões rodaram completas (killboard, camada
> de dados); as outras 4 sofreram throttle transitório do servidor e foram
> complementadas pela validação cética + conhecimento do autor dos módulos.
> Cada item cita a fragilidade real (arquivo/linha ou nº do cache) e o método de fix.
> Ordenado por **alavancagem** (correção > destrava-dados > rigor > fusão > novos).

---

## A. Correções — os números de hoje saem enviesados (fazer primeiro)

### A1. Normalizar consumo do killboard por EXPOSIÇÃO (horas ingeridas), não por dia-calendário  · alto · médio
`demand.consumable_burn` faz `per_day = units / days`, e `gameinfo.demand_price_divergence`
usa `SUM(victim_units)/COUNT(DISTINCT day)`. Mas a ingestão cobre **4-5 horas em 4 de 5
dias** (runs/dia 17/16/13/85/26 batem com kills 4252/3651/3141/20325/5328). Logo
"demanda/dia" mede **uptime do coletor**, não consumo do jogo — infla dias de alta
coleta e subestima os outros. **Fix:** dividir por `horas_cobertas` (de `kill_events.ts`)
e escalar para 24 h; persistir a exposição por dia. Afeta burn, divergência,
make_or_buy e watchlist_roi.

### A2. `item_family` colapsa artefatos como armas distintas  · médio · fácil
`demand.item_family` só remove tier e `@ench`: `T4_2H_BOW_KEEPER@4 → 2H_BOW_KEEPER`,
`T4_2H_DUALCROSSBOW_HELL → ..._HELL`. No cache os arcos-artefato são o topo de
destruição (BOW_KEEPER@4=235, DUALCROSSBOW_HELL@2=189) — `meta_shift` fragmenta a meta
em "famílias" que são a mesma linha de arma. **Fix:** remover também os sufixos
`_KEEPER/_HELL/_UNDEAD/_AVALON/_CRYSTAL` ao derivar a família.

### A3. Filtrar âncoras stale em make_or_buy / burn / watchlist_roi  · médio · médio
`_cache_price_dicts` lê a tabela `prices` inteira sem filtro de data; `clean_price_rows`
só pega outlier **entre** cidades, não preço velho. **27% dos asks** têm
`sell_price_min_date` > 2 dias e entram no `min()` — `make_or_buy` chega a comparar com
ordem fantasma. **Fix:** descartar pontas com idade > N dias (a data já vem por linha)
antes de min/ranking.

---

## B. Destravar dados — barato e de alta alavancagem (matura muitas análises)

### B1. Preencher `price_snapshots_daily` na coleta, não só na poda  · alto · médio
A tabela de roll-up diário está **VAZIA** — só seria escrita no `snapshot_prune`. **Fix:**
ao fim de `client.collect()`, `INSERT OR REPLACE` agregando o dia corrente
(min/avg/max das duas pontas, COUNT) a partir de `price_snapshots`. Backfill imediato
dos 6 dias (6,07 M linhas) e histórico de longo prazo que sobrevive à retenção de 7 dias.

### B2. History profundo (365 d) p/ itens líquidos + habilitar `time_scale=1`  · médio · médio
Em t24 só **53 itens têm > 180 d**, 1616 têm ≤ 45 d; t1 tem 1 item obsoleto. `collect()`
só pede t24/30 d, embora `HISTORY_FETCH_WINDOWS[24]` já liste 365. A AODP serve 365 d de
uma vez — a profundidade vem **na 1ª recarga**, sem esperar. Destrava regime, reversão,
risco e correlação com séries longas.

### B3. Congelar a série diária de demanda/share por família  · alto · médio
Só existe `item_demand_daily` (por item cru, sem exposição nem família). `meta_shift` e
`soldier_kit` recomputam on-the-fly sobre `kill_event_equipment`, que é **podado**. **Fix:**
nova tabela `demand_family_daily` (dia, família, slot, unidades, exposição) escrita na
ingestão — precisa **começar agora** para amadurecer (hoje seriam ~5 linhas/família).

### B4. Cobertura 24 h UTC + roll-up horário (tarefa do SO)  · alto · fácil(coleta)
A coleta é um daemon **dentro do `app.py`** — se o servidor não fica 24 h ligado, faltam
horas (buraco fixo 05h-10h; `price_snapshots` cobre 19 de 24 h UTC). **Fix:** registrar no
Agendador do Windows um job a cada ~30 min chamando `analyze.py collect`, e guardar um
agregado **horário permanente** (o roll-up diário perde a hora). Destrava timing
intradiário, nowcast e o heatmap de spread.

### B5. Cobrir o gap de exposição da ingestão no pico  · alto · médio
`MAX_OFFSET=1000`, 6 páginas × 51, a cada 10 min. **95% dos runs saturam** a varredura
(max_seen 306, e o pico de 15/06 fez ~26 ev/min) — eventos além do offset 1000 são
**perdidos em silêncio**. **Fix:** paginar mais fundo e/ou intervalo menor em pico, e
registrar a taxa de saturação para saber quando o killboard está subamostrado.

### B6. Watchlist priorizada por ROI-de-informação  · médio · médio
`watch_add` é `INSERT OR IGNORE` sem critério; ~1.090 itens vigiados não têm série.
**Fix:** `watch rebuild` que pontua por volume × variância-de-spread × destruição e mantém
os top-N — coleta foca onde a análise rende.

### B7. Ouro profundo e contínuo  · baixo · fácil
`gold` tem 720 pts (30 d) porque o uso padrão é `count=24`. **Fix:** no loop automático,
`get_gold(count=1000)` 1×/dia. Vira fator macro para risco/previsão (o preço do ouro
move toda a economia).

---

## C. Rigor — tornar confiáveis os números que já mostramos

### C1. Backtest walk-forward dos sinais de previsão  · alto · médio
`mean_reversion` e `pair_trade` emitem `signal`/`direction` mas **nunca são validados** —
`backtest.signal_backtest` só testa o flip instantâneo. **Fix:** para cada disparo
histórico, medir o retorno realizado em t+meia-vida; reportar acerto e edge médio. Sem
isso o "sinal" é promessa não auditada. (history t24 já tem 186 d; 268 + 4346 séries.)

### C2. Refinar-vs-vender com percentil HISTÓRICO do prêmio  · alto · médio
`production.refine_premium` é 100% instantâneo — o próprio docstring admite o gap. **Fix:**
série diária derivada do prêmio (history 187 d dos pares de refino) + `stats` (z sobre
resíduos) → veredito "refine/venda" com percentil 90-180 d, não só o agora.

### C3. Detecção de quebra de regime (Chow/CUSUM caseiro) como porteiro temporal  · alto · difícil
Ausente (só docstring). **Fix:** varredura de breakpoint por SSR segmentado vs único
(reusando `stats.linear_fit`), significância por permutação; a janela pós-quebra vira a
única válida para reversão/VWAP/previsibilidade (evita envenenar sinais pós-patch).

### C4. Suavizar/poolar a demanda; `gate` por dias cobertos  · médio · médio
`meta_shift` usa contagens brutas, `recent=2` pode cair num único dia de 3 h. **Fix:**
média móvel + mínimo de dias bem-cobertos antes de LIGAR o sinal de Δshare.

### C5. Calibrar `VOL_BANDS` aos percentis empíricos  · baixo · fácil
`risk.py:19` tem `[(0.35,'seguro'),(0.75,'médio')]` marcado "(calibráveis)" e nunca
calibrado. **Fix:** tercis da `vol_annual_pct` sobre as ~4346 séries ≥ 60 d, ou selo
relativo (percentil-na-categoria).

### C6. trap_signals: 3º sinal (fantasma por-ordem)  · médio · médio
Faz cruzado + outlier; falta a sobrevivência condicional **por ordem** (pareamento
t→t+1 de `survival`) anexada à decisão. Camada de saneamento que protege flips/recommend.

### C7. Acoplar o tempo de ciclo do `capital_allocation` à curva de `survival`  · médio · médio
Hoje assume ~1 ciclo/dia. **Fix:** usar `survival.persistence` por bucket como
denominador de tempo real (limitado pela série curta, mas tira o "teto orientativo").

---

## D. Integração e fusão de sinais — a camada que falta

### D1. Régua de risco em TODA oportunidade  · médio · médio (alto valor prático)
`risk_profile`/`position_size` existem mas só via `cmd_risk` com item explícito —
`recommend`/`scan`/lab dão lucro **sem nenhuma régua de risco**. **Fix:** anexar
vol/maxDD/VaR e "compre até N" como coluna em recommend/scan e selo no Item Lab.

### D2. Escore composto que FUNDE os sinais  · alto · médio
`opportunity_score` (app.py) só pondera lucro/frescor/ROI/liquidez. **Fix:** fundir num
escore único: flip + market-making + divergência demanda×preço (killboard) + reversão +
risco + previsibilidade — cada um como fator normalizado, com a previsibilidade/regime
como porteiro. É o que transforma 22 análises soltas em um ranking acionável.

### D3. Fechar o loop: registrar trades/feedback e calibrar `CAPTURE_RATE`/`SCORE_ANCHORS`  · médio · médio
`positions` e `service_order_feedback` estão vazias; `backtest` compara prometido vs
mercado, nunca execução real. **Fix:** registrar a aceitação/realização das ordens de
serviço e usar o realizado para calibrar as âncoras e a taxa de captura (hoje fixas).

### D4. Atribuição de PnL marcada a risco  · médio · médio (bloqueado por dado)
`cmd_pos` dá PnL aberto/realizado sem atribuição timing-vs-spread, VaR por posição nem
"qual sinal pagou". Latente até `positions` ser preenchida (ver D3).

---

## E. Upgrades de estimador (Python puro, sem numpy)

### E1. Sobrevivência → Kaplan-Meier  · médio
`survival` usa razão por faixa de idade; KM dá a curva de sobrevivência própria com
censura — régua honesta do flip-fantasma e melhor multiplicador para o lucro-esperado.

### E2. Volatilidade → EWMA / realized vol  · fácil
`risk._pstdev` dos retornos é estática. EWMA (λ≈0.94) responde a mudança de regime e dá
um VaR mais atual.

### E3. Intervalos de confiança por bootstrap  · médio
Meia-vida (AR1), β (par trading) e vol saem como ponto sem incerteza. Bootstrap por
reamostragem (Python puro) dá IC — essencial em série curta para não vender certeza falsa.

### E4. Encolhimento (James-Stein/Bayes) p/ séries curtas  · médio
z de reversão, β e vol de itens com poucos dias deviam encolher para a média da
categoria — corta o excesso de sinais "fortes" em dado fino. Casa com C5 e a
previsibilidade.

### E5. Winsorização consistente vs descarte  · fácil
`clean_price_rows` zera outliers; padronizar winsorização (capar no percentil) onde faz
sentido evita perder informação e mantém a contagem estável.

---

## F. Novas análises ainda pendentes (do backlog, genuinamente não construídas)

- **Cidade-bônus por cadeia** (matriz refino×craft): `vertical_pnl` roda numa cidade só;
  falta o cruzamento inter-cidade. Bloqueio honesto: transporte é só peso (sem frete/risco).
- **Economia de fazenda**: estrutura do dump existe, mas a cobertura de preço dos itens
  de fazenda é esparsa (0-7 de 40 linhas com preço) — bloqueado por dado.
- **Timing intradiário / nowcast / lead-lag / elasticidade defasada**: todos bloqueados
  pela série fina curta (retenção 7 d, sem roll-up horário) — destravam com B2/B3/B4.

---

## Sequência sugerida

1. **Quick wins de correção+dado** (A2, C5, B1, B7) — baratos, e A1/A2 corrigem viés real.
2. **Maturadores** (B2, B3, B4, B5) — começam a acumular já; quanto antes, melhor.
3. **Rigor** (C1, C2, C3) — fazem os sinais valerem como sinal.
4. **Fusão** (D1, D2, D3) — viram tudo num ranking acionável e auto-calibrável.
5. **Estimadores** (E1-E5) — polimento metodológico contínuo.

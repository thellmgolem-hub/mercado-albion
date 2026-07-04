# Auditoria da plataforma — 2026-07-02

Auditoria multiagente (6 dimensões: segurança/multi-tenant, camada de dados dual,
correção do backend, frontend, lacunas de produto, integração Discord). Cada achado
concreto passou por um verificador adversarial independente. **22 achados confirmados,
2 rejeitados, 6 lacunas de higiene.** Branch `multi-inquilino-fase1`.

## Sumário executivo

A base está **saudável**: a Fase 1 multi-inquilino está de fato aplicada e testada
(orgs/entitlements/is_super, `_actor_org`/`_require_entitlement`, ChainStore por org,
`MultiTenantIsolationTests`), e **não há vazamento cross-org** além do ALTA-1 já conhecido
(`/api/admin/*` não org-scoped, deixado de propósito). O `schema_pg.sql` está atualizado
com as tabelas novas — o deploy no Supabase não quebra por drift de schema.

Como o app hoje roda **local (127.0.0.1)** e a hospedagem foi adiada, **nenhum achado é
um bug crítico em produção agora**. Os de segurança são **latentes** (mordem quando
hospedar ou servir na LAN); os de frontend são **polimento de robustez**; os de correção
afetam análises de nicho. O que realmente importa está em 3 baldes:

1. **3 correções que afetam decisão de mercado hoje** (BM sumindo do advisor, sizing de
   risco inflado, corrida de resposta em 6 sub-abas do Item).
2. **2 travas do caminho hospedado** que vão explodir no deploy se não corrigidas antes
   (conexão "idle in transaction" no pooler; confiança cega em header de IP).
3. **Higiene que já dói local**: o `cache.db` está em **1,4 GB** e a retenção documentada
   (7 dias) **nunca roda sozinha**; e **não há backup** das contas/portfolio.

Para o Discord: dá para ligar **um relatório diário via webhook HOJE**, sem bot e sem
hospedagem. O bot de verdade (gateway `discord.py`, não Interactions HTTP) e o tributo
vêm depois, e o caminho de menor risco é construir o **núcleo do tributo web-first** antes
de qualquer bot.

---

## Achados confirmados

### Segurança / multi-tenant

**S1 · [MÉDIA] Confiança default em headers de IP forjáveis** — `app.py:264`
`AUTH_TRUST_PROXY` vem ligado por padrão (`config.py:207`) e `_client_ip` devolve
verbatim `CF-Connecting-IP`/`True-Client-IP` (aceitos incondicionalmente) e o token
**mais à esquerda** do `X-Forwarded-For` — que é o que o próprio cliente injeta.
Sem hospedagem atrás de Cloudflare (Render puro/LAN), o cliente controla esses headers e
**derruba 3 defesas**: rate-limit de login por IP, `MAX_IPS_PER_ACCOUNT`, e o vínculo de
sessão por IP. Latente até hospedar.
**Fix (1 linha p/ o pior caso):** trocar o default de `ALBION_TRUST_PROXY` para `"0"` em
`config.py:207`; opt-in explícito só quando houver borda que saneie os headers. Ideal:
adicionar `ALBION_TRUSTED_PROXIES` e só honrar headers quando `request.client.host` for
um proxy conhecido, contando o XFF a partir da direita pelos hops confiáveis.

**S2 · [BAIXA] Fail-open do token de sweep na LAN** — `app.py:671`
Com `ALBION_SWEEP_TOKEN` vazio, `/api/sweep` e `/api/intel-sweep` ficam **sem auth** mesmo
com auth ligada (estão em `PUBLIC_AUTH_PATHS`). No modo `SERVE_LAN`, qualquer um na rede
chama `/api/sweep?count=400` em loop e queima o orçamento de 180 req/min da AODP.
**Fix:** `if not config.SWEEP_TOKEN and (store.backend() != "sqlite" or config.SERVE_LAN):`
→ 503. Preserva a conveniência dev em localhost.

**S3 · [BAIXA] Vínculo por IP fail-open sem device + migração não mata sessões** — `auth.py:712`
`if ips:` pula a checagem quando a conta não tem device ativo; e `_revoke_legacy_devices`
revoga devices legados mas **não apaga as sessões** — um cookie roubado autentica de
qualquer IP até expirar (7 dias).
**Fix:** na migração, ao revogar device legado, `DELETE FROM auth_sessions` + bump
`session_version` da conta (espelha `reset_device`). Opcional: `if ips:` → fail-closed.

**S4 · [BAIXA] `AuthManager.cleanup()` nunca é chamado** — `auth.py:792`
`auth_login_throttle` e `auth_sessions` crescem sem teto (sessões expiradas só saem se o
token for reapresentado).
**Fix:** chamada preguiçosa com trava de 1h no topo de `login()` (antes de abrir `_tx`,
que não é reentrante), ou no handler `/api/sweep` que o cron da nuvem já toca.

**S5 · [BAIXA] `/icon/{item_id}` fora do `/api` e da auth** — `app.py:2165`
Superfície não autenticada que dispara fetch externo por cache-miss e apende em
`icon_errors.log` **sem rotação**. Sem traversal (regex trava), mas amplifica tráfego/disco.
**Fix:** cache negativo curto (300s) por id que falha + teto de tamanho no log.

### Camada de dados dual (travas do caminho hospedado)

**D1 · [MÉDIA] Leituras deixam conexão "idle in transaction" no Postgres** — `client.py:374`
`_fetch_ages`, leituras finais de `get_prices`/`get_history`/`get_gold`, `sweep_state` e
`gameinfo._checkpoint` fazem SELECT sem commit/rollback na conexão gravável. No pooler do
Supabase (modo transação, porta 6543), cada tick do cron deixa `aodp.db` em transação
aberta — **pina um backend do pool** e, com `idle_in_transaction_session_timeout`, a sessão
é derrubada e **todos os endpoints falham até reiniciar** (não há reconexão).
**Fix:** contextmanager `_read()` que segura o lock e dá `rollback()` no finally (no-op no
SQLite); trocar `with self.db_lock:` por `with self._read():` nos blocos **só-leitura**.

**D2 · [BAIXA] `watch_*`/`sync_static_items` sem gate de backend** — `client.py:491`
`INSERT OR IGNORE INTO watchlist` e `static_items` (fora do `_PG_SCHEMA`) dão traceback no
caminho Postgres.
**Fix:** early-return por backend (`if getattr(self.db,"backend","sqlite") != "sqlite": return`)
nas 5 funções — mantém a decisão de projeto (watchlist é só-SQLite).

**D3 · [BAIXA] `check_pg.py` não cobre `/api/demand` nem `/api/flip-advisor`** — `tools/check_pg.py:53`
O validador de dialeto promete "todos os endpoints do piloto" mas pula dois que leem o
banco. Um `datetime('now')` reintroduzido em `demand.py` passaria batido e só quebraria
em produção.
**Fix:** adicionar `/api/demand` (burn/quality) e `/api/flip-advisor` à lista.

### Correção do backend

**C1 · [MÉDIA] `clean_price_rows` zera ordens legítimas do Mercado Negro** — `microstructure.py:207`
O BM é tratado como âncora e zerado no corte transversal, **censurando exatamente os maiores
prêmios de BM** em `advisor --bm`, `/api/flip-advisor?include_black_market=true`,
`/api/logi?view=bm` e `logi bm` — a rota de maior lucro some do ranking.
**Fix:** excluir linhas `city == "Black Market"` do pool de z (ordens do sistema, prêmio
estrutural). O teste existente segue verde; adicionar caso afirmando que o BM sobrevive.

**C2 · [MÉDIA] `risk size` calcula vol sobre séries de cidades concatenadas** — `analyze.py:2500`
Blocos por cidade emendados criam saltos falsos nas fronteiras (Caerleon ~2× Martlock =
±log(2) espúrio), inflando vol/drawdown → Kelly/units errados.
**Fix:** usar a maior série de **uma** cidade (mesmo critério de `/api/risk`, `app.py:2097`);
idealmente a série da cidade de compra do flip.

**C3 · [BAIXA] `animal_economy` assume ração de custo ZERO sem cotação de planta** — `island.py:188`
Cache recém-semeado (tem animais, não tem culturas) → lucro/dia de pecuária inflado sem aviso.
**Fix:** pular animais com `total_nut > 0 and spn is None` (padrão skip-if-unpriced do módulo).

**C4 · [BAIXA] `cmd_lab`/`cmd_recommend` passam string de cidade crua** — `analyze.py:848`
`lab T4_BAG --cities "Mercado Negro"` não normaliza → resultado vazio com diagnóstico errado
("sem histórico").
**Fix:** `parse_cities()` + re-serializar CSV (já traduz rótulo PT e morre em typo).

**C5 · [BAIXA] `/api/demand` burn mistura janelas** — `app.py:1663`
Queima medida em `days` (0,25–45d) vs oferta **sempre 7d fixos** → cobertura diverge do CLI.
**Fix:** `_market_volume(con, days=days)` (o default 7 fica idêntico).

### Frontend

**F1 · [MÉDIA] Corrida de resposta obsoleta em 6 sub-abas do Item** — `web/app.js:1145`
`loadPrecos`, `loadVender`, `loadCraft`, `loadOrigin`, `loadRisco`, `runHist` não checam se
`state.item` mudou após o `await` (só `loadWiki` checa). Trocar de item rápido → tabela/gráfico
do item errado orientando decisão. **É o mesmo bug que o `happySeq` corrigiu no widget novo.**
**Fix:** replicar a guarda `if (!state.item || state.item.id !== it.id) return;` após cada await.

**F2 · [MÉDIA] `prodFetchGraph` sem guarda de sequência** — `web/app.js:2836`
Duas buscas de `/api/prodchain` concorrentes (adicionar 2 produtos rápido) podem terminar fora
de ordem → nó some do canvas, foco/cidade/posição perdidos.
**Fix:** token monotônico + `if (seq !== prodFetchGraph._seq || pl !== state.prodLine) return;`.

**F3 · [MÉDIA] Descoberta filtrada sobrescrita por recomendações default** — `web/app.js:1074`
Durante coleta automática, o retry de 45s / `pollCollectStatus` / toggle premium recarregam
`dashRecsTable` com filtros **default**, apagando os filtros que o usuário aplicou.
**Fix:** background refreshes reusam `state.dashParams` + guarda de sequência.

**F4 · [MÉDIA] Sem tratamento global de sessão expirada (401)** — `web/app.js:58`
Sessão expira com a aba aberta → todos os painéis mostram "erro" genérico e os timers batem
401 em loop para sempre; nada instrui o usuário a relogar.
**Fix:** em `apiError()`, `if (res.status===401 && body.classList.contains('authenticated')) showLogin('Sessão expirada.')`.

**F5 · [MÉDIA] Toggle premium não invalida a aba Ilha** — `web/app.js:917`
Ilha usa `premium` mas `islSubLoaded` não é limpo → números ficam com imposto antigo, contra o
toast "análises recalculadas". (Consultor também tem checkbox premium dessincronizado.)
**Fix:** limpar `islSubLoaded` e recarregar a sub-aba ativa da Ilha no handler.

**F6 · [BAIXA] `selectPrecosItem` sem `.catch` + usa `r[0]`** — `web/app.js:1536`
Clicar num favorito com API fora → clique engolido; e abre `r[0]` em vez de match exato.
**Fix:** `.catch(() => open(it))` + `r.find(x => x.id === it.id)`.

**F7 · [BAIXA] `navigateToItem` com catch vazio** — `web/app.js:1350`
Clique em wiki-link com API instável → nada acontece, sem feedback.
**Fix:** `catch (e) { toast('erro ao buscar item: ' + e.message); }`.

**F8 · [BAIXA] `loadItemSub` marca carregado antes do fetch** — `web/app.js:1194`
Loader falha → voltar à sub-aba não tenta de novo (Origem/Risco/Cadeia sem refresh).
**Fix:** `delete state.itemLoadedFor.<sub>` no catch de cada loader.

**F9 · [BAIXA] Código morto** — `web/app.js:62`
`apiSend` (l.62-73) e o param `rowAttrs` de `renderTable` nunca são usados.
**Fix:** apagar ambos.

---

## Lacunas de higiene (crítico de completude)

- **L1 · [P1] Retenção nunca roda sozinha — `cache.db` em 1,4 GB.** `SNAPSHOT_RETENTION_DAYS=7`
  mas `price_snapshots` tem 2,44M linhas desde 20/jun; `snapshot_prune` só roda no CLI manual
  `prune`. O auto-coletor coleta a cada 30 min e **nunca poda nem faz VACUUM**. → agendar `prune`
  no loop do coletor (ex.: 1×/dia) + VACUUM periódico.
- **L2 · [P1] Sem backup dos dados insubstituíveis.** Contas/sessões/watchlist/positions vivem no
  mesmo `cache.db` gitignorado; único backup é de 3 semanas atrás (19,5 MB vs 1,4 GB hoje). Disco
  corrompido = perda de contas, PnL e toda a série. → script de backup do `.db` (ou só das tabelas
  de auth/positions) agendado.
- **L3 · [P1] Zero CI.** Sem `.github/`; 3 arquivos de teste versionados, 20+ commits nesta branch
  (incluindo schema de auth/orgs) sem gate automático. → GitHub Actions rodando `python -m unittest`.
- **L4 · [P2] Deps sem pin + API deprecada do FastAPI.** `requirements.txt` só com piso, sem lock;
  `app.py:177` usa `@app.on_event("startup")` (deprecado). Uma major futura pode derrubar o
  auto-coletor/bootstrap no deploy sem mudar código. → pinar versões + migrar para `lifespan`.
- **L5 · [P2] Sem logging estruturado.** Zero `logging` no código; erros do coletor viram `print`
  truncado; ~11 blocos `except: pass`. Falhas de servidor/auth não deixam rastro. → `logging` básico
  em arquivo com rotação.
- **L6 · [P2] Documentação defasada.** README não menciona Ilha/Linha de Produção/Consultor/
  multi-tenant; **CLAUDE.md não documenta a Fase 1 multi-inquilino** (orgs/entitlements) apesar de
  já aplicada. Próximo dev/agente opera às cegas. → atualizar ambos.

---

## Roadmap de produto (visão do dono → o que falta)

Estado: a Fase 1 multi-inquilino existe e está testada. **Do `PLANO_DISCORD_GUILD.md` nada
foi implementado** — as visões "plataforma = console de admin, Discord = interface do membro"
e "tributo semanal com baixa por auditor" são 100% plano. A Linha de Produção é um planejador
**estático** maduro, não um quadro vivo (sem estado por nó, sem proveniência, por-dono e não
por-org). O motor de laborers está completo na CLI, parcial na web (`laborplan` é CLI-only).

| Prioridade | Item | Esforço |
|---|---|---|
| **P0** | Núcleo do tributo (metas/reportes/relógio 14d) **web-first, sem Discord**: tabelas duais + `GuildStore` (espelha ChainStore) + endpoints `/api/guild/*` + máquina de estados testada. Roda 100% local. | dias |
| **P0** | Aba **Guild** no console (atribuir metas, fila de auditoria, relógios) — auditor faz a baixa na web; guild usa de verdade antes de haver bot. | dias |
| P1 | Destravar hospedagem mínima (tarefa #34: Render+Supabase+cron) — dependência dura de qualquer interface de membro. | horas (+ decisão do dono) |
| P1 | Expor `laborplan` na web (`GET /api/laborplan` + card "Plano do dia" na aba Trabalhadores). | horas |
| P1 | Linha de Produção → quadro vivo: cadeia **compartilhada** da org + estado/proveniência por nó. | dias |
| P2 | Relógio → entitlements (renovação/expiração na mesma transação). | dias |
| P2 | Pré-condições SaaS antes de abrir p/ outras guilds (corrigir ALTA-1, `username_norm` global vs por-org, org nova inactive). **Não antecipar.** | semanas |

---

## Integração Discord — plano em fases

Hoje só existe o webhook do CLI (`analyze.py report --discord`, `DISCORD_WEBHOOK_URL` vazio).
A Fase 1 multi-inquilino deixou o gancho pronto: `orgs.discord_guild_id` (índice único) mapeia
servidor→org. O bot **não** consegue autenticar hoje (o `_auth_guard` só conhece cookie).

**Desvio deliberado do plano:** como a hospedagem foi adiada e o dono roda local, usar **bot
gateway (`discord.py`, WebSocket de saída)** em vez de Interactions HTTP — sem URL pública, sem
HTTPS, sem Ed25519, sem keep-alive. Limitação honesta: **o bot só fica online com o PC ligado**
(aceitável p/ 10 membros; documentar). A troca para Interactions HTTP fica para quando hospedar,
e se os handlers forem funções puras chamando a API, essa troca não toca regra de negócio.

- **Fase 0 (HOJE, sem bot, sem hospedagem)** — preencher `DISCORD_WEBHOOK_URL` e agendar
  `analyze.py report --discord` 1–2×/dia via Agendador do Windows. **Armadilha:** o `print` com
  ▲/▼ estoura `UnicodeEncodeError` sob `schtasks` (stdout cp1252) → definir `PYTHONIOENCODING=utf-8`
  na tarefa. Depois: extrair `_post_discord(text)` e adicionar digests de ilha/laborplan/flip-advisor.
- **Fase 1 (local, auth ligada)** — token de serviço no `_auth_guard` (ramo `X-Service-Token`, sem
  IP/CSRF); **bot gateway** com slash-commands read-only (`/preco`, `/buscar`, `/flip <orçamento>`,
  `/ilha`, `/felicidade`); vínculo membro→conta→org por código (`gen_link_code`) resolvendo org via
  `orgs.discord_guild_id`. Assinante analytics-only fica só nos comandos públicos de preço — **nunca**
  metas/chains/status de membro.
- **Fase 2 (tributo)** — `GuildStore` + relógio de 14 dias (task asyncio no próprio bot, sem cron
  externo) + endpoints de escrita `/api/guild/*` com máquina de estados testada; automação do cargo
  "Ferramentas" (trivial no gateway: `member.add_roles`); quadro de produção espelhado (1 embed/semana
  editado a cada aprovação) + metas a partir de cadeia salva (`assign-from-chain` via `prodchain.solve`).
- **Futuro (exige hospedagem)** — trocar gateway por Interactions HTTP (Ed25519, defer, keep-alive,
  clock-tick via cron-job.org). Nada das fases 0–2 é jogado fora.

---

## Rejeitados (2)

Dois achados propostos pelos auditores foram derrubados na verificação (não reais / defensáveis).
Não listados por não gerarem ação.

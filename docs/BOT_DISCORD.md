# Bot Discord da guild — guia de instalação (Fase 1)

O bot (`tools/discord_bot.py`) conecta ao Discord pelo **gateway WebSocket**
(discord.py 2.x). A conexão é de SAÍDA: não precisa de URL pública, HTTPS nem
verificação Ed25519 — roda em qualquer PC, atrás de NAT/firewall doméstico.
Ele traduz slash commands em chamadas à API do app, autenticadas por um
**token de serviço** (header `X-Service-Token`).

> **Limitação honesta**: o bot roda no SEU computador. Ele só fica online
> enquanto o PC estiver ligado com `python tools/discord_bot.py` E o app
> (`python app.py`) rodando. Se qualquer um dos dois cair, os comandos param
> de responder (o Discord mostra "O aplicativo não respondeu"). Para o bot
> ficar 24/7 é preciso hospedá-lo (fase futura — os handlers já são funções
> puras transport-agnósticas, prontos para portar a Interactions HTTP).

## Pré-requisitos

- App rodando local (`python app.py`, porta 8528) com pelo menos 1 admin
  criado (`python manage_accounts.py bootstrap <usuario>`).
- Python com `discord.py` 2.x: `pip install -U discord.py`
  (o `httpx` o projeto já usa).

## 1. Criar a Application e o Bot no Developer Portal

1. Acesse <https://discord.com/developers/applications> → **New Application**
   → dê um nome (ex.: "Mercado Albion").
2. Menu **Bot**:
   - **Reset Token** → copie o token (aparece UMA vez). Ele vira a env
     `DISCORD_BOT_TOKEN`. Trate como senha — quem tem o token controla o bot.
   - Em **Privileged Gateway Intents**, deixe TUDO desligado
     (Presence / Server Members / Message Content). O bot só usa slash
     commands — as intents padrão bastam.
3. Menu **Installation** (ou OAuth2 → URL Generator):
   - Escopos: **`bot`** + **`applications.commands`**.
   - Permissões do bot: nenhuma é obrigatória para slash commands (as
     respostas vão pelo webhook da interação). Marque só **Send Messages**
     se quiser folga para o futuro.
4. Abra a URL de convite gerada, escolha o servidor da guild e autorize.

## 2. Gerar o token de serviço (lado do app)

O bot NÃO usa login/senha: ele se apresenta à API com um token de serviço
com escopos mínimos. Na pasta do projeto:

```
python manage_accounts.py service-token create --label discord_bot --scopes discord_link,discord_read
```

Anote o token `svc_...` exibido — ele **não aparece de novo** (o banco guarda
só o hash). Ele vira a env `ALBION_SERVICE_TOKEN`.

Gestão: `service-token list` (metadados; nunca o segredo) e
`service-token revoke --id N` (revoga na hora — o bot passa a receber 401).

## 3. Rodar o bot

PowerShell (na pasta do projeto, com o app já rodando):

```powershell
$env:DISCORD_BOT_TOKEN = "<token do passo 1>"
$env:ALBION_SERVICE_TOKEN = "svc_<token do passo 2>"
# opcional: id do SEU servidor p/ os comandos aparecerem na hora
$env:DISCORD_GUILD_ID = "123456789012345678"
python tools/discord_bot.py
```

Envs:

| Env | Obrigatória | Default | Para quê |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | sim | — | token do bot (Developer Portal → Bot) |
| `ALBION_SERVICE_TOKEN` | sim | — | token `svc_...` do passo 2 |
| `ALBION_API_URL` | não | `http://127.0.0.1:8528` | base da API do app |
| `DISCORD_GUILD_ID` | não | — | sync instantâneo dos comandos num servidor |
| `ALBION_PUBLIC_URL` | não | `https://mercado-albion.onrender.com` | base PÚBLICA p/ os ícones dos embeds (o Discord busca a imagem pela internet, não pelo `ALBION_API_URL` interno) |

Sem `DISCORD_GUILD_ID` o registro dos comandos é **global** e o Discord pode
levar até ~1 hora para exibi-los. Com a env, aparecem imediatamente no
servidor indicado (recomendado no primeiro uso).

## 4. Vincular os membros (uma vez por pessoa)

1. O admin gera um código para a conta do membro (15 min de validade, 1 uso):
   `python manage_accounts.py link-code <account_id>`
   (ids em `python manage_accounts.py list`).
2. O admin passa o código ao membro (8 caracteres, sem 0/O/1/I/L).
3. O membro digita `/vincular <código>` no Discord — a resposta é efêmera
   (só ele vê). Pronto: `/minhas-metas` e `/meu-status` passam a funcionar.

A org dos dados vem SEMPRE da conta vinculada (`auth_accounts.org_id`) —
nunca do servidor Discord de onde o comando foi digitado.

## Mapa comando → endpoint

| Comando | Endpoint da API | Visibilidade |
|---|---|---|
| `/preco <item>` | `GET /api/search` (resolve o id) + `GET /api/prices` | pública |
| `/comparar <item>` | `GET /api/prices` (todas as cidades: barras + melhor rota de flip) | pública |
| `/buscar <termo>` | `GET /api/search?group=true` | pública |
| `/builds <arma> [conteúdo]` | `data/builds.json` (embed com ícone da arma + itens em PT-BR + habilidades; sem chamar a API) | pública |
| `/flip <orçamento> [cidade]` | `GET /api/flip-advisor` | pública |
| `/ilha` | `GET /api/island?view=laborers` (top 5) | pública |
| `/felicidade <tier> [prédio] [trabalhadores] [família]` | `GET /api/laborer-happiness` (painel do jogo + rendimento por diário; assume mobília/troféus ideais = teto da config) | pública |
| `/vender <item>` | `GET /api/prices` (ranking de venda por cidade) | pública |
| `/ouro` | `GET /api/gold` (cotação + tendência 48h) | pública |
| `/recomendar` | `GET /api/recommendations` (top 5 do dia) | pública |
| `/plano [família] [tier] [trabalhadores]` | `GET /api/laborplan` (cesta + lucro/dia) | pública |
| `/quadro` | `GET /api/discord/board` (metas × entregues × relógio da guild) | pública |
| `/vincular <código>` | `POST /api/discord/link` | **efêmera** |
| `/minhas-metas` | `GET /api/discord/my-assignments` | **efêmera** |
| `/meu-status` | `GET /api/discord/my-status` | **efêmera** |
| `/reportar <item> <qtd> [nota]` | `POST /api/discord/report` (liga a meta da semana sozinho; relógio pausa) | **efêmera** |
| `/pendentes` | `GET /api/discord/pending` (só auditor vinculado) | **efêmera** |
| `/aprovar <id> [nota]` | `POST /api/discord/approve` (zera relógio; alimenta a cadeia) | **efêmera** |
| `/rejeitar <id> [nota]` | `POST /api/discord/reject` (relógio retoma) | **efêmera** |

"Efêmera" = só o autor do comando vê a resposta (metas e relógio de tributo
são dados pessoais do membro). Erros da API viram mensagens amigáveis em
PT-BR — o bot nunca despeja traceback no canal (fica no console dele).

## Intuitivo para iniciante (sem decorar nome de item)

- **Autocomplete** em `/preco`, `/vender`, `/comparar` e `/buscar`: digite 2+
  letras (PT ou EN) e **escolha da lista** — o `value` da escolha é o id do item,
  e `_resolve_item` prioriza id exato, então a seleção resolve direto.
- **`/builds`**: dois menus (árvore de arma + conteúdo) — nenhum texto. Lê
  `data/builds.json` (gerado por `scripts/build_builds_data.py` a partir do guia
  da guild: cada item já resolvido em **nome PT-BR oficial + id + ícone**). O
  embed mostra o ícone da arma como thumbnail e lista arma/cabeça/peito/pés/capa/
  poção/comida em PT. Regenerar após editar o guia:
  `python scripts/build_builds_data.py --generate <saida.json>`.
- **`/comparar`**: barras ASCII do preço por cidade + a melhor rota de flip
  (comprar mais barato → vender na maior ordem de compra).

### Imagens de loadout das builds (estáticas)

`/builds` mostra, além do embed, uma **imagem de loadout** (todos os ícones dos
itens montados numa figura) via `embed.set_image`. Elas são **PRÉ-GERADAS** por
`scripts/build_build_images.py` e servidas como estáticos em `web/builds/*.png`
(o campo `image` de cada build em `data/builds.json` aponta o caminho). Motivo de
serem estáticas: `render.albiononline.com` bloqueia o fetch server-side via
Cloudflare e a nuvem Linux não tem o fallback PowerShell do `/icon`; gerando no
Windows (onde o fallback funciona) e comitando os PNGs, a nuvem só entrega a
imagem pronta. A **thumbnail** da arma no embed aponta direto pro render oficial
(`item_render_url`) — o proxy de imagem do Discord busca de lá. Regenerar após
mudar as builds: `python scripts/build_builds_data.py --generate <saida>` e
depois `python scripts/build_build_images.py`.

## Quadro semanal automático (opcional)

Com as envs `ALBION_BOARD_CHANNEL_ID` (id do canal, ex.: #tributo — clique
direito no canal → Copiar ID, com o modo desenvolvedor ligado) e
`DISCORD_BOARD_USER_ID` (snowflake de um membro VINCULADO — define de qual
org é o quadro; normalmente o do dono), o bot posta o quadro no canal e
**edita a mesma mensagem** 4×/dia e após cada `/aprovar` ou `/rejeitar`.

## Bot embarcado no servidor (nuvem)

Na nuvem (Render) NÃO é preciso rodar `python tools/discord_bot.py`: defina
apenas `DISCORD_BOT_TOKEN` no Environment — o servidor sobe o bot dentro do
próprio processo, provisiona sozinho o token de serviço (label `inproc-bot`,
rotacionado a cada boot) e o cron de 1 min que mantém o app acordado mantém o
bot online 24/7. Blueprint de canais sugerido: `#mercado` (comandos públicos),
`#tributo` (comandos efêmeros + quadro automático), `#auditoria` (cargo de
oficial; /pendentes /aprovar /rejeitar funcionam em qualquer canal, efêmeros).

## Problemas comuns

- **Os comandos não aparecem no Discord** — sem `DISCORD_GUILD_ID` o sync
  global demora até ~1h. Defina a env com o id do servidor (clique direito no
  nome do servidor → "Copiar ID do servidor"; requer o Modo Desenvolvedor em
  Configurações → Avançado) e reinicie o bot.
- **"Não consegui falar com a API do app"** — o `python app.py` não está
  rodando, ou `ALBION_API_URL` aponta para o lugar errado.
- **"Token de serviço inválido ou revogado"** — o `ALBION_SERVICE_TOKEN` está
  errado, foi revogado, ou o app está sem admin (bootstrap pendente). Gere
  outro token (passo 2).
- **"O aplicativo não respondeu"** (erro do próprio Discord) — o bot está
  offline (PC desligado / processo parado) ou demorou mais de 3 s para
  confirmar; o bot usa `defer`, então isso normalmente significa bot parado.
- **`/minhas-metas` responde 403 de plano** — a org da conta do membro está
  sem o entitlement `operacao` ativo (mesma regra da aba Guild na web).

## Próximas fases

Bot hospedado (Interactions HTTP com verificação Ed25519), reporte de tributo
direto pelo Discord e espelho de papéis: ver `docs/AUDITORIA_2026-07-02.md`
(seção Discord) e `docs/PLANO_DISCORD_GUILD.md`.

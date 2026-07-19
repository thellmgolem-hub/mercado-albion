# Publicar o piloto — grátis e sem cartão (Render + Supabase + cron)

Guia passo a passo. Três peças grátis: **Supabase** (banco), **Render** (roda o
app), **cron-job.org** (o "despertador" que alimenta o sweep e mantém o app
acordado). Nada de cartão. Tempo: ~30–45 min.

> Por que Render e não Vercel: o app é um processo que mantém ~17 MB em memória
> (Wiki) e é alimentado por um toque a cada minuto. Render roda isso como um
> servidor de verdade; o sweep de 1 min ainda o mantém acordado (o plano free
> dorme após 15 min ocioso). Detalhes em `tools/check_pg.py` e `db/schema_pg.sql`.

## 1) Supabase (o banco) — sem cartão
1. Crie conta em supabase.com → **New project**. Guarde a senha do banco.
2. Menu **SQL Editor** → **New query** → cole TODO o conteúdo de
   `db/schema_pg.sql` → **Run**. (Cria as tabelas do piloto.)
3. Menu **Project Settings → Database → Connection string** → aba **Transaction
   pooler** (porta **6543**). Copie a URL. Troque `[YOUR-PASSWORD]` pela senha do
   passo 1. Essa é a sua **DATABASE_URL**.

## 2) Render (roda o app) — sem cartão
1. Suba este repositório no GitHub (privado tudo bem).
2. render.com → entre com o GitHub → **New → Blueprint** → escolha o repo.
   O Render lê o `render.yaml` e propõe o serviço `mercado-albion` (free).
3. Em **Environment**, preencha:
   - `DATABASE_URL` = a URL do pooler do Supabase (passo 1.3)
   - `ALBION_BOOTSTRAP_ADMIN` = `admin` (só nesta primeira vez)
   - (`ALBION_SWEEP_TOKEN` o Render gera sozinho; `ALBION_AUTH_COOKIE_SECURE`
     já vem como `1`.)
4. **Create / Deploy**. Espere o build. Anote a URL pública
   (ex.: `https://mercado-albion.onrender.com`).

## 3) Primeiro login (admin)
1. Aba **Logs** do serviço no Render: procure a linha
   `[BOOTSTRAP] SENHA TEMPORARIA: ...`. Copie a senha.
2. Abra a URL do app → entre com usuário `admin` + essa senha → **troque a
   senha** quando pedir.
3. Volte no Render → **Environment** → **apague** a variável
   `ALBION_BOOTSTRAP_ADMIN` (já cumpriu o papel).

## 4) A coleta — GitHub Actions (banda grátis), NÃO no Render
> ⚠️ MUDANÇA IMPORTANTE (jul/2026): a coleta NÃO roda mais no Render. O Render
> tem só **5 GB/mês de banda no free** e a coleta baixava ~25 GB/mês da AODP →
> o serviço foi **SUSPENSO por banda**. A coleta agora roda no **GitHub Actions**
> (banda grátis e ilimitada), gravando direto no Supabase. O Render só SERVE.
> **NUNCA aponte um cron para `/api/sweep` ou `/api/intel-sweep`** — isso re-liga
> o download no Render e re-cria o incidente.

**Ligar a coleta (uma vez):**
1. O repositório precisa ser **público** (destrava minutos ilimitados do Actions;
   o código não tem segredo — o `DATABASE_URL` vai por Secret cifrado). Em repo
   privado o teto de 2.000 min/mês estoura.
2. No GitHub: **Settings → Secrets and variables → Actions → New secret** →
   `DATABASE_URL` = a string do pooler do Supabase.
3. Mesma tela, aba **Variables → New variable** → `COLETOR_ATIVO` = `1`.
4. Pronto: `.github/workflows/coletor.yml` varre o mercado a cada 15 min e
   `coletor-poda.yml` poda 1×/dia. O killboard (killfeed/demanda) vem junto no
   mesmo job. Acompanhe em **Actions**.

**No Render (env do serviço):** deixe `ALBION_NO_WATCHDOG=1` (já vem no
render.yaml). Sem isso, o vigia interno reassume a coleta no vão entre jobs do
Actions e **volta a gastar banda no Render**.

**Manter o Render acordado + vigiar a saúde (opcional, redundância):** o cron-job.org,
se usado, deve bater SÓ em `https://SUA-URL.onrender.com/api/health` a cada
~10 min (endpoint público e leve). Isso acorda o Render e mantém o Supabase vivo,
**sem baixar nada**. O keep-alive interno do bot já faz isso; o cron externo é
redundância, não dependência.

**Alarme (recomendado):** ligue o workflow `vigia-externo.yml` — crie a variável
`PUBLIC_URL` (a URL do app) e o secret `ALBION_ALERT_WEBHOOK` (webhook de um canal
de alertas). Ele checa a saúde a cada 15 min e AVISA no Discord se algo cair —
você sabe antes da guilda. Ligue também os alertas de e-mail de cota nativos do
**Render** e do **Supabase** (cobrem banda/disco sem depender de código).

## Conferir se está vivo
- `https://SUA-URL.onrender.com/api/status` mostra `backend: postgres` e as
  linhas de `prices`/`history` subindo conforme o cron roda.
- O endereço raiz abre a plataforma. As "Recomendações" e o hub Avançado
  começam vazios e enchem nas primeiras horas de sweep.

## Acesso das contas (limite de IPs)
- Cada conta é usável de no **máximo 2 IPs** diferentes (anti-compartilhamento).
  Um 3º IP é recusado no login. Para liberar, o admin clica **Liberar IPs** na aba
  **Contas** (ou roda `manage_accounts.py reset-device <id>`).
- O IP real vem do proxy do Render: `ALBION_TRUST_PROXY=1` (já no `render.yaml`)
  manda o app usar o último IP do `X-Forwarded-For` — o que o proxy anexou, o
  único não-forjável. O padrão é **desligado**: sem proxy na frente, esse header
  seria forjável. Se puser uma CDN na frente (ex. Cloudflare), defina também
  `ALBION_EDGE_HEADER` com o header que ela grava (ex.: `cf-connecting-ip`).
- O coletor automático local NÃO roda na nuvem (lá quem alimenta é o cron do
  passo 4). As "Recomendações" enchem conforme o sweep varre o mercado.

## Avisos honestos
- **Supabase free pausa** após ~1 semana SEM atividade. O cron contínuo evita
  isso; se o cron parar por dias, "despause" o projeto no painel do Supabase.
- **Ícones**: o proxy de ícone é Windows-only; na nuvem o navegador busca os
  ícones direto do CDN do Albion (fallback do frontend) — funciona normal.
- **Killboard**: o piloto guarda só o **agregado diário** de destruição
  (kill_demand_daily, ~MB), então **Guild** (fazer-vs-comprar, regear, ranking)
  e o **mapa de reposição** da Logística funcionam (via o 2º cron). O que NÃO
  volta é o que precisa do evento cru: **PvP puro** (meta de armas, win-rate) —
  limite estrutural, não de espaço.

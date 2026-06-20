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

## 4) O cron (o coração) — cron-job.org, sem cartão
1. No Render → **Environment** → copie o valor de `ALBION_SWEEP_TOKEN`.
2. cron-job.org → crie conta → **Create cronjob**:
   - **URL**: `https://SUA-URL.onrender.com/api/sweep?token=SEU_TOKEN&count=100`
   - **Schedule**: a cada **1 minuto**.
3. Salve e **ative**. Pronto: a cada minuto ele busca 100 itens e mantém o app
   acordado. Em ~1,5–2 h varre os ~10.400 itens e recicla, sozinho.
4. **(Opcional, recomendado) Segundo cronjob — killboard.** Crie outro cronjob:
   - **URL**: `https://SUA-URL.onrender.com/api/intel-sweep?token=SEU_TOKEN`
   - **Schedule**: a cada **10 minutos**.
   Esse alimenta o agregado de destruição (kill_demand_daily) que liga o hub
   **Guild** (fazer-vs-comprar, regear, ranking de destruição) e o **mapa de
   reposição** da Logística. Sem ele, essas telas ficam vazias (o resto funciona
   normal).

## Conferir se está vivo
- `https://SUA-URL.onrender.com/api/status` mostra `backend: postgres` e as
  linhas de `prices`/`history` subindo conforme o cron roda.
- O endereço raiz abre a plataforma. As "Recomendações" e o hub Avançado
  começam vazios e enchem nas primeiras horas de sweep.

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

# Servidor Discord "Operarius" — guia de montagem

Guia para o dono da guild criar o servidor no **seu próprio Discord** e plugar o bot
**Mercado Albion** (`client_id 1526297405903994910`), já hospedado 24/7 no Render.

O bot tem **17 comandos** (slash `/`). Os de **mercado/ilha são públicos** (funcionam assim
que o bot entra no servidor). Os de **tributo** precisam de 1 passo extra (login admin no
painel web + vínculo de membros) — ver a seção "Tributo" no fim.

---

## Passo 1 — Criar o servidor

1. No Discord, canto inferior esquerdo → botão **+** (Adicionar um servidor).
2. **Criar meu próprio** → **Para mim e meus amigos**.
3. Nome: **Operarius**. Criar.

## Passo 2 — Criar os cargos (Configurações do servidor → Cargos)

- **@Oficial** — dá acesso ao canal de auditoria. Cor a gosto. Marque as permissões que
  quiser (não precisa de nada especial pro bot).
- **@Membro** — o cargo padrão (`@everyone`) já cobre; opcionalmente crie @Membro pra
  organizar.

## Passo 3 — Criar as categorias e canais

Crie 2 categorias e os canais de texto abaixo:

```
📈 MERCADO
   #comece-aqui        (só leitura p/ membros — guia fixado)
   #mercado
   #ilha-e-producao

🏰 GUILD
   #tributo
   #auditoria          (privado: só @Oficial e o bot enxergam)
```

Para deixar **#comece-aqui** só-leitura: editar canal → Permissões → `@everyone` →
desligar **Enviar mensagens** (deixe **Ver canal** ligado).

Para deixar **#auditoria** privado: editar canal → Permissões → `@everyone` → desligar
**Ver canal**; adicionar **@Oficial** com **Ver canal** ligado.

## Passo 4 — Convidar o bot

Abra este link no navegador (logado no seu Discord), escolha o servidor **Operarius** e
**Autorize**:

```
https://discord.com/oauth2/authorize?client_id=1526297405903994910&permissions=2048&scope=bot+applications.commands
```

O bot **Mercado Albion** aparece na lista de membros. (A permissão pedida é só "Enviar
mensagens", pro quadro automático de tributo.)

## Passo 5 — (recomendado) comandos aparecerem NA HORA

Sem isso, os comandos `/` podem demorar até ~1h pra aparecer (sync global do Discord).
Para aparecerem imediatamente no Operarius:

1. Discord → Config. do usuário → **Avançado** → ligar **Modo desenvolvedor**.
2. Clique com o botão direito no nome do servidor **Operarius** → **Copiar ID do servidor**.
3. No **Render** → serviço mercado-albion → **Environment** → **Add variable**:
   - Key: `DISCORD_GUILD_ID`
   - Value: (cole o ID do servidor)
   - **Save, rebuild, and deploy**.

## Passo 6 — Fixar as mensagens-guia

Em cada canal, cole a mensagem correspondente (seção abaixo) e **fixe** (clique na
mensagem → ⋯ → Fixar mensagem).

---

## Mensagens para colar e fixar

### #comece-aqui
```
👋 Bem-vindo ao **Operarius** — o QG da guild no Albion (servidor Américas).

Aqui temos um assistente de mercado: o bot **Mercado Albion**. É só digitar `/` em
qualquer canal e escolher um comando — a resposta vem na hora.

📊 **Aberto a todos** (use em #mercado e #ilha-e-producao):
• `/preco` — preço de um item por cidade
• `/vender` — melhor cidade pra vender
• `/ouro` — cotação do ouro + tendência
• `/flip` — melhores compras pro seu orçamento
• `/recomendar` — as 5 melhores oportunidades agora
• `/buscar` — achar o nome/id de um item
• `/ilha` — melhores diários de trabalhador
• `/felicidade` — móveis/troféus pra felicidade do trabalhador
• `/plano` — plano de produção pra N trabalhadores

🎯 **Tributo da guild** (use em #tributo):
• `/vincular` — liga seu Discord à sua conta (peça o código a um @Oficial)
• `/minhas-metas`, `/meu-status`, `/reportar`, `/quadro`

Dúvidas? Chame um @Oficial. Bons negócios! 💰
```

### #mercado
```
💹 **MERCADO** — preços, ouro e flips. Digite `/` e escolha:

• `/preco` item: `bolsa t4` → preço atual por cidade
• `/vender` item: `manto t6` → melhor cidade pra vender
• `/ouro` → cotação + tendência de 48h
• `/flip` orcamento: `500000` → melhores compras pro seu bolso
• `/recomendar` → top 5 oportunidades do dia
• `/buscar` termo: `espada t7` → descobrir o id de um item

Preços vêm do Albion Online Data Project (Américas), atualizados de minuto em minuto.
```

### #ilha-e-producao
```
🏝️ **ILHA & PRODUÇÃO**

• `/ilha` → top 5 diários de trabalhador (margem vazio→cheio)
• `/felicidade` tier_trabalhador: `7` familia: `Ferreiro`
     → móveis e troféus ideais + rendimento por diário
• `/plano` familia: `Ferreiro` trabalhadores: `5`
     → plano de produção (o que craftar e o lucro/dia)
```

### #tributo
```
🎯 **TRIBUTO DA GUILD**

Primeiro, vincule sua conta (só uma vez):
1️⃣ Peça a um @Oficial um **código de vínculo** (8 letras).
2️⃣ Digite: `/vincular` codigo: `SEU-CÓDIGO`

Depois é só usar:
• `/minhas-metas` → o que você precisa entregar esta semana
• `/meu-status` → seu relógio (quanto tempo falta)
• `/reportar` item: `minério t4` quantidade: `500` → avisa uma entrega
• `/quadro` → o placar da guild (metas × entregas)

🔒 As respostas de tributo são **privadas** — só você as vê.
```

### #auditoria (só @Oficial)
```
🛡️ **AUDITORIA** (oficiais)

• `/pendentes` → fila de reportes aguardando aprovação
• `/aprovar` id: `12` → aprova a entrega (zera o relógio do membro)
• `/rejeitar` id: `12` → rejeita (o relógio volta a contar)

Para gerar um código de vínculo pra um membro: painel web da plataforma
(https://mercado-albion.onrender.com — aba admin).
```

---

## Tributo — o que falta pra funcionar 100%

Os comandos de **mercado e ilha já funcionam** assim que o bot entra no servidor. Os de
**tributo** (`/vincular`, `/reportar`, `/minhas-metas`, `/pendentes`, `/quadro`…) dependem de:

1. **Login admin no painel web** (https://mercado-albion.onrender.com) — a senha temporária
   de bootstrap aparece 1 vez no log do Render (procure por `BOOTSTRAP`). Trocar no 1º acesso.
2. Com o admin logado: **vincular a própria conta** (gera um código no painel → usa `/vincular`
   no Discord) e **definir as metas da semana** (aba Guild).
3. Só então os membros vinculam e reportam.

Isso é o "passo A" que ficou pra depois. Quando quiser, o Claude te guia.

## Quadro automático (opcional, depois do tributo)

Com as envs `ALBION_BOARD_CHANNEL_ID` (id do #tributo) e `DISCORD_BOARD_USER_ID` (seu
snowflake de membro vinculado) no Render, o bot posta o quadro no #tributo e **edita a mesma
mensagem** 4×/dia e após cada aprovação/rejeição.

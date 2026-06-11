# Schema real da API gameinfo (validado em 2026-06-11, servidor Américas)

Base: `https://gameinfo.albiononline.com/api/gameinfo`
(Europa: `gameinfo-ams.albiononline.com`, Ásia: `gameinfo-sgp.albiononline.com`)

## /events — eventos de kill

- Paginação: `limit` máx. **51**; `offset` máx. **1000** (1051 → HTTP 400).
  Cobertura por varredura: ~1.050 eventos mais recentes.
- Campos do evento: `EventId`, `TimeStamp` (ISO com sufixo Z), `BattleId`
  (igual ao EventId em kills isoladas), `Type` (ex.: KILL), `Category`,
  `KillArea` (ex.: OPEN_WORLD), `Location` (**veio nulo em 51/51 na
  amostra — não prometer risco por zona via Location**), `GvGMatch`,
  `TotalVictimKillFame`, `numberOfParticipants`, `groupMemberCount`,
  `Killer`, `Victim`, `Participants[]`, `GroupMembers[]`.
- Killer/Victim/Participants: `Id`, `Name`, `GuildId/Name`, `AllianceId/Name/Tag`,
  `AverageItemPower`, `KillFame`, `DeathFame`, `Equipment` (slots: MainHand,
  OffHand, Head, Armor, Shoes, Bag, Cape, Mount, Potion, Food — cada um
  `{Type, Count, Quality}`; Type usa o id de mercado, ex.
  `T4_MAIN_HOLYSTAFF_AVALON@2`), `Inventory[]` (vítima: itens carregados —
  essencial para interdição de transporte). Participants trazem `DamageDone`/
  `SupportHealingDone`.

## /battles — batalhas

- `id`, `startTime`, `endTime`, `timeout`, `battle_TIMEOUT`, `totalKills`,
  `totalFame`, `clusterName` (pode ser nulo), `players{}`, `guilds{}`,
  `alliances{}` (dicts id → {name, kills, deaths, killFame}).
- Paginação: `limit`/`offset` + `sort=recent`.

## Regras operacionais adotadas

- Coleta incremental por `EventId` (checkpoint do maior id visto); parar a
  varredura quando a página só contém ids já conhecidos.
- Sem rate limit documentado: 1 req/s com backoff em 5xx/timeout; o serviço
  costuma instabilizar em horário de pico (504) — erros não derrubam a rodada.
- Equipamento gravado para killer e vítima; inventário só da vítima;
  participantes viram linhas de ator (sem equipamento) para conter volume.

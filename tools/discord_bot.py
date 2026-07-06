# -*- coding: utf-8 -*-
"""Bot Discord da guild (Fase 1) — gateway WebSocket, roda LOCALMENTE.

    python tools/discord_bot.py

Conecta ao Discord via discord.py 2.x (WebSocket de SAÍDA — não precisa de URL
pública, HTTPS nem verificação Ed25519) e traduz slash commands em chamadas à
API do app (FastAPI), autenticadas pelo header X-Service-Token. O bot NUNCA
resolve org pelo servidor Discord: os endpoints /api/discord/* usam sempre a
org do MEMBRO vinculado (auth_discord_links -> auth_accounts.org_id).

Env obrigatórias:
    DISCORD_BOT_TOKEN     token do bot (Developer Portal > Bot > Token)
    ALBION_SERVICE_TOKEN  token de serviço svc_... (manage_accounts.py
                          service-token create --scopes discord_link,discord_read)
Env opcionais:
    ALBION_API_URL        base da API (default http://127.0.0.1:8528)
    DISCORD_GUILD_ID      id do servidor p/ sync instantâneo dos comandos
                          (sem ele o sync é global — pode levar até 1h)

Os handlers de comando são FUNÇÕES PURAS (async): recebem os parâmetros + o
cliente da API e devolvem TEXTO — nenhum objeto do discord.py entra neles.
Isso deixa a lógica transport-agnóstica: ao hospedar, dá para portar para
Interactions HTTP (webhook) trocando só a casca do gateway.

Passo a passo completo (Developer Portal, convite, token): docs/BOT_DISCORD.md.
"""
import os
import sys
import traceback

import httpx

API_URL_DEFAULT = "http://127.0.0.1:8528"
MAX_CHARS = 1900          # margem sob o limite de 2000 do Discord
HTTP_TIMEOUT = 20.0       # /api/flip-advisor com refresh pode demorar

# cidades aceitas pelo /flip (compra ancorada onde o jogador está)
FLIP_CITIES = ["Bridgewatch", "Caerleon", "Fort Sterling", "Lymhurst",
               "Martlock", "Thetford", "Brecilien"]
DEFAULT_FLIP_CITY = "Caerleon"

# famílias de trabalhador p/ /felicidade (coleta tem troféu de TIPO; fabricação
# WARRIOR/MAGE/HUNTER/TOOLMAKER só tem o geral — o backend decide, aqui é só UI)
LABORER_FAMILIES = ["WOOD", "ORE", "STONE", "HIDE", "FIBER", "FISHERMAN",
                    "MERCENARY", "WARRIOR", "MAGE", "HUNTER", "TOOLMAKER"]

STATE_PT = {"em_dia": "Em dia", "pendente": "Pendente (aguardando auditoria)",
            "atrasado": "Atrasado", "desligado": "Desligado"}


# ------------------------------------------------------------ cliente da API
class ApiError(Exception):
    """Erro estruturado da API ({detail, code}) — vira mensagem amigável."""

    def __init__(self, status, detail, code=None):
        super().__init__(f"{status}: {detail}")
        self.status = int(status)
        self.detail = detail or ""
        self.code = code or ""


class ApiClient:
    """httpx.AsyncClient com base_url + X-Service-Token em toda chamada."""

    def __init__(self, base_url, service_token, timeout=HTTP_TIMEOUT):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout,
            headers={"X-Service-Token": service_token})

    async def get(self, path, params=None):
        return await self._request("GET", path, params=params)

    async def post(self, path, json=None):
        return await self._request("POST", path, json=json)

    async def _request(self, method, path, **kw):
        resp = await self._client.request(method, path, **kw)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            raise ApiError(resp.status_code,
                           (data.get("detail") if isinstance(data, dict)
                            else None) or resp.text[:200],
                           data.get("code") if isinstance(data, dict) else None)
        return resp.json()

    async def aclose(self):
        await self._client.aclose()


# ------------------------------------------------------------- formatação
def fmt_silver(v):
    """1234567.8 -> '1.234.568' (separador de milhar PT-BR); None/0 -> '-'."""
    if v is None:
        return "-"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v == 0:
        return "-"
    return f"{v:,.0f}".replace(",", ".")


def fmt_age(minutes):
    """Idade do dado em minutos -> '12m' / '3h' / '2d'; None -> '?'."""
    if minutes is None:
        return "?"
    m = int(minutes)
    if m < 60:
        return f"{m}m"
    if m < 48 * 60:
        return f"{m // 60}h"
    return f"{m // (24 * 60)}d"


def clip(text, limit=MAX_CHARS):
    """Corta no limite do Discord preservando linhas inteiras."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 20]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + "\n... (cortado)"


def code_block(text):
    return "```\n" + clip(text) + "\n```"


def friendly_error(exc: ApiError) -> str:
    """Traduz {detail, code} da API em mensagem PT-BR — nunca traceback."""
    msgs = {
        "service_invalid": ("Token de serviço inválido ou revogado. Regenere "
                            "com `manage_accounts.py service-token create` e "
                            "atualize ALBION_SERVICE_TOKEN."),
        "service_scope": ("O token de serviço não tem o escopo necessário "
                          "(precisa de discord_link + discord_read)."),
        "link_code_invalid": ("Código de vínculo não encontrado. Confira os 8 "
                              "caracteres com o admin (sem 0/O/1/I/L)."),
        "link_code_used": ("Este código já foi usado. Peça um novo ao admin "
                           "(`manage_accounts.py link-code <conta>`)."),
        "link_code_expired": ("Código expirado (validade de 15 minutos). Peça "
                              "um novo ao admin."),
        "discord_already_linked": ("Este Discord já está vinculado a uma conta. "
                                   "Fale com o admin para desfazer o vínculo."),
        "invalid_discord_id": "Não consegui identificar seu usuário Discord.",
        "entitlement_missing": ("A organização da sua conta não tem o plano "
                                "'operação' ativo — fale com o admin da guild."),
        "entitlement_expired": ("O plano 'operação' da sua organização expirou "
                                "— fale com o admin da guild."),
    }
    if exc.code in msgs:
        return msgs[exc.code]
    if exc.status == 404:
        return ("Você ainda não está vinculado a uma conta. Peça um código ao "
                "admin e use /vincular.")
    if exc.status == 401:
        return "A API recusou o bot (401). Verifique o ALBION_SERVICE_TOKEN."
    if exc.status >= 500:
        return "A API está com problema interno (500). Tente de novo em instantes."
    return f"A API recusou o pedido: {exc.detail or exc.status}"


# ---------------------------------------------------- handlers (funções puras)
# Cada handler recebe (api, params...) e devolve o TEXTO da resposta. Nenhum
# tipo do discord.py aqui — portável p/ Interactions HTTP sem tocar na lógica.

async def _resolve_item(api, termo):
    """Melhor item p/ o termo (mesma busca da web); None se nada casar."""
    res = await api.get("/api/search", params={"q": termo, "limit": 5})
    return res[0] if res else None


async def handle_preco(api, termo: str) -> str:
    """/preco — resolve o id pelo termo e mostra os preços q1 por cidade."""
    item = await _resolve_item(api, termo)
    if not item:
        return f"Nenhum item encontrado para '{termo}'. Tente /buscar {termo}."
    rows = await api.get("/api/prices",
                         params={"items": item["id"], "qualities": "1"})
    rows = [r for r in rows
            if (r.get("sell_price_min") or 0) > 0
            or (r.get("buy_price_max") or 0) > 0]
    head = (f"{item['pt']} (T{item['tier']}.{item['ench']}) — {item['id']}\n"
            f"qualidade 1 | preço 0 = sem dado (omitido)\n")
    if not rows:
        return code_block(head + "\nSem cotação no momento — tente mais tarde.")
    lines = [f"{'Cidade':<14} {'Venda(min)':>12} {'Compra(max)':>12} Idade"]
    for r in sorted(rows, key=lambda r: -(r.get("sell_price_min") or 0)):
        lines.append(
            f"{r['city']:<14} {fmt_silver(r.get('sell_price_min')):>12} "
            f"{fmt_silver(r.get('buy_price_max')):>12} "
            f"{fmt_age(r.get('sell_age_min'))}")
    return code_block(head + "\n".join(lines))


async def handle_buscar(api, termo: str) -> str:
    """/buscar — lista candidatos (variantes de encanto colapsadas)."""
    res = await api.get("/api/search",
                        params={"q": termo, "limit": 10, "group": "true"})
    if not res:
        return f"Nenhum item encontrado para '{termo}'."
    lines = [f"Itens para '{termo}':", ""]
    for it in res:
        ench = ""
        if it.get("enchants") and len(it["enchants"]) > 1:
            ench = " (encantos " + ",".join(map(str, it["enchants"])) + ")"
        lines.append(f"T{it['tier']} {it['pt']:<34} {it['id']}{ench}")
    lines += ["", "Use /preco com o nome ou o id exato."]
    return code_block("\n".join(lines))


async def handle_flip(api, orcamento: float, cidade: str) -> str:
    """/flip — consultor de flips por orçamento (GET /api/flip-advisor)."""
    res = await api.get("/api/flip-advisor",
                        params={"budget": orcamento, "city": cidade})
    shopping = res.get("shopping_list") or []
    summ = res.get("summary") or {}
    head = (f"Flips p/ {fmt_silver(orcamento)} de prata em {cidade}\n"
            f"lucro estimado: {fmt_silver(summ.get('lucro_total_estimado'))} "
            f"(ROI {summ.get('roi_total_pct', 0)}%) | "
            f"usa {fmt_silver(summ.get('orcamento_usado'))} do orçamento\n")
    if not shopping:
        return code_block(
            head + "\nNada lucrativo agora com esse orçamento nessa cidade.\n"
                   "O cache pode estar frio — tente outra cidade ou mais tarde.")
    lines = []
    for i, ln in enumerate(shopping[:10], 1):
        lines.append(
            f"{i:>2}. {ln['name_pt']} (T{ln['tier']}.{ln['ench']} q{ln['quality']})\n"
            f"    compra {ln['buy_city']} {fmt_silver(ln['buy_price'])} -> "
            f"vende {ln['sell_city']} {fmt_silver(ln['sell_price'])}\n"
            f"    {ln['units']}x | lucro {fmt_silver(ln['lucro_liquido'])} "
            f"(ROI {ln['roi_pct']}%) | conf. {ln.get('confidence_label', '?')}")
    if len(shopping) > 10:
        lines.append(f"... +{len(shopping) - 10} itens (veja a aba Flips na web)")
    return code_block(head + "\n" + "\n".join(lines))


async def handle_ilha(api) -> str:
    """/ilha — top 5 diários de trabalhador por margem (vazio -> cheio)."""
    res = await api.get("/api/island", params={"view": "laborers", "limit": 5})
    rows = res.get("rows") or []
    if not rows:
        return code_block(
            "Sem dados de trabalhadores ainda — o cache precisa de preços dos "
            "diários (a coleta automática preenche com o tempo).")
    lines = ["Top 5 diários de trabalhador (margem vazio -> cheio):", ""]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. {r['family']} T{r['tier']} — margem {fmt_silver(r['margin'])}\n"
            f"   comprar vazio: {r['buy_city']} {fmt_silver(r['empty_price'])} | "
            f"vender cheio: {r['sell_city']} {fmt_silver(r['full_net'])}")
        if r.get("resource"):
            lines.append(
                f"   entrega {r.get('resource_pt') or r['resource']} — melhor "
                f"venda: {r['resource_sell_city']} "
                f"{fmt_silver(r['resource_net'])}")
    return code_block("\n".join(lines))


async def handle_felicidade(api, tier_trabalhador: int, predio: int = 8,
                            trabalhadores: int = 1, familia=None) -> str:
    """/felicidade — painel do jogo (camas/mesas/troféus) + rendimento por diário.

    Assume o CENÁRIO IDEAL da configuração: mobília suficiente no tier do prédio
    e todos os troféus gerais colocáveis — mostra o TETO alcançável (o painel
    real do jogador só fica abaixo se faltar peça)."""
    gen = ",".join(str(t) for t in range(2, min(8, predio) + 1))
    params = {"laborer_tier": tier_trabalhador, "building_tier": predio,
              "n_laborers": trabalhadores, "general_tiers": gen}
    if familia:
        params["family"] = familia
    res = await api.get("/api/laborer-happiness", params=params)
    num = lambda x: f"{x:g}".replace(".", ",")
    pan = (f"Camas {num(res['camas']['score'])}/{res['camas']['cap']} | "
           f"Mesas {num(res['mesas']['score'])}/{res['mesas']['cap']} | "
           f"Trofeus {num(res['trofeus']['score'])}/100 | "
           f"Total {num(res['total'])}/{res['total_max']}")
    yields = " | ".join(f"T{y['diario_tier']} {num(y['yield_pct'])}%"
                        for y in res.get("yield_por_diario") or [])
    lines = [
        f"Trabalhador T{res['laborer_tier']} em prédio T{res['building_tier']}"
        + (f" ({res['family']})" if res.get("family") else "")
        + f" — teto da configuração ({trabalhadores} trab.)",
        pan,
        f"rendimento por diário: {yields}",
    ]
    extras = (res.get("hints") or [])[:2] + (res.get("trancados") or [])[:2]
    if extras:
        lines += [""] + [f"- {h}" for h in extras]
    return code_block("\n".join(lines))


async def handle_vincular(api, codigo: str, discord_user_id: int) -> str:
    """/vincular — consome o código gerado na console e grava o vínculo."""
    res = await api.post("/api/discord/link",
                         json={"code": codigo.strip(),
                               "discord_user_id": discord_user_id})
    return (f"Vínculo criado! Você agora é **{res['username']}** "
            f"(papel: {res['role']}). Use /minhas-metas e /meu-status.")


async def handle_minhas_metas(api, discord_user_id: int) -> str:
    """/minhas-metas — metas semanais SÓ do membro vinculado (org dele)."""
    res = await api.get("/api/discord/my-assignments",
                        params={"discord_user_id": discord_user_id})
    week = res.get("week_now", "?")
    rows = res.get("assignments") or []
    current = [a for a in rows if a.get("week_start") == week]
    older = len(rows) - len(current)
    lines = [f"Semana atual (início {week}):"]
    if current:
        for a in current:
            note = f" — {a['note']}" if a.get("note") else ""
            sector = f" [{a['sector']}]" if a.get("sector") else ""
            lines.append(f"- {a['qty_target']}x {a['item_id']}{sector}{note}")
    else:
        lines.append("- (nenhuma meta atribuída a você nesta semana)")
    if older > 0:
        lines.append(f"(+{older} meta(s) de semanas anteriores — veja na web)")
    lines += ["", "Entregou? Reporte na aba Guild da plataforma — o relógio "
                  "pausa enquanto o auditor confere."]
    return code_block("\n".join(lines))


async def handle_meu_status(api, discord_user_id: int) -> str:
    """/meu-status — relógio de tributo SÓ do membro vinculado."""
    res = await api.get("/api/discord/my-status",
                        params={"discord_user_id": discord_user_id})
    st = res.get("status")
    if not st:
        return ("Seu relógio de tributo ainda não foi iniciado — ele começa na "
                "primeira meta atribuída a você.")
    state = STATE_PT.get(st.get("state"), st.get("state", "?"))
    lines = [f"Estado: {state}",
             f"Relógio: {st.get('clock_days', '?')} dia(s)"]
    if st.get("days_left") is not None:
        lines.append(f"Prazo: {st['days_left']} dia(s) até o desligamento (14d)")
    if st.get("paused"):
        lines.append("Relógio PAUSADO — há reporte seu aguardando auditoria.")
    if st.get("tools_revoked"):
        lines.append("Ferramentas removidas — regularize com o auditor da guild.")
    return code_block("\n".join(lines))


# ------------------------------------------------------ casca discord.py 2.x
def build_bot(api: ApiClient):
    """Monta o Client + CommandTree ligando cada slash command ao handler puro."""
    import discord
    from discord import app_commands

    intents = discord.Intents.default()   # nada de privileged (members/presence)

    class GuildBot(discord.Client):
        def __init__(self):
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self):
            gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
            if gid:
                # sync por servidor: comandos aparecem na hora (dev/uso próprio)
                guild = discord.Object(id=int(gid))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()     # global: pode levar até ~1h

        async def on_ready(self):
            print(f"[bot] conectado como {self.user} "
                  f"(API {api._client.base_url})", flush=True)

    bot = GuildBot()
    tree = bot.tree

    async def _respond(interaction, coro, ephemeral=False):
        """defer -> handler -> followup; erro da API vira mensagem amigável."""
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        try:
            text = await coro
        except ApiError as exc:
            text = friendly_error(exc)
        except httpx.HTTPError:
            text = ("Não consegui falar com a API do app. Ela está rodando "
                    "(python app.py) e o ALBION_API_URL está certo?")
        except Exception:
            traceback.print_exc()          # log no console; nunca no canal
            text = "Erro inesperado ao processar o comando. Veja o log do bot."
        await interaction.followup.send(clip(text), ephemeral=ephemeral)

    # ---------------------------------------------------- comandos públicos
    @tree.command(name="preco", description="Preços atuais de um item por cidade")
    @app_commands.describe(item="Nome (PT/EN) ou id do item — ex.: bolsa t4")
    async def preco_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_preco(api, item))

    @tree.command(name="buscar", description="Busca itens pelo nome (PT/EN) ou id")
    @app_commands.describe(termo="Termo de busca — ex.: manto, t6 espada")
    async def buscar_cmd(interaction: discord.Interaction, termo: str):
        await _respond(interaction, handle_buscar(api, termo))

    @tree.command(name="flip",
                  description="Melhores compras p/ seu orçamento (flip advisor)")
    @app_commands.describe(orcamento="Prata disponível (ex.: 500000)",
                           cidade="Cidade onde você está (padrão: Caerleon)")
    @app_commands.choices(cidade=[app_commands.Choice(name=c, value=c)
                                  for c in FLIP_CITIES])
    async def flip_cmd(interaction: discord.Interaction, orcamento: float,
                       cidade: str = DEFAULT_FLIP_CITY):
        if orcamento <= 0:
            await interaction.response.send_message(
                "O orçamento precisa ser maior que zero.", ephemeral=True)
            return
        await _respond(interaction, handle_flip(api, orcamento, cidade))

    @tree.command(name="ilha",
                  description="Top 5 diários de trabalhador (margem vazio->cheio)")
    async def ilha_cmd(interaction: discord.Interaction):
        await _respond(interaction, handle_ilha(api))

    @tree.command(name="felicidade",
                  description="Painel de felicidade + rendimento por diário (teto da config)")
    @app_commands.describe(
        tier_trabalhador="Tier do TRABALHADOR (2-8)",
        predio="Tier do PRÉDIO (casa/guild hall, 2-8; tranca mobília/troféus)",
        trabalhadores="Quantos trabalhadores dividem o prédio (padrão 1)",
        familia="Família (fabricação não tem troféu de tipo)")
    @app_commands.choices(familia=[app_commands.Choice(name=f, value=f)
                                   for f in LABORER_FAMILIES])
    async def felicidade_cmd(interaction: discord.Interaction,
                             tier_trabalhador: app_commands.Range[int, 2, 8],
                             predio: app_commands.Range[int, 2, 8] = 8,
                             trabalhadores: app_commands.Range[int, 1, 99] = 1,
                             familia: str = None):
        await _respond(interaction, handle_felicidade(
            api, tier_trabalhador, predio, trabalhadores=trabalhadores,
            familia=familia))

    # ------------------------------------- comandos do membro (EFÊMEROS)
    @tree.command(name="vincular",
                  description="Vincula seu Discord à sua conta da plataforma")
    @app_commands.describe(codigo="Código de 8 caracteres gerado pelo admin")
    async def vincular_cmd(interaction: discord.Interaction, codigo: str):
        await _respond(interaction,
                       handle_vincular(api, codigo, interaction.user.id),
                       ephemeral=True)

    @tree.command(name="minhas-metas",
                  description="Suas metas semanais de tributo (só você vê)")
    async def minhas_metas_cmd(interaction: discord.Interaction):
        await _respond(interaction,
                       handle_minhas_metas(api, interaction.user.id),
                       ephemeral=True)

    @tree.command(name="meu-status",
                  description="Seu relógio de tributo (só você vê)")
    async def meu_status_cmd(interaction: discord.Interaction):
        await _respond(interaction,
                       handle_meu_status(api, interaction.user.id),
                       ephemeral=True)

    return bot


# --------------------------------------------------------------------- main
def main():
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    service_token = os.environ.get("ALBION_SERVICE_TOKEN", "").strip()
    api_url = os.environ.get("ALBION_API_URL", API_URL_DEFAULT).strip()
    missing = [name for name, val in
               (("DISCORD_BOT_TOKEN", bot_token),
                ("ALBION_SERVICE_TOKEN", service_token)) if not val]
    if missing:
        print("Faltam variaveis de ambiente: " + ", ".join(missing))
        print("  DISCORD_BOT_TOKEN     -> Developer Portal > Bot > Reset Token")
        print("  ALBION_SERVICE_TOKEN  -> python manage_accounts.py "
              "service-token create --label discord_bot "
              "--scopes discord_link,discord_read")
        print("Guia completo: docs/BOT_DISCORD.md")
        sys.exit(1)

    try:
        import discord  # noqa: F401 — valida a dependência antes de conectar
    except ImportError:
        print("O pacote discord.py nao esta instalado. Instale com:")
        print("  pip install -U discord.py")
        print("(precisa ser discord.py 2.x — o bot usa app_commands)")
        sys.exit(1)

    api = ApiClient(api_url, service_token)
    bot = build_bot(api)
    print(f"[bot] iniciando (API {api_url}) — Ctrl+C para sair", flush=True)
    try:
        bot.run(bot_token, log_handler=None)
    finally:
        # fecha o cliente HTTP fora do loop do bot (já encerrado aqui)
        import asyncio
        try:
            asyncio.run(api.aclose())
        except RuntimeError:
            pass


if __name__ == "__main__":
    main()

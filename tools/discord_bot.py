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
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

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


# ----------------------------------------------------------- builds da guild
# Dados estáticos (data/builds.json, gerado por scripts/build_builds_data.py):
# cada build já vem com os itens resolvidos em PT-BR oficial + id + ícone.
BUILD_TREES = [
    ("Fogo", "Cajados de Fogo"),
    ("Gelo", "Cajados de Gelo"),
    ("Arcos", "Arcos"),
    ("Natureza", "Cajados da Natureza"),
    ("Amaldiçoados", "Cajados Amaldiçoados (Amaldiçoados)"),
    ("Bordões", "Bordões (Quarterstaffs)"),
    ("Machados", "Machados"),
]
BUILD_CONTENTS = [
    ("Facção / ZvZ", "Faccao/ZvZ"),
    ("Arena 5v5", "Arena 5v5"),
    ("Hellgate 5v5", "Hellgate 5v5"),
    ("Abyssal 3v3", "Abyssal 3v3"),
    ("Corrompida 1v1", "Corrompida 1v1"),
]
_TREE_LABEL = {v: lbl for lbl, v in BUILD_TREES}
_CONTENT_LABEL = {v: lbl for lbl, v in BUILD_CONTENTS}
_SLOT_LABEL = [
    ("weapon", "🗡️ Arma"), ("offhand", "🛡️ Off-hand"),
    ("head", "🪖 Cabeça"), ("chest", "👕 Peito"),
    ("shoes", "👢 Pés"), ("cape", "🧥 Capa"),
    ("potion", "🧪 Poção"), ("food", "🍲 Comida"),
]
_BUILDS_CACHE = None


def load_builds():
    """Lê data/builds.json uma vez (lista de builds); [] se ausente."""
    global _BUILDS_CACHE
    if _BUILDS_CACHE is None:
        try:
            p = Path(__file__).resolve().parent.parent / "data" / "builds.json"
            _BUILDS_CACHE = json.loads(p.read_text(encoding="utf-8")).get(
                "builds", [])
        except (OSError, ValueError):
            _BUILDS_CACHE = []
    return _BUILDS_CACHE


def find_builds(tree_value, content_value=None):
    out = [b for b in load_builds() if b.get("tree") == tree_value]
    if content_value:
        out = [b for b in out if b.get("content") == content_value]
    return out


def build_title(build):
    tree = _TREE_LABEL.get(build.get("tree"), build.get("tree", ""))
    content = _CONTENT_LABEL.get(build.get("content"), build.get("content", ""))
    return f"⚔️ {build.get('buildName', '')} — {tree} · {content}"


def build_card_text(build):
    """Markdown de uma build (descrição de embed OU texto puro). Só PT-BR."""
    items = build.get("items") or {}
    lines = []
    for slot, label in _SLOT_LABEL:
        it = items.get(slot)
        if it and it.get("pt_clean"):
            lines.append(f"{label}: **{it['pt_clean']}**")
    ab = build.get("abilities") or {}
    hab = " · ".join(f"{k} {ab[key]}" for k, key in
                     (("Q", "q"), ("W", "w"), ("E", "e")) if ab.get(key))
    if ab.get("passive"):
        hab += f" · passiva {ab['passive']}"
    body = "\n".join(lines)
    if hab:
        body += f"\n\n✨ **Habilidades:** {hab}"
    if build.get("execution"):
        body += f"\n\n📋 {build['execution']}"
    if build.get("note"):
        body += f"\n\n⚠️ {build['note']}"
    return body


def item_render_url(item_id, size=128):
    """URL oficial do ícone no render da SBI — o proxy do Discord busca daqui
    (mais confiável que o nosso /icon, que na nuvem Linux não tem o fallback
    PowerShell que contorna o Cloudflare)."""
    return (f"https://render.albiononline.com/v1/item/{item_id}.png"
            f"?size={size}") if item_id else None


def weapon_icon_url(build, public_url=None):
    it = (build.get("items") or {}).get("weapon")
    return item_render_url(it.get("id")) if it and it.get("id") else None


# ------------------------------------------------------------ guia de comandos
# Fonte única do /ajuda: toda ferramenta do bot, com exemplo. Mantido aqui
# (não em texto solto) para nunca sair do sincronismo com os comandos reais.
HELP_SECTIONS = [
    ("💹 Mercado", [
        ("/preco", "preço de um item por cidade — ex.: `/preco bolsa t4`"),
        ("/comparar", "preço entre TODAS as cidades + melhor rota de flip"),
        ("/vender", "melhor cidade para vender um item"),
        ("/ouro", "cotação do ouro + tendência de 48h"),
        ("/flip", "quanto você tem → o que comprar — ex.: `/flip 500000`"),
        ("/recomendar", "as 5 melhores oportunidades de flip agora"),
        ("/buscar", "descobrir o nome/id de um item"),
    ]),
    ("🏝️ Ilha & Produção", [
        ("/ilha", "top 5 diários de trabalhador (margem vazio→cheio)"),
        ("/felicidade", "móveis/troféus ideais + rendimento — ex.: "
                        "`/felicidade tier_trabalhador: 7`"),
        ("/plano", "plano de produção p/ N trabalhadores"),
    ]),
    ("⚔️ Builds da guild", [
        ("/builds", "escolha a árvore de arma + o conteúdo → build com os "
                    "itens em PT-BR e imagem do loadout"),
    ]),
    ("🎯 Tributo da guild", [
        ("/vincular", "liga seu Discord à sua conta (peça o código a um oficial)"),
        ("/minhas-metas", "suas metas da semana (só você vê)"),
        ("/meu-status", "seu relógio de tributo (só você vê)"),
        ("/reportar", "avisa uma entrega — ex.: "
                      "`/reportar item: minério t4 quantidade: 500`"),
        ("/quadro", "placar da guild: metas × entregas"),
    ]),
    ("🛡️ Auditoria (oficiais)", [
        ("/pendentes", "fila de reportes aguardando aprovação"),
        ("/aprovar", "aprova um reporte (zera o relógio do membro)"),
        ("/rejeitar", "rejeita um reporte (relógio volta a contar)"),
    ]),
]
HELP_INTRO = ("Sou o assistente de mercado do Albion (servidor Américas). "
              "Digite **/** e escolha um comando — a resposta vem na hora.\n"
              "🔎 Nos comandos de item (**/preco, /comparar, /vender, /buscar**) "
              "não precisa saber o nome exato: comece a digitar e **clique na "
              "lista** (autocomplete).")


def help_fields():
    """Campos do embed do /ajuda (nome da seção, texto). Pura p/ testar."""
    out = []
    for title, cmds in HELP_SECTIONS:
        body = "\n".join(f"**{c}** — {desc}" for c, desc in cmds)
        out.append((title, body[:1024]))
    return out


def build_image_url(build, public_url):
    """URL pública da imagem de loadout pré-gerada (web/builds/*.png), se houver."""
    img = build.get("image")
    return (public_url.rstrip("/") + img) if img else None


# ---------------------------------------------------- handlers (funções puras)
# Cada handler recebe (api, params...) e devolve o TEXTO da resposta. Nenhum
# tipo do discord.py aqui — portável p/ Interactions HTTP sem tocar na lógica.

async def _resolve_item(api, termo):
    """Melhor item p/ o termo (mesma busca da web); None se nada casar.

    Se o termo for um id exato (ex.: veio do autocomplete), prioriza esse item.
    """
    res = await api.get("/api/search", params={"q": termo, "limit": 5})
    alvo = (termo or "").strip().lower()
    for it in res:
        if it.get("id", "").lower() == alvo:
            return it
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


def _bar(value, vmax, width=12):
    """Barra ASCII proporcional (comparação visual sem gerar imagem)."""
    if not vmax or not value or value <= 0:
        return ""
    n = int(round(width * value / vmax))
    return "█" * max(1, n)


async def handle_comparar(api, termo: str) -> str:
    """/comparar — preço do item entre TODAS as cidades (barras + rota de flip)."""
    item = await _resolve_item(api, termo)
    if not item:
        return f"Nenhum item encontrado para '{termo}'. Tente /buscar {termo}."
    rows = await api.get("/api/prices",
                         params={"items": item["id"], "qualities": "1"})
    sells = [(r["city"], r.get("sell_price_min") or 0) for r in rows
             if (r.get("sell_price_min") or 0) > 0]
    buys = [(r["city"], r.get("buy_price_max") or 0) for r in rows
            if (r.get("buy_price_max") or 0) > 0]
    head = (f"{item['pt']} (T{item['tier']}.{item['ench']}) — "
            f"comparação entre cidades (qualidade 1)\n")
    if not sells and not buys:
        return code_block(head + "\nSem cotação agora — o cache pode estar frio.")
    lines = ["VENDA mais barata (comprar aqui):"] if sells else []
    vmax = max((v for _, v in sells), default=0)
    for city, v in sorted(sells, key=lambda x: x[1]):
        lines.append(f"  {city:<13} {fmt_silver(v):>12}  {_bar(v, vmax)}")
    if buys:
        lines.append("")
        lines.append("Maior ordem de COMPRA (vender aqui):")
        bmax = max(v for _, v in buys)
        for city, v in sorted(buys, key=lambda x: -x[1]):
            lines.append(f"  {city:<13} {fmt_silver(v):>12}  {_bar(v, bmax)}")
    if sells and buys:
        buy_city, buy_p = min(sells, key=lambda x: x[1])
        sell_city, sell_p = max(buys, key=lambda x: x[1])
        if sell_p > buy_p:
            lines += ["", (f"Rota: comprar em {buy_city} ({fmt_silver(buy_p)}) "
                           f"-> vender ordem em {sell_city} "
                           f"({fmt_silver(sell_p)}) = +{fmt_silver(sell_p - buy_p)}"
                           f" bruto/un (antes de impostos)")]
    return code_block(head + "\n".join(lines))


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


def _fmt_ts(unix):
    """Timestamp unix -> 'dd/mm hh:mm' local; None -> '?'."""
    try:
        return datetime.fromtimestamp(float(unix)).strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


async def handle_reportar(api, discord_user_id: int, termo: str, qty: int,
                          nota=None) -> str:
    """/reportar — auto-reporte de entrega (relógio PAUSA até o auditor).

    Se o membro tem meta da semana p/ o item, o reporte é ligado a ela
    automaticamente; senão entra como bônus."""
    item = await _resolve_item(api, termo)
    if not item:
        return f"Nenhum item casa com '{termo}'. Tente o id exato (ex.: T4_ORE)."
    assignment_id = None
    try:
        res = await api.get("/api/discord/my-assignments",
                            params={"discord_user_id": discord_user_id})
        week = res.get("week_now")
        for a in res.get("assignments") or []:
            if a.get("week_start") == week and a.get("item_id") == item["id"]:
                assignment_id = a.get("id")
                break
    except ApiError:
        pass                                   # sem metas: reporte bônus
    body = {"discord_user_id": discord_user_id, "item_id": item["id"],
            "qty": int(qty)}
    if assignment_id:
        body["assignment_id"] = assignment_id
    if nota:
        body["note"] = str(nota)[:280]
    res = await api.post("/api/discord/report", json=body)
    kind = ("ligada à sua meta da semana" if assignment_id
            else "bônus (sem meta ligada)")
    return (f"Entrega registrada (#{res['id']}): {int(qty)}x {item['pt']} "
            f"— {kind}.\nSeu relógio está PAUSADO até o auditor conferir.")


async def handle_pendentes(api, discord_user_id: int) -> str:
    """/pendentes — fila de auditoria da org (só operador vinculado)."""
    res = await api.get("/api/discord/pending",
                        params={"discord_user_id": discord_user_id})
    rows = res.get("pending") or []
    if not rows:
        return "Fila vazia — nenhum reporte aguardando auditoria."
    lines = [f"{len(rows)} reporte(s) na fila — use /aprovar <id> ou "
             f"/rejeitar <id>:", ""]
    for r in rows[:20]:
        meta = (f" (meta #{r['assignment_id']})" if r.get("assignment_id")
                else " (bônus)")
        lines.append(f"#{r['id']:<4} conta {r['account_id']}: "
                     f"{r['qty_reported']}x {r['item_id']}{meta} — "
                     f"{_fmt_ts(r.get('reported_at'))}")
    if len(rows) > 20:
        lines.append(f"... +{len(rows) - 20} (veja a aba Guild na web)")
    return code_block("\n".join(lines))


async def handle_aprovar(api, discord_user_id: int, report_id: int,
                         nota=None) -> str:
    """/aprovar — zera o relógio do membro; alimenta a cadeia se houver meta."""
    body = {"discord_user_id": discord_user_id, "report_id": int(report_id)}
    if nota:
        body["note"] = str(nota)[:280]
    res = await api.post("/api/discord/approve", json=body)
    out = (f"Aprovado #{res['id']}: relógio da conta {res['account_id']} "
           f"ZERADO (em dia).")
    if res.get("reactivated"):
        out += "\nMembro REATIVADO — estava desligado; devolva o cargo no Discord."
    if res.get("from_chain_id"):
        out += (f"\nEstoque da cadeia #{res['from_chain_id']} alimentado "
                f"(+{res.get('qty_reported')} {res.get('item_id')}).")
    return out


async def handle_rejeitar(api, discord_user_id: int, report_id: int,
                          nota=None) -> str:
    """/rejeitar — retoma a contagem (se não restar outro reporte pendente)."""
    body = {"discord_user_id": discord_user_id, "report_id": int(report_id)}
    if nota:
        body["note"] = str(nota)[:280]
    res = await api.post("/api/discord/reject", json=body)
    state = STATE_PT.get(res.get("state"), res.get("state", "?"))
    return (f"Rejeitado #{res['id']}: conta {res['account_id']} agora está "
            f"{state}.")


def _board_text(res) -> str:
    """Quadro semanal: por membro, estado + metas item aprovado/alvo + total."""
    members = res.get("members") or []
    lines = [f"QUADRO DA SEMANA — início {res.get('week_start', '?')}", ""]
    done_all = target_all = 0
    for m in members:
        st = STATE_PT.get(m.get("state"), m.get("state") or "sem relógio")
        metas = m.get("metas") or []
        done_all += sum(int(mt.get("qty_aprovada") or 0) for mt in metas)
        target_all += sum(int(mt.get("qty_target") or 0) for mt in metas)
        parts = (", ".join(f"{mt['item_id']} "
                           f"{mt.get('qty_aprovada') or 0}/{mt['qty_target']}"
                           for mt in metas) if metas else "sem metas")
        lines.append(f"{m.get('username', '?'):<16} [{st}] {parts}")
    if target_all:
        pct = 100.0 * done_all / target_all
        lines += ["", f"Total da guild: {fmt_silver(done_all)}/"
                      f"{fmt_silver(target_all)} ({pct:.0f}%)"]
    return code_block("\n".join(lines))


async def handle_quadro(api, discord_user_id: int) -> str:
    """/quadro — metas × entregas aprovadas × relógio de toda a org."""
    res = await api.get("/api/discord/board",
                        params={"discord_user_id": discord_user_id})
    return _board_text(res)


async def handle_vender(api, termo: str) -> str:
    """/vender — melhor cidade p/ VENDER o item (maior venda q1)."""
    item = await _resolve_item(api, termo)
    if not item:
        return f"Nenhum item casa com '{termo}'. Tente /buscar {termo}."
    rows = await api.get("/api/prices",
                         params={"items": item["id"], "qualities": "1"})
    rows = [r for r in rows if (r.get("sell_price_min") or 0) > 0]
    if not rows:
        return code_block(f"{item['pt']} — sem cotação de venda no momento.")
    rows.sort(key=lambda r: -(r.get("sell_price_min") or 0))
    best = rows[0]
    lines = [f"Onde vender {item['pt']} (T{item['tier']}.{item['ench']}):", ""]
    for i, r in enumerate(rows[:6], 1):
        star = " <- MELHOR" if r is best else ""
        lines.append(f"{i}. {r['city']:<14} {fmt_silver(r['sell_price_min']):>12} "
                     f"{fmt_age(r.get('sell_age_min'))}{star}")
    lines += ["", "Preço bruto q1 (imposto 4% premium / 8% sem, anúncio 2,5% "
                  "em ordem — Mercado Negro não paga anúncio)."]
    return code_block("\n".join(lines))


async def handle_ouro(api) -> str:
    """/ouro — cotação atual + tendência simples (prata por 1 ouro)."""
    pts = await api.get("/api/gold", params={"count": 48})
    if not pts:
        return "Sem cotação de ouro no cache ainda."
    cur, old = pts[-1], pts[0]
    delta = (cur["price"] - old["price"]) / old["price"] * 100 if old["price"] else 0
    arrow = "subindo" if delta > 0.5 else ("caindo" if delta < -0.5 else "estável")
    return code_block(
        f"Ouro: {fmt_silver(cur['price'])} prata/ouro ({arrow}, "
        f"{delta:+.1f}% em 48h)\n"
        f"Sua prata em ouro: 1M = {fmt_silver(1_000_000 / cur['price'])} ouro")


async def handle_recomendar(api) -> str:
    """/recomendar — top 5 oportunidades do dia (cache local)."""
    res = await api.get("/api/recommendations", params={"limit": 5})
    rows = res.get("opportunities") or []
    if not rows:
        return ("Sem recomendações no momento — o cache está esquentando "
                "(a coleta enche as janelas com o tempo).")
    lines = ["Top oportunidades agora:", ""]
    for i, o in enumerate(rows[:5], 1):
        name = o.get("name_pt") or o.get("item_id", "?")
        lucro = o.get("net_profit") or o.get("lucro_liquido") or o.get("profit")
        roi = o.get("roi_pct") or o.get("roi")
        lines.append(f"{i}. {name} — {o.get('buy_city', '?')} -> "
                     f"{o.get('sell_city', '?')} | lucro {fmt_silver(lucro)}"
                     + (f" (ROI {roi}%)" if roi is not None else ""))
    lines += ["", "Detalhes e filtros: aba Início da plataforma."]
    return code_block("\n".join(lines))


async def handle_plano(api, familia: str = "WARRIOR", tier: int = 4,
                       trabalhadores: int = 9) -> str:
    """/plano — laborplan: cesta de produção p/ N trabalhadores de fabricação."""
    res = await api.get("/api/laborplan",
                        params={"family": familia, "tier": tier,
                                "laborers": trabalhadores})
    if not res.get("available"):
        return f"Sem plano: {res.get('reason', 'cache frio p/ essa família/tier')}"
    basket = res.get("basket") or []
    lines = [f"PLANO {res['family']} T{res['tier']} × {res['n_laborers']} "
             f"trabalhadores ({res['crafts_needed']} crafts/dia):", ""]
    for b in basket[:5]:
        lines.append(f"- {b.get('item_pt') or b['item']}: {b['crafts_dia']}/dia "
                     f"({b.get('craft_city', '?')} -> {b.get('sell_city', '?')}, "
                     f"lucro {fmt_silver(b.get('lucro_item_dia'))}/dia)")
    if len(basket) > 5:
        lines.append(f"  ... +{len(basket) - 5} itens na cesta")
    ref = ", ".join(f"{k} {fmt_silver(v)}"
                    for k, v in (res.get("refined_per_day") or {}).items())
    if ref:
        lines.append(f"Material/dia (net RRR): {ref}")
    lines += ["", f"LUCRO/DIA estimado: {fmt_silver(res.get('profit_day'))}"]
    if res.get("market_limited"):
        lines.append(f"AVISO: mercado satura em {res['market_capacity']} "
                     f"crafts/dia — alimenta ~{res['laborers_feedable']} "
                     f"trabalhadores desta família/tier.")
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
            self._start_board_task()
            import asyncio
            asyncio.create_task(self._post_guide())

        # ---------------- guia fixo (1 mensagem editada com TODOS os comandos)
        # Com ALBION_GUIDE_CHANNEL_ID (id do canal, ex.: #comece-aqui), o bot
        # posta/edita o embed do /ajuda ali no boot — instruções sempre visíveis
        # pra todos, sem depender de ninguém digitar /ajuda.
        async def _post_guide(self):
            ch_id = os.environ.get("ALBION_GUIDE_CHANNEL_ID", "").strip()
            if not ch_id:
                return
            try:
                import discord
                channel = (self.get_channel(int(ch_id))
                           or await self.fetch_channel(int(ch_id)))
                emb = discord.Embed(
                    title="📖 Guia de comandos — Mercado Albion",
                    description=HELP_INTRO, color=0xC9A24B)
                for name, body in help_fields():
                    emb.add_field(name=name, value=body, inline=False)
                emb.set_footer(text="Digite /ajuda a qualquer momento p/ rever.")
                state_path = Path("data") / "bot_state.json"
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                key = f"guide:{ch_id}"
                msg = None
                if state.get(key):
                    try:
                        msg = await channel.fetch_message(int(state[key]))
                    except Exception:
                        msg = None
                if msg:
                    await msg.edit(content=None, embed=emb)
                else:
                    msg = await channel.send(embed=emb)
                    state[key] = msg.id
                    try:
                        state_path.parent.mkdir(exist_ok=True)
                        state_path.write_text(json.dumps(state),
                                              encoding="utf-8")
                    except OSError:
                        pass
            except Exception as exc:
                print(f"[bot] guia automático falhou: {exc!r}", flush=True)

        # ---------------- quadro automático (1 mensagem editada no canal)
        # Requer ALBION_BOARD_CHANNEL_ID (id do canal, ex.: #tributo) e
        # DISCORD_BOARD_USER_ID (snowflake de um membro VINCULADO — define a
        # org cujo quadro é publicado; normalmente o do dono). Sem as envs,
        # o quadro segue disponível só via /quadro.
        def _start_board_task(self):
            if getattr(self, "_board_started", False):
                return
            ch = os.environ.get("ALBION_BOARD_CHANNEL_ID", "").strip()
            uid = os.environ.get("DISCORD_BOARD_USER_ID", "").strip()
            if not ch or not uid:
                return
            self._board_started = True
            import asyncio

            async def loop():
                while True:
                    await self.refresh_board()
                    await asyncio.sleep(6 * 3600)   # 4x/dia + após auditorias
            asyncio.create_task(loop())

        def schedule_board_refresh(self):
            """Re-edita o quadro após /aprovar e /rejeitar (não bloqueia)."""
            import asyncio
            if getattr(self, "_board_started", False):
                asyncio.create_task(self.refresh_board())

        async def refresh_board(self):
            try:
                ch_id = int(os.environ["ALBION_BOARD_CHANNEL_ID"])
                uid = int(os.environ["DISCORD_BOARD_USER_ID"])
                res = await api.get("/api/discord/board",
                                    params={"discord_user_id": uid})
                text = _board_text(res)
                channel = self.get_channel(ch_id) or await self.fetch_channel(ch_id)
                state_path = Path("data") / "bot_state.json"
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                msg_id = state.get(str(ch_id))
                msg = None
                if msg_id:
                    try:
                        msg = await channel.fetch_message(int(msg_id))
                    except Exception:
                        msg = None              # mensagem apagada: reposta
                if msg:
                    await msg.edit(content=text)
                else:
                    msg = await channel.send(text)
                    state[str(ch_id)] = msg.id
                    try:
                        state_path.parent.mkdir(exist_ok=True)
                        state_path.write_text(
                            json.dumps(state), encoding="utf-8")
                    except OSError:
                        pass
            except Exception as exc:            # nunca derruba o bot
                print(f"[bot] quadro automático falhou: {exc!r}", flush=True)

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

    async def item_ac(interaction: discord.Interaction, current: str):
        """Autocomplete de item: digite 2+ letras (PT/EN) e escolha da lista.

        O `value` é o id do item — o handler prioriza id exato, então a escolha
        do usuário resolve direto, sem depender de digitar o nome certo.
        """
        current = (current or "").strip()
        if len(current) < 2:
            return []
        try:
            res = await api.get("/api/search",
                                params={"q": current, "limit": 20,
                                        "group": "true"})
        except Exception:
            return []
        out, seen = [], set()
        for it in res:
            iid = it.get("id")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            name = f"T{it.get('tier', '?')} {it.get('pt', iid)}"[:100]
            out.append(app_commands.Choice(name=name, value=iid))
            if len(out) >= 20:
                break
        return out

    # ---------------------------------------------------- comandos públicos
    @tree.command(name="preco", description="Preços atuais de um item por cidade")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def preco_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_preco(api, item))

    @tree.command(name="comparar",
                  description="Compara o preço de um item entre TODAS as cidades")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def comparar_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_comparar(api, item))

    @tree.command(name="buscar", description="Busca itens pelo nome (PT/EN) ou id")
    @app_commands.describe(termo="Termo de busca — ex.: manto, t6 espada")
    @app_commands.autocomplete(termo=item_ac)
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

    @tree.command(name="reportar",
                  description="Reporta entrega de tributo (pausa seu relógio)")
    @app_commands.describe(item="Item entregue (nome ou id — ex.: minério t4)",
                           quantidade="Quantidade entregue",
                           nota="Observação p/ o auditor (opcional)")
    async def reportar_cmd(interaction: discord.Interaction, item: str,
                           quantidade: app_commands.Range[int, 1, 1_000_000_000],
                           nota: str = None):
        await _respond(interaction,
                       handle_reportar(api, interaction.user.id, item,
                                       quantidade, nota=nota),
                       ephemeral=True)

    # ------------------------------------ comandos do AUDITOR (EFÊMEROS)
    @tree.command(name="pendentes",
                  description="Fila de reportes aguardando auditoria (auditor)")
    async def pendentes_cmd(interaction: discord.Interaction):
        await _respond(interaction,
                       handle_pendentes(api, interaction.user.id),
                       ephemeral=True)

    @tree.command(name="aprovar",
                  description="Aprova um reporte: zera o relógio do membro (auditor)")
    @app_commands.describe(id="Número do reporte (veja /pendentes)",
                           nota="Observação da auditoria (opcional)")
    async def aprovar_cmd(interaction: discord.Interaction, id: int,
                          nota: str = None):
        await _respond(interaction,
                       handle_aprovar(api, interaction.user.id, id, nota=nota),
                       ephemeral=True)
        bot.schedule_board_refresh()

    @tree.command(name="rejeitar",
                  description="Rejeita um reporte: relógio volta a contar (auditor)")
    @app_commands.describe(id="Número do reporte (veja /pendentes)",
                           nota="Motivo da rejeição (opcional)")
    async def rejeitar_cmd(interaction: discord.Interaction, id: int,
                           nota: str = None):
        await _respond(interaction,
                       handle_rejeitar(api, interaction.user.id, id, nota=nota),
                       ephemeral=True)
        bot.schedule_board_refresh()

    # ------------------------------------------- públicos (mercado/quadro)
    @tree.command(name="quadro",
                  description="Quadro da semana: metas × entregas × relógio da guild")
    async def quadro_cmd(interaction: discord.Interaction):
        await _respond(interaction, handle_quadro(api, interaction.user.id))

    @tree.command(name="vender",
                  description="Melhor cidade para vender um item")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def vender_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_vender(api, item))

    @tree.command(name="ouro", description="Cotação do ouro + tendência 48h")
    async def ouro_cmd(interaction: discord.Interaction):
        await _respond(interaction, handle_ouro(api))

    @tree.command(name="recomendar",
                  description="Top 5 oportunidades de flip agora")
    async def recomendar_cmd(interaction: discord.Interaction):
        await _respond(interaction, handle_recomendar(api))

    @tree.command(name="plano",
                  description="Plano de produção p/ N trabalhadores (laborplan)")
    @app_commands.describe(familia="Família de fabricação",
                           tier="Tier do diário (2-8)",
                           trabalhadores="Quantos trabalhadores alimentar")
    @app_commands.choices(familia=[
        app_commands.Choice(name=f, value=f)
        for f in ("WARRIOR", "HUNTER", "MAGE", "TOOLMAKER", "MERCENARY")])
    async def plano_cmd(interaction: discord.Interaction,
                        familia: str = "WARRIOR",
                        tier: app_commands.Range[int, 2, 8] = 4,
                        trabalhadores: app_commands.Range[int, 1, 99] = 9):
        await _respond(interaction,
                       handle_plano(api, familia, tier, trabalhadores))

    # URL pública p/ os ícones (o embed é buscado pelos servidores do Discord,
    # então precisa ser acessível na internet — não o 127.0.0.1 do bot embarcado).
    public_url = os.environ.get(
        "ALBION_PUBLIC_URL", "https://mercado-albion.onrender.com").rstrip("/")

    @tree.command(name="ajuda",
                  description="Guia de TODOS os comandos e como usar cada um")
    async def ajuda_cmd(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        emb = discord.Embed(title="📖 Guia de comandos — Mercado Albion",
                            description=HELP_INTRO, color=0xC9A24B)
        for name, body in help_fields():
            emb.add_field(name=name, value=body, inline=False)
        emb.set_footer(text="Digite /ajuda a qualquer momento para rever isto.")
        await interaction.followup.send(embed=emb)

    @tree.command(
        name="builds",
        description="Builds meta da guild por arma e conteúdo (com ícones)")
    @app_commands.describe(arma="Árvore de arma",
                           conteudo="Tipo de conteúdo (opcional)")
    @app_commands.choices(
        arma=[app_commands.Choice(name=lbl, value=val)
              for lbl, val in BUILD_TREES],
        conteudo=[app_commands.Choice(name=lbl, value=val)
                  for lbl, val in BUILD_CONTENTS])
    async def builds_cmd(interaction: discord.Interaction, arma: str,
                         conteudo: str = None):
        await interaction.response.defer(thinking=True)
        try:
            matches = find_builds(arma, conteudo)
            if not matches:
                tree_lbl = _TREE_LABEL.get(arma, arma)
                avail = sorted({_CONTENT_LABEL.get(b["content"], b["content"])
                                for b in find_builds(arma)})
                await interaction.followup.send(
                    f"Não há build de **{tree_lbl}** para esse conteúdo.\n"
                    f"Conteúdos com build nessa árvore: {', '.join(avail) or '—'}.")
                return
            embeds = []
            for b in matches[:6]:
                desc = (b.get("role") or "").strip()
                desc = (desc + "\n\n" if desc else "") + build_card_text(b)
                emb = discord.Embed(title=build_title(b), description=desc[:4000],
                                    color=0xC9A24B)
                icon = weapon_icon_url(b)
                if icon:
                    emb.set_thumbnail(url=icon)
                img = build_image_url(b, public_url)
                if img:
                    emb.set_image(url=img)
                embeds.append(emb)
            note = None
            if not conteudo and len(matches) > 6:
                note = f"(mostrando 6 de {len(matches)} — escolha um conteúdo p/ filtrar)"
            await interaction.followup.send(content=note, embeds=embeds)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "Erro ao montar a build. Veja o log do bot.")

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

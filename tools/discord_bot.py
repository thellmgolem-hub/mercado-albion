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
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

API_URL_DEFAULT = "http://127.0.0.1:8528"
MAX_CHARS = 1900          # margem sob o limite de 2000 do Discord
# Leitura longa: /recomendar e /flip varrem o mercado inteiro e, no Postgres
# free (throttled), passam dos 20s. O bot faz defer(thinking=True), e o Discord
# aceita followup por ~15min, entao esperar aqui e seguro. O connect fica curto
# (app embarcado no localhost) p/ falhar rapido se a API estiver mesmo fora.
HTTP_TIMEOUT = 90.0
HTTP_CONNECT_TIMEOUT = 5.0

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

# ---------------------------------------------------- busca LOCAL de itens
# O autocomplete NÃO pode depender da API: quando o app engasga, o picker some
# e o comando fica inusável (incidente jul/2026). O bot carrega o
# data/items_db.json (mesmo repo/máquina) e busca em memória — resposta <1ms,
# sempre dentro do prazo de ~3s do Discord, mesmo com a API fora do ar.
_ITEMS_CACHE = None
_AC_TIER = re.compile(r"^t([1-8])$")
_AC_ENCH = re.compile(r"^@([0-4])$")
_AC_TE = re.compile(r"^([1-8])\.([0-4])$")


def _norm_txt(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def load_items_local():
    global _ITEMS_CACHE
    if _ITEMS_CACHE is None:
        try:
            raw = json.loads(
                (Path(__file__).resolve().parent.parent / "data"
                 / "items_db.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        for it in raw:
            it["_n"] = _norm_txt(
                f"{it.get('pt', '')} {it.get('en', '')} {it.get('id', '')}")
        _ITEMS_CACHE = raw
    return _ITEMS_CACHE


def local_item_search(q, limit=20):
    """Busca de item p/ o autocomplete (PURA, testável): todos os tokens devem
    aparecer no nome PT/EN/id; 't4', '@2' e '4.2' filtram tier/encanto; sem
    '@N' no termo, as variantes encantadas colapsam no item-base."""
    items = load_items_local()
    if not items:
        return []
    tokens, tier, ench = [], None, None
    for t in _norm_txt(q).split():
        m = _AC_TE.match(t)
        if m:
            tier, ench = int(m.group(1)), int(m.group(2))
            continue
        m = _AC_TIER.match(t)
        if m:
            tier = int(m.group(1))
            continue
        m = _AC_ENCH.match(t)
        if m:
            ench = int(m.group(1))
            continue
        tokens.append(t)
    out = []
    for it in items:
        if tier is not None and it.get("tier") != tier:
            continue
        if ench is not None and (it.get("ench") or 0) != ench:
            continue
        if ench is None and (it.get("ench") or 0):
            continue                    # sem @N: só a variante base
        if all(t in it["_n"] for t in tokens):
            out.append(it)
    # nome mais curto primeiro (match mais "cheio"), depois tier crescente
    out.sort(key=lambda x: (len(x.get("pt") or ""), x.get("tier") or 9))
    return out[:limit]

# Mural de Conquistas: intervalo do laço que busca abates novos e posta
KILLFEED_INTERVAL = 150   # segundos (~2,5 min; abaixo dos 1000 ev/20min de pico)
# KillArea da API -> rótulo PT-BR do local do abate (Location vem nulo)
KILLAREA_PT = {
    "OPEN_WORLD": "mundo aberto", "MIST": "Brumas", "MISTS": "Brumas",
    "CORRUPTED_DUNGEON": "masmorra corrompida", "HELLGATE": "portal infernal",
    "ARENA": "arena", "EXPEDITION": "expedição", "GVG": "GvG",
    "AVALON_DUNGEON": "masmorra avaloniana", "ABBEY": "abadia",
}


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
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=HTTP_CONNECT_TIMEOUT),
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


def fmt_data_hora(age_min):
    """Idade do dado (min) -> HORÁRIO (BR, UTC-3) de quando o preço foi visto,
    ex.: '14h32'. Se for de +1 dia, o horário sozinho enganaria, então mostra
    'há Nd'. Substitui o rótulo opaco de 'confiabilidade' por algo que o jogador
    entende: a hora de que vem o dado de cada ponta (compra/venda)."""
    if age_min is None:
        return "?"
    m = int(age_min)
    if m >= 24 * 60:
        return f"há {m // (24 * 60)}d"
    t = datetime.now(timezone.utc) - timedelta(minutes=m, hours=3)  # UTC-3
    return t.strftime("%Hh%M")


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
    # Deixa folga p/ as cercas ``` (8 chars): assim o total já sai <= MAX_CHARS
    # e o clip() de _respond (que reaplica o limite) não corta o fecho ```.
    return "```\n" + clip(text, MAX_CHARS - 8) + "\n```"


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
    # cajados
    ("Fogo", "Cajados de Fogo"),
    ("Gelo", "Cajados de Gelo"),
    ("Natureza", "Cajados da Natureza"),
    ("Sagrado", "Cajados Sagrados"),
    ("Amaldiçoados", "Cajados Amaldiçoados (Amaldiçoados)"),
    # à distância
    ("Arcos", "Arcos"),
    ("Bestas", "Bestas"),
    # corpo a corpo
    ("Espadas", "Espadas"),
    ("Machados", "Machados"),
    ("Maças", "Maças"),
    ("Lanças", "Lanças"),
    ("Bordões", "Bordões (Quarterstaffs)"),
    ("Luvas de Combate", "Luvas de Combate"),
]
BUILD_CONTENTS = [
    ("Facção / ZvZ", "Faccao/ZvZ"),
    ("Open World / Brumas", "Openworld/Brumas"),
    ("Corrompida 1v1", "Corrompida 1v1"),
    ("Hellgate 5v5", "Hellgate 5v5"),
    ("Abyssal 3v3", "Abyssal 3v3"),
    ("Arena 5v5", "Arena 5v5"),
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
    # nome da build em PT (buildName_pt, do nome PT da arma); cai pro EN só se
    # faltar — guilda 100% BR não vê título em inglês.
    nome = build.get("buildName_pt") or build.get("buildName", "")
    tree = _TREE_LABEL.get(build.get("tree"), build.get("tree", ""))
    content = _CONTENT_LABEL.get(build.get("content"), build.get("content", ""))
    return f"⚔️ {nome} — {tree} · {content}"


def build_card_text(build):
    """Markdown de uma build (descrição de embed OU texto puro). Só PT-BR."""
    items = build.get("items") or {}
    lines = []
    for slot, label in _SLOT_LABEL:
        it = items.get(slot)
        if it and it.get("pt_clean"):
            lines.append(f"{label}: **{it['pt_clean']}**")
    ab = build.get("abilities") or {}
    # nome PT oficial da skill (build_spells_pt.py), caindo pro EN se faltar
    _sk = lambda key: ab.get(key + "_pt") or ab.get(key)
    hab = " · ".join(f"{k} {_sk(key)}" for k, key in
                     (("Q", "q"), ("W", "w"), ("E", "e")) if ab.get(key))
    if ab.get("passive"):
        hab += f" · passiva {_sk('passive')}"
    body = "\n".join(lines)
    if hab:
        body += f"\n\n✨ **Habilidades:** {hab}"
    ex = build.get("execution_pt") or build.get("execution")   # PT (build_notes_pt)
    if ex:
        body += f"\n\n📋 {ex}"
    nt = build.get("note_pt") or build.get("note")
    if nt:
        body += f"\n\n⚠️ {nt}"
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
        ("/preco", "mostra o preço de um item em cada cidade. Ex.: `/preco bolsa t4`"),
        ("/comparar", "põe as cidades lado a lado e já aponta a rota de flip"),
        ("/historico", "gráfico da evolução do preço, comparando as cidades de maior giro"),
        ("/vender", "diz onde vale mais a pena vender"),
        ("/ouro", "cotação do ouro e como ela andou nas últimas 48h"),
        ("/flip", "você diz quanta prata tem e ele monta a lista de compras. "
                  "Ex.: `/flip 500000`"),
        ("/recomendar", "as 5 melhores oportunidades do momento"),
        ("/buscar", "acha o nome ou o id de um item"),
    ]),
    ("🏝️ Ilha & Produção", [
        ("/ilha", "os 5 melhores diários de trabalhador, pela margem de vazio pra cheio"),
        ("/felicidade", "os móveis e troféus ideais e o rendimento que dá. "
                        "Ex.: `/felicidade tier_trabalhador: 7`"),
        ("/plano", "monta um plano de produção pra vários trabalhadores"),
        ("/craftar", "diz se vale craftar um item, com a margem e a lista de compras. "
                     "Ex.: `/craftar cajado de fogo t4`"),
        ("/refinar", "ranking do refino: onde compensa refinar o bruto ou vender bruto. "
                     "Ex.: `/refinar tier: 4`"),
        ("/foco", "ranking de prata por ponto de foco (craft e refino)"),
        ("/produzir", "pra fazer N de um item, a lista de compras de recurso bruto. "
                      "Ex.: `/produzir espada larga t5 quantidade: 20`"),
        ("/guild", "decisões da guild: fabricar-vs-comprar, o que mais se perde, regear"),
    ]),
    ("📈 Análise avançada", [
        ("/escanear", "varre uma categoria inteira e lista os melhores flips. "
                      "Ex.: `/escanear categoria: weapons tier: 6`"),
        ("/lab", "raio-X de um item: caro ou barato vs a tendência, z, momentum"),
        ("/demanda", "killboard: consumíveis mais gastos e qualidade destruída"),
        ("/logistica", "prêmio do Mercado Negro, se vale subir qualidade, reposição"),
        ("/micro", "market-making: os melhores spreads de compra/venda agora"),
        ("/risco", "volatilidade, drawdown e VaR por item, ou correlação"),
        ("/sinais", "previsão de um item: reversão à média, quebra de regime"),
        ("/origem", "de quais mobs um item cai (por fama)"),
    ]),
    ("⚔️ Builds da guild", [
        ("/builds", "é só escolher a arma e o conteúdo. Vem a build pronta, "
                    "com os itens em português e a imagem do loadout"),
    ]),
    ("🎯 Tributo da guild", [
        ("/vincular", "liga seu Discord à sua conta. Peça o código a um oficial"),
        ("/personagem", "registra seu nick do Albion (pras metas e seu perfil)"),
        ("/minhas-metas", "suas metas da semana. Só você vê"),
        ("/meu-status", "seu relógio de tributo. Só você vê"),
        ("/reportar", "avisa que você entregou algo. "
                      "Ex.: `/reportar item: minério t4 quantidade: 500`"),
        ("/quadro", "o placar da guild: metas contra o que já entrou"),
    ]),
    ("🛡️ Auditoria (oficiais)", [
        ("/pendentes", "a fila de reportes esperando aprovação"),
        ("/aprovar", "aprova um reporte e zera o relógio do membro"),
        ("/rejeitar", "rejeita um reporte, e o relógio volta a correr"),
    ]),
    ("⚔️ Mural de Conquistas (oficiais)", [
        ("/mural-canal", "usa o canal atual pra celebrar os abates da guild"),
        ("/mural-guilda", "escolhe a guilda a vigiar (nome exato do jogo)"),
        ("/mural-status", "mostra a configuração do mural"),
        ("/mural-remover", "para de vigiar uma guilda"),
    ]),
    ("🏛️ Organização (admin)", [
        ("/camadas", "mostra as camadas: Visitante → Aprendiz → Oficial → Mestre"),
        ("/organizar-servidor", "cria os cargos e canais das camadas de uma vez"),
    ]),
]
# Título do embed do guia — usado no /ajuda, no guia automático E como marcador
# p/ reencontrar a própria mensagem no canal (idempotência sem depender de disco).
GUIDE_TITLE = "📖 Guia de comandos — Mercado Albion"
BOARD_MARKER = "QUADRO DA SEMANA"     # cabeçalho do quadro (idem: reencontro no canal)
HELP_INTRO = ("Sou o bot de mercado do Albion, servidor Américas. "
              "Digita **/** e escolhe um comando; a resposta chega ali mesmo.\n"
              "🔎 Nos comandos de item (**/preco, /comparar, /vender, /buscar**) "
              "você não precisa saber o nome certo. Começa a digitar e "
              "**clica na sugestão** que aparecer.")


def help_fields():
    """Campos do embed do /ajuda (nome da seção, texto). Pura p/ testar."""
    out = []
    for title, cmds in HELP_SECTIONS:
        body = "\n".join(f"**{c}** {desc}" for c, desc in cmds)
        out.append((title, body[:1024]))
    return out


def build_image_url(build, public_url):
    """URL pública da imagem de loadout pré-gerada (web/builds/*.png), se houver."""
    img = build.get("image")
    return (public_url.rstrip("/") + img) if img else None


def build_image_file(build, idx):
    """Lê o PNG de loadout pré-gerado (web/builds/*.png) do disco e devolve um
    discord.File pra ANEXAR ao embed. Anexar (em vez de linkar via
    ALBION_PUBLIC_URL) faz o Discord SEMPRE renderizar a imagem: não depende do
    Render estar acordado nem do fetch do Cloudflare. O bot roda embarcado, então
    tem o arquivo no disco. Devolve None se não houver imagem gerada."""
    import discord   # import é lazy no módulo (só dentro de função); replica aqui
    img = build.get("image")            # ex.: /builds/<slug>.png
    if not img:
        return None
    path = Path(__file__).resolve().parent.parent / "web" / img.lstrip("/")
    if not path.is_file():
        return None
    return discord.File(str(path), filename=f"build{idx}.png")


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


# ---------------------------------------------------- histórico com gráfico
QUICKCHART_BASE = "https://quickchart.io/chart"


def _sample(seq, n):
    """<= n pontos com amostragem uniforme, preservando o primeiro e o último."""
    if len(seq) <= n:
        return list(seq)
    step = (len(seq) - 1) / (n - 1)
    return [seq[round(i * step)] for i in range(n)]


def quickchart_url(title, labeled_series, max_len=1990):
    """URL GET do QuickChart p/ até 2 séries [(cidade, [(rótulo, preço), ...])].

    O gráfico é renderizado pelo quickchart.io (serviço público, sem chave) e o
    Discord busca a imagem pelo proxy dele — na URL vão SÓ preços públicos de
    mercado. Degrada (menos séries/pontos) até caber no limite de URL de embed;
    None se nem a menor versão couber ou não houver série."""
    for n_series, n_points in ((2, 20), (1, 20), (1, 12)):
        use = [s for s in labeled_series[:n_series] if s[1]]
        if not use:
            return None
        base_labels = [lb for lb, _ in _sample(use[0][1], n_points)]
        datasets = []
        for (city, pts), color in zip(use, ("#c9a24b", "#4bc0c0")):
            by_label = dict(_sample(pts, n_points))
            datasets.append({"label": city,
                             "data": [by_label.get(lb) for lb in base_labels],
                             "borderColor": color, "fill": False,
                             "spanGaps": True})
        cfg = {"type": "line",
               "data": {"labels": base_labels, "datasets": datasets},
               "options": {"title": {"display": True, "text": title[:60]}}}
        url = (QUICKCHART_BASE + "?w=520&h=300&c="
               + quote(json.dumps(cfg, separators=(",", ":"),
                                  ensure_ascii=False)))
        if len(url) <= max_len:
            return url
    return None


async def handle_historico(api, termo: str, dias: int = 30) -> dict:
    """/historico — preço médio diário + gráfico comparando as 2 cidades com
    mais volume.

    Exceção ao contrato de texto dos handlers: devolve dict
    {text, chart_url, title} — a casca monta o embed com a imagem."""
    item = await _resolve_item(api, termo)
    if not item:
        return {"text": (f"Nenhum item encontrado para '{termo}'. "
                         f"Tente /buscar {termo}."),
                "chart_url": None, "title": None}
    series = await api.get("/api/history",
                           params={"items": item["id"], "days": dias,
                                   "time_scale": 24, "quality": 1})
    series = [s for s in series if s.get("data")]
    if not series:
        return {"text": (f"{item['pt']} — sem histórico no cache para {dias} "
                         "dias. A coleta enche as janelas com o tempo."),
                "chart_url": None, "title": None}
    # cidades com mais volume primeiro (o gráfico compara as 2 maiores)
    series.sort(key=lambda s: -sum(p.get("item_count") or 0 for p in s["data"]))
    lines, labeled = [], []
    for s in series[:2]:
        pts = [(p["ts"][8:10] + "/" + p["ts"][5:7], p["avg_price"])
               for p in s["data"] if (p.get("avg_price") or 0) > 0]
        if not pts:
            continue
        labeled.append((s.get("city", "?"), pts))
        prices = [v for _, v in pts]
        var = ((prices[-1] - prices[0]) / prices[0] * 100) if prices[0] else 0.0
        vol = sum(p.get("item_count") or 0 for p in s["data"]) / max(1, dias)
        lines.append(f"**{s.get('city', '?')}** · média "
                     f"{fmt_silver(sum(prices) / len(prices))} · mín "
                     f"{fmt_silver(min(prices))} · máx {fmt_silver(max(prices))}"
                     f" · {var:+.1f}% no período · ~{fmt_silver(vol)} un/dia")
    if not labeled:
        return {"text": f"{item['pt']} — histórico sem preços válidos no período.",
                "chart_url": None, "title": None}
    title = f"{item['pt']} (T{item['tier']}.{item['ench']}) — {dias} dias"
    return {"text": "\n".join(lines), "title": title,
            "chart_url": quickchart_url(title, labeled)}


async def handle_flip(api, orcamento: float, cidade: str) -> str:
    """/flip — consultor de flips por orçamento (GET /api/flip-advisor)."""
    res = await api.get("/api/flip-advisor",
                        params={"budget": orcamento, "city": cidade})
    shopping = res.get("shopping_list") or []
    summ = res.get("summary") or {}
    head = (f"Flips p/ {fmt_silver(orcamento)} de prata em {cidade}\n"
            f"lucro estimado: {fmt_silver(summ.get('lucro_total_estimado'))} "
            f"(retorno {summ.get('roi_total_pct', 0)}%) | "
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
            f"(retorno {ln['roi_pct']}%)\n"
            f"    preço visto: compra {fmt_data_hora(ln.get('buy_age_min'))} · "
            f"venda {fmt_data_hora(ln.get('sell_age_min'))}")
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
    # /api/gold vem ORDER BY ts DESC (mais NOVO primeiro): pts[0]=atual, pts[-1]=~48h atrás.
    cur, old = pts[0], pts[-1]
    if not cur.get("price"):
        return "Sem cotação de ouro válida no cache ainda."
    delta = (cur["price"] - old["price"]) / old["price"] * 100 if old.get("price") else 0
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
                     + (f" (retorno {roi}%)" if roi is not None else ""))
        lines.append(f"    preço visto: compra {fmt_data_hora(o.get('buy_age_min'))}"
                     f" · venda {fmt_data_hora(o.get('sell_age_min'))}")
    lines += ["", "preço visto = hora (BR) em que cada ponta foi coletada.",
              "Detalhes e filtros: aba Início da plataforma."]
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


async def handle_craftar(api, item: str) -> str:
    """/craftar — vale a pena craftar este item? Melhor cenário de craft com a
    lista de compras dos insumos (GET /api/craft; busca preço ao vivo)."""
    res = await api.get("/api/craft", params={"item": item})
    meta = res.get("item") or {}
    nome = meta.get("name_pt") or meta.get("id", item)
    rows = res.get("rows") or []
    if not rows:
        return code_block(
            f"{nome}: sem cotação suficiente pra calcular o craft agora "
            "(mercado/cache frio). Tente outro item ou mais tarde.")
    b = rows[0]                       # melhor cenário (ordenado por margem)
    margem = b.get("margin") or 0
    veredito = ("VALE A PENA craftar" if margem > 0
                else "NÃO compensa (compre pronto)")
    bonus = " · cidade-bônus" if b.get("is_bonus_city") else ""
    lines = [
        f"{nome} (T{meta.get('tier', '?')}.{meta.get('ench', 0)}) — {veredito}",
        "",
        f"Craftar em {b.get('craft_city', '?')} (RRR {b.get('rrr_pct')}%{bonus})",
        f"  materiais {fmt_silver(b.get('materials'))}  ->  vende em "
        f"{b.get('sell_city', '?')} por {fmt_silver(b.get('revenue'))}",
        f"  LUCRO por craft: {fmt_silver(margem)}"
        + (f" ({b['margin_pct']}%)" if b.get("margin_pct") is not None else ""),
    ]
    src = b.get("sourcing") or []
    if src:
        lines += ["", "Lista de compras (insumos):"]
        for s in src:
            lines.append(
                f"  {s.get('count', '?')}x {s.get('name_pt') or s.get('id')} "
                f"em {s.get('buy_city', '?')} "
                f"({fmt_silver(s.get('unit_price'))}/un)")
    return code_block("\n".join(lines))


async def handle_refinar(api, tier: int = None) -> str:
    """/refinar — ranking de refino: pra cada material refinado, se vale mais
    refinar o bruto ou vender o bruto, e onde (GET /api/prod?view=refine)."""
    res = await api.get("/api/prod", params={"view": "refine"})
    rows = res.get("rows") or []
    if tier:
        rows = [r for r in rows
                if str(r.get("item_id", "")).startswith(f"T{tier}_")]
    if not rows:
        alvo = f" T{tier}" if tier else ""
        return code_block(
            f"Sem dados de refino{alvo} agora (a coleta preenche com o tempo).")
    titulo = f"Refino{(' T' + str(tier)) if tier else ''}: refinar ou vender o bruto?"
    lines = [titulo, ""]
    for r in rows[:12]:
        nome = r.get("name_pt") or r.get("item_id")
        v = "REFINAR" if (r.get("margin") or 0) > 0 else "vender bruto"
        lines.append(
            f"{v:>12} · {nome}: margem {fmt_silver(r.get('margin'))} "
            f"({r.get('premium_pct')}%) em {r.get('city', '?')} "
            f"(RRR {r.get('rrr_pct')}%)")
    lines += ["", "margem = refinar o bruto e vender vs. comprar o refinado pronto."]
    return code_block("\n".join(lines))


# ---------------------------------------------- análises avançadas (plataforma)
# Cada handler espelha um endpoint /api do app. Campos conferidos 1:1 com o
# contrato do backend (workflow map-endpoint-contracts). Muitas análises de
# killboard saem VAZIAS até o intel-sweep acumular — tratamos com mensagem amiga.

def _nome(r):
    return r.get("name_pt") or r.get("item_id") or "?"


_GUILD_VIEW = {"fabricar": "makeorbuy", "destruicao": "watch", "regear": "kit"}


async def handle_guild(api, tipo: str = "fabricar") -> str:
    """/guild — decisões da guild pelo killboard: fabricar-vs-comprar, o que a
    guild mais perde, e custo de regear (GET /api/guild)."""
    view = _GUILD_VIEW.get(tipo, "makeorbuy")
    res = await api.get("/api/guild", params={"view": view})
    if view == "kit":
        basket = res.get("basket") or []
        series = res.get("series") or []
        if not basket:
            return code_block("Sem dados de killboard ainda p/ o custo de regear "
                              "(enche conforme o intel-sweep coleta).")
        lines = ["Custo de regear — cesta ponderada pelo que a guild mais perde:", ""]
        for b in basket[:8]:
            lines.append(f"- {_nome(b)} (peso {round((b.get('weight') or 0) * 100)}%)")
        if series:
            last = series[-1]
            lines += ["", f"Índice de custo (base 100): {round(last.get('index') or 0)}"
                          f" · cesta hoje {fmt_silver(last.get('cost'))}"]
        return code_block("\n".join(lines))
    rows = res.get("rows") or []
    if not rows:
        return code_block("Sem dados de killboard ainda (o intel-sweep precisa "
                          "acumular umas horas). Tente mais tarde.")
    if view == "watch":
        lines = ["O que a guild mais perde em PvP (vale produzir/estocar):", ""]
        for r in rows[:10]:
            preco = f", ~{fmt_silver(r.get('price'))} cada" if r.get("price") else ""
            lines.append(f"- {_nome(r)}: {r.get('destroyed')} destruídos{preco}")
        return code_block("\n".join(lines))
    lines = ["Fabricar ou comprar? (itens que a guild consome)", ""]
    for r in rows[:10]:
        v = (r.get("verdict") or "?").upper()
        eco = (f" (economia {r.get('save_pct')}%)"
               if r.get("save_pct") is not None else "")
        lines.append(f"{v:>7} · {_nome(r)}: fazer em {r.get('internal_city', '?')} por "
                     f"{fmt_silver(r.get('internal_cost'))} vs mercado "
                     f"{fmt_silver(r.get('market_price'))}{eco}")
    return code_block("\n".join(lines))


async def handle_foco(api) -> str:
    """/foco — ranking de prata por ponto de FOCO (craft e refino)
    (GET /api/prod?view=focus)."""
    res = await api.get("/api/prod", params={"view": "focus"})
    rows = res.get("rows") or []
    if not rows:
        return code_block("Sem dados de foco agora (cache frio). Tente mais tarde.")
    lines = ["Prata por ponto de FOCO (melhor cidade por receita):", ""]
    for r in rows[:12]:
        tipo = r.get("tipo") or ("refino" if r.get("is_refining") else "craft")
        lines.append(f"- {_nome(r)} [{tipo}]: {r.get('silver_per_focus')}/foco em "
                     f"{r.get('city', '?')} (+{fmt_silver(r.get('focus_gain'))} vs "
                     "sem foco)")
    lines += ["", "número = lucro extra por ponto de foco gasto."]
    return code_block("\n".join(lines))


_DEMANDA_VIEW = {"consumo": "burn", "qualidade": "quality"}


async def handle_demanda(api, tipo: str = "consumo") -> str:
    """/demanda — killboard: consumíveis mais queimados/dia e qualidade
    destruída (GET /api/demand)."""
    view = _DEMANDA_VIEW.get(tipo, "burn")
    res = await api.get("/api/demand", params={"view": view})
    rows = res.get("rows") or []
    if not rows:
        return code_block("Sem dados de killboard ainda (o intel-sweep precisa "
                          "acumular). Tente mais tarde.")
    if view == "quality":
        lines = ["Qualidade destruída (vale fabricar Q4+?):", ""]
        for r in rows[:10]:
            lines.append(f"- {_nome(r)}: {r.get('destroyed')} destr., Q4+ "
                         f"{r.get('share_q4plus_pct')}%, prêmio qualidade "
                         f"{r.get('ev_quality_premium')}x (dominante Q{r.get('dominant_q')})")
        return code_block("\n".join(lines))
    lines = ["Consumíveis mais queimados por dia (poção/comida):", ""]
    for r in rows[:10]:
        flag = " ⚠ pouca oferta" if r.get("undersupplied") else ""
        cov = f", cobertura {r.get('coverage')}" if r.get("coverage") is not None else ""
        sd = (f", ~{fmt_silver(r.get('silver_per_day'))}/dia"
              if r.get("silver_per_day") else "")
        lines.append(f"- {_nome(r)}: {r.get('per_day')}/dia{cov}{sd}{flag}")
    return code_block("\n".join(lines))


SCAN_CATS = ["weapons", "armors", "head", "shoes", "offhands", "capes", "bags",
             "mounts", "consumables", "gathering", "crafting", "artefacts"]


async def handle_escanear(api, categoria: str, tier_min: int = None) -> str:
    """/escanear — varre uma categoria e lista os melhores flips (GET /api/scan)."""
    params = {"cat": categoria, "limit": 12}
    if tier_min:
        params["tier_min"] = tier_min
    res = await api.get("/api/scan", params=params)
    opps = res.get("opportunities") or []
    alvo = f" T{tier_min}+" if tier_min else ""
    if not opps:
        return code_block(f"Nada lucrativo em '{categoria}'{alvo} agora "
                          "(preços frios ou sem margem).")
    lines = [f"Melhores flips em {categoria}{alvo}:", ""]
    for o in opps[:10]:
        t = o.get("tier")
        tag = f" T{t}.{o.get('ench') or 0}" if t else ""
        lines.append(f"- {_nome(o)}{tag}: {o.get('buy_city', '?')} -> "
                     f"{o.get('sell_city', '?')} lucro {fmt_silver(o.get('profit'))} "
                     f"(retorno {o.get('roi_pct')}%)")
        lines.append(f"    preço visto: compra {fmt_data_hora(o.get('buy_age_min'))}"
                     f" · venda {fmt_data_hora(o.get('sell_age_min'))}")
    lines += ["", "preço visto = hora (BR) em que cada ponta foi coletada."]
    return code_block("\n".join(lines))


_LOGI_VIEW = {"mercadonegro": "bm", "qualidade": "ladder", "reposicao": "restock"}


async def handle_logistica(api, tipo: str = "mercadonegro") -> str:
    """/logistica — prêmio do Mercado Negro, ganho de subir qualidade, e mapa de
    reposição do killboard (GET /api/logi)."""
    view = _LOGI_VIEW.get(tipo, "bm")
    res = await api.get("/api/logi", params={"view": view})
    rows = res.get("rows") or []
    if not rows:
        msg = ("Sem dados de reposição ainda (depende do killboard)."
               if view == "restock" else "Sem dados agora (cache frio).")
        return code_block(msg)
    if view == "ladder":
        lines = ["Vale subir a qualidade? (prêmio do melhor degrau)", ""]
        for r in rows[:10]:
            lines.append(f"- {_nome(r)} em {r.get('city', '?')}: {r.get('best_step')} "
                         f"+{fmt_silver(r.get('best_premium_abs'))} "
                         f"({r.get('best_premium_pct')}%)")
        return code_block("\n".join(lines))
    if view == "restock":
        lines = ["Reposição: o que a guild perde e onde comprar barato p/ revender:", ""]
        for r in rows[:10]:
            lines.append(f"- {_nome(r)}: {r.get('buy_city', '?')} -> "
                         f"{r.get('sell_city', '?')} lucro {fmt_silver(r.get('profit'))} "
                         f"(demanda {r.get('demand_units')})")
        return code_block("\n".join(lines))
    lines = ["Prêmio do Mercado Negro (vender gear lá vs cidade real):", ""]
    for r in rows[:10]:
        qs = f" q{r.get('quality')}" if r.get("quality") else ""
        lines.append(f"- {_nome(r)}{qs}: BM {fmt_silver(r.get('bm_net'))} vs "
                     f"{r.get('best_city', '?')} {fmt_silver(r.get('best_city_net'))} "
                     f"(+{r.get('premium_pct')}%)")
    return code_block("\n".join(lines))


async def handle_lab(api, item: str) -> str:
    """/lab — estatística de UM item numa cidade: caro/barato vs tendência, z,
    momentum, e onde comprar mais barato (GET /api/item-analysis)."""
    res = await api.get("/api/item-analysis", params={"item": item})
    meta = res.get("item") or {}
    nome = meta.get("pt") or meta.get("id", item)
    series = [s for s in (res.get("series") or []) if (s.get("points") or 0) > 0]
    if not series:
        return code_block(f"{nome}: sem histórico coletado o bastante p/ análise. "
                          "Colete mais e tente de novo.")
    s = max(series, key=lambda r: (r.get("avg_daily_volume") or 0,
                                   r.get("points") or 0))
    lines = [f"{nome} — {s.get('city', '?')} (q{s.get('quality')}, "
             f"{s.get('points')} pts)", ""]
    stance = (s.get("interpretation") or {}).get("stance")
    if stance:
        lines.append(f"Leitura: {stance}")
    if s.get("vwap") is not None:
        lines.append(f"VWAP {fmt_silver(s.get('vwap'))} · último "
                     f"{fmt_silver(s.get('latest_price'))}")
    bits = []
    if s.get("robust_z") is not None:
        bits.append(f"z {s['robust_z']:+.1f}")
    if s.get("price_percentile") is not None:
        bits.append(f"percentil {round(s['price_percentile'])}%")
    if s.get("momentum_pct") is not None:
        bits.append(f"momentum {s['momentum_pct']:+.1f}%")
    if bits:
        lines.append(" · ".join(bits))
    comp = res.get("comparison") or {}
    if comp.get("cheapest_city"):
        lines += ["", f"Mais barato: {comp.get('cheapest_city')} "
                      f"({fmt_silver(comp.get('cheapest_vwap'))}) · spread "
                      f"{comp.get('vwap_spread_pct')}%"]
    return code_block("\n".join(lines))


def _mob_name(mob):
    """'T5_MOB_DEMON_VETERAN_BOSS' -> 'Demon Veteran Boss' (aproxima; o dump não
    traz o nome do mob em PT)."""
    s = re.sub(r"^T\d+_MOB_?", "", str(mob or ""))
    return s.replace("_", " ").title() or str(mob or "?")


async def handle_origem(api, item: str) -> str:
    """/origem — de quais mobs o item cai, por fama (GET /api/origin)."""
    res = await api.get("/api/origin", params={"item": item})
    meta = res.get("item") or {}
    nome = meta.get("name_pt") or meta.get("id", item)
    sources = res.get("sources") or []
    if not sources:
        return code_block(f"{nome}: não vem de mob (é craftado/refinado/coletado, "
                          "ou sem dado de drop).")
    lines = [f"De onde vem {nome} (mobs que dropam, por fama):", ""]
    for s in sources[:10]:
        cat = f" · {s.get('cat')}" if s.get("cat") else ""
        lines.append(f"- {_mob_name(s.get('mob'))} (T{s.get('tier')}, "
                     f"fama {fmt_silver(s.get('fame'))}{cat})")
    return code_block("\n".join(lines))


async def handle_micro(api) -> str:
    """/micro — market-making: melhores spreads de compra/venda por ordem
    (GET /api/micro?view=spread)."""
    res = await api.get("/api/micro", params={"view": "spread"})
    rows = res.get("rows") or []
    if not rows:
        return code_block("Sem oportunidades de spread agora (livro fino ou "
                          "cache frio).")
    lines = ["Market-making: melhores spreads (comprar e vender na ordem):", ""]
    for r in rows[:10]:
        qs = f" q{r.get('quality')}" if r.get("quality") else ""
        lines.append(f"- {_nome(r)}{qs} em {r.get('city', '?')}: "
                     f"{fmt_silver(r.get('net_per_unit'))}/un ({r.get('net_pct')}%), "
                     f"~{fmt_silver(r.get('potential_day'))}/dia")
    lines += ["", "spread = diferença ordem de compra vs venda, já com taxas."]
    return code_block("\n".join(lines))


_RISCO_VIEW = {"perfil": "profile", "correlacao": "corr"}


async def handle_risco(api, tipo: str = "perfil") -> str:
    """/risco — volatilidade/drawdown/VaR por item, ou correlação entre itens
    (GET /api/risk)."""
    view = _RISCO_VIEW.get(tipo, "profile")
    res = await api.get("/api/risk", params={"view": view})
    rows = res.get("rows") or []
    if not rows:
        return code_block("Sem dados de risco agora (precisa de histórico; "
                          "cache frio).")
    if view == "corr":
        lines = ["Itens que andam juntos / se protegem (correlação):", ""]
        for r in rows[:10]:
            a = r.get("a_pt") or r.get("a")
            b = r.get("b_pt") or r.get("b")
            lines.append(f"- {a} × {b}: {r.get('corr')} ({r.get('tipo')})")
        return code_block("\n".join(lines))
    lines = ["Risco de preço por item (volatilidade / drawdown / VaR):", ""]
    for r in rows[:10]:
        ci = r.get("vol_ci")
        ci_s = f" (IC {ci})" if ci and ci != "—" else ""
        lines.append(f"- {_nome(r)}: {r.get('risk_label')} · vol "
                     f"{r.get('vol_shrunk_pct')}%{ci_s} · drawdown "
                     f"{r.get('max_drawdown_pct')}% · VaR {r.get('var_1d_pct')}%")
    return code_block("\n".join(lines))


async def handle_sinais(api, item: str) -> str:
    """/sinais — previsão de UM item: risco, reversão à média, quebra de regime e
    previsibilidade (GET /api/item_signals)."""
    it = await _resolve_item(api, item)
    if not it:
        return code_block(f"Não achei o item '{item}'. Tente /buscar {item}.")
    res = await api.get("/api/item_signals", params={"item": it["id"]})
    nome = it.get("pt") or it["id"]
    if not res.get("available"):
        return code_block(f"{nome}: sem histórico coletado p/ sinais. Colete o "
                          "item e tente de novo.")
    lines = [f"Sinais de {nome} — {res.get('city', '?')} "
             f"({res.get('points')} pts)", ""]
    risk = res.get("risk") or {}
    if risk.get("risk_label"):
        lines.append(f"Risco: {risk.get('risk_label')} "
                     f"(vol {risk.get('vol_annual_pct')}%)")
    rev = res.get("reversion") or {}
    if rev.get("direction"):
        disparou = "SIM" if rev.get("signal") else "não"
        lines.append(f"Reversão: {rev.get('direction')} — alvo "
                     f"{fmt_silver(rev.get('target'))}, gap {rev.get('gap_pct')}% "
                     f"(disparou: {disparou})")
    reg = res.get("regime") or {}
    if reg.get("significant"):
        lines.append(f"Regime: quebra recente ({reg.get('kind')})")
    pred = res.get("predictability") or {}
    if pred.get("label"):
        lines.append(f"Previsibilidade: {pred.get('label')} "
                     f"({round((pred.get('predictability') or 0) * 100)}%)")
    if len(lines) <= 2:
        lines.append("Série ainda curta p/ sinais firmes — colete mais.")
    return code_block("\n".join(lines))


async def handle_produzir(api, item: str, qty: int = 1) -> str:
    """/produzir — cadeia de produção em texto: pra fazer N do item, a lista de
    compras de recurso BRUTO + custo + lucro se vender (GET /api/prodchain/plan)."""
    res = await api.get("/api/prodchain/plan", params={"item": item, "qty": qty})
    meta = res.get("item") or {}
    nome = meta.get("name_pt") or meta.get("id", item)
    t = meta.get("tier")
    tag = f" T{t}.{meta.get('enchant', 0)}" if t else ""
    if not res.get("available"):
        return code_block(f"{nome}: sem plano de produção agora (item já é bruto, "
                          "sem receita, ou cache frio).")
    shop = res.get("shopping") or []
    if not shop:
        return code_block(f"{nome}: nada a comprar (já é bruto ou sem receita).")
    lines = [f"Produzir {qty}× {nome}{tag} — lista de compras (recurso bruto):", ""]
    for s in shop[:15]:
        c = f" = {fmt_silver(s.get('cost'))}" if s.get("cost") else " (sem preço)"
        lines.append(f"  {s.get('qty')}x {s.get('name_pt') or s.get('id')} "
                     f"em {s.get('city', '?')}{c}")
    if len(shop) > 15:
        lines.append(f"  ... +{len(shop) - 15} materiais")
    lines += ["", f"Custo dos materiais: {fmt_silver(res.get('buy_cost'))}"]
    if res.get("focus_points"):
        lines.append(f"Foco necessário: {fmt_silver(res.get('focus_points'))} pontos")
    if res.get("revenue"):
        roi = (f" (retorno {res.get('roi_pct')}%)"
               if res.get("roi_pct") is not None else "")
        lines.append(f"Se vender tudo: receita {fmt_silver(res.get('revenue'))} · "
                     f"LUCRO {fmt_silver(res.get('profit'))}{roi}")
    elif res.get("missing_sell"):
        lines.append("(produto final sem cotação de venda — lucro não estimado)")
    return code_block("\n".join(lines))


# ------------------------------------------------- Mural de Conquistas (killfeed)
def _killarea_pt(area):
    if not area:
        return None
    return KILLAREA_PT.get(str(area).upper(),
                           str(area).replace("_", " ").lower())


def killfeed_embed_data(kill):
    """(título, descrição, url_do_ícone, cor) de um abate — PURO e testável.
    Celebra a vitória; nunca menciona morte de membro (o feed só traz kills)."""
    killer = kill.get("killer") or "Um membro"
    victim = kill.get("victim") or "um inimigo"
    title = f"⚔️ {killer} abateu {victim}!"
    lines = []
    vg = kill.get("victim_guild")
    va = kill.get("victim_alliance")
    alvo = f"🎯 Vítima: **{victim}**"
    if vg:
        alvo += f" [{vg}]"
    if va:
        alvo += f" ({va})"
    lines.append(alvo)
    weapon = kill.get("killer_weapon_pt")
    if weapon:
        lines.append(f"🗡️ Arma: {weapon}")
    fame = kill.get("fame")
    if fame:
        lines.append(f"🏆 Fama do abate: {fmt_silver(fame)}")
    area = _killarea_pt(kill.get("kill_area"))
    if area:
        lines.append(f"📍 Local: {area}")
    mates = kill.get("guildmates") or []
    if mates:
        lines.append(f"🤝 Com: {', '.join(mates[:6])}"
                     + (" e mais…" if len(mates) > 6 else ""))
    ip, vip = kill.get("killer_ip"), kill.get("victim_ip")
    if ip or vip:
        lines.append(f"⚙️ IP {round(ip or 0)} vs {round(vip or 0)}")
    return title, "\n".join(lines), kill.get("killer_weapon_icon"), 0xF1C40F


async def handle_mural_canal(api, discord_user_id, channel_id):
    try:
        r = await api.post("/api/killfeed/config", json={
            "discord_user_id": discord_user_id, "action": "set_channel",
            "channel_id": str(channel_id)})
    except ApiError as e:
        return friendly_error(e)
    guilds = r.get("guilds") or []
    if guilds:
        return (f"✅ **Mural de Conquistas** ligado NESTE canal. Vou comemorar "
                f"aqui cada abate de: {', '.join(guilds)}.")
    return ("✅ **Mural de Conquistas** ligado NESTE canal.\n\n⚠️ Falta dizer "
            "qual guilda vigiar: use `/mural-guilda nome:<sua guilda>` com a "
            "grafia EXATA do nome no jogo.")


async def handle_mural_guilda(api, discord_user_id, nome):
    try:
        r = await api.post("/api/killfeed/config", json={
            "discord_user_id": discord_user_id, "action": "add_guild",
            "guild_name": nome})
    except ApiError as e:
        return friendly_error(e)
    guilds = r.get("guilds") or []
    tip = ("" if r.get("channel_id") else
           "\n\n⚠️ Falta o canal: rode `/mural-canal` no canal onde quer os posts.")
    return (f"✅ Vigiando **{', '.join(guilds)}**. Agora o Mural comemora quando "
            f"um membro dessa guilda abater outro jogador." + tip)


async def handle_mural_remover(api, discord_user_id, nome):
    try:
        r = await api.post("/api/killfeed/config", json={
            "discord_user_id": discord_user_id, "action": "remove_guild",
            "guild_name": nome})
    except ApiError as e:
        return friendly_error(e)
    guilds = r.get("guilds") or []
    return (f"✅ Removi **{nome}**. Guildas vigiadas: "
            f"{', '.join(guilds) if guilds else '— (nenhuma)'}.")


async def handle_mural_status(api, discord_user_id):
    try:
        r = await api.get("/api/killfeed/config",
                          params={"discord_user_id": discord_user_id})
    except ApiError as e:
        return friendly_error(e)
    ch = r.get("channel_id")
    guilds = r.get("guilds") or []
    lines = [
        "📜 **Mural de Conquistas**",
        f"• Canal: {('<#' + str(ch) + '>') if ch else '— (use /mural-canal)'}",
        f"• Estado: {'ligado ✅' if r.get('active') else 'desligado ⏸️'}",
        f"• Guildas vigiadas: {', '.join(guilds) if guilds else '— (use /mural-guilda)'}",
    ]
    if r.get("min_fame"):
        lines.append(f"• Fama mínima p/ postar: {fmt_silver(r['min_fame'])}")
    if ch and guilds:
        lines.append("\nTudo pronto — os abates novos vão aparecer aqui em "
                     "até ~3 min. 🎉")
    return "\n".join(lines)


# --------------------------------------------- vínculo conta -> personagem
async def handle_personagem(api, discord_user_id, nick=None):
    """Sem nick: mostra o personagem registrado. Com nick: registra/atualiza."""
    if not nick or not nick.strip():
        try:
            r = await api.get("/api/discord/character",
                              params={"discord_user_id": discord_user_id})
        except ApiError as e:
            return friendly_error(e)
        if r.get("char_name"):
            extra = ("" if r.get("char_id")
                     else " (ainda não achei no killboard — normal se for novo)")
            return (f"🎮 Seu personagem registrado: **{r['char_name']}**.{extra}\n"
                    "Pra trocar: `/personagem nick:<seu nick>`.")
        return ("Você ainda não registrou seu personagem. Use "
                "`/personagem nick:<seu nick no Albion>`.")
    try:
        r = await api.post("/api/discord/character", json={
            "discord_user_id": discord_user_id, "char_name": nick.strip()})
    except ApiError as e:
        return friendly_error(e)
    if r.get("resolved"):
        g = r.get("guild")
        return (f"✅ Personagem **{r['char_name']}** registrado"
                + (f" (guilda {g})" if g else "")
                + ". Vai servir pras metas e pro seu perfil de fama.")
    cands = r.get("candidates") or []
    hint = f" Parecidos: {', '.join(cands)}." if cands else ""
    return (f"✅ Anotei **{r['char_name']}**, mas ainda não achei esse nick no "
            f"killboard (normal se você jogou pouco ou a grafia difere).{hint}")


# ------------------------------------------- camadas do servidor (organização)
# Três ANÉIS de acesso concêntricos. O de fora é o Visitante (entrou no Discord
# mas ainda NÃO é do projeto); Aprendiz é o 1º degrau DENTRO da guild. A linha
# de progressão é a dos mesteres: Aprendiz -> Oficial -> Mestre.
# A permissão é sempre configurada na CATEGORIA (os canais herdam) — ajustar
# canal a canal é o que gera buraco de acesso.
SERVER_ROLES = [
    # (nome, cor, para que serve)
    ("Mestre", 0xC9A24B, "liderança da guild"),
    ("Oficial", 0x3B82F6, "responsável por uma área (mester)"),
    ("Aprendiz", 0x22C55E, "1º nível DENTRO do projeto"),
    ("Visitante", 0x9CA3AF, "entrou no Discord, ainda não é do projeto"),
]
RING_INTERNO = ("Aprendiz", "Oficial", "Mestre")   # quem vê a área da guild
RING_STAFF = ("Oficial", "Mestre")                 # quem vê o comando

SERVER_PLAN = [
    {"cat": "📢 ENTRADA", "ring": "publico", "channels": [
        {"name": "regras", "type": "text", "readonly": True},
        {"name": "comece-aqui", "type": "text", "readonly": True},
        {"name": "recrutamento", "type": "text"},
        {"name": "avisos", "type": "text", "readonly": True},
        {"name": "Lobby", "type": "voice"},
    ]},
    {"cat": "🛡️ GUILDA", "ring": "interno", "channels": [
        {"name": "geral", "type": "text"},
        {"name": "conquistas", "type": "text"},
        {"name": "mercado-e-flips", "type": "text"},
        {"name": "tributo-e-metas", "type": "text"},
        {"name": "producao-e-logistica", "type": "text"},
    ]},
    {"cat": "⚙️ COMANDO", "ring": "staff", "channels": [
        {"name": "comando", "type": "text"},
        {"name": "recrutamento-interno", "type": "text"},
        {"name": "logs-do-bot", "type": "text"},
    ]},
]

_RING_PT = {"publico": "todos (inclui Visitante)",
            "interno": "Aprendiz, Oficial e Mestre",
            "staff": "só Oficial e Mestre"}


def server_plan_summary():
    """Texto do que as camadas criam — puro, testável, usado no /organizar."""
    out = ["**Cargos**"]
    for name, _cor, para in SERVER_ROLES:
        out.append(f"• @{name} — {para}")
    out.append("")
    out.append("**Categorias** (permissão na categoria; os canais herdam)")
    for b in SERVER_PLAN:
        out.append(f"• {b['cat']} — vê: {_RING_PT[b['ring']]}")
        out.append("   " + ", ".join("#" + c["name"] for c in b["channels"]))
    return "\n".join(out)


async def apply_server_plan(guild, dsc):
    """Cria cargos/categorias/canais que FALTAM e ajusta a permissão da
    categoria. NUNCA apaga nem move o que já existe. `dsc` é o módulo discord
    (injetável). Devolve o relatório do que fez."""
    report = []
    roles = {r.name: r for r in guild.roles}
    for name, cor, _para in SERVER_ROLES:
        if name in roles:
            report.append(f"@{name}: já existia")
            continue
        roles[name] = await guild.create_role(
            name=name, colour=dsc.Colour(cor), hoist=True,
            reason="camadas da guild")
        report.append(f"@{name}: criado")
    everyone = guild.default_role

    def overwrites(ring):
        ow = {}
        if ring == "publico":
            return ow                     # todos veem: herda o padrão
        ow[everyone] = dsc.PermissionOverwrite(view_channel=False)
        for rn in (RING_INTERNO if ring == "interno" else RING_STAFF):
            if roles.get(rn):
                ow[roles[rn]] = dsc.PermissionOverwrite(view_channel=True)
        return ow

    cats = {c.name: c for c in guild.categories}
    existing = {c.name.lower() for c in guild.channels}
    for b in SERVER_PLAN:
        cat = cats.get(b["cat"])
        if cat is None:
            cat = await guild.create_category(
                b["cat"], overwrites=overwrites(b["ring"]),
                reason="camadas da guild")
            report.append(f"{b['cat']}: categoria criada")
        else:
            await cat.edit(overwrites=overwrites(b["ring"]))
            report.append(f"{b['cat']}: permissões ajustadas")
        for ch in b["channels"]:
            if ch["name"].lower() in existing:
                report.append(f"   #{ch['name']}: já existia (não mexi)")
                continue
            if ch["type"] == "voice":
                await guild.create_voice_channel(
                    ch["name"], category=cat, reason="camadas da guild")
            else:
                new = await guild.create_text_channel(
                    ch["name"], category=cat, reason="camadas da guild")
                if ch.get("readonly"):   # só a staff escreve nos murais fixos
                    await new.set_permissions(everyone, send_messages=False)
            report.append(f"   #{ch['name']}: criado")
    return report


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
            # Um DISCORD_GUILD_ID malformado (ValueError no int) NÃO pode derrubar
            # o boot do bot: cai no sync global. Idem p/ erro transitório de sync.
            gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
            try:
                if gid:
                    # sync por servidor: comandos aparecem na hora (dev/uso próprio)
                    guild = discord.Object(id=int(gid))
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                    # tira os comandos GLOBAIS antigos. Sem isto, um comando que
                    # já tinha sido registrado global (antes de existir o
                    # DISCORD_GUILD_ID) aparece DUPLICADO no menu: uma vez como
                    # global, outra como do servidor. Limpar o global deixa só a
                    # cópia do servidor (que já foi sincronizada acima).
                    self.tree.clear_commands(guild=None)
                    await self.tree.sync()
                else:
                    await self.tree.sync()     # global: pode levar até ~1h
            except Exception as exc:
                print(f"[bot] sync de comandos falhou ({exc!r}); tentando global",
                      flush=True)
                try:
                    await self.tree.sync()
                except Exception:
                    pass

        async def on_ready(self):
            print(f"[bot] conectado como {self.user} "
                  f"(API {api._client.base_url})", flush=True)
            self._start_board_task()
            self._start_killfeed_task()
            import asyncio
            asyncio.create_task(self._post_guide())

        # ------------------------------------------- estado idempotente
        # O id da mensagem (guia/quadro) é gravado em data/bot_state.json APENAS
        # como cache rápido. No Render o FS é EFÊMERO (some a cada redeploy/cold
        # start), então NUNCA confiamos só nele: se o id sumir, varremos o canal
        # pela própria mensagem (por marcador) e editamos — sem duplicar.
        @staticmethod
        def _load_state():
            try:
                return json.loads(
                    (Path("data") / "bot_state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

        @staticmethod
        def _save_state_id(state, key, msg_id):
            state[key] = msg_id
            try:
                p = Path("data") / "bot_state.json"
                p.parent.mkdir(exist_ok=True)
                p.write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                pass

        async def _msg_from_state(self, channel, state, key):
            mid = state.get(key)
            if not mid:
                return None
            try:
                return await channel.fetch_message(int(mid))
            except Exception:
                return None

        async def _find_own_message(self, channel, match):
            """Última mensagem DESTE bot no canal que casa `match` — reencontro
            robusto no FS efêmero (não posta duplicata após redeploy)."""
            try:
                async for m in channel.history(limit=50):
                    if m.author.id == self.user.id and match(m):
                        return m
            except Exception:
                pass
            return None

        # ---------------- guia fixo (1 mensagem editada com TODOS os comandos)
        # Com ALBION_GUIDE_CHANNEL_ID (id do canal, ex.: #comece-aqui), o bot
        # posta/edita o embed do /ajuda ali no boot — instruções sempre visíveis
        # pra todos, sem depender de ninguém digitar /ajuda.
        async def _post_guide(self):
            if getattr(self, "_guide_posted", False):
                return                          # guarda de reentrância (on_ready 2x)
            ch_id = os.environ.get("ALBION_GUIDE_CHANNEL_ID", "").strip()
            if not ch_id:
                return
            self._guide_posted = True
            try:
                import discord
                channel = (self.get_channel(int(ch_id))
                           or await self.fetch_channel(int(ch_id)))
                emb = discord.Embed(title=GUIDE_TITLE, description=HELP_INTRO,
                                    color=0xC9A24B)
                for name, body in help_fields():
                    emb.add_field(name=name, value=body, inline=False)
                emb.set_footer(text="Digite /ajuda a qualquer momento p/ rever.")
                state, key = self._load_state(), f"guide:{ch_id}"
                msg = await self._msg_from_state(channel, state, key)
                if msg is None:                 # id perdeu no redeploy: acha no canal
                    msg = await self._find_own_message(
                        channel,
                        lambda m: bool(m.embeds)
                        and (m.embeds[0].title or "") == GUIDE_TITLE)
                if msg:
                    await msg.edit(content=None, embed=emb)
                else:
                    msg = await channel.send(embed=emb)
                    try:
                        await msg.pin()
                    except Exception:
                        pass                    # sem permissão de fixar: tudo bem
                self._save_state_id(state, key, msg.id)
            except Exception as exc:
                self._guide_posted = False      # deixa tentar de novo no próximo boot
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

        # ---------------- Mural de Conquistas (laço que posta abates novos)
        # Roda sempre; o servidor auto-limita (poll não bate no gameinfo sem
        # guilda vigiada). Canal e guilda vêm dos comandos /mural-* (por org).
        def _start_killfeed_task(self):
            if getattr(self, "_killfeed_started", False):
                return
            self._killfeed_started = True
            import asyncio

            async def loop():
                await asyncio.sleep(25)     # deixa o boot assentar
                while True:
                    try:
                        await self._killfeed_tick()
                    except Exception as exc:
                        print(f"[bot] mural tick falhou: {exc!r}", flush=True)
                    await asyncio.sleep(KILLFEED_INTERVAL)
            asyncio.create_task(loop())

        async def _killfeed_tick(self):
            import discord
            res = await api.get("/api/killfeed/poll")
            for g in res.get("groups") or []:
                ch_id, kills = g.get("channel_id"), g.get("kills") or []
                if not ch_id or not kills:
                    continue
                # Isolamento por org: canal inacessível / sem permissão de enviar
                # / rate-limit numa org NUNCA pode abortar as outras. Um erro no
                # envio encerra ESTA org (o checkpoint já avançou no servidor —
                # perda aceitável) e segue para a próxima.
                try:
                    channel = (self.get_channel(int(ch_id))
                               or await self.fetch_channel(int(ch_id)))
                    batch = []
                    for k in kills:
                        title, desc, thumb, color = killfeed_embed_data(k)
                        emb = discord.Embed(title=title, description=desc,
                                            color=color)
                        if thumb:
                            emb.set_thumbnail(url=thumb)
                        ts = k.get("timestamp")
                        if ts:
                            emb.set_footer(
                                text=f"{ts[:19].replace('T', ' ')} UTC")
                        batch.append(emb)
                        if len(batch) == 10:   # limite do Discord por mensagem
                            await channel.send(
                                embeds=batch,
                                allowed_mentions=discord.AllowedMentions.none())
                            batch = []
                    if batch:
                        await channel.send(
                            embeds=batch,
                            allowed_mentions=discord.AllowedMentions.none())
                except Exception as exc:
                    print(f"[bot] mural: falha na org {g.get('org_id')} / canal "
                          f"{ch_id} ({exc!r}); sigo p/ a próxima", flush=True)
                    continue

        async def refresh_board(self):
            try:
                ch_id = int(os.environ["ALBION_BOARD_CHANNEL_ID"])
                uid = int(os.environ["DISCORD_BOARD_USER_ID"])
                res = await api.get("/api/discord/board",
                                    params={"discord_user_id": uid})
                text = _board_text(res)
                channel = self.get_channel(ch_id) or await self.fetch_channel(ch_id)
                state, key = self._load_state(), str(ch_id)
                msg = await self._msg_from_state(channel, state, key)
                if msg is None:                 # id perdeu no redeploy: acha no canal
                    msg = await self._find_own_message(
                        channel, lambda m: BOARD_MARKER in (m.content or ""))
                if msg:
                    await msg.edit(content=text)
                else:
                    msg = await channel.send(text)
                self._save_state_id(state, key, msg.id)
            except Exception as exc:            # nunca derruba o bot
                print(f"[bot] quadro automático falhou: {exc!r}", flush=True)

    bot = GuildBot()
    tree = bot.tree

    async def _respond(interaction, coro, ephemeral=True):
        """defer -> handler -> followup; erro da API vira mensagem amigável.
        ephemeral=True por PADRÃO: as consultas do bot são PRIVADAS (só quem
        chamou vê), pra ninguém ficar olhando a pesquisa do outro e não poluir
        o canal quando vários usam ao mesmo tempo."""
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
        # LOCAL primeiro (nunca falha nem estoura os 3s do Discord); API só
        # como fallback raro se o items_db.json não estiver no disco.
        hits = local_item_search(current, limit=20)
        if not hits:
            try:
                hits = await api.get("/api/search",
                                     params={"q": current, "limit": 20,
                                             "group": "true"}) or []
            except Exception:
                return []
        out, seen = [], set()
        for it in hits:
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

    @tree.command(name="historico",
                  description="Gráfico do preço: evolução + comparação entre cidades")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)",
                           dias="Janela em dias (3-90, padrão 30)")
    @app_commands.autocomplete(item=item_ac)
    async def historico_cmd(interaction: discord.Interaction, item: str,
                            dias: app_commands.Range[int, 3, 90] = 30):
        # PRIVADO (ephemeral): a efemeridade tem que casar entre o defer e TODOS
        # os followup, senão o 1º "edita" o defer e vaza público.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            res = await handle_historico(api, item, dias)
        except ApiError as exc:
            await interaction.followup.send(friendly_error(exc), ephemeral=True)
            return
        except httpx.HTTPError:
            await interaction.followup.send(
                "Não consegui falar com a API do app. Tente de novo em instantes.",
                ephemeral=True)
            return
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "Erro inesperado ao montar o histórico. Veja o log do bot.",
                ephemeral=True)
            return
        if not res.get("chart_url"):
            await interaction.followup.send(clip(res["text"]), ephemeral=True)
            return
        emb = discord.Embed(title=f"📈 {res['title']}",
                            description=res["text"][:4000], color=0xC9A24B)
        emb.set_image(url=res["chart_url"])
        emb.set_footer(text="Preço médio diário (AODP, q1) · volume é piso censurado")
        await interaction.followup.send(embed=emb, ephemeral=True)

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

    @tree.command(name="personagem",
                  description="Registra/mostra seu personagem do Albion")
    @app_commands.describe(nick="Seu nick EXATO no jogo (vazio = ver o atual)")
    async def personagem_cmd(interaction: discord.Interaction, nick: str = None):
        await _respond(interaction,
                       handle_personagem(api, interaction.user.id, nick),
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

    # ------------------------------------------- Mural de Conquistas (admin)
    @tree.command(name="mural-canal",
                  description="Usa ESTE canal como Mural de Conquistas (admin)")
    async def mural_canal_cmd(interaction: discord.Interaction):
        await _respond(interaction,
                       handle_mural_canal(api, interaction.user.id,
                                          interaction.channel_id),
                       ephemeral=True)

    @tree.command(name="mural-guilda",
                  description="Vigia uma guilda: celebra os abates dela (admin)")
    @app_commands.describe(nome="Nome EXATO da guilda no jogo")
    async def mural_guilda_cmd(interaction: discord.Interaction, nome: str):
        await _respond(interaction,
                       handle_mural_guilda(api, interaction.user.id, nome),
                       ephemeral=True)

    @tree.command(name="mural-remover",
                  description="Para de vigiar uma guilda no Mural (admin)")
    @app_commands.describe(nome="Nome da guilda a remover")
    async def mural_remover_cmd(interaction: discord.Interaction, nome: str):
        await _respond(interaction,
                       handle_mural_remover(api, interaction.user.id, nome),
                       ephemeral=True)

    @tree.command(name="mural-status",
                  description="Mostra a configuração do Mural de Conquistas (admin)")
    async def mural_status_cmd(interaction: discord.Interaction):
        await _respond(interaction,
                       handle_mural_status(api, interaction.user.id),
                       ephemeral=True)

    # ------------------------------------------- camadas do servidor (admin)
    @tree.command(name="camadas",
                  description="Mostra as camadas (Visitante/Aprendiz/Oficial/Mestre) que serão criadas")
    async def camadas_cmd(interaction: discord.Interaction):
        async def _plan():
            return ("🏛️ **Camadas da guild** — o que o `/organizar-servidor` cria:\n\n"
                    + server_plan_summary()
                    + "\n\n_Não apaga nem move nada: só cria o que falta._")
        await _respond(interaction, _plan(), ephemeral=True)

    @tree.command(name="organizar-servidor",
                  description="Cria os cargos e canais das camadas da guild (admin)")
    async def organizar_servidor_cmd(interaction: discord.Interaction):
        async def _run():
            perms = getattr(interaction.user, "guild_permissions", None)
            is_owner = bool(interaction.guild
                            and interaction.guild.owner_id == interaction.user.id)
            if not (is_owner or (perms and perms.administrator)):
                return "❌ Só o dono do servidor ou um admin pode organizar as camadas."
            try:
                report = await apply_server_plan(interaction.guild, discord)
            except discord.Forbidden:
                return ("❌ O bot não tem permissão pra criar cargos/canais.\n\n"
                        "Vá em **Configurações do Servidor → Cargos → Mercado "
                        "Albion** e ligue **Gerenciar Cargos** e **Gerenciar "
                        "Canais** (e deixe o cargo dele acima de Mestre na "
                        "lista). Depois rode `/organizar-servidor` de novo.")
            except Exception as exc:
                return f"❌ Falhou ao organizar: `{exc!r}`"
            return ("🏛️ **Camadas aplicadas**\n" + code_block("\n".join(report))
                    + "\nAgora dê `@Aprendiz` a quem já é do projeto e "
                      "`@Visitante` a quem ainda não é.")
        await _respond(interaction, _run(), ephemeral=True)

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

    @tree.command(name="craftar",
                  description="Vale a pena craftar? Margem + lista de compras")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def craftar_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_craftar(api, item))

    @tree.command(name="refinar",
                  description="Refino: compensa refinar o bruto ou vender bruto?")
    @app_commands.describe(tier="Filtra por tier (2-8; opcional)")
    async def refinar_cmd(interaction: discord.Interaction,
                          tier: app_commands.Range[int, 2, 8] = None):
        await _respond(interaction, handle_refinar(api, tier))

    @tree.command(name="guild",
                  description="Decisões da guild: fabricar-vs-comprar, o que perde, regear")
    @app_commands.describe(tipo="Que análise da guild")
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Fabricar ou comprar", value="fabricar"),
        app_commands.Choice(name="O que a guild mais perde", value="destruicao"),
        app_commands.Choice(name="Custo de regear", value="regear")])
    async def guild_cmd(interaction: discord.Interaction, tipo: str = "fabricar"):
        await _respond(interaction, handle_guild(api, tipo))

    @tree.command(name="foco",
                  description="Ranking de prata por ponto de foco (craft e refino)")
    async def foco_cmd(interaction: discord.Interaction):
        await _respond(interaction, handle_foco(api))

    @tree.command(name="produzir",
                  description="Cadeia de produção: lista de compras p/ fazer N de um item")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)",
                           quantidade="Quantas unidades produzir (padrão 1)")
    @app_commands.autocomplete(item=item_ac)
    async def produzir_cmd(interaction: discord.Interaction, item: str,
                           quantidade: app_commands.Range[int, 1, 100000] = 1):
        await _respond(interaction, handle_produzir(api, item, quantidade))

    @tree.command(name="demanda",
                  description="Killboard: consumíveis mais gastos e qualidade destruída")
    @app_commands.describe(tipo="Consumo (poção/comida) ou qualidade destruída")
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Consumíveis (poção/comida)", value="consumo"),
        app_commands.Choice(name="Qualidade destruída", value="qualidade")])
    async def demanda_cmd(interaction: discord.Interaction, tipo: str = "consumo"):
        await _respond(interaction, handle_demanda(api, tipo))

    @tree.command(name="escanear",
                  description="Varre uma categoria e lista os melhores flips")
    @app_commands.describe(categoria="Categoria a varrer",
                           tier="Tier mínimo (opcional)")
    @app_commands.choices(categoria=[
        app_commands.Choice(name=c, value=c) for c in SCAN_CATS])
    async def escanear_cmd(interaction: discord.Interaction, categoria: str,
                           tier: app_commands.Range[int, 1, 8] = None):
        await _respond(interaction, handle_escanear(api, categoria, tier))

    @tree.command(name="logistica",
                  description="Prêmio do Mercado Negro, subir qualidade, reposição")
    @app_commands.describe(tipo="Que análise de logística")
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Prêmio do Mercado Negro", value="mercadonegro"),
        app_commands.Choice(name="Vale subir a qualidade?", value="qualidade"),
        app_commands.Choice(name="Mapa de reposição (killboard)", value="reposicao")])
    async def logistica_cmd(interaction: discord.Interaction,
                            tipo: str = "mercadonegro"):
        await _respond(interaction, handle_logistica(api, tipo))

    @tree.command(name="lab",
                  description="Estatística de um item: caro/barato vs tendência, z, momentum")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def lab_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_lab(api, item))

    @tree.command(name="origem",
                  description="De quais mobs um item cai (por fama)")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def origem_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_origem(api, item))

    @tree.command(name="micro",
                  description="Market-making: melhores spreads de compra/venda")
    async def micro_cmd(interaction: discord.Interaction):
        await _respond(interaction, handle_micro(api))

    @tree.command(name="risco",
                  description="Volatilidade/drawdown/VaR por item, ou correlação")
    @app_commands.describe(tipo="Perfil de risco ou correlação entre itens")
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Perfil de risco por item", value="perfil"),
        app_commands.Choice(name="Correlação entre itens", value="correlacao")])
    async def risco_cmd(interaction: discord.Interaction, tipo: str = "perfil"):
        await _respond(interaction, handle_risco(api, tipo))

    @tree.command(name="sinais",
                  description="Previsão de um item: reversão à média, regime, risco")
    @app_commands.describe(item="Comece a digitar e escolha da lista (PT/EN)")
    @app_commands.autocomplete(item=item_ac)
    async def sinais_cmd(interaction: discord.Interaction, item: str):
        await _respond(interaction, handle_sinais(api, item))

    # URL pública p/ os ícones (o embed é buscado pelos servidores do Discord,
    # então precisa ser acessível na internet — não o 127.0.0.1 do bot embarcado).
    public_url = os.environ.get(
        "ALBION_PUBLIC_URL", "https://mercado-albion.onrender.com").rstrip("/")

    @tree.command(name="ajuda",
                  description="Guia de TODOS os comandos e como usar cada um")
    async def ajuda_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        emb = discord.Embed(title=GUIDE_TITLE,
                            description=HELP_INTRO, color=0xC9A24B)
        for name, body in help_fields():
            emb.add_field(name=name, value=body, inline=False)
        emb.set_footer(text="Digite /ajuda a qualquer momento para rever isto.")
        await interaction.followup.send(embed=emb, ephemeral=True)

    @tree.command(
        name="builds",
        description="Builds meta da guild por arma e conteúdo (com ícones)")
    @app_commands.describe(arma="Árvore de arma",
                           conteudo="Tipo de conteúdo (opcional)")
    @app_commands.choices(
        arma=[app_commands.Choice(name=lbl, value=val)
              for lbl, val in BUILD_TREES],
        conteudo=([app_commands.Choice(name="Todos os conteúdos", value="todos")]
                  + [app_commands.Choice(name=lbl, value=val)
                     for lbl, val in BUILD_CONTENTS]))
    async def builds_cmd(interaction: discord.Interaction, arma: str,
                         conteudo: str = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # "Todos os conteúdos" (ou sem escolher) = mostra TODAS as builds da
            # classe, com card + imagem — igual a escolher a classe fazia antes.
            if conteudo == "todos":
                conteudo = None
            matches = find_builds(arma, conteudo)
            if not matches:
                tree_lbl = _TREE_LABEL.get(arma, arma)
                avail = sorted({_CONTENT_LABEL.get(b["content"], b["content"])
                                for b in find_builds(arma)})
                await interaction.followup.send(
                    f"Não há build de **{tree_lbl}** para esse conteúdo.\n"
                    f"Conteúdos com build nessa árvore: {', '.join(avail) or '—'}.",
                    ephemeral=True)
                return
            embeds, files = [], []
            total = 0                     # Discord: soma de TODOS os embeds <= 6000
            for b in matches[:10]:        # Discord: máx 10 embeds/anexos por msg
                desc = (b.get("role") or "").strip()
                desc = ((desc + "\n\n" if desc else "") + build_card_text(b))[:4000]
                title = build_title(b)
                if embeds and total + len(title) + len(desc) > 5800:
                    break                 # não estoura o teto agregado (falha 400)
                total += len(title) + len(desc)
                emb = discord.Embed(title=title, description=desc, color=0xC9A24B)
                # Imagem do LOADOUT INTEIRO anexada do disco (attachment://) — o
                # Discord sempre renderiza, sem depender de ALBION_PUBLIC_URL/Render
                # acordado nem do Cloudflare (que engolia o thumbnail antigo da arma).
                f = build_image_file(b, len(files))
                if f:
                    files.append(f)
                    emb.set_image(url=f"attachment://{f.filename}")
                embeds.append(emb)
            note = None
            if len(matches) > len(embeds):
                note = (f"São {len(matches)} builds de {_TREE_LABEL.get(arma, arma)}; "
                        f"mostrei {len(embeds)} (limite do Discord). Escolha um "
                        f"conteúdo específico pra ver as demais.")
            await interaction.followup.send(content=note, embeds=embeds,
                                            files=files, ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "Erro ao montar a build. Veja o log do bot.", ephemeral=True)

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

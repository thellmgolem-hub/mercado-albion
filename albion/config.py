# -*- coding: utf-8 -*-
"""Constantes do app: servidores, cidades, taxas e limites da API."""
import os

SERVERS = {
    "americas": "https://west.albion-online-data.com",
    "europa": "https://europe.albion-online-data.com",
    "asia": "https://east.albion-online-data.com",
}
DEFAULT_SERVER = "americas"

# Localizações de mercado aceitas pela API (strings exatas)
CITIES = [
    "Bridgewatch",
    "Caerleon",
    "Fort Sterling",
    "Lymhurst",
    "Martlock",
    "Thetford",
    "Brecilien",
    "Black Market",
]
ROYAL_CITIES = ["Bridgewatch", "Caerleon", "Fort Sterling", "Lymhurst", "Martlock", "Thetford"]

CITY_LABELS_PT = {
    "Bridgewatch": "Bridgewatch",
    "Caerleon": "Caerleon",
    "Fort Sterling": "Fort Sterling",
    "Lymhurst": "Lymhurst",
    "Martlock": "Martlock",
    "Thetford": "Thetford",
    "Brecilien": "Brecilien",
    "Black Market": "Mercado Negro",
}

QUALITIES = {1: "Normal", 2: "Bom", 3: "Notável", 4: "Excelente", 5: "Obra-prima"}

# Taxas do mercado (fração do preço)
SALES_TAX_PREMIUM = 0.04      # imposto de venda com premium
SALES_TAX_NO_PREMIUM = 0.08   # imposto de venda sem premium
SETUP_FEE = 0.025             # taxa de anúncio (ordens de venda e de compra), não reembolsável

CATEGORIES_PT = {
    "weapons": "Armas",
    "armors": "Armaduras",
    "head": "Capacetes",
    "shoes": "Botas",
    "offhands": "Mão Secundária",
    "capes": "Capas",
    "bags": "Bolsas",
    "mounts": "Montarias",
    "consumables": "Consumíveis",
    "gathering": "Equip. de Coleta",
    "crafting": "Recursos & Criação",
    "artefacts": "Artefatos",
    "farming": "Fazenda",
    "furniture": "Mobília",
    "vanity": "Vaidade",
    "other": "Outros",
}

SUBCATEGORIES_PT = {
    "resources": "Recursos brutos",
    "refinedresources": "Recursos refinados",
    "potions": "Poções",
    "cookedfood": "Comidas",
    "vanity": "Vaidade",
    "luxurygoods": "Bens de luxo",
    "tokens": "Fichas",
    "maps": "Mapas",
    "lootitem": "Itens de saque",
    "questitems": "Itens de missão",
    "trash": "Sucata",
    "artefacts": "Artefatos",
    "rune": "Runas",
    "soul": "Almas",
    "relic": "Relíquias",
    "avalonianenergy": "Energia Avaloniana",
    "shard_avalonian": "Fragmentos Avalonianos",
    "bag": "Bolsas",
    "cape": "Capas",
    "journal": "Diários",
    "labourers": "Trabalhadores",
    "seed": "Sementes",
    "animals": "Animais",
    "ridinghorse": "Cavalos",
    "ridableanimals": "Montarias",
    "armoredhorse": "Cavalos blindados",
    "rareanimals": "Animais raros",
    "essence": "Essências",
    "skillbook": "Livros",
    "fishingbait": "Iscas",
    "fish": "Peixes",
    "event": "Evento",
}

# Categorias que o Mercado Negro compra (equipamento de combate)
BLACK_MARKET_CATEGORIES = {"weapons", "armors", "head", "shoes", "offhands", "capes", "bags"}

# Limites da API pública do Albion Online Data Project
# Oficial: 180 req/min E 300 req/5min (simultâneos, por IP) — janelas com margem
RATE_WINDOWS = [(60, 150), (300, 280)]  # (segundos, máx. de requisições)
MAX_URL_LEN = 3900            # limite prático de URL (oficial: 4096)
MAX_ITEMS_PER_REQUEST = 50    # mantém o tamanho da resposta razoável

PRICES_TTL = 300              # 5 min de cache para preços ao vivo
HISTORY_TTL = 1800            # 30 min de cache para histórico
GOLD_TTL = 300

# Janelas canônicas de busca de histórico por escala (dias). Pedidos menores
# reaproveitam o cache da janela maior — 1 requisição cobre várias consultas.
HISTORY_FETCH_WINDOWS = {24: [90, 180, 365], 6: [30, 90], 1: [7, 30]}

# Retenção de snapshots BRUTOS (30 min). O histórico de longo prazo fica em
# price_snapshots_daily (agregado, permanente); os snapshots crus só servem à
# análise de curto prazo (survival/backtest de flip fantasma). Com ~1M
# linhas/dia para a watchlist cheia, 7 dias bastam e limitam a tabela a um
# tamanho em que as análises rodam em segundos.
SNAPSHOT_RETENTION_DAYS = 7
# Intervalo da poda automática de snapshots no servidor (0 = desligada)
AUTO_PRUNE_INTERVAL_H = 24

# Âncoras ABSOLUTAS do opportunity_score (escala log; valor da âncora = 100):
# lucro/unidade (prata), potencial/dia (prata), liquidez (itens/dia)
SCORE_ANCHORS = {"profit": 50_000, "daily": 1_000_000, "liquidity": 200}

# Cidade com bônus de refino por família. Default = fallback; se
# data/craft_data.json existir (gerado de craftingmodifiers.json do dump), os
# valores são DERIVADOS do jogo em vez de hardcoded (resolve a pendência da
# auditoria sobre RRR "não validado").
REFINING_BONUS_CITY = {
    "WOOD": "Fort Sterling",
    "FIBER": "Lymhurst",
    "ROCK": "Bridgewatch",
    "HIDE": "Martlock",
    "ORE": "Thetford",
}
# RRR de refino (%) — fallback; sobrescrito pelo craft_data se disponível
REFINING_RRR = {"base": 15.2, "bonus": 36.7,
                "base_focus": 43.5, "bonus_focus": 53.9}

try:
    import json as _json
    from pathlib import Path as _Path
    _cd = _json.loads((_Path(__file__).resolve().parent.parent
                       / "data" / "craft_data.json").read_text(encoding="utf-8"))
    _fam = {"WOOD": "wood", "FIBER": "fiber", "ROCK": "rock",
            "HIDE": "hide", "ORE": "ore"}
    _ref = _cd.get("refining", {})
    REFINING_BONUS_CITY = {f: _ref[r]["city"] for f, r in _fam.items()
                           if r in _ref} or REFINING_BONUS_CITY
    # média das famílias (todas iguais no jogo) -> RRR derivado
    if _ref:
        _any = next(iter(_ref.values()))
        REFINING_RRR = {
            "base": _any["rrr_base_pct"], "bonus": _any["rrr_bonus_pct"],
            "base_focus": _any["rrr_base_focus_pct"],
            "bonus_focus": _any["rrr_bonus_focus_pct"],
        }
except (OSError, ValueError, KeyError, StopIteration):
    pass  # mantém os fallbacks

# Teto de capital sugerido por ordem de serviço (missões da camada leiga)
ORDER_MAX_CAPITAL = 2_000_000

# Fração realista do volume diário que um jogador captura ao spread atual
# (competição + slippage). Aplicada no "Pot./dia realista"; calibrável por
# backtest no futuro.
CAPTURE_RATE = 0.20

# Coleta automática da watchlist pelo servidor (0 = desligada)
AUTO_COLLECT_INTERVAL_MIN = 30

# Webhook do Discord para `analyze.py report --discord` (cole a URL do canal:
# Configurações do canal > Integrações > Webhooks > Novo webhook > Copiar URL)
DISCORD_WEBHOOK_URL = ""

# API pública de kills/batalhas (killboard) — ver docs/SCHEMA_GAMEINFO.md
GAMEINFO_BASES = {
    "americas": "https://gameinfo.albiononline.com/api/gameinfo",
    "europa": "https://gameinfo-ams.albiononline.com/api/gameinfo",
    "asia": "https://gameinfo-sgp.albiononline.com/api/gameinfo",
}
# páginas de 51 eventos por varredura incremental. 18×51=918 (< MAX_OFFSET 1000)
# cobre ~90 ev/min num intervalo de 10 min; em 6 páginas (306) um pico saturava
# e os eventos além do topo eram perdidos em silêncio (o checkpoint avançava
# sobre eles). A varredura para cedo ao alcançar o checkpoint, então fora de
# pico isso não custa requisições extras.
GAMEINFO_EVENT_PAGES = 18
GAMEINFO_BATTLE_PAGES = 2
# O killboard das Américas gera ~50 eventos/min em pico: cadência própria,
# mais rápida que a coleta de mercado
AUTO_INTEL_INTERVAL_MIN = 10

# Acesso pela rede local (oficiais da guild abrem http://SEU_IP:8528).
SERVE_LAN = False

# Autenticacao local pseudonima. Ativa por padrao; a variavel de desativacao
# existe apenas para a suite de testes e manutencao offline controlada.
AUTH_REQUIRED = os.environ.get("ALBION_AUTH_DISABLED") != "1"
AUTH_COOKIE_SECURE = os.environ.get("ALBION_AUTH_COOKIE_SECURE") == "1"
AUTH_SESSION_COOKIE = "albion_session"
AUTH_DEVICE_COOKIE = "albion_device"
# Máx. de itens por rodada de coleta. 2.300 itens ≈ 92 requisições à API
# (2 × itens/50), ~40 s por rodada — confortável dentro de 150/min e 300/5min.
COLLECT_MAX_ITEMS = 2500

USER_AGENT = "albion-market-local/1.0 (app local de consulta de mercado)"

# --- Sweep fatiado (piloto na nuvem) -------------------------------------
# O sweep varre o mercado INTEIRO em fatias, tocado por um cron a cada minuto.
# Preços sempre frescos; histórico só refeito se mais velho que SWEEP_HISTORY_TTL
# (muda devagar — refazer a cada ciclo seria desperdício de requisições).
SWEEP_ITEMS_PER_TICK = 100        # itens por toque (~2 chunks de preço/histórico)
SWEEP_HISTORY_TTL = 6 * 3600      # refaz histórico do item no máx. de 6 em 6 h
SWEEP_HISTORY_DAYS = 90           # janela canônica de histórico (escala 24h)
# Universo de varredura = TODO item negociável. 'vanity' são skins não-vendáveis
# (0/696 com mercado). 'other' e 'furniture' contêm itens negociáveis (diários,
# labourers, mobília, mapas) — por isso filtramos por SUBcategoria-lixo, não pela
# categoria inteira, para não deixar de fora ~700 itens com mercado real.
SWEEP_SKIP_CATEGORIES = {"vanity"}
# Subcategorias claramente sem mercado (quest/loot/expedição vinculada). NÃO
# inclui 'guilds' (martelos de cerco/estandartes SÃO craftáveis/vendáveis) nem
# 'killtrophy' (mobília, mesma família vendável das outras 311 de mobília).
SWEEP_SKIP_SUBS = {"lootitem", "questitems", "trash", "hardcoreexpeditions"}
# Token exigido no /api/sweep (defina em prod; vazio = liberado p/ dev local)
SWEEP_TOKEN = os.environ.get("ALBION_SWEEP_TOKEN", "")

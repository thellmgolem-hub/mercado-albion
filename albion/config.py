# -*- coding: utf-8 -*-
"""Constantes do app: servidores, cidades, taxas e limites da API."""

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

# Retenção de snapshots brutos (comando `analyze.py prune` agrega em
# price_snapshots_daily antes de apagar)
SNAPSHOT_RETENTION_DAYS = 180

# Âncoras ABSOLUTAS do opportunity_score (escala log; valor da âncora = 100):
# lucro/unidade (prata), potencial/dia (prata), liquidez (itens/dia)
SCORE_ANCHORS = {"profit": 50_000, "daily": 1_000_000, "liquidity": 200}

# Fração realista do volume diário que um jogador captura ao spread atual
# (competição + slippage). Aplicada no "Pot./dia realista"; calibrável por
# backtest no futuro.
CAPTURE_RATE = 0.20

# Coleta automática da watchlist pelo servidor (0 = desligada)
AUTO_COLLECT_INTERVAL_MIN = 30

# Webhook do Discord para `analyze.py report --discord` (cole a URL do canal:
# Configurações do canal > Integrações > Webhooks > Novo webhook > Copiar URL)
DISCORD_WEBHOOK_URL = ""

# Acesso pela rede local (oficiais da guild abrem http://SEU_IP:8528).
# Defina ACCESS_TOKEN para exigir ?token=... na primeira visita (vira cookie).
SERVE_LAN = False
ACCESS_TOKEN = ""
# Máx. de itens por rodada de coleta. 2.300 itens ≈ 92 requisições à API
# (2 × itens/50), ~40 s por rodada — confortável dentro de 150/min e 300/5min.
COLLECT_MAX_ITEMS = 2500

USER_AGENT = "albion-market-local/1.0 (app local de consulta de mercado)"

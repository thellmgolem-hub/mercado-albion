# -*- coding: utf-8 -*-
"""analyze.py — CLI de análise do mercado do Albion Online (servidor das Américas).

Consulta a API pública do Albion Online Data Project através dos módulos do
pacote `albion` (com cache local em data/cache.db). Saída em tabela alinhada,
JSON ou CSV (flag global --format, aceita antes ou depois do subcomando).

Exemplos de uso:
  python analyze.py search bolsa --limit 5
  python analyze.py search "t4 espada" --cat weapons --tier-min 4 --tier-max 4
  python analyze.py prices T4_BAG --qualities 1
  python analyze.py prices "Bolsa do Adepto,T5_BAG" --cities Martlock,Lymhurst --fresh
  python analyze.py flips T5_BAG --min-profit 0 --buy-mode instant --sell-mode order
  python analyze.py flips T6_BAG,T7_BAG --no-premium --qualities 1,2 --same-city
  python analyze.py scan --cat bags --tier-min 4 --min-profit 5000 --volume
  python analyze.py scan --cat armors --sub plate_armor --ench 0,1 --min-roi 10 --limit 20
  python analyze.py sell T6_HEAD_PLATE_SET1 --qualities 1
  python analyze.py history T4_BAG --cities Martlock --quality 1 --days 7 --scale 24
  python analyze.py gold --count 24
  python analyze.py status --detail
  python analyze.py sql "SELECT city, COUNT(*) AS n FROM prices GROUP BY city"
  python analyze.py --format json flips T5_BAG
  python analyze.py prices T4_BAG --format csv

Dicas:
  - Itens aceitam id exato (T4_BAG), nome PT exato (Bolsa do Adepto) ou termos
    de busca não ambíguos; separe múltiplos itens por vírgula.
  - Tokens de busca: "t4" (tier), "@1" (encantamento), "4.1" (tier.encanto).
  - Cidades aceitam o nome da API ou o rótulo PT (ex.: "Mercado Negro").
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from albion import config
from albion.flips import age_minutes, compute_flips, where_to_sell
from albion.items import ItemDB, norm

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FORMATS = ("table", "json", "csv")


# ---------------------------------------------------------------- utilidades

def die(msg: str, code: int = 1):
    print(f"ERRO: {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str):
    """Mensagem informativa no stderr (mantém o stdout limpo para parsing)."""
    print(msg, file=sys.stderr)


def api_guard(fn):
    try:
        return fn()
    except Exception as e:
        try:
            import httpx
        except ModuleNotFoundError:
            httpx = None
        if httpx is not None and isinstance(e, httpx.HTTPError):
            die(f"Falha ao consultar a API do Albion Data: {e}")
        raise


def make_aodp():
    """Cria o cliente AODP apenas nos comandos que precisam de rede/httpx."""
    try:
        from albion.client import AODP
    except ModuleNotFoundError as e:
        if e.name == "httpx":
            die("Dependência ausente: httpx. Rode "
                "`python -m pip install -r requirements.txt` ou use o run.bat.")
        raise
    return AODP()


def fmt_int(n) -> str:
    """1234567 -> '1.234.567' (estilo BR)."""
    return f"{int(n):,}".replace(",", ".")


def fmt_float(n, dec: int = 1) -> str:
    """1234.5 -> '1.234,5' (estilo BR)."""
    s = f"{n:,.{dec}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, int):
        return fmt_int(v)
    if isinstance(v, float):
        return fmt_int(v) if v.is_integer() else fmt_float(v)
    return str(v)


def render_table(rows: list[dict], cols: list[tuple[str, str]]):
    if not rows:
        print("Nenhum resultado.")
        return
    headers = [h for _, h in cols]
    numeric = []
    for k, _ in cols:
        vals = [r.get(k) for r in rows]
        numeric.append(all(v is None or (isinstance(v, (int, float))
                                         and not isinstance(v, bool))
                           for v in vals))
    grid = [[fmt_cell(r.get(k)) for k, _ in cols] for r in rows]
    widths = [max(len(headers[i]), *(len(g[i]) for g in grid))
              for i in range(len(cols))]
    line = "  ".join(
        h.rjust(w) if num else h.ljust(w)
        for h, w, num in zip(headers, widths, numeric))
    print(line.rstrip())
    print("  ".join("-" * w for w in widths))
    for g in grid:
        line = "  ".join(
            c.rjust(w) if num else c.ljust(w)
            for c, w, num in zip(g, widths, numeric))
        print(line.rstrip())


def emit(rows: list[dict], cols: list[tuple[str, str]], fmt: str):
    """Imprime as linhas no formato pedido.

    table: colunas selecionadas, números no estilo BR
    json : dicionários completos (todos os campos), valores crus
    csv  : colunas selecionadas, valores crus
    """
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif fmt == "csv":
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow([k for k, _ in cols])
        for r in rows:
            w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in cols])
    else:
        render_table(rows, cols)


# ---------------------------------------------------- parsing de argumentos

def parse_csv(value):
    if not value:
        return None
    return [v.strip() for v in str(value).split(",") if v.strip()]


def parse_qualities(value):
    vals = parse_csv(value)
    if not vals:
        return None
    out = []
    for v in vals:
        try:
            q = int(v)
        except ValueError:
            die(f"Qualidade inválida: '{v}' (use números de 1 a 5)")
        if not 1 <= q <= 5:
            die(f"Qualidade fora do intervalo: {q} (use 1 a 5)")
        out.append(q)
    return out


def parse_ench(value):
    vals = parse_csv(value)
    if not vals:
        return None
    out = []
    for v in vals:
        try:
            e = int(v)
        except ValueError:
            die(f"Encantamento inválido: '{v}' (use números de 0 a 4)")
        if not 0 <= e <= 4:
            die(f"Encantamento fora do intervalo: {e} (use 0 a 4)")
        out.append(e)
    return out


def parse_cities(value):
    vals = parse_csv(value)
    if not vals:
        return None
    lookup = {}
    for c in config.CITIES:
        lookup[norm(c)] = c
        lookup[norm(config.CITY_LABELS_PT.get(c, c))] = c
    out = []
    for v in vals:
        city = lookup.get(norm(v))
        if not city:
            die(f"Cidade desconhecida: '{v}'. Válidas: {', '.join(config.CITIES)}")
        out.append(city)
    return out


def parse_city_scope(cities, buy_cities=None, sell_cities=None):
    """Resolve cidades de consulta e filtros opcionais de compra/venda."""
    base = parse_cities(cities)
    buy = parse_cities(buy_cities)
    sell = parse_cities(sell_cities)
    if base is None:
        selected = set((buy or []) + (sell or []))
        base = [c for c in config.CITIES if c in selected] if selected else config.CITIES
    return base, (set(buy) if buy else None), (set(sell) if sell else None)


def resolve_item(db: ItemDB, term: str) -> dict:
    """Resolve um termo para um item: id exato > nome exato > busca não ambígua."""
    term = term.strip()
    it = db.get(term)
    if it:
        return it
    results = db.search(term, limit=6)
    if not results:
        die(f"Item não encontrado: '{term}'. Use `search` para localizar o id.")
    nterm = norm(term)
    exact = [r for r in results
             if norm(r["pt"]) == nterm or norm(r["en"]) == nterm
             or r["id"].lower() == nterm]
    if exact:
        return exact[0]
    if len(results) == 1:
        return results[0]
    print(f"ERRO: termo ambíguo: '{term}'. Candidatos:", file=sys.stderr)
    for r in results[:5]:
        print(f"  {r['id']:<32} {r['pt']} ({r['tier']}.{r['ench']})",
              file=sys.stderr)
    sys.exit(2)


def resolve_items(db: ItemDB, value: str) -> list[dict]:
    terms = parse_csv(value)
    if not terms:
        die("Informe ao menos um item (separe múltiplos por vírgula).")
    seen, out = set(), []
    for t in terms:
        it = resolve_item(db, t)
        if it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def te(meta: dict) -> str:
    return f"{meta.get('tier', 0)}.{meta.get('ench', 0)}"


def city_pt(city: str) -> str:
    return config.CITY_LABELS_PT.get(city, city)


# ---------------------------------------------------------------- comandos

def cmd_search(args, fmt):
    db = ItemDB()
    results = db.search(" ".join(args.termo), cat=args.cat,
                        sub=args.sub, ench=args.ench,
                        tier_min=args.tier_min, tier_max=args.tier_max,
                        limit=args.limit)
    rows = [{
        "id": r["id"],
        "nome": r["pt"],
        "tier_ench": f"{r['tier']}.{r['ench']}",
        "categoria": config.CATEGORIES_PT.get(r["cat"], r["cat"]),
        "subcategoria": config.SUBCATEGORIES_PT.get(r["sub"], r["sub"] or "-"),
    } for r in results]
    emit(rows, [("id", "Id"), ("nome", "Nome"), ("tier_ench", "T.E"),
                ("categoria", "Categoria"), ("subcategoria", "Subcategoria")],
         fmt)


def cmd_prices(args, fmt):
    db = ItemDB()
    items = resolve_items(db, " ".join(args.itens))
    item_ids = [i["id"] for i in items]
    cities = parse_cities(args.cities) or config.CITIES
    quals = parse_qualities(args.qualities)
    aodp = make_aodp()
    max_age = 0 if args.fresh else args.max_age
    price_rows = api_guard(lambda: aodp.get_prices(item_ids, cities,
                                                   max_age=max_age))
    item_order = {iid: n for n, iid in enumerate(item_ids)}
    city_order = {c: n for n, c in enumerate(cities)}
    rows = []
    for r in price_rows:
        if quals and r["quality"] not in quals:
            continue
        sell = r["sell_price_min"] or None
        buy = r["buy_price_max"] or None
        if sell is None and buy is None:
            continue
        meta = db.get(r["item_id"]) or {}
        rows.append({
            "item_id": r["item_id"],
            "item": meta.get("pt", r["item_id"]),
            "tier_ench": te(meta),
            "qualidade": r["quality"],
            "cidade": city_pt(r["city"]),
            "venda_min": sell,
            "idade_venda_min": age_minutes(r["sell_price_min_date"]),
            "compra_max": buy,
            "idade_compra_min": age_minutes(r["buy_price_max_date"]),
            "_ord": (item_order.get(r["item_id"], 99), r["quality"],
                     city_order.get(r["city"], 99)),
        })
    rows.sort(key=lambda r: r.pop("_ord"))
    emit(rows, [("item", "Item"), ("tier_ench", "T.E"), ("qualidade", "Q"),
                ("cidade", "Cidade"), ("venda_min", "Venda mín"),
                ("idade_venda_min", "Idade V (min)"),
                ("compra_max", "Compra máx"),
                ("idade_compra_min", "Idade C (min)")], fmt)


FLIP_COLS = [
    ("name_pt", "Item"), ("tier_ench", "T.E"), ("quality", "Q"),
    ("buy_city", "Comprar em"), ("buy_price", "Compra"),
    ("sell_city", "Vender em"), ("sell_price", "Venda"),
    ("profit", "Lucro"), ("roi_pct", "ROI %"),
    ("confidence_label", "Conf."),
    ("buy_age_min", "Idade C (min)"), ("sell_age_min", "Idade V (min)"),
]


def _prep_flip_rows(opps: list[dict]) -> list[dict]:
    for o in opps:
        o["tier_ench"] = f"{o['tier']}.{o['ench']}"
        o["buy_city"] = city_pt(o["buy_city"])
        sell_city = city_pt(o["sell_city"])
        if o.get("bm_order_quality") and o["bm_order_quality"] != o["quality"]:
            sell_city += f" (ordem q{o['bm_order_quality']})"
        o["sell_city"] = sell_city
    return opps


def cmd_flips(args, fmt):
    db = ItemDB()
    items = resolve_items(db, " ".join(args.itens))
    item_ids = [i["id"] for i in items]
    cities, buy_set, sell_set = parse_city_scope(
        args.cities, args.buy_cities, args.sell_cities)
    aodp = make_aodp()
    price_rows = api_guard(lambda: aodp.get_prices(
        item_ids, cities, max_age=args.max_age))
    metas = {i["id"]: i for i in items}
    opps = compute_flips(
        price_rows, metas, premium=args.premium, buy_mode=args.buy_mode,
        sell_mode=args.sell_mode, min_profit=args.min_profit,
        min_roi=args.min_roi, same_city=args.same_city,
        max_age_buy=args.max_age_buy, max_age_sell=args.max_age_sell,
        qualities=parse_qualities(args.qualities),
        buy_cities=buy_set, sell_cities=sell_set)[:args.limit]
    info(f"Modo: compra {args.buy_mode} / venda {args.sell_mode} / "
         f"{'com' if args.premium else 'sem'} premium — "
         f"{len(opps)} oportunidade(s).")
    emit(_prep_flip_rows(opps), FLIP_COLS, fmt)


def cmd_scan(args, fmt):
    db = ItemDB()
    if args.cat:
        valid = sorted(db.categories())
        if args.cat not in valid:
            die(f"Categoria desconhecida: '{args.cat}'. "
                f"Válidas: {', '.join(valid)}")
    items = db.filter(cat=args.cat, sub=args.sub, tier_min=args.tier_min,
                      tier_max=args.tier_max, ench_list=parse_ench(args.ench),
                      limit=args.max_items)
    if not items:
        die("Nenhum item corresponde aos filtros (--cat/--sub/--tier/--ench).")
    item_ids = [i["id"] for i in items]
    cities, buy_set, sell_set = parse_city_scope(
        args.cities, args.buy_cities, args.sell_cities)
    info(f"Escaneando {len(item_ids)} itens em {len(cities)} cidades...")
    aodp = make_aodp()
    price_rows = api_guard(lambda: aodp.get_prices(
        item_ids, cities, max_age=args.max_age))
    metas = {i["id"]: i for i in items}
    opps = compute_flips(
        price_rows, metas, premium=args.premium, buy_mode=args.buy_mode,
        sell_mode=args.sell_mode, min_profit=args.min_profit,
        min_roi=args.min_roi, same_city=args.same_city,
        max_age_buy=args.max_age_buy, max_age_sell=args.max_age_sell,
        qualities=parse_qualities(args.qualities),
        buy_cities=buy_set, sell_cities=sell_set)[:args.limit]

    cols = list(FLIP_COLS)
    if args.volume and opps:
        info("Buscando volume diário (últimos 7 dias)...")
        top_ids = list(dict.fromkeys(o["item_id"] for o in opps))[:60]
        vol_cities = sorted({c for o in opps
                             for c in (o["buy_city"], o["sell_city"])})
        series = api_guard(lambda: aodp.get_history(
            top_ids, vol_cities, time_scale=24, days=7))
        vol = {}
        for s in series:
            pts = s["data"]
            if pts:
                total = sum(p["item_count"] for p in pts)
                days_n = max(len({p["ts"][:10] for p in pts}), 1)
                vol[(s["item_id"], s["city"], s["quality"])] = round(total / days_n)
        for o in opps:
            vb = vol.get((o["item_id"], o["buy_city"], o["quality"]))
            vs = vol.get((o["item_id"], o["sell_city"], o["quality"]))
            o["vol_compra_dia"] = vb
            o["vol_venda_dia"] = vs
            # 0 é dado real (item ilíquido); None = sem histórico
            o["potencial_dia"] = (round(min(vb, vs) * o["profit"])
                                  if vb is not None and vs is not None else None)
        cols += [("vol_compra_dia", "Vol C/dia"), ("vol_venda_dia", "Vol V/dia"),
                 ("potencial_dia", "Potencial/dia")]

    info(f"{len(item_ids)} itens escaneados — {len(opps)} oportunidade(s) "
         f"(compra {args.buy_mode} / venda {args.sell_mode} / "
         f"{'com' if args.premium else 'sem'} premium).")
    emit(_prep_flip_rows(opps), cols, fmt)


def cmd_sell(args, fmt):
    db = ItemDB()
    items = resolve_items(db, " ".join(args.itens))
    item_ids = [i["id"] for i in items]
    aodp = make_aodp()
    price_rows = api_guard(lambda: aodp.get_prices(
        item_ids, config.CITIES, max_age=args.max_age))
    metas = {i["id"]: i for i in items}
    results = where_to_sell(price_rows, metas, premium=args.premium,
                            qualities=parse_qualities(args.qualities))
    method_pt = {"instant": "venda instantânea", "order": "ordem de venda"}
    rows = []
    for res in results:
        for n, o in enumerate(res["options"], 1):
            city = city_pt(o["city"])
            if o.get("bm_order_quality") and o["bm_order_quality"] != res["quality"]:
                city += f" (ordem q{o['bm_order_quality']})"
            rows.append({
                "item_id": res["item_id"],
                "item": res["name_pt"],
                "tier_ench": f"{res['tier']}.{res['ench']}",
                "qualidade": res["quality"],
                "rank": n,
                "cidade": city,
                "metodo": method_pt.get(o["method"], o["method"]),
                "preco": o["price"],
                "liquido": o["net"],
                "idade_min": o["age_min"],
            })
    info(f"Receita líquida {'com' if args.premium else 'sem'} premium "
         f"(imposto {int(100 * (config.SALES_TAX_PREMIUM if args.premium else config.SALES_TAX_NO_PREMIUM))}%"
         f" + 2,5% de anúncio nas ordens).")
    emit(rows, [("item", "Item"), ("tier_ench", "T.E"), ("qualidade", "Q"),
                ("rank", "#"), ("cidade", "Cidade"), ("metodo", "Método"),
                ("preco", "Preço"), ("liquido", "Líquido"),
                ("idade_min", "Idade (min)")], fmt)


def cmd_history(args, fmt):
    db = ItemDB()
    items = resolve_items(db, " ".join(args.itens))
    item_ids = [i["id"] for i in items]
    cities = parse_cities(args.cities) or config.CITIES
    aodp = make_aodp()
    series = api_guard(lambda: aodp.get_history(
        item_ids, cities, time_scale=args.scale, days=args.days,
        max_age=args.max_age))
    if args.quality is not None:
        series = [s for s in series if s["quality"] == args.quality]
    rows = []
    for s in sorted(series, key=lambda s: (s["item_id"], s["city"], s["quality"])):
        meta = db.get(s["item_id"]) or {}
        for p in s["data"]:
            rows.append({
                "item_id": s["item_id"],
                "item": meta.get("pt", s["item_id"]),
                "cidade": city_pt(s["city"]),
                "qualidade": s["quality"],
                "data": p["ts"].replace("T", " ")[:16],
                "preco_medio": round(p["avg_price"], 1),
                "volume": p["item_count"],
            })
    emit(rows, [("item", "Item"), ("cidade", "Cidade"), ("qualidade", "Q"),
                ("data", "Data"), ("preco_medio", "Preço médio"),
                ("volume", "Volume")], fmt)


def cmd_gold(args, fmt):
    aodp = make_aodp()
    points = api_guard(lambda: aodp.get_gold(args.count))
    points = sorted(points, key=lambda p: p["ts"])  # ordem cronológica
    rows = []
    prev = None
    for p in points:
        var = (round(100 * (p["price"] - prev) / prev, 2)
               if prev else None)
        rows.append({
            "data": p["ts"].replace("T", " ")[:16],
            "preco": p["price"],
            "variacao_pct": var,
        })
        prev = p["price"]
    rows.reverse()  # exibe do mais recente para o mais antigo
    emit(rows, [("data", "Data"), ("preco", "Preço (prata/ouro)"),
                ("variacao_pct", "Variação %")], fmt)


def cmd_sql(args, fmt):
    q = args.query.strip()
    if not re.match(r"^select\b", q, re.IGNORECASE):
        die("Apenas queries SELECT são permitidas (somente leitura).")
    if ";" in q.rstrip().rstrip(";"):
        die("Apenas uma instrução SQL por vez.")
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die(f"Cache não encontrado: {db_path}. Rode um comando de preços antes.")
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        cur = con.execute(q)
        names = [d[0] for d in cur.description] if cur.description else []
        data = cur.fetchall()
        con.close()
    except sqlite3.Error as e:
        die(f"Erro SQL: {e}")
    rows = [dict(zip(names, r)) for r in data]
    emit(rows, [(n, n) for n in names], fmt)


def cmd_status(args, fmt):
    """Resumo somente-leitura da cobertura local de dados."""
    db_path = (DATA / "cache.db").resolve()
    rows = []
    db = ItemDB()
    rows.append({
        "grupo": "itens",
        "nome": "items_db",
        "linhas": len(db.items),
        "itens": len(db.items),
        "ultimo": None,
        "obs": "metadados locais",
    })
    if not db_path.exists():
        rows.append({
            "grupo": "cache",
            "nome": "cache.db",
            "linhas": 0,
            "itens": None,
            "ultimo": None,
            "obs": "cache nao encontrado",
        })
        emit(rows, [("grupo", "Grupo"), ("nome", "Nome"), ("linhas", "Linhas"),
                    ("itens", "Itens"), ("ultimo", "Ultimo"), ("obs", "Obs")],
             fmt)
        return

    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        existing = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        rows.append({
            "grupo": "cache",
            "nome": "cache.db",
            "linhas": db_path.stat().st_size,
            "itens": None,
            "ultimo": None,
            "obs": "tamanho em bytes",
        })
        specs = [
            ("prices", "MAX(datetime(fetched_at,'unixepoch'))",
             "COUNT(DISTINCT item_id)"),
            ("price_snapshots", "MAX(datetime(fetched_at,'unixepoch'))",
             "COUNT(DISTINCT item_id)"),
            ("history", "MAX(datetime(fetched_at,'unixepoch'))",
             "COUNT(DISTINCT item_id)"),
            ("gold", "MAX(ts)", "NULL"),
            ("fetch_log", "MAX(datetime(fetched_at,'unixepoch'))", "NULL"),
            ("watchlist", "MAX(datetime(added_at,'unixepoch'))",
             "COUNT(DISTINCT item_id)"),
            ("collection_runs", "MAX(datetime(started_at,'unixepoch'))", "NULL"),
        ]
        for name, latest_expr, item_expr in specs:
            if name not in existing:
                rows.append({
                    "grupo": "tabela",
                    "nome": name,
                    "linhas": 0,
                    "itens": None,
                    "ultimo": None,
                    "obs": "tabela ainda nao criada",
                })
                continue
            count, items_n, latest = con.execute(
                f"SELECT COUNT(*), {item_expr}, {latest_expr} FROM {name}"
            ).fetchone()
            rows.append({
                "grupo": "tabela",
                "nome": name,
                "linhas": count,
                "itens": items_n,
                "ultimo": latest,
                "obs": "",
            })
        if args.detail:
            for city, count, items_n, latest in con.execute("""
                SELECT city, COUNT(*), COUNT(DISTINCT item_id),
                       MAX(datetime(fetched_at,'unixepoch'))
                FROM prices
                GROUP BY city
                ORDER BY COUNT(*) DESC
            """):
                rows.append({
                    "grupo": "prices/cidade",
                    "nome": city_pt(city),
                    "linhas": count,
                    "itens": items_n,
                    "ultimo": latest,
                    "obs": "",
                })
    finally:
        con.close()

    emit(rows, [("grupo", "Grupo"), ("nome", "Nome"), ("linhas", "Linhas"),
                ("itens", "Itens"), ("ultimo", "Ultimo"), ("obs", "Obs")],
         fmt)


def cmd_watch(args, fmt):
    db = ItemDB()
    aodp = make_aodp()
    if args.acao == "list":
        rows = []
        for w in aodp.watch_list():
            meta = db.get(w["item_id"]) or {}
            rows.append({"item_id": w["item_id"],
                         "item": meta.get("pt", w["item_id"]),
                         "tier_ench": te(meta)})
        emit(rows, [("item_id", "Id"), ("item", "Item"),
                    ("tier_ench", "T.E")], fmt)
        return
    if not args.itens:
        die("Informe os itens (separados por vírgula).")
    items = resolve_items(db, ",".join(args.itens))
    ids = [i["id"] for i in items]
    if args.acao == "add":
        aodp.watch_add(ids)
        info(f"{len(ids)} item(ns) na watchlist.")
    else:
        aodp.watch_remove(ids)
        info(f"{len(ids)} item(ns) removido(s).")


def cmd_collect(args, fmt):
    db = ItemDB()
    aodp = make_aodp()
    item_ids = None
    if args.itens:
        item_ids = [i["id"] for i in resolve_items(db, ",".join(args.itens))]
    elif args.cat:
        item_ids = [i["id"] for i in db.filter(
            cat=args.cat, sub=args.sub, tier_min=args.tier_min,
            tier_max=args.tier_max, limit=args.max_items)]
    cities = parse_cities(args.cities) or config.CITIES
    n = len(item_ids) if item_ids is not None else len(aodp.watch_list())
    info(f"Coletando preços + histórico de {n} item(ns) em "
         f"{len(cities)} cidades (pode levar minutos pelo rate limit)...")
    res = api_guard(lambda: aodp.collect(
        item_ids=item_ids, cities=cities, days=args.days,
        max_items=args.max_items, source="cli"))
    emit([res], [("items", "Itens"), ("price_rows", "Linhas de preço"),
                 ("history_series", "Séries de histórico")], fmt)


def cmd_recommend(args, fmt):
    from app import recommendations  # carrega o app (ItemDB + AODP) sob demanda
    res = recommendations(
        cat=args.cat, sub=args.sub, tier_min=args.tier_min,
        tier_max=args.tier_max, qualities=args.qualities or "1",
        premium=args.premium, min_profit=args.min_profit,
        min_roi=args.min_roi, min_daily_volume=args.min_volume,
        min_active_days=args.min_active_days, history_days=args.history_days,
        max_age_buy=args.max_age_buy, max_age_sell=args.max_age_sell,
        buy_cities=args.buy_cities, sell_cities=args.sell_cities,
        limit=args.limit)
    cov = res.get("coverage", {})
    info(f"{res.get('items_considered', 0)} itens avaliados · cobertura: "
         f"{cov.get('price_items', 0)}/{cov.get('catalog_items', 0)} com preço, "
         f"{cov.get('history_items', 0)} com histórico.")
    rows = _prep_flip_rows(res.get("opportunities", []))
    cols = [("opportunity_score", "Score"), ("opportunity_label", "Selo"),
            *FLIP_COLS,
            ("liquidity_day", "Liq/dia"), ("daily_potential", "Pot/dia")]
    emit(rows, cols, fmt)


def cmd_lab(args, fmt):
    from app import item_analysis
    res = item_analysis(item=args.item, cities=args.cities,
                        quality=args.quality, time_scale=args.scale,
                        days=args.days, cache_only=not args.fetch)
    if fmt == "json":
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    comp = res.get("comparison") or {}
    if comp:
        info(f"Mais barata (VWAP): {city_pt(comp.get('cheapest_city', '-'))} · "
             f"mais cara: {city_pt(comp.get('most_expensive_city', '-'))} · "
             f"spread {comp.get('vwap_spread_pct')}% · mais líquida: "
             f"{city_pt(comp.get('most_liquid_city', '-'))}")
    rows = []
    for s in res.get("series", []):
        if not s.get("points"):
            continue
        rows.append({
            "cidade": city_pt(s["city"]),
            "pontos": s["points"],
            "preco": s.get("latest_price"),
            "vwap": s.get("vwap"),
            "mediana": s.get("median_price"),
            "z": s.get("z_score"),
            "z_robusto": s.get("robust_z"),
            "momentum_pct": s.get("momentum_pct"),
            "vol_dia": s.get("avg_daily_volume"),
            "qualidade_dado": s.get("data_quality_score"),
            "leitura": (s.get("interpretation") or {}).get("stance"),
        })
    if not rows:
        info("Sem histórico local — use `collect` ou `lab --fetch` para buscar.")
    emit(rows, [("cidade", "Cidade"), ("pontos", "Pts"), ("preco", "Preço"),
                ("vwap", "VWAP"), ("mediana", "Mediana"), ("z", "Z(res)"),
                ("z_robusto", "Z rob."), ("momentum_pct", "Momentum %"),
                ("vol_dia", "Vol/dia"), ("qualidade_dado", "Dado"),
                ("leitura", "Leitura")], fmt)


def cmd_survival(args, fmt):
    """Persistência das ordens do topo medida nos snapshots próprios."""
    from albion import survival as survival_mod
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` algumas vezes antes.")
    item_id = None
    if args.item:
        db = ItemDB()
        item_id = resolve_item(db, args.item)["id"]
    cities = parse_cities(args.city) if args.city else None
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        res = survival_mod.persistence(
            con, config.DEFAULT_SERVER, item_id=item_id,
            city=cities[0] if cities else None, quality=args.quality)
    finally:
        con.close()
    if fmt == "json":
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    info(f"{res['snapshot_pairs']} pares de coleta analisados. Persistência ="
         " mesma ordem do topo ainda lá na coleta seguinte (proxy de flip"
         " fantasma). Alimente com coletas repetidas da watchlist.")
    rows = []
    side_pt = {"sell": "venda (anúncio)", "buy": "compra (ordem)"}
    for side, data in res["sides"].items():
        for b in data["buckets"]:
            if not b["pairs"]:
                continue
            rows.append({
                "lado": side_pt.get(side, side),
                "idade_da_ordem": b["age_label"],
                "pares": b["pairs"],
                "persistiu_pct": b["rate_pct"],
                "gap_mediano_min": b["median_gap_min"],
            })
    emit(rows, [("lado", "Lado"), ("idade_da_ordem", "Idade da ordem"),
                ("pares", "Pares"), ("persistiu_pct", "Persistiu %"),
                ("gap_mediano_min", "Gap mediano (min)")], fmt)


def cmd_prune(args, fmt):
    aodp = make_aodp()
    res = aodp.snapshot_prune(days=args.days)
    info(f"Snapshots com mais de {args.days} dias agregados em "
         "price_snapshots_daily e removidos.")
    emit([res], [("aggregated_days", "Dias agregados"),
                 ("deleted_rows", "Linhas brutas removidas")], fmt)


# ------------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(
        prog="analyze.py",
        description="Análises do mercado do Albion Online (Américas) via "
                    "API do Albion Online Data Project.",
        epilog=__doc__.split("Exemplos de uso:")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=FORMATS, default="table",
                    help="formato de saída (padrão: table)")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=FORMATS, default=argparse.SUPPRESS,
                        help="formato de saída (table|json|csv)")

    sub = ap.add_subparsers(
        dest="cmd", required=True,
        metavar="{search,prices,flips,scan,sell,history,recommend,lab,"
                "watch,collect,survival,gold,status,prune,sql}")

    p = sub.add_parser("search", parents=[common],
                       help="busca itens por nome PT/EN ou id")
    p.add_argument("termo", nargs="+",
                   help="termo de busca (aceita tokens t4, @1, 4.1)")
    p.add_argument("--cat", help="categoria (ex.: bags, weapons, armors)")
    p.add_argument("--sub", help="subcategoria (ex.: plate_armor, potions)")
    p.add_argument("--tier-min", type=int)
    p.add_argument("--tier-max", type=int)
    p.add_argument("--ench", type=int, help="encantamento exato (0-4)")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("prices", parents=[common],
                       help="preços atuais por cidade")
    p.add_argument("itens", nargs="+",
                   help="itens separados por vírgula (id ou nome PT)")
    p.add_argument("--cities", help="cidades separadas por vírgula")
    p.add_argument("--qualities", help="qualidades 1-5 separadas por vírgula")
    p.add_argument("--fresh", action="store_true",
                   help="ignora o cache e busca dados novos (max_age=0)")
    p.add_argument("--max-age", type=int, default=config.PRICES_TTL,
                   help="idade máxima do cache de preços em segundos")
    p.set_defaults(func=cmd_prices)

    def flip_flags(p):
        p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                       default=True,
                       help="com premium (imposto 4%%); --no-premium = 8%%")
        p.add_argument("--buy-mode", choices=("instant", "order"),
                       default="instant", help="modo de compra")
        p.add_argument("--sell-mode", choices=("instant", "order"),
                       default="instant", help="modo de venda")
        p.add_argument("--min-profit", type=float, default=0,
                       help="lucro mínimo por unidade (prata)")
        p.add_argument("--qualities",
                       help="qualidades 1-5 separadas por vírgula")
        p.add_argument("--same-city", action="store_true",
                       help="permite comprar e vender na mesma cidade")
        p.add_argument("--cities", help="cidades separadas por vírgula")
        p.add_argument("--max-age", type=int, default=config.PRICES_TTL,
                       help="idade máxima do cache de preços em segundos")
        p.add_argument("--buy-cities",
                       help="cidades de compra separadas por vírgula")
        p.add_argument("--sell-cities",
                       help="cidades de venda separadas por vírgula")
        p.add_argument("--max-age-buy", type=int,
                       help="idade máxima do dado de compra, em minutos")
        p.add_argument("--max-age-sell", type=int,
                       help="idade máxima do dado de venda, em minutos")

    p = sub.add_parser("flips", parents=[common],
                       help="oportunidades de flip para itens específicos")
    p.add_argument("itens", nargs="+",
                   help="itens separados por vírgula (id ou nome PT)")
    flip_flags(p)
    p.add_argument("--min-roi", type=float, help="ROI mínimo em %%")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_flips)

    p = sub.add_parser("scan", parents=[common],
                       help="escaneia uma categoria em busca de flips")
    p.add_argument("--cat", help="categoria (ex.: bags, weapons, armors)")
    p.add_argument("--sub", help="subcategoria (ex.: plate_armor, potions)")
    p.add_argument("--tier-min", type=int)
    p.add_argument("--tier-max", type=int)
    p.add_argument("--ench", help="encantamentos 0-4 separados por vírgula")
    flip_flags(p)
    p.add_argument("--min-roi", type=float, help="ROI mínimo em %%")
    p.add_argument("--limit", type=int, default=50,
                   help="máx. de oportunidades exibidas (padrão: 50)")
    p.add_argument("--max-items", type=int, default=300,
                   help="máx. de itens consultados (padrão: 300)")
    p.add_argument("--volume", action="store_true",
                   help="busca volume diário (mais lento; usa histórico 7d)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sell", parents=[common],
                       help="onde vender: ranqueia cidades/métodos")
    p.add_argument("itens", nargs="+",
                   help="itens separados por vírgula (id ou nome PT)")
    p.add_argument("--qualities", help="qualidades 1-5 separadas por vírgula")
    p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="com premium (imposto 4%%); --no-premium = 8%%")
    p.add_argument("--max-age", type=int, default=config.PRICES_TTL,
                   help="idade máxima do cache de preços em segundos")
    p.set_defaults(func=cmd_sell)

    p = sub.add_parser("history", parents=[common],
                       help="histórico de preço médio e volume")
    p.add_argument("itens", nargs="+",
                   help="itens separados por vírgula (id ou nome PT)")
    p.add_argument("--cities", help="cidades separadas por vírgula")
    p.add_argument("--quality", type=int, help="filtra uma qualidade (1-5)")
    p.add_argument("--days", type=int, default=7,
                   help="dias de histórico (padrão: 7)")
    p.add_argument("--scale", type=int, choices=(1, 6, 24), default=24,
                   help="resolução em horas: 1, 6 ou 24 (padrão: 24)")
    p.add_argument("--max-age", type=int, default=config.HISTORY_TTL,
                   help="idade máxima do cache de histórico em segundos")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("recommend", parents=[common],
                       help="recomendações cache-only com score absoluto")
    p.add_argument("--cat"); p.add_argument("--sub")
    p.add_argument("--tier-min", type=int); p.add_argument("--tier-max", type=int)
    p.add_argument("--qualities", help="qualidades 1-5 (padrão: 1)")
    p.add_argument("--premium", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--min-profit", type=float, default=0)
    p.add_argument("--min-roi", type=float)
    p.add_argument("--min-volume", type=float, default=20,
                   help="volume diário mínimo (padrão: 20)")
    p.add_argument("--min-active-days", type=int, default=2)
    p.add_argument("--history-days", type=int, default=7)
    p.add_argument("--max-age-buy", type=int, default=720)
    p.add_argument("--max-age-sell", type=int, default=720)
    p.add_argument("--buy-cities"); p.add_argument("--sell-cities")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_recommend)

    p = sub.add_parser("lab", parents=[common],
                       help="Item Lab: estatística e leitura de um item")
    p.add_argument("item", help="id ou nome PT do item")
    p.add_argument("--cities", help="cidades separadas por vírgula")
    p.add_argument("--quality", type=int, default=1)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--scale", type=int, choices=(1, 6, 24), default=24)
    p.add_argument("--fetch", action="store_true",
                   help="busca dados novos na API (padrão: só cache local)")
    p.set_defaults(func=cmd_lab)

    p = sub.add_parser("watch", parents=[common],
                       help="gerencia a watchlist de coleta")
    p.add_argument("acao", choices=("add", "rm", "list"))
    p.add_argument("itens", nargs="*",
                   help="itens (para add/rm), separados por vírgula")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("collect", parents=[common],
                       help="coleta preços + histórico (watchlist ou filtros)")
    p.add_argument("--itens", help="itens específicos separados por vírgula")
    p.add_argument("--cat"); p.add_argument("--sub")
    p.add_argument("--tier-min", type=int); p.add_argument("--tier-max", type=int)
    p.add_argument("--cities", help="cidades separadas por vírgula")
    p.add_argument("--days", type=int, default=30,
                   help="janela de histórico (padrão: 30)")
    p.add_argument("--max-items", type=int, default=config.COLLECT_MAX_ITEMS)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("survival", parents=[common],
                       help="persistência das ordens (proxy de flip fantasma)")
    p.add_argument("--item", help="filtra um item (id ou nome PT)")
    p.add_argument("--city", help="filtra uma cidade")
    p.add_argument("--quality", type=int, choices=range(1, 6))
    p.set_defaults(func=cmd_survival)

    p = sub.add_parser("prune", parents=[common],
                       help="agrega snapshots antigos e compacta o cache")
    p.add_argument("--days", type=int, default=config.SNAPSHOT_RETENTION_DAYS,
                   help=f"idade mínima para agregar (padrão: "
                        f"{config.SNAPSHOT_RETENTION_DAYS})")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("gold", parents=[common],
                       help="preço do ouro (prata por ouro)")
    p.add_argument("--count", type=int, default=24,
                   help="número de pontos (padrão: 24, 1 por hora)")
    p.set_defaults(func=cmd_gold)

    p = sub.add_parser("status", parents=[common],
                       help="resume a cobertura local do cache")
    p.add_argument("--detail", action="store_true",
                   help="inclui cobertura de preços por cidade")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sql", parents=[common],
                       help="SQL somente-leitura no cache (data/cache.db)")
    p.add_argument("query",
                   help='query SELECT, ex.: "SELECT COUNT(*) FROM prices"')
    p.set_defaults(func=cmd_sql)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    fmt = getattr(args, "format", "table")
    try:
        args.func(args, fmt)
    except KeyboardInterrupt:
        die("Interrompido pelo usuário.", 130)


if __name__ == "__main__":
    main()

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
    if args.acao == "rebuild":
        # B6: prioriza a coleta por ROI-de-informação = liquidez × demanda real.
        # Itens que giram muito (history) e/ou são muito destruídos (killboard)
        # valem mais a coleta que a lista plana atual.
        with aodp.db_lock:
            vol = dict(aodp.db.execute(
                """SELECT item_id, AVG(item_count) FROM history
                   WHERE server=? AND time_scale=24 AND quality=1 AND item_count>0
                     AND ts>=date('now','-7 days') GROUP BY item_id""",
                [aodp.server]).fetchall())
            dem = dict(aodp.db.execute(
                """SELECT item_id, SUM(victim_units) FROM item_demand_daily
                   WHERE server=? AND day>=date('now','-7 days')
                   GROUP BY item_id""", [aodp.server]).fetchall())
        import math as _m
        cand = set(vol) | set(dem)
        scored = sorted(
            ({"item_id": i,
              "vol": round(vol.get(i, 0) or 0, 1),
              "demanda": int(dem.get(i, 0) or 0),
              "score": round((vol.get(i, 0) or 0) * (1 + _m.log1p(dem.get(i, 0) or 0)))}
             for i in cand),
            key=lambda r: -r["score"])[:args.limit]
        ids = [r["item_id"] for r in scored]
        if args.replace:
            aodp.watch_replace(ids)
            info(f"Watchlist substituída pelos {len(ids)} itens de maior "
                 "ROI-de-informação (liquidez × demanda).")
        else:
            before = {w["item_id"] for w in aodp.watch_list()}
            new = [i for i in ids if i not in before]
            aodp.watch_add(new)
            info(f"{len(new)} item(ns) de alto ROI-de-informação adicionados "
                 f"(top {len(ids)}; use --replace para enxugar a lista).")
        for r in scored:
            r["item"] = (db.get(r["item_id"]) or {}).get("pt", r["item_id"])
        emit(scored, [("item", "Item"), ("vol", "Vol/dia"),
                      ("demanda", "Destruídos 7d"), ("score", "Score")], fmt)
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
    if args.skip_if_recent:
        with aodp.db_lock:
            last = aodp.db.execute(
                "SELECT MAX(started_at) FROM collection_runs"
                " WHERE server=? AND ok=1", [aodp.server]).fetchone()[0]
        if last is not None:
            import time as _time
            age_min = (_time.time() - last) / 60
            if age_min < args.skip_if_recent:
                info(f"Última coleta há {age_min:.0f} min"
                     f" (< {args.skip_if_recent}) — nada a fazer.")
                return
    item_ids = None
    if args.itens:
        item_ids = [i["id"] for i in resolve_items(db, ",".join(args.itens))]
    elif args.cat:
        item_ids = [i["id"] for i in db.filter(
            cat=args.cat, sub=args.sub, tier_min=args.tier_min,
            tier_max=args.tier_max, limit=args.max_items)]
    cities = parse_cities(args.cities) or config.CITIES
    n = len(item_ids) if item_ids is not None else len(aodp.watch_list())
    # B2: --deep busca 365 d de histórico (recarga semanal) p/ destravar
    # regime/risco/reversão sobre séries longas; o normal fica em --days (30).
    days = 365 if args.deep else args.days
    info(f"Coletando preços + histórico ({days} d) de {n} item(ns) em "
         f"{len(cities)} cidades (pode levar minutos pelo rate limit)...")
    res = api_guard(lambda: aodp.collect(
        item_ids=item_ids, cities=cities, days=days,
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
        capture_rate=config.CAPTURE_RATE, limit=args.limit)
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
                        days=args.days, max_age=config.HISTORY_TTL,
                        cache_only=not args.fetch)
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


def _movers_24h(limit=5):
    """Maiores variações de preço (venda mín., q1) na watchlist em ~24 h."""
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        return []
    import time as _time
    now = _time.time()
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute("""
            WITH wl AS (SELECT item_id FROM watchlist WHERE server=:srv),
            recent AS (
              SELECT item_id, city, sell_price_min, fetched_at,
                     ROW_NUMBER() OVER (PARTITION BY item_id, city
                                        ORDER BY fetched_at DESC) rn
              FROM price_snapshots
              WHERE server=:srv AND quality=1 AND sell_price_min > 0
                AND item_id IN (SELECT item_id FROM wl)
                AND fetched_at >= :recente),
            antigo AS (
              SELECT item_id, city, sell_price_min,
                     ROW_NUMBER() OVER (PARTITION BY item_id, city
                                        ORDER BY fetched_at DESC) rn
              FROM price_snapshots
              WHERE server=:srv AND quality=1 AND sell_price_min > 0
                AND item_id IN (SELECT item_id FROM wl)
                AND fetched_at BETWEEN :ini AND :fim)
            SELECT r.item_id, r.city, a.sell_price_min, r.sell_price_min
            FROM recent r JOIN antigo a
              ON a.item_id = r.item_id AND a.city = r.city
            WHERE r.rn = 1 AND a.rn = 1
        """, {"srv": config.DEFAULT_SERVER, "recente": now - 6 * 3600,
              "ini": now - 36 * 3600, "fim": now - 18 * 3600}).fetchall()
    finally:
        con.close()
    movers = []
    for item_id, city, old, new in rows:
        if old and new and old > 500:  # ignora micro-preços ruidosos
            movers.append({"item_id": item_id, "city": city, "old": old,
                           "new": new, "pct": round(100 * (new / old - 1), 1)})
    movers.sort(key=lambda m: -abs(m["pct"]))
    return movers[:limit * 2]


def cmd_report(args, fmt):
    """Relatório do dia: recomendações, backtest, movers e cobertura."""
    from app import recommendations
    db = ItemDB()
    # chamada direta à função do app: todos os defaults Query() precisam
    # ser passados explicitamente
    rec = recommendations(
        limit=args.top, premium=args.premium, max_age_buy=720,
        max_age_sell=720, min_daily_volume=20, min_active_days=2,
        history_days=7, capture_rate=config.CAPTURE_RATE)
    cov = rec.get("coverage", {})
    lines = ["**Mercado Albion — relatório** (Américas)"]
    lines.append(f"Cobertura: {cov.get('price_items', 0)} itens com preço · "
                 f"{cov.get('history_items', 0)} com histórico · "
                 f"watchlist coletada a cada "
                 f"{config.AUTO_COLLECT_INTERVAL_MIN} min")

    opps = rec.get("opportunities", [])[:args.top]
    if opps:
        lines.append("")
        lines.append(f"**Top {len(opps)} oportunidades agora** "
                     "(lucro líq./un · Pot./dia realista · confiança):")
        for o in opps:
            meta = db.get(o["item_id"]) or {}
            lines.append(
                f"- `{o['opportunity_score']:.0f}` {meta.get('pt', o['item_id'])} "
                f"T{o['tier']}.{o['ench']} — {city_pt(o['buy_city'])} → "
                f"{city_pt(o['sell_city'])}: {fmt_int(o['profit'])} prata "
                f"({o['roi_pct']:.0f}%) · {fmt_int(o.get('daily_realistic') or 0)}/dia"
                f" · conf. {o['confidence_label']}")
    else:
        lines.append("Sem oportunidades com os filtros padrão agora.")

    movers = _movers_24h(limit=5)
    if movers:
        lines.append("")
        lines.append("**Maiores variações (~24 h, venda mín. q1):**")
        for m in movers:
            meta = db.get(m["item_id"]) or {}
            seta = "▲" if m["pct"] > 0 else "▼"
            lines.append(f"- {seta} {m['pct']:+.1f}% {meta.get('pt', m['item_id'])}"
                         f" em {city_pt(m['city'])}: {fmt_int(m['old'])} → "
                         f"{fmt_int(m['new'])}")

    try:
        from albion import backtest as backtest_mod
        db_path = (DATA / "cache.db").resolve()
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            metas = {i["id"]: i for i in db.items}
            bt = backtest_mod.signal_backtest(con, config.DEFAULT_SERVER,
                                              metas, premium=args.premium)
        finally:
            con.close()
        ov = bt.get("overall", {})
        if ov.get("n"):
            lines.append("")
            lines.append(f"**Backtest** ({bt['run_pairs_used']} pares de coleta):"
                         f" acerto {ov['hit_rate_pct']}% · captura "
                         f"{ov['capture_pct']}% do lucro prometido")
    except sqlite3.Error:
        pass

    text = "\n".join(lines)
    print(text)
    if args.discord:
        url = config.DISCORD_WEBHOOK_URL
        if not url:
            die("DISCORD_WEBHOOK_URL vazio em albion/config.py — cole a URL "
                "do webhook do canal e tente de novo.")
        import httpx
        for i in range(0, len(text), 1900):
            r = httpx.post(url, json={"content": text[i:i + 1900]}, timeout=20)
            r.raise_for_status()
        info("Relatório publicado no Discord.")


REFINED_BY_FAMILY = {"WOOD": "PLANKS", "ORE": "METALBAR", "FIBER": "CLOTH",
                     "HIDE": "LEATHER", "ROCK": "STONEBLOCK",
                     "STONE": "STONEBLOCK"}


def cmd_refine(args, fmt):
    """Margem de refino por cidade, com receitas exatas do jogo.

    RRR = taxa de retorno de recursos (36,7% em cidade com bônus sem foco;
    53,9% com foco; ~15,2% fora de bônus). O retorno reduz o custo efetivo
    dos insumos: custo × (1 − RRR).
    """
    from albion.flips import age_minutes, sell_revenue
    fam = args.familia.upper()
    ref = REFINED_BY_FAMILY.get(fam)
    if not ref:
        die(f"Família desconhecida: {args.familia}. "
            f"Use: {', '.join(sorted(set(REFINED_BY_FAMILY)))}")
    e = args.ench
    refined_id = (f"T{args.tier}_{ref}_LEVEL{e}@{e}" if e
                  else f"T{args.tier}_{ref}")
    recipes_path = DATA / "recipes_refining.json"
    if not recipes_path.exists():
        die("Receitas não geradas — rode `python scripts/build_recipes.py`.")
    recipes = json.loads(recipes_path.read_text(encoding="utf-8"))
    recipe = recipes.get(refined_id)
    if not recipe:
        die(f"Sem receita para {refined_id} (T2 não refina; encanto só T4+).")
    db = ItemDB()
    meta = db.get(refined_id) or {}
    ids = [refined_id] + [i["id"] for i in recipe["inputs"]]
    aodp = make_aodp()
    price_rows = api_guard(lambda: aodp.get_prices(ids, config.ROYAL_CITIES
                                                   + ["Brecilien"],
                                                   max_age=args.max_age))
    by_city = {}
    for r in price_rows:
        if r["quality"] == 1:
            by_city.setdefault(r["city"], {})[r["item_id"]] = r
    rrr = args.rrr / 100
    rows = []
    for city, prices in by_city.items():
        out = prices.get(refined_id)
        if not out:
            continue
        cost = 0
        ok = True
        for inp in recipe["inputs"]:
            p = prices.get(inp["id"], {}).get("sell_price_min") or 0
            if p <= 0:
                ok = False
                break
            cost += inp["count"] * p
        if not ok:
            continue
        eff_cost = cost * (1 - rrr) + args.fee
        sell_ord = out.get("sell_price_min") or 0
        sell_inst = out.get("buy_price_max") or 0
        m_ord = (round(sell_revenue(sell_ord, "order", args.premium) - eff_cost)
                 if sell_ord > 0 else None)
        m_inst = (round(sell_revenue(sell_inst, "instant", args.premium)
                        - eff_cost) if sell_inst > 0 else None)
        rows.append({
            "cidade": city_pt(city),
            "custo_insumos": cost,
            "custo_efetivo": round(eff_cost),
            "refinado_ordem": sell_ord or None,
            "refinado_inst": sell_inst or None,
            "margem_ordem": m_ord,
            "margem_inst": m_inst,
            "margem_ordem_pct": (round(100 * m_ord / eff_cost, 1)
                                 if m_ord is not None and eff_cost else None),
            "idade_min": age_minutes(out.get("sell_price_min_date")),
            "_sort": m_ord if m_ord is not None else (m_inst or -9e9),
        })
    rows.sort(key=lambda r: -(r.pop("_sort") or 0))
    info(f"Refino de {meta.get('pt', refined_id)} ({refined_id}) — receita: "
         + " + ".join(f"{i['count']}x {(db.get(i['id']) or {}).get('pt', i['id'])}"
                      for i in recipe["inputs"])
         + f" · RRR {args.rrr}% · taxa de estação {args.fee} prata"
         f" · {'com' if args.premium else 'sem'} premium."
         " Compra de insumos e venda na MESMA cidade.")
    emit(rows, [("cidade", "Cidade"), ("custo_insumos", "Insumos"),
                ("custo_efetivo", "Custo efetivo"),
                ("refinado_ordem", "Venda (ordem)"),
                ("refinado_inst", "Venda (inst.)"),
                ("margem_ordem", "Margem ordem"),
                ("margem_inst", "Margem inst."),
                ("margem_ordem_pct", "Margem %"),
                ("idade_min", "Idade (min)")], fmt)


def cmd_craft(args, fmt):
    """Margem de craft com receitas e RRR reais do jogo (craft_data)."""
    from albion import craft
    db = ItemDB()
    it = resolve_item(db, " ".join(args.item))
    recipe = craft.recipe_for(it["id"])
    if not recipe:
        die(f"Sem receita de craft para {it['id']} (item não-craftável ou "
            "receita ausente no dump).")
    ids = [it["id"]] + [i["id"] for i in recipe["inputs"]]
    aodp = make_aodp()
    rows_raw = api_guard(lambda: aodp.get_prices(ids, config.ROYAL_CITIES,
                                                 max_age=args.max_age))
    # menor venda q1 por (item, cidade)
    price = {}
    for r in rows_raw:
        if r["quality"] != 1:
            continue
        sp = r["sell_price_min"] or 0
        if sp > 0:
            key = (r["item_id"], r["city"])
            if key not in price or sp < price[key]:
                price[key] = sp

    def price_of(item_id, city):
        return price.get((item_id, city))

    res = craft.margins(it["id"], recipe, price_of, premium=args.premium,
                        sell_mode=args.sell_mode, focus=args.focus, fee=args.fee)
    inputs_txt = " + ".join(
        f"{i['count']}x {(db.get(i['id']) or {}).get('pt', i['id'])}"
        for i in recipe["inputs"])
    bonus = craft.bonus_city(recipe.get("category"))
    info(f"Craft de {it['pt']} ({it['id']}) — receita: {inputs_txt} · foco "
         f"{recipe.get('focus')} · categoria {recipe.get('category')} "
         f"(cidade-bônus: {bonus or '—'}) · {'com' if args.premium else 'sem'} "
         f"premium · {'com' if args.focus else 'sem'} foco. Compra insumos e "
         "vende na MESMA cidade; custo já com retorno de recursos (RRR).")
    rows = [{
        "cidade": city_pt(r["city"]) + ("  ★" if r["is_bonus_city"] else ""),
        "insumos": r["materials"], "rrr_pct": r["rrr_pct"],
        "custo_efetivo": r["eff_cost"], "venda": r["sell"],
        "margem": r["margin"], "margem_pct": r["margin_pct"],
        "prata_foco": r["silver_per_focus"],
    } for r in res]
    emit(rows, [("cidade", "Cidade"), ("insumos", "Insumos"),
                ("rrr_pct", "RRR %"), ("custo_efetivo", "Custo efetivo"),
                ("venda", "Venda"), ("margem", "Margem"),
                ("margem_pct", "Margem %"), ("prata_foco", "Prata/foco")], fmt)


def _clean_mob(mob_id):
    """T5_MOB_DEMON_YELLOW_VETERAN_BOSS -> 'Demon Yellow Veteran Boss'."""
    s = mob_id or ""
    for pre in ("MOB_", ):
        i = s.find(pre)
        if i >= 0:
            s = s[i + len(pre):]
    return s.replace("_", " ").title()


def cmd_origin(args, fmt):
    """De onde o item nasce: mobs/conteúdo que o dropam (lado da oferta)."""
    db = ItemDB()
    it = resolve_item(db, " ".join(args.item))
    path = DATA / "supply_data.json"
    if not path.exists():
        die("Rode `python scripts/build_supply_data.py` primeiro.")
    supply = json.loads(path.read_text(encoding="utf-8"))
    srcs = supply.get(it["id"]) or supply.get(it["id"].split("@")[0]) or []
    if not srcs:
        info(f"{it['pt']} ({it['id']}) não tem fonte de drop conhecida — "
             "provavelmente é craftado/refinado, não dropado por mob.")
        return
    info(f"De onde nasce {it['pt']} ({it['id']}) — top {len(srcs)} fontes de "
         "drop por fama do mob (entrada via PvE no mercado):")
    rows = [{
        "mob": _clean_mob(s["mob"]),
        "tier": s["tier"],
        "tipo": s.get("cat") or "-",
        "fama": s["fame"],
    } for s in srcs]
    emit(rows, [("mob", "Mob/conteúdo"), ("tier", "Tier"),
                ("tipo", "Tipo"), ("fama", "Fama")], fmt)


def cmd_journals(args, fmt):
    """Margem de diários: comprar vazio, vender cheio (o 'salário' da fama).

    Não desconta a fama gasta para encher — é exatamente o que o crafter
    avalia: quanto o mercado paga pela fama dele em cada família/tier.
    """
    from albion.flips import age_minutes, sell_revenue
    db = ItemDB()
    empties = [i for i in db.items
               if "_JOURNAL_" in i["id"] and i["id"].endswith("_EMPTY")]
    if args.tier_min:
        empties = [i for i in empties if i["tier"] >= args.tier_min]
    if args.tier_max:
        empties = [i for i in empties if i["tier"] <= args.tier_max]
    if args.family:
        fam = args.family.upper()
        empties = [i for i in empties if f"_{fam}_" in i["id"] + "_"]
    if not empties:
        die("Nenhum diário corresponde aos filtros.")
    pairs = [(e, db.get(e["id"][:-6] + "_FULL")) for e in empties]
    pairs = [(e, f) for e, f in pairs if f]
    ids = [x["id"] for e, f in pairs for x in (e, f)]
    aodp = make_aodp()
    price_rows = api_guard(lambda: aodp.get_prices(ids, config.CITIES,
                                                   max_age=args.max_age))
    best = {}
    for r in price_rows:
        if r["quality"] != 1:
            continue
        d = best.setdefault(r["item_id"], {})
        sp, bp = r["sell_price_min"] or 0, r["buy_price_max"] or 0
        if sp > 0 and (not d.get("buy") or sp < d["buy"][0]):
            d["buy"] = (sp, r["city"], age_minutes(r["sell_price_min_date"]))
        if bp > 0 and (not d.get("sell_inst") or bp > d["sell_inst"][0]):
            d["sell_inst"] = (bp, r["city"], age_minutes(r["buy_price_max_date"]))
        if sp > 0 and (not d.get("sell_ord") or sp > d["sell_ord"][0]):
            d["sell_ord"] = (sp, r["city"], age_minutes(r["sell_price_min_date"]))
    rows = []
    for e, f in pairs:
        be, bf = best.get(e["id"], {}), best.get(f["id"], {})
        if "buy" not in be:
            continue
        cost = be["buy"][0]
        m_inst = (round(sell_revenue(bf["sell_inst"][0], "instant",
                                     args.premium) - cost)
                  if "sell_inst" in bf else None)
        m_ord = (round(sell_revenue(bf["sell_ord"][0], "order",
                                    args.premium) - cost)
                 if "sell_ord" in bf else None)
        rows.append({
            "diario": e["pt"].replace(" (Vazio)", ""),
            "tier": e["tier"],
            "vazio": cost,
            "cidade_compra": city_pt(be["buy"][1]),
            "cheio_inst": bf.get("sell_inst", (None,))[0],
            "cheio_ordem": bf.get("sell_ord", (None,))[0],
            "margem_inst": m_inst,
            "margem_ordem": m_ord,
            "cidade_venda": city_pt(bf.get("sell_ord", bf.get(
                "sell_inst", (0, "-")))[1]),
            "_sort": max(m_inst or -9e9, m_ord or -9e9),
        })
    rows.sort(key=lambda r: -r.pop("_sort"))
    info(f"{len(rows)} diários avaliados ({'com' if args.premium else 'sem'}"
         " premium). Margem não desconta a fama para encher. A margem 'inst.'"
         " (contra ordens de compra reais) é a executável; a coluna 'ordem'"
         " usa o menor anúncio atual — desconfie de valores absurdos.")
    emit(rows[:args.limit],
         [("diario", "Diário"), ("tier", "T"), ("vazio", "Vazio (compra)"),
          ("cidade_compra", "Onde comprar"), ("cheio_inst", "Cheio (inst.)"),
          ("cheio_ordem", "Cheio (ordem)"), ("margem_inst", "Margem inst."),
          ("margem_ordem", "Margem ordem"), ("cidade_venda", "Onde vender")],
         fmt)


def cmd_backtest(args, fmt):
    """Backtest de sinal: o lucro prometido se realizou na coleta seguinte?"""
    from albion import backtest as backtest_mod
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` algumas vezes antes.")
    db = ItemDB()
    metas = {i["id"]: i for i in db.items}
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        res = backtest_mod.signal_backtest(
            con, config.DEFAULT_SERVER, metas, premium=args.premium,
            min_profit=args.min_profit, max_runs=args.max_runs)
    finally:
        con.close()
    if fmt == "json":
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    ov = res["overall"]
    info(f"{res['run_pairs_used']} pares de coleta usados (de {res['runs_total']}"
         f" rodadas) · {res['no_counterparty']} oportunidades sem contraparte"
         " na coleta seguinte (excluídas das médias).")
    if not ov.get("n"):
        info("Ainda sem pares suficientes — deixe a coleta acumular rodadas.")
        return
    rows = [{"grupo": "GERAL", "faixa": "-", **ov}]
    for k, v in res["by_expected_roi"].items():
        if v.get("n"):
            rows.append({"grupo": "ROI esperado %", "faixa": k, **v})
    for k, v in res["by_route"].items():
        if v.get("n"):
            rows.append({"grupo": "rota", "faixa": k, **v})
    for k, v in res["by_sell_age_min"].items():
        if v.get("n"):
            rows.append({"grupo": "idade venda (min)", "faixa": k, **v})
    emit(rows, [("grupo", "Grupo"), ("faixa", "Faixa"), ("n", "N"),
                ("hit_rate_pct", "Acerto %"),
                ("expected_profit_avg", "Esperado médio"),
                ("realized_profit_avg", "Realizado médio"),
                ("capture_pct", "Captura %")], fmt)


def cmd_intel(args, fmt):
    """Inteligência de killboard: coleta pública e índice de destruição."""
    from albion import gameinfo
    if args.acao == "collect":
        aodp = make_aodp()
        gameinfo.ensure_assumptions(aodp)
        aodp.sync_static_items(ItemDB().items)
        gameinfo.seed_combat_tags(aodp)
        results = []
        if args.source in ("events", "all"):
            results.append(gameinfo.ingest_events(aodp, max_pages=args.pages))
        if args.source in ("battles", "all"):
            results.append(gameinfo.ingest_battles(aodp))
        n = gameinfo.aggregate_demand_daily(aodp)
        info(f"item_demand_daily reagregado: {n} linhas (últimos 3 dias).")
        emit(results, [("source", "Fonte"), ("pages", "Páginas"),
                       ("seen", "Vistos"), ("inserted", "Novos"),
                       ("ok", "OK"), ("error", "Erro")], fmt)
        return

    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `intel collect` antes.")
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        if args.acao == "status":
            st = gameinfo.intel_status(con, config.DEFAULT_SERVER)
            if fmt == "json":
                print(json.dumps(st, ensure_ascii=False, indent=2))
                return
            rows = [{"tabela": k, "valor": str(v)} for k, v in st.items()]
            emit(rows, [("tabela", "Tabela"), ("valor", "Valor")], fmt)
            return
        if args.acao == "risk":
            res = gameinfo.risk_summary(con, config.DEFAULT_SERVER,
                                        days=args.days)
            cls = gameinfo.classification_summary(
                con, config.DEFAULT_SERVER, days=args.days)
            if fmt == "json":
                print(json.dumps({**res, "classificacao": cls},
                                 ensure_ascii=False, indent=2))
                return
            info(f"Classificação estrutural de {cls['total_mortes']} mortes "
                 f"(janela {args.days:g}d): "
                 + " · ".join(f"{c['classe']} {c['pct']}%"
                              for c in cls["classes"])
                 + f" | vítimas coletoras: {cls['vitimas_coletoras']}"
                 f" · com montaria de carga: "
                 f"{cls['vitimas_montaria_transporte']}"
                 f" · com inventário 10+: "
                 f"{cls['vitimas_inventario_pesado']}")
            info("Mortes por área × hora UTC (Location da API vem nulo; "
                 "KillArea é o melhor proxy público hoje).")
            emit(res["por_area_hora"][:args.limit],
                 [("kill_area", "Área"), ("hora_utc", "Hora UTC"),
                  ("mortes", "Mortes"), ("fama", "Fama destruída")], fmt)
            if res["zvz_recentes"]:
                info("Batalhas grandes recentes (proxy de ZvZ, >=15 kills):")
                emit(res["zvz_recentes"],
                     [("inicio", "Início"), ("zona", "Zona"),
                      ("kills", "Kills"), ("jogadores", "Jogadores"),
                      ("guildas", "Guildas"), ("fama", "Fama")], fmt)
            return
        if args.acao == "validate":
            out = [gameinfo.validate_signals(con, config.DEFAULT_SERVER,
                                             horizon_days=h)
                   for h in (1, 3)]
            info("Backtest dos alertas de divergência: retorno do preço após"
                 " o sinal (precisa de sinais logados há 1+/3+ dias — o"
                 " servidor loga 1x/hora).")
            emit(out, [("horizon_days", "Horizonte (dias)"), ("n", "N"),
                       ("hit_rate_pct", "Acerto %"),
                       ("retorno_medio_pct", "Retorno médio %"),
                       ("retorno_mediano_pct", "Mediano %"),
                       ("pior_pct", "Pior %"), ("melhor_pct", "Melhor %")],
                 fmt)
            return
        if args.acao == "signals":
            sigs = gameinfo.demand_price_divergence(
                con, config.DEFAULT_SERVER, limit=args.limit)
            db = ItemDB()
            for s in sigs:
                s["item"] = (db.get(s["item_id"]) or {}).get("pt", s["item_id"])
            info("Divergência demanda × preço: destruição subindo com preço"
                 " ainda atrasado (precisa de alguns dias de coleta de kills"
                 " + histórico de preço dos itens).")
            emit(sigs, [("item", "Item"),
                        ("demanda_dia_recente", "Dem./dia (recente)"),
                        ("demanda_dia_base", "Dem./dia (base)"),
                        ("demanda_ratio", "Δ demanda"),
                        ("preco_ratio", "Δ preço"),
                        ("volume_dia", "Vol. mercado/dia"),
                        ("nota", "Nota")], fmt)
            return
        # top: índice de destruição cruzado com nomes/preços locais
        top = gameinfo.destruction_top(
            con, config.DEFAULT_SERVER, days=args.days, role=args.role,
            include_inventory=args.inventory, limit=args.limit)
    finally:
        con.close()
    db = ItemDB()
    rows = []
    for t in top:
        meta = db.get(t["item_id"]) or {}
        rows.append({
            "item": meta.get("pt", t["item_id"]),
            "tier_ench": te(meta) if meta else "-",
            "unidades": t["unidades"],
            "eventos": t["eventos"],
            "preco_ref": t["preco_ref"],
            "valor_estimado": t["valor_estimado"],
            "valor_trash_estimado": t.get("valor_trash_estimado"),
        })
    papel = {"victim": "perdidos pelas vítimas",
             "killer": "usados pelos killers"}.get(args.role, args.role)
    info(f"Itens mais {papel} nas últimas {args.days:g} dia(s)"
         f"{' (incluindo inventário)' if args.inventory else ''}. Valor = "
         "unidades × menor venda q1 nas cidades reais (sem ajuste de trash).")
    emit(rows, [("item", "Item"), ("tier_ench", "T.E"),
                ("unidades", "Unidades"), ("eventos", "Eventos"),
                ("preco_ref", "Preço ref."),
                ("valor_estimado", "Valor estimado"),
                ("valor_trash_estimado", "Pós-trash")], fmt)


def cmd_pos(args, fmt):
    """Portfolio: registra operações reais e acompanha o PnL (imposto 4%)."""
    from albion.flips import sell_revenue
    db = ItemDB()
    aodp = make_aodp()
    if args.acao == "add":
        it = resolve_item(db, args.item)
        city = parse_cities(args.city)[0] if args.city else None
        pid = aodp.pos_add(it["id"], args.qty, args.price, buy_city=city,
                           quality=args.quality, note=args.note)
        info(f"Posição #{pid}: {args.qty}x {it['pt']} @ {fmt_int(args.price)}.")
        return
    if args.acao == "sell":
        if not args.pos_id or args.price is None:
            die("Use: pos sell --id N --price P [--city C]")
        city = parse_cities(args.city)[0] if args.city else None
        if not aodp.pos_close(args.pos_id, args.price, sell_city=city):
            die(f"Posição #{args.pos_id} não encontrada ou já fechada.")
        info(f"Posição #{args.pos_id} fechada @ {fmt_int(args.price)}.")
        return
    if args.acao == "rm":
        if not args.pos_id:
            die("Use: pos rm --id N")
        if not aodp.pos_delete(args.pos_id):
            die(f"Posição #{args.pos_id} não encontrada.")
        info(f"Posição #{args.pos_id} removida.")
        return

    # list
    positions = aodp.pos_list(include_closed=args.all)
    if not positions:
        info("Nenhuma posição registrada. Use `pos add`.")
        return
    open_ids = sorted({p["item_id"] for p in positions if not p["closed_at"]})
    cur_best = {}
    if open_ids:
        price_rows = api_guard(lambda: aodp.get_prices(open_ids, config.CITIES))
        for r in price_rows:
            bp = r["buy_price_max"] or 0
            key = (r["item_id"], r["quality"])
            if bp > 0 and bp > cur_best.get(key, (0,))[0]:
                cur_best[key] = (bp, r["city"])
    rows, pnl_aberto, pnl_fechado = [], 0.0, 0.0
    for p in positions:
        meta = db.get(p["item_id"]) or {}
        if p["closed_at"]:
            pnl = p["qty"] * (sell_revenue(p["sell_price"], "instant", True)
                              - p["buy_price"])
            pnl_fechado += pnl
            estado, atual = "fechada", p["sell_price"]
        else:
            best = cur_best.get((p["item_id"], p["quality"]))
            atual = best[0] if best else None
            pnl = (p["qty"] * (sell_revenue(atual, "instant", True)
                               - p["buy_price"]) if atual else None)
            if pnl is not None:
                pnl_aberto += pnl
            estado = "aberta"
        rows.append({
            "id": p["id"], "estado": estado,
            "item": meta.get("pt", p["item_id"]), "q": p["quality"],
            "qtd": p["qty"], "compra": p["buy_price"],
            "atual_venda": atual, "pnl": round(pnl) if pnl is not None else None,
            "nota": p["note"] or "-",
        })
    info(f"PnL aberto (marcado a mercado, venda instantânea, premium): "
         f"{fmt_int(pnl_aberto)} · PnL realizado: {fmt_int(pnl_fechado)}")
    emit(rows, [("id", "#"), ("estado", "Estado"), ("item", "Item"),
                ("q", "Q"), ("qtd", "Qtd"), ("compra", "Compra"),
                ("atual_venda", "Venda atual"), ("pnl", "PnL líq."),
                ("nota", "Nota")], fmt)


def cmd_indexes(args, fmt):
    """Índice de preço (base 100) ponderado por volume para uma cesta."""
    db = ItemDB()
    basket = [i["id"] for i in db.filter(cat=args.cat, sub=args.sub,
                                         tier_min=args.tier_min,
                                         tier_max=args.tier_max)]
    if not basket:
        die("Cesta vazia — confira --cat/--sub.")
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado.")
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        ph = ",".join("?" * len(basket))
        rows = con.execute(f"""
            SELECT substr(ts,1,10) AS dia,
                   SUM(item_count * avg_price) * 1.0 / SUM(item_count) AS vwap,
                   SUM(item_count) AS volume
            FROM history
            WHERE server=? AND time_scale=24 AND quality=1
              AND item_count > 0 AND avg_price > 0
              AND ts >= date('now', ?)
              AND item_id IN ({ph})
            GROUP BY dia ORDER BY dia
        """, [config.DEFAULT_SERVER, f"-{args.days} days", *basket]).fetchall()
    finally:
        con.close()
    if not rows:
        die("Sem histórico para a cesta — rode `collect` antes.")
    base = rows[0][1]
    out = [{"dia": d, "indice": round(100 * v / base, 2),
            "vwap": round(v, 1), "volume": vol}
           for d, v, vol in rows]
    info(f"Cesta: {len(basket)} itens ({args.cat or 'todas'}/"
         f"{args.sub or 'todas'}) · base 100 = {out[0]['dia']}"
         " · VWAP diário q1, ponderado por volume.")
    emit(out, [("dia", "Dia"), ("indice", "Índice"), ("vwap", "VWAP"),
               ("volume", "Volume")], fmt)


def cmd_micro(args, fmt):
    """Microestrutura: market-making intra-cidade, livro e armadilhas."""
    from albion import microstructure as mc
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` antes.")
    premium = not args.no_premium
    cities = parse_cities(args.cities)
    quals = parse_qualities(args.qualities)
    item_ids = None
    db = ItemDB()
    if args.itens:
        item_ids = [i["id"] for i in resolve_items(db, " ".join(args.itens))]
    name = lambda iid: (db.get(iid) or {}).get("pt", iid)

    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        if args.acao == "spread":
            res = mc.market_making_menu(
                con, config.DEFAULT_SERVER, premium=premium,
                max_age_min=args.max_age, cities=cities, item_ids=item_ids,
                qualities=quals, min_net=args.min_net,
                min_liquidity=args.min_volume, limit=args.limit)
            for r in res:
                r["item"] = name(r["item_id"]); r["city_pt"] = city_pt(r["city"])
            info(f"Market-making intra-cidade (premium={'sim' if premium else 'não'}"
                 "): posta ordem de compra E de venda no MESMO mercado. net já "
                 "desconta imposto + 2,5% de anúncio nas duas pontas. Ordenado "
                 "por potencial/dia (net × giro × 20%).")
            emit(res, [("item", "Item"), ("city_pt", "Cidade"),
                       ("quality", "Q"), ("buy_price_max", "Compra"),
                       ("sell_price_min", "Venda"), ("net_per_unit", "Net/un"),
                       ("net_pct", "Net %"), ("liquidity_day", "Giro/dia"),
                       ("potential_day", "Pot./dia"), ("age_min", "Idade")], fmt)
        elif args.acao == "book":
            res = mc.book_metrics(
                con, config.DEFAULT_SERVER, cities=cities, item_ids=item_ids,
                qualities=quals, max_age_min=args.max_age, limit=args.limit)
            for r in res:
                r["item"] = name(r["item_id"]); r["city_pt"] = city_pt(r["city"])
            info("Largura do topo do livro (PROXY de dispersão, não quantidade "
                 "real): quanto mais larga, mais fácil o preço anda com volume.")
            emit(res, [("item", "Item"), ("city_pt", "Cidade"), ("quality", "Q"),
                       ("sell_min", "Venda mín"), ("sell_max", "Venda máx"),
                       ("sell_width_pct", "Larg.venda %"),
                       ("buy_width_pct", "Larg.compra %"),
                       ("age_min", "Idade")], fmt)
        elif args.acao == "traps":
            res = mc.trap_signals(
                con, config.DEFAULT_SERVER, cities=cities, item_ids=item_ids,
                qualities=quals, max_age_min=args.max_age, limit=args.limit)
            for r in res:
                r["item"] = name(r["item_id"]); r["city_pt"] = city_pt(r["city"])
                r["flags_pt"] = ", ".join(r["flags"])
            info("Ordens suspeitas: 'cruzado' = venda <= compra (livro impossível"
                 "); 'outlier' = preço fora da curva entre cidades; 'novo' = ordem "
                 "do topo não estava na coleta anterior (não-confirmada).")
            emit(res, [("item", "Item"), ("city_pt", "Cidade"), ("quality", "Q"),
                       ("sell_min", "Venda mín"), ("buy_max", "Compra máx"),
                       ("sell_z", "z venda"), ("flags_pt", "Sinais"),
                       ("persist", "Persist."), ("age_min", "Idade")], fmt)
        elif args.acao == "capital":
            # C7: taxa de preenchimento real (survival) escala o giro capturável
            from albion import survival as _surv
            try:
                sv = _surv.persistence(con, config.DEFAULT_SERVER)
                rates = [b["rate_pct"] for b in sv["sides"]["sell"]["buckets"]
                         if b.get("rate_pct") is not None and b["pairs"] >= 3]
                fill = (sum(rates) / len(rates) / 100) if rates else None
            except Exception:
                fill = None
            res = mc.capital_allocation(
                con, config.DEFAULT_SERVER, capital=args.capital,
                premium=premium, max_age_min=args.max_age, cities=cities,
                min_liquidity=max(1, args.min_volume), fill_rate=fill,
                limit=args.limit)
            if fmt == "json":
                print(json.dumps(res, ensure_ascii=False, indent=2)); return
            for p in res["plan"]:
                p["item"] = name(p["item_id"]); p["city_pt"] = city_pt(p["city"])
            info(f"Alocação de {res['capital']:,} de prata por velocidade "
                 f"(lucro/dia por prata investida). Usado: {res['capital_used']:,}"
                 f" · lucro/dia estimado: {res['profit_day_total']:,}"
                 + (f" · próximo de fora rende {res['marginal_yield_pct']}%/dia"
                    if res.get('marginal_yield_pct') else "")
                 + (f" · taxa de preenchimento (survival): {round(fill*100)}%."
                    if fill else " · giro a 100% (survival sem dados — teto)."))
            emit(res["plan"], [("item", "Item"), ("city_pt", "Cidade"),
                               ("yield_day_pct", "Rend/dia %"),
                               ("net_per_unit", "Net/un"),
                               ("units", "Unid."), ("alloc_capital", "Capital"),
                               ("profit_day", "Lucro/dia")], fmt)
        else:  # hours
            if not item_ids:
                die("micro hours exige --itens <item> e --cities <cidade>.")
            if not cities:
                die("micro hours exige --cities <cidade>.")
            res = mc.hourly_spread(
                con, config.DEFAULT_SERVER, item_ids[0], cities[0],
                quality=(quals[0] if quals else 1), premium=premium,
                days=args.days)
            info(f"Spread líquido por hora UTC de {name(item_ids[0])} em "
                 f"{city_pt(cities[0])} (price_snapshots, {args.days}d). "
                 "Poucas amostras/hora = padrão de coleta, não de mercado.")
            emit(res, [("hour_utc", "Hora UTC"), ("net_median", "Net mediano"),
                       ("samples", "Amostras")], fmt)
    finally:
        con.close()


def _load_cache_prices(con, server, qualities=(1,)):
    """Menor venda por (item, cidade, qualidade) da tabela prices (cache)."""
    ph = ",".join("?" * len(qualities))
    rows = con.execute(
        f"""SELECT item_id, city, quality, sell_price_min FROM prices
            WHERE server=? AND sell_price_min>0 AND quality IN ({ph})""",
        [server, *qualities]).fetchall()
    allq, q1 = {}, {}
    for item, city, q, sp in rows:
        key = (item, city, q)
        if key not in allq or sp < allq[key]:
            allq[key] = sp
        if q == 1 and ((item, city) not in q1 or sp < q1[(item, city)]):
            q1[(item, city)] = sp
    return q1, allq


_PRICE_COLS = ("item_id", "city", "quality", "sell_price_min",
               "sell_price_min_date", "sell_price_max", "sell_price_max_date",
               "buy_price_min", "buy_price_min_date", "buy_price_max",
               "buy_price_max_date", "fetched_at")


def _cache_price_dicts(con, server, cities=None, item_ids=None):
    """Linhas da tabela prices (cache) no formato dos rows da API."""
    where = ["server=?"]
    params = [server]
    if cities:
        where.append(f"city IN ({','.join('?' * len(cities))})")
        params += list(cities)
    if item_ids:
        where.append(f"item_id IN ({','.join('?' * len(item_ids))})")
        params += list(item_ids)
    rows = con.execute(
        f"SELECT {','.join(_PRICE_COLS)} FROM prices WHERE {' AND '.join(where)}",
        params).fetchall()
    return [dict(zip(_PRICE_COLS, r)) for r in rows]


def _meta_for(db, price_rows):
    ids = {r["item_id"] for r in price_rows}
    return {iid: m for iid in ids if (m := db.get(iid))}


def cmd_logi(args, fmt):
    """Arbitragem & logística: carga, reposição, escada de qualidade, BM."""
    from albion import logistics as logi
    from albion.microstructure import clean_price_rows
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` antes.")
    premium = not args.no_premium
    db = ItemDB()
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        if args.acao == "cargo":
            bcity = parse_cities(args.buy_city)
            scity = parse_cities(args.sell_city)
            if not bcity or not scity:
                die("cargo exige --buy-city e --sell-city.")
            bcity, scity = bcity[0], scity[0]
            prows = clean_price_rows(_cache_price_dicts(con, config.DEFAULT_SERVER, [bcity, scity]))
            metas = _meta_for(db, prows)
            opps = compute_flips(prows, metas, premium=premium,
                                 buy_mode=args.buy_mode, sell_mode=args.sell_mode,
                                 buy_cities={bcity}, sell_cities={scity},
                                 qualities=[1])
            vol = {}
            for iid, n in con.execute(
                    """SELECT item_id, SUM(item_count)*1.0/COUNT(DISTINCT substr(ts,1,10))
                       FROM history WHERE server=? AND time_scale=24 AND quality=1
                         AND city=? AND item_count>0 AND ts>=date('now','-7 days')
                       GROUP BY item_id""",
                    [config.DEFAULT_SERVER, scity]).fetchall():
                vol[iid] = n
            res = logi.cargo_knapsack(opps, lambda i: vol.get(i, 0),
                                      w_max=args.kg, limit=args.limit)
            if fmt == "json":
                print(json.dumps(res, ensure_ascii=False, indent=2)); return
            info(f"Carga ótima {city_pt(bcity)}→{city_pt(scity)} em {res['w_max']}kg"
                 f": lucro/viagem {res['trip_profit']:,} · usado {res['used_kg']}kg"
                 + (f" · preço-sombra do kg: {res['shadow_price_per_kg']}"
                    if res.get('shadow_price_per_kg') else "")
                 + ". Teto por item = volume×20%.")
            rows = [{"item": b["name_pt"], "unid": b["units"], "kg": b["kg"],
                     "lucro": b["profit"], "lucro_kg": b["profit_per_kg"]}
                    for b in res["basket"]]
            emit(rows, [("item", "Item"), ("unid", "Unid."), ("kg", "Peso"),
                        ("lucro", "Lucro"), ("lucro_kg", "Lucro/kg")], fmt)
        elif args.acao == "ladder":
            cities = parse_cities(args.cities)
            prows = clean_price_rows(_cache_price_dicts(con, config.DEFAULT_SERVER, cities))
            res = logi.quality_ladder(prows, _meta_for(db, prows),
                                      premium=premium, sell_mode=args.sell_mode,
                                      min_premium_pct=args.min_premium,
                                      limit=args.limit)
            for r in res:
                r["city_pt"] = city_pt(r["city"])
                r["quals"] = ",".join(map(str, r["qualities"]))
            info("Prêmio por degrau de qualidade na MESMA cidade (maior salto). "
                 "Compre a qualidade barata que ainda satisfaz a ordem-alvo.")
            emit(res, [("name_pt", "Item"), ("city_pt", "Cidade"),
                       ("quals", "Q cotadas"), ("best_step", "Maior salto"),
                       ("best_premium_pct", "Prêmio %"),
                       ("best_premium_abs", "Prêmio prata")], fmt)
        elif args.acao == "bm":
            prows = clean_price_rows(_cache_price_dicts(con, config.DEFAULT_SERVER))
            res = logi.black_market_premium(prows, _meta_for(db, prows),
                                            premium=premium, limit=args.limit)
            for r in res:
                r["best_city_pt"] = city_pt(r["best_city"])
            info("Prêmio do Mercado Negro sobre a melhor venda nas cidades reais "
                 "(venda instantânea na ordem do sistema, sem taxa de anúncio). "
                 "Só equipamento de combate.")
            emit(res, [("name_pt", "Item"), ("quality", "Q"),
                       ("best_city_pt", "Melhor real"),
                       ("best_city_net", "Real líq"), ("bm_net", "BM líq"),
                       ("premium_abs", "Prêmio"), ("premium_pct", "Prêmio %")], fmt)
        else:  # restock
            demand = con.execute(
                """SELECT item_id, SUM(victim_units) AS u FROM item_demand_daily
                   WHERE server=? AND day >= date('now', ?)
                   GROUP BY item_id HAVING u>0 ORDER BY u DESC LIMIT 400""",
                [config.DEFAULT_SERVER, f"-{args.days} days"]).fetchall()
            if not demand:
                die("Sem demanda no killboard — rode `intel collect` antes.")
            prows = clean_price_rows(_cache_price_dicts(con, config.DEFAULT_SERVER))
            res = logi.restock_map(demand, prows, _meta_for(db, prows),
                                   premium=premium, limit=args.limit)
            for r in res:
                r["buy_pt"] = city_pt(r["buy_city"])
                r["sell_pt"] = city_pt(r["sell_city"])
            info(f"Mapa de reposição ({args.days}d): o servidor está perdendo "
                 "estes itens (killboard) — onde comprar barato e vender. "
                 "Ordenado por demanda × lucro/kg.")
            emit(res, [("name_pt", "Item"), ("demand_units", "Perdidos"),
                       ("buy_pt", "Comprar em"), ("buy_price", "Custo"),
                       ("sell_pt", "Vender em"), ("sell_net", "Venda líq"),
                       ("profit", "Lucro/un"), ("profit_per_kg", "Lucro/kg")], fmt)
    finally:
        con.close()


def _history_daily(con, server, item_ids=None, cities=None, days=180,
                   with_count=False):
    """Linhas (item, city, dia, avg_price[, item_count]) do history q1 no cache."""
    where = ["server=?", "time_scale=24", "quality=1", "avg_price>0",
             "ts >= date('now', ?)"]
    params = [server, f"-{int(days)} days"]
    if item_ids:
        where.append(f"item_id IN ({','.join('?' * len(item_ids))})")
        params += list(item_ids)
    if cities:
        where.append(f"city IN ({','.join('?' * len(cities))})")
        params += list(cities)
    cols = "item_id, city, substr(ts,1,10) AS day, avg_price"
    if with_count:
        cols += ", item_count"
    return con.execute(
        f"""SELECT {cols} FROM history WHERE {' AND '.join(where)}
            ORDER BY item_id, city, day""", params).fetchall()


def cmd_fc(args, fmt):
    """Previsão & econometria: reversão à média, par trading, previsibilidade."""
    from albion import forecast as fc
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` antes.")
    db = ItemDB()
    name = lambda iid: (db.get(iid) or {}).get("pt", iid)
    cities = parse_cities(args.cities)
    item_ids = None
    if args.itens:
        item_ids = [i["id"] for i in resolve_items(db, " ".join(args.itens))]
    elif args.cat or args.sub or args.tier_min or args.tier_max:
        item_ids = [i["id"] for i in db.filter(cat=args.cat, sub=args.sub,
                    tier_min=args.tier_min, tier_max=args.tier_max)]
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        rows = _history_daily(con, config.DEFAULT_SERVER, item_ids, cities,
                              days=args.days, with_count=True)
    finally:
        con.close()
    if not rows:
        die("Sem histórico no cache para o filtro — rode `collect`/`history`.")

    if args.acao == "pair":
        if not item_ids or len(item_ids) != 1:
            die("pair exige exatamente 1 item (compara entre cidades).")
        by_city = {}
        for item, city, day, price, _c in rows:
            by_city.setdefault(city, {})[day] = price
        out = []
        cs = sorted(by_city)
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                ca, cb = cs[i], cs[j]
                common = sorted(set(by_city[ca]) & set(by_city[cb]))
                if len(common) < args.min_points:
                    continue
                pa = [by_city[ca][d] for d in common]
                pb = [by_city[cb][d] for d in common]
                r = fc.pair_trade(pa, pb, min_points=args.min_points)
                if r:
                    r["pair"] = f"{city_pt(ca)} × {city_pt(cb)}"
                    out.append(r)
        out.sort(key=lambda r: (-(1 if r["signal"] else 0), -abs(r["z_spread"])))
        info(f"Par trading de {name(item_ids[0])} entre cidades: spread cointegrado "
             "e desviado. 'sinal' = z forte + meia-vida curta + cobre as taxas.")
        emit(out[:args.limit], [("pair", "Par"), ("z_spread", "z spread"),
             ("halflife_days", "Meia-vida"), ("gross_gap_pct", "Desvio %"),
             ("action", "Ação"), ("signal", "Sinal")], fmt)
        return

    if args.acao == "regime":
        # quebra estrutural por item×cidade. O teste de permutação é caro
        # (O(N²)×perms), então: item específico -> rigor cheio; varredura ->
        # só as séries mais longas (mais informativas) e menos permutações.
        series = {}
        for item, city, _day, price, _c in rows:
            series.setdefault((item, city), []).append(price)
        targeted = bool(item_ids and len(item_ids) <= 3)
        perms = 150 if targeted else 80
        ranked = sorted(series.items(), key=lambda kv: -len(kv[1]))
        if not targeted:
            ranked = ranked[:60]            # teto de séries na varredura
            info(f"(varredura: {len(ranked)} séries mais longas, {perms} "
                 "permutações; passe um item p/ rigor cheio)")
        out = []
        for (item, city), prices in ranked:
            br = fc.structural_break(prices, min_seg=max(6, args.min_points // 2),
                                     permutations=perms)
            if br and br["significant"]:
                out.append({"item": name(item), "city_pt": city_pt(city), **br})
        out.sort(key=lambda r: (r["p_value"], -abs(r["level_change_pct"] or 0)))
        info(f"Quebras de regime ({args.days}d, teste de permutação): a janela "
             "PÓS-quebra é a única confiável p/ reversão/VWAP/previsibilidade.")
        emit(out[:args.limit], [("item", "Item"), ("city_pt", "Cidade"),
             ("points", "Dias"), ("kind", "Tipo"), ("break_frac", "Quebra em"),
             ("mean_before", "Média antes"), ("mean_after", "Média depois"),
             ("level_change_pct", "Δ nível %"), ("valid_points", "Dias válidos"),
             ("p_value", "p")], fmt)
        return

    series, counts = {}, {}
    for item, city, day, price, c in rows:
        series.setdefault((item, city), []).append(price)
        counts.setdefault((item, city), []).append(c or 0)
    out = []
    if args.acao == "revert":
        for (item, city), prices in series.items():
            r = fc.mean_reversion(prices, min_points=args.min_points)
            if r and (not args.signals or r["signal"]):
                out.append({"item": name(item), "city_pt": city_pt(city), **r})
        out.sort(key=lambda r: (-(1 if r["signal"] else 0), -abs(r["z_resid"])))
        info(f"Reversão à média ({args.days}d): alvo de equilíbrio + meia-vida. "
             "'sinal' = preço esticado (|z|>=1,5) e meia-vida <= 7 dias.")
        emit(out[:args.limit], [("item", "Item"), ("city_pt", "Cidade"),
             ("current", "Atual"), ("target", "Alvo"), ("gap_pct", "Gap %"),
             ("z_resid", "z"), ("halflife_days", "Meia-vida"),
             ("direction", "Direção"), ("signal", "Sinal")], fmt)
    else:  # predict
        for (item, city), prices in series.items():
            r = fc.predictability(prices, counts.get((item, city)),
                                  min_points=args.min_points)
            if r:
                out.append({"item": name(item), "city_pt": city_pt(city), **r})
        out.sort(key=lambda r: -r["predictability"])
        info(f"Score de previsibilidade ({args.days}d): autocorrelação + R² da "
             "tendência + estabilidade de volume. Porteiro dos outros sinais.")
        emit(out[:args.limit], [("item", "Item"), ("city_pt", "Cidade"),
             ("points", "Dias"), ("autocorr_lag1", "Autocorr"),
             ("trend_r2", "R² tend."), ("vol_stability", "Estab.vol"),
             ("predictability", "Score"), ("label", "Rótulo")], fmt)


def _market_volume(con, server, days=7):
    """Volume diário médio de mercado por item (history q1, escala 24h)."""
    return {iid: n for iid, n in con.execute(
        """SELECT item_id, SUM(item_count)*1.0/COUNT(DISTINCT substr(ts,1,10))
           FROM history WHERE server=? AND time_scale=24 AND quality=1
             AND item_count>0 AND ts>=date('now', ?) GROUP BY item_id""",
        [server, f"-{int(days)} days"]).fetchall()}


def cmd_demand(args, fmt):
    """Inteligência de demanda (killboard): consumíveis, qualidade, meta."""
    from albion import demand as dm
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado.")
    db = ItemDB()
    name = lambda iid: (db.get(iid) or {}).get("pt", iid)
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        if args.acao == "burn":
            q1, _ = _clean_price_lookups(con, config.DEFAULT_SERVER)
            price_item = {}
            for (item, _c), p in q1.items():
                if item not in price_item or p < price_item[item]:
                    price_item[item] = p
            vol = _market_volume(con, config.DEFAULT_SERVER, days=args.days)
            res = dm.consumable_burn(
                con, config.DEFAULT_SERVER, days=args.days,
                price_of=price_item.get, vol_of=vol.get, limit=args.limit)
            for r in res:
                r["item"] = name(r["item_id"])
                r["cob"] = "SUBABASTECIDO" if r["undersupplied"] else ""
            info(f"Queima de consumíveis ({args.days}d): unidades destruídas/dia "
                 "no killboard vs oferta de mercado. cobertura<1 = subabastecido.")
            emit(res, [("item", "Item"), ("per_day", "Queima/dia"),
                       ("market_vol_day", "Oferta/dia"), ("coverage", "Cobertura"),
                       ("silver_per_day", "Prata/dia"), ("cob", "")], fmt)
        elif args.acao == "quality":
            _, allq = _clean_price_lookups(con, config.DEFAULT_SERVER)
            pq = {}
            for (item, _c, q), p in allq.items():
                key = (item, q)
                if key not in pq or p < pq[key]:
                    pq[key] = p
            res = dm.destroyed_quality(
                con, config.DEFAULT_SERVER, days=args.days,
                price_q=lambda i, q: pq.get((i, q)), limit=args.limit)
            for r in res:
                r["item"] = name(r["item_id"])
            info(f"Qualidade do gear destruído ({args.days}d) × prêmio de qualidade. "
                 "E[prêmio] alto = vale craftar/estocar qualidade alta, não q1.")
            emit(res, [("item", "Item"), ("destroyed", "Destruídos"),
                       ("share_q4plus_pct", "Q4+ %"), ("dominant_q", "Q dom."),
                       ("ev_quality_premium", "E[prêmio]")], fmt)
        else:  # meta
            res = dm.meta_shift(con, config.DEFAULT_SERVER, days=args.days,
                                recent=args.recent, limit=args.limit)
            info(f"Mudança de meta (recente {args.recent}d vs {args.days}d): builds "
                 "arma+armadura ganhando participação nas mortes (Δshare).")
            emit(res, [("build", "Build (arma+armadura)"), ("recent_n", "N recente"),
                       ("share_recent_pct", "Share rec.%"),
                       ("share_base_pct", "Share base%"), ("delta_pct", "Δ %")], fmt)
    finally:
        con.close()


def cmd_guild(args, fmt):
    """Guild & estratégico: ROI de coleta, cesta de regear, make-or-buy."""
    from albion import guild as gd
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado.")
    db = ItemDB()
    name = lambda iid: (db.get(iid) or {}).get("pt", iid)
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        if args.acao == "watch":
            q1, _ = _clean_price_lookups(con, config.DEFAULT_SERVER)
            price_item = {}
            for (item, _c), p in q1.items():
                if item not in price_item or p < price_item[item]:
                    price_item[item] = p
            res = gd.watchlist_roi(con, config.DEFAULT_SERVER,
                                   price_of=price_item.get, days=args.days,
                                   limit=args.limit)
            for r in res["add"]:
                r["item"] = name(r["item_id"])
            info(f"Prioridade de coleta ({args.days}d): itens MUITO destruídos que "
                 f"NÃO estão na watchlist ({res['watched_count']} já vigiados). "
                 "Adicione com `watch add`.")
            emit(res["add"], [("item", "Item (ADD à watchlist)"),
                 ("destroyed", "Destruídos"), ("active_days", "Dias ativos"),
                 ("price", "Preço"), ("score", "Score")], fmt)
        elif args.acao == "kit":
            res = gd.soldier_kit_index(con, config.DEFAULT_SERVER, days=args.days,
                                       hist_days=args.hist_days)
            if fmt == "json":
                print(json.dumps(res, ensure_ascii=False, indent=2)); return
            if not res["series"]:
                die("Sem história suficiente p/ a cesta — colete os itens de regear.")
            info("Índice Soldier's Kit (base 100): custo de re-equipar a guild, "
                 "cesta ponderada pelo uso real no killboard. Top da cesta: "
                 + ", ".join(name(i) for i, _ in res["basket"][:5]))
            emit(res["series"], [("day", "Dia"), ("cost", "Custo cesta"),
                                 ("index", "Índice")], fmt)
        else:  # makeorbuy
            q1, _ = _clean_price_lookups(con, config.DEFAULT_SERVER)
            res = gd.make_or_buy(con, config.DEFAULT_SERVER,
                                 price_of=lambda i, c: q1.get((i, c)),
                                 days=args.days, premium=not args.no_premium,
                                 focus=args.focus, limit=args.limit)
            for r in res:
                r["item"] = name(r["item_id"])
                r["city_pt"] = city_pt(r["internal_city"])
            info(f"Make-or-buy ({args.days}d): itens que a guild consome — custo de "
                 "fazer (interno) vs comprar (mercado). save>0 = fazer compensa.")
            emit(res, [("item", "Item"), ("demand_units", "Demanda"),
                       ("internal_cost", "Fazer"), ("city_pt", "Cidade"),
                       ("market_price", "Comprar"), ("save_pct", "Economia %"),
                       ("verdict", "Veredito")], fmt)
    finally:
        con.close()


def cmd_risk(args, fmt):
    """Risco & portfólio: perfil de risco, sizing, correlação."""
    from albion import risk
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` antes.")
    db = ItemDB()
    name = lambda iid: (db.get(iid) or {}).get("pt", iid)
    cities = parse_cities(args.cities)
    item_ids = None
    if args.itens:
        item_ids = [i["id"] for i in resolve_items(db, " ".join(args.itens))]
    elif args.cat or args.sub or args.tier_min or args.tier_max:
        item_ids = [i["id"] for i in db.filter(cat=args.cat, sub=args.sub,
                    tier_min=args.tier_min, tier_max=args.tier_max)]
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        rows = _history_daily(con, config.DEFAULT_SERVER, item_ids, cities,
                              days=args.days)
    finally:
        con.close()
    if not rows:
        die("Sem histórico no cache para o filtro — rode `collect`/`history`.")

    if args.acao == "profile":
        series = {}
        for item, city, _day, price in rows:
            series.setdefault((item, city), []).append(price)
        out = []
        for (item, city), prices in series.items():
            rp = risk.risk_profile(prices, min_points=args.min_points)
            if rp:
                out.append({"item": name(item), "city_pt": city_pt(city),
                            "_iid": item, "_city": city, "_prices": prices, **rp})
        if not out:
            die(f"Nenhuma série com >= {args.min_points} dias. Baixe o --min-points "
                "ou colete mais histórico.")
        # C5: calibra o selo aos TERCIS do cross-section real (relativo), em vez
        # dos cortes fixos — 'seguro' é o terço menos volátil do que foi medido.
        bands = risk.calibrate_bands([r["vol_annual_pct"] / 100 for r in out])
        for r in out:
            r["risk_label"] = risk.label_for(r["vol_annual_pct"] / 100, bands)
            del r["_iid"], r["_city"], r["_prices"]
        order = {"seguro": 0, "médio": 1, "especulativo": 2}
        out.sort(key=lambda r: (order.get(r["risk_label"], 9), -r["vol_annual_pct"]))
        calib = "tercis do cross-section" if bands is not risk.VOL_BANDS else "default"
        info(f"Perfil de risco (history diário, {args.days}d, >= {args.min_points} "
             f"dias). Vol anual./EWMA · max DD · VaR 1d 5% · selo ({calib}).")
        emit(out[:args.limit], [("item", "Item"), ("city_pt", "Cidade"),
             ("points", "Dias"), ("vol_annual_pct", "Vol %a.a."),
             ("vol_ewma_pct", "Vol EWMA"),
             ("max_drawdown_pct", "Max DD %"), ("var_1d_pct", "VaR 1d %"),
             ("sortino", "Sortino"), ("risk_label", "Selo")], fmt)
    elif args.acao == "corr":
        if not item_ids:
            die("corr exige uma cesta: --cat/--sub/--tier ou itens.")
        by_item = {}
        for item, _city, day, price in rows:
            by_item.setdefault(item, {}).setdefault(day, []).append(price)
        series = {it: {d: sum(v) / len(v) for d, v in dd.items()}
                  for it, dd in by_item.items()}
        if len(series) > 80:
            series = dict(list(series.items())[:80])
            info("(cesta truncada em 80 itens p/ a matriz de correlação)")
        pairs = risk.correlation_pairs(series, min_common=args.min_points,
                                       limit=args.limit)
        for p in pairs:
            p["a_pt"] = name(p["a"]); p["b_pt"] = name(p["b"])
            p["tipo"] = "andam juntos" if p["corr"] >= 0.6 else \
                ("hedge" if p["corr"] <= 0.1 else "fraca")
        info("Correlação dos retornos diários (não níveis). corr alta = "
             "concentração de risco; baixa/negativa = candidato a hedge.")
        emit(pairs, [("a_pt", "Item A"), ("b_pt", "Item B"),
                     ("corr", "Correl."), ("common_days", "Dias comuns"),
                     ("tipo", "Leitura")], fmt)
    else:  # size
        if not args.itens:
            die("size exige um item e --capital.")
        it = resolve_item(db, " ".join(args.itens))
        prices = [p for i, c, d, p in rows if i == it["id"]]
        rp = risk.risk_profile(prices, min_points=args.min_points)
        vol = (rp or {}).get("vol_annual_pct", 0) / 100
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            prows = _cache_price_dicts(con, config.DEFAULT_SERVER, item_ids=[it["id"]])
            liq = {iid: n for iid, n in con.execute(
                """SELECT item_id, AVG(item_count) FROM history WHERE server=?
                   AND time_scale=24 AND quality=1 AND item_id=? AND item_count>0
                   AND ts>=date('now','-7 days') GROUP BY item_id""",
                [config.DEFAULT_SERVER, it["id"]]).fetchall()}
        finally:
            con.close()
        from albion.microstructure import clean_price_rows
        opps = compute_flips(clean_price_rows(prows), {it["id"]: it},
                             premium=not args.no_premium, qualities=[1])
        if not opps:
            die("Sem flip cotado p/ o item no cache (colete preços primeiro).")
        best = opps[0]
        size = risk.position_size(
            best["profit"], best["buy_price"], vol,
            liq.get(it["id"], 0), persistence=args.persistence,
            capital=args.capital)
        if fmt == "json":
            print(json.dumps({"flip": best, "risk": rp, "size": size},
                             ensure_ascii=False, indent=2)); return
        info(f"Sizing de {it['pt']}: melhor flip {city_pt(best['buy_city'])}→"
             f"{city_pt(best['sell_city'])} lucro/un {best['profit']:,.0f} · "
             f"vol {round(vol*100,1)}%a.a. · limitado por {size['limited_by']}.")
        emit([{**size}], [("units", "Comprar (un)"), ("kelly_frac", "Fração Kelly"),
              ("capital_used", "Capital"), ("profit_total", "Lucro total"),
              ("limited_by", "Limite")], fmt)


def _clean_price_lookups(con, server, max_age_days=3):
    """(q1, allq) das linhas de prices SANEADAS: sem preços-âncora (outlier z
    entre cidades) E sem pontas STALE — uma ordem com sell_price_min_date mais
    velha que max_age_days é provável flip-fantasma e não deve entrar no min()
    de make_or_buy/burn/watchlist."""
    from albion.microstructure import clean_price_rows
    # corte de frescor em ISO (a data da ordem usa 'T'; placeholders '0001-' caem)
    cutoff = con.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S','now', ?)",
        [f"-{int(max_age_days)} days"]).fetchone()[0]
    rows = clean_price_rows(_cache_price_dicts(con, server))
    q1, allq = {}, {}
    for r in rows:
        sp = r.get("sell_price_min") or 0
        d = r.get("sell_price_min_date") or ""
        if sp <= 0 or d < cutoff:        # sem preço ou ponta velha -> ignora
            continue
        item, city, q = r["item_id"], r["city"], r["quality"]
        allq[(item, city, q)] = min(allq.get((item, city, q), sp), sp)
        if q == 1:
            q1[(item, city)] = min(q1.get((item, city), sp), sp)
    return q1, allq


def cmd_prod(args, fmt):
    """Economia de produção: foco, cadeia vertical, refinar-vs-vender, qualidade."""
    from albion import production as prod
    db_path = (DATA / "cache.db").resolve()
    if not db_path.exists():
        die("Cache não encontrado — rode `collect` antes.")
    premium = not args.no_premium
    cities = parse_cities(args.cities)
    db = ItemDB()
    name = lambda iid: (db.get(iid) or {}).get("pt", iid)
    con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        q1, allq = _load_cache_prices(
            con, config.DEFAULT_SERVER,
            qualities=(1, 2, 3, 4, 5) if args.acao == "quality" else (1,))
    finally:
        con.close()

    if args.acao == "focus":
        res = prod.focus_efficiency(q1, premium=premium, sell_mode=args.sell_mode,
                                    cities=cities, min_margin=args.min_margin,
                                    limit=args.limit)
        for r in res:
            r["item"] = name(r["item_id"]); r["city_pt"] = city_pt(r["city"])
            r["tipo"] = "refino" if r["is_refining"] else "craft"
        info("Prata por foco da cadeia inteira (craft + refino), melhor cidade. "
             "Só itens com insumo+produto cotados no cache. 'ganho_foco' = quanto "
             "o foco rendeu vs sem foco.")
        emit(res, [("item", "Item"), ("tipo", "Tipo"), ("city_pt", "Cidade"),
                   ("silver_per_focus", "Prata/foco"), ("focus", "Foco"),
                   ("margin_focus", "Margem c/foco"), ("focus_gain", "Ganho foco")],
             fmt)
    elif args.acao == "chain":
        if not args.itens:
            die("prod chain exige um item (ex.: `prod chain \"Espada Larga T5\"`).")
        it = resolve_item(db, " ".join(args.itens))
        res = prod.vertical_pnl(it["id"], q1, premium=premium,
                                sell_mode=args.sell_mode, focus=args.focus,
                                city=cities[0] if cities else None)
        if fmt == "json":
            print(json.dumps(res, ensure_ascii=False, indent=2)); return
        info(f"PnL de cadeia de {it['pt']} em {city_pt(res['city'])} "
             f"({'com' if args.focus else 'sem'} foco). Venda líq: "
             f"{res['sell_net']} · custo de fazer a raiz: {res['make_cost']} · "
             f"PnL fazer: {res['pnl_make']}. Cada degrau mostra comprar vs fazer.")
        rows = [{"item": name(s["item_id"]), "nivel": s["depth"],
                 "comprar": s["buy"], "fazer": s["make"],
                 "decisao": s["decision"]} for s in res["steps"]]
        emit(rows, [("item", "Insumo"), ("nivel", "Nível"),
                    ("comprar", "Comprar"), ("fazer", "Fazer"),
                    ("decisao", "Decisão")], fmt)
    elif args.acao == "refine":
        if args.itens:
            it = resolve_item(db, " ".join(args.itens))
            res = prod.refine_premium(it["id"], q1, premium=premium,
                                      sell_mode=args.sell_mode, focus=args.focus,
                                      cities=cities)
            for r in res:
                r["item"] = name(r["item_id"])
                r["city_pt"] = city_pt(r["city"]) + ("  ★" if r["is_bonus_city"] else "")
            info(f"Refinar {it['pt']} vs vender o bruto, por cidade "
                 f"({'com' if args.focus else 'sem'} foco). Prêmio % > 0 = refinar paga.")
            # C2: situa o prêmio de hoje no histórico (percentil + z) — não só o agora
            recipe = prod.craft.recipe_for(it["id"])
            if recipe:
                hist_ids = [it["id"]] + [i["id"] for i in recipe["inputs"]]
                con2 = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
                try:
                    hrows = _history_daily(con2, config.DEFAULT_SERVER, hist_ids,
                                           cities, days=args.hist_days)
                finally:
                    con2.close()
                hist = {}
                for i2, c2, d2, p2 in hrows:
                    hist.setdefault((i2, c2), {})[d2] = p2
                hp = prod.refine_premium_history(
                    it["id"], hist, premium=premium, sell_mode=args.sell_mode,
                    focus=args.focus, city=(cities[0] if cities else None))
                if hp:
                    info(f"Histórico ({hp['days']}d, {city_pt(hp['city'])}): prêmio "
                         f"hoje {hp['premium_now_pct']}% = percentil {hp['percentile']} "
                         f"(z {hp['z_resid']}); faixa p10/med/p90 = "
                         f"{hp['p10']}/{hp['median']}/{hp['p90']}% → {hp['verdict']}.")
            emit(res, [("city_pt", "Cidade"), ("raw_cost", "Custo bruto"),
                       ("rrr_pct", "RRR %"), ("refined_net", "Refinado líq"),
                       ("margin", "Margem"), ("premium_pct", "Prêmio %"),
                       ("verdict", "Veredito")], fmt)
        else:  # ranqueia todos os refinados cotados no cache
            from albion import production as prod2
            recipes = prod2.craft._load("recipes_refining.json")
            best = []
            for rid in recipes:
                r = prod.refine_premium(rid, q1, premium=premium,
                                        sell_mode=args.sell_mode, focus=args.focus,
                                        cities=cities)
                if r:
                    top = r[0]
                    top["item"] = name(rid)
                    top["city_pt"] = city_pt(top["city"]) + ("  ★" if top["is_bonus_city"] else "")
                    best.append(top)
            best.sort(key=lambda r: -r["premium_pct"])
            info("Melhor cidade de cada material refinado cotado no cache, "
                 "ordenado por prêmio de refino (refinar vs vender o bruto).")
            emit(best[:args.limit], [("item", "Refinado"), ("city_pt", "Cidade"),
                 ("premium_pct", "Prêmio %"), ("margin", "Margem"),
                 ("rrr_pct", "RRR %"), ("verdict", "Veredito")], fmt)
    else:  # quality
        if not args.itens:
            die("prod quality exige um item (ex.: `prod quality \"Arco do Adepto\"`).")
        it = resolve_item(db, " ".join(args.itens))
        res = prod.quality_ev(it["id"], allq, premium=premium,
                              sell_mode=args.sell_mode, focus=args.focus,
                              cities=cities)
        if not res:
            die("Sem qualidades suficientes cotadas no cache p/ esperança "
                "(precisa de >=3 qualidades com preço). Colete mais e tente de novo.")
        for r in res:
            r["item"] = name(r["item_id"])
            r["city_pt"] = city_pt(r["city"]) + ("  ★" if r["is_bonus_city"] else "")
            r["quals"] = ",".join(map(str, r["qualities_priced"]))
        info(f"Valor esperado do craft de {it['pt']} ponderado por qualidade "
             "(pesos 689/250/50/10/1 do jogo) vs só-q1. uplift = ganho de "
             "olhar a esperança em vez de q1.")
        emit(res, [("city_pt", "Cidade"), ("quals", "Q cotadas"),
                   ("eff_cost", "Custo efetivo"), ("margin_q1", "Margem q1"),
                   ("margin_ev", "Margem esperada"), ("ev_uplift", "Uplift")], fmt)


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
        metavar="{search,prices,flips,scan,sell,history,recommend,lab,craft,watch,"
                "collect,intel,survival,backtest,journals,refine,report,pos,"
                "indexes,prod,logi,risk,fc,demand,guild,micro,gold,status,prune,sql}")

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
    p.add_argument("acao", choices=("add", "rm", "list", "rebuild"))
    p.add_argument("itens", nargs="*",
                   help="itens (para add/rm), separados por vírgula")
    p.add_argument("--limit", type=int, default=500,
                   help="quantos itens manter no rebuild (padrão: 500)")
    p.add_argument("--replace", action="store_true",
                   help="rebuild substitui a watchlist (em vez de só adicionar)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("collect", parents=[common],
                       help="coleta preços + histórico (watchlist ou filtros)")
    p.add_argument("--itens", help="itens específicos separados por vírgula")
    p.add_argument("--cat"); p.add_argument("--sub")
    p.add_argument("--tier-min", type=int); p.add_argument("--tier-max", type=int)
    p.add_argument("--cities", help="cidades separadas por vírgula")
    p.add_argument("--days", type=int, default=30,
                   help="janela de histórico (padrão: 30)")
    p.add_argument("--deep", action="store_true",
                   help="recarga profunda de 365 d (semanal) p/ séries longas")
    p.add_argument("--max-items", type=int, default=config.COLLECT_MAX_ITEMS)
    p.add_argument("--skip-if-recent", type=int, metavar="MIN",
                   help="não coleta se a última rodada OK tiver menos de MIN "
                        "minutos (evita duplicar com o servidor aberto)")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("refine", parents=[common],
                       help="margem de refino por cidade (receitas do jogo)")
    p.add_argument("familia", help="wood|ore|fiber|hide|rock")
    p.add_argument("--tier", type=int, required=True)
    p.add_argument("--ench", type=int, default=0, choices=range(0, 5))
    p.add_argument("--rrr", type=float, default=36.7,
                   help="retorno de recursos %% (36,7 bônus; 53,9 foco; 15,2 sem)")
    p.add_argument("--fee", type=float, default=0,
                   help="taxa da estação por refino, em prata (padrão: 0)")
    p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--max-age", type=int, default=config.PRICES_TTL)
    p.set_defaults(func=cmd_refine)

    p = sub.add_parser("craft", parents=[common],
                       help="margem de craft de um item (receitas+RRR reais)")
    p.add_argument("item", nargs="+", help="id ou nome PT do item craftável")
    p.add_argument("--sell-mode", choices=("instant", "order"), default="order")
    p.add_argument("--focus", action="store_true",
                   help="usa RRR com foco (retorno maior)")
    p.add_argument("--fee", type=float, default=0,
                   help="taxa da estação por craft, em prata")
    p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--max-age", type=int, default=config.PRICES_TTL)
    p.set_defaults(func=cmd_craft)

    p = sub.add_parser("origin", parents=[common],
                       help="de onde o item nasce: mobs que o dropam")
    p.add_argument("item", nargs="+", help="id ou nome PT do item")
    p.set_defaults(func=cmd_origin)

    p = sub.add_parser("journals", parents=[common],
                       help="margem de diários: vazio -> cheio, por família/tier")
    p.add_argument("--family",
                   help="WOOD|ORE|FIBER|HIDE|STONE|FISHING|MERCENARY|GENERAL")
    p.add_argument("--tier-min", type=int)
    p.add_argument("--tier-max", type=int)
    p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--max-age", type=int, default=config.PRICES_TTL)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_journals)

    p = sub.add_parser("report", parents=[common],
                       help="relatório do dia (opcional: publica no Discord)")
    p.add_argument("--top", type=int, default=8,
                   help="quantas oportunidades listar (padrão: 8)")
    p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--discord", action="store_true",
                   help="publica via webhook (config.DISCORD_WEBHOOK_URL)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("backtest", parents=[common],
                       help="valida o sinal: lucro prometido vs realizado")
    p.add_argument("--premium", action=argparse.BooleanOptionalAction,
                   default=True)
    p.add_argument("--min-profit", type=float, default=500,
                   help="só avalia oportunidades com lucro esperado acima disso")
    p.add_argument("--max-runs", type=int, default=60,
                   help="quantas rodadas de coleta recentes usar (padrão: 60)")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("survival", parents=[common],
                       help="persistência das ordens (proxy de flip fantasma)")
    p.add_argument("--item", help="filtra um item (id ou nome PT)")
    p.add_argument("--city", help="filtra uma cidade")
    p.add_argument("--quality", type=int, choices=range(1, 6))
    p.set_defaults(func=cmd_survival)

    p = sub.add_parser("intel", parents=[common],
                       help="killboard: coleta pública e índice de destruição")
    p.add_argument("acao", choices=("collect", "top", "signals", "risk",
                                    "validate", "status"))
    p.add_argument("--source", choices=("events", "battles", "all"),
                   default="all", help="fonte da coleta (padrão: all)")
    p.add_argument("--pages", type=int, default=config.GAMEINFO_EVENT_PAGES,
                   help="páginas de 51 eventos por varredura")
    p.add_argument("--days", type=float, default=1,
                   help="janela do ranking em dias (padrão: 1)")
    p.add_argument("--role", choices=("victim", "killer"), default="victim")
    p.add_argument("--inventory", action="store_true",
                   help="inclui o inventário das vítimas (transporte)")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_intel)

    p = sub.add_parser("pos", parents=[common],
                       help="portfolio: registra compras/vendas reais e PnL")
    p.add_argument("acao", choices=("add", "sell", "list", "rm"))
    p.add_argument("item", nargs="?", help="item (para add)")
    p.add_argument("--id", dest="pos_id", type=int, help="id da posição")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--price", type=float, help="preço unitário")
    p.add_argument("--city", help="cidade")
    p.add_argument("--quality", type=int, default=1, choices=range(1, 6))
    p.add_argument("--note", help="anotação livre")
    p.add_argument("--all", action="store_true",
                   help="lista também as fechadas")
    p.set_defaults(func=cmd_pos)

    p = sub.add_parser("indexes", parents=[common],
                       help="índice de preço (base 100) de uma cesta de itens")
    p.add_argument("--cat", help="categoria da cesta (ex.: crafting)")
    p.add_argument("--sub", help="subcategoria (ex.: resources)")
    p.add_argument("--tier-min", type=int)
    p.add_argument("--tier-max", type=int)
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_indexes)

    p = sub.add_parser("demand", parents=[common],
                       help="demanda (killboard): consumíveis, qualidade destruída, meta")
    p.add_argument("acao", choices=("burn", "quality", "meta"))
    p.add_argument("--days", type=float, default=7, help="janela killboard")
    p.add_argument("--recent", type=float, default=2, help="janela recente (meta)")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_demand)

    p = sub.add_parser("guild", parents=[common],
                       help="guild: ROI de coleta, cesta de regear, make-or-buy")
    p.add_argument("acao", choices=("watch", "kit", "makeorbuy"))
    p.add_argument("--days", type=float, default=7, help="janela killboard")
    p.add_argument("--hist-days", type=int, default=120, help="janela do índice (kit)")
    p.add_argument("--focus", action="store_true", help="usa foco (makeorbuy)")
    p.add_argument("--no-premium", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_guild)

    p = sub.add_parser("fc", parents=[common],
                       help="previsão: reversão, par trading, previsibilidade, regime")
    p.add_argument("acao", choices=("revert", "pair", "predict", "regime"))
    p.add_argument("itens", nargs="*", help="itens (pair exige 1)")
    p.add_argument("--cat", help="categoria (cesta)")
    p.add_argument("--sub", help="subcategoria")
    p.add_argument("--tier-min", type=int)
    p.add_argument("--tier-max", type=int)
    p.add_argument("--cities", help="cidades (filtra)")
    p.add_argument("--days", type=int, default=180, help="janela de history")
    p.add_argument("--min-points", type=int, default=20,
                   help="dias mínimos (revert/predict 20; pair 60)")
    p.add_argument("--signals", action="store_true",
                   help="só linhas com sinal (revert)")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_fc)

    p = sub.add_parser("risk", parents=[common],
                       help="risco & portfólio: perfil de risco, sizing, correlação")
    p.add_argument("acao", choices=("profile", "size", "corr"))
    p.add_argument("itens", nargs="*", help="itens (size exige 1; profile/corr opc.)")
    p.add_argument("--cat", help="categoria (cesta)")
    p.add_argument("--sub", help="subcategoria")
    p.add_argument("--tier-min", type=int)
    p.add_argument("--tier-max", type=int)
    p.add_argument("--cities", help="cidades (filtra)")
    p.add_argument("--days", type=int, default=180, help="janela de history (padrão 180)")
    p.add_argument("--min-points", type=int, default=30,
                   help="dias mínimos de série (padrão 30)")
    p.add_argument("--capital", type=float, help="capital p/ sizing")
    p.add_argument("--persistence", type=float, default=0.7,
                   help="persistência da ordem 0..1 (sizing; padrão 0,7)")
    p.add_argument("--no-premium", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_risk)

    p = sub.add_parser("logi", parents=[common],
                       help="logística: carga, reposição, escada de qualidade, BM")
    p.add_argument("acao", choices=("cargo", "restock", "ladder", "bm"))
    p.add_argument("--buy-city", help="cidade de compra (cargo)")
    p.add_argument("--sell-city", help="cidade de venda (cargo)")
    p.add_argument("--cities", help="cidades (ladder)")
    p.add_argument("--kg", type=float, default=1500,
                   help="capacidade de carga em kg (cargo; padrão: 1500 = boi)")
    p.add_argument("--buy-mode", choices=("instant", "order"), default="instant")
    p.add_argument("--sell-mode", choices=("instant", "order"), default="order")
    p.add_argument("--no-premium", action="store_true")
    p.add_argument("--min-premium", type=float, default=0,
                   help="prêmio %% mínimo (ladder)")
    p.add_argument("--days", type=float, default=1, help="janela killboard (restock)")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_logi)

    p = sub.add_parser("prod", parents=[common],
                       help="produção: foco, cadeia vertical, refinar-vs-vender, qualidade")
    p.add_argument("acao", choices=("focus", "chain", "refine", "quality"))
    p.add_argument("itens", nargs="*",
                   help="item (chain/quality exigem; refine opcional)")
    p.add_argument("--cities", help="cidades (filtra)")
    p.add_argument("--sell-mode", choices=("instant", "order"), default="order")
    p.add_argument("--no-premium", action="store_true")
    p.add_argument("--focus", action="store_true", help="usa foco (RRR maior)")
    p.add_argument("--min-margin", type=float, default=0,
                   help="margem mínima (focus)")
    p.add_argument("--hist-days", type=int, default=120,
                   help="janela do percentil histórico (refine; padrão 120)")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_prod)

    p = sub.add_parser("micro", parents=[common],
                       help="microestrutura: market-making, livro, armadilhas")
    p.add_argument("acao", choices=("spread", "book", "traps", "capital",
                                    "hours"))
    p.add_argument("itens", nargs="*",
                   help="itens (opcional p/ spread/book/traps; obrigatório p/ hours)")
    p.add_argument("--cities", help="cidades (filtra; obrig. p/ hours)")
    p.add_argument("--qualities", help="qualidades (ex.: 1,2)")
    p.add_argument("--no-premium", action="store_true",
                   help="usa imposto de 8% (sem premium)")
    p.add_argument("--max-age", type=int, default=720,
                   help="idade máx. do livro em min (padrão: 720)")
    p.add_argument("--min-net", type=float, default=1,
                   help="net mínimo por unidade (spread)")
    p.add_argument("--min-volume", type=float, default=0,
                   help="giro/dia mínimo (spread/capital)")
    p.add_argument("--capital", type=float,
                   help=f"capital p/ alocar (padrão: {config.ORDER_MAX_CAPITAL})")
    p.add_argument("--days", type=int, default=7, help="janela p/ hours")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_micro)

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

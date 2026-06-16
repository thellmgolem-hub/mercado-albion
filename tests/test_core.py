import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import app
from albion import stats, survival
from albion.client import AODP
from albion.flips import buy_cost, compute_flips, confidence, sell_revenue
from albion.items import ItemDB


DATE = "2026-06-10T12:00:00"


def row(item_id, city, quality, sell=0, buy=0):
    return {
        "item_id": item_id,
        "city": city,
        "quality": quality,
        "sell_price_min": sell,
        "sell_price_min_date": DATE if sell else "0001-01-01T00:00:00",
        "sell_price_max": 0,
        "sell_price_max_date": "0001-01-01T00:00:00",
        "buy_price_min": 0,
        "buy_price_min_date": "0001-01-01T00:00:00",
        "buy_price_max": buy,
        "buy_price_max_date": DATE if buy else "0001-01-01T00:00:00",
        "fetched_at": 0,
    }


class FlipMathTests(unittest.TestCase):
    def test_market_fee_formulas(self):
        self.assertEqual(buy_cost(1000, "instant"), 1000)
        self.assertAlmostEqual(buy_cost(1000, "order"), 1025)
        self.assertAlmostEqual(sell_revenue(1000, "instant", True), 960)
        self.assertAlmostEqual(sell_revenue(1000, "order", True), 935)
        self.assertAlmostEqual(sell_revenue(1000, "instant", False), 920)

    def test_confidence_uses_stalest_side(self):
        self.assertEqual(confidence(10, 20)[:2], (100, "alta"))
        self.assertEqual(confidence(45, 100)[:2], (75, "media"))
        self.assertEqual(confidence(180, 240)[:2], (50, "baixa"))
        self.assertEqual(confidence(None, 20)[:2], (0, "muito baixa"))

    def test_basic_city_flip_profit(self):
        rows = [
            row("T5_BAG", "Martlock", 1, sell=1000, buy=900),
            row("T5_BAG", "Caerleon", 1, sell=1300, buy=1500),
        ]
        meta = {"T5_BAG": {"pt": "Bolsa", "en": "Bag", "cat": "bags", "w": 1}}

        opps = compute_flips(rows, meta, buy_mode="instant", sell_mode="instant")

        best = opps[0]
        self.assertEqual(best["buy_city"], "Martlock")
        self.assertEqual(best["sell_city"], "Caerleon")
        self.assertEqual(best["profit"], 440)
        self.assertEqual(best["roi_pct"], 44)
        self.assertIn("confidence_score", best)

    def test_black_market_lower_quality_order_is_usable(self):
        rows = [
            row("T5_BAG", "Martlock", 2, sell=500, buy=0),
            row("T5_BAG", "Black Market", 1, sell=0, buy=1000),
        ]
        meta = {"T5_BAG": {"pt": "Bolsa", "en": "Bag", "cat": "bags", "w": 1}}

        opps = compute_flips(rows, meta, qualities=[2])

        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0]["sell_city"], "Black Market")
        self.assertEqual(opps[0]["bm_order_quality"], 1)
        self.assertEqual(opps[0]["profit"], 460)


class ItemAndApiTests(unittest.TestCase):
    def test_item_search_understands_tier_enchant_tokens(self):
        db = ItemDB()

        results = db.search("bolsa 5.0", limit=10)

        self.assertTrue(any(r["id"] == "T5_BAG" for r in results))

    def test_grouped_search_collapses_enchants_under_limit(self):
        db = ItemDB()

        # sem agrupar, os 5 encantos de T4_BAG ocupam 5 linhas
        flat = db.search("bolsa do adepto", limit=10)
        self.assertGreater(sum(1 for r in flat
                               if r["id"].split("@")[0] == "T4_BAG"), 1)

        # agrupando, vira 1 linha-base com a lista de encantos
        grouped = db.search("bolsa do adepto", limit=10, group=True)
        bags = [r for r in grouped if r["id"].split("@")[0] == "T4_BAG"]
        self.assertEqual(len(bags), 1)
        self.assertEqual(bags[0]["enchants"], [0, 1, 2, 3, 4])
        self.assertEqual(bags[0]["base_id"], "T4_BAG")

    def test_partial_fallback_when_strict_and_fails(self):
        db = ItemDB()
        # AND estrito não acha (palavra divergente), mas o item existe:
        # 'manto de thetford' -> a Capa de Thetford deveria aparecer
        strict = db.search("manto de thetford xyz", group=True, limit=10)
        self.assertTrue(strict, "fallback parcial deveria retornar algo")
        # busca exata não pode ser degradada pelo fallback
        exact = db.search("bolsa do adepto", group=True, limit=5)
        self.assertEqual(exact[0]["id"], "T4_BAG")
        # token único continua estrito (sem fallback): 'bolsa' acha as bolsas
        one = db.search("bolsa", group=True, limit=200)
        self.assertTrue(all("bolsa" in r["pt"].lower()
                            or "bolsa" in r["en"].lower()
                            or "bag" in r["id"].lower() for r in one))

    def test_grouped_search_limit_counts_base_items(self):
        db = ItemDB()
        # "arco": dezenas de itens-base, mas os encantos entupiam o limite.
        # agrupado, o limite passa a contar itens-base distintos.
        grouped = db.search("arco", limit=40, group=True)
        bases = {r["base_id"] for r in grouped}
        self.assertEqual(len(grouped), len(bases))      # nenhuma duplicata
        self.assertGreaterEqual(len(bases), 30)         # alcança muito mais

    def test_meta_and_status_endpoints_are_available(self):
        client = TestClient(app.app)

        meta = client.get("/api/meta")
        status = client.get("/api/status")
        # regressão: nenhum default de query param pode violar o próprio
        # limite (ex.: /api/collect com default 2500 e le=600 quebrava o
        # botão Coletar) — validado pelo schema OpenAPI inteiro
        spec = client.get("/openapi.json").json()
        for path, methods in spec["paths"].items():
            for method in methods.values():
                for param in method.get("parameters", []):
                    sch = param.get("schema", {})
                    if "default" in sch and "maximum" in sch:
                        self.assertLessEqual(
                            sch["default"], sch["maximum"],
                            f"default > maximum em {path}:{param['name']}")
        recs = client.get("/api/recommendations", params={"limit": 5})
        lab = client.get("/api/item-analysis", params={
            "item": "T4_FIBER",
            "cities": "Bridgewatch,Lymhurst",
            "quality": 1,
            "days": 7,
            "time_scale": 24,
        })

        self.assertEqual(meta.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(recs.status_code, 200)
        self.assertEqual(lab.status_code, 200)
        self.assertEqual(meta.json()["server"], "americas")
        self.assertGreaterEqual(meta.json()["item_count"], 12000)
        self.assertIn("prices", status.json()["tables"])
        self.assertIn("opportunities", recs.json())
        self.assertIn("series", lab.json())

    def test_aodp_initializes_price_snapshots_table(self):
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                tables = {
                    r[0] for r in client.db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                }
                indexes = {
                    r[0] for r in client.db.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'")
                }
                # consulta por janela de tempo deve usar seek por índice, não
                # varredura completa (regressão de BACKTEST-1/SURVIVAL-1)
                plan = " ".join(str(r) for r in client.db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM price_snapshots"
                    " WHERE server=? AND fetched_at BETWEEN ? AND ?",
                    ("americas", 0, 9e9)).fetchall())
            finally:
                client.db.close()

        self.assertIn("price_snapshots", tables)
        self.assertIn("watchlist", tables)
        self.assertIn("price_snapshots_daily", tables)
        self.assertIn("idx_price_snapshots_time", indexes)
        self.assertIn("idx_price_snapshots_time", plan)


def _series(prices, volumes=None, hours=False):
    from datetime import datetime, timedelta
    volumes = volumes or [10] * len(prices)
    base = datetime(2026, 1, 1)
    step = timedelta(hours=1) if hours else timedelta(days=1)
    return {
        "item_id": "T4_TEST", "city": "Martlock", "quality": 1,
        "data": [{"ts": (base + step * i).isoformat(),
                  "avg_price": p, "item_count": v}
                 for i, (p, v) in enumerate(zip(prices, volumes))],
    }


class StatsTests(unittest.TestCase):
    def test_linear_fit_recovers_known_slope(self):
        a, b = stats.linear_fit([10, 12, 14, 16, 18])
        self.assertAlmostEqual(a, 10)
        self.assertAlmostEqual(b, 2)

    def test_true_median_even_n(self):
        m = stats.analyze_history_series(
            _series([100, 200, 300, 400]), days=4, time_scale=24,
            global_latest_ts=None)
        self.assertEqual(m["median_price"], 250)  # proxy antigo daria 300

    def test_z_score_is_residual_based_not_trend(self):
        # tendência de alta perfeita + último ponto na linha: z deve ser ~0,
        # não fortemente positivo como seria o z de níveis
        prices = [100 + 5 * i for i in range(10)]
        prices[3] += 2  # ruído mínimo para haver desvio residual
        m = stats.analyze_history_series(
            _series(prices), days=10, time_scale=24, global_latest_ts=None)
        self.assertLess(abs(m["z_score"]), 1)
        # z de níveis seria ~+1,5 num caso desses — checagem de contraste
        level_z = (prices[-1] - sum(prices) / len(prices)) / stats.std(prices)
        self.assertGreater(level_z, 1.3)

    def test_anomaly_detected_on_flat_series(self):
        # série estável com ruído (MAD > 0) e uma anomalia real no fim
        prices = [100, 101, 99, 100.5, 99.5, 101, 99, 100, 101, 140]
        m = stats.analyze_history_series(
            _series(prices), days=10, time_scale=24, global_latest_ts=None)
        self.assertGreater(m["z_score"], 2)
        self.assertGreater(m["robust_z"], 2)

    def test_vwap_weights_by_volume(self):
        m = stats.analyze_history_series(
            _series([100, 200], volumes=[30, 10]), days=2, time_scale=24,
            global_latest_ts=None)
        self.assertAlmostEqual(m["vwap"], (100 * 30 + 200 * 10) / 40, places=2)

    def test_forecast_band_uses_residual_std(self):
        prices = [100 + 5 * i for i in range(10)]
        prices[5] += 1
        m = stats.analyze_history_series(
            _series(prices), days=10, time_scale=24, global_latest_ts=None)
        f = m["naive_forecast_next"]
        band = f["high"] - f["price"]
        # desvio dos níveis (~15) daria banda enorme; resíduos dão banda pequena
        self.assertLess(band, 2)
        self.assertGreater(f["price"], prices[-1])  # segue a tendência

    def test_abs_score_anchors(self):
        anchor = 50_000
        self.assertEqual(stats.abs_score(0, anchor), 0)
        self.assertEqual(stats.abs_score(None, anchor), 0)
        self.assertAlmostEqual(stats.abs_score(anchor, anchor), 100, places=5)
        self.assertEqual(stats.abs_score(anchor * 10, anchor), 100)  # teto
        self.assertLess(stats.abs_score(anchor / 10, anchor),
                        stats.abs_score(anchor / 2, anchor))

    def test_momentum_window_respects_time_scale(self):
        # escala horária: janela de 7 dias = 168 pontos; com 200 pontos a
        # referência do momentum é o ponto -169, não o -8
        prices = [100.0] * 200
        for i in range(1, 169):
            prices[-i] = 110.0  # subiu 10% nos últimos 7 dias
        m = stats.analyze_history_series(
            _series(prices, hours=True), days=9, time_scale=1,
            global_latest_ts=None)
        self.assertAlmostEqual(m["momentum_pct"], 10.0, places=1)


class SurvivalTests(unittest.TestCase):
    def test_persistence_by_age_bucket(self):
        from datetime import datetime, timezone
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()

        def iso(epoch):
            return datetime.fromtimestamp(epoch, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S")

        def snap(item, fetched, sell_price, sell_date):
            return ("americas", item, "Martlock", 1,
                    sell_price, sell_date, 0, "0001-01-01T00:00:00",
                    0, "0001-01-01T00:00:00", 0, "0001-01-01T00:00:00",
                    fetched)

        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                rows = [
                    # ordem jovem (15 min) que SOBREVIVE à coleta seguinte
                    snap("T4_A", base, 100, iso(base - 15 * 60)),
                    snap("T4_A", base + 1800, 100, iso(base - 15 * 60)),
                    # ordem velha (500 min) que SOME na coleta seguinte
                    snap("T4_B", base, 100, iso(base - 500 * 60)),
                    snap("T4_B", base + 1800, 120, iso(base + 600)),
                ]
                client.db.executemany(
                    "INSERT INTO price_snapshots VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                client.db.commit()
                # days=None: testa a lógica de bucketing com timestamps fixos
                # (fora de qualquer janela recente)
                res = survival.persistence(client.db, "americas", days=None)
            finally:
                client.db.close()

        sell = {b["age_label"]: b for b in res["sides"]["sell"]["buckets"]}
        self.assertEqual(res["snapshot_pairs"], 2)
        self.assertEqual(sell["0-30 min"]["pairs"], 1)
        self.assertEqual(sell["0-30 min"]["rate_pct"], 100.0)
        self.assertEqual(sell["480-1440 min"]["pairs"], 1)
        self.assertEqual(sell["480-1440 min"]["rate_pct"], 0.0)
        # lado de compra sem dados (preço 0) não conta pares
        self.assertEqual(res["sides"]["buy"]["pairs"], 0)


class BacktestTests(unittest.TestCase):
    def test_signal_backtest_expected_vs_realized(self):
        from datetime import datetime, timezone
        from albion import backtest as bt
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        date1 = "2026-01-01T11:50:00"

        def snap(item, city, fetched, sell, buy):
            return ("americas", item, city, 1, sell, date1, 0,
                    "0001-01-01T00:00:00", 0, "0001-01-01T00:00:00",
                    buy, date1 if buy else "0001-01-01T00:00:00", fetched)

        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                # duas rodadas de coleta, 30 min de distância
                client.db.executemany(
                    "INSERT INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    [("americas", base, base + 60, "test", 2, 8, 0, 1, None),
                     ("americas", base + 1800, base + 1860, "test", 2, 8, 0, 1,
                      None)])
                rows = [
                    # rota A: compra 1000 em Martlock, BM paga 2000 em R1;
                    # em R2 a ordem do BM caiu para 1500 (captura parcial)
                    snap("T5_BAG", "Martlock", base, 1000, 0),
                    snap("T5_BAG", "Black Market", base, 0, 2000),
                    snap("T5_BAG", "Martlock", base + 1800, 1000, 0),
                    snap("T5_BAG", "Black Market", base + 1800, 0, 1500),
                ]
                client.db.executemany(
                    "INSERT INTO price_snapshots VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                client.db.commit()
                metas = {"T5_BAG": {"pt": "Bolsa", "en": "Bag",
                                    "cat": "bags", "w": 1}}
                # regressão: o endpoint usa row_factory=Row (não ordenável)
                import sqlite3 as _sq
                client.db.row_factory = _sq.Row
                res = bt.signal_backtest(client.db, "americas", metas,
                                         premium=True, min_profit=0)
            finally:
                client.db.close()

        ov = res["overall"]
        self.assertEqual(res["run_pairs_used"], 1)
        self.assertEqual(ov["n"], 1)
        # esperado: 0.96*2000 - 1000 = 920 ; realizado: 0.96*1500 - 1000 = 440
        self.assertAlmostEqual(ov["expected_profit_avg"], 920, places=1)
        self.assertAlmostEqual(ov["realized_profit_avg"], 440, places=1)
        self.assertAlmostEqual(ov["capture_pct"], 47.8, delta=0.1)
        self.assertEqual(ov["hit_rate_pct"], 100.0)
        self.assertEqual(res["by_route"]["Mercado Negro"]["n"], 1)


class IntelTests(unittest.TestCase):
    def test_ingest_dedup_and_destruction_top(self):
        from datetime import datetime, timezone
        from albion import gameinfo

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
        event = {
            "EventId": 10, "TimeStamp": ts, "BattleId": 10, "Type": "KILL",
            "KillArea": "OPEN_WORLD", "Location": None,
            "TotalVictimKillFame": 5000, "numberOfParticipants": 1,
            "groupMemberCount": 1,
            "Killer": {"Id": "k1", "Name": "Killer", "AverageItemPower": 1200,
                       "Equipment": {"MainHand": {"Type": "T4_TESTBOW",
                                                  "Count": 1, "Quality": 1}},
                       "Inventory": []},
            "Victim": {"Id": "v1", "Name": "Victim", "AverageItemPower": 1100,
                       "Equipment": {"MainHand": {"Type": "T4_TESTSWORD",
                                                  "Count": 1, "Quality": 2}},
                       "Inventory": [{"Type": "T4_LOOT", "Count": 5,
                                      "Quality": 1}]},
            "Participants": [],
        }

        class FakeClient:
            def events_page(self, offset):
                return [event] if offset == 0 else []

        with TemporaryDirectory() as tmp:
            aodp = AODP(db_path=Path(tmp) / "cache.db")
            try:
                aodp.db.execute(
                    "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("americas", "T4_TESTSWORD", "Martlock", 1, 100, ts,
                     0, "", 0, "", 0, "", 0))
                aodp.db.commit()
                r1 = gameinfo.ingest_events(aodp, FakeClient(), max_pages=2)
                r2 = gameinfo.ingest_events(aodp, FakeClient(), max_pages=2)
                top = gameinfo.destruction_top(aodp.db, "americas", days=1,
                                               role="victim")
                top_inv = gameinfo.destruction_top(
                    aodp.db, "americas", days=1, role="victim",
                    include_inventory=True)
            finally:
                aodp.db.close()

        self.assertEqual(r1["inserted"], 1)
        self.assertEqual(r2["inserted"], 0)  # idempotente via checkpoint
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["item_id"], "T4_TESTSWORD")
        self.assertEqual(top[0]["valor_estimado"], 100)
        self.assertEqual(top[0]["valor_trash_estimado"], 30)  # 30% padrão
        inv = {t["item_id"]: t for t in top_inv}
        self.assertEqual(inv["T4_LOOT"]["unidades"], 5)
        self.assertIsNone(inv["T4_LOOT"]["valor_estimado"])  # sem preço ref.


class DivergenceTests(unittest.TestCase):
    def test_demand_aggregation_and_price_divergence(self):
        from datetime import datetime, timedelta, timezone
        from albion import gameinfo

        now = datetime.now(timezone.utc)

        def day(offset):
            return (now - timedelta(days=offset)).strftime("%Y-%m-%d")

        with TemporaryDirectory() as tmp:
            aodp = AODP(db_path=Path(tmp) / "cache.db")
            try:
                # base (D-3..D-7): 10 un/dia; recente (D..D-2): 30 un/dia
                for off in range(8):
                    # recent_days=2 => 'recente' = off 0,1 (hoje + ontem);
                    # base = off 2..7. Recente 30/dia, base 10/dia => ratio 3.0
                    units = 30 if off <= 1 else 10
                    eid = 100 + off
                    aodp.db.execute(
                        "INSERT INTO kill_events VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("americas", eid, f"{day(off)}T12:00:00.000Z", eid,
                         "KILL", "OPEN_WORLD", None, 1000, 1, 1, "k", "v", 0))
                    aodp.db.execute(
                        "INSERT INTO kill_event_equipment VALUES "
                        "(?,?,?,?,?,?,?)",
                        ("americas", eid, "victim", "MainHand",
                         "T4_TESTSWORD", units, 1))
                    # preço flat 100, volume 50/dia (history diário q1)
                    aodp.db.execute(
                        "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)",
                        ("americas", "T4_TESTSWORD", "Martlock", 1, 24,
                         f"{day(off)}T00:00:00", 50, 100.0, 0))
                aodp.db.commit()
                n = gameinfo.aggregate_demand_daily(aodp, days_back=10)
                sigs = gameinfo.demand_price_divergence(
                    aodp.db, "americas", recent_days=2, base_days=5,
                    min_units_day=5)
            finally:
                aodp.db.close()

        self.assertEqual(n, 8)  # 8 dias agregados
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s["item_id"], "T4_TESTSWORD")
        self.assertAlmostEqual(s["demanda_ratio"], 3.0, places=1)
        self.assertAlmostEqual(s["preco_ratio"], 1.0, places=2)
        self.assertAlmostEqual(s["volume_dia"], 50.0, places=0)


class SignalValidationTests(unittest.TestCase):
    def test_validate_signals_measures_realized_return(self):
        import time as _time
        from datetime import datetime, timedelta, timezone
        from albion import gameinfo

        now = datetime.now(timezone.utc)
        emitted = _time.time() - 2 * 86400  # sinal de 2 dias atrás
        target_day = datetime.fromtimestamp(
            emitted + 86400, timezone.utc).strftime("%Y-%m-%d")

        with TemporaryDirectory() as tmp:
            aodp = AODP(db_path=Path(tmp) / "cache.db")
            try:
                aodp.db.execute(
                    "INSERT INTO demand_signal_log VALUES (?,?,?,?,?,?,?,?)",
                    ("americas", emitted, "T4_TESTSWORD", 30, 3.0,
                     100.0, 1.0, 50))
                # 1 dia após o sinal, o VWAP foi 110 -> retorno +10%
                aodp.db.execute(
                    "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)",
                    ("americas", "T4_TESTSWORD", "Martlock", 1, 24,
                     f"{target_day}T00:00:00", 50, 110.0, 0))
                aodp.db.commit()
                res = gameinfo.validate_signals(aodp.db, "americas",
                                                horizon_days=1)
            finally:
                aodp.db.close()

        self.assertEqual(res["n"], 1)
        self.assertEqual(res["hit_rate_pct"], 100.0)
        self.assertAlmostEqual(res["retorno_medio_pct"], 10.0, places=1)


class ServiceOrderTests(unittest.TestCase):
    def test_gatherer_orders_use_instant_sell_and_exclude_caerleon(self):
        from datetime import datetime, timedelta, timezone
        from albion import orders

        now = datetime.now(timezone.utc)
        ts_now = now.strftime("%Y-%m-%dT%H:%M:%S")

        with TemporaryDirectory() as tmp:
            aodp = AODP(db_path=Path(tmp) / "cache.db")
            try:
                aodp.db.execute(
                    "INSERT INTO static_items VALUES (?,?,?,?,?,?,?)",
                    ("T4_TESTRESOURCE", "Recurso", "crafting", "resources",
                     4, 0, 1.0))
                # anúncio absurdo (sell=999999) NÃO pode disparar a missão;
                # o gatilho é a ordem de compra real (buy_price_max)
                for city, buy_max in (("Caerleon", 1000), ("Martlock", 200),
                                      ("Lymhurst", 105)):
                    aodp.db.execute(
                        "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("americas", "T4_TESTRESOURCE", city, 1,
                         999999, ts_now, 0, "", 0, "", buy_max, ts_now,
                         now.timestamp()))
                    for d in range(7):
                        ts = (now - timedelta(days=d)).strftime(
                            "%Y-%m-%dT00:00:00")
                        aodp.db.execute(
                            "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)",
                            ("americas", "T4_TESTRESOURCE", city, 1, 24,
                             ts, 100, 100.0, now.timestamp()))
                aodp.db.commit()
                rows = orders.gatherer_sell_orders(
                    aodp.db, "americas", min_premium_pct=10, min_volume=20)
            finally:
                aodp.db.close()

        self.assertTrue(rows)
        # Caerleon excluída mesmo pagando mais; Lymhurst (5% > média) fora
        self.assertTrue(all(r["city_to"] != "Caerleon" for r in rows))
        self.assertEqual(rows[0]["city_to"], "Martlock")
        self.assertEqual(len(rows), 1)
        self.assertIn("instantânea", rows[0]["explanation_short"])

    def test_trader_orders_respect_capital_cap(self):
        from albion import orders
        rec = {"opportunity_label": "executar", "item_id": "T5_BAG",
               "buy_city": "Martlock", "sell_city": "Lymhurst",
               "buy_price": 10_000, "cost": 10_000, "profit": 2_000,
               "roi_pct": 20.0, "buy_age_min": 10, "liquidity_day": 5_000,
               "capture_rate": 0.2, "confidence_label": "alta"}
        out = orders.trader_orders([rec], max_capital=100_000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["quantity_base"], 10)  # 100k / 10k
        self.assertEqual(out[0]["capital_required"], 100_000)
        # custo unitário acima do teto -> missão não é emitida
        rec2 = {**rec, "cost": 200_000, "buy_price": 200_000}
        self.assertEqual(orders.trader_orders([rec2], max_capital=100_000),
                         [])

    def test_refiner_orders_use_bonus_rrr_only_in_bonus_city(self):
        from albion import orders
        # preços idênticos em Thetford (bônus de minério) e Martlock:
        # com 36,7% a margem passa do corte; com 15,2% fica negativa
        with TemporaryDirectory() as tmp:
            aodp = AODP(db_path=Path(tmp) / "cache.db")
            try:
                for city in ("Thetford", "Martlock"):
                    for item, price in (("T4_ORE", 100),
                                        ("T3_METALBAR", 100),
                                        ("T4_METALBAR", 250)):
                        aodp.db.execute(
                            "INSERT INTO prices VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("americas", item, city, 1, price,
                             "2026-06-11T12:00:00", 0, "", 0, "", 0, "", 0))
                aodp.db.commit()
                out = orders.refiner_orders(
                    aodp.db, "americas", tiers=(4,), min_margin_pct=5)
            finally:
                aodp.db.close()
        metal = [o for o in out if o["item_id"] == "T4_METALBAR"]
        self.assertEqual(len(metal), 1)
        self.assertEqual(metal[0]["city_to"], "Thetford")
        self.assertIn("com bônus", metal[0]["explanation_short"])


class CraftTests(unittest.TestCase):
    def test_craft_rrr_derived_from_dump(self):
        from albion import craft, config
        # refino derivado bate com os valores conhecidos (15,2%/36,7%)
        self.assertAlmostEqual(config.REFINING_RRR["base"], 15.2, delta=0.2)
        self.assertAlmostEqual(config.REFINING_RRR["bonus"], 36.7, delta=0.2)
        # craft de item (categoria 0.15): base 15,2%; cidade-bônus ~24,8%
        bow_bonus_city = craft.bonus_city("bow")
        self.assertTrue(bow_bonus_city)
        rrr_bonus = craft.craft_rrr("bow", bow_bonus_city) * 100
        rrr_base = craft.craft_rrr("bow", "Caerleon") * 100
        self.assertGreater(rrr_bonus, rrr_base)
        self.assertAlmostEqual(rrr_bonus, 24.8, delta=0.5)

    def test_craft_margins_use_rrr_and_rank(self):
        from albion import craft
        recipe = {"inputs": [{"id": "T4_PLANKS", "count": 10}],
                  "focus": 100, "category": "bow"}
        prices = {("T4_TESTBOW", "Lymhurst"): 2000,
                  ("T4_PLANKS", "Lymhurst"): 100,
                  ("T4_TESTBOW", "Thetford"): 2000,
                  ("T4_PLANKS", "Thetford"): 100}
        res = craft.margins("T4_TESTBOW", recipe,
                            lambda i, c: prices.get((i, c)),
                            premium=True, sell_mode="order")
        by = {r["city"]: r for r in res}
        # Lymhurst (bônus de bow) tem RRR maior -> custo efetivo menor -> margem maior
        self.assertGreater(by["Lymhurst"]["rrr_pct"], by["Thetford"]["rrr_pct"])
        self.assertGreater(by["Lymhurst"]["margin"], by["Thetford"]["margin"])
        self.assertEqual(res[0]["city"], "Lymhurst")  # ordenado por margem


class SeasonalityAndScoreTests(unittest.TestCase):
    def test_weekend_volume_ratio_and_note(self):
        from datetime import datetime, timedelta
        base = datetime(2026, 1, 1)  # quinta-feira
        prices, volumes = [], []
        for i in range(28):
            wd = (base + timedelta(days=i)).weekday()
            prices.append(100 + (i % 3))  # ruído leve
            volumes.append(30 if wd >= 5 else 10)
        m = stats.analyze_history_series(
            _series(prices, volumes), days=28, time_scale=24,
            global_latest_ts=None)
        self.assertAlmostEqual(m["weekend_volume_ratio"], 3.0, places=1)
        self.assertTrue(any("fim de semana" in n
                            for n in m["interpretation"]["notes"]))
        self.assertEqual(len(m["weekday_profile"]), 7)

    def test_score_uses_realistic_daily_with_scaled_anchor(self):
        o = {"profit": 50_000, "roi_pct": 30, "confidence_score": 100,
             "liquidity_day": 200, "daily_potential": 1_000_000,
             "daily_realistic": 200_000, "capture_rate": 0.2}
        app._score_recommendations([o])
        # todos os fatores nas âncoras -> score máximo
        self.assertAlmostEqual(o["opportunity_score"], 100, delta=0.5)
        self.assertEqual(o["opportunity_label"], "executar")


class MicrostructureTests(unittest.TestCase):
    def _price_row(self, item, city, q, smin, smax, bmin, bmax, fetched):
        return ("americas", item, city, q, smin, DATE, smax, DATE,
                bmin, DATE, bmax, DATE, fetched)

    def test_market_making_filters_traps_and_bm(self):
        import time
        from albion import microstructure as mc
        now = time.time()
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                rows = [
                    # MM real: spread positivo plausível em 3 cidades
                    self._price_row("T4_POT", "Martlock", 1, 100, 110, 70, 80, now),
                    self._price_row("T4_POT", "Lymhurst", 1, 102, 112, 71, 81, now),
                    self._price_row("T4_POT", "Thetford", 1, 99, 109, 69, 79, now),
                    # ordem-âncora (outlier): venda absurda numa cidade
                    self._price_row("T4_POT", "Caerleon", 1, 100000, 100000,
                                    70, 80, now),
                    # livro cruzado: venda <= compra -> descartado
                    self._price_row("T4_BAG", "Martlock", 1, 50, 60, 40, 90, now),
                    # Mercado Negro: não é MM-able -> excluído
                    self._price_row("T4_SWORD", "Black Market", 1, 500, 0,
                                    0, 200, now),
                ]
                client.db.executemany(
                    "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                # liquidez para o item de MM (history diário)
                client.db.executemany(
                    "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)",
                    [("americas", "T4_POT", "Martlock", 1, 24,
                      "2026-06-1%dT00:00:00" % d, 500, 100, now)
                     for d in range(1, 6)])
                client.db.commit()
                menu = mc.market_making_menu(client.db, "americas",
                                             premium=True, max_age_min=None)
                traps = mc.trap_signals(client.db, "americas", max_age_min=None)
            finally:
                client.db.close()

        cities = {m["city"] for m in menu}
        self.assertIn("Martlock", cities)
        self.assertNotIn("Black Market", cities)        # BM excluído
        self.assertNotIn("Caerleon", cities)            # âncora outlier filtrada
        self.assertTrue(all(m["item_id"] != "T4_BAG" for m in menu))  # cruzado
        mm = next(m for m in menu if m["city"] == "Martlock")
        # net = 100*(1-0.04-0.025) - 80*(1.025) = 93.5 - 82 = 11.5
        self.assertAlmostEqual(mm["net_per_unit"], 11.5, places=1)
        self.assertEqual(mm["liquidity_day"], 500)

        flags = {(t["item_id"], t["city"]): t["flags"] for t in traps}
        self.assertIn("cruzado", flags[("T4_BAG", "Martlock")])
        self.assertIn("outlier", flags[("T4_POT", "Caerleon")])


class ProductionTests(unittest.TestCase):
    def test_raw_resource_classification(self):
        from albion import production as prod
        self.assertTrue(prod.is_raw_resource("T5_ORE"))
        self.assertTrue(prod.is_raw_resource("T6_HIDE_LEVEL1@1"))
        self.assertFalse(prod.is_raw_resource("T5_METALBAR"))
        self.assertFalse(prod.is_raw_resource("T6_2H_BOW"))
        # transmutação de bruto é ignorada na cadeia de produção
        self.assertIsNone(prod._prod_recipe("T5_ORE"))

    def test_rrr_refining_family_matches_dump(self):
        from albion import production as prod
        rec = {"category": None}
        # ore refina em Thetford: 15,3% fora / 53,9% na cidade-bônus com foco
        self.assertAlmostEqual(
            prod.rrr_for("T5_METALBAR", rec, "Lymhurst", False) * 100, 15.3, places=1)
        self.assertAlmostEqual(
            prod.rrr_for("T5_METALBAR", rec, "Thetford", True) * 100, 53.9, places=1)

    def test_focus_efficiency_uses_refining_rrr(self):
        from albion import production as prod
        # receita real T4_METALBAR = 2x T4_ORE + 1x T3_METALBAR, foco 54
        price = {
            ("T4_METALBAR", "Thetford"): 1000,
            ("T4_ORE", "Thetford"): 100,
            ("T3_METALBAR", "Thetford"): 200,
        }
        res = prod.focus_efficiency(price, premium=True, sell_mode="order",
                                    cities=["Thetford"])
        row = next(r for r in res if r["item_id"] == "T4_METALBAR")
        self.assertTrue(row["is_refining"])
        # eff c/foco = 400*(1-0.5392)=184,3; venda líq=935; margem=750,7; /54=13,9
        self.assertAlmostEqual(row["silver_per_focus"], 13.9, delta=0.3)
        self.assertGreater(row["focus_gain"], 0)


class LogisticsTests(unittest.TestCase):
    def test_clean_price_rows_zeroes_outlier(self):
        from albion.microstructure import clean_price_rows
        rows = [row("T4_X", c, 1, sell=s) for c, s in
                [("Martlock", 100), ("Lymhurst", 105), ("Thetford", 98),
                 ("Caerleon", 10_000_000)]]
        out = {r["city"]: r for r in clean_price_rows(rows)}
        self.assertEqual(out["Caerleon"]["sell_price_min"], 0)   # âncora zerada
        self.assertEqual(out["Martlock"]["sell_price_min"], 100)  # mantida

    def test_cargo_knapsack_respects_caps(self):
        from albion import logistics as logi
        flips = [
            {"item_id": "A", "name_pt": "A", "profit": 100, "weight": 1,
             "buy_city": "X", "sell_city": "Y", "quality": 1},   # 100/kg
            {"item_id": "B", "name_pt": "B", "profit": 50, "weight": 1,
             "buy_city": "X", "sell_city": "Y", "quality": 1},   # 50/kg
        ]
        vol = {"A": 50, "B": 1000}        # cap A = 50*0.2=10 unid
        res = logi.cargo_knapsack(flips, lambda i: vol.get(i, 0), w_max=15,
                                  capture_rate=0.2)
        basket = {b["item_id"]: b for b in res["basket"]}
        # A é mais lucrativa/kg: enche o cap (10 unid, 10kg), depois B nos 5kg
        self.assertEqual(basket["A"]["units"], 10)
        self.assertEqual(basket["B"]["units"], 5)
        self.assertAlmostEqual(res["used_kg"], 15)
        self.assertEqual(res["trip_profit"], 10 * 100 + 5 * 50)

    def test_bm_premium_uses_correct_fee_no_setup(self):
        from albion import logistics as logi
        # BM compra a 1000; melhor real instantâneo = ordem de compra de 900
        rows = [row("T4_SWORD", "Black Market", 1, buy=1000),
                row("T4_SWORD", "Martlock", 1, buy=900)]
        meta = {"T4_SWORD": {"pt": "Espada", "cat": "weapons", "w": 5}}
        res = logi.black_market_premium(rows, meta, premium=True)
        self.assertEqual(len(res), 1)
        r = res[0]
        # BM net = 1000*(1-0.04) = 960 (sem taxa de anúncio); real = 900*(1-0.04)=864
        self.assertEqual(r["bm_net"], 960)
        self.assertEqual(r["best_city_net"], 864)


class RiskTests(unittest.TestCase):
    def test_risk_profile_drawdown_and_label(self):
        from albion import risk
        rp = risk.risk_profile([100, 120, 60, 90], min_points=3)
        self.assertEqual(rp["max_drawdown_pct"], -50.0)   # 120 -> 60
        self.assertEqual(rp["risk_label"], "especulativo")  # vol enorme
        flat = risk.risk_profile([100, 100.5, 100.2, 100.4, 100.1, 100.3],
                                 min_points=3)
        self.assertEqual(flat["risk_label"], "seguro")

    def test_position_size_caps(self):
        from albion import risk
        s = risk.position_size(profit_per_unit=100, buy_price=1000,
                               vol_annual=0.365, liquidity_day=100,
                               persistence=1.0, capital=1_000_000)
        self.assertEqual(s["units"], 20)            # 100*0.2*1 < capital cap
        self.assertEqual(s["limited_by"], "liquidez")

    def test_correlation_identical_series(self):
        from albion import risk
        a = {f"d{i}": 100 + i * 5 + (i % 2) for i in range(8)}
        series = {"A": dict(a), "B": dict(a)}
        pairs = risk.correlation_pairs(series, min_common=4)
        self.assertEqual(len(pairs), 1)
        self.assertAlmostEqual(pairs[0]["corr"], 1.0, places=3)


if __name__ == "__main__":
    unittest.main()

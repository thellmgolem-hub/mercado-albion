import os
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

# A suite legada testa a API economica sem precisar fabricar sessoes. Os
# fluxos de autenticacao sao cobertos isoladamente abaixo.
os.environ.setdefault("ALBION_AUTH_DISABLED", "1")

import app
from albion.auth import AuthError, AuthManager
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
                # regressão: o endpoint usa row_factory=Row (não ordenável).
                # client.db agora é o wrapper store._SqliteConn; o row_factory
                # vive na conexão crua (.raw), senão o guard vira no-op.
                import sqlite3 as _sq
                client.db.raw.row_factory = _sq.Row
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

    def test_ingest_no_false_saturation_on_empty_page(self):
        # regressão: run incremental calmo (checkpoint existe, página 0 traz
        # novos, página 1 vazia) NÃO pode marcar saturação (B5)
        from albion import gameinfo
        ts = "2026-06-16T12:00:00.000Z"

        def ev(eid):
            return {"EventId": eid, "TimeStamp": ts, "Type": "KILL",
                    "KillArea": "OPEN_WORLD", "Killer": {"Id": "k"},
                    "Victim": {"Id": "v"}, "Participants": []}

        class Fake:
            def __init__(self, pages):
                self.pages = pages

            def events_page(self, offset):
                return self.pages.get(offset // 51, [])

        with TemporaryDirectory() as tmp:
            aodp = AODP(db_path=Path(tmp) / "cache.db")
            try:
                # 1ª carga: define o checkpoint
                gameinfo.ingest_events(aodp, Fake({0: [ev(100), ev(99)]}), max_pages=18)
                # 2ª carga: só novos na página 0, página 1 vazia (API esgotou)
                r = gameinfo.ingest_events(
                    aodp, Fake({0: [ev(102), ev(101)]}), max_pages=18)
            finally:
                aodp.db.close()
        self.assertEqual(r["inserted"], 2)
        self.assertFalse(r["saturated"])      # página vazia != burst


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


class RollupTests(unittest.TestCase):
    def test_rollup_daily_aggregates_snapshots(self):
        import time as _t
        now = _t.time()

        def snap(item, fetched, sell, buy):
            return ("americas", item, "Martlock", 1, sell, "d", 0, "d",
                    0, "d", buy, "d", fetched)
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                client.db.executemany(
                    "INSERT INTO price_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [snap("T4_X", now - 3600, 100, 80),
                     snap("T4_X", now - 1800, 120, 90)])
                client.db.commit()
                client._rollup_daily(["T4_X"], days=2)
                row = client.db.execute(
                    "SELECT sell_min, sell_max, buy_max, samples "
                    "FROM price_snapshots_daily WHERE item_id='T4_X'").fetchone()
            finally:
                client.db.close()
        self.assertIsNotNone(row)            # daily deixou de ficar vazio
        self.assertEqual(row[0], 100)        # sell_min
        self.assertEqual(row[1], 120)        # sell_max
        self.assertEqual(row[2], 90)         # buy_max
        self.assertEqual(row[3], 2)          # 2 snapshots agregados

    def test_rollup_boundary_day_is_complete(self):
        # regressão: o dia de fronteira deve agregar TODOS os seus snapshots,
        # não só os que caem na janela deslizante (cutoff alinhado à meia-noite)
        from datetime import datetime, timezone, timedelta
        midnight2 = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        early = (midnight2 + timedelta(hours=2)).timestamp()    # 02:00
        late = (midnight2 + timedelta(hours=20)).timestamp()    # 20:00

        def snap(fetched, sell):
            return ("americas", "T4_Y", "Martlock", 1, sell, "d", 0, "d",
                    0, "d", 0, "d", fetched)
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                client.db.executemany(
                    "INSERT INTO price_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [snap(early, 50), snap(late, 200)])
                client.db.commit()
                client._rollup_daily(["T4_Y"], days=3)   # janela inclui o dia -2
                row = client.db.execute(
                    "SELECT sell_min, sell_avg, sell_max, samples "
                    "FROM price_snapshots_daily WHERE item_id='T4_Y'").fetchone()
            finally:
                client.db.close()
        self.assertEqual(row[0], 50)         # manhã não foi perdida
        self.assertEqual(row[2], 200)        # tarde presente
        self.assertEqual(row[3], 2)          # dia inteiro, não parcial
        self.assertAlmostEqual(row[1], 125)  # avg dos 2


class RigorIIITests(unittest.TestCase):
    def test_meta_shift_gates_low_count_noise(self):
        from datetime import datetime, timezone, timedelta
        from albion import demand as dm
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                eid = 0
                # 1 build raro (2 mortes) + 1 build comum (30 mortes) na janela
                def add(weapon, armor, k):
                    nonlocal eid
                    for _ in range(k):
                        eid += 1
                        ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
                        client.db.execute(
                            "INSERT INTO kill_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("americas", eid, ts, 0, "kill", "OPEN", None, 0, 4,
                             1, "k", "v", 0))
                        client.db.execute(
                            "INSERT INTO kill_event_equipment VALUES (?,?,?,?,?,?,?)",
                            ("americas", eid, "victim", "MainHand", weapon, 1, 1))
                        client.db.execute(
                            "INSERT INTO kill_event_equipment VALUES (?,?,?,?,?,?,?)",
                            ("americas", eid, "victim", "Armor", armor, 1, 1))
                add("T4_2H_BOW", "T4_ARMOR_LEATHER_SET1", 30)
                add("T4_MAIN_SWORD", "T4_ARMOR_PLATE_SET1", 2)
                client.db.commit()
                res = dm.meta_shift(client.db, "americas", days=7, recent=7,
                                    min_recent_n=15)
            finally:
                client.db.close()
        builds = {r["build"] for r in res}
        self.assertIn("2H_BOW + ARMOR_LEATHER_SET1", builds)   # 30 >= 15
        self.assertNotIn("MAIN_SWORD + ARMOR_PLATE_SET1", builds)  # 2 < 15 gated

    def test_capital_fill_rate_scales_turnover(self):
        from albion import microstructure as mc
        import time as _t
        now = _t.time()
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                client.db.execute(
                    "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("americas", "T4_POT", "Martlock", 1, 110, DATE, 0, DATE,
                     0, DATE, 80, DATE, now))
                client.db.executemany(
                    "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)",
                    [("americas", "T4_POT", "Martlock", 1, 24,
                      "2026-06-1%dT00:00:00" % d, 1000, 100, now)
                     for d in range(1, 6)])
                client.db.commit()
                full = mc.capital_allocation(client.db, "americas",
                                             capital=10_000_000, fill_rate=1.0)
                half = mc.capital_allocation(client.db, "americas",
                                             capital=10_000_000, fill_rate=0.5)
            finally:
                client.db.close()
        pf = next(p for p in full["plan"] if p["item_id"] == "T4_POT")
        ph = next(p for p in half["plan"] if p["item_id"] == "T4_POT")
        # metade do fill -> metade do giro/dia capturável (e do lucro/dia)
        self.assertAlmostEqual(ph["profit_day"], pf["profit_day"] / 2, delta=1)


class ChainFarmTests(unittest.TestCase):
    def test_chain_city_ranks_and_marks_bonus(self):
        from albion import production as prod
        cities = ["Lymhurst", "Thetford", "Bridgewatch"]
        # T4_2H_BOW = 32x T4_PLANKS (categoria bow, bônus em Lymhurst)
        price = {("T4_2H_BOW", c): 10000 for c in cities}
        price.update({("T4_PLANKS", c): 200 for c in cities})
        res = prod.chain_city("T4_2H_BOW", price,
                              weight_of=lambda i: 0.5, cities=cities)
        self.assertTrue(res)
        bonus = [r for r in res if r["is_bonus_city"]]
        self.assertTrue(bonus)
        # a cidade-bônus tem RRR de craft maior que as demais
        self.assertGreater(bonus[0]["rrr_pct"],
                           min(r["rrr_pct"] for r in res if not r["is_bonus_city"]))
        self.assertEqual(res, sorted(res, key=lambda r: -r["margin"]))

    def test_farm_economy_profit_per_day(self):
        from albion import production as prod
        cities = ["Martlock"]
        # cadeia real: T3_FARM_OX_BABY -> T3_FARM_OX_GROWN (growtime 158400s, off 0.84)
        price = {("T3_FARM_OX_BABY", "Martlock"): 100,
                 ("T3_FARM_OX_GROWN", "Martlock"): 1000}
        res = prod.farm_economy(price, cities=cities)
        self.assertTrue(res["available"])
        row = next((r for r in res["rows"] if r["baby"] == "T3_FARM_OX_BABY"), None)
        self.assertIsNotNone(row)
        # yield 1.84; receita 1.84*935; lucro/(158400/86400=1.833d)
        self.assertGreater(row["profit_per_day"], 800)
        self.assertLess(row["profit_per_day"], 950)


class EstimatorTests(unittest.TestCase):
    def test_winsorize_caps_extremes(self):
        from albion import risk
        xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 1000]   # 1000 é outlier (cauda alta)
        w = risk.winsorize(xs, p=10)
        self.assertEqual(len(w), len(xs))
        self.assertEqual(max(w), 9)               # 1000 -> p90 (=9)
        self.assertEqual(min(w), 2)               # 1   -> p10 (=2), capa as 2 caudas

    def test_shrink_pulls_short_samples(self):
        from albion import risk
        # n pequeno -> perto do prior; n grande -> perto do estimador
        self.assertAlmostEqual(risk.shrink(100, 2, 40, k=20), 100 * (2 / 22) + 40 * (20 / 22), places=1)
        self.assertGreater(risk.shrink(100, 200, 40, k=20), 90)   # confia no item

    def test_bootstrap_ci_brackets_value(self):
        from albion import risk
        xs = [0.01 * (i % 5 - 2) for i in range(40)]   # retornos ~simétricos
        ci = risk._bootstrap_ci(xs, lambda v: risk._pstdev(v))
        self.assertIsNotNone(ci)
        self.assertLessEqual(ci[0], ci[1])

    def test_km_survival_curve_monotone(self):
        from datetime import datetime, timezone
        from albion import survival
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()

        def iso(e):
            return datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        def snap(item, fetched, sp, sd):
            return ("americas", item, "Martlock", 1, sp, sd, 0,
                    "0001-01-01T00:00:00", 0, "0001-01-01T00:00:00", 0,
                    "0001-01-01T00:00:00", fetched)
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                rows = [snap("T4_A", base, 100, iso(base - 15 * 60)),
                        snap("T4_A", base + 1800, 100, iso(base - 15 * 60))]
                client.db.executemany(
                    "INSERT INTO price_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
                client.db.commit()
                res = survival.persistence(client.db, "americas", days=None)
            finally:
                client.db.close()
        buckets = res["sides"]["sell"]["buckets"]
        kms = [b["km_survival_pct"] for b in buckets if b["km_survival_pct"] is not None]
        self.assertTrue(kms)                       # curva KM presente
        self.assertTrue(all(a >= b - 1e-9 for a, b in zip(kms, kms[1:])))  # não-crescente


class FusionTests(unittest.TestCase):
    def test_composite_blends_signals(self):
        from albion import fusion
        base = 60
        self.assertEqual(fusion.composite(base)[0], 60)               # sem fatores
        # risco alto penaliza
        self.assertLess(fusion.composite(base,
                        risk_profile={"vol_annual_pct": 100})[0], 60)
        # previsibilidade nula encolhe via porteiro (60*0.6=36)
        c2, _ = fusion.composite(base, predictability={"predictability": 0.0})
        self.assertAlmostEqual(c2, 36, delta=1)
        # reversão a favor + divergência bonificam
        c3, _ = fusion.composite(base, reversion={"signal": True,
                                 "direction": "comprar", "z_resid": -2.5},
                                 divergence=True)
        self.assertGreater(c3, 60)
        # clamp em 0..100
        self.assertLessEqual(fusion.composite(200)[0], 100)
        self.assertGreaterEqual(fusion.composite(5,
                                risk_profile={"vol_annual_pct": 500})[0], 0)


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


class RigorTests(unittest.TestCase):
    def test_calibrate_bands_tercis_and_label(self):
        from albion import risk
        vols = [i / 100 for i in range(3, 99, 3)]   # 0.03..0.96
        bands = risk.calibrate_bands(vols)
        self.assertEqual(len(bands), 2)
        self.assertLess(bands[0][0], bands[1][0])     # t1 < t2
        # selo relativo: vol baixa -> seguro; alta -> especulativo
        self.assertEqual(risk.label_for(0.01, bands), "seguro")
        self.assertEqual(risk.label_for(5.0, bands), "especulativo")

    def test_ewma_vol_present_and_responsive(self):
        from albion import risk
        # série calma e depois turbulenta: EWMA deve superar a vol estática
        calm = [100 + (i % 2) for i in range(40)]
        shock = [100, 130, 90, 140, 80, 150]
        rp = risk.risk_profile(calm + shock, min_points=20)
        self.assertIsNotNone(rp["vol_ewma_pct"])
        self.assertGreater(rp["vol_ewma_pct"], rp["vol_annual_pct"])

    def test_refine_premium_history_percentile(self):
        from albion import production as prod
        city = "Thetford"
        days = [f"2026-05-{d:02d}" for d in range(1, 26)]   # 25 dias
        # insumos constantes; produto subindo -> prêmio crescente, hoje no topo
        hist = {("T5_ORE", city): {d: 100 for d in days},
                ("T4_METALBAR", city): {d: 200 for d in days},
                ("T5_METALBAR", city): {d: 500 + i * 8 for i, d in enumerate(days)}}
        hp = prod.refine_premium_history("T5_METALBAR", hist, city=city,
                                         min_days=20)
        self.assertIsNotNone(hp)
        self.assertEqual(hp["days"], 25)
        self.assertGreaterEqual(hp["percentile"], 90)        # hoje é o topo
        self.assertIn("refinar", hp["verdict"])

    def test_backtest_reversion_rewards_reverting_series(self):
        import math
        import random
        from albion import forecast as fc
        # processo AR(1) mean-reverting (semente fixa -> determinístico): o
        # sinal de reversão deve disparar e ACERTAR a maioria das vezes
        rng = random.Random(42)
        x, base, prices = 0.0, math.log(1000), []
        for _ in range(160):
            x = 0.6 * x + rng.gauss(0, 0.06)
            prices.append(math.exp(base + x))
        bt = fc.backtest_reversion(prices, min_history=25)
        self.assertIsNotNone(bt)
        self.assertGreater(bt["n_signals"], 5)
        self.assertGreaterEqual(bt["hit_rate_pct"], 70)   # reversão real paga
        self.assertGreater(bt["avg_edge_pct"], 0)
        # série curta demais -> None
        self.assertIsNone(fc.backtest_reversion([100, 101, 102], min_history=25))

    def test_structural_break_detects_level_shift(self):
        from albion import forecast as fc
        # 30 dias ~100, depois 30 dias ~200: quebra de NÍVEL no meio
        prices = [100 + (i % 3) for i in range(30)] + [200 + (i % 3) for i in range(30)]
        br = fc.structural_break(prices, permutations=80)
        self.assertIsNotNone(br)
        self.assertTrue(br["significant"])
        self.assertEqual(br["kind"], "nível")
        self.assertAlmostEqual(br["break_frac"], 0.5, delta=0.12)
        self.assertGreater(br["level_change_pct"], 50)


class ForecastTests(unittest.TestCase):
    def test_mean_reversion_direction_and_sanity_guard(self):
        from albion import forecast as fc
        # série em tendência de alta, mas o último ponto cai abaixo da tendência
        prices = [100 + i for i in range(29)] + [110]
        r = fc.mean_reversion(prices, min_points=10)
        self.assertEqual(r["direction"], "comprar")   # z < 0
        self.assertEqual(r["current"], 110)
        # ponto final anômalo (10x a mediana) não vira sinal
        wild = [100 + (i % 5) for i in range(30)] + [1000]
        rw = fc.mean_reversion(wild, min_points=10)
        self.assertFalse(rw["signal"])

    def test_predictability_returns_score(self):
        import math
        from albion import forecast as fc
        prices = [math.exp(0.01 * i) * 100 for i in range(40)]
        r = fc.predictability(prices, min_points=20)
        self.assertIn(r["label"], ("modelável", "cautela", "ruído"))
        self.assertTrue(0 <= r["predictability"] <= 1)
        self.assertGreater(r["trend_r2"], 0.9)        # série quase log-linear

    def test_pair_trade_needs_length_and_returns_beta(self):
        from albion import forecast as fc
        self.assertIsNone(fc.pair_trade([1, 2, 3], [1, 2, 3], min_points=60))
        a = [100 + i + (i % 3) for i in range(70)]
        b = [50 + 0.5 * i + (i % 2) for i in range(70)]
        r = fc.pair_trade(a, b, min_points=60)
        self.assertIsNotNone(r)
        self.assertIn("beta", r)
        self.assertIn(r["action"],
                      ("vender A / comprar B", "comprar A / vender B"))


class DemandGuildTests(unittest.TestCase):
    def test_item_family(self):
        from albion import demand as dm
        self.assertEqual(dm.item_family("T4_MAIN_AXE@1"), "MAIN_AXE")
        self.assertEqual(dm.item_family("T8_2H_BOW"), "2H_BOW")
        # canal de artefato funde na linha-base (base_only padrão)
        self.assertEqual(dm.item_family("T4_2H_BOW_KEEPER@4"), "2H_BOW")
        self.assertEqual(dm.item_family("T4_2H_DUALCROSSBOW_HELL@2"),
                         "2H_DUALCROSSBOW")
        # mas dá para manter a distinção do artefato quando se quer
        self.assertEqual(dm.item_family("T4_2H_BOW_KEEPER@4", base_only=False),
                         "2H_BOW_KEEPER")

    def test_consumable_burn_normalizes_by_exposure(self):
        from datetime import datetime, timezone, timedelta
        from albion import demand as dm
        now = datetime.now(timezone.utc)
        # 2 kills em 2 HORAS distintas (não 2 dias) -> exposição = 2h
        rows = [(now - timedelta(hours=2), 1, 30),
                (now - timedelta(hours=3), 2, 18)]
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                for ts_dt, eid, units in rows:
                    ts = ts_dt.strftime("%Y-%m-%dT%H:%M:%S")
                    client.db.execute(
                        "INSERT INTO kill_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("americas", eid, ts, 0, "kill", "OPEN", None, 0, 4, 1,
                         "k", "v", 0))
                    client.db.execute(
                        "INSERT INTO kill_event_equipment VALUES (?,?,?,?,?,?,?)",
                        ("americas", eid, "victim", "Potion", "T8_POT", units, 1))
                client.db.commit()
                res = dm.consumable_burn(
                    client.db, "americas", days=7,
                    price_of=lambda i: 100, vol_of=lambda i: 5)
            finally:
                client.db.close()
        r = next(x for x in res if x["item_id"] == "T8_POT")
        self.assertEqual(r["burned_units"], 48)
        # exposição = 2h = 2/24 dia; per_day = 48 / (2/24) = 576 (não 48/7)
        self.assertAlmostEqual(r["per_day"], 576.0, delta=1)
        self.assertTrue(r["undersupplied"])                    # 5 << 576

    def test_watchlist_roi_ranks_destruction(self):
        # ranking de destruição sobre kill_demand_daily (killboard magro):
        # mais destruído ranqueia mais alto; sem conceito de watchlist no piloto
        from albion import guild as gd
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                client.db.executemany(
                    "INSERT INTO kill_demand_daily VALUES (?,?,?,?,?,?,?)",
                    [("americas", "2026-06-15", "T6_BAG", "Bag", 1, 100, 1),
                     ("americas", "2026-06-15", "T7_BAG", "Bag", 1, 20, 1)])
                client.db.commit()
                res = gd.watchlist_roi(client.db, "americas",
                                       price_of=lambda i: 1000, days=3650)
            finally:
                client.db.close()
        ranked = [r["item_id"] for r in res["add"]]
        self.assertIn("T6_BAG", ranked)
        self.assertIn("T7_BAG", ranked)
        self.assertLess(ranked.index("T6_BAG"), ranked.index("T7_BAG"))
        self.assertEqual(res["watched_count"], 0)


class PvpTests(unittest.TestCase):
    def test_weapon_meta_win_rate(self):
        from datetime import datetime, timezone, timedelta
        from albion import pvp
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        with TemporaryDirectory() as tmp:
            client = AODP(db_path=Path(tmp) / "cache.db")
            try:
                # 3 confrontos: AXE vence 2 (killer), perde 1 (vítima); BOW o inverso
                fights = [(1, "T4_2H_AXE", "T4_2H_BOW"),
                          (2, "T4_2H_AXE", "T4_2H_BOW"),
                          (3, "T4_2H_BOW", "T4_2H_AXE")]
                for eid, killer_w, victim_w in fights:
                    client.db.execute(
                        "INSERT INTO kill_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("americas", eid, ts, 0, "kill", "OPEN", None, 0, 2, 1,
                         "k", "v", 0))
                    client.db.execute(
                        "INSERT INTO kill_event_equipment VALUES (?,?,?,?,?,?,?)",
                        ("americas", eid, "killer", "MainHand", killer_w, 1, 1))
                    client.db.execute(
                        "INSERT INTO kill_event_equipment VALUES (?,?,?,?,?,?,?)",
                        ("americas", eid, "victim", "MainHand", victim_w, 1, 1))
                client.db.commit()
                res = pvp.weapon_meta(client.db, "americas", days=7, min_fights=1)
                # janela FRACIONÁRIA deve ser honrada (eventos em now-1h):
                # cutoff -12h inclui; com int(0.5)=0 dias daria -0d (=now) e zeraria
                frac = pvp.weapon_meta(client.db, "americas", days=0.5, min_fights=1)
            finally:
                client.db.close()
        self.assertEqual(frac["total_fights"], 6)   # bug do int(days) corrigido
        rows = {r["weapon"]: r for r in res["rows"]}
        self.assertEqual(rows["2H_AXE"]["wins"], 2)
        self.assertEqual(rows["2H_AXE"]["losses"], 1)
        self.assertAlmostEqual(rows["2H_AXE"]["win_rate_pct"], 66.7, delta=0.2)
        self.assertEqual(rows["2H_BOW"]["wins"], 1)
        self.assertEqual(rows["2H_BOW"]["losses"], 2)
        self.assertEqual(res["total_fights"], 6)   # 3 killers + 3 vítimas


class WikiTests(unittest.TestCase):
    """Camada wiki: índice reverso, cadeia recursiva, lista de compras."""
    def setUp(self):
        from albion import wiki
        from albion.items import ItemDB
        self.wiki = wiki
        db = ItemDB()
        self.name = lambda i: (db.get(i) or {}).get("pt", i)
        self.tier = lambda i: (db.get(i.split("@")[0]) or {}).get("tier")

    def test_used_in_inverts_recipes(self):
        # T4_ORE é insumo de T4_METALBAR (refino) -> aparece no índice reverso
        rows, total = self.wiki.used_in("T4_ORE", self.name)
        outs = {r["item_id"] for r in rows}
        self.assertIn("T4_METALBAR", outs)
        self.assertGreaterEqual(total, len(rows))

    def test_canonical_id_normalizes_enchanted_refined(self):
        # alias de craft X@n -> id real X_LEVELn@n (refinado encantado)
        self.assertEqual(self.wiki.canonical_id("T4_METALBAR@1"), "T4_METALBAR_LEVEL1@1")
        # equipamento encantado fica intacto (X@n é id real)
        self.assertEqual(self.wiki.canonical_id("T4_2H_CLAYMORE@1"), "T4_2H_CLAYMORE@1")
        # base sem encanto intacta
        self.assertEqual(self.wiki.canonical_id("T4_METALBAR"), "T4_METALBAR")

    def test_chain_ids_reach_raw(self):
        ids = self.wiki.chain_item_ids("T4_METALBAR")
        self.assertIn("T4_METALBAR", ids)
        self.assertIn("T4_ORE", ids)        # desce até o minério bruto

    def test_production_tree_expands_and_lists_raw(self):
        # preço fixo p/ todos; a árvore deve expandir e a lista somar o bruto
        res = self.wiki.production_tree(
            "T4_METALBAR", lambda i: 100, self.name, self.tier, qty=1)
        self.assertTrue(res["tree"]["children"])          # expandiu a receita
        shop_ids = {s["id"] for s in res["shopping"]}
        self.assertIn("T4_ORE", shop_ids)                 # bruto na lista
        # custo de matéria-prima > 0 com preço 100 em tudo
        self.assertGreater(res["raw_cost"] or 0, 0)


class ApiUiTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app.app)

    def test_item_signals_unknown_is_404(self):
        r = self.c.get('/api/item_signals', params={'item': 'LIXO_INEXISTENTE_XYZ'})
        self.assertEqual(r.status_code, 404)   # antes 400 (dead code na 944)

    def test_micro_endpoint_shapes(self):
        r = self.c.get('/api/micro', params={'view': 'spread', 'limit': 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get('view'), 'spread')
        r2 = self.c.get('/api/micro', params={'view': 'capital', 'limit': 3})
        self.assertEqual(r2.status_code, 200)
        self.assertIn('plan', r2.json())

    def test_recommendations_fused_param_ok(self):
        r = self.c.get('/api/recommendations',
                       params={'fused': 'true', 'min_daily_volume': 0, 'limit': 3})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get('fused'))

    def test_prod_endpoint_views(self):
        # hub Avançado/Produção: cada view responde 200 com a forma esperada
        for view in ('focus', 'refine'):
            r = self.c.get('/api/prod', params={'view': view, 'limit': 3})
            self.assertEqual(r.status_code, 200, view)
            self.assertEqual(r.json().get('view'), view)
            self.assertIsInstance(r.json().get('rows'), list)

    def test_guild_endpoint_views(self):
        # Guild revivido sobre kill_demand_daily (killboard magro): watch/
        # makeorbuy devolvem rows, kit devolve series — listas mesmo com
        # agregado vazio (sem quebrar).
        for view in ('watch', 'makeorbuy'):
            r = self.c.get('/api/guild', params={'view': view, 'days': 7, 'limit': 3})
            self.assertEqual(r.status_code, 200, view)
            self.assertEqual(r.json().get('view'), view)
            self.assertIsInstance(r.json().get('rows'), list)
        rk = self.c.get('/api/guild', params={'view': 'kit', 'days': 7})
        self.assertEqual(rk.status_code, 200)
        self.assertIsInstance(rk.json().get('series'), list)

    def test_logi_endpoint_views(self):
        # hub Avançado/Logística: bm/ladder/restock todos 200 com rows
        for view in ('bm', 'ladder', 'restock'):
            r = self.c.get('/api/logi', params={'view': view, 'days': 7, 'limit': 3})
            self.assertEqual(r.status_code, 200, view)
            self.assertEqual(r.json().get('view'), view)
            self.assertIsInstance(r.json().get('rows'), list)

    def test_sweep_universe_and_token_gate(self):
        # universo de varredura não-vazio e em ordem estável (cursor = offset)
        uni = app._market_universe()
        self.assertGreater(len(uni), 1000)
        self.assertEqual(uni, sorted(uni))
        # com token configurado, requisição com token errado -> 403 (não busca API)
        app.config.SWEEP_TOKEN = "segredo"
        try:
            r = self.c.get('/api/sweep', params={'token': 'errado', 'count': 10})
            self.assertEqual(r.status_code, 403)
        finally:
            app.config.SWEEP_TOKEN = ""

    def test_risk_endpoint_views(self):
        # hub Avançado/Risco: profile e corr respondem 200 com rows; o perfil
        # limita o universo aos itens mais líquidos (bootstrap caro)
        rp = self.c.get('/api/risk', params={'view': 'profile', 'limit': 5})
        self.assertEqual(rp.status_code, 200)
        self.assertEqual(rp.json().get('view'), 'profile')
        self.assertIsInstance(rp.json().get('rows'), list)
        rc = self.c.get('/api/risk', params={'view': 'corr', 'limit': 5})
        self.assertEqual(rc.status_code, 200)
        self.assertEqual(rc.json().get('view'), 'corr')
        self.assertIsInstance(rc.json().get('rows'), list)

    def test_wiki_endpoint_shape(self):
        r = self.c.get('/api/wiki', params={'item': 'T4_2H_CLAYMORE'})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn('details', j)
        self.assertIn('tree', j)
        self.assertIsInstance(j.get('shopping'), list)
        self.assertIsInstance(j.get('used_in'), list)
        self.assertTrue(j['tree'].get('children'))   # claymore expande a cadeia

    def test_risk_empty_filter_short_circuits(self):
        # filtro sem match => item_ids vazio: NÃO pode cair no caminho ilimitado
        # (IN vazio varreria tudo). Deve voltar vazio na hora.
        r = self.c.get('/api/risk', params={'view': 'profile', 'cat': 'XYZ_INEXISTENTE'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get('rows'), [])


class CraftStudioTests(unittest.TestCase):
    """Estúdio de craft: lote (output), custo de foco por spec, sourcing,
    melhor venda e teto anti-âncora."""
    def setUp(self):
        from albion import craft
        self.craft = craft
        # receita sintética: 2 insumos, sai em lote de 5, categoria potion
        self.recipe = {"inputs": [{"id": "A", "count": 8}, {"id": "B", "count": 4}],
                       "focus": 84, "output": 5, "category": "potion"}

    def test_focus_cost_halves_per_10000_fce(self):
        self.assertAlmostEqual(self.craft.focus_cost(84, 0), 84.0)
        self.assertAlmostEqual(self.craft.focus_cost(84, 10000), 42.0)
        self.assertAlmostEqual(self.craft.focus_cost(84, 20000), 21.0)

    def test_output_quantity_multiplies_revenue(self):
        acq = {("A", "Caerleon"): 100, ("B", "Caerleon"): 50,
               ("PROD", "Caerleon"): 1000}
        rows = self.craft.studio(
            "PROD", self.recipe, lambda i, c: acq.get((i, c)),
            lambda i, c: None, premium=True, sell_mode="order",
            source_cities=["Caerleon"], sell_cities=["Caerleon"])
        self.assertTrue(rows)
        r = rows[0]
        self.assertEqual(r["output"], 5)
        # receita = 5 × venda líquida de 1 unidade (ordem: 1 - 0.04 - 0.025)
        self.assertEqual(r["revenue"], round(5 * 1000 * (1 - 0.04 - 0.025)))

    def test_sources_each_input_cheapest_and_sells_best(self):
        acq = {("A", "Thetford"): 100, ("A", "Lymhurst"): 130,
               ("B", "Lymhurst"): 40, ("B", "Thetford"): 60,
               ("PROD", "Caerleon"): 1000, ("PROD", "Thetford"): 1200}
        rows = self.craft.studio(
            "PROD", self.recipe, lambda i, c: acq.get((i, c)),
            lambda i, c: None, premium=True, sell_mode="order",
            source_cities=["Thetford", "Lymhurst"],
            sell_cities=["Caerleon", "Thetford"])
        best = rows[0]
        buys = {s["id"]: s["buy_city"] for s in best["sourcing"]}
        self.assertEqual(buys["A"], "Thetford")    # A mais barato em Thetford
        self.assertEqual(buys["B"], "Lymhurst")    # B mais barato em Lymhurst
        self.assertEqual(best["sell_city"], "Thetford")  # melhor venda (1200>1000)

    def test_bonus_city_has_higher_rrr(self):
        acq = {("A", c): 100 for c in ("Brecilien", "Thetford")}
        acq.update({("B", c): 50 for c in ("Brecilien", "Thetford")})
        acq.update({("PROD", "Caerleon"): 1000})
        rows = self.craft.studio(
            "PROD", self.recipe, lambda i, c: acq.get((i, c)),
            lambda i, c: None, premium=True, sell_mode="order",
            source_cities=["Brecilien", "Thetford"], sell_cities=["Caerleon"])
        by_city = {r["craft_city"]: r for r in rows}
        # potion tem bônus em Brecilien -> RRR maior que numa cidade sem bônus
        self.assertGreater(by_city["Brecilien"]["rrr_pct"],
                           by_city["Thetford"]["rrr_pct"])
        self.assertTrue(by_city["Brecilien"]["is_bonus_city"])

    def test_refined_resource_uses_refining_specialty(self):
        # refinado (planks=wood) deve pegar a especialidade +40% na cidade-bônus,
        # não cair no craft_rrr só-estação (15,2% em toda cidade)
        c = self.craft
        self.assertEqual(c.unified_bonus_city("T4_PLANKS", None), "Fort Sterling")
        bonus = c.unified_rrr("T4_PLANKS", None, "Fort Sterling", False)
        base = c.unified_rrr("T4_PLANKS", None, "Thetford", False)
        self.assertGreater(bonus, base + 0.15)   # +40% de especialidade vira ~+21pp

    def test_band_drops_sell_anchor_and_buy_troll(self):
        c = self.craft
        # teto de venda (band) força a venda real, não a âncora de 1e6
        acq = {("A", "Caerleon"): 100, ("B", "Caerleon"): 50,
               ("PROD", "Caerleon"): 1000, ("PROD", "Thetford"): 1_000_000}
        band = {"PROD": (None, 5000), "A": (None, None), "B": (None, None)}
        rows = c.studio(
            "PROD", self.recipe, lambda i, x: acq.get((i, x)),
            lambda i, x: None, premium=True, sell_mode="order",
            source_cities=["Caerleon"], sell_cities=["Caerleon", "Thetford"],
            band_of=lambda i: band.get(i, (None, None)))
        self.assertEqual(rows[0]["sell_city"], "Caerleon")
        self.assertEqual(rows[0]["sell_unit"], 1000)
        # piso de compra (band) ignora a ordem-isca de 1 prata num insumo
        acq2 = {("A", "Bridgewatch"): 1, ("A", "Thetford"): 100,
                ("B", "Thetford"): 50, ("PROD", "Caerleon"): 1000}
        band2 = {"A": (30, None), "B": (None, None), "PROD": (None, None)}
        rows2 = c.studio(
            "PROD", self.recipe, lambda i, x: acq2.get((i, x)),
            lambda i, x: None, premium=True, sell_mode="order",
            source_cities=["Bridgewatch", "Thetford"], sell_cities=["Caerleon"],
            band_of=lambda i: band2.get(i, (None, None)))
        buys = {s["id"]: s["buy_city"] for s in rows2[0]["sourcing"]}
        self.assertEqual(buys["A"], "Thetford")    # isca de 1 prata descartada

    def test_anchor_band_detects_majority_anchor(self):
        c = self.craft
        # 5/7 cidades em ~1M, 2 reais ~5k: o salto pega o teto mesmo em maioria
        _, ceiling = c.anchor_band([5000, 5000, 1_000_000, 1_000_000, 1_000_000])
        self.assertIsNotNone(ceiling)
        self.assertLess(ceiling, 100_000)
        # piso pega a isca de 1 prata
        floor, _ = c.anchor_band([1, 250, 300])
        self.assertIsNotNone(floor)
        self.assertGreater(floor, 1)

    def test_bm_gate_blocks_non_combat_sell(self):
        c = self.craft
        # poção (não-combate) não pode vender no Mercado Negro mesmo com bid alto
        acq = {("A", "Caerleon"): 100, ("B", "Caerleon"): 50,
               ("PROD", "Caerleon"): 1000}
        bid = {("PROD", "Black Market"): 9000}
        rows = c.studio(
            "PROD", self.recipe, lambda i, x: acq.get((i, x)),
            lambda i, x: bid.get((i, x)), premium=True, sell_mode="order",
            source_cities=["Caerleon"], sell_cities=["Caerleon", "Black Market"])
        self.assertNotEqual(rows[0]["sell_city"], "Black Market")


class AuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.auth = AuthManager(self.con, threading.Lock())
        self.admin = self.auth.bootstrap_admin("admin.local")

    def tearDown(self):
        self.con.close()

    def test_bootstrap_hashes_password_and_closes_bootstrap(self):
        row = self.con.execute(
            "SELECT password_hash,password_salt,password_algo "
            "FROM auth_accounts WHERE id=?", [self.admin["account_id"]]
        ).fetchone()
        self.assertNotIn(self.admin["temporary_password"], row)
        self.assertTrue(row[2].startswith("scrypt$"))
        with self.assertRaises(AuthError) as ctx:
            self.auth.bootstrap_admin("outro.admin")
        self.assertEqual(ctx.exception.code, "bootstrap_closed")

    def test_failed_login_is_persisted_for_progressive_lockout(self):
        with self.assertRaises(AuthError):
            self.auth.login("admin.local", "senha definitivamente errada")
        row = self.con.execute(
            "SELECT failures FROM auth_login_throttle WHERE username_norm=?",
            ["admin.local"]).fetchone()
        self.assertEqual(row[0], 1)

    def test_device_binding_session_and_password_rotation(self):
        first = self.auth.login(
            "admin.local", self.admin["temporary_password"],
            device_label="PC de teste")
        self.assertTrue(first["device_token"])
        # sessão é validada COM o cookie de dispositivo casado (vínculo por req.)
        self.assertEqual(
            self.auth.authenticate(
                first["session_token"], first["device_token"])["account_id"],
            self.admin["account_id"])
        # sessão sem o device cookie casado é rejeitada (não é bearer puro)
        with self.assertRaises(AuthError) as ctxd:
            self.auth.authenticate(first["session_token"])
        self.assertEqual(ctxd.exception.code, "device_unrecognized")
        with self.assertRaises(AuthError) as ctx:
            self.auth.login("admin.local", self.admin["temporary_password"])
        self.assertEqual(ctx.exception.code, "device_conflict")

        new_password = "Frase segura local 2026!"
        self.auth.change_password(
            self.admin["account_id"], self.admin["temporary_password"],
            new_password)
        with self.assertRaises(AuthError):
            self.auth.authenticate(first["session_token"])
        second = self.auth.login(
            "admin.local", new_password, first["device_token"])
        self.assertFalse(second["account"]["must_change_password"])

    def test_lock_never_blocks_correct_password(self):
        # 5 senhas erradas trancam o throttle, mas a senha CORRETA ainda entra
        # (senão um atacante tranca a conta da vítima — DoS pré-auth).
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.auth.login("admin.local", "senha errada qualquer")
        res = self.auth.login("admin.local", self.admin["temporary_password"],
                              device_label="pc")
        self.assertEqual(res["account"]["id"], self.admin["account_id"])

    def test_change_password_rejects_reuse(self):
        with self.assertRaises(AuthError) as ctx:
            self.auth.change_password(
                self.admin["account_id"], self.admin["temporary_password"],
                self.admin["temporary_password"])
        self.assertEqual(ctx.exception.code, "password_reuse")

    def test_weak_single_class_passphrase_rejected(self):
        from albion.auth import validate_password
        with self.assertRaises(AuthError):
            validate_password("aaaaaaaaaaaaaaaa")        # 16 chars, 1 distinto
        with self.assertRaises(AuthError):
            validate_password("abcabcabcabcabca")        # poucos distintos
        validate_password("uma frase longa e variada 2026")  # diversa -> ok

    def test_admin_lifecycle_revokes_access_and_keeps_last_admin(self):
        made = self.auth.create_account(
            self.admin["account_id"], "membro.um", role="member",
            profiles=["gatherer", "crafter"])
        account = self.auth.get_account(made["account_id"])
        self.assertEqual(account["profiles"], ["crafter", "gatherer"])
        with self.assertRaises(AuthError) as ctx:
            self.auth.update_account(999, self.admin["account_id"], active=False)
        self.assertEqual(ctx.exception.code, "last_admin")
        disabled = self.auth.update_account(
            self.admin["account_id"], made["account_id"], active=False)
        self.assertFalse(disabled["active"])

    def test_reset_device_allows_a_new_binding(self):
        first = self.auth.login(
            "admin.local", self.admin["temporary_password"])
        self.auth.reset_device(
            self.admin["account_id"], self.admin["account_id"])
        second = self.auth.login(
            "admin.local", self.admin["temporary_password"],
            device_label="PC novo")
        self.assertNotEqual(first["device_token"], second["device_token"])

    def test_http_login_csrf_forced_password_and_admin_api(self):
        old_manager = app.auth_manager
        old_required = app.config.AUTH_REQUIRED
        app.auth_manager = self.auth
        app.config.AUTH_REQUIRED = True
        client = TestClient(app.app)
        try:
            self.assertEqual(client.get("/api/meta").status_code, 401)
            login = client.post("/api/auth/login", json={
                "username": "admin.local",
                "password": self.admin["temporary_password"],
                "device_label": "Navegador de teste",
            })
            self.assertEqual(login.status_code, 200)
            me = client.get("/api/auth/me")
            self.assertEqual(me.status_code, 200)
            csrf = me.json()["csrf"]
            self.assertEqual(client.get("/api/meta").status_code, 403)
            changed = client.post(
                "/api/auth/change-password",
                headers={"X-CSRF-Token": csrf},
                json={"current_password": self.admin["temporary_password"],
                      "new_password": "Frase segura para HTTP 2026!"})
            self.assertEqual(changed.status_code, 200)

            login = client.post("/api/auth/login", json={
                "username": "admin.local",
                "password": "Frase segura para HTTP 2026!",
            })
            self.assertEqual(login.status_code, 200)
            csrf = login.json()["csrf"]
            self.assertEqual(client.get("/api/meta").status_code, 200)
            no_csrf = client.post("/api/admin/accounts", json={
                "username": "sem.csrf", "role": "viewer"})
            self.assertEqual(no_csrf.status_code, 403)
            created = client.post(
                "/api/admin/accounts", headers={"X-CSRF-Token": csrf},
                json={"username": "com.csrf", "role": "viewer"})
            self.assertEqual(created.status_code, 200)
            self.assertIn("temporary_password", created.json())
        finally:
            app.auth_manager = old_manager
            app.config.AUTH_REQUIRED = old_required


if __name__ == "__main__":
    unittest.main()

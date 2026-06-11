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

    def test_meta_and_status_endpoints_are_available(self):
        client = TestClient(app.app)

        meta = client.get("/api/meta")
        status = client.get("/api/status")
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
            finally:
                client.db.close()

        self.assertIn("price_snapshots", tables)
        self.assertIn("watchlist", tables)
        self.assertIn("price_snapshots_daily", tables)


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
                res = survival.persistence(client.db, "americas")
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


if __name__ == "__main__":
    unittest.main()

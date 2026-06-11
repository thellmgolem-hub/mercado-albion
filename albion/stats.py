# -*- coding: utf-8 -*-
"""Camada estatística do Item Lab (extraída de app.py para ser testável).

Decisões metodológicas (ver AUDITORIA.md, seção 2):
- z-score calculado sobre os RESÍDUOS da tendência linear (não sobre níveis),
  para não confundir tendência com anomalia em séries não-estacionárias;
  complementado por um z robusto (mediana + MAD).
- Mediana verdadeira (statistics.median), não proxy.
- Janelas de média móvel/momentum definidas em TEMPO (dias) e convertidas em
  pontos conforme a escala (1h/6h/24h).
- Intervalo da previsão ingênua usa o desvio dos resíduos da regressão, com
  cobertura nominal declarada (~68% sob erros normais).
"""
import math
import statistics
from datetime import datetime

MA_FAST_DAYS = 7
MA_SLOW_DAYS = 30
MOMENTUM_DAYS = 7


def mean(values):
    return sum(values) / len(values) if values else None


def std(values):
    if len(values) < 2:
        return None
    m = mean(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def linear_fit(values):
    """Regressão linear y = a + b*x sobre índices 0..n-1 -> (a, b) ou None."""
    n = len(values)
    if n < 2:
        return None
    xs = range(n)
    mx = (n - 1) / 2
    my = mean(values)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den
    return my - b * mx, b


def residuals(values, fit):
    a, b = fit
    return [y - (a + b * x) for x, y in enumerate(values)]


def pct_change(new, old):
    if old in (None, 0) or new is None:
        return None
    return (new / old - 1) * 100


def price_percentile(values, latest):
    if not values or latest is None:
        return None
    return 100 * sum(1 for v in values if v <= latest) / len(values)


def last_ma(values, window_pts):
    if not values or window_pts < 1:
        return None
    w = min(window_pts, len(values))
    return mean(values[-w:])


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:19])
    except ValueError:
        return None


def history_expected_points(days, time_scale):
    return max(1, int(math.ceil(days * 24 / time_scale)))


def history_quality_score(point_count, active_ratio, latest_ts,
                          global_latest_ts, days, time_scale):
    coverage = min(point_count / history_expected_points(days, time_scale), 1)
    active = min(active_ratio, 1)
    if latest_ts and global_latest_ts:
        age_hours = max(0, (global_latest_ts - latest_ts).total_seconds() / 3600)
        recency = 1 if age_hours <= time_scale else 0.75 if age_hours <= time_scale * 3 else 0.4
    else:
        recency = 0
    score = 100 * (0.45 * coverage + 0.35 * active + 0.20 * recency)
    return round(score, 1)


def interpret_series(metrics):
    notes = []
    z = metrics.get("z_score")
    vz = metrics.get("volume_z_score")
    momentum = metrics.get("momentum_pct")
    trend = metrics.get("trend_slope_pct_per_point")
    active = metrics.get("active_ratio") or 0
    quality = metrics.get("data_quality_score") or 0

    if quality < 45:
        notes.append("Amostra fraca: use como sinal exploratorio, nao como decisao.")
    if z is not None:
        if z <= -2:
            notes.append("Preco muito abaixo da tendencia historica: possivel desconto ou estresse temporario.")
        elif z <= -1:
            notes.append("Preco abaixo da tendencia historica: vale investigar compra se volume confirmar.")
        elif z >= 2:
            notes.append("Preco muito acima da tendencia historica: risco de mercado esticado.")
        elif z >= 1:
            notes.append("Preco acima da tendencia historica: cuidado ao comprar para estoque.")
    if momentum is not None:
        if momentum >= 8:
            notes.append("Momentum de alta forte no periodo recente.")
        elif momentum <= -8:
            notes.append("Momentum de queda forte no periodo recente.")
    if trend is not None:
        if trend > 0:
            notes.append("Inclinacao estatistica positiva na janela analisada.")
        elif trend < 0:
            notes.append("Inclinacao estatistica negativa na janela analisada.")
    if vz is not None and vz >= 2:
        notes.append("Volume recente anormalmente alto: pode indicar demanda real ou evento pontual.")
    wr = metrics.get("weekend_volume_ratio")
    if wr is not None:
        if wr >= 1.25:
            notes.append(f"Volume {round((wr - 1) * 100)}% maior no fim de semana: planeje vendas para sab/dom.")
        elif wr <= 0.75:
            notes.append(f"Volume {round((1 - wr) * 100)}% menor no fim de semana: mercado de dias uteis.")
    if active < 0.5:
        notes.append("Baixa frequencia de vendas: risco maior de ficar preso no item.")
    if not notes:
        notes.append("Serie sem anomalia forte; interpretar junto com spread, frescor e profundidade.")

    if quality < 45:
        stance = "dados fracos"
    elif z is not None and z <= -1 and active >= 0.5:
        stance = "investigar compra"
    elif z is not None and z >= 1.5:
        stance = "evitar entrada cara"
    elif momentum is not None and momentum >= 8 and active >= 0.5:
        stance = "tendencia de alta"
    elif momentum is not None and momentum <= -8:
        stance = "queda/esperar"
    else:
        stance = "neutro/monitorar"
    return {"stance": stance, "notes": notes}


def analyze_history_series(series, days, time_scale, global_latest_ts):
    """Estatística descritiva + tendência + interpretação para uma série."""
    pts = [p for p in series.get("data", [])
           if p.get("avg_price", 0) and p.get("item_count", 0) is not None]
    pts.sort(key=lambda p: p["ts"])
    prices = [float(p["avg_price"]) for p in pts if p["avg_price"] > 0]
    volumes = [float(p.get("item_count") or 0) for p in pts if p["avg_price"] > 0]
    if not prices:
        return {
            "item_id": series["item_id"],
            "city": series["city"],
            "quality": series["quality"],
            "points": 0,
            "interpretation": {"stance": "sem dados", "notes": ["Sem historico utilizavel."]},
        }

    total_volume = sum(volumes)
    traded_value = sum(v * p for v, p in zip(volumes, prices))
    vwap = traded_value / total_volume if total_volume else mean(prices)
    latest = prices[-1]
    previous = prices[-2] if len(prices) >= 2 else None
    first = prices[0]
    mean_price = mean(prices)
    std_price = std(prices)

    # tendência + resíduos: o z mede desvio DO PADRÃO, não a própria tendência
    fit = linear_fit(prices)
    slope = fit[1] if fit else None
    resid = residuals(prices, fit) if fit else None
    std_resid = std(resid) if resid else None
    z_score = (resid[-1] / std_resid) if std_resid else None

    med = statistics.median(prices)
    mad = statistics.median([abs(p - med) for p in prices]) if prices else None
    robust_z = (0.6745 * (latest - med) / mad) if mad else None

    mean_volume = mean(volumes)
    std_volume = std(volumes)
    volume_z = ((volumes[-1] - mean_volume) / std_volume
                if std_volume and volumes else None)
    slope_pct = (slope / mean_price * 100) if slope is not None and mean_price else None

    # janelas em TEMPO convertidas para pontos conforme a escala
    ppd = 24 / time_scale
    ma_fast_pts = max(1, round(MA_FAST_DAYS * ppd))
    ma_slow_pts = max(1, round(MA_SLOW_DAYS * ppd))
    momentum_pts = min(max(1, round(MOMENTUM_DAYS * ppd)), len(prices) - 1)
    momentum = (pct_change(latest, prices[-1 - momentum_pts])
                if momentum_pts >= 1 and len(prices) >= 2 else None)

    log_returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i] > 0 and prices[i - 1] > 0
    ]
    vol_ret = std(log_returns)
    active_days = len({p["ts"][:10] for p in pts if (p.get("item_count") or 0) > 0})
    active_ratio = active_days / max(days, 1)
    latest_ts = parse_ts(pts[-1]["ts"])

    # sazonalidade dia-da-semana (precisa de >= 2 semanas de dados diários)
    weekday_profile = None
    weekend_ratio = None
    if time_scale == 24 and len(pts) >= 14:
        agg = {i: [0.0, 0.0, 0] for i in range(7)}  # vol, preço, n
        for p, price, vol in zip(pts, prices, volumes):
            dt = parse_ts(p["ts"])
            if dt is None:
                continue
            wd = dt.weekday()
            agg[wd][0] += vol
            agg[wd][1] += price
            agg[wd][2] += 1
        labels = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
        weekday_profile = [
            {"dia": labels[i], "volume_medio": round(v / n, 1),
             "preco_medio": round(pr / n, 2)}
            for i, (v, pr, n) in agg.items() if n > 0
        ]
        wk = [v / n for i, (v, pr, n) in agg.items() if n > 0 and i < 5]
        we = [v / n for i, (v, pr, n) in agg.items() if n > 0 and i >= 5]
        if wk and we and mean(wk):
            weekend_ratio = round(mean(we) / mean(wk), 2)
    quality_score = history_quality_score(
        len(pts), active_ratio, latest_ts, global_latest_ts, days, time_scale)

    metrics = {
        "item_id": series["item_id"],
        "city": series["city"],
        "quality": series["quality"],
        "points": len(pts),
        "first_ts": pts[0]["ts"],
        "latest_ts": pts[-1]["ts"],
        "first_price": round(first, 2),
        "latest_price": round(latest, 2),
        "previous_price": round(previous, 2) if previous is not None else None,
        "change_from_first_pct": round(pct_change(latest, first), 2) if first else None,
        "change_prev_pct": round(pct_change(latest, previous), 2) if previous else None,
        "mean_price": round(mean_price, 2) if mean_price is not None else None,
        "median_price": round(med, 2),
        "mad_price": round(mad, 2) if mad is not None else None,
        "vwap": round(vwap, 2) if vwap is not None else None,
        "min_price": round(min(prices), 2),
        "max_price": round(max(prices), 2),
        "std_price": round(std_price, 2) if std_price is not None else None,
        "z_score": round(z_score, 2) if z_score is not None else None,
        "robust_z": round(robust_z, 2) if robust_z is not None else None,
        "price_percentile": round(price_percentile(prices, latest), 1),
        "ma_fast": round(last_ma(prices, ma_fast_pts), 2),
        "ma_slow": round(last_ma(prices, ma_slow_pts), 2),
        "ma_fast_window_days": MA_FAST_DAYS,
        "ma_slow_window_days": MA_SLOW_DAYS,
        "momentum_pct": round(momentum, 2) if momentum is not None else None,
        "momentum_window_days": MOMENTUM_DAYS,
        "trend_slope_abs_per_point": round(slope, 4) if slope is not None else None,
        "trend_slope_pct_per_point": round(slope_pct, 3) if slope_pct is not None else None,
        "return_volatility": round(vol_ret, 4) if vol_ret is not None else None,
        "total_volume": round(total_volume, 1),
        "avg_daily_volume": round(total_volume / max(days, 1), 1),
        "latest_volume": round(volumes[-1], 1),
        "mean_volume": round(mean_volume, 1) if mean_volume is not None else None,
        "volume_z_score": round(volume_z, 2) if volume_z is not None else None,
        "active_days": active_days,
        "active_ratio": round(active_ratio, 3),
        "data_quality_score": quality_score,
        "weekday_profile": weekday_profile,
        "weekend_volume_ratio": weekend_ratio,
    }
    metrics["interpretation"] = interpret_series(metrics)
    if slope is not None and std_resid:
        forecast = max(0, latest + slope)
        metrics["naive_forecast_next"] = {
            "price": round(forecast, 2),
            "low": round(max(0, forecast - std_resid), 2),
            "high": round(forecast + std_resid, 2),
            "method": "tendencia linear +/- 1 desvio dos residuos (~68% se erros normais)",
        }
    return metrics


def compare_item_series(analyses):
    usable = [a for a in analyses if a.get("points", 0) > 0 and a.get("vwap")]
    if not usable:
        return {}
    cheapest = min(usable, key=lambda a: a["vwap"])
    most_expensive = max(usable, key=lambda a: a["vwap"])
    most_liquid = max(usable, key=lambda a: a.get("avg_daily_volume") or 0)
    best_quality = max(usable, key=lambda a: a.get("data_quality_score") or 0)
    spread = pct_change(most_expensive["vwap"], cheapest["vwap"])
    return {
        "cheapest_city": cheapest["city"],
        "cheapest_vwap": cheapest["vwap"],
        "most_expensive_city": most_expensive["city"],
        "most_expensive_vwap": most_expensive["vwap"],
        "vwap_spread_pct": round(spread, 2) if spread is not None else None,
        "most_liquid_city": most_liquid["city"],
        "most_liquid_avg_daily_volume": most_liquid.get("avg_daily_volume"),
        "best_data_city": best_quality["city"],
        "best_data_score": best_quality.get("data_quality_score"),
    }


def abs_score(value, anchor):
    """Score 0-100 em escala log com âncora absoluta (anchor -> 100)."""
    if not value or value <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log10(1 + value) / math.log10(1 + anchor))

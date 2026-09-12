"""Loads the traffic ensemble trained in train_model.py and turns its raw
vehicles-per-hour prediction into a 0-100 congestion score plus a short
forecast horizon, for the FastAPI layer to serve. The artifact also contains
an auxiliary annual road-demand model, which is not mixed into hourly city
forecasts because its target and sampling frequency are different.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd

BASE = Path(__file__).parent
MODEL_PATH = BASE / "model" / "traffic_model.joblib"

# Illustrative per-asset demand multipliers. The trained model itself learns
# real hour-of-day / day-of-week / weather / holiday effects from the Metro
# Interstate Traffic Volume dataset; because that dataset comes from a single
# physical sensor, these per-location weights are a simple, clearly-labelled
# scaling layer so the same real model can drive several map locations at
# once, rather than a claim that each road was individually trained on.
LOCATION_WEIGHT = {
    "ITO": 1.15,
    "Connaught Place": 1.05,
    "Pragati Maidan": 0.95,
    "India Gate": 0.7,
    "AIIMS / Ring Road": 1.1,
}

_bundle: Optional[dict] = None


def _load() -> dict:
    global _bundle
    if _bundle is None:
        _bundle = joblib.load(MODEL_PATH)
    return _bundle


def _congestion_from_volume(volume: float, hour: int) -> float:
    bundle = _load()
    stats = bundle["hourly_stats"].get(hour) or bundle["hourly_stats"].get(str(hour))
    lo, hi = stats["min"], stats["max"]
    if hi <= lo:
        return 50.0
    pct = (volume - lo) / (hi - lo) * 100
    return max(3.0, min(97.0, pct))


def predict_volume(hour: int, day_of_week: int, month: int, temp_c: float, rain_1h: float, snow_1h: float, clouds_all: int, weather_main: str, is_holiday: int = 0) -> float:
    bundle = _load()
    is_weekend = int(day_of_week >= 5)
    row = pd.DataFrame([{
        "hour": hour, "day_of_week": day_of_week, "month": month,
        "is_weekend": is_weekend, "is_holiday": is_holiday,
        "is_rush_hour": int(((7 <= hour <= 10) or (16 <= hour <= 19)) and not is_weekend),
        "hour_sin": np.sin(2 * np.pi * hour / 24), "hour_cos": np.cos(2 * np.pi * hour / 24),
        "dow_sin": np.sin(2 * np.pi * day_of_week / 7), "dow_cos": np.cos(2 * np.pi * day_of_week / 7),
        "month_sin": np.sin(2 * np.pi * (month - 1) / 12), "month_cos": np.cos(2 * np.pi * (month - 1) / 12),
        "temp_c": temp_c, "rain_1h": rain_1h, "snow_1h": snow_1h,
        "clouds_all": clouds_all, "weather_main": weather_main,
    }])
    return float(bundle["pipeline"].predict(row)[0])


def forecast(location: str, rain_1h: float = 0.0, temp_c: float = 22.0, clouds_all: int = 40, weather_main: str = "Clouds", horizon_minutes: int = 180, step_minutes: int = 15) -> list[dict]:
    """Real-model forecast: walks the clock forward and asks the trained
    RandomForest for each future hour, decaying the current rain input as a
    simple persistence assumption (heavier rain now fades over the horizon).
    """
    now = datetime.now()
    weight = LOCATION_WEIGHT.get(location, 1.0)
    points = []
    for minute in range(0, horizon_minutes + 1, step_minutes):
        t = now + timedelta(minutes=minute)
        decay = max(0.0, 1 - minute / (horizon_minutes * 1.4))
        vol = predict_volume(
            hour=t.hour, day_of_week=t.weekday(), month=t.month,
            temp_c=temp_c, rain_1h=rain_1h * decay, snow_1h=0.0,
            clouds_all=clouds_all, weather_main=weather_main,
        )
        congestion = _congestion_from_volume(vol * weight, t.hour)
        points.append({"minute": minute, "congestion": round(congestion), "predictedVolume": round(vol * weight)})
    return points


def model_metrics() -> dict:
    import json
    return json.loads((BASE / "model" / "metrics.json").read_text())

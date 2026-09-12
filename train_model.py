"""
Train CityTwin's traffic-volume prediction model on the real, public
Metro Interstate Traffic Volume dataset (Hogue, J., 2019, UCI ML Repository,
https://doi.org/10.24432/C5X60B) — hourly westbound I-94 traffic near
Minneapolis-St Paul, MN, Oct 2012 - Sep 2018, with matching hourly weather
and US public-holiday data.

Run:
    python train_model.py

Produces:
    model/traffic_model.joblib   - trained RandomForestRegressor + preprocessing
    model/metrics.json           - held-out test metrics + dataset summary
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, VotingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

BASE = Path(__file__).parent
DATA_PATH = BASE / "data" / "Metro_Interstate_Traffic_Volume.csv"
ROAD_COUNT_PATH = BASE / "data" / "Traffic_Volumes_Provincial_Highway_System.csv"
MODEL_DIR = BASE / "model"
MODEL_DIR.mkdir(exist_ok=True)

NUMERIC_FEATURES = [
    "hour", "day_of_week", "month", "is_weekend", "is_holiday", "is_rush_hour",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos",
    "temp_c", "rain_1h", "snow_1h", "clouds_all",
]
CATEGORICAL_FEATURES = ["weather_main"]


def train_road_demand_model() -> tuple[object, dict]:
    """Train a separate model for open annual road-count observations.

    This target is ADT, not hourly volume, so it must remain separate from
    the I-94 hourly forecasting model rather than being mixed into its labels.
    """
    road_df = pd.read_csv(ROAD_COUNT_PATH)
    road_df["date"] = pd.to_datetime(road_df["date"], format="%m/%d/%Y", errors="coerce")
    road_df["sectionLength"] = pd.to_numeric(road_df["sectionLength"], errors="coerce")
    road_df["adt"] = pd.to_numeric(road_df["adt"], errors="coerce")
    road_df = road_df.dropna(subset=["date", "sectionLength", "adt"])
    road_df["year"] = road_df["date"].dt.year
    road_df["month"] = road_df["date"].dt.month
    numeric = ["highway", "section", "sectionLength", "year", "month"]
    categorical = ["group", "type", "county", "direction"]
    X = road_df[numeric + categorical].copy()
    y = road_df["adt"]
    split = int(len(road_df) * 0.8)
    preprocess = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical),
    ], remainder="passthrough")
    model = Pipeline([
        ("prep", preprocess),
        ("road_rf", RandomForestRegressor(n_estimators=240, max_depth=18, min_samples_leaf=2, n_jobs=-1, random_state=52)),
    ])
    model.fit(X.iloc[:split], y.iloc[:split])
    prediction = model.predict(X.iloc[split:])
    return model, {
        "dataset": "Traffic Volumes - Provincial Highway System, Government of Canada",
        "source": "https://open.canada.ca/data/en/dataset/b5b01346-6a3f-a523-9f52-c61a52791356",
        "rows": int(len(road_df)),
        "target": "annual average daily traffic (ADT)",
        "test_mae_vehicles_per_day": round(float(mean_absolute_error(y.iloc[split:], prediction)), 1),
        "test_r2": round(float(r2_score(y.iloc[split:], prediction)), 4),
    }


def load_and_clean() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH)

    # Documented data-quality issues in this public dataset:
    # 1) ~7.6k exact-duplicate timestamps (repeated weather_description rows) -> keep first
    df["date_time"] = pd.to_datetime(df["date_time"])
    df = df.drop_duplicates(subset="date_time", keep="first")
    # 2) one erroneous rain_1h reading of 9831mm/hour -> clip to a physically plausible max
    df["rain_1h"] = df["rain_1h"].clip(upper=100)
    # 3) a handful of temp==0K (absolute zero, sensor error) -> drop
    df = df[df["temp"] > 0]

    df["temp_c"] = df["temp"] - 273.15
    df["hour"] = df["date_time"].dt.hour
    df["day_of_week"] = df["date_time"].dt.dayofweek
    df["month"] = df["date_time"].dt.month
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    df["is_holiday"] = (df["holiday"] != "None").astype(int)
    df["is_rush_hour"] = (((df["hour"].between(7, 10)) | (df["hour"].between(16, 19))) & (df["is_weekend"] == 0)).astype(int)
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    df["dow_sin"] = np.sin(2 * np.pi * df["day_of_week"] / 7)
    df["dow_cos"] = np.cos(2 * np.pi * df["day_of_week"] / 7)
    df["month_sin"] = np.sin(2 * np.pi * (df["month"] - 1) / 12)
    df["month_cos"] = np.cos(2 * np.pi * (df["month"] - 1) / 12)
    return df.sort_values("date_time").reset_index(drop=True)


def main() -> None:
    df = load_and_clean()
    X = df[NUMERIC_FEATURES + CATEGORICAL_FEATURES]
    y = df["traffic_volume"]

    # Chronological split (last 15% of the timeline held out) so we evaluate
    # like a real forecasting deployment rather than shuffling time away.
    split = int(len(df) * 0.85)
    X_train, X_test = X.iloc[:split], X.iloc[split:]
    y_train, y_test = y.iloc[:split], y.iloc[split:]

    preprocess = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL_FEATURES),
    ], remainder="passthrough")

    ensemble = VotingRegressor([
        ("rf", RandomForestRegressor(n_estimators=320, max_depth=20, min_samples_leaf=2, max_features=0.85, n_jobs=-1, random_state=42)),
        ("extra", ExtraTreesRegressor(n_estimators=240, max_depth=24, min_samples_leaf=2, max_features=0.9, n_jobs=-1, random_state=43)),
        ("hist", HistGradientBoostingRegressor(max_iter=260, learning_rate=0.055, max_leaf_nodes=31, l2_regularization=0.8, random_state=44)),
    ], weights=[0.45, 0.3, 0.25])
    model = Pipeline([
        ("prep", preprocess),
        ("ensemble", ensemble),
    ])
    model.fit(X_train, y_train)

    pred = model.predict(X_test)
    mae = mean_absolute_error(y_test, pred)
    r2 = r2_score(y_test, pred)

    # Learn typical volume-by-hour-of-day range so we can rescale a predicted
    # volume into a 0-100 "congestion %" for the dashboard, independent of
    # whichever city/road the demo is pointed at.
    hourly_stats = df.groupby("hour")["traffic_volume"].agg(["min", "max", "mean"]).to_dict(orient="index")
    road_demand_model, road_metrics = train_road_demand_model()

    joblib.dump({"pipeline": model, "road_demand_pipeline": road_demand_model, "hourly_stats": hourly_stats, "global_max": float(df["traffic_volume"].max())}, MODEL_DIR / "traffic_model.joblib", compress=3)

    metrics = {
        "dataset": "Metro Interstate Traffic Volume (Hogue, 2019, UCI ML Repository, doi:10.24432/C5X60B)",
        "rows_raw": int(pd.read_csv(DATA_PATH).shape[0]),
        "rows_after_cleaning": int(len(df)),
        "date_range": [str(df["date_time"].min()), str(df["date_time"].max())],
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "test_mae_vehicles_per_hour": round(float(mae), 1),
        "test_r2": round(float(r2), 4),
        "model": "VotingRegressor(RandomForest + ExtraTrees + HistGradientBoosting)",
        "model_version": "traffic-ensemble-v3-with-adt-auxiliary",
        "features": NUMERIC_FEATURES + CATEGORICAL_FEATURES,
        "supplemental_dataset": road_metrics,
    }
    (MODEL_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()

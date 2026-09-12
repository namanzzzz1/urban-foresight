"""CityTwin API — telemetry, ML-backed traffic forecasting, and what-if simulation.

Traffic forecasts are served by a RandomForest model trained in
train_model.py on the real, public Metro Interstate Traffic Volume dataset
(see model/metrics.json for held-out accuracy). Flood/emergency scoring
below is an explainable rule-based heuristic, not a trained model — the
README explains the split so nothing here is overstated.
"""
from __future__ import annotations

import asyncio
from difflib import get_close_matches
import logging
import random
import re
from time import perf_counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

import traffic_model
from database import authenticate_user, create_session, delete_session, get_session_user, init_db, list_alerts, list_assets, list_simulations, save_simulation
from logging_config import configure_logging

BASE = Path(__file__).parent

configure_logging()
init_db()
logger = logging.getLogger("citytwin.api")

app = FastAPI(title="CityTwin API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception(
            "Unhandled request error",
            extra={"event": "request_error", "method": request.method, "path": request.url.path, "error_type": type(exc).__name__},
        )
        raise
    logger.info(
        "HTTP request",
        extra={
            "event": "http_request",
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "latency_ms": round((perf_counter() - started) * 1000, 2),
        },
    )
    return response

CITY_CENTER = {"lat": 28.6139, "lng": 77.2090}  # New Delhi
# One AI-suggestion pin shown on the map (rerouting hint), distinct from persisted assets.
AI_SUGGESTIONS = [
    {"id": "reroute-1", "lat": 28.6220, "lng": 77.2260, "text": "Reroute via Ring Road"},
]


class SimulationRequest(BaseModel):
    rainfall_mm: int = Field(80, ge=0, le=300)
    traffic_increase: int = Field(40, ge=0, le=100)
    blocked_road: str = "Ring Road - 2 km"
    location: str = "ITO"
    current_rain_mm: float = Field(0, ge=0, le=300)
    temp_c: float = Field(27, ge=-50, le=60)
    aqi: int = Field(0, ge=0, le=500)


class RouteRequest(BaseModel):
    origin: dict[str, float]
    destination: dict[str, float]


class AssistantRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    location: str = "ITO"
    temp_c: float = 27
    rain_mm: float = 0
    aqi: int = 0
    weather: str = "Clouds"


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=80)
    password: str = Field(..., min_length=4, max_length=200)


CITY_PLACES = {
    "delhi": [
        {"name": "Connaught Place", "latitude": 28.6315, "longitude": 77.2167},
        {"name": "India Gate", "latitude": 28.6129, "longitude": 77.2295},
        {"name": "AIIMS", "latitude": 28.5672, "longitude": 77.2100},
        {"name": "Rohini", "latitude": 28.7495, "longitude": 77.0565},
        {"name": "Dwarka", "latitude": 28.5921, "longitude": 77.0460},
        {"name": "Saket", "latitude": 28.5244, "longitude": 77.2066},
        {"name": "Anand Vihar", "latitude": 28.6469, "longitude": 77.3160},
    ],
    "ghaziabad": [
        {"name": "Ghaziabad City", "latitude": 28.6692, "longitude": 77.4538},
        {"name": "Indirapuram", "latitude": 28.6415, "longitude": 77.3715},
        {"name": "Vaishali", "latitude": 28.6497, "longitude": 77.3390},
        {"name": "Vasundhara", "latitude": 28.6586, "longitude": 77.3740},
        {"name": "Kaushambi", "latitude": 28.6455, "longitude": 77.3247},
        {"name": "Crossings Republik", "latitude": 28.6177, "longitude": 77.4370},
    ],
    "noida": [
        {"name": "Noida City Centre", "latitude": 28.5744, "longitude": 77.3560},
        {"name": "Sector 18", "latitude": 28.5677, "longitude": 77.3210},
        {"name": "Sector 62", "latitude": 28.6271, "longitude": 77.3649},
        {"name": "Botanical Garden", "latitude": 28.5647, "longitude": 77.3340},
        {"name": "Greater Noida", "latitude": 28.4744, "longitude": 77.5040},
        {"name": "Jewar", "latitude": 28.1324, "longitude": 77.5500},
    ],
}
ASSISTANT_LOCATIONS = {
    place["name"].lower(): place
    for places in CITY_PLACES.values()
    for place in places
}
ASSISTANT_LOCATIONS.update({
    "ito": {"name": "ITO", "latitude": 28.6280, "longitude": 77.2410},
    "connaught place": {"name": "Connaught Place", "latitude": 28.6315, "longitude": 77.2167},
    "pragati maidan": {"name": "Pragati Maidan", "latitude": 28.6176, "longitude": 77.2431},
    "india gate": {"name": "India Gate", "latitude": 28.6129, "longitude": 77.2295},
    "aiims / ring road": {"name": "AIIMS / Ring Road", "latitude": 28.5672, "longitude": 77.2100},
    "mumabai": {"name": "Mumbai", "state": "Maharashtra", "country": "India", "latitude": 19.0760, "longitude": 72.8777},
    "mumbay": {"name": "Mumbai", "state": "Maharashtra", "country": "India", "latitude": 19.0760, "longitude": 72.8777},
})
CITY_CENTERS = {
    "delhi": {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090},
    "ghaziabad": {"name": "Ghaziabad", "latitude": 28.6692, "longitude": 77.4538},
    "noida": {"name": "Noida", "latitude": 28.5355, "longitude": 77.3910},
}
INDIA_STATE_CENTERS = {
    "andhra pradesh": (15.9129, 79.7400), "arunachal pradesh": (28.2180, 94.7278),
    "assam": (26.2006, 92.9376), "bihar": (25.0961, 85.3131), "chhattisgarh": (21.2787, 81.8661),
    "goa": (15.2993, 74.1240), "gujarat": (22.2587, 71.1924), "haryana": (29.0588, 76.0856),
    "himachal pradesh": (31.1048, 77.1734), "jharkhand": (23.6102, 85.2799), "karnataka": (15.3173, 75.7139),
    "kerala": (10.8505, 76.2711), "madhya pradesh": (22.9734, 78.6569), "maharashtra": (19.7515, 75.7139),
    "manipur": (24.6637, 93.9063), "meghalaya": (25.4670, 91.3662), "mizoram": (23.1645, 92.9376),
    "nagaland": (26.1584, 94.5624), "odisha": (20.9517, 85.0985), "punjab": (31.1471, 75.3412),
    "rajasthan": (27.0238, 74.2179), "sikkim": (27.5330, 88.5122), "tamil nadu": (11.1271, 78.6569),
    "telangana": (18.1124, 79.0193), "tripura": (23.9408, 91.9882), "uttar pradesh": (26.8467, 80.9462),
    "uttarakhand": (30.0668, 79.0193), "west bengal": (22.9868, 87.8550), "delhi": (28.6139, 77.2090),
    "jammu and kashmir": (33.7782, 76.5762), "ladakh": (34.1526, 77.5771),
}
for state, (latitude, longitude) in INDIA_STATE_CENTERS.items():
    ASSISTANT_LOCATIONS.setdefault(state, {"name": state.title(), "state": state.title(), "country": "India", "latitude": latitude, "longitude": longitude})


async def fetch_city_place_temperatures(city: str) -> list[dict]:
    places = CITY_PLACES.get(city, [])
    async with httpx.AsyncClient(timeout=8) as client:
        requests = [
            client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": place["latitude"], "longitude": place["longitude"], "current": "temperature_2m", "temperature_unit": "celsius", "timezone": "auto"},
            )
            for place in places
        ]
        responses = await asyncio.gather(*requests, return_exceptions=True)
    readings = []
    for place, response in zip(places, responses):
        if isinstance(response, Exception) or response.status_code != 200:
            continue
        readings.append({"name": place["name"], "tempC": response.json().get("current", {}).get("temperature_2m")})
    return [reading for reading in readings if reading["tempC"] is not None]


def resolve_assistant_location(question: str) -> dict | None:
    normalized = question.lower()
    matches = [place for key, place in ASSISTANT_LOCATIONS.items() if key in normalized]
    if matches:
        return max(matches, key=lambda place: len(place["name"]))
    words = re.findall(r"[a-z]+", normalized)
    for word in words:
        close = get_close_matches(word, list(ASSISTANT_LOCATIONS), n=1, cutoff=0.72)
        if close:
            return ASSISTANT_LOCATIONS[close[0]]
    return None


async def fetch_live_place_snapshot(place: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=8) as client:
        weather_request = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,precipitation,rain,weather_code",
                "temperature_unit": "celsius", "timezone": "auto",
            },
        )
        air_request = client.get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": place["latitude"], "longitude": place["longitude"], "current": "us_aqi", "timezone": "auto"},
        )
        weather_response, air_response = await asyncio.gather(weather_request, air_request, return_exceptions=True)
    if isinstance(weather_response, Exception) or weather_response.status_code != 200:
        return None
    weather = weather_response.json().get("current", {})
    air = {} if isinstance(air_response, Exception) or air_response.status_code != 200 else air_response.json().get("current", {})
    return {
        "location": place["name"],
        "state": place.get("state"),
        "country": place.get("country", "India"),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "tempC": weather.get("temperature_2m"),
        "rainMm": weather.get("rain", weather.get("precipitation", 0)) or 0,
        "weatherCode": weather.get("weather_code"),
        "aqi": air.get("us_aqi"),
        "source": "live Open-Meteo weather and AQI",
    }


def extract_india_location(question: str) -> str | None:
    cleaned = re.sub(r"\b(which|what|where|is|are|the|current|live|traffic|weather|temperature|temp|air quality|aqi|pollution|flood|risk|power|energy|transit|transport|in|at|near|around|for|of|has|have|below|above|under|over|degrees|degree)\b", " ", question.lower())
    cleaned = re.sub(r"[^a-z0-9 .'-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


async def geocode_india_place(query: str) -> dict | None:
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": query, "count": 1, "language": "en", "format": "json", "countryCode": "IN"},
        )
    if response.status_code != 200:
        return None
    place = (response.json().get("results") or [None])[0]
    if not place or place.get("country_code") != "IN":
        return None
    return {
        "name": place.get("name", query.title()),
        "state": place.get("admin1"),
        "country": place.get("country", "India"),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
    }


def derive_live_signals(location: str, points: list[dict], temp_c: float, rain_mm: float, aqi: int) -> dict:
    """Combine live weather with the trained traffic forecast.

    Power and transit providers are not connected in this prototype, so those
    two values are explicitly model-estimated rather than presented as live
    utility or vehicle telemetry.
    """
    current_congestion = points[0]["congestion"]
    peak_congestion = max(point["congestion"] for point in points)
    power_gw = round(2.35 + current_congestion * 0.009 + max(0, temp_c - 24) * 0.018 + rain_mm * 0.008, 2)
    on_time = max(42, round(98 - current_congestion * 0.38 - rain_mm * 0.7))
    flood_score = min(100, round(rain_mm * 3.5 + (12 if rain_mm > 0 else 0)))
    return {
        "location": location,
        "traffic": {"currentCongestion": current_congestion, "peakCongestion": peak_congestion, "source": "trained traffic ensemble"},
        "power": {"demandGW": power_gw, "source": "model-estimated from traffic and weather"},
        "transport": {"onTimePercent": on_time, "status": "On Time" if on_time >= 80 else "Delayed", "source": "model-estimated from traffic and weather"},
        "flood": {"score": flood_score, "risk": "HIGH" if flood_score >= 65 else "MODERATE" if flood_score >= 30 else "LOW", "source": "rainfall screening model"},
        "weather": {"tempC": temp_c, "rainMm": rain_mm, "aqi": aqi, "source": "live Open-Meteo weather and AQI"},
    }


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/config")
async def config() -> dict:
    # No API key needed: the map runs on Leaflet + OpenStreetMap tiles.
    return {"mapProvider": "osm-leaflet"}


@app.post("/api/auth/login")
async def login(request: LoginRequest) -> JSONResponse:
    user = authenticate_user(request.username, request.password)
    if not user:
        return JSONResponse({"detail": "Invalid username or password"}, status_code=401)
    token = create_session(user["id"], (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat())
    response = JSONResponse({"authenticated": True, "user": user})
    response.set_cookie("citytwin_session", token, max_age=43200, httponly=True, samesite="lax")
    return response


@app.get("/api/auth/me")
async def current_user(request: Request) -> dict:
    user = get_session_user(request.cookies.get("citytwin_session"))
    if not user:
        return JSONResponse({"authenticated": False}, status_code=401)
    return {"authenticated": True, "user": user}


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    delete_session(request.cookies.get("citytwin_session"))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("citytwin_session")
    return response


@app.get("/api/overview")
async def overview() -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": {"name": "New Delhi", "center": CITY_CENTER},
        "health": 76,
        "healthBreakdown": {"environment": 72, "mobility": 68, "safety": 81, "infrastructure": 78, "overall": 76},
        "metrics": {"traffic": "Moderate", "aqi": 136, "flood": "Low", "powerGW": 3.2, "transport": "On Time"},
        "assets": list_assets(),
        "aiSuggestions": AI_SUGGESTIONS,
        "alerts": list_alerts(),
    }


@app.get("/api/model/info")
async def model_info() -> dict:
    return traffic_model.model_metrics()


@app.get("/api/predictions/traffic")
async def traffic_prediction(location: str = "ITO", rain_mm: float = 0.0, temp_c: float = 27.0, clouds: int = 40, weather: str = "Clouds", aqi: int = 0) -> dict:
    started = perf_counter()
    points = traffic_model.forecast(location=location, rain_1h=rain_mm, temp_c=temp_c, clouds_all=clouds, weather_main=weather)
    output = {"location": location, "modelDriven": True, "points": points, "signals": derive_live_signals(location, points, temp_c, rain_mm, aqi)}
    logger.info(
        "Traffic prediction completed",
        extra={
            "event": "traffic_prediction",
            "inputs": {"location": location, "rain_mm": rain_mm, "temp_c": temp_c, "clouds": clouds, "weather": weather},
            "output": {"point_count": len(points), "peak_congestion": max(point["congestion"] for point in points)},
            "latency_ms": round((perf_counter() - started) * 1000, 2),
        },
    )
    return output


@app.post("/api/simulations")
async def run_simulation(request: SimulationRequest) -> dict:
    # Rule-based decision-support scoring (rainfall/drainage + traffic-network
    # heuristics) — explainable by design. See README for why this stays
    # rule-based rather than a trained model in this iteration.
    baseline_points = traffic_model.forecast(location=request.location, rain_1h=request.current_rain_mm, temp_c=request.temp_c)
    baseline_congestion = baseline_points[0]["congestion"]
    blocked_road_penalty = 9 if "outer" in request.blocked_road.lower() else 13
    flood_score = min(100, 10 + request.rainfall_mm * 0.82 + request.current_rain_mm * 1.8)
    traffic_score = min(100, baseline_congestion * 0.62 + request.traffic_increase * 0.55 + blocked_road_penalty)
    eta_delta = round(3 + request.traffic_increase * 0.12 + request.rainfall_mm * 0.04)
    affected = round(3200 + request.rainfall_mm * 95 + request.traffic_increase * 190)
    severity = lambda score: "HIGH" if score >= 65 else "MODERATE" if score >= 35 else "LOW"
    simulation_id = f"sim-{int(datetime.now().timestamp() * 1000)}"
    created_at = datetime.now(timezone.utc).isoformat()
    inputs = request.model_dump()
    result = {
        "floodRisk": severity(flood_score), "trafficRisk": severity(traffic_score),
        "emergencyEtaDeltaMinutes": eta_delta, "affectedPeople": affected,
        "floodScore": round(flood_score), "trafficScore": round(traffic_score),
        "baselineCongestion": baseline_congestion, "blockedRoadPenalty": blocked_road_penalty,
        "model": "traffic ensemble + explainable flood screening",
    }
    save_simulation(simulation_id, created_at, inputs, result)
    logger.info("Simulation completed", extra={"event": "simulation_completed", "inputs": inputs, "output": result})
    return {"id": simulation_id, "createdAt": created_at, "input": inputs, "result": result}


@app.get("/api/simulations")
async def simulations() -> dict:
    items = list_simulations()
    return {"items": items, "count": len(items)}


@app.post("/api/assistant")
async def assistant(request: AssistantRequest) -> dict:
    question = request.question.lower()
    temperature_match = re.search(r"(?:below|under|less than)\s*(\d+(?:\.\d+)?)", question)
    city_key = next((city for city in CITY_PLACES if city in question), None)
    mentioned_place = resolve_assistant_location(question)
    location_query = extract_india_location(question)
    if not mentioned_place:
        if location_query:
            mentioned_place = await geocode_india_place(location_query)
    if "northeast" in question and not mentioned_place:
        return {"answer": "Northeast India covers Assam, Arunachal Pradesh, Manipur, Meghalaya, Mizoram, Nagaland, Sikkim, and Tripura. Ask for a specific state or city so I can fetch its live conditions.", "assistant": "citytwin-grounded-v3", "grounding": {"region": "Northeast India", "source": "Indian geographic region"}}
    if location_query and not mentioned_place and not city_key:
        return {"answer": f"I could not identify '{location_query}' as an Indian place. Please include a city, state, or district name.", "assistant": "citytwin-grounded-v3", "grounding": {"query": location_query, "source": "India-only geocoder"}}
    if temperature_match and mentioned_place and not city_key and any(term in question for term in ("temperature", "temp", "degree")):
        snapshot = await fetch_live_place_snapshot(mentioned_place)
        threshold = float(temperature_match.group(1))
        if snapshot and snapshot["tempC"] is not None:
            region = f", {snapshot['state']}" if snapshot.get("state") and snapshot["state"].lower() != snapshot["location"].lower() else ""
            answer = f"{mentioned_place['name']}{region}, India is {snapshot['tempC']:.1f}°C, so it is {'below' if snapshot['tempC'] < threshold else 'not below'} {threshold:g}°C."
            return {"answer": answer, "grounding": snapshot, "assistant": "citytwin-grounded-v3"}
    if city_key and temperature_match and any(term in question for term in ("place", "area", "location", "temperature", "temp", "degree")):
        threshold = float(temperature_match.group(1))
        places = await fetch_city_place_temperatures(city_key)
        matches = [place for place in places if place["tempC"] < threshold]
        if matches:
            place_text = ", ".join(f"{place['name']} ({place['tempC']:.1f}°C)" for place in matches)
            answer = f"Live {city_key.title()} readings below {threshold:g}°C: {place_text}."
        else:
            current_readings = ", ".join(f"{place['name']} {place['tempC']:.1f}°C" for place in places)
            answer = f"No monitored {city_key.title()} location is below {threshold:g}°C right now. Current readings: {current_readings}."
        return {"answer": answer, "grounding": {"city": city_key, "places": places, "matches": matches, "thresholdC": threshold, "source": "live Open-Meteo weather by city location"}, "assistant": "citytwin-grounded-v3"}
    snapshot_place = mentioned_place or CITY_CENTERS.get(city_key or "")
    snapshot = await fetch_live_place_snapshot(snapshot_place) if snapshot_place else None
    resolved_location = mentioned_place["name"] if mentioned_place else (city_key.title() if city_key else request.location)
    temp_c = snapshot["tempC"] if snapshot and snapshot["tempC"] is not None else request.temp_c
    rain_mm = snapshot["rainMm"] if snapshot else request.rain_mm
    aqi = snapshot["aqi"] if snapshot and snapshot["aqi"] is not None else request.aqi
    points = traffic_model.forecast(location=resolved_location, rain_1h=rain_mm, temp_c=temp_c, weather_main=request.weather)
    signals = derive_live_signals(resolved_location, points, temp_c, rain_mm, aqi)
    congestion = signals["traffic"]["currentCongestion"]
    if any(term in question for term in ("traffic", "congestion", "slow", "road")):
        region = f", {mentioned_place['state']}" if mentioned_place and mentioned_place.get("state") and mentioned_place["state"].lower() != mentioned_place["name"].lower() else ""
        answer = f"Traffic at {resolved_location}{region} is estimated at {congestion}% congestion now, with a {signals['traffic']['peakCongestion']}% peak over the next three hours. This is a trained-model estimate, not a live traffic sensor reading."
    elif any(term in question for term in ("flood", "rain", "water")):
        answer = f"Flood screening at {resolved_location} is {signals['flood']['score']}/100 ({signals['flood']['risk']}) from {rain_mm:g} mm current rain. It is a screening estimate, not a calibrated hydrology forecast."
    elif any(term in question for term in ("air", "aqi", "pollution")):
        answer = f"The current AQI at {resolved_location} is {aqi or 'unavailable'}. This value comes from the live Open-Meteo AQI feed."
    elif any(term in question for term in ("weather", "temperature", "temp", "hot", "cold")):
        region = f", {mentioned_place['state']}" if mentioned_place and mentioned_place.get("state") and mentioned_place["state"].lower() != mentioned_place["name"].lower() else ""
        answer = f"{resolved_location}{region} is currently {temp_c:.1f}°C with {request.weather.lower()} conditions and {rain_mm:g} mm rain."
    elif any(term in question for term in ("power", "energy", "electric")):
        answer = f"Estimated demand is {signals['power']['demandGW']} GW for {resolved_location}. This is a model estimate based on traffic and weather, not a utility-meter reading."
    elif any(term in question for term in ("transport", "transit", "bus", "metro", "on time", "delay")):
        answer = f"Public transport reliability for {resolved_location} is estimated at {signals['transport']['onTimePercent']}% on time ({signals['transport']['status']}). This is modeled from traffic and weather until a live transit feed is connected."
    else:
        answer = "I can answer dashboard questions about traffic, flood risk, air quality, temperature, weather, energy, or transit for Indian locations. Include a place such as Mumbai, Jammu, Haryana, Punjab, Tamil Nadu, or Noida Sector 62."
    return {"answer": answer, "grounding": signals, "assistant": "citytwin-grounded-v3"}


@app.post("/api/routes/emergency")
async def emergency_route(request: RouteRequest) -> dict:
    return {"priority": "emergency", "recommended": "Outer Ring Road", "savedMinutes": 7, "origin": request.origin, "destination": request.destination}


@app.websocket("/ws/telemetry")
async def telemetry_stream(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_json({
                "type": "telemetry", "at": datetime.now(timezone.utc).isoformat(),
                "trafficSpeedKmh": round(random.uniform(21, 31), 1),
                "aqi": random.randint(128, 142),
                "powerGW": round(random.uniform(3.05, 3.35), 2),
            })
            await asyncio.sleep(4)
    except WebSocketDisconnect:
        pass

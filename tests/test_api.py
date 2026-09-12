from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_overview_uses_seeded_persistent_data():
    response = client.get("/api/overview")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["assets"]) == 5
    assert payload["assets"][0]["name"] == "AIIMS / Ring Road"
    assert len(payload["alerts"]) == 4


def test_prediction_shape_and_metadata():
    response = client.get("/api/predictions/traffic?location=ITO&rain_mm=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["modelDriven"] is True
    assert len(payload["points"]) == 13
    assert {"minute", "congestion", "predictedVolume"} <= payload["points"][0].keys()
    assert payload["signals"]["weather"]["source"] == "live Open-Meteo weather and AQI"
    assert payload["signals"]["power"]["source"].startswith("model-estimated")


def test_simulation_is_persisted_and_listed():
    response = client.post(
        "/api/simulations",
        json={"rainfall_mm": 120, "traffic_increase": 60, "blocked_road": "Ring Road - 2 km"},
    )

    assert response.status_code == 200
    simulation = response.json()
    history = client.get("/api/simulations").json()
    assert history["count"] >= 1
    assert any(item["id"] == simulation["id"] for item in history["items"])
    assert simulation["result"]["floodRisk"] == "HIGH"
    assert simulation["result"]["model"] == "traffic ensemble + explainable flood screening"


def test_grounded_assistant_answers_from_live_context():
    response = client.post(
        "/api/assistant",
        json={"question": "Why is traffic bad near ITO?", "location": "ITO", "temp_c": 32, "rain_mm": 4, "aqi": 142, "weather": "Rain"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["assistant"] == "citytwin-grounded-v3"
    assert "ITO" in payload["answer"]
    assert payload["grounding"]["weather"]["source"] == "live Open-Meteo weather and AQI"


def test_assistant_filters_live_delhi_places_by_temperature(monkeypatch):
    async def fake_temperatures(city):
        assert city == "delhi"
        return [{"name": "Rohini", "tempC": 28.4}, {"name": "Saket", "tempC": 31.2}]

    monkeypatch.setattr("main.fetch_city_place_temperatures", fake_temperatures)
    response = client.post("/api/assistant", json={"question": "Which places in Delhi have temperature below 30 degrees?"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["assistant"] == "citytwin-grounded-v3"
    assert "Rohini" in payload["answer"]
    assert "Saket" not in payload["answer"]


def test_assistant_supports_ghaziabad_places(monkeypatch):
    async def fake_temperatures(city):
        assert city == "ghaziabad"
        return [{"name": "Indirapuram", "tempC": 29.1}, {"name": "Vaishali", "tempC": 30.4}]

    monkeypatch.setattr("main.fetch_city_place_temperatures", fake_temperatures)
    response = client.post("/api/assistant", json={"question": "Which places in Ghaziabad have temperature below 30 degrees?"})

    assert response.status_code == 200
    payload = response.json()
    assert "Indirapuram" in payload["answer"]
    assert "Vaishali" not in payload["answer"]


def test_assistant_supports_noida_places(monkeypatch):
    async def fake_temperatures(city):
        assert city == "noida"
        return [{"name": "Sector 18", "tempC": 29.6}, {"name": "Sector 62", "tempC": 30.8}]

    monkeypatch.setattr("main.fetch_city_place_temperatures", fake_temperatures)
    response = client.post("/api/assistant", json={"question": "Which places in Noida have temperature below 30 degrees?"})

    assert response.status_code == 200
    payload = response.json()
    assert "Sector 18" in payload["answer"]
    assert "Sector 62" not in payload["answer"]


def test_assistant_uses_named_noida_sector_for_traffic(monkeypatch):
    async def fake_snapshot(place):
        assert place["name"] == "Sector 62"
        return {"location": "Sector 62", "tempC": 31.0, "rainMm": 0, "weatherCode": 3, "aqi": 90, "source": "live Open-Meteo weather and AQI"}

    monkeypatch.setattr("main.fetch_live_place_snapshot", fake_snapshot)
    response = client.post("/api/assistant", json={"question": "Which place in Noida Sector 62 has traffic?"})

    assert response.status_code == 200
    assert "Sector 62" in response.json()["answer"]


def test_assistant_geocodes_any_indian_city(monkeypatch):
    async def fake_geocode(query):
        assert query == "mumbai"
        return {"name": "Mumbai", "state": "Maharashtra", "country": "India", "latitude": 19.076, "longitude": 72.8777}

    async def fake_snapshot(place):
        assert place["name"] == "Mumbai"
        return {"location": "Mumbai", "state": "Maharashtra", "country": "India", "tempC": 29.2, "rainMm": 2, "weatherCode": 2, "aqi": 88, "source": "live Open-Meteo weather and AQI"}

    monkeypatch.setattr("main.geocode_india_place", fake_geocode)
    monkeypatch.setattr("main.fetch_live_place_snapshot", fake_snapshot)
    response = client.post("/api/assistant", json={"question": "What is the weather in Mumbai?"})

    assert response.status_code == 200
    assert "Mumbai, Maharashtra" in response.json()["answer"]


def test_assistant_supports_west_bengal_state_center(monkeypatch):
    async def fake_snapshot(place):
        assert place["name"] == "West Bengal"
        return {"location": "West Bengal", "state": "West Bengal", "country": "India", "tempC": 28.8, "rainMm": 3, "weatherCode": 61, "aqi": 96, "source": "live Open-Meteo weather and AQI"}

    monkeypatch.setattr("main.fetch_live_place_snapshot", fake_snapshot)
    response = client.post("/api/assistant", json={"question": "What is the temperature in West Bengal?"})

    assert response.status_code == 200
    assert "West Bengal" in response.json()["answer"]


def test_assistant_does_not_fallback_for_unknown_place():
    response = client.post("/api/assistant", json={"question": "What is the temperature in Atlantis?"})

    assert response.status_code == 200
    assert "could not identify" in response.json()["answer"]
    assert "New Delhi" not in response.json()["answer"]


def test_auth_login_session_and_logout():
    invalid = client.post("/api/auth/login", json={"username": "operator", "password": "wrong-password"})
    assert invalid.status_code == 401

    login = client.post("/api/auth/login", json={"username": "operator", "password": "citytwin-demo"})
    assert login.status_code == 200
    assert login.json()["user"]["username"] == "operator"
    assert "citytwin_session" in client.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert client.get("/api/auth/me").status_code == 401

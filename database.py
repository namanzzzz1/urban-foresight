"""SQLite persistence for CityTwin operational data."""
from __future__ import annotations

import json
import os
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("CITYTWIN_DB_PATH", BASE / "data" / "citytwin.db"))

SEED_ASSETS = [
    {"id": "connaught", "name": "Connaught Place", "lat": 28.6315, "lng": 77.2167, "status": "moderate", "type": "traffic", "label": "Moderate traffic"},
    {"id": "aiims", "name": "AIIMS / Ring Road", "lat": 28.5672, "lng": 77.2100, "status": "high", "type": "incident", "label": "Accident · 2 lane blockage"},
    {"id": "pragati", "name": "Pragati Maidan", "lat": 28.6176, "lng": 77.2431, "status": "high", "type": "traffic", "label": "High congestion"},
    {"id": "india-gate", "name": "India Gate", "lat": 28.6129, "lng": 77.2295, "status": "normal", "type": "traffic", "label": "Normal"},
    {"id": "yamuna", "name": "Yamuna Basin", "lat": 28.6140, "lng": 77.2580, "status": "moderate", "type": "flood", "label": "Flood risk: Moderate"},
]
SEED_ALERTS = [
    {"title": "Road Accident", "location": "Ring Road, Near AIIMS", "severity": "high", "age": "5 min ago"},
    {"title": "Rising Water Level", "location": "Yamuna Basin", "severity": "medium", "age": "12 min ago"},
    {"title": "High Air Pollution", "location": "Anand Vihar", "severity": "medium", "age": "18 min ago"},
    {"title": "Power Fluctuation", "location": "East Delhi", "severity": "low", "age": "24 min ago"},
]
DEFAULT_USERNAME = os.getenv("CITYTWIN_DEFAULT_USERNAME", "operator")
DEFAULT_PASSWORD = os.getenv("CITYTWIN_DEFAULT_PASSWORD", "citytwin-demo")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    salt_hex, digest_hex = encoded.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 240_000).hex()
    return hmac.compare_digest(candidate, digest_hex)


def init_db() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                status TEXT NOT NULL,
                type TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                location TEXT NOT NULL,
                severity TEXT NOT NULL,
                age TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulations (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                inputs_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        now = datetime.now(timezone.utc).isoformat()
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            connection.execute(
                "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                (DEFAULT_USERNAME, _hash_password(DEFAULT_PASSWORD), "CityTwin Operator", now),
            )
        if connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0:
            connection.executemany(
                "INSERT INTO assets (id, name, lat, lng, status, type, label, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(a["id"], a["name"], a["lat"], a["lng"], a["status"], a["type"], a["label"], now) for a in SEED_ASSETS],
            )
        if connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0:
            connection.executemany(
                "INSERT INTO alerts (title, location, severity, age, created_at) VALUES (?, ?, ?, ?, ?)",
                [(a["title"], a["location"], a["severity"], a["age"], now) for a in SEED_ALERTS],
            )


def list_assets() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute("SELECT id, name, lat, lng, status, type, label FROM assets ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def list_alerts() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute("SELECT title, location, severity, age FROM alerts ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def save_simulation(simulation_id: str, created_at: str, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO simulations (id, created_at, inputs_json, result_json) VALUES (?, ?, ?, ?)",
            (simulation_id, created_at, json.dumps(inputs), json.dumps(result)),
        )


def list_simulations(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, created_at, inputs_json, result_json FROM simulations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": row["id"], "createdAt": row["created_at"], "input": json.loads(row["inputs_json"]), "result": json.loads(row["result_json"])}
        for row in rows
    ]


def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute("SELECT id, username, password_hash, display_name FROM users WHERE lower(username) = lower(?)", (username.strip(),)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "displayName": row["display_name"]}


def create_session(user_id: int, expires_at: str) -> str:
    token = secrets.token_urlsafe(32)
    with _connect() as connection:
        connection.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)", (token, user_id, datetime.now(timezone.utc).isoformat(), expires_at))
    return token


def get_session_user(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _connect() as connection:
        row = connection.execute(
            "SELECT u.id, u.username, u.display_name, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    return {"id": row["id"], "username": row["username"], "displayName": row["display_name"]}


def delete_session(token: str | None) -> None:
    if token:
        with _connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))

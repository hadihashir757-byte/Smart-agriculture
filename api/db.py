"""SQLite storage for sensor readings and plant-health predictions.

Kept deliberately simple (stdlib sqlite3, no ORM) since this runs on a free
hosting tier with modest traffic. Note: most free hosts (e.g. Render's free
web service) have an EPHEMERAL filesystem - this file is wiped on redeploy/
restart. That's fine for "latest reading" lookups used by /get-recommendation,
but don't treat this as durable long-term storage. If you want the sensor
history to survive restarts, point DB_PATH at a mounted persistent disk (paid
tier) or periodically export to external storage.
"""

import json
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "app.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            temperature_c REAL,
            humidity_pct REAL,
            soil_pct INTEGER,
            light_pct INTEGER,
            rain_pct INTEGER,
            water_pct INTEGER,
            pump_status INTEGER,
            crop_type TEXT,
            plant_stage TEXT
        );

        CREATE TABLE IF NOT EXISTS plant_health_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            predicted_class TEXT,
            confidence REAL,
            accepted INTEGER,      -- 0 if rejected by quality gate, 1 if a real prediction was made
            reject_reason TEXT,
            growth_stage TEXT,     -- 'seedling' or 'mature', see growth_stage.py
            top_predictions TEXT   -- JSON list of [label, probability] for the top few diseases
        );

        CREATE TABLE IF NOT EXISTS growth_baseline (
            id INTEGER PRIMARY KEY CHECK (id = 1),  -- single row, always id=1
            baseline_ratio REAL NOT NULL,
            set_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS irrigation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            water INTEGER,
            duration_sec INTEGER,
            reason TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def insert_sensor_reading(data):
    conn = get_connection()
    conn.execute(
        """INSERT INTO sensor_readings
           (ts, temperature_c, humidity_pct, soil_pct, light_pct, rain_pct,
            water_pct, pump_status, crop_type, plant_stage)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            data.get("temperature_c"),
            data.get("humidity_pct"),
            data.get("soil_pct"),
            data.get("light_pct"),
            data.get("rain_pct"),
            data.get("water_pct"),
            data.get("pump_status"),
            data.get("crop_type", "tomato"),
            data.get("plant_stage", "mature"),
        ),
    )
    conn.commit()
    conn.close()


def latest_sensor_reading():
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sensor_readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_plant_health_prediction(predicted_class, confidence, accepted, reject_reason=None,
                                    growth_stage=None, top_predictions=None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO plant_health_predictions
           (ts, predicted_class, confidence, accepted, reject_reason, growth_stage, top_predictions)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), predicted_class, confidence, int(accepted), reject_reason, growth_stage,
         json.dumps(top_predictions) if top_predictions is not None else None),
    )
    conn.commit()
    conn.close()


def latest_accepted_plant_health():
    conn = get_connection()
    row = conn.execute(
        """SELECT * FROM plant_health_predictions
           WHERE accepted = 1 ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_growth_baseline():
    conn = get_connection()
    row = conn.execute("SELECT * FROM growth_baseline WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def set_growth_baseline(ratio):
    conn = get_connection()
    conn.execute(
        """INSERT INTO growth_baseline (id, baseline_ratio, set_at) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET baseline_ratio = excluded.baseline_ratio, set_at = excluded.set_at""",
        (ratio, time.time()),
    )
    conn.commit()
    conn.close()


def insert_irrigation_log(water, duration_sec, reason):
    conn = get_connection()
    conn.execute(
        "INSERT INTO irrigation_log (ts, water, duration_sec, reason) VALUES (?, ?, ?, ?)",
        (time.time(), int(water), duration_sec, reason),
    )
    conn.commit()
    conn.close()

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

# Where the database lives. Point DATA_DIR at a folder that SURVIVES restarts
# (a normal folder on your own server is fine) so the growth baseline,
# decision log and cooldowns are not lost.
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")


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

        CREATE TABLE IF NOT EXISTS zone_growth_baseline (
            zone_id INTEGER PRIMARY KEY,
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

        CREATE TABLE IF NOT EXISTS watering_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            zone_id INTEGER,
            pump INTEGER,
            seconds INTEGER,
            decision TEXT NOT NULL,      -- 'water' or 'skip'
            reason TEXT,
            command_id TEXT,
            status TEXT NOT NULL,        -- recommended | dry_run | sent_accepted | sent_rejected | send_failed | skipped
            station_reply TEXT,          -- JSON reply from the station (verified result), if any
            inputs TEXT                  -- JSON of the readings the decision was based on
        );

        CREATE TABLE IF NOT EXISTS app_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),  -- single row, always id=1
            demo_mode INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        """
    )
    # older databases: add the zone column to plant_health_predictions
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(plant_health_predictions)")]
    if "zone_id" not in cols:
        conn.execute("ALTER TABLE plant_health_predictions ADD COLUMN zone_id INTEGER")
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
                                    growth_stage=None, top_predictions=None, zone_id=None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO plant_health_predictions
           (ts, predicted_class, confidence, accepted, reject_reason, growth_stage, top_predictions, zone_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), predicted_class, confidence, int(accepted), reject_reason, growth_stage,
         json.dumps(top_predictions) if top_predictions is not None else None, zone_id),
    )
    conn.commit()
    conn.close()


def latest_accepted_plant_health(max_age_s=None, zone_id=None):
    """Newest accepted prediction. If max_age_s is given and the newest one is
    older than that, return None (a stale photo result must not drive decisions).
    With zone_id: only that zone's photos (and photos that carry no zone, e.g. uploaded by hand)."""
    conn = get_connection()
    if zone_id is None:
        row = conn.execute(
            """SELECT * FROM plant_health_predictions
               WHERE accepted = 1 ORDER BY ts DESC LIMIT 1"""
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM plant_health_predictions
               WHERE accepted = 1 AND (zone_id = ? OR zone_id IS NULL) ORDER BY ts DESC LIMIT 1""",
            (int(zone_id),),
        ).fetchone()
    conn.close()
    if not row:
        return None
    row = dict(row)
    if max_age_s is not None and (time.time() - row["ts"]) > max_age_s:
        return None
    return row


def get_growth_baseline(zone_id=None):
    """The growth baseline of one zone (zone_id given) or the single shared one (no zone)."""
    conn = get_connection()
    if zone_id is None:
        row = conn.execute("SELECT * FROM growth_baseline WHERE id = 1").fetchone()
    else:
        row = conn.execute("SELECT * FROM zone_growth_baseline WHERE zone_id = ?", (int(zone_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_growth_baseline(ratio, zone_id=None):
    conn = get_connection()
    if zone_id is None:
        conn.execute(
            """INSERT INTO growth_baseline (id, baseline_ratio, set_at) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET baseline_ratio = excluded.baseline_ratio, set_at = excluded.set_at""",
            (ratio, time.time()),
        )
    else:
        conn.execute(
            """INSERT INTO zone_growth_baseline (zone_id, baseline_ratio, set_at) VALUES (?, ?, ?)
               ON CONFLICT(zone_id) DO UPDATE SET baseline_ratio = excluded.baseline_ratio, set_at = excluded.set_at""",
            (int(zone_id), ratio, time.time()),
        )
    conn.commit()
    conn.close()


def get_demo_mode():
    conn = get_connection()
    row = conn.execute("SELECT demo_mode FROM app_state WHERE id = 1").fetchone()
    conn.close()
    return bool(row["demo_mode"]) if row else False


def set_demo_mode(enabled):
    conn = get_connection()
    conn.execute(
        """INSERT INTO app_state (id, demo_mode, updated_at) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET demo_mode = excluded.demo_mode, updated_at = excluded.updated_at""",
        (int(enabled), time.time()),
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


# ---------------- watering decisions (cooldown + audit log) ----------------

def insert_watering_decision(zone_id, pump, seconds, decision, reason, command_id, status, inputs):
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO watering_decisions
           (ts, zone_id, pump, seconds, decision, reason, command_id, status, inputs)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), zone_id, pump, seconds, decision, reason, command_id, status,
         json.dumps(inputs) if inputs is not None else None),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def update_watering_decision(row_id, status, station_reply=None):
    conn = get_connection()
    conn.execute(
        "UPDATE watering_decisions SET status = ?, station_reply = ? WHERE id = ?",
        (status, json.dumps(station_reply) if station_reply is not None else None, row_id),
    )
    conn.commit()
    conn.close()


def last_watering_ts(zone_id):
    """Time of the last water command for this zone that the station did not
    reject (recommended-only / dry-run / skipped rows do not start a cooldown)."""
    conn = get_connection()
    row = conn.execute(
        """SELECT ts FROM watering_decisions
           WHERE zone_id = ? AND decision = 'water' AND status = 'sent_accepted'
           ORDER BY ts DESC LIMIT 1""",
        (zone_id,),
    ).fetchone()
    conn.close()
    return row["ts"] if row else None


def recent_watering_decisions(limit=50):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM watering_decisions ORDER BY ts DESC LIMIT ?", (int(limit),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- small key/value state for the background agent (survives restarts) ----
def _ensure_state_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT)")


def get_state(key, default=None):
    conn = get_connection()
    _ensure_state_table(conn)
    row = conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_state(key, value):
    conn = get_connection()
    _ensure_state_table(conn)
    conn.execute("INSERT INTO agent_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    conn.commit()
    conn.close()

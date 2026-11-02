# What changed in this branch (rover-thingsboard-integration)

Security
- `api/auth.py`: API_KEY is now required (no built-in default), header-only auth, constant-time compare.
  Key-in-URL is off unless `ALLOW_QUERY_API_KEY=true`.

Reliability
- `DATA_DIR` setting so the database lives on a folder that survives restarts (fixes the lost growth baseline).
- Plant-health results older than `PLANT_HEALTH_MAX_AGE_S` are ignored.

New AI -> station path
- `POST /zone-event`: zone readings in, safe decision out (`api/rover_routes.py`, `api/watering.py`).
- Safety layer: known zones only, stale/missing soil, rain, per-zone cooldown, seconds clamped,
  unique `command_id` + `issued_at` on every command.
- `api/thingsboard.py`: sends the `water` RPC (two-way) and records the station's real reply; reads the tank level.
- Decision log in SQLite + `GET /decisions`. `AUTO_WATER=false` by default (dry run).
- Tests: `tests/test_watering.py`. Docs: `docs/`. Service file: `deploy/`.

NOT tested against a live ThingsBoard account or the real station - test with AUTO_WATER=false first.

## Update: smarter watering rules
- Rain forecast >= 5 mm: skip if soil >= 40%, otherwise a short top-up (max 15 s). Critical (<20%) still waters.
- Plant-health photo result now matters (confident >= 0.6 and fresh): disease -> skip (gentle 15 s max if soil <20%), water stress -> +5 s, overwatered -> skip.
- Tank level unreadable -> skip (when ThingsBoard is configured).

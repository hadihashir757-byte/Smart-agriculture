# Smart Agriculture – Project Handoff

_Written 3 Nov 2026. No passwords, tokens or Wi-Fi details are in this file. Keep them in `.env` and in the Arduino sketches on your own computer only._

## 1. What the project does
A small farm that waters itself. A **rover** (ESP32) drives a line between plant zones, measures soil, temperature, humidity, light and rain, and reports to **ThingsBoard**. An **AI server** on the Mac reads those reports, looks at the weather forecast and a phone-camera photo of the plant, and decides whether, how long and in how many turns to water. The **irrigation station** (Arduino UNO R4 WiFi) controls two pumps and watches the water tank, and it can refuse any command that is unsafe.

```
 Rover (ESP32) --Wi-Fi/MQTT--> ThingsBoard cloud <--REST/RPC-- AI server (Mac, Flask + agent)
      |  UDP every 3 s (zone, at_zone, estop)                         |  weather (Open-Meteo)
      v                                                               |  phone camera (LensCast snapshot)
 Irrigation station (UNO R4) <--MQTT RPC "water"/"setPumpN"-----------+
   2 pumps via relays, tank sensor, MQ135 air sensor
```

## 2. Parts and where they live
| Part | Where | Notes |
|---|---|---|
| Rover firmware **r15** | ESP32, Arduino IDE | DHT11 on D21, live mode, holdZone, two-zone route (section 3a). Sent as a file in chat (contains secrets). Placeholder copy in `arduino/rover_esp32/` (PR #4). |
| Station firmware **v0.6** | UNO R4 WiFi | Pumps D7/D8, HC-SR04 D9/D10, MQ135 A0. Sent as a file in chat. |
| AI server | Mac, `api/` | Flask web server + background agent (`api/agent.py`), both started by launchd (`deploy/`). |
| Cloud | ThingsBoard EU free plan | Devices: `rover 01`, `irrigation station`, `ai_server`. Dashboards: Rover (main), Station. |
| Camera | Android phone + LensCast app | Agent fetches `/snapshot`. Not yet installed/tested. |
| Code | github.com/hadihashir757-byte/Smart-agriculture | Open PRs #2, #3, #4, #5. Merge order: #3, then #5, then #2 and #4. |

## 3. How decisions are made
**Normal mode** (`api/watering.py`, `irrigation_rules.py`, `water_amount.py`)
1. Skip if: tank below 15 % or unknown, soil at or above 80 %, rain detected now, reading older than 15 min, same zone watered in the last 30 min, plant has a fungal disease (unless soil is critical).
2. Critically dry (below 20 %): water even if rain is forecast.
3. Rain forecast 5 mm or more: skip if soil is 40 % or higher, otherwise a short top-up (max 15 s, one turn).
4. Otherwise water when soil is below the crop/stage start point (tomato 30/40, plantain 50/60, pepper 28/35, mango 25/40, other 30/35 for mature/seedling).
5. **How long:** soil gap to the comfortable level (tomato 60 %, plantain 70 %, pepper 60 %, mango 50 %, seedling +5 %) × crop factor (tomato 1.0, pepper 0.85, plantain 1.25, mango 1.1) × stage factor (seedling 0.7) × weather factor (32 °C+ ×1.2, 18 °C− ×0.85, humidity ≤30 % ×1.1, ≥85 % ×0.9), then plant-health adjustment (stress +5 s, fungal gentle). Limits 5–60 s.
6. **In how many turns:** at most 20 s per turn, 60 s rest between turns, soil re-measured after each turn, stops early at the comfortable level.
7. The rover must be stopped at that zone, otherwise no pump starts. The server checks this and the station checks it again.

**Demo mode** (`api/irrigation_plan.py`, dashboard switch)
- No rain expected: pump 3 s, wait 5 s, three times. If soil is still under 80 %, 5 s bursts (soil measured after each) until 80 %.
- Rain expected: only up to 40 %. Pump 5 s, wait 8 s, twice, then 5 s bursts until 40 %.
- Stops on: tank low, rain, demo switched off, station refusal, rover not at zone, or 10 extra bursts.
- The rover is asked to stay at the zone while watering (max 5 min, renewed each turn).

Every decision writes a plain-English report (CHECKED / DECISION / WHY) to ThingsBoard (`ai_report`), shown on the dashboard.

## 3a. Rover route (firmware r16, line mode)
Tape layout: `ZONE 2 --- CENTRE --- ZONE 1`. The centre is plain tape (reads 010), so the rover **estimates distance** (speed x time) to find it. Zone 1 is where the tape ends (010, then 000). Zone 2 is a cross-tape (111).
1. Start on the centre, switch to line mode, drive forward slowly (soft start).
2. Tape ends: **zone 1**. Stop, `zone_arrived` (new `arrival_id`, the server takes one photo), stay at least 60 s (probe, readings, AI watering; longer while the AI holds the rover, max 5 min).
3. Reverse slowly (steering flipped) until the estimated position is the centre, wait 2 s, reverse on until the sensors read 111 = **zone 2**. Photo, stay at least 60 s.
4. Drive forward until the estimated position is the centre, stop at once, switch **line mode OFF**, report `route_finished`.
**Rain** (intensity above 15 % for 2 s): stop what it is doing, lift the soil arm, go back to the centre (reverse from the zone 1 side, forward from the zone 2 side), send the event `alert_rain_return` (telemetry `rain_abort = true`, alarm "Rain: rover returned to centre"), then `rain_return_done` and line mode OFF.
Telemetry: `route_phase`, `route_pos_cm`, `rain_abort`, `at_zone`, `arrival_id`. Tunables at the top of the sketch: ZONE_STAY_MS, CENTRE_PAUSE_MS, FWD_SPEED_CM_S, REV_SPEED_CM_S, ZONE1_DIST_CM, ZONE2_DIST_CM, SPEED_LINE, SPEED_REVERSE, LINE_RAMP_PWM_PER_S, RAIN_RETURN_PERCENT. **Accuracy:** the centre is only as accurate as the speed numbers; measure the real cm/s forward and reverse, and enter the real distances ZONE1_DIST_CM / ZONE2_DIST_CM if you know them. The rover also accepts `measureSoil` while stopped at a zone in line mode.

**Two crops (per-zone).** `ZONE_CROP_MAP` says which crop grows in which zone (zones not listed use `DEFAULT_CROP`). The crop of the zone picks the watering rules, and each zone has its own photo health result and growth baseline (`plant_health_predictions.zone_id`, table `zone_growth_baseline`), so a mango photo never changes a tomato decision. Mango has no disease model until you train one (`python3 api/train_plant_health_mango.py` on a folder of mango leaf photos, one sub-folder per class, in `data/mango_leaf_disease/`); the server then uses `models/plant_health_model_mango.joblib` automatically. Until then its photo still sets the growth stage. Photos uploaded by hand without `zone_id` count for every zone.

## 4. Safety design
- **The station has the last word.** It refuses: e-stop, bad pump, bad duration, duplicate or expired command, clock not synced, tank low or invalid, rover link lost, rover not at zone, pump already running, other pump running, pump resting. Limits: normal 30 s per run + 60 s rest; demo 5 s + 4 s rest.
- **E-stop** stops everything and blocks new commands.
- **Manual pump switches** (dashboard) bypass the tank, rover, rest and time checks and run until switched off. In demo mode they switch off after 5 minutes. E-stop still stops them.
- `AUTO_WATER=false` is the default: the server only recommends. Turn it on only after the logs look right.
- Mango has watering rules but no disease-photo model yet (the photo is still used for the growth stage). Soil sensor is **not calibrated yet**, so no soil percentage is sent and the AI cannot act on real readings.

## 5. Data traffic (ThingsBoard free plan)
Limits seen on the account: 500,000 messages a month, 5 dashboards (4 used), 5 devices, 200 alarms. Normal mode: each board sends all values every 5 minutes. **Live mode** (dashboard switch, auto-off after 30 min) sends every 5 s. Events (controls, e-stop, soil measurement, pump start/stop, alerts) are sent at once.

## 6. Dashboard (Rover) – what is on it
Controls: E-stop, clear E-stop, forward/back/left/right, turns, measure soil, line-mode switch, speed slider and lock. Sensors: temperature, humidity, light, soil, rain. AI cards: decision, water seconds (total), turns, status, zone, plant health, plant state, AI reason, care tip, target %, AI explanation, AI live step, **AI reasoning report**. Station: Live data, DEMO mode, Manual pump 1 and 2 switches, Pump 1/2 value cards and LEDs, water tank gauge, Demo/Live status cards.

## 7. Alarms (ThingsBoard → Alarms → Alarm rules)
Rover: Emergency stop active (critical), Line lost or timeout (major), Rain sensor blocked (minor), Temperature sensor failed (minor), High temperature 38 °C+ (warning), Rover offline (major).
Station: Tank low below 15 % (critical), Emergency stop active (critical), Tank sensor invalid (major), Rover link lost at station (major), Pump 1 / Pump 2 running longer than 3 minutes (major), Station offline (major).
Devices use the profiles "Rover" and "Irrigation station". No e-mail or phone notifications are set up; alarms show on the Alarms page and the bell icon.

## 8. Names you will need
**RPC methods.** Rover: move, turn, setSpeed, setSafety, setTurnCal, setMode/setValue, measureSoil, stop, estop, clearEstop, setLive/getLive, holdZone. Station: water, setDemo/getDemo, setLive/getLive, setPump1/2 and getPump1/2, stopAll, estop, clearEstop, getStatus.
**Server settings (`.env`).** API_KEY, DATA_DIR, LOCATION_LAT/LON, ZONE_PUMP_MAP, ZONE_CROP_MAP (e.g. `1:tomato,2:mango`), DEFAULT_CROP, AUTO_WATER, TB_BASE_URL, TB_USERNAME, TB_PASSWORD, STATION_DEVICE_NAME, ROVER_DEVICE_NAME, AI_DEVICE_TOKEN, CAMERA_BASE_URL, CAMERA_USER, CAMERA_PASSWORD, AGENT_POLL_S (10 recommended), PHOTO_MODE (zone | timer | both), PHOTO_ARRIVAL_MAX_AGE_S, PHOTO_INTERVAL_S. Quote values that contain spaces.

## 9. Tests
124 automated tests pass (`pytest`). 50 end-to-end scenarios (normal mode, seconds and turns, demo plans, plan execution) are in `tests/scenarios.py`; results are in `docs/SCENARIO_REPORT.md`. These use a simulated station and ThingsBoard. **Nothing has run on the real hardware yet.**

## 10. To do next
1. Upload rover r15 and station v0.6. Set the same shared key in both. Check relay polarity, calibrate the tank distances, wire pump power with flyback diodes.
2. Calibrate the soil sensor (dry-air and in-water readings).
3. Merge the PRs, update the Mac, restart the agent, fill `.env`, keep `AUTO_WATER=false`, check the AI reasoning report, then try demo mode.
4. Install LensCast, set a password, set `CAMERA_BASE_URL`. The agent now takes one photo each time the rover arrives at a zone (`PHOTO_MODE=zone`, needs `AGENT_POLL_S=10`).
5. Not built: dashboard buttons for photo and zoom, live camera video on the dashboard (needs an HTTPS link to the Mac), alarm notifications by e-mail.
6. Keep the Mac awake while the system runs (`sudo pmset -c sleep 0 disksleep 0`).

## 11. Rules for the repository
Never commit Wi-Fi passwords, ThingsBoard passwords, device tokens or the `.env` file. The sketches that contain them stay on your computer.

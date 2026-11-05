# Rover -> AI server -> irrigation station

```
Rover (ESP32) --telemetry--> ThingsBoard --rule chain (REST API call)--> POST /zone-event
                                                                           |
                  irrigation station <--RPC "water" (two-way)-- ThingsBoard REST <--+  (only if AUTO_WATER=true)
```

The server only REQUESTS watering. The station still checks everything locally and can refuse
(tank low, duplicate, expired, resting, too long). The station's real answer is stored in the
decision log (`GET /decisions`, field `status`: `sent_accepted` / `sent_rejected` / ...), so
"recommended" and "actually done" are never confused.

## 1. Test by hand first (dry run, AUTO_WATER=false)

```bash
curl -s -X POST https://YOUR-SERVER/zone-event \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"zone_id":1,"soil_moisture_percent":22,"temperature_c":29,"humidity_percent":55,"rain_detected":false}'
curl -s -H "X-API-Key: $API_KEY" https://YOUR-SERVER/decisions
```

Fields: `zone_id` (required), `soil_moisture_percent`, `temperature_c`, `humidity_percent`,
`light_percent`, `rain_detected`, optional `crop_type`, `tank_level_percent`, `reading_ts` (epoch s).

## 2. ThingsBoard rule chain (rover -> server)

In the Root Rule Chain add: **Message type switch** (Post telemetry) -> **Script filter**
(`return msg.event == 'soil_measured';`) -> **REST API call**
- URL: `https://YOUR-SERVER/zone-event`, method POST
- Header `X-API-Key`: your key  (Content-Type: application/json)
- Body: the message itself. Make sure the rover's `soil_measured` telemetry carries
  `zone_id`, `soil_moisture_percent`, `temperature_c`, `humidity_percent`, `rain_detected`
  (a Script transformation node can add missing keys from the device's latest telemetry).

Note: the rover only sends `soil_moisture_percent` after `SOIL_CALIBRATED` is set to `true`
in the rover firmware - until then the server will correctly answer "no soil moisture reading".

## 3. Turning real watering on

Set in `.env`: `TB_USERNAME`, `TB_PASSWORD` (use a dedicated ThingsBoard user), then `AUTO_WATER=true`
and restart. Watch `/decisions` for the first few runs. Keep the station's own limits as the final authority.

## 4. Optional dashboard feed

Create a ThingsBoard device `ai_server`, put its token in `AI_DEVICE_TOKEN`. Each decision is then
published as telemetry: `ai_zone_id`, `ai_decision`, `ai_seconds`, `ai_reason`, `ai_status`, `ai_plant_label`.

## Watering rules (summary)
| Situation | Result |
|---|---|
| Tank < 15% or unreadable | skip |
| Raining now (rain sensor) | skip |
| Soil >= 80% | skip |
| Soil < 20% | water (max 30 s) even if rain is forecast |
| Rain forecast >= 5 mm, soil >= 40% | skip, wait for rain |
| Rain forecast >= 5 mm, soil 20-40% | short top-up, max 15 s |
| Otherwise | water if below the crop/stage start point (tomato 40/30, plantain 60/50) |
| Seconds | 20 + gap to start point, +10 if hot (>=32 C) or dry air (<=30%), clamped 5-30 |
| Photo: fungal disease (blight, spot, mold, sigatoka, cordana, pestalotiopsis) | skip; if soil < 20% gentle, max 15 s |
| Photo: mites / virus / healthy | no change |
| Photo: wilt / water stress | +5 s |
| Photo: overwatered | skip |
| Same zone watered < 30 min ago | skip |
Photo results only count if confidence >= 0.6 and the photo is under 2 hours old.

## AI cards on the Rover dashboard
Create a ThingsBoard device `ai_server`, copy its access token into `AI_DEVICE_TOKEN` in `.env`, restart.
The server then sends: ai_decision, ai_seconds, ai_reason, ai_status, ai_zone_id (after each zone event) and
ai_plant_label, ai_plant_stage, ai_care_tip, ai_plant_confidence (after each accepted plant photo).
Empty values are never sent, so a good value is not overwritten by a blank.


## Photos on the dashboard

Every processed photo is shrunk to a small JPEG (about 240 px, roughly 10 KB) and sent to the `ai_server` device as telemetry:
`ai_photo_zone1`, `ai_photo_zone2` (photos taken at that zone) and `ai_photo_custom` (photos sent to `/upload-image` without a `zone_id`),
each with a `..._label` key (zone, crop, time, and why it was rejected if it was). The Rover dashboard has three cards
("Zone 1 photo", "Zone 2 photo", "My custom photo") that show them, plus a "Live camera" card with the camera stream
(an `https` tunnel address; see the handoff notes). The thumbnails are sent even when a photo is rejected by the quality check.

## Weather forecast on the dashboard

Every `WEATHER_PUSH_S` seconds (default 900) the agent sends the Open-Meteo forecast for `LOCATION_LAT` / `LOCATION_LON` to the `ai_server` device:
`ai_weather_condition`, `ai_weather_temp_c`, `ai_weather_rain_chance_pct`, `ai_weather_rain_mm_6h`, `ai_weather_wind_kmh`, `ai_weather_summary`.
The Rover dashboard has a "Weather forecast" card and four value cards (temperature, rain chance, rain in the next 6 h, wind).
The same forecast is what the watering decision uses to skip watering when rain is expected.

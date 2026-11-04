# Rover (ESP32) firmware r16
Line-following rover with a soil probe on a servo arm. Reads a one-wire DHT11/DHT22 on GPIO21, sends telemetry to ThingsBoard over MQTT, accepts RPC commands, and talks to the irrigation station over UDP.

## Route (line mode)
Tape layout: `ZONE 2 --- CENTRE --- ZONE 1`. The centre is plain tape, found by estimated distance. Zone 1 is where the tape ends. Zone 2 is a cross-tape (all three IR sensors black).
1. Start on the centre, drive forward slowly to zone 1, stay 60 s (photo, soil probe, AI irrigation).
2. Reverse to the centre, wait 2 s, reverse on to zone 2, stay 60 s.
3. Drive forward to the centre, stop, switch line mode off.
Rain above 15 %: stop, return to the centre, send an alarm (`alert_rain_return`), switch line mode off.

## Before uploading from the Arduino IDE
1. Replace `YOUR_WIFI_NAME`, `YOUR_WIFI_PASSWORD`, `YOUR_ROVER_THINGSBOARD_TOKEN` near the top of the file, and set `STATION_KEY` to the same value as in the station sketch. Never commit the real values.
2. Install libraries: DHT sensor library (Adafruit) + Adafruit Unified Sensor, PubSubClient, ArduinoJson (7.x).
3. Set `DHT_TYPE` (DHT11 = blue sensor, DHT22 = white).
4. Measure the real speeds (drive 1 metre in line mode and time it) and set `FWD_SPEED_CM_S` and `REV_SPEED_CM_S`. Enter `ZONE1_DIST_CM` / `ZONE2_DIST_CM` if you know the distances. See the ROUTE settings at the top of the sketch.
5. Calibrate the soil and rain sensors (`SOIL_*`, `RAIN_*` settings) before relying on the readings.

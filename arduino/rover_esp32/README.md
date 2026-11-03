# Rover (ESP32) firmware r11
Reads a one-wire DHT11/DHT22 on GPIO21, sends telemetry to ThingsBoard over MQTT, accepts RPC commands.

Before uploading from the Arduino IDE:
1. Replace `YOUR_WIFI_NAME`, `YOUR_WIFI_PASSWORD`, `YOUR_ROVER_THINGSBOARD_TOKEN` near the top of the file. Never commit the real values.
2. Install libraries: DHT sensor library (Adafruit) + Adafruit Unified Sensor, PubSubClient, ArduinoJson.
3. Set `DHT_TYPE` (DHT11 = blue sensor, DHT22 = white).

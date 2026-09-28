// Weather Monitoring System - Arduino UNO R4 WiFi + Blynk + Auto Irrigation
//                              + Smart Agriculture Flask/AI server sync
//
// REQUIRED LIBRARIES (install via Arduino IDE Library Manager before compiling):
//   - Blynk                (BlynkSimpleWifi.h)
//   - DHT sensor library    (DHT.h)
//   - ArduinoHttpClient     (by Arduino) - for talking to the Flask server
//   - ArduinoJson           (by Benoit Blanchon) - for building/parsing that JSON
//
// SETUP: copy secrets.h.example to secrets.h in this same folder and fill in
// your real WiFi/Blynk/server values. secrets.h is gitignored - never commit it.
//
// Sensors: DHT11 (temp/humidity), LDR (light), rain sensor, soil moisture sensor,
//          HC-SR04 ultrasonic (water tank level)
// Actuators: 1-Channel Relay Module (Water Pump), Buzzer (low water alert),
//            Tank Full LED
// Status LEDs: Pump LED (Pin 5), Real-World Mode Green LED (Pin 6), Demo Mode Red LED (Pin 3),
//              Tank Full LED (Pin 12)
//
// Blynk datastreams:
//   V0 Temperature (Double, C)      V1 Humidity (Double, %)
//   V2 Light (Integer, 0-100)       V3 Rain (Integer, 0-100)
//   V4 Soil moisture (Integer, 0-100)
//   V5 Pump Status (Integer, 0 or 1)
//   V6 Manual Pump Switch (Integer, 0 or 1)
//   V7 Demo Mode Switch (Integer, 0 or 1)
//   V8 Mode Indicator (String) - shows "REAL-WORLD MODE" or "DEMO MODE" on the dashboard
//   V9 Water Tank Level (Integer, 0-100) - from HC-SR04 ultrasonic sensor
//   V10 Plant Health Advisory (String) - from the Flask/AI server, ADVISORY ONLY.
//       This never controls the pump - all irrigation safety logic below is
//       fully local and keeps working even if the server is unreachable,
//       asleep (free-tier hosts sleep when idle), or returns garbage.
//
// View your dashboard at: https://blynk.cloud/dashboard/758759/global/devices/288049
// (that link is just for you to open in a browser - it isn't used by the device)
//
// Water level (HC-SR04) wiring: TRIG -> D9, ECHO -> D10, VCC -> 5V, GND -> GND.
// No voltage divider needed - Uno R4 WiFi is 5V logic, unlike an ESP32.
// Water level is wired into pump safety: below LOW_WATER_THRESHOLD_PCT, automatic
// watering cannot start (and is stopped immediately if running); manual watering
// via V6 is never blocked by water level - the person is responsible for judging it.
//
// Buzzer (low water alert): signal pin -> D11, VCC -> 5V, GND -> GND.
// Sounds continuously while water level is below LOW_WATER_THRESHOLD_PCT or the
// water sensor is faulty; silent otherwise. Written for an active buzzer module
// (just needs HIGH/LOW). If you're using a bare passive piezo instead, replace
// the digitalWrite(BUZZER_PIN, ...) calls with tone(BUZZER_PIN, 2000) / noTone(BUZZER_PIN).
//
// Tank Full LED: anode -> resistor (~470 ohm) -> D12, cathode -> GND.
// Lights up while water level is at or above TANK_FULL_THRESHOLD_PCT.

#include "secrets.h"

#define BLYNK_TEMPLATE_ID    "TMPL3f85tQguP"
#define BLYNK_TEMPLATE_NAME  "uno r4"
#define BLYNK_AUTH_TOKEN     BLYNK_AUTH_TOKEN_VALUE

// Optional: uncomment for detailed Blynk logs in Serial Monitor
// #define BLYNK_PRINT Serial

#include <WiFiS3.h>
#include <BlynkSimpleWifi.h>
#include <DHT.h>
#include <ArduinoHttpClient.h>
#include <ArduinoJson.h>

// ---------------- WiFi Credentials (from secrets.h) ----------------
char ssid[] = WIFI_SSID;
char pass[] = WIFI_PASS;

// ---------------- Flask/AI Server ----------------
WiFiSSLClient sslClient;
HttpClient httpClient(sslClient, SERVER_HOST, 443);
const unsigned long SERVER_HTTP_TIMEOUT_MS = 5000UL;  // bounded on purpose - see syncWithServer()

// ---------------- Hardware Pins ----------------
#define DHTPIN         2
#define DHTTYPE        DHT11
#define RELAY_PIN      4
#define PUMP_LED_PIN   5   // Breadboard LED for Pump Status
#define GREEN_LED_PIN  6   // Real-World Mode Indicator LED
#define RED_LED_PIN    3   // Demo Mode Indicator LED

#define LDR_PIN        A0
#define RAIN_PIN       A1
#define SOIL_PIN       A2

// Optional power pins for rain/soil sensors
const bool USE_POWER_PINS = false;
#define RAIN_PWR_PIN   7
#define SOIL_PWR_PIN   8

// Water tank level - HC-SR04 ultrasonic sensor, mounted at the top of the
// tank pointing down at the water surface
#define WATER_TRIG_PIN 9
#define WATER_ECHO_PIN 10

// Water alerts
#define BUZZER_PIN     11  // Low water level alert
#define FULL_LED_PIN   12  // Lights up when the tank is full

// ---------------- Relay Logic ----------------
// true  = relay turns ON when pin is LOW  (most 5V relay modules)
// false = relay turns ON when pin is HIGH
const bool RELAY_ACTIVE_LOW = false;  // your relay is active-HIGH

// ---------------- Calibration Settings ----------------
// Set to true, upload, read raw values in Serial Monitor (115200 baud),
// fill in the values below, then set back to false and upload again.
const bool CALIBRATION_MODE = false;

// Light: raw value in darkness (0%) and in bright light (100%).
// This LDR module reads HIGH in the dark and LOW in bright light,
// so the values are "inverted". For best accuracy replace 1023 / 0 with
// your own readings (cover the LDR = dark value, torch on it = bright value).
const int LIGHT_DARK_VAL   = 1023;
const int LIGHT_BRIGHT_VAL = 0;

// Rain: raw value when dry (0%) and when fully wet (100%)
const int RAIN_DRY_VAL     = 1023;
const int RAIN_WET_VAL     = 0;

// Soil: raw value in dry air (0%) and submerged in water (100%).
// *** Replace with YOUR sensor's readings from CALIBRATION_MODE. ***
const int SOIL_DRY_VAL     = 550;
const int SOIL_WET_VAL     = 250;

// If the raw soil reading is further than this outside the calibrated range,
// the sensor is treated as faulty and automatic watering is disabled.
const int SOIL_FAULT_MARGIN = 100;

// Cheap capacitive soil sensors drift for the first 20-30s after being
// inserted/powered (the onboard oscillator warms up). This smooths that out
// with an exponential moving average instead of using the raw reading directly.
// Lower = smoother/slower to react to real changes, higher = faster/noisier.
const float SOIL_SMOOTHING_ALPHA_REAL = 0.15;  // ~20s to settle - normal operation
const float SOIL_SMOOTHING_ALPHA_DEMO = 0.10;  // steadier than real-world, but still catches up in time for the 3s demo burst

// ---------------- Water Tank Level Settings (HC-SR04) ----------------
// Distance from the sensor to the water surface when the tank is EMPTY
// (long distance = bottom of tank) and FULL (short distance = near the
// sensor). Measure these for your actual tank and replace the placeholders.
const int TANK_EMPTY_DISTANCE_CM = 30;  // *** placeholder - measure & replace ***
const int TANK_FULL_DISTANCE_CM  = 3;   // *** placeholder - measure & replace ***

// Below this water level %, the pump will not start (manual or automatic),
// and any pump already running is stopped immediately. This protects the
// pump from running dry when the reservoir empties.
const int LOW_WATER_THRESHOLD_PCT = 15;

// At or above this water level %, the tank is considered full and the
// Tank Full LED lights up.
const int TANK_FULL_THRESHOLD_PCT = 90;

const unsigned long WATER_CHECK_MS = 2000UL;  // check water level every 2s

// ---------------- Irrigation Parameters ----------------
const int SOIL_DRY_THRESHOLD_PCT = 30;  // Start pump below this
const int SOIL_WET_TARGET_PCT    = 60;  // Stop pump at/above this (normal burst mode)
const int MAX_FAILED_BURSTS      = 3;   // Bursts before switching to continuous run

// After MAX_FAILED_BURSTS, the pump runs continuously (no burst limit, no cooldown)
// until soil moisture reaches this higher target.
const int CONTINUOUS_TARGET_PCT       = 80;
// Safety cap: if continuous mode runs this long without reaching the target,
// something is likely wrong (blocked pipe, dry reservoir, stuck sensor) - stop
// the pump and lock out auto irrigation rather than run indefinitely.
//
// *** If your pump is a submersible with no float switch / low-water cutoff  ***
// *** in the reservoir, consider shortening this to 30000-60000 (30-60 s).   ***
// *** 5 minutes of uninterrupted running with an empty reservoir can burn    ***
// *** out a small pump - the soil sensor cannot detect that condition.       ***
const unsigned long CONTINUOUS_MAX_RUNTIME_MS = 300000UL; // 5 minutes

// Default Timings (Real-World Mode)
bool demoMode = false;
unsigned long pumpMaxOnTime    = 10000UL;   // 10s auto burst
unsigned long pumpCooldownTime = 600000UL;  // 10 minutes cooldown
unsigned long manualMaxOnTime  = 60000UL;   // 1 minute manual limit
float soilSmoothingAlpha       = SOIL_SMOOTHING_ALPHA_REAL;

const unsigned long IRRIGATION_CHECK_MS = 1000UL;   // Check soil every 1s

// ---------------- General Settings ----------------
const uint8_t       NUM_SAMPLES       = 10;

// Blynk free-tier plans cap you at ~100,000 device messages/month, and every
// single Blynk.virtualWrite() call burns one message. sendSensorData() writes
// up to 7 datastreams per call (V0,V1,V2,V3,V4,V5,V9). At a 5-second interval
// that's roughly 7 * (30*24*3600/5) =~ 3.6 MILLION messages/month - about 36x
// over the free quota. None of these readings change fast enough to need
// sub-minute dashboard updates during normal, day-to-day operation, so
// REAL-WORLD mode stays slow enough to keep monthly usage safely under quota
// (~60,000 msgs/month at 7 writes/call). But a slow dashboard makes for a
// bad live demo - flipping the existing Demo Mode switch (V7) now also
// speeds this up to every 5 seconds, same as it already does for the pump
// burst/cooldown timings below. A demo only runs for minutes, not a full
// month, so the quota risk from the fast interval is negligible as long as
// you flip back to Real-World mode afterward. Local pump/safety control
// (irrigationTask, waterLevelTask) is unaffected either way - those run
// every 1-2s regardless and never touch Blynk directly.
const unsigned long SEND_INTERVAL_REAL_MS = 300000UL;  // 5 min - safe for everyday/long-term operation
const unsigned long SEND_INTERVAL_DEMO_MS = 5000UL;    // 5 sec - live demo, flip back afterward
unsigned long sendInterval = SEND_INTERVAL_REAL_MS;
int sendDataTimerId = -1;   // set in setup(), used by BLYNK_WRITE(V7) to retime this timer

const unsigned long CHECK_INTERVAL_MS = 10000UL;

DHT dht(DHTPIN, DHTTYPE);
BlynkTimer timer;

// System States
bool isPumpOn           = false;
bool manualPumpOverride = false;
unsigned long pumpStartTime   = 0;
unsigned long lastPumpOffTime = 0;
int  lastSoilPct        = 0;
bool soilSensorOk       = true;
float smoothedSoilRaw   = -1;  // -1 = "not yet initialized"
int  failedBursts       = 0;
bool irrigationLockout  = false;
bool continuousMode     = false;  // true = running straight through to CONTINUOUS_TARGET_PCT
int  lastWaterPct       = 0;      // starts at 0 = fail-safe (treated as empty until first reading)
bool waterSensorOk      = true;
bool lowWaterAlertOn    = false;  // tracks buzzer state, for edge-triggered logging
bool tankFullOn         = false;  // tracks full-LED state, for edge-triggered logging
unsigned long ignoreWritesUntil = 0;  // BLYNK_WRITE calls before this time are ignored
                                      // (Blynk can auto-resend a widget's last stored
                                      // value right after connecting; this filters that out)

// AI-triggered run (from Flask's /get-recommendation): deliberately its own
// state, separate from manualPumpOverride and the local burst/continuous
// logic. Unlike a manual run, an AI-triggered run still respects the water
// level safety cutoff (see the universal safety check in processIrrigation) -
// no human is watching it, so the safety net has to stay in place. Unlike a
// normal automatic burst, it doesn't participate in failedBursts/continuous-
// mode escalation - the server already decided a specific duration.
bool aiTriggeredActive   = false;
unsigned long aiMaxOnTime = 0;

// Cached from the last sendSensorData() call so syncWithServer() (which runs
// on its own separate timer) can POST them without re-reading every sensor.
float lastTempC      = NAN;
float lastHumidityPct = NAN;
int   lastLightPct    = 0;
int   lastRainPct     = 0;

// ---------------- Helper Functions ----------------

int readAveraged(uint8_t pin) {
  long sum = 0;
  for (uint8_t i = 0; i < NUM_SAMPLES; i++) {
    sum += analogRead(pin);
    delay(2);
  }
  return (int)(sum / NUM_SAMPLES);
}

int readSensor(uint8_t pin, uint8_t powerPin) {
  if (USE_POWER_PINS) {
    digitalWrite(powerPin, HIGH);
    delay(50);
  }
  int value = readAveraged(pin);
  if (USE_POWER_PINS) {
    digitalWrite(powerPin, LOW);
  }
  return value;
}

// Reads the soil sensor and folds it into a running exponential average,
// so the multi-second thermal-drift warm-up settles out on its own instead
// of jumping straight to a still-drifting instant value. Used both for
// irrigation decisions and for calibration-mode output.
int readSmoothedSoilRaw() {
  int instant = readSensor(SOIL_PIN, SOIL_PWR_PIN);
  if (smoothedSoilRaw < 0) {
    smoothedSoilRaw = instant;  // first reading: start from here, don't ramp up from 0
  } else {
    smoothedSoilRaw += soilSmoothingAlpha * (instant - smoothedSoilRaw);
  }
  return (int)(smoothedSoilRaw + 0.5f);
}

// Works in both directions (rawAt0 may be larger or smaller than rawAt100)
int toPercent(int raw, int rawAt0, int rawAt100) {
  if (rawAt0 == rawAt100) return 0;
  long pct = map(raw, rawAt0, rawAt100, 0, 100);
  return (int)constrain(pct, 0, 100);
}

bool soilReadingValid(int raw) {
  int lo = min(SOIL_DRY_VAL, SOIL_WET_VAL) - SOIL_FAULT_MARGIN;
  int hi = max(SOIL_DRY_VAL, SOIL_WET_VAL) + SOIL_FAULT_MARGIN;
  return (raw >= lo && raw <= hi);
}

// Takes up to 3 ultrasonic readings (HC-SR04 needs ~60ms between pulses) and
// averages the valid ones. Returns -1 if no valid echo was received at all.
int readDistanceCM() {
  long sum = 0;
  int validCount = 0;

  for (uint8_t i = 0; i < 3; i++) {
    digitalWrite(WATER_TRIG_PIN, LOW);
    delayMicroseconds(2);
    digitalWrite(WATER_TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(WATER_TRIG_PIN, LOW);

    unsigned long duration = pulseIn(WATER_ECHO_PIN, HIGH, 30000UL);  // 30ms timeout (~5m range)
    if (duration > 0) {
      sum += (long)(duration * 0.01715);  // speed of sound: 0.0343 cm/us, round trip /2
      validCount++;
    }
    delay(60);  // HC-SR04 needs a gap between pulses
  }

  if (validCount == 0) return -1;
  return (int)(sum / validCount);
}

// Runs every WATER_CHECK_MS, independent of WiFi/Blynk
void waterLevelTask() {
  int distanceCM = readDistanceCM();

  if (distanceCM < 0) {
    if (waterSensorOk) Serial.println(">> Water level sensor reading failed (no echo)! <<");
    waterSensorOk = false;
  } else {
    waterSensorOk = true;
    lastWaterPct = toPercent(distanceCM, TANK_EMPTY_DISTANCE_CM, TANK_FULL_DISTANCE_CM);
  }

  Serial.print("[water] distance=");
  Serial.print(distanceCM);
  Serial.print("cm pct=");
  Serial.print(lastWaterPct);
  Serial.print(" sensorOk=");
  Serial.println(waterSensorOk ? "yes" : "NO");

  // ----- Low water buzzer -----
  bool lowWater = !waterSensorOk || lastWaterPct < LOW_WATER_THRESHOLD_PCT;
  digitalWrite(BUZZER_PIN, lowWater ? HIGH : LOW);
  if (lowWater != lowWaterAlertOn) {
    lowWaterAlertOn = lowWater;
    Serial.println(lowWater ? ">> Buzzer ON - water level low (or sensor fault) <<"
                            : ">> Buzzer OFF - water level OK <<");
  }

  // ----- Tank full LED -----
  bool tankFull = waterSensorOk && lastWaterPct >= TANK_FULL_THRESHOLD_PCT;
  digitalWrite(FULL_LED_PIN, tankFull ? HIGH : LOW);
  if (tankFull != tankFullOn) {
    tankFullOn = tankFull;
    Serial.println(tankFull ? ">> Tank Full LED ON - tank is full <<"
                            : ">> Tank Full LED OFF <<");
  }
}

// Update Green & Red LEDs according to current operating mode
void updateModeLEDs() {
  if (demoMode) {
    digitalWrite(RED_LED_PIN, HIGH);   // Demo Mode ON -> Red LED ON
    digitalWrite(GREEN_LED_PIN, LOW);  // Real-World OFF
  } else {
    digitalWrite(GREEN_LED_PIN, HIGH); // Real-World ON -> Green LED ON
    digitalWrite(RED_LED_PIN, LOW);    // Demo Mode OFF
  }
}

// Push the current Real-World/Demo status to the Blynk dashboard (V8)
void pushModeStatus() {
  if (Blynk.connected()) {
    Blynk.virtualWrite(V8, demoMode ? "DEMO MODE" : "REAL-WORLD MODE");
  }
}

// ---------------- Pump Control ----------------

void setRelayState(bool turnOn) {
  digitalWrite(RELAY_PIN, (turnOn == RELAY_ACTIVE_LOW) ? LOW : HIGH);
}

void setPump(bool state, bool isManual) {
  if (isPumpOn == state) return;

  isPumpOn = state;
  setRelayState(isPumpOn);

  // Toggle pump status LED on breadboard
  digitalWrite(PUMP_LED_PIN, isPumpOn ? HIGH : LOW);

  if (isPumpOn) {
    pumpStartTime = millis();
    Serial.println(isManual ? ">> Pump & LED turned ON manually via Blynk <<"
                            : ">> Pump & LED turned ON automatically (soil dry) <<");
  } else {
    lastPumpOffTime = millis();
    Serial.println(">> Pump & LED turned OFF <<");
  }

  if (Blynk.connected()) {
    Blynk.virtualWrite(V5, isPumpOn ? 1 : 0);
  }
}

void processIrrigation() {
  unsigned long now = millis();

  if (isPumpOn) {
    // ----- Universal safety: low water level or sensor fault stops an
    //       AUTOMATIC (burst or continuous) run immediately. Manual runs
    //       are left alone - the person watching the switch is responsible
    //       for judging the water level themselves. -----
    if (!manualPumpOverride && (!waterSensorOk || lastWaterPct < LOW_WATER_THRESHOLD_PCT)) {
      Serial.println(">> LOW WATER LEVEL (or sensor fault). Emergency pump stop! <<");
      continuousMode = false;
      irrigationLockout = true;
      setPump(false, false);
      return;
    }

    // ----- Manual Run -----
    if (manualPumpOverride) {
      if (now - pumpStartTime >= manualMaxOnTime) {
        Serial.println(">> Manual run limit reached. Shutting off pump. <<");
        manualPumpOverride = false;
        setPump(false, false);
        if (Blynk.connected()) Blynk.virtualWrite(V6, 0);
      }
      return;
    }

    // ----- AI-Triggered Run (from Flask's /get-recommendation) -----
    // Note: this branch is only reached at all if the universal safety
    // check above (water level / sensor fault) already passed, since this
    // isn't manualPumpOverride - an AI-triggered run gets the same water
    // safety protection as any other automatic run.
    if (aiTriggeredActive) {
      if (now - pumpStartTime >= aiMaxOnTime) {
        Serial.println(">> AI-triggered run duration complete. Shutting off pump. <<");
        aiTriggeredActive = false;
        setPump(false, false);
      } else if (!soilSensorOk) {
        Serial.println(">> Soil sensor fault during AI-triggered run. Shutting off pump. <<");
        aiTriggeredActive = false;
        setPump(false, false);
      }
      return;
    }

    // ----- Continuous "final push" run (after MAX_FAILED_BURSTS) -----
    if (continuousMode) {
      if (now - pumpStartTime >= CONTINUOUS_MAX_RUNTIME_MS) {
        Serial.println(">> Continuous run safety timeout reached without hitting target. Auto irrigation LOCKED. <<");
        continuousMode = false;
        irrigationLockout = true;
        setPump(false, false);
      } else if (!soilSensorOk) {
        Serial.println(">> Soil sensor fault during continuous run. Shutting off pump. <<");
        continuousMode = false;
        setPump(false, false);
      } else if (lastSoilPct >= CONTINUOUS_TARGET_PCT) {
        Serial.println(">> Continuous run reached target moisture. Shutting off pump. <<");
        continuousMode = false;
        failedBursts = 0;
        setPump(false, false);
      }
      return;
    }

    // ----- Normal burst-based automatic run -----
    if (now - pumpStartTime >= pumpMaxOnTime) {
      failedBursts++;
      Serial.print(">> Burst ended by timer, soil not at target. Failed bursts: ");
      Serial.println(failedBursts);

      if (failedBursts >= MAX_FAILED_BURSTS) {
        // Don't shut the pump off - keep it running straight into continuous mode
        // so we don't lose the momentum we already have.
        continuousMode = true;
        Serial.println(">> 3 failed bursts. Switching to CONTINUOUS run until soil reaches target (pump stays ON). <<");
      } else {
        setPump(false, false);
      }
    } else if (!soilSensorOk) {
      Serial.println(">> Soil sensor fault. Shutting off pump. <<");
      setPump(false, false);
    } else if (lastSoilPct >= SOIL_WET_TARGET_PCT) {
      Serial.println(">> Soil moisture reached target. Shutting off pump. <<");
      failedBursts = 0;
      setPump(false, false);
    }
    return;
  }

  // ----- Pump is OFF -----
  if (lastSoilPct >= SOIL_DRY_THRESHOLD_PCT) failedBursts = 0;

  if (manualPumpOverride || !soilSensorOk || irrigationLockout ||
      !waterSensorOk || lastWaterPct < LOW_WATER_THRESHOLD_PCT) return;

  bool cooldownFinished = (lastPumpOffTime == 0) ||
                          (now - lastPumpOffTime >= pumpCooldownTime);

  if (lastSoilPct < SOIL_DRY_THRESHOLD_PCT && cooldownFinished) {
    setPump(true, false);
  }
}

// Runs every second, independent of WiFi/Blynk and of the data-sending timer
void irrigationTask() {
  int soilRaw = readSmoothedSoilRaw();
  lastSoilPct = toPercent(soilRaw, SOIL_DRY_VAL, SOIL_WET_VAL);

  bool ok = soilReadingValid(soilRaw);
  if (ok != soilSensorOk) {
    soilSensorOk = ok;
    Serial.println(ok ? ">> Soil sensor reading OK <<"
                      : ">> Soil reading out of range! Auto irrigation disabled. <<");
  }

  processIrrigation();
}

// ---------------- Blynk Handlers ----------------

// Manual Switch (V6)
BLYNK_WRITE(V6) {
  bool wantOn = (param.asInt() == 1);

  if (millis() < ignoreWritesUntil) {
    Serial.println(">> Ignoring V6 write during connect grace period (likely a stale synced value, not a real tap) <<");
    return;
  }

  if (wantOn) {
    // Turning the switch on means "I checked the system, try again"
    irrigationLockout = false;
    failedBursts = 0;
    continuousMode = false;

    manualPumpOverride = true;
    if (isPumpOn) {
      pumpStartTime = millis();   // take over from auto mode, restart timer
    } else {
      setPump(true, true);
    }
  } else if (manualPumpOverride) {
    manualPumpOverride = false;
    setPump(false, true);
  }
}

// Demo Mode Switch (V7)
BLYNK_WRITE(V7) {
  if (millis() < ignoreWritesUntil) {
    Serial.println(">> Ignoring V7 write during connect grace period (likely a stale synced value, not a real tap) <<");
    return;
  }

  demoMode = (param.asInt() == 1);

  if (demoMode) {
    pumpMaxOnTime    = 3000UL;    // 3-second burst
    pumpCooldownTime = 5000UL;    // 5-second cooldown
    manualMaxOnTime  = 10000UL;   // 10-second manual run
    soilSmoothingAlpha = SOIL_SMOOTHING_ALPHA_DEMO;
    sendInterval = SEND_INTERVAL_DEMO_MS;
    Serial.println(">> Switched to DEMO MODE (Fast timings - RED LED ON) <<");
  } else {
    pumpMaxOnTime    = 10000UL;   // 10-second burst
    pumpCooldownTime = 600000UL;  // 10-minute cooldown
    manualMaxOnTime  = 60000UL;   // 1-minute manual run
    soilSmoothingAlpha = SOIL_SMOOTHING_ALPHA_REAL;
    sendInterval = SEND_INTERVAL_REAL_MS;
    Serial.println(">> Switched to REAL-WORLD MODE (GREEN LED ON) <<");
  }

  if (sendDataTimerId != -1) {
    timer.changeInterval(sendDataTimerId, sendInterval);
  }

  updateModeLEDs();
  pushModeStatus();
}

// ---------------- Connection Management ----------------

void maintainConnection() {
  // Never block (WiFi/Blynk retries can take ~15 s) while the pump is running
  if (isPumpOn) return;

  if (WiFi.status() != WL_CONNECTED) {
    Serial.print("Connecting to WiFi... ");
    WiFi.begin(ssid, pass);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 10000UL) {
      delay(250);
    }

    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("failed, will retry.");
      return;
    }
    Serial.println("connected.");
  }

  if (!Blynk.connected()) {
    Serial.print("Connecting to Blynk... ");
    if (Blynk.connect(5000)) {
      Serial.println("connected.");
      ignoreWritesUntil = millis() + 3000UL;  // 3s grace period against stale synced writes
      // Push the device state to the app (do NOT sync from the app)
      Blynk.virtualWrite(V6, manualPumpOverride ? 1 : 0);
      Blynk.virtualWrite(V5, isPumpOn ? 1 : 0);
      Blynk.virtualWrite(V7, demoMode ? 1 : 0);
      Blynk.virtualWrite(V9, lastWaterPct);
      pushModeStatus();
    } else {
      Serial.println("failed, will retry.");
    }
  }
}

// ---------------- Calibration Output ----------------

void printRawValues() {
  int ldr  = readAveraged(LDR_PIN);
  int rain = readSensor(RAIN_PIN, RAIN_PWR_PIN);
  int soil = readSmoothedSoilRaw();

  int waterDist = readDistanceCM();

  Serial.print("RAW  LDR: ");   Serial.print(ldr);
  Serial.print(" | Rain: ");    Serial.print(rain);
  Serial.print(" | Soil: ");    Serial.print(soil);
  Serial.print(" | Water distance: "); Serial.print(waterDist); Serial.println(" cm");
}

// ---------------- Flask/AI Server Sync ----------------
//
// Two things happen here: (1) sensor readings get POSTed to Flask so it has
// something to base a recommendation on, and (2) the resulting recommendation
// is read back - the plant-health label goes straight to the Blynk dashboard
// (V10, purely informational), but the irrigation.water/duration_sec fields
// can actually START a watering run (see AI-Triggered Run in
// processIrrigation()). That run still goes through all the same local
// safety checks as any other automatic run - the water-level cutoff in
// particular is NEVER bypassed here, unlike a manual run.
//
// Guarded the same way maintainConnection() already guards Blynk/WiFi
// reconnects: never run while the pump is on, and never let the network
// call block for long. Free-tier hosts (Render, etc.) can take 20-50s to
// wake from sleep - without a short timeout, that would freeze this
// single-threaded loop (and with it, water-level safety checks) for the
// same duration. A bounded ~5s timeout per call means worst case we just
// miss this sync cycle and fall back to purely local soil-threshold
// irrigation until the next one, instead of risking a stuck safety loop.

void postSensorDataToServer() {
  StaticJsonDocument<256> doc;
  if (!isnan(lastTempC))      doc["temperature_c"] = lastTempC;
  if (!isnan(lastHumidityPct)) doc["humidity_pct"] = lastHumidityPct;
  doc["soil_pct"]    = lastSoilPct;
  doc["light_pct"]   = lastLightPct;
  doc["rain_pct"]    = lastRainPct;
  doc["water_pct"]   = lastWaterPct;
  doc["pump_status"] = isPumpOn ? 1 : 0;

  String body;
  serializeJson(doc, body);

  httpClient.setTimeout(SERVER_HTTP_TIMEOUT_MS);
  httpClient.beginRequest();
  httpClient.post("/sensor-data");
  httpClient.sendHeader("Content-Type", "application/json");
  httpClient.sendHeader("X-API-Key", SERVER_API_KEY);
  httpClient.sendHeader("Content-Length", body.length());
  httpClient.beginBody();
  httpClient.print(body);
  httpClient.endRequest();

  int statusCode = httpClient.responseStatusCode();
  httpClient.responseBody();  // drain so the connection is clean for the next request
  Serial.print("[server] POST /sensor-data -> ");
  Serial.println(statusCode);
}

// Starts a local, safety-respecting watering run based on the server's
// recommendation. Deliberately mirrors the guard conditions already used
// for local auto-start (see the "Pump is OFF" section of processIrrigation)
// rather than trusting the server's judgement alone - the server can say
// "water", but the device still has the final say on whether it's safe to.
void actOnRecommendation(bool aiWater, int aiDurationSec) {
  if (!aiWater) return;
  if (isPumpOn || manualPumpOverride || irrigationLockout) return;
  if (!soilSensorOk || !waterSensorOk || lastWaterPct < LOW_WATER_THRESHOLD_PCT) return;

  aiMaxOnTime = (unsigned long)constrain(aiDurationSec, 5, 120) * 1000UL;
  aiTriggeredActive = true;
  setPump(true, false);
  Serial.print(">> AI-triggered watering started, duration: ");
  Serial.print(aiMaxOnTime / 1000);
  Serial.println("s <<");
}

void fetchRecommendationFromServer() {
  httpClient.setTimeout(SERVER_HTTP_TIMEOUT_MS);
  httpClient.beginRequest();
  httpClient.get("/get-recommendation");
  httpClient.sendHeader("X-API-Key", SERVER_API_KEY);
  httpClient.endRequest();

  int statusCode = httpClient.responseStatusCode();
  String response = httpClient.responseBody();

  if (statusCode != 200) {
    Serial.print("[server] GET /get-recommendation -> ");
    Serial.println(statusCode);
    return;
  }

  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, response);
  if (err) {
    Serial.print("[server] failed to parse recommendation JSON: ");
    Serial.println(err.c_str());
    return;
  }

  const char* label = "unknown";
  if (!doc["plant_health"].isNull()) {
    label = doc["plant_health"]["label"] | "unknown";
  }
  if (Blynk.connected()) {
    Blynk.virtualWrite(V10, label);
  }

  bool aiWater = doc["irrigation"]["water"] | false;
  int aiDurationSec = doc["irrigation"]["duration_sec"] | 0;
  const char* reason = doc["irrigation"]["reason"] | "";

  Serial.print("[server] Plant health: "); Serial.print(label);
  Serial.print(" | AI irrigation: "); Serial.print(aiWater ? "WATER" : "skip");
  Serial.print(" (" ); Serial.print(reason); Serial.println(")");

  actOnRecommendation(aiWater, aiDurationSec);
}

void syncWithServer() {
  if (isPumpOn) return;                      // never contend with pump safety loop
  if (WiFi.status() != WL_CONNECTED) return;
  if (String(SERVER_HOST).startsWith("YOUR-SERVER-HOST")) return;  // not deployed/configured yet

  postSensorDataToServer();
  fetchRecommendationFromServer();
}

// ---------------- Telemetry Output ----------------

void sendSensorData() {
  float h = dht.readHumidity();
  float t = dht.readTemperature();

  int ldrRaw  = readAveraged(LDR_PIN);
  int rainRaw = readSensor(RAIN_PIN, RAIN_PWR_PIN);

  int lightPct = toPercent(ldrRaw,  LIGHT_DARK_VAL, LIGHT_BRIGHT_VAL);
  int rainPct  = toPercent(rainRaw, RAIN_DRY_VAL,   RAIN_WET_VAL);
  int soilPct  = lastSoilPct;  // updated every second by irrigationTask()

  bool dhtOk  = !(isnan(h) || isnan(t));
  bool online = Blynk.connected();

  // Cache for syncWithServer(), which runs on its own separate timer and
  // needs these same values without re-reading every sensor itself.
  if (dhtOk) {
    lastTempC = t;
    lastHumidityPct = h;
  }
  lastLightPct = lightPct;
  lastRainPct = rainPct;

  if (online) {
    Blynk.virtualWrite(V2, lightPct);
    Blynk.virtualWrite(V3, rainPct);
    Blynk.virtualWrite(V4, soilPct);
    Blynk.virtualWrite(V5, isPumpOn ? 1 : 0);
    Blynk.virtualWrite(V9, lastWaterPct);

    if (dhtOk) {
      Blynk.virtualWrite(V0, t);
      Blynk.virtualWrite(V1, h);
    }
  }

  if (dhtOk) {
    Serial.print("Temp: ");     Serial.print(t, 1); Serial.print(" C | ");
    Serial.print("Humidity: "); Serial.print(h, 1); Serial.print(" % | ");
  } else {
    Serial.print("DHT read failed | ");
  }
  Serial.print("Light: "); Serial.print(lightPct); Serial.print("% | ");
  Serial.print("Rain: ");  Serial.print(rainPct);  Serial.print("% | ");
  Serial.print("Soil: ");  Serial.print(soilPct);  Serial.print("% | ");
  Serial.print("Water: "); Serial.print(lastWaterPct); Serial.print("% | ");
  Serial.print("Pump: ");  Serial.print(isPumpOn ? "ON" : "OFF");
  if (demoMode) Serial.print(" [DEMO]");
  if (continuousMode) Serial.print(" [CONTINUOUS]");
  if (irrigationLockout) Serial.print(" [AUTO LOCKED]");
  Serial.println(online ? "" : "  [offline]");
}

// ---------------- Setup & Loop ----------------

void setup() {
  Serial.begin(115200);
  delay(1500);

  // Configure Pins
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(PUMP_LED_PIN, OUTPUT);
  pinMode(GREEN_LED_PIN, OUTPUT);
  pinMode(RED_LED_PIN, OUTPUT);

  setRelayState(false);               // pump OFF at boot
  digitalWrite(PUMP_LED_PIN, LOW);

  pinMode(WATER_TRIG_PIN, OUTPUT);
  pinMode(WATER_ECHO_PIN, INPUT);
  digitalWrite(WATER_TRIG_PIN, LOW);

  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(FULL_LED_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  digitalWrite(FULL_LED_PIN, LOW);

  // Default mode indicator (Real-World Mode = Green ON)
  updateModeLEDs();

  if (USE_POWER_PINS) {
    pinMode(RAIN_PWR_PIN, OUTPUT);
    pinMode(SOIL_PWR_PIN, OUTPUT);
    digitalWrite(RAIN_PWR_PIN, LOW);
    digitalWrite(SOIL_PWR_PIN, LOW);
  }

  analogReadResolution(10);
  dht.begin();

  if (CALIBRATION_MODE) {
    Serial.println("=== CALIBRATION MODE: raw values only ===");
    timer.setInterval(1000L, printRawValues);
    return;
  }

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("WiFi module not found!");
    while (true) { delay(1000); }
  }

  Blynk.config(BLYNK_AUTH_TOKEN, "blynk.cloud", 80);
  maintainConnection();

  timer.setInterval(IRRIGATION_CHECK_MS, irrigationTask);
  timer.setInterval(WATER_CHECK_MS, waterLevelTask);
  timer.setInterval(CHECK_INTERVAL_MS, maintainConnection);
  sendDataTimerId = timer.setInterval(sendInterval, sendSensorData);
  // Server sync stays on its own fixed interval, independent of demo mode -
  // no benefit to polling it faster during a demo (the plant-health label
  // only changes when a new photo is analyzed), and it'd otherwise hammer
  // the Flask server every 5s for no reason while demoing.
  timer.setInterval(SEND_INTERVAL_REAL_MS, syncWithServer);
}

void loop() {
  if (!CALIBRATION_MODE && Blynk.connected()) {
    Blynk.run();
  }
  timer.run();
}

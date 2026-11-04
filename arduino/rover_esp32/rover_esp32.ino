// ============================================================
//  AI Smart Agriculture - ROVER firmware (ESP32)  r15
//  ROUTE (line mode), r16:  centre (plain tape, 010) -> forward -> ZONE 1 (tape ends) -> stay 1 min
//        -> reverse to the centre (distance estimate), wait 2 s -> reverse on to ZONE 2 (cross-tape 111) -> stay 1 min
//        -> forward to the centre -> stop and switch LINE mode OFF.
//        Rain above 15 %: stop, go back to the centre, send an alarm, LINE mode OFF.  See the ROUTE settings below.
//  Sensors : DHT11/DHT22 (1 wire, GPIO21), LDR, rain module, capacitive soil sensor
//  Probe   : soil sensor on a servo arm (retracted while driving)
//  Drive   : 2 DC motors via L298N, 3 IR line sensors
//  Control : local web page (same Wi-Fi) + ThingsBoard RPC (MQTT)
//  Cloud   : ThingsBoard telemetry over MQTT
//
//  Libraries (Library Manager):
//    - PubSubClient   (Nick O'Leary)
//    - ArduinoJson    (Benoit Blanchon, version 7.x)
//  (Servo is driven directly with the ESP32 PWM hardware - no servo library)
//  SINGLE FILE: save as rover_esp32.ino inside a folder named  rover_esp32
//  Edit Wi-Fi + ThingsBoard token in the SETTINGS section below.
// ============================================================
#include <WiFi.h>
#include <WebServer.h>
#include <DHT.h>          // Arduino library: "DHT sensor library" by Adafruit (+ "Adafruit Unified Sensor")
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <WiFiUdp.h>
// ======================= SETTINGS (edit this section only) =======================
// ---------- Wi-Fi ----------
#define WIFI_SSID        "YOUR_WIFI_NAME"
#define WIFI_PASSWORD    "YOUR_WIFI_PASSWORD"

// ---------- ThingsBoard (MQTT) ----------
// Same as your test:  mosquitto_pub -h mqtt.eu.thingsboard.cloud -p 1883 -t v1/devices/me/telemetry -u <TOKEN> -m "{temperature:25}"
// The rover uses MQTT (port 1883) instead of HTTP so it can also receive control commands.
#define TB_HOST          "mqtt.eu.thingsboard.cloud"   // MQTT host of your EU cloud server
#define TB_PORT          1883
// Device -> "Credentials" -> Access token. Do not share this publicly.
#define TB_ACCESS_TOKEN  "YOUR_ROVER_THINGSBOARD_TOKEN"

// ---------- Device info ----------
#define DEVICE_ID        "rover_01"

// ---------- Zone info to the irrigation station (UNO R4 WiFi) over the local Wi-Fi (UDP) ----------
// Sent whenever the rover reports (every 3 s + on events), even if ThingsBoard is down.
// STATION_IP: put the station's IP (shown in the station's Serial Monitor) or "" to broadcast on the local network.
// Use the SAME STATION_KEY in the station sketch. Change it to your own word.
#define STATION_IP        ""
#define STATION_UDP_PORT  4210
#define STATION_KEY       "change_this_shared_key"

// ---------- Temperature + humidity: ONE data wire (DHT11 or DHT22) ----------
#define PIN_DHT          21        // sensor DATA pin -> GPIO21 (VCC -> 3.3 V, GND -> GND)
#define DHT_TYPE         DHT11     // blue 3-pin sensor = DHT11. White DHT22 / AM2302 = change to DHT22

// ---------- Analog / digital sensors ----------
// Use ADC1 pins only (32-39). ADC2 pins stop working when Wi-Fi is on.
#define PIN_LDR          34     // LDR MODULE analog pin AO -> GPIO34 (module powered from 3.3 V)
// Light percent: 0 = dark, 100 = very bright. CALIBRATE with light_raw readings:
//  LIGHT_DARK_RAW   = raw value with the sensor covered / in a dark room
//  LIGHT_BRIGHT_RAW = raw value under the brightest light you expect (torch / direct sun)
// Works whichever way your module goes (some read LOWER in bright light, some HIGHER).
// Placeholders below are guesses - NOT real calibration. Light is relative, NOT lux.
#define LIGHT_CALIBRATED   false
#define LIGHT_DARK_RAW     3500
#define LIGHT_BRIGHT_RAW   500
#define PIN_RAIN_DO      27     // Rain module digital output
#define RAIN_ACTIVE_LOW  true   // most modules: LOW = water detected. Test yours!

// Rain INTENSITY percent from the module's analog pin AO (how wet the plate is, NOT mm of rain).
// Power the module from 3.3 V so AO never goes above what the ESP32 pin accepts.
#define PIN_RAIN_AO      39     // rain module AO -> VN pin = GPIO39 (ADC1, input-only)
// CALIBRATE: read RAW with the plate completely dry, then with it fully wet (water drops over the traces).
// Most modules read HIGH when dry and LOWER when wet. Placeholders below - replace after testing.
#define RAIN_CALIBRATED  false
#define RAIN_DRY_RAW     4000
#define RAIN_WET_RAW     1200

// Touch pad for "is something blocking the rain sensor?" (ESP32 touch pin T6 = GPIO14; GPIO13 is now the motor IN2 pin, do not use it)
// Connect a small separate foil/copper pad lying beside the rain plate (NOT the plate traces).
#define PIN_RAIN_TOUCH           14
#define TOUCH_BASELINE_SAMPLES   20     // averaged at boot (keep the sensor DRY and CLEAR at power-on)
#define TOUCH_DEVIATION_PERCENT  20     // change from baseline that counts as "something on the sensor"
#define BLOCK_CONFIRM_MS         5000   // must persist this long (ignores passing leaves/insects)
#define ENABLE_RAIN_BLOCK_CHECK  false  // false = OFF (no foil pad / GPIO13 wire needed). Set true later to use it.

// ---------- Soil moisture probe (capacitive sensor on a servo arm) ----------
#define PIN_SOIL         35     // sensor AOUT -> ADC1 input-only pin. Power sensor from 3.3 V!
#define PIN_SERVO        23     // servo signal wire (servo power from separate 5 V, common GND)

// CALIBRATE THESE (see notes): raw ADC value in dry air/dry soil and in water/wet soil.
// Capacitive sensors read HIGHER when dry. Placeholders only - NOT real calibration.
#define SOIL_CALIBRATED  false
#define SOIL_DRY_RAW     3000
#define SOIL_WET_RAW     1300

#define SOIL_SAMPLES     20     // readings averaged per measurement
#define SOIL_SETTLE_MS   2500   // wait after probe is in the soil before reading
#define SOIL_HOLD_MS     10000  // arm stays DOWN this long (from touching the soil) before it retracts

// Servo arm angles (degrees, 0-180). Adjust to your arm so UP is clear of ground.
#define SERVO_UP_ANGLE   20     // retracted / safe while driving (power-on position!)
#define SERVO_DOWN_ANGLE 110    // sensor pushed into soil
#define SERVO_MIN_US     500
#define SERVO_MAX_US     2400
#define SERVO_STEP_DEG   2      // smooth, slow movement (degrees per step)
#define SERVO_STEP_MS    20

// ---------- IR line sensors: THREE digital modules ----------
// Mount in a row, facing the floor. Black tape runs under the CENTRE sensor;
// LEFT and RIGHT sit just outside the tape and normally see white floor.
#define PIN_IR_LEFT      33
#define PIN_IR_CENTER    4
#define PIN_IR_RIGHT     32
// Most IR modules output LOW on white and HIGH on black (LED lights on white).
// Test with Serial Monitor and flip this if the rover turns the wrong way.
#define IR_BLACK_IS_HIGH true

// ---------- Motor driver (L298N / L293D style) ----------
// ENA / ENB: KEEP BOTH JUMPER CAPS ON the L298N and leave the ENA/ENB header pins UNCONNECTED.
// Speed is made by PWM on the four IN pins instead (so no enable wires are needed).
// IN1 is on GPIO 25 (not 14): GPIO 14 pulses at boot and made the motor twitch.
#define PIN_IN1          25     // left motor
#define PIN_IN2          13
#define PIN_IN3          18     // right motor
#define PIN_IN4          19
// If FORWARD makes the rover SPIN instead of going straight, one side is wired the opposite way.
// Instead of rewiring, set that side to true (left or right) and re-upload.
#define INVERT_LEFT      false
#define INVERT_RIGHT     false

#define PWM_FREQ         1000
#define PWM_BITS         8      // duty 0..255

// Manual driving speed is a PERCENT (1-100) set from the dashboard slider. It maps to PWM MIN_DRIVE_PWM..255.
#define MIN_DRIVE_PWM       110    // below this the motors only hum / stall. Raise it if 1% does not move the wheels
#define SAFE_SPEED_PERCENT  60     // speed while the SAFETY LOCK is ON, and at every power-on
// Turning uses a FIXED speed so the degrees stay calibrated (the speed slider does not change turns).
#define TURN_SPEED_PWM      200
#define TURN_MS_PER_DEG     7.0f   // CALIBRATE: milliseconds of turning per degree (see the guide), or use RPC setTurnCal
#define TURN_STEP_DEG       15     // one press of Left / Right = this many degrees
#define SPEED_LINE       120    // line following speed (slow, the rover carries a load). Below about 110 the motors may only hum
#define SPEED_LINE_TURN  120

// ---------- Safety / timing ----------
#define MANUAL_TIMEOUT_MS   600    // manual move stops if no new command in this time
#define MANUAL_MAX_RUN_MS   600000 // forward/backward from the dashboard keeps going until Stop, but never longer than this (0 = no limit)
#define LINK_LOST_STOP_MS   2000   // ...and it stops if Wi-Fi / ThingsBoard is lost. Wi-Fi loss: at once. Cloud-only loss: noticed in about 5-10 s (MQTT keep-alive)
#define TELEMETRY_MS        3000   // link to the irrigation station: every 3 s over your home Wi-Fi (free)
#define CLOUD_IDLE_MS     300000   // ThingsBoard, normal: ALL values every 5 minutes
#define CLOUD_LIVE_MS       5000   // ThingsBoard, LIVE (demo) mode: ALL values every 5 seconds
#define LIVE_MAX_MS      1800000UL // live mode switches itself OFF after 30 minutes (protects the free ThingsBoard limits)
#define SENSOR_READ_MS      1000   // read sensors every 1 s (so each upload is fresh)
#define MQTT_RETRY_MS       5000
#define WIFI_RETRY_MS       10000

// ---------- ROUTE (line following) ----------
//   Floor layout (tape):   ZONE 2 (cross-tape) ---- CENTRE (plain tape, reads 010) ---- ZONE 1 (tape ends)
//   The CENTRE cannot be seen by the sensors, so the rover ESTIMATES how far it has driven (speed x time).
//   Route: centre -> forward -> zone 1 (stay) -> reverse to the centre (pause) -> reverse to zone 2 (stay)
//          -> forward to the centre -> stop, LINE mode OFF.
#define ZONE_STAY_MS          60000   // minimum time spent at each zone (readings + AI irrigation). 60000 = 1 minute
#define ZONE_PHOTO_WAIT_MS    15000   // wait at the zone for the phone photo BEFORE the soil probe lowers (the server photographs on arrival)
#define CENTRE_PAUSE_MS        2000   // pause at the centre while reversing from zone 1 to zone 2
#define OUT_MIN_MS             1500   // outward leg: a tape END is only believed after this much driving
#define ZONE1_END_CONFIRM_MS    200   // sensors must read 000 this long after a straight run to count as "tape ended"
#define ZONE2_CREEP_MS            0   // reverse a little further after seeing zone 2's cross-tape (0 = stop on it at once)
#define BACK_FIND_MS           4000   // after leaving zone 1 the sensors are past the end of the tape; reverse straight this long to find it
#define ZONE_TIMEOUT_MS      120000   // one leg may take at most this long -> stop + alert (line lost / stuck)
#define LINE_LOST_MS           3000   // all three sensors see white for this long (mid-route) -> stop + alert
#define CROSS_DEBOUNCE_MS       400   // zone 2's cross-tape must be seen as 111 only after the sensors left the previous black area
// --- distance estimate (needed to find the centre). MEASURE these once with a ruler and a stopwatch: ---
//   let the rover drive 1 metre in line mode (watch the Serial Monitor) and note the seconds -> cm per second.
#define FWD_SPEED_CM_S        8.0f    // real forward speed at SPEED_LINE, in cm per second
#define REV_SPEED_CM_S        6.0f    // real reverse speed at SPEED_REVERSE, in cm per second (only the ratio to the forward speed really matters)
#define ZONE1_DIST_CM           0     // if you know the distance centre -> zone 1 in cm, enter it (0 = the rover measures it on the way out)
#define ZONE2_DIST_CM           0     // distance centre -> zone 2 in cm (0 = measured)
// --- rain ---
#define RAIN_RETURN_PERCENT    15     // rain intensity above this (%) for RAIN_CONFIRM_MS: stop, go back to the centre, alarm, LINE mode off
#define RAIN_CONFIRM_MS      2000
// --- soft start: slow, smooth speed-up because of the load ---
#define LINE_RAMP_PWM_PER_S   150     // how fast the motor power may rise (PWM units per second). 150 = from 0 to 120 in about 0.8 s
// Reverse driving (sensors are on the FRONT, so reversing wobbles more: keep it SLOW, then tune)
#define SPEED_REVERSE           115   // reverse line-following speed (PWM 0-255). Motors may hum below about 110
#define SPEED_REVERSE_TURN      130   // steering speed while reversing (the outer wheel; the inner wheel stops)
#define REVERSE_FLIP_STEERING  true   // true = left/right sensors steer the opposite way when reversing (normal for front sensors). If the rover runs OFF the tape in reverse, set false
// ===================== END OF SETTINGS =====================

// ---------------- Types / state ----------------
enum Mode  { MODE_MANUAL, MODE_LINE };
enum Drive { D_STOP, D_FWD, D_BACK, D_LEFT, D_RIGHT };
enum Probe { P_IDLE, P_LOWERING, P_SETTLE, P_MEASURE, P_HOLD, P_RAISING };
enum ZoneStage { ZS_PHOTO, ZS_PROBE };
enum RoutePhase { RP_OUT, RP_STAY1, RP_BACK, RP_STAY2, RP_HOME, RP_RET_REV, RP_DONE };

Mode   mode = MODE_MANUAL;       // start safe: manual, motors stopped
Drive  manualDir = D_STOP;
unsigned long lastManualCmdMs = 0;
bool   manualLatched = false;       // true = dashboard forward/backward: runs until the next command
unsigned long manualStartMs = 0;
int    manualSpeedPct = SAFE_SPEED_PERCENT;
bool   speedLocked = true;          // safety lock: ON = speed fixed at SAFE_SPEED_PERCENT
float  turnMsPerDeg = TURN_MS_PER_DEG;
bool   estopClearPending = false;
unsigned long lastLinkOkMs = 0;
unsigned long manualRunUntilMs = 0;   // for RPC timed moves (0 = use heartbeat timeout)

int  zoneIndex = 0;              // 0 = not at a zone yet
bool atZone = false;
ZoneStage zoneStage = ZS_PHOTO;
unsigned long zoneStartMs = 0, zoneLeftMs = 0;
bool routeFinished = false;
int  lastTurn = 0;                    // -1 = line was last seen on the left, +1 = right
unsigned long lineLostSince = 0;      // 0 = line currently seen
RoutePhase phase = RP_OUT;
unsigned long phaseStartMs = 0;
bool onCross = true;                  // sensors are on a cross-tape right now
unsigned long crossEndMs = 0;
bool lastWasCentre = false;           // last line pattern was 010 (straight on the tape)
bool foundLine = false;               // the tape has been seen in this leg
unsigned long creepUntilMs = 0;
float posCm = 0;                      // estimated position on the line: 0 = centre, + toward zone 1, - toward zone 2
float zone1Cm = 0;
float fwdScale = 1.0f;                // learned from the outward leg when ZONE1_DIST_CM is known (corrects the forward speed estimate)
unsigned long lastStepMs = 0;
unsigned long centrePauseUntilMs = 0;
bool centrePauseDone = false;
bool rainAbort = false;               // rain made the rover return to the centre (stays true until the next route)
unsigned long rainSince = 0;
uint32_t arrivalId = 0;               // new random number at every arrival at a zone (the server takes one photo per id)

const char *phaseName() {
  switch (phase) {
    case RP_OUT:     return "to_zone1";
    case RP_STAY1:   return "at_zone1";
    case RP_BACK:    return centrePauseDone ? "reverse_to_zone2" : "reverse_to_centre";
    case RP_STAY2:   return "at_zone2";
    case RP_HOME:    return "to_centre";
    case RP_RET_REV: return "to_centre";
    default:         return "finished";
  }
}

void startPhase(RoutePhase p) {
  phase = p;
  phaseStartMs = millis();
  zoneLeftMs = phaseStartMs;
  lastStepMs = phaseStartMs;
  lineLostSince = 0; lastTurn = 0;
  onCross = true; crossEndMs = phaseStartMs;
  lastWasCentre = false; foundLine = false; creepUntilMs = 0;
  centrePauseUntilMs = 0; centrePauseDone = false;
  Serial.printf("[ROUTE] %s (position %.0f cm)\n", phaseName(), posCm);
}

// ---- safety state ----
bool estop = false;                 // latched emergency stop
bool rainBlocked = false;           // rain sensor covered/dirty (touch says so, rain says dry)
bool lineTimeout = false;           // no zone marker within ZONE_TIMEOUT_MS
uint32_t touchBaseline = 0;
float touchDevPct = 0;
unsigned long blockedSince = 0;

struct SensorData {
  bool  dhtValid = false;
  float temperature = 0, humidity = 0;
  int   lightRaw = 0;
  float lightPercent = 0;      // 0 = dark, 100 = bright (relative, not lux)
  bool  rainDetected = false;
  int   rainRaw = 0;
  float rainPercent = 0;       // 0 = dry, 100 = fully wet plate
  // soil: only updated by a probe measurement (never stale-resent as "current")
  bool  soilValid = false;
  int   soilRaw = 0;
  float soilPercent = 0;
} sensors;

// ---- servo / probe state ----
float servoCur = SERVO_UP_ANGLE;      // ASSUMES arm is physically UP at power-on
float servoTarget = SERVO_UP_ANGLE;
unsigned long servoLastStepMs = 0;
Probe probe = P_IDLE;
unsigned long probeT = 0;
long soilSum = 0; int soilN = 0; unsigned long soilLastSampleMs = 0;

WiFiClient   netClient;
WiFiUDP      udp;
unsigned long udpSeq = 0;
float lastSoilPct = -1;          // last probe result (percent), -1 = none yet
int   lastSoilZone = 0;
PubSubClient mqtt(netClient);
WebServer    web(80);

unsigned long tSensor = 0, tTelemetry = 0, tMqtt = 0, tWifi = 0;

void sendTelemetry(const char *reason);   // forward declaration
bool zoneHold = false;                    // the AI asked the rover to stay at the zone while it waters
unsigned long zoneHoldUntilMs = 0;
#define ZONE_HOLD_MAX_MS 300000UL         // never hold longer than 5 minutes
bool liveMode = false;                    // LIVE (demo) data mode - OFF after every reset
unsigned long liveUntilMs = 0;
const char *driveName();                  // forward declaration
void updateTouch();                       // forward declaration

// ---------------- Motor control ----------------
void pwmSetup() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_IN1, PWM_FREQ, PWM_BITS);
  ledcAttach(PIN_IN2, PWM_FREQ, PWM_BITS);
  ledcAttach(PIN_IN3, PWM_FREQ, PWM_BITS);
  ledcAttach(PIN_IN4, PWM_FREQ, PWM_BITS);
  ledcAttach(PIN_SERVO, 50, 14);                 // servo: 50 Hz, 14-bit
#else
  ledcSetup(4, PWM_FREQ, PWM_BITS); ledcAttachPin(PIN_IN1, 4);
  ledcSetup(5, PWM_FREQ, PWM_BITS); ledcAttachPin(PIN_IN2, 5);
  ledcSetup(6, PWM_FREQ, PWM_BITS); ledcAttachPin(PIN_IN3, 6);
  ledcSetup(7, PWM_FREQ, PWM_BITS); ledcAttachPin(PIN_IN4, 7);
  ledcSetup(2, 50, 14);             ledcAttachPin(PIN_SERVO, 2);
#endif
}

void pwmWrite(int pin, int ch, int duty) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(pin, duty);
#else
  ledcWrite(ch, duty);
#endif
}

// speed: -255..255 (negative = reverse). PWM goes on the IN pins; the ENA/ENB caps stay on.
void setMotor(int inA, int chA, int inB, int chB, int speed) {
  speed = constrain(speed, -255, 255);
  pwmWrite(inA, chA, speed > 0 ? speed : 0);
  pwmWrite(inB, chB, speed < 0 ? -speed : 0);
}

int curL = 0, curR = 0;                        // last wheel command (before the INVERT flags)
void setMotors(int left, int right) {
  curL = left; curR = right;
  if (INVERT_LEFT)  left  = -left;
  if (INVERT_RIGHT) right = -right;
  setMotor(PIN_IN1, 4, PIN_IN2, 5, left);
  setMotor(PIN_IN3, 6, PIN_IN4, 7, right);
}

void stopMotors() { setMotors(0, 0); }

int manualPwm() {                                   // speed percent (1-100) -> PWM
  int pct = constrain(manualSpeedPct, 1, 100);
  return MIN_DRIVE_PWM + (255 - MIN_DRIVE_PWM) * (pct - 1) / 99;
}

void applyManual(Drive d) {
  switch (d) {
    case D_FWD:   setMotors(manualPwm(), manualPwm()); break;
    case D_BACK:  setMotors(-manualPwm(), -manualPwm()); break;
    case D_LEFT:  setMotors(-TURN_SPEED_PWM, TURN_SPEED_PWM); break;
    case D_RIGHT: setMotors(TURN_SPEED_PWM, -TURN_SPEED_PWM); break;
    default:      stopMotors(); break;
  }
}

// ---------------- Servo (probe arm) ----------------
void servoWriteAngle(float angle) {
  angle = constrain(angle, 0.0f, 180.0f);
  float us = SERVO_MIN_US + (SERVO_MAX_US - SERVO_MIN_US) * (angle / 180.0f);
  int duty = (int)(us * 16384.0f / 20000.0f);     // 20 ms period, 14-bit
  pwmWrite(PIN_SERVO, 2, duty);
}

bool servoAtTarget() { return fabs(servoCur - servoTarget) < 0.5f; }

// Moves the arm a few degrees at a time (slow = gentle on the sensor and the soil)
void updateServo() {
  unsigned long now = millis();
  if (now - servoLastStepMs < SERVO_STEP_MS) return;
  servoLastStepMs = now;
  if (servoAtTarget()) return;
  if (servoCur < servoTarget) servoCur = min(servoCur + SERVO_STEP_DEG, servoTarget);
  else                        servoCur = max(servoCur - SERVO_STEP_DEG, servoTarget);
  servoWriteAngle(servoCur);
}

bool armRetracted() { return probe == P_IDLE && servoAtTarget() && servoTarget == SERVO_UP_ANGLE; }

// ---------------- Sensors ----------------
DHT dht(PIN_DHT, DHT_TYPE);

bool readDHT(float &t, float &h) {
  float hh = dht.readHumidity();          // the library waits its own minimum interval between reads
  float tt = dht.readTemperature();
  if (isnan(hh) || isnan(tt)) return false;                          // no/bad reply from the sensor
  if (hh < 0 || hh > 100 || tt < -20 || tt > 70) return false;       // implausible
  h = hh; t = tt;
  return true;
}

void readSensors() {   // fast "environment" sensors; soil is read only by the probe
  float t, h;
  sensors.dhtValid = readDHT(t, h);
  if (sensors.dhtValid) { sensors.temperature = t; sensors.humidity = h; }

  long sum = 0;
  for (int i = 0; i < 8; i++) { sum += analogRead(PIN_LDR); delay(2); }
  sensors.lightRaw = sum / 8;                       // relative value 0..4095, NOT lux
  float lp = (float)(sensors.lightRaw - LIGHT_DARK_RAW) * 100.0f / (float)(LIGHT_BRIGHT_RAW - LIGHT_DARK_RAW);
  sensors.lightPercent = constrain(lp, 0.0f, 100.0f);

  bool lvl = digitalRead(PIN_RAIN_DO);
  sensors.rainDetected = RAIN_ACTIVE_LOW ? (lvl == LOW) : (lvl == HIGH);

  long rsum = 0;
  for (int i = 0; i < 8; i++) { rsum += analogRead(PIN_RAIN_AO); delay(2); }
  sensors.rainRaw = rsum / 8;
  float rp = (float)(RAIN_DRY_RAW - sensors.rainRaw) * 100.0f / (float)(RAIN_DRY_RAW - RAIN_WET_RAW);
  sensors.rainPercent = constrain(rp, 0.0f, 100.0f);
  updateTouch();
}

bool irOnBlack(int pin) {
  bool v = digitalRead(pin);
  return IR_BLACK_IS_HIGH ? v : !v;
}

// ---------------- Rain-sensor blockage check (ESP32 touch pin) ----------------
// Idea: the touch pad's capacitance changes when something (leaf, dirt, hand, cover)
// sits on the sensor. If the touch value moved a lot but the rain module says "dry",
// something is blocking it. If it says "rain", the change is expected -> no alert.
uint32_t touchAvg() {
  uint32_t s = 0;
  for (int i = 0; i < 7; i++) { s += touchRead(PIN_RAIN_TOUCH); delay(1); }
  return s / 7;
}

void touchCalibrate() {
  if (!ENABLE_RAIN_BLOCK_CHECK) { Serial.println("[TOUCH] rain-blocked check is OFF"); return; }
  delay(200);
  uint32_t s = 0;
  for (int i = 0; i < TOUCH_BASELINE_SAMPLES; i++) { s += touchAvg(); delay(20); }
  touchBaseline = s / TOUCH_BASELINE_SAMPLES;
  Serial.printf("[TOUCH] baseline = %u (keep sensor dry and clear at boot)\n", touchBaseline);
}

void updateTouch() {
  if (touchBaseline == 0) return;
  uint32_t v = touchAvg();
  touchDevPct = fabs((float)v - (float)touchBaseline) * 100.0f / (float)touchBaseline;
  bool deviates = touchDevPct > TOUCH_DEVIATION_PERCENT;
  bool candidate = deviates && !sensors.rainDetected;
  unsigned long t = millis();
  if (candidate) {
    if (blockedSince == 0) blockedSince = t;
    if (t - blockedSince >= BLOCK_CONFIRM_MS) rainBlocked = true;
  } else {
    blockedSince = 0;
    rainBlocked = false;
  }
}

// ---------------- Soil probe sequence ----------------
// lower arm -> wait to settle -> average samples -> raise arm -> send result
void probeAbort() {                       // used by E-stop: pull the arm up immediately
  if (probe != P_IDLE) {
    sensors.soilValid = false;
    probe = P_RAISING;
    servoTarget = SERVO_UP_ANGLE;
  }
}

void probeStart() {
  if (estop || probe != P_IDLE) return;
  stopMotors();
  manualDir = D_STOP;
  sensors.soilValid = false;
  probe = P_LOWERING;
  servoTarget = SERVO_DOWN_ANGLE;
  Serial.println("[PROBE] lowering");
}

void probeStep() {
  unsigned long now = millis();
  switch (probe) {
    case P_IDLE: break;

    case P_LOWERING:
      if (servoAtTarget()) { probe = P_SETTLE; probeT = now; Serial.println("[PROBE] settling"); }
      break;

    case P_SETTLE:
      if (now - probeT >= SOIL_SETTLE_MS) { probe = P_MEASURE; soilSum = 0; soilN = 0; soilLastSampleMs = 0; }
      break;

    case P_MEASURE:
      if (now - soilLastSampleMs >= 50) {
        soilLastSampleMs = now;
        soilSum += analogRead(PIN_SOIL);
        soilN++;
      }
      if (soilN >= SOIL_SAMPLES) {
        int raw = soilSum / soilN;
        sensors.soilRaw = raw;
        // near 0 or full-scale usually means disconnected / shorted sensor
        sensors.soilValid = (raw > 100 && raw < 4000);
        if (sensors.soilValid && SOIL_CALIBRATED) {
          float pct = (float)(SOIL_DRY_RAW - raw) * 100.0f / (float)(SOIL_DRY_RAW - SOIL_WET_RAW);
          sensors.soilPercent = constrain(pct, 0.0f, 100.0f);
        }
        Serial.printf("[PROBE] soil raw=%d valid=%d\n", raw, sensors.soilValid);
        probe = P_HOLD;                               // keep the arm down until SOIL_HOLD_MS has passed
      }
      break;

    case P_HOLD:
      if (now - probeT >= SOIL_HOLD_MS) {             // probeT = moment the arm reached the soil
        probe = P_RAISING;
        servoTarget = SERVO_UP_ANGLE;                 // ALWAYS retract before anything else
        Serial.println("[PROBE] retracting");
      }
      break;

    case P_RAISING:
      if (servoAtTarget()) {
        probe = P_IDLE;
        Serial.println("[PROBE] retracted");
        readSensors();
        sendTelemetry("soil_measured");
      }
      break;
  }
}

// ---------------- Telemetry (ThingsBoard) ----------------
// ---------------- Zone info -> irrigation station (UDP, local network) ----------------
void sendStationUdp(const char *reason) {
  if (WiFi.status() != WL_CONNECTED) return;
  if (!strcmp(reason, "soil_measured") && sensors.soilValid && SOIL_CALIBRATED) {
    lastSoilPct = sensors.soilPercent;
    lastSoilZone = zoneIndex;
  }
  JsonDocument d;
  d["k"] = STATION_KEY;
  d["from"] = DEVICE_ID;
  d["seq"] = ++udpSeq;
  d["zone"] = zoneIndex;
  d["at_zone"] = atZone;
  d["mode"] = (mode == MODE_LINE) ? "line" : "manual";
  d["estop"] = estop;
  d["event"] = reason;
  d["live"] = liveMode;
  if (lastSoilPct >= 0) { d["soil_pct"] = (int)(lastSoilPct + 0.5f); d["soil_zone"] = lastSoilZone; }
  char b[340];
  size_t n = serializeJson(d, b, sizeof(b));
  IPAddress target;
  if (strlen(STATION_IP) > 0 && target.fromString(STATION_IP)) { /* fixed IP */ }
  else target = IPAddress((uint32_t)WiFi.localIP() | ~(uint32_t)WiFi.subnetMask());   // broadcast
  udp.beginPacket(target, STATION_UDP_PORT);
  udp.write((const uint8_t *)b, n);
  udp.endPacket();
}

void sendTelemetry(const char *reason) {
  if (strcmp(reason, "periodic") != 0) sendStationUdp(reason);   // local copy for the station (the 3 s timer sends the periodic ones)
  if (!mqtt.connected()) return;
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["zone_id"] = zoneIndex;
  doc["mode"] = (mode == MODE_LINE) ? "line" : "manual";
  doc["dht_valid"] = sensors.dhtValid;
  if (sensors.dhtValid) {                           // never send invalid values as real ones
    doc["temperature_c"] = sensors.temperature;
    doc["humidity_percent"] = sensors.humidity;
  }
  doc["light_raw"] = sensors.lightRaw;
  doc["light_percent"] = (int)(sensors.lightPercent + 0.5f);
  doc["light_calibrated"] = LIGHT_CALIBRATED;
  doc["rain_detected"] = sensors.rainDetected;
  doc["rain_raw"] = sensors.rainRaw;
  doc["rain_intensity_percent"] = (int)(sensors.rainPercent + 0.5f);
  doc["rain_calibrated"] = RAIN_CALIBRATED;
  doc["rain_blocked"] = rainBlocked;
  doc["touch_dev_percent"] = touchDevPct;
  doc["estop"] = estop;
  doc["line_timeout"] = lineTimeout;
  doc["arm_up"] = armRetracted();
  doc["drive"] = driveName();
  doc["drive_seconds"] = (manualDir != D_STOP && mode == MODE_MANUAL) ? (int)((millis() - manualStartMs) / 100) / 10.0f : 0.0f;
  doc["speed_percent"] = manualSpeedPct;
  doc["speed_locked"] = speedLocked;

  if (!strcmp(reason, "soil_measured")) {           // soil only sent with a fresh probe reading
    doc["soil_valid"] = sensors.soilValid;
    doc["soil_raw"] = sensors.soilRaw;
    doc["soil_calibrated"] = SOIL_CALIBRATED;
    if (sensors.soilValid && SOIL_CALIBRATED) doc["soil_moisture_percent"] = sensors.soilPercent;
  }
  doc["event"] = reason;
  doc["live_mode"] = liveMode;
  doc["zone_hold"] = zoneHold;
  doc["route_phase"] = phaseName();
  doc["route_pos_cm"] = (int)(posCm + (posCm >= 0 ? 0.5f : -0.5f));
  doc["rain_abort"] = rainAbort;
  doc["at_zone"] = atZone;
  if (atZone) doc["arrival_id"] = arrivalId;
  doc["rssi"] = WiFi.RSSI();
  char buf[1100];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish("v1/devices/me/telemetry", (const uint8_t *)buf, n, false);
  Serial.printf("[TB] %s\n", buf);
}

// ---------------- Mode / commands ----------------
void setMode(Mode m) {
  if (m == MODE_LINE && estop) { Serial.println("[BLOCKED] e-stop is ON - clear it before line mode"); return; }
  mode = m;
  stopMotors();
  manualDir = D_STOP;
  manualRunUntilMs = 0;
  if (m == MODE_LINE) { routeFinished = false; atZone = false; lineTimeout = false; zoneIndex = 0; lastSoilPct = -1; rainAbort = false; rainSince = 0; posCm = 0; zone1Cm = 0; fwdScale = 1.0f; startPhase(RP_OUT); }
  Serial.printf("[MODE] %s\n", m == MODE_LINE ? "LINE" : "MANUAL");
}

void estopSet() {                         // latched: stays on until clearEstop
  estop = true;
  estopClearPending = false;
  manualLatched = false;
  stopMotors();
  manualDir = D_STOP;
  manualRunUntilMs = 0;
  probeAbort();
  mode = MODE_MANUAL;
  Serial.println("[ESTOP] ACTIVE");
  sendTelemetry("alert_estop");
}

void estopClear() {                       // only allowed once the arm is safely up
  Serial.println("[ESTOP] clear requested");
  if (!armRetracted()) {
    probe = P_IDLE;                       // drop any unfinished probe step and just bring the arm up
    servoTarget = SERVO_UP_ANGLE;
    estopClearPending = true;             // clears automatically as soon as the arm is up (about 1 s)
    Serial.println("[ESTOP] arm not up yet - bringing it up, will clear when it is");
    return;
  }
  estopClearPending = false;
  estop = false;
  Serial.println("[ESTOP] cleared");
  sendTelemetry("estop_cleared");
}

Drive parseDir(const String &s) {
  if (s == "F" || s == "forward") return D_FWD;
  if (s == "B" || s == "backward" || s == "back") return D_BACK;
  if (s == "L" || s == "left") return D_LEFT;
  if (s == "R" || s == "right") return D_RIGHT;
  return D_STOP;
}

void manualCommand(Drive d, unsigned long runMs, bool latched = false) {
  if (estop) { Serial.println("[BLOCKED] emergency stop is ON - send clearEstop"); return; }
  if (mode != MODE_MANUAL) { Serial.println("[BLOCKED] rover is in LINE mode - switch to manual first"); return; }
  if (d != D_STOP && !armRetracted()) { Serial.println("[BLOCKED] soil arm is not fully up"); return; }
  if (d != D_STOP) Serial.println("[DRIVE] motors ON");
  manualDir = d;
  lastManualCmdMs = millis();
  manualStartMs = millis();
  manualLatched = latched && d != D_STOP;
  manualRunUntilMs = runMs ? millis() + runMs : 0;
  applyManual(d);
}

// Timed turn: degrees > 0 = right, < 0 = left. No gyro, so it is calibrated by time (turnMsPerDeg).
void turnDegrees(float deg) {
  float a = fabs(deg);
  if (a < 1.0f || a > 720.0f) { Serial.println("[TURN] ignored (use 1..720 degrees)"); return; }
  Serial.printf("[TURN] %.0f deg %s\n", a, deg > 0 ? "right" : "left");
  manualCommand(deg > 0 ? D_RIGHT : D_LEFT, (unsigned long)(a * turnMsPerDeg), false);
}

const char *driveName() {
  if (mode == MODE_LINE) return "line_following";
  switch (manualDir) {
    case D_FWD: return "forward";
    case D_BACK: return "backward";
    case D_LEFT: return "turning_left";
    case D_RIGHT: return "turning_right";
    default: return "stopped";
  }
}

// ThingsBoard RPC:  {"method":"move","params":"forward"}
//                   {"method":"setMode","params":"line"}   (or "manual", or true/false)
//                   {"method":"setValue","params":true}    (switch widget: ON = line, OFF = manual)  /  getValue
//                   {"method":"move","params":"forward"|"backward"}  runs until the next command (stop / other move)
//                   {"method":"move","params":"left"|"right"}        small turn (TURN_STEP_DEG degrees)
//                   {"method":"turn","params":90}  (+ right, - left; 90, -90, 180, 360 ...)
//                   {"method":"setSpeed","params":70}  (1-100, manual mode only, safety lock must be OFF)
//                   {"method":"setSafety","params":true}  (true = lock ON / safe speed, false = lock OFF)
//                   {"method":"setTurnCal","params":7.0}  (ms per degree, calibration)
//                   {"method":"measureSoil"}               (manual mode, rover stopped)
//                   {"method":"stop"}
//                   {"method":"estop"}  /  {"method":"clearEstop"}
void onMqtt(char *topic, byte *payload, unsigned int len) {
  String t(topic);
  Serial.printf("[RPC RAW] %s  %.*s\n", topic, (int)len, (const char *)payload);   // exactly what the dashboard sent
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char *method = doc["method"] | "";
  String params = doc["params"].is<const char *>() ? String((const char *)doc["params"]) : "";
  JsonVariantConst pv = doc["params"];
  bool pOn;                                          // true = LINE mode, false = MANUAL (for ON/OFF switches)
  if (pv.is<bool>()) pOn = pv.as<bool>();
  else if (pv.is<int>()) pOn = (pv.as<int>() != 0);
  else pOn = (params == "line" || params == "true" || params == "on" || params == "1");
  Serial.printf("[RPC] %s %s\n", method, params.c_str());

  if (!strcmp(method, "move")) {
    Drive d = parseDir(params);
    if (d == D_LEFT || d == D_RIGHT) turnDegrees(d == D_RIGHT ? TURN_STEP_DEG : -TURN_STEP_DEG);   // one click = small turn
    else if (d == D_STOP) Serial.println("[RPC] move IGNORED: params must be \"forward\", \"backward\", \"left\" or \"right\" (use the stop method to stop)");
    else manualCommand(d, 0, true);                                                                  // forward/backward: until the next command
  }
  else if (!strcmp(method, "turn")) {                  // params: degrees, + = right, - = left (e.g. 90, -90, 180, 360)
    float deg = (pv.is<float>() || pv.is<int>()) ? pv.as<float>() : params.toFloat();
    turnDegrees(deg);
  }
  else if (!strcmp(method, "setSpeed")) {              // params: 1..100 (percent). Only in manual mode and with the safety lock OFF
    int v = (pv.is<float>() || pv.is<int>()) ? (int)(pv.as<float>() + 0.5f) : params.toInt();
    if (mode != MODE_MANUAL)      Serial.println("[SPEED] ignored: not in manual mode");
    else if (speedLocked)         Serial.println("[SPEED] ignored: safety lock is ON");
    else { manualSpeedPct = constrain(v, 1, 100); Serial.printf("[SPEED] %d %%\n", manualSpeedPct); if (manualDir == D_FWD || manualDir == D_BACK) applyManual(manualDir); }
  }
  else if (!strcmp(method, "setSafety")) {             // params true = lock ON (speed back to the safe value), false = lock OFF
    speedLocked = pOn;
    if (speedLocked) { manualSpeedPct = SAFE_SPEED_PERCENT; if (manualDir == D_FWD || manualDir == D_BACK) applyManual(manualDir); }
    Serial.printf("[SAFETY LOCK] %s\n", speedLocked ? "ON" : "OFF");
  }
  else if (!strcmp(method, "setTurnCal")) {            // params: ms per degree (2..20), kept until reboot
    float v = (pv.is<float>() || pv.is<int>()) ? pv.as<float>() : params.toFloat();
    if (v >= 2.0f && v <= 20.0f) { turnMsPerDeg = v; Serial.printf("[TURN CAL] %.2f ms/deg\n", turnMsPerDeg); }
  }
  else if (!strcmp(method, "getSpeed") || !strcmp(method, "getSafety") || !strcmp(method, "getTurnCal")) { /* answered below */ }
  else if (!strcmp(method, "stop"))         { manualCommand(D_STOP, 0); if (mode == MODE_LINE) setMode(MODE_MANUAL); }
  else if (!strcmp(method, "setMode"))      setMode(pOn ? MODE_LINE : MODE_MANUAL);
  else if (!strcmp(method, "setValue"))     setMode(pOn ? MODE_LINE : MODE_MANUAL);   // ThingsBoard switch widget (ON = line)
  else if (!strcmp(method, "getValue"))     { /* switch asks for its state: answered below */ }
  else if (!strcmp(method, "measureSoil"))  { if (mode == MODE_MANUAL || atZone) probeStart(); }   // also while stopped at a zone (the AI re-measures after each watering turn)
  else if (!strcmp(method, "setLive"))      { liveMode = pOn; liveUntilMs = millis() + LIVE_MAX_MS; sendTelemetry("live_changed"); }
  else if (!strcmp(method, "getLive"))      { /* answered below */ }
  else if (!strcmp(method, "holdZone"))     { zoneHold = pOn; zoneHoldUntilMs = millis() + ZONE_HOLD_MAX_MS; Serial.printf("[HOLD] stay at zone: %s\n", zoneHold ? "YES (max 5 min)" : "no"); }
  else if (!strcmp(method, "estop"))        estopSet();
  else if (!strcmp(method, "clearEstop"))   estopClear();
  else Serial.printf("[RPC] UNKNOWN method '%s' - check the button's method name (it is case sensitive)\n", method);

  int idx = t.lastIndexOf('/');
  String reqId = t.substring(idx + 1);
  String respTopic = "v1/devices/me/rpc/response/" + reqId;
  String resp = String("{\"mode\":\"") + (mode == MODE_LINE ? "line" : "manual") + "\",\"zone\":" + zoneIndex +
                ",\"arm_up\":" + (armRetracted() ? "true" : "false") + "}";
  if (!strcmp(method, "getValue") || !strcmp(method, "setValue")) resp = (mode == MODE_LINE) ? "true" : "false";
  else if (!strcmp(method, "getSpeed") || !strcmp(method, "setSpeed")) resp = String(manualSpeedPct);
  else if (!strcmp(method, "getSafety") || !strcmp(method, "setSafety")) resp = speedLocked ? "true" : "false";
  else if (!strcmp(method, "getTurnCal") || !strcmp(method, "setTurnCal")) resp = String(turnMsPerDeg, 2);
  else if (!strcmp(method, "getLive") || !strcmp(method, "setLive")) resp = liveMode ? "true" : "false";
  else if (!strcmp(method, "holdZone")) resp = zoneHold ? "true" : "false";
  mqtt.publish(respTopic.c_str(), resp.c_str());
  // Rover controls are reported to ThingsBoard IMMEDIATELY (estop / clearEstop / measureSoil send their own report)
  if (!strcmp(method, "move") || !strcmp(method, "turn") || !strcmp(method, "setSpeed") || !strcmp(method, "setSafety") ||
      !strcmp(method, "setTurnCal") || !strcmp(method, "stop") || !strcmp(method, "setMode") || !strcmp(method, "setValue"))
    sendTelemetry("control");
}

// ---------------- Line following / route ----------------
// Soft start: the motor power may only RISE at LINE_RAMP_PWM_PER_S (slows down and steering corrections are immediate).
int rampStep(int cur, int target, int maxStep) {
  bool speedingUp = abs(target) > abs(cur) && (cur == 0 || (cur > 0) == (target > 0));
  if (!speedingUp) return target;
  return target > cur ? min(target, cur + maxStep) : max(target, cur - maxStep);
}

void lineMotors(int l, int r) {
  static unsigned long t = 0;
  unsigned long now = millis();
  unsigned long dt = now - t; t = now;
  if (dt > 100) dt = 100;
  int m = (int)((long)LINE_RAMP_PWM_PER_S * (long)dt / 1000L);
  if (m < 1) m = 1;
  setMotors(rampStep(curL, l, m), rampStep(curR, r, m));
}

// Wheel command for line following. Forward: (left, right). Reverse with flipped steering: the wheels swap sides and run backwards.
void driveLine(int l, int r, bool reverse) {
  if (!reverse) lineMotors(l, r);
  else if (REVERSE_FLIP_STEERING) lineMotors(-r, -l);
  else lineMotors(-l, -r);
}

// Follows the tape. Returns false when all three sensors see white (no line).
bool followLine(bool L, bool C, bool R, bool reverse) {
  int sp = reverse ? SPEED_REVERSE : SPEED_LINE;
  int st = reverse ? SPEED_REVERSE_TURN : SPEED_LINE_TURN;
  int inner = 0;                                 // the inner wheel just stops (gentle steering; a load needs smooth moves)
  int outer = reverse ? st : st + 30;
  if (C && !L && !R)      { driveLine(sp, sp, reverse); }                              // centred on the line
  else if (L && C && R)   { driveLine(sp, sp, reverse); }                              // a cross-tape: keep going straight
  else if (L && !R)       { lastTurn = -1; driveLine(inner, outer, reverse); }         // line drifted to the left
  else if (R && !L)       { lastTurn = 1;  driveLine(outer, inner, reverse); }         // line drifted to the right
  else if (L && R)        { driveLine(st, st, reverse); }                              // odd shape: creep ahead
  else return false;
  return true;
}

// Dead reckoning: add the distance driven since the last step (from the wheel command and the measured speeds).
void integratePosition(unsigned long now) {
  unsigned long dt = now - lastStepMs;
  lastStepMs = now;
  if (dt > 100) dt = 100;
  int avg = (curL + curR) / 2;
  if (avg > 0)      posCm += FWD_SPEED_CM_S * fwdScale * ((float)avg / SPEED_LINE) * dt / 1000.0f;
  else if (avg < 0) posCm -= REV_SPEED_CM_S * ((float)(-avg) / SPEED_REVERSE) * dt / 1000.0f;
}

void arriveAtZone(int z) {
  unsigned long legMs = millis() - phaseStartMs;
  stopMotors();
  atZone = true;
  zoneStage = ZS_PHOTO;
  zoneStartMs = millis();
  zoneIndex = z;
  phase = (z == 1) ? RP_STAY1 : RP_STAY2;
  phaseStartMs = zoneStartMs;
  lineLostSince = 0;
  arrivalId = (esp_random() & 0x7fffffff) | 1;       // new id: the server takes one photo for it
  if (z == 1) {
    if (ZONE1_DIST_CM > 0) {                          // known distance: removes the drift and teaches the forward speed
      if (posCm > 10.0f) fwdScale = ZONE1_DIST_CM / posCm;
      posCm = ZONE1_DIST_CM;
    }
    zone1Cm = posCm;
  } else {
    if (ZONE2_DIST_CM > 0) posCm = -ZONE2_DIST_CM;
    else if (posCm > -5.0f) posCm = -zone1Cm;         // the centre was not found by the estimate: assume zone 2 is as far as zone 1
  }
  readSensors();
  Serial.printf("[ZONE] arrived zone %d at %.0f cm (this leg took %.1f s) - staying %lu s\n", z, posCm, legMs / 1000.0f, (unsigned long)(ZONE_STAY_MS / 1000));
  sendTelemetry("zone_arrived");               // the server uses this event (photo + irrigation decision)
}

void routeAlert(const char *what, const char *event) {
  stopMotors();
  lineTimeout = true;
  Serial.printf("[ALERT] %s - stopping\n", what);
  setMode(MODE_MANUAL);
  sendTelemetry(event);
}

// At the centre: stop, switch LINE mode OFF, report.
void finishAtCentre() {
  stopMotors();
  atZone = false;
  zoneIndex = 0;
  zoneHold = false;
  posCm = 0;
  phase = RP_DONE;
  routeFinished = true;
  Serial.println(rainAbort ? "[ROUTE] back at the centre after rain - LINE mode OFF" : "[ROUTE] back at the centre - finished, LINE mode OFF");
  setMode(MODE_MANUAL);                        // line mode OFF (the dashboard switch turns off)
  sendTelemetry(rainAbort ? "rain_return_done" : "route_finished");
}

// Rain: give up what we were doing and go back to the centre.
void startRainReturn() {
  rainAbort = true;
  zoneHold = false;
  atZone = false;
  centrePauseUntilMs = 0;
  if (probe != P_IDLE) probeAbort();            // pull the soil arm up first
  Serial.printf("[ALERT] RAIN %.0f %% - returning to the centre (position %.0f cm)\n", sensors.rainPercent, posCm);
  sendTelemetry("alert_rain_return");           // alarm to the cloud
  if (phase == RP_HOME || phase == RP_RET_REV) return;                       // already on the way to the centre
  if (posCm > 1.0f)       startPhase(RP_RET_REV);                           // zone 1 side: reverse to the centre
  else if (posCm < -1.0f) startPhase(RP_HOME);                              // zone 2 side: forward to the centre
  else                    finishAtCentre();                                 // already at the centre
}

void lineFollowStep() {
  if (routeFinished || phase == RP_DONE) { stopMotors(); return; }
  unsigned long now = millis();

  // ---- rain check (every phase) ----
  if (!rainAbort) {
    if (sensors.rainPercent > RAIN_RETURN_PERCENT) {
      if (rainSince == 0) rainSince = now;
      if (now - rainSince >= RAIN_CONFIRM_MS) { startRainReturn(); if (routeFinished) return; }
    } else rainSince = 0;
  }

  // ---- standing at a zone: photo, soil probe, AI irrigation ----
  if (phase == RP_STAY1 || phase == RP_STAY2) {
    stopMotors();
    lastStepMs = now;
    if (zoneStage == ZS_PHOTO) {
      if (now - zoneStartMs >= ZONE_PHOTO_WAIT_MS) { probeStart(); zoneStage = ZS_PROBE; }
    }
    bool stayOver = (now - zoneStartMs >= ZONE_STAY_MS);
    bool held = zoneHold && (long)(now - zoneHoldUntilMs) < 0;        // the AI is watering: stay until it lets go (max 5 min)
    if (zoneStage == ZS_PROBE && probe == P_IDLE && armRetracted() && stayOver && !held) {
      atZone = false;
      Serial.printf("[ZONE] leaving zone %d\n", zoneIndex);
      startPhase(phase == RP_STAY1 ? RP_BACK : RP_HOME);
    }
    return;
  }

  if (!armRetracted()) { stopMotors(); lastStepMs = now; return; }      // safety: never drive with the arm down

  if (now - phaseStartMs > ZONE_TIMEOUT_MS) { routeAlert("leg timeout", "alert_line_timeout"); return; }

  integratePosition(now);

  bool L = irOnBlack(PIN_IR_LEFT);
  bool C = irOnBlack(PIN_IR_CENTER);
  bool R = irOnBlack(PIN_IR_RIGHT);
  bool reverse = (phase == RP_BACK || phase == RP_RET_REV);
  if (L || C || R) foundLine = true;
  if (C && !L && !R) lastWasCentre = true; else if (L || R) lastWasCentre = false;

  // cross-tape detection (111) - only zone 2 has one
  bool crossEdge = false;
  if (L && C && R) {
    if (!onCross && now - crossEndMs > CROSS_DEBOUNCE_MS) crossEdge = true;
    onCross = true;
  } else {
    if (onCross) crossEndMs = now;
    onCross = false;
  }

  // ---- reaching the centre (distance estimate) ----
  if (phase == RP_HOME && posCm >= 0) { finishAtCentre(); return; }                 // forward from zone 2 (or rain): stop at once
  if (phase == RP_RET_REV && posCm <= 0) { finishAtCentre(); return; }              // reverse from zone 1 after rain
  if (phase == RP_BACK && !centrePauseDone && posCm <= 0) {                         // reverse from zone 1: pause at the centre
    stopMotors();
    centrePauseDone = true;
    centrePauseUntilMs = now + CENTRE_PAUSE_MS;
    Serial.printf("[ROUTE] at the centre (%.0f cm) - waiting %lu ms\n", posCm, (unsigned long)CENTRE_PAUSE_MS);
    return;
  }
  if (centrePauseUntilMs) {
    if ((long)(now - centrePauseUntilMs) < 0) { stopMotors(); lastStepMs = now; return; }
    centrePauseUntilMs = 0;                                                         // pause over: reverse on
    lastStepMs = now;
  }

  // ---- zone 2: cross-tape while reversing ----
  if (creepUntilMs) {
    if ((long)(now - creepUntilMs) >= 0) { creepUntilMs = 0; arriveAtZone(2); }
    else driveLine(SPEED_REVERSE, SPEED_REVERSE, true);
    return;
  }
  if (crossEdge && phase == RP_BACK) {
    if (!centrePauseDone) Serial.println("[ROUTE] cross-tape seen before the centre estimate - the distance estimate is too long (check FWD/REV_SPEED_CM_S)");
    if (ZONE2_CREEP_MS > 0) { creepUntilMs = now + ZONE2_CREEP_MS; return; }
    arriveAtZone(2);
    return;
  }

  if (followLine(L, C, R, reverse)) { lineLostSince = 0; return; }

  // ---- all three sensors see white ----
  if (phase == RP_OUT && lastWasCentre && now - phaseStartMs > OUT_MIN_MS) {   // 010 then 000 = the tape ended = zone 1
    if (lineLostSince == 0) lineLostSince = now;
    if (now - lineLostSince >= ZONE1_END_CONFIRM_MS) { arriveAtZone(1); return; }
    lineMotors(SPEED_LINE, SPEED_LINE);
    return;
  }
  bool finding = reverse && !foundLine;               // leaving zone 1: we are past the end of the tape, reverse straight to find it
  if (lineLostSince == 0) lineLostSince = finding ? phaseStartMs : now;
  if (now - lineLostSince > (finding ? (unsigned long)BACK_FIND_MS : (unsigned long)LINE_LOST_MS)) { routeAlert("line lost", "alert_line_lost"); return; }
  if (reverse)                 driveLine(SPEED_REVERSE, SPEED_REVERSE, true);
  else if (lastTurn <= 0)      lineMotors(0, SPEED_LINE_TURN);                      // search toward the last seen side
  else                         lineMotors(SPEED_LINE_TURN, 0);
}

// ---------------- Local web control ----------------
const char PAGE[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Rover</title><style>
body{font-family:sans-serif;text-align:center;background:#1b1f23;color:#eee;margin:0;padding:12px}
button{width:90px;height:90px;margin:5px;font-size:26px;border:0;border-radius:14px;background:#2e7d32;color:#fff;touch-action:manipulation}
.s{background:#c62828}.m{width:150px;height:50px;font-size:16px;background:#1565c0}
#st{margin:10px;font-size:14px;white-space:pre}
</style></head><body><h2>Smart Agri Rover</h2>
<div><button class="m" onclick="mode('manual')">MANUAL</button><button class="m" onclick="mode('line')">LINE FOLLOW</button></div>
<div><button id="F">&#9650;</button></div>
<div><button id="L">&#9664;</button><button class="s" onclick="fetch('/stop')">&#9632;</button><button id="R">&#9654;</button></div>
<div><button id="B">&#9660;</button></div>
<div><button class="m" onclick="fetch('/probe')">MEASURE SOIL</button></div>
<div><button class="m s" onclick="fetch('/estop')">EMERGENCY STOP</button><button class="m" onclick="fetch('/clear')">CLEAR E-STOP</button></div>
<div id="st">...</div>
<script>
let timer=null;
function send(d){fetch('/cmd?d='+d).catch(()=>{});}
function hold(id){const e=document.getElementById(id);
 const start=ev=>{ev.preventDefault();send(id);clearInterval(timer);timer=setInterval(()=>send(id),250);};
 const end=ev=>{ev.preventDefault();clearInterval(timer);send('S');};
 e.addEventListener('touchstart',start);e.addEventListener('mousedown',start);
 e.addEventListener('touchend',end);e.addEventListener('mouseup',end);e.addEventListener('mouseleave',end);}
['F','B','L','R'].forEach(hold);
function mode(m){fetch('/mode?m='+m);}
setInterval(()=>fetch('/status').then(r=>r.text()).then(t=>document.getElementById('st').innerText=t).catch(()=>{}),1500);
</script></body></html>
)rawliteral";

void setupWeb() {
  web.on("/", []() { web.send_P(200, "text/html", PAGE); });
  web.on("/cmd", []() {
    manualCommand(parseDir(web.arg("d")), 0);       // heartbeat style: stops if you let go / lose Wi-Fi
    web.send(200, "text/plain", "ok");
  });
  web.on("/mode", []() {
    setMode(web.arg("m") == "line" ? MODE_LINE : MODE_MANUAL);
    web.send(200, "text/plain", "ok");
  });
  web.on("/stop", []() {                            // square STOP button: also leaves LINE mode
    manualCommand(D_STOP, 0);
    if (mode == MODE_LINE) setMode(MODE_MANUAL);
    web.send(200, "text/plain", "ok");
  });
  web.on("/estop", []() { estopSet(); web.send(200, "text/plain", "ok"); });
  web.on("/clear", []() { estopClear(); web.send(200, "text/plain", "ok"); });
  web.on("/probe", []() {
    if (mode == MODE_MANUAL) probeStart();
    web.send(200, "text/plain", "ok");
  });
  web.on("/status", []() {
    const char *armTxt = armRetracted() ? "UP (safe)" : (probe == P_IDLE ? "moving" : "PROBING");
    String soil = "no reading yet";
    if (probe == P_IDLE && (sensors.soilRaw > 0)) {
      soil = String("raw ") + sensors.soilRaw;
      if (sensors.soilValid && SOIL_CALIBRATED) soil += String(" = ") + String(sensors.soilPercent, 0) + " %";
      if (!sensors.soilValid) soil += " (INVALID)";
    }
    String s = String("Mode: ") + (mode == MODE_LINE ? "LINE" : "MANUAL") + "  Zone: " + zoneIndex + "  Route: " + phaseName() + " (" + String(posCm, 0) + " cm)" +
               "\nArm: " + armTxt + "  Soil: " + soil +
               "\nTemp: " + (sensors.dhtValid ? String(sensors.temperature, 1) + " C" : "invalid") +
               "  Hum: " + (sensors.dhtValid ? String(sensors.humidity, 1) + " %" : "invalid") +
               "\nLight(raw): " + sensors.lightRaw + " = " + String(sensors.lightPercent, 0) + "%" + (LIGHT_CALIBRATED ? "" : " (uncalibrated)") + "  Rain: " + (sensors.rainDetected ? "YES" : "no") + " " + String(sensors.rainPercent, 0) + "%" + (RAIN_CALIBRATED ? "" : " (uncalibrated)") +
               "\nCloud: " + (mqtt.connected() ? "connected" : "offline") +
               "\nALERTS: " + (estop ? "[E-STOP] " : "") + (rainBlocked ? "[RAIN SENSOR BLOCKED] " : "") +
               (lineTimeout ? "[LINE LOST] " : "") +
               ((!estop && !rainBlocked && !lineTimeout) ? "none" : "") +
               "  (touch dev " + String(touchDevPct, 0) + "%)";
    web.send(200, "text/plain", s);
  });
  web.begin();
}

// ---------------- Wi-Fi / MQTT (non-blocking) ----------------
void maintainConnections() {
  unsigned long now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (now - tWifi > WIFI_RETRY_MS) { tWifi = now; Serial.println("[WiFi] reconnecting"); WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASSWORD); }
    return;
  }
  // Connecting can block for a couple of seconds, so only try while the motors are idle (stopped manual, or waiting at a zone)
  bool motorsIdle = (manualDir == D_STOP) && (mode == MODE_MANUAL || atZone);
  if (!mqtt.connected() && motorsIdle && now - tMqtt > MQTT_RETRY_MS) {
    tMqtt = now;
    Serial.print("[TB] connecting... ");
    if (mqtt.connect(DEVICE_ID, TB_ACCESS_TOKEN, NULL)) {
      Serial.println("ok");
      mqtt.subscribe("v1/devices/me/rpc/request/+");
      sendTelemetry("connected");              // full report right away so the dashboard fills in
    } else {
      Serial.printf("failed rc=%d\n", mqtt.state());
    }
  }
}

// ---------------- Arduino entry points ----------------
void setup() {
  // Motor pins LOW first, before anything else, so the wheels never twitch at power-on
  pinMode(PIN_IN1, OUTPUT); digitalWrite(PIN_IN1, LOW);
  pinMode(PIN_IN2, OUTPUT); digitalWrite(PIN_IN2, LOW);
  pinMode(PIN_IN3, OUTPUT); digitalWrite(PIN_IN3, LOW);
  pinMode(PIN_IN4, OUTPUT); digitalWrite(PIN_IN4, LOW);
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== ROVER FIRMWARE 2026-11-03 r14 (DHT11 + 5-min cloud updates + LIVE mode + holdZone) ===");
  Serial.println("Serial test: type f b l r s e c i + Enter  (forward, back, left, right, stop, e-stop, clear, status)");

  pwmSetup();
  stopMotors();
  servoWriteAngle(SERVO_UP_ANGLE);                  // arm to the retracted position

  pinMode(PIN_RAIN_DO, INPUT);
  pinMode(PIN_RAIN_AO, INPUT);
  pinMode(PIN_IR_LEFT, INPUT);
  pinMode(PIN_IR_CENTER, INPUT);
  pinMode(PIN_IR_RIGHT, INPUT);
  pinMode(PIN_SOIL, INPUT);
  analogReadResolution(12);
  dht.begin();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) { delay(300); Serial.print("."); }
  if (WiFi.status() == WL_CONNECTED) Serial.printf("\n[WiFi] IP: http://%s\n", WiFi.localIP().toString().c_str());
  else Serial.println("\n[WiFi] not connected yet (will keep retrying; IR line mode still works)");

  mqtt.setServer(TB_HOST, TB_PORT);
  mqtt.setKeepAlive(5);                             // notice a lost connection within ~8 s
  mqtt.setSocketTimeout(2);                         // a failed connect gives up after ~2 s instead of blocking longer
  mqtt.setCallback(onMqtt);
  mqtt.setBufferSize(900);
  setupWeb();
  touchCalibrate();
  readSensors();
}

// ---------------- Test from the Serial Monitor (no cloud needed) ----------------
// Type ONE letter and press Enter:  f=forward 2 s  b=backward 2 s  l=left 90  r=right 90
//                                   s=stop  e=emergency stop  c=clear e-stop  i=print status
void printStatus() {
  Serial.printf("[STATUS] route=%s mode=%s estop=%d arm_up=%d probe=%d drive=%s speed=%d%% lock=%d wifi=%d mqtt=%d\n",
                phaseName(), mode == MODE_LINE ? "LINE" : "MANUAL", (int)estop, (int)armRetracted(), (int)probe, driveName(),
                manualSpeedPct, (int)speedLocked, (int)(WiFi.status() == WL_CONNECTED), (int)mqtt.connected());
}

void serialCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'f') { Serial.println("[SERIAL] forward 2 s");  manualCommand(D_FWD, 2000, false); }
    else if (c == 'b') { Serial.println("[SERIAL] backward 2 s"); manualCommand(D_BACK, 2000, false); }
    else if (c == 'l') { Serial.println("[SERIAL] left 90");  turnDegrees(-90); }
    else if (c == 'r') { Serial.println("[SERIAL] right 90"); turnDegrees(90); }
    else if (c == 's') { Serial.println("[SERIAL] stop");     manualCommand(D_STOP, 0); }
    else if (c == 'e') { Serial.println("[SERIAL] e-stop");   estopSet(); }
    else if (c == 'c') { Serial.println("[SERIAL] clear e-stop"); estopClear(); }
    else if (c == 'i') { /* status printed below */ }
    else continue;
    printStatus();
  }
}

void loop() {
  unsigned long now = millis();
  serialCommands();

  maintainConnections();
  if (WiFi.status() == WL_CONNECTED) { web.handleClient(); mqtt.loop(); }

  if (mqtt.connected()) lastLinkOkMs = now;
  updateServo();
  probeStep();
  if (estopClearPending && armRetracted()) estopClear();

  if (now - tSensor >= SENSOR_READ_MS && probe == P_IDLE) { tSensor = now; readSensors(); }
  if (now - tTelemetry >= TELEMETRY_MS) { tTelemetry = now; sendStationUdp("periodic"); }      // station link, every 3 s
  if (liveMode && (long)(now - liveUntilMs) >= 0) { liveMode = false; Serial.println("[LIVE] 30 minutes over - live mode OFF"); sendTelemetry("live_changed"); }
  static unsigned long tCloud = 0;
  if (now - tCloud >= (liveMode ? CLOUD_LIVE_MS : CLOUD_IDLE_MS)) { tCloud = now; sendTelemetry("periodic"); }   // ThingsBoard

  // Send an alert immediately when the rain-sensor-blocked state changes
  static bool prevBlocked = false;
  if (rainBlocked != prevBlocked) {
    prevBlocked = rainBlocked;
    Serial.println(rainBlocked ? "[ALERT] rain sensor blocked" : "[ALERT] rain sensor clear");
    sendTelemetry(rainBlocked ? "alert_rain_blocked" : "alert_rain_cleared");
  }

  now = millis();       // IMPORTANT: refresh the clock. Commands handled above (web/MQTT) were stamped AFTER the old "now",
                        // which made "now - stamp" wrap to a huge number and stopped forward/backward at once.
  if (estop || probe != P_IDLE) {
    stopMotors();                                   // no movement during E-stop or while probing
  } else if (mode == MODE_MANUAL) {
    // Safety: stop if the controller stops sending commands (or Wi-Fi drops)
    bool expired;
    if (manualLatched) {                            // dashboard forward/backward: keeps going until told to stop...
      bool linkBad = (WiFi.status() != WL_CONNECTED) || (!mqtt.connected() && now - lastLinkOkMs > LINK_LOST_STOP_MS);
      bool tooLong = MANUAL_MAX_RUN_MS && (now - manualStartMs > MANUAL_MAX_RUN_MS);
      expired = linkBad || tooLong;                 // ...but a lost link or the max run time stops it
      if (expired && manualDir != D_STOP) Serial.println(linkBad ? "[SAFETY] link lost - stopping" : "[SAFETY] max run time - stopping");
    } else {
      expired = manualRunUntilMs ? (now >= manualRunUntilMs)
                                 : (now - lastManualCmdMs > MANUAL_TIMEOUT_MS);
    }
    if (manualDir != D_STOP && expired) { manualDir = D_STOP; manualRunUntilMs = 0; manualLatched = false; stopMotors(); }
  } else {
    lineFollowStep();                               // runs locally, even with no Wi-Fi
  }
}

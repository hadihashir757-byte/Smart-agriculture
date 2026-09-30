// ============================================================================
// FUTURE ADDITION - CO2 Monitoring (not wired into the main sketch)
// ============================================================================
// This is a standalone reference for adding a CO2 sensor later. It is NOT
// included in weather_monitoring_system.ino and changes nothing about the
// current working setup - copy the relevant pieces into the main sketch
// yourself when you're ready to add this for real.
//
// Why CO2, out of all possible "air monitoring" sensors: CO2 concentration
// directly affects photosynthesis rate, so it's the one air-quality reading
// that actually ties into plant growth - unlike general air-quality/VOC
// sensors, which mostly matter for human health, not plant health.
//
// HARDWARE: MH-Z19 (or MH-Z19B/C) NDIR CO2 sensor - the standard choice for
// this kind of project. Cheap gas sensors like the MQ-135 are NOT true CO2
// sensors (they respond to a mix of gases and need heavy calibration) - the
// MH-Z19 gives genuine, usable ppm readings out of the box.
//
// WIRING (UNO R4 WiFi):
//   MH-Z19 VCC  -> 5V
//   MH-Z19 GND  -> GND
//   MH-Z19 TX   -> Arduino RX1 (pin 0... actually Serial1's RX pin, check your
//                  board's pinout diagram - labeled RX1 on most UNO R4 boards)
//   MH-Z19 RX   -> Arduino TX1 (Serial1's TX pin, labeled TX1)
//
// Uses Serial1 (hardware UART), not the USB Serial port - this project
// already uses Serial (USB) for all its debug logging, so Serial1 keeps the
// CO2 sensor's communication completely separate from that.
//
// LIBRARY NEEDED: "MH-Z19" by Jonathan Dempsey (Arduino Library Manager)

#include <MHZ19.h>

#define CO2_RX_PIN 0  // check your board - this should be Serial1's RX
#define CO2_TX_PIN 1  // check your board - this should be Serial1's TX

MHZ19 mhz19;

// How this would slot into the existing project, if/when you wire it in:
//   1. Add a new Blynk datastream, e.g. V15 "CO2 Level" (Integer, ppm)
//   2. Call readCO2() inside sendSensorData(), same as the other sensors
//   3. Add Blynk.virtualWrite(V15, co2ppm) alongside the other virtualWrite calls
//   4. Optionally include it in postSensorDataToServer()'s JSON body too, if
//      you want Flask/the AI layer to eventually factor it into anything

void setupCO2Sensor() {
  Serial1.begin(9600);
  mhz19.begin(Serial1);
  mhz19.autoCalibration(false);  // manual calibration is more reliable for a
                                  // fixed indoor/greenhouse setup than the
                                  // sensor's built-in auto-calibration, which
                                  // assumes it periodically sees fresh outdoor
                                  // air (~400ppm) - often untrue indoors
}

// Returns CO2 in ppm, or -1 if the reading looks invalid (sensor not warmed
// up yet - MH-Z19 needs ~3 minutes after power-on before readings stabilize,
// or a communication fault).
int readCO2() {
  int co2 = mhz19.getCO2();
  if (co2 <= 0 || co2 > 10000) {  // sanity bounds - outside this range means
                                    // a bad reading, not a real measurement
    return -1;
  }
  return co2;
}

// Rough reference for what CO2 levels mean in a growing context:
//   400-1000 ppm   - normal outdoor/well-ventilated range
//   1000-2000 ppm  - typical enclosed room, generally fine for most plants
//   2000-5000 ppm  - poor ventilation; some crops in commercial greenhouses
//                    are deliberately enriched to 1000-1500ppm for FASTER
//                    growth, but that's an intentional controlled setup, not
//                    a warning sign for a home garden
//   >5000 ppm      - unusual, check ventilation and sensor placement

#pragma once
#include <Arduino.h>
#include "discipliner.h"
#include "gps.h"
#include "adf4351.h"

class StatusManager {
public:
    StatusManager(Discipliner &disc, GPSParser &gps,
                  ADF4351 &adf1, ADF4351 &adf2);
    void begin();
    void update();          // call every ~500ms from core0
    void printDebug();      // verbose serial output
    void setDiscAvgWindowSecs(uint32_t secs) { _discAvgWindowSecs = secs; }
    void setMeasuredOCXO(double measuredHz, double freqErrorPpb) {
        _measuredFreqHz = measuredHz;
        _measuredFreqErrorPpb = freqErrorPpb;
    }
    // Allow external code to assert/clear the alarm LED
    // Steady alarm indicates hardware failure; flashing alarm indicates
    // out-of-lock/health condition.
    void setAlarmSteady(bool on);
    void setAlarmFlash(bool on);
    void setStatusIntervalMs(uint32_t intervalMs) { _statusIntervalMs = intervalMs; }
    uint32_t statusIntervalMs() const { return _statusIntervalMs; }

private:
    Discipliner& _disc;
    GPSParser&   _gps;
    ADF4351&     _adf1;
    ADF4351&     _adf2;

    uint32_t _lastPrint;
    uint32_t _adf1LostMs;
    uint32_t _adf2LostMs;
    bool     _alarmActiveSteady;
    bool     _alarmActiveFlash;
    bool     _alarmFlashOn;
    uint32_t _lastAlarmFlashMs;
    // Satellite LED blink state
    bool     _satsBlinkOn;
    uint32_t _lastSatsBlinkMs;
    // ADF lock LED blink state
    bool     _adf1BlinkOn;
    bool     _adf2BlinkOn;
    uint32_t _lastAdfBlinkMs;
    uint32_t _discAvgWindowSecs;
    uint32_t _statusIntervalMs;
    double   _measuredFreqHz;
    double   _measuredFreqErrorPpb;

    void setLED(uint8_t pin, bool on);
    // (implementation in source)
    void updateSatelliteLEDs(const GPSState &gs);
    void updateAdfLEDs(bool adf1locked, bool adf2locked);
};

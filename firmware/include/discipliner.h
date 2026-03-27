#pragma once
#include <Arduino.h>
#include "mcp4725.h"

enum class DiscState {
    WARMUP,       // Waiting for GPS fix and warmup period
    ACQUIRING,    // Have GPS, accumulating phase error
    LOCKED,       // Disciplined and within threshold
    HOLDOVER,     // GPS lost, coasting on last correction
    FREERUN       // No GPS ever seen, free running
};

class Discipliner {
public:
    Discipliner(MCP4725 &dac);

    void begin();

    // Call once per 1PPS pulse, pass phase error in nanoseconds
    // Positive = OCXO running fast (needs EFC reduced)
    void update(int32_t phaseError_ns, bool gpsValid);

    // Advance warmup/GPS-tracking state every PPS without applying a PI
    // correction.  Call this each second when the accumulation window is
    // not yet complete so warmup counts real seconds, not averaging windows.
    void tickWarmup(bool gpsValid);

    DiscState state()       { return _state; }
    uint16_t  dacValue()    { return _dacValue; }
    int32_t   phaseError()  { return _lastError; }
    float     frequency()   { return _freqOffset_ppb; }
    uint32_t  lockSeconds() { return _lockMs / 1000; }
    float     pGain() const { return _pGain; }
    float     iGain() const { return _iGain; }

    // Enable/disable DAC usage (useful if no DAC attached)
    void setDACEnabled(bool en) { _dacEnabled = en; }
    // Reset the integral to the current DAC value — prevents windup after
    // any period where the loop was suspended (e.g. EFC cal).
    void resetIntegral() { _integral = (float)_dacValue; }
    // Freeze/unfreeze the PI loop (DAC writes still work, update() is a no-op)
    void setCalActive(bool active) { _calActive = active; }
    bool calActive() const { return _calActive; }
    // Allow external code to set DAC value immediately (uses same path as PI)
    void setDACValue(uint16_t val);
    bool setLoopGains(float pGain, float iGain);

private:
    MCP4725&  _dac;
    DiscState _state;
    uint16_t  _dacValue;
    float     _integral;
    int32_t   _lastError;
    float     _freqOffset_ppb;
    float     _pGain;
    float     _iGain;
    uint32_t  _warmupCount;
    uint32_t  _lockMs;        // milliseconds continuously within lock threshold
    uint32_t  _lockEnteredMs; // millis() when lock window started
    uint32_t  _holdoverSecs;
    uint32_t  _lastGPSsec;
    bool      _everHadGPS;
    bool      _dacEnabled;
    bool      _calActive;
    // EEPROM save state
    uint32_t  _lastSavedMs;
    uint16_t  _lastSavedValue;
    // EMA of abs error for lock detection (smooths GPS/counter noise)
    float     _lockErrEMA;
    uint32_t  _lastDacMotionMs;

    void applyDAC(uint16_t val);
};

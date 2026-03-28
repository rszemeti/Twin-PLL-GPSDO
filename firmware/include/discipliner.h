#pragma once
#include <Arduino.h>
#include "config.h"
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

    // Call once per averaging window with the averaged frequency error in ppb.
    // Positive = OCXO running fast (needs EFC reduced)
    // avgWindow: number of seconds in the rolling average; I gain is divided
    //            by this so the per-second call produces the same total
    //            correction as the old once-per-window call.
    void update(int32_t freqError_ppb, bool gpsValid, uint32_t avgWindow = 1);

    // Advance warmup/GPS-tracking state every PPS without applying a PI
    // correction.  Call this each second when the accumulation window is
    // not yet complete so warmup counts real seconds, not averaging windows.
    void tickWarmup(bool gpsValid);

    // Snapshot the current DAC value into the lock-detection ring buffer.
    // Call this every second from main, independent of the averaging
    // window used by update().
    void feedLockSample();

    DiscState state()       { return _state; }
    uint16_t  dacValue()    { return _dacValue; }
    uint16_t  lastSavedDAC() { return _lastSavedValue; }
    int32_t   freqError()   { return _lastFreqError; }
    float     frequency()   { return _freqOffset_ppb; }
    uint32_t  lockSeconds() { return _lockSecs; }
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
    int32_t   _lastFreqError;
    float     _freqOffset_ppb;
    float     _pGain;
    float     _iGain;
    uint32_t  _warmupCount;
    uint32_t  _lockSecs;       // seconds continuously locked
    uint32_t  _holdoverSecs;
    uint32_t  _lastGPSsec;
    bool      _everHadGPS;
    bool      _dacEnabled;
    bool      _calActive;
    // EEPROM save state
    uint32_t  _lastSavedMs;
    uint16_t  _lastSavedValue;
    // Lock detection ring buffer (per-second DAC snapshots)
    uint16_t  _lockBuf[DISC_LOCK_BUF_SIZE];
    uint16_t  _lockBufIdx;     // next write position
    uint16_t  _lockBufCount;   // samples written (saturates at BUF_SIZE)

    void applyDAC(uint16_t val);
    void evaluateLock();
};

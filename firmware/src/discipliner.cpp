#include "discipliner.h"
#include "config.h"
#include <EEPROM.h>
#include <math.h>

Discipliner::Discipliner(MCP4725 &dac)
    : _dac(dac),
      _state(DiscState::WARMUP),
      _dacValue(DAC_CENTRE),
      _integral(DAC_CENTRE),
      _lastError(0),
      _freqOffset_ppb(0.0f),
    _pGain(DISC_P_GAIN),
    _iGain(DISC_I_GAIN),
      _calActive(false),
      _warmupCount(0),
      _lockMs(0),
      _lockEnteredMs(0),
      _holdoverSecs(0),
      _lastGPSsec(0),
            _everHadGPS(false), _lastSavedMs(0), _lastSavedValue(DAC_CENTRE),
        _dacEnabled(true), _lockErrEMA(0.0f), _lastDacMotionMs(0) {}

void Discipliner::begin() {
    // Load saved "unlocked" DAC value from EEPROM if present
    uint16_t saved = DAC_CENTRE;
    // EEPROM emulation on RP2040 core should allow get/put
    EEPROM.get(DAC_EEPROM_ADDR, saved);
    if (saved >= DAC_MIN && saved <= DAC_MAX) {
        _dacValue = saved;
    } else {
        _dacValue = DAC_CENTRE;
    }
    _integral = (float)_dacValue;
    _lastSavedValue = _dacValue;
    _lastSavedMs = millis();
    _lastDacMotionMs = _lastSavedMs;
    applyDAC(_dacValue);
}

void Discipliner::tickWarmup(bool gpsValid) {
    if (gpsValid) {
        _lastGPSsec  = millis() / 1000;
        _everHadGPS  = true;
        _holdoverSecs = 0;
    }
    if (_state == DiscState::FREERUN) {
        if (gpsValid) _state = DiscState::WARMUP;
        return;
    }
    if (_state == DiscState::WARMUP) {
        if (!_everHadGPS) { _state = DiscState::FREERUN; return; }
        if (!gpsValid) return;
        _warmupCount++;
        if (_warmupCount >= DISC_WARMUP_SECS) {
            _state = DiscState::ACQUIRING;
            // Preserve the restored/current DAC operating point across restart.
            _integral = (float)_dacValue;
        }
    }
}

void Discipliner::update(int32_t phaseError_ns, bool gpsValid) {
    if (_calActive) return;  // loop suspended during cal

    if (gpsValid) {
        _lastGPSsec  = millis() / 1000;
        _everHadGPS  = true;
        _holdoverSecs = 0;
    } else {
        _holdoverSecs = (millis() / 1000) - _lastGPSsec;
    }

    // State machine
    switch (_state) {

        case DiscState::FREERUN:
            return;   // tickWarmup() handles FREERUN→WARMUP

        case DiscState::WARMUP:
            return;   // tickWarmup() handles warmup countdown;

        case DiscState::ACQUIRING:
        case DiscState::LOCKED:
            if (!gpsValid) {
                _state = DiscState::HOLDOVER;
                return;
            }
            break;

        case DiscState::HOLDOVER:
            if (gpsValid) {
                _state = DiscState::LOCKED;
            }
            // Keep last DAC value, don't update
            return;
    }

    _lastError = phaseError_ns;
    int32_t absErr = abs(phaseError_ns);

    // Update EMA of absolute error for lock detection — smooths GPS/counter noise
    _lockErrEMA = DISC_LOCK_EMA_ALPHA * (float)absErr
                  + (1.0f - DISC_LOCK_EMA_ALPHA) * _lockErrEMA;

    // Reduce gain when locked to narrow bandwidth and reduce jitter
    float effectiveI = (_state == DiscState::LOCKED)
                       ? _iGain * DISC_I_GAIN_LOCKED_RATIO
                       : _iGain;

    uint16_t prevDacValue = _dacValue;

    // Negative feedback: positive error (OCXO fast) must reduce DAC/integral
    _integral -= effectiveI * (float)phaseError_ns;

    // Clamp integral
    if (_integral > DAC_MAX) _integral = DAC_MAX;
    if (_integral < DAC_MIN) _integral = DAC_MIN;

    _dacValue = (uint16_t)_integral;

    _freqOffset_ppb = ((float)_dacValue - DAC_CENTRE) * 0.1f;

    uint32_t now = millis();
    if ((uint16_t)abs((int)_dacValue - (int)prevDacValue) >= DISC_LOCK_DAC_STEP_THRESHOLD) {
        _lastDacMotionMs = now;
    }

    // Lock detection — use smoothed EMA error plus hysteresis so occasional
    // noisy windows do not prevent lock or cause rapid lock/unlock chatter.
    if (_state == DiscState::LOCKED) {
        if (_lockErrEMA > DISC_LOCK_EXIT_THRESHOLD_NS) {
            _lockMs = 0;
            _lockEnteredMs = 0;
            _state = DiscState::ACQUIRING;
        } else if (_lockEnteredMs != 0) {
            _lockMs = now - _lockEnteredMs;
        }
    } else {
        bool dacSettled = (now - _lastDacMotionMs) >= DISC_LOCK_DAC_SETTLE_MS;
        if (dacSettled && _lockErrEMA < DISC_LOCK_ENTER_THRESHOLD_NS) {
            if (_lockEnteredMs == 0) _lockEnteredMs = now;
            _lockMs = now - _lockEnteredMs;
            if (_state == DiscState::ACQUIRING && _lockMs >= DISC_LOCK_MIN_MS)
                _state = DiscState::LOCKED;
        } else {
            _lockMs = 0;
            _lockEnteredMs = 0;
        }
    }

    applyDAC(_dacValue);

    // Occasionally save DAC as "unlocked" reference when it changes
    uint32_t saveNow = millis();
    uint32_t interval_ms = (uint32_t)DAC_SAVE_INTERVAL_SECS * 1000UL;
    if ((uint16_t)abs((int)_dacValue - (int)_lastSavedValue) >= DAC_SAVE_HYSTERESIS
        && (saveNow - _lastSavedMs) >= interval_ms) {
        EEPROM.put(DAC_EEPROM_ADDR, _dacValue);
        EEPROM.commit();
        _lastSavedValue = _dacValue;
        _lastSavedMs = saveNow;
    }
}

void Discipliner::applyDAC(uint16_t val) {
    _dacValue = val;
    if (_dacEnabled) {
#if USE_PWM_DAC
        analogWrite(PWM_DAC_PIN, val);
#else
        _dac.setVoltage(val);
#endif
    }
}

void Discipliner::setDACValue(uint16_t val) {
    _dacValue = val;
    _integral = (float)val;
    applyDAC(val);
}

bool Discipliner::setLoopGains(float pGain, float iGain) {
    if (!isfinite(pGain) || !isfinite(iGain)) {
        return false;
    }
    if (pGain < DISC_P_GAIN_MIN || pGain > DISC_P_GAIN_MAX) {
        return false;
    }
    if (iGain < DISC_I_GAIN_MIN || iGain > DISC_I_GAIN_MAX) {
        return false;
    }
    _pGain = pGain;
    _iGain = iGain;
    return true;
}

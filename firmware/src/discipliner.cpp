#include "discipliner.h"
#include "config.h"
#include <EEPROM.h>
#include <math.h>

Discipliner::Discipliner(MCP4725 &dac)
    : _dac(dac),
      _state(DiscState::WARMUP),
      _dacValue(DAC_CENTRE),
      _integral(DAC_CENTRE),
      _lastFreqError(0),
      _freqOffset_ppb(0.0f),
    _pGain(DISC_P_GAIN),
    _iGain(DISC_I_GAIN),
      _calActive(false),
      _warmupSecs(DISC_WARMUP_SECS),
      _warmupCount(0),
      _lockSecs(0),
      _holdoverSecs(0),
      _lastGPSsec(0),
            _everHadGPS(false), _lastSavedMs(0), _lastSavedValue(DAC_CENTRE),
        _dacEnabled(true), _lockBufIdx(0), _lockBufCount(0) {
    memset(_lockBuf, 0, sizeof(_lockBuf));
}

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
        if (_warmupCount >= _warmupSecs) {
            _state = DiscState::ACQUIRING;
            // Preserve the restored/current DAC operating point across restart.
            _integral = (float)_dacValue;
        }
    }
}

void Discipliner::update(int32_t freqError_ppb, bool gpsValid, uint32_t avgWindow) {
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

    _lastFreqError = freqError_ppb;

    // Reduce gain when locked to narrow bandwidth and reduce jitter
    float effectiveI = (_state == DiscState::LOCKED)
                       ? _iGain * DISC_I_GAIN_LOCKED_RATIO
                       : _iGain;

    // Scale I gain by 1/avgWindow so per-second calls produce the same
    // total correction as the old once-per-window call.
    if (avgWindow > 1) effectiveI /= (float)avgWindow;

    // Negative feedback: positive error (OCXO fast) must reduce DAC/integral
    _integral -= effectiveI * (float)freqError_ppb;

    // Clamp integral
    if (_integral > DAC_MAX) _integral = DAC_MAX;
    if (_integral < DAC_MIN) _integral = DAC_MIN;

    // P term: instantaneous correction on top of the integral
    float pCorrection = _pGain * (float)freqError_ppb;
    float dacOut = _integral - pCorrection;

    // Clamp final output
    if (dacOut > DAC_MAX) dacOut = DAC_MAX;
    if (dacOut < DAC_MIN) dacOut = DAC_MIN;

    _dacValue = (uint16_t)dacOut;

    _freqOffset_ppb = ((float)_dacValue - DAC_CENTRE) * 0.1f;

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

void Discipliner::feedLockSample() {
    // Only collect DAC samples while actively disciplining.
    // During WARMUP / FREERUN / HOLDOVER the DAC is static and would
    // fill the buffer with a constant, giving a false "stable" reading.
    if (_state != DiscState::ACQUIRING && _state != DiscState::LOCKED)
        return;

    // Snapshot current DAC value into ring buffer
    _lockBuf[_lockBufIdx] = _dacValue;
    _lockBufIdx = (_lockBufIdx + 1) % DISC_LOCK_BUF_SIZE;
    if (_lockBufCount < DISC_LOCK_BUF_SIZE) _lockBufCount++;

    evaluateLock();

    if (_state == DiscState::LOCKED) {
        _lockSecs++;
    } else {
        _lockSecs = 0;
    }
}

void Discipliner::evaluateLock() {
    // Need a full buffer before making any lock decision
    if (_lockBufCount < DISC_LOCK_BUF_SIZE) return;

    // Find min and max DAC value over the buffer window
    uint16_t minDac = _lockBuf[0];
    uint16_t maxDac = _lockBuf[0];
    for (uint16_t i = 1; i < DISC_LOCK_BUF_SIZE; i++) {
        if (_lockBuf[i] < minDac) minDac = _lockBuf[i];
        if (_lockBuf[i] > maxDac) maxDac = _lockBuf[i];
    }
    uint16_t dacRange = maxDac - minDac;
    bool railed = (_dacValue <= DAC_MIN || _dacValue >= DAC_MAX);

    if (_state == DiscState::LOCKED) {
        // Drop lock if DAC is moving too much or has railed
        if (dacRange > DISC_LOCK_DAC_RANGE_EXIT || railed) {
            _state = DiscState::ACQUIRING;
            // Reset buffer so re-lock requires a full window of stability
            _lockBufIdx = 0;
            _lockBufCount = 0;
        }
    } else {
        // Enter lock when DAC is stable and not railed
        if (dacRange <= DISC_LOCK_DAC_RANGE_ENTER && !railed) {
            _state = DiscState::LOCKED;
        }
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

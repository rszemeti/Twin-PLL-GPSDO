#include "discipliner.h"
#include "config.h"
#include <EEPROM.h>

Discipliner::Discipliner(MCP4725 &dac)
    : _dac(dac),
      _state(DiscState::WARMUP),
      _dacValue(DAC_CENTRE),
      _integral(DAC_CENTRE),
      _lastError(0),
      _freqOffset_ppb(0.0f),
      _warmupCount(0),
      _lockSecs(0),
      _holdoverSecs(0),
      _lastGPSsec(0),
            _everHadGPS(false), _lastSavedMs(0), _lastSavedValue(DAC_CENTRE),
            _dacEnabled(true) {}

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
    _lastSavedValue = _dacValue;
    _lastSavedMs = millis();
    applyDAC(_dacValue);
}

void Discipliner::update(int32_t phaseError_ns, bool gpsValid) {

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
            if (gpsValid) _state = DiscState::WARMUP;
            return;

        case DiscState::WARMUP:
            if (!_everHadGPS) {
                _state = DiscState::FREERUN;
                return;
            }
            if (!gpsValid) return;
            _warmupCount++;
            if (_warmupCount >= DISC_WARMUP_SECS) {
                _state = DiscState::ACQUIRING;
                _integral = DAC_CENTRE;
            }
            return;

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

    // PI controller
    // Phase error in ns → frequency correction in DAC counts
    // Positive error = OCXO fast → reduce EFC voltage → reduce DAC
    _lastError = phaseError_ns;

    float p = DISC_P_GAIN * (float)phaseError_ns;
    _integral += DISC_I_GAIN * (float)phaseError_ns;

    // Clamp integral
    if (_integral > DAC_MAX) _integral = DAC_MAX;
    if (_integral < DAC_MIN) _integral = DAC_MIN;

    float correction = _integral - p;
    if (correction > DAC_MAX) correction = DAC_MAX;
    if (correction < DAC_MIN) correction = DAC_MIN;

    _dacValue = (uint16_t)correction;

    // Frequency offset estimate in ppb
    // Assumes DAC_CENTRE = 0ppb, full range = OCXO EFC range
    // Adjust scaling to match your specific OCXO EFC sensitivity
    _freqOffset_ppb = ((float)_dacValue - DAC_CENTRE) * 0.1f;

    // Lock detection
    if (abs(phaseError_ns) < DISC_LOCK_THRESHOLD_NS) {
        _lockSecs++;
        if (_state == DiscState::ACQUIRING && _lockSecs > 10)
            _state = DiscState::LOCKED;
    } else {
        _lockSecs = 0;
        if (_state == DiscState::LOCKED)
            _state = DiscState::ACQUIRING;
    }

    applyDAC(_dacValue);

    // Occasionally save DAC as "unlocked" reference when it changes
    uint32_t now = millis();
    uint32_t interval_ms = (uint32_t)DAC_SAVE_INTERVAL_SECS * 1000UL;
    // Save only if the DAC changed by more than the hysteresis threshold
    // and the configured interval has elapsed.
    if ((uint16_t)abs((int)_dacValue - (int)_lastSavedValue) >= DAC_SAVE_HYSTERESIS
        && (now - _lastSavedMs) >= interval_ms) {
        EEPROM.put(DAC_EEPROM_ADDR, _dacValue);
        _lastSavedValue = _dacValue;
        _lastSavedMs = now;
    }
}

void Discipliner::applyDAC(uint16_t val) {
    _dacValue = val;
    if (_dacEnabled) {
        _dac.setVoltage(val);
    }
}

void Discipliner::setDACValue(uint16_t val) {
    applyDAC(val);
}

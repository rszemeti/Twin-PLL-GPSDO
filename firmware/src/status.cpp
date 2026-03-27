#include "status.h"
#include "config.h"
#include <ArduinoJson.h>

StatusManager::StatusManager(Discipliner &disc, GPSParser &gps,
                             ADF4351 &adf1, ADF4351 &adf2)
    : _disc(disc), _gps(gps), _adf1(adf1), _adf2(adf2),
      _lastPrint(0), _adf1LostMs(0), _adf2LostMs(0),
        _alarmActiveSteady(false), _alarmActiveFlash(false), _alarmFlashOn(false), _lastAlarmFlashMs(0),
        _satsBlinkOn(false), _lastSatsBlinkMs(0),
                _adf1BlinkOn(false), _adf2BlinkOn(false), _lastAdfBlinkMs(0), _discAvgWindowSecs(DISC_AVERAGE_SECS),
                                _statusIntervalMs(5000), _measuredFreqHz(0.0), _measuredFreqErrorPpb(0.0) {}

void StatusManager::begin() {
    pinMode(LED_GPS_LOCK,    OUTPUT);
    pinMode(LED_DISCIPLINED, OUTPUT);
    pinMode(LED_ADF1_LOCK,   OUTPUT);
    pinMode(LED_ADF2_LOCK,   OUTPUT);
    pinMode(LED_ALARM,       OUTPUT);
    pinMode(ALARM_PIN,       OUTPUT);
    pinMode(LED_SATS_BLINK,  OUTPUT);
    pinMode(LED_SATS_USED,   OUTPUT);
    pinMode(LED_ADF1_LOCK,   OUTPUT);
    pinMode(LED_ADF2_LOCK,   OUTPUT);

    // All off at start
    setLED(LED_GPS_LOCK,    false);
    setLED(LED_DISCIPLINED, false);
    setLED(LED_ADF1_LOCK,   false);
    setLED(LED_ADF2_LOCK,   false);
    setLED(LED_ALARM,       false);
    setAlarmSteady(false);
    setAlarmFlash(false);
    setLED(LED_SATS_BLINK, false);
    setLED(LED_SATS_USED,  false);
    setLED(LED_ADF1_LOCK,  false);
    setLED(LED_ADF2_LOCK,  false);
}

void StatusManager::setLED(uint8_t pin, bool on) {
    digitalWrite(pin, on ? HIGH : LOW);
}

void StatusManager::setAlarmSteady(bool on) {
    _alarmActiveSteady = on;
    if (on) {
        digitalWrite(ALARM_PIN, LOW); // active low
        setLED(LED_ALARM, true);
    } else {
        // if flash mode is not active, turn alarm output off
        if (!_alarmActiveFlash) {
            digitalWrite(ALARM_PIN, HIGH);
            setLED(LED_ALARM, false);
        }
    }
}

void StatusManager::setAlarmFlash(bool on) {
    _alarmActiveFlash = on;
    if (!on) {
        // stop flashing; if steady not set, ensure alarm is off
        if (!_alarmActiveSteady) {
            digitalWrite(ALARM_PIN, HIGH);
            setLED(LED_ALARM, false);
        }
        _alarmFlashOn = false;
    } else {
        // start flashing immediately (unless steady active)
        if (!_alarmActiveSteady) {
            _alarmFlashOn = true;
            digitalWrite(ALARM_PIN, LOW);
            setLED(LED_ALARM, true);
        }
        _lastAlarmFlashMs = millis();
    }
}

void StatusManager::update() {
    const GPSState& gs = _gps.state();

    // GPS lock LED
    setLED(LED_GPS_LOCK, gs.hasFix && gs.ppsValid);

    // Disciplined LED
    setLED(LED_DISCIPLINED,
        _disc.state() == DiscState::LOCKED ||
        _disc.state() == DiscState::HOLDOVER);

    // ADF4351 lock detect with timeout
    bool adf1locked = _adf1.isLocked();
    bool adf2locked = _adf2.isLocked();
    // Update ADF LEDs (solid when locked, blink when unlocked)
    updateAdfLEDs(adf1locked, adf2locked);

    uint32_t now = millis();

    if (!adf1locked) {
        if (_adf1LostMs == 0) _adf1LostMs = now;
    } else {
        _adf1LostMs = 0;
    }

    if (!adf2locked) {
        if (_adf2LostMs == 0) _adf2LostMs = now;
    } else {
        _adf2LostMs = 0;
    }

    bool adf1alarm = _adf1LostMs &&
        (now - _adf1LostMs) > (ALARM_LOCK_TIMEOUT * 1000UL);
    bool adf2alarm = _adf2LostMs &&
        (now - _adf2LostMs) > (ALARM_LOCK_TIMEOUT * 1000UL);
    bool gpsAlarm  = gs.ppsValid == false &&
        _disc.state() != DiscState::FREERUN;

    // Use flashing alarm for runtime health issues (out-of-lock). Steady
    // alarm is reserved for init-time hardware failures.
    bool flashAlarm = adf1alarm || adf2alarm || gpsAlarm;
    setAlarmFlash(flashAlarm);

    // Handle flashing alarm toggling (only when steady alarm not active)
    const uint32_t alarmBlinkInterval = 500; // ms
    if (_alarmActiveSteady) {
        // steady alarm already set by setAlarmSteady(); ensure LED/output reflect it
        digitalWrite(ALARM_PIN, LOW);
        setLED(LED_ALARM, true);
    } else if (_alarmActiveFlash) {
        if (now - _lastAlarmFlashMs >= alarmBlinkInterval) {
            _lastAlarmFlashMs = now;
            _alarmFlashOn = !_alarmFlashOn;
            digitalWrite(ALARM_PIN, _alarmFlashOn ? LOW : HIGH);
            setLED(LED_ALARM, _alarmFlashOn);
        }
    } else {
        // no alarm
        digitalWrite(ALARM_PIN, HIGH);
        setLED(LED_ALARM, false);
    }

    // Debug print every 5 seconds
    if (now - _lastPrint >= _statusIntervalMs) {
        _lastPrint = now;
        printDebug();
    }

    // Update satellite LEDs (blink + sats-used)
    updateSatelliteLEDs(gs);

    // (ADF LEDs handled above)
}

void StatusManager::updateAdfLEDs(bool adf1locked, bool adf2locked) {
    const uint32_t now = millis();
    const uint32_t blinkInterval = 500; // ms

    if (adf1locked) {
        _adf1BlinkOn = false;
        setLED(LED_ADF1_LOCK, true);
    } else {
        if (now - _lastAdfBlinkMs >= blinkInterval) {
            _lastAdfBlinkMs = now;
            _adf1BlinkOn = !_adf1BlinkOn;
        }
        setLED(LED_ADF1_LOCK, _adf1BlinkOn);
    }

    if (adf2locked) {
        _adf2BlinkOn = false;
        setLED(LED_ADF2_LOCK, true);
    } else {
        if (now - _lastAdfBlinkMs >= blinkInterval) {
            _lastAdfBlinkMs = now;
            _adf2BlinkOn = !_adf2BlinkOn;
        }
        setLED(LED_ADF2_LOCK, _adf2BlinkOn);
    }
}

void StatusManager::updateSatelliteLEDs(const GPSState &gs) {
    // Blink LED_SATS_BLINK at 0.5s intervals when any satellites are reported
    uint32_t now = millis();
    if (gs.satellites > 0) {
        if (now - _lastSatsBlinkMs >= 500) {
            _lastSatsBlinkMs = now;
            _satsBlinkOn = !_satsBlinkOn;
            setLED(LED_SATS_BLINK, _satsBlinkOn);
        }
    } else {
        // No sats: keep LED off
        _satsBlinkOn = false;
        setLED(LED_SATS_BLINK, false);
    }

    // LED_SATS_USED: solid on when satellites in use >= 4 (typical usable fix)
    setLED(LED_SATS_USED, gs.satellites >= 4 && gs.hasFix);
}

void StatusManager::printDebug() {
    const GPSState& gs = _gps.state();
    // If JSON mode enabled, print a compact single-line JSON object suitable
    // for machine parsing. Otherwise, print legacy human-readable debug.
#if JSON_OUTPUT
    // Use ArduinoJson to produce robust JSON output
    StaticJsonDocument<512> doc;
    doc["gps_fix"] = gs.hasFix;
    doc["gps_pps"] = gs.ppsValid;
    doc["sats"] = gs.satellites;
    doc["sats_used"] = gs.satellitesUsed;
    doc["sats_in_view"] = gs.satellitesInView;
    doc["hdop"] = gs.hdop;
    doc["time"] = gs.timeStr;
    doc["date"] = gs.dateStr;
    doc["pps_count"] = gs.ppsCount;
    switch (_disc.state()) {
        case DiscState::WARMUP:    doc["disc_state"] = "WARMUP"; break;
        case DiscState::ACQUIRING: doc["disc_state"] = "ACQUIRING"; break;
        case DiscState::LOCKED:    doc["disc_state"] = "LOCKED"; break;
        case DiscState::HOLDOVER:  doc["disc_state"] = "HOLDOVER"; break;
        case DiscState::FREERUN:   doc["disc_state"] = "FREERUN"; break;
    }
    doc["phase_error_ns"] = _disc.phaseError();
    doc["disc_avg_window_s"] = _discAvgWindowSecs;
    doc["disc_avg_phase_ns"] = _disc.phaseError();
    doc["disc_p_gain"] = _disc.pGain();
    doc["disc_i_gain"] = _disc.iGain();
    doc["status_interval_ms"] = _statusIntervalMs;
    doc["dac_value"] = _disc.dacValue();
    doc["freq_ppb"] = _disc.frequency();
    doc["measured_freq_hz"] = _measuredFreqHz;
    doc["measured_freq_error_ppb"] = _measuredFreqErrorPpb;
    doc["adf1_locked"] = _adf1.isLocked();
    doc["adf2_locked"] = _adf2.isLocked();
    doc["alarm_steady"] = _alarmActiveSteady;
    doc["alarm_flash"] = _alarmActiveFlash;
    serializeJson(doc, Serial);
    Serial.println();
#else
    Serial.println("=============================");
    Serial.print("GPS fix: ");    Serial.println(gs.hasFix    ? "YES" : "NO");
    Serial.print("GPS 1PPS: ");   Serial.println(gs.ppsValid  ? "YES" : "NO");
    Serial.print("Sats: ");       Serial.println(gs.satellites);
    Serial.print("Time: ");       Serial.println(gs.timeStr);
    Serial.print("Date: ");       Serial.println(gs.dateStr);
    Serial.print("PPS count: ");  Serial.println(gs.ppsCount);

    Serial.print("Disc state: ");
    switch (_disc.state()) {
        case DiscState::WARMUP:    Serial.println("WARMUP");    break;
        case DiscState::ACQUIRING: Serial.println("ACQUIRING"); break;
        case DiscState::LOCKED:    Serial.println("LOCKED");    break;
        case DiscState::HOLDOVER:  Serial.println("HOLDOVER");  break;
        case DiscState::FREERUN:   Serial.println("FREERUN");   break;
    }

    Serial.print("Phase error: "); Serial.print(_disc.phaseError());
    Serial.println(" ns");
    Serial.print("DAC value: ");   Serial.println(_disc.dacValue());
    Serial.print("Freq offset: "); Serial.print(_disc.frequency(), 3);
    Serial.println(" ppb");
    Serial.print("Measured OCXO: "); Serial.print(_measuredFreqHz, 6);
    Serial.println(" Hz");
    Serial.print("Measured freq error: "); Serial.print(_measuredFreqErrorPpb, 3);
    Serial.println(" ppb");

    Serial.print("ADF1 (104MHz) locked: ");
    Serial.println(_adf1.isLocked() ? "YES" : "NO");
    Serial.print("ADF2 (116MHz) locked: ");
    Serial.println(_adf2.isLocked() ? "YES" : "NO");

    Serial.print("Alarm (steady): ");
    Serial.println(_alarmActiveSteady ? "YES" : "NO");
    Serial.print("Alarm (flash): ");
    Serial.println(_alarmActiveFlash ? "YES" : "NO");
#endif
}

// (no additional update() - behavior handled above)

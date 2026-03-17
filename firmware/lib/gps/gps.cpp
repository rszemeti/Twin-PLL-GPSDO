#include "gps.h"
#include <string.h>
#include <stdlib.h>

GPSParser::GPSParser(HardwareSerial &serial)
    : _serial(serial), _idx(0) {
    memset(&_state, 0, sizeof(_state));
}

void GPSParser::begin(uint32_t baud) {
    _serial.begin(baud);
}

// Called from PIO IRQ handler on each 1PPS rising edge
void GPSParser::notifyPPS() {
    _state.ppsValid  = true;
    _state.lastPPSms = millis();
    _state.ppsCount++;
}

void GPSParser::update() {
    while (_serial.available()) {
        char c = _serial.read();
        if (c == '\n') {
            _buf[_idx] = 0;
            parseLine();
            _idx = 0;
        } else if (c != '\r' && _idx < sizeof(_buf) - 1) {
            _buf[_idx++] = c;
        }
    }

    // Invalidate 1PPS if no pulse seen for >2 seconds
    if (_state.ppsValid && (millis() - _state.lastPPSms) > 2000) {
        _state.ppsValid = false;
    }
}

bool GPSParser::checksum(const char *s) {
    if (s[0] != '$') return false;
    uint8_t calc = 0;
    int i = 1;
    while (s[i] && s[i] != '*') calc ^= s[i++];
    if (s[i] != '*') return false;
    uint8_t got = strtoul(&s[i+1], nullptr, 16);
    return calc == got;
}

void GPSParser::parseLine() {
    if (!checksum(_buf)) return;
    if (strncmp(_buf, "$GPRMC", 6) == 0 ||
        strncmp(_buf, "$GNRMC", 6) == 0)
        parseGPRMC(_buf);
    else if (strncmp(_buf, "$GPGGA", 6) == 0 ||
             strncmp(_buf, "$GNGGA", 6) == 0)
        parseGPGGA(_buf);
    else if (strncmp(_buf, "$GPGSV", 6) == 0 ||
             strncmp(_buf, "$GNGSV", 6) == 0)
        parseGPGSV(_buf);
}

// Minimal GPRMC parser - extracts fix status, time, date
void GPSParser::parseGPRMC(const char *s) {
    // $GPRMC,hhmmss,A/V,lat,N/S,lon,E/W,spd,crs,ddmmyy,,,*cs
    char buf[128];
    strncpy(buf, s, sizeof(buf)-1);
    char *tok = strtok(buf, ",");
    int field = 0;
    while (tok) {
        switch(field) {
            case 1: strncpy(_state.timeStr, tok, sizeof(_state.timeStr)-1); break;
            case 2: _state.hasFix = (tok[0] == 'A'); break;
            case 9: strncpy(_state.dateStr, tok, sizeof(_state.dateStr)-1); break;
        }
        tok = strtok(nullptr, ",");
        field++;
    }
}

// Minimal GPGGA parser - extracts satellites used and HDOP
void GPSParser::parseGPGGA(const char *s) {
    // $GPGGA,time,lat,N,lon,E,fix,sats,...
    char buf[128];
    strncpy(buf, s, sizeof(buf)-1);
    buf[sizeof(buf)-1] = 0;
    char *tok = strtok(buf, ",");
    int field = 0;
    while (tok) {
        switch(field) {
            case 7:
                _state.satellitesUsed = (uint8_t)atoi(tok);
                _state.satellites = _state.satellitesUsed;
                break;
            case 8:
                _state.hdop = (float)atof(tok);
                break;
        }
        tok = strtok(nullptr, ",");
        field++;
    }
}

// Minimal GPGSV parser - extracts satellites in view
void GPSParser::parseGPGSV(const char *s) {
    // $GPGSV,total_msgs,msg_num,sats_in_view,...
    char buf[128];
    strncpy(buf, s, sizeof(buf)-1);
    buf[sizeof(buf)-1] = 0;
    char *tok = strtok(buf, ",");
    int field = 0;
    while (tok) {
        if (field == 3) {
            _state.satellitesInView = (uint8_t)atoi(tok);
            break;
        }
        tok = strtok(nullptr, ",");
        field++;
    }
}

const GPSState& GPSParser::state() {
    return _state;
}

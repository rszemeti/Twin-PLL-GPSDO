#pragma once
#include <Arduino.h>

struct GPSState {
    bool    hasFix;
    bool    ppsValid;
    uint8_t satellites;        // legacy alias: satellites used
    uint8_t satellitesUsed;    // satellites used in solution
    uint8_t satellitesInView;  // satellites in view (from GSV)
    float   hdop;              // horizontal dilution of precision
    int32_t latRaw;      // degrees * 1e7
    int32_t lonRaw;      // degrees * 1e7
    uint32_t lastPPSms;  // millis() of last 1PPS
    uint32_t ppsCount;   // total 1PPS edges seen
    char     timeStr[12];
    char     dateStr[8];
};

class GPSParser {
public:
    GPSParser(HardwareSerial &serial);
    void begin(uint32_t baud);
    void update();           // call from loop, parses incoming NMEA
    void notifyPPS();        // call from PIO IRQ on each 1PPS edge
    const GPSState& state();

private:
    HardwareSerial& _serial;
    GPSState        _state;
    char            _buf[128];
    uint8_t         _idx;

    void parseLine();
    void parseGPRMC(const char *s);
    void parseGPGGA(const char *s);
    void parseGPGSV(const char *s);
    bool checksum(const char *s);
};

// Note: 1PPS edge capture is handled by PIOTimingEngine.
// GPSParser handles NMEA parsing only.
// Call gps.notifyPPS() from the PIO IRQ handler to update
// the ppsValid flag and pulse counter.

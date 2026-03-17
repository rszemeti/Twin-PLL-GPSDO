#pragma once
#include <Arduino.h>
#include <Wire.h>

class MCP4725 {
public:
    MCP4725(uint8_t addr = 0x60);
    void begin();
    void setVoltage(uint16_t value);   // 0-4095
    uint16_t getValue();

private:
    uint8_t  _addr;
    uint16_t _value;
};

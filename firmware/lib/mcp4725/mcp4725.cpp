#include "mcp4725.h"

MCP4725::MCP4725(uint8_t addr) : _addr(addr), _value(2048) {}

void MCP4725::begin() {
    Wire.begin();
    Serial.printf("MCP4725::begin addr=0x%02X\n", _addr);
    setVoltage(_value);
}

void MCP4725::setVoltage(uint16_t value) {
    if (value > 4095) value = 4095;
    _value = value;
    Wire.beginTransmission(_addr);
    Wire.write(0x40);                    // write DAC, no EEPROM
    Wire.write((_value >> 4) & 0xFF);   // upper 8 bits
    Wire.write((_value & 0x0F) << 4);   // lower 4 bits
    int ret = Wire.endTransmission();
    if (ret != 0) {
        Serial.printf("MCP4725: I2C write failed addr=0x%02X code=%d\n", _addr, ret);
    } else {
        Serial.printf("MCP4725: setVoltage %u (0x%03X)\n", _value, _value);
    }
}

uint16_t MCP4725::getValue() {
    return _value;
}

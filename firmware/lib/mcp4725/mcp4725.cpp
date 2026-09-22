#include "mcp4725.h"
#include "../../include/config.h"

MCP4725::MCP4725(uint8_t addr) : _addr(addr), _value(2048) {}

void MCP4725::begin() {
    // I2C_SDA_PIN/I2C_SCL_PIN (14/15) are only valid pins for the RP2040/
    // RP2350's I2C1 block, not the default I2C0 (Wire) pins (GPIO4/5) — so
    // this must run on Wire1, remapped to the actual hardware pins.
    Wire1.setSDA(I2C_SDA_PIN);
    Wire1.setSCL(I2C_SCL_PIN);
    Wire1.begin();
    Serial.printf("MCP4725::begin addr=0x%02X\n", _addr);
    setVoltage(_value);
}

void MCP4725::setVoltage(uint16_t value) {
    if (value > 4095) value = 4095;
    _value = value;
    Wire1.beginTransmission(_addr);
    Wire1.write(0x40);                    // write DAC, no EEPROM
    Wire1.write((_value >> 4) & 0xFF);   // upper 8 bits
    Wire1.write((_value & 0x0F) << 4);   // lower 4 bits
    int ret = Wire1.endTransmission();
    if (ret != 0) {
        Serial.printf("MCP4725: I2C write failed addr=0x%02X code=%d\n", _addr, ret);
    } else {
        Serial.printf("MCP4725: setVoltage %u (0x%03X)\n", _value, _value);
    }
}

uint16_t MCP4725::getValue() {
    return _value;
}

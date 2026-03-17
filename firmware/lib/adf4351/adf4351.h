#pragma once
#include <Arduino.h>

class ADF4351 {
public:
    ADF4351(uint8_t clk, uint8_t mosi, uint8_t le, uint8_t ce, uint8_t ld);
    void begin();
    void program(const uint32_t regs[6]);
    bool isLocked();
    void enable(bool on);
    void writeReg(uint32_t reg);

private:
    uint8_t _clk, _mosi, _le, _ce, _ld;
};

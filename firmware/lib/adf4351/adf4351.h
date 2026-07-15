#pragma once
#include <Arduino.h>

class ADF4351 {
public:
    ADF4351(uint8_t le, uint8_t ce, uint8_t ld);
    void begin();
    void program(const uint32_t regs[6]);
    bool isLocked();
    void enable(bool on);
    void writeReg(uint32_t reg);

private:
    static void beginSharedSPI();

    uint8_t _le, _ce, _ld;
};

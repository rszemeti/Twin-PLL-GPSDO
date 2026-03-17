#include "adf4351.h"

ADF4351::ADF4351(uint8_t clk, uint8_t mosi, uint8_t le, uint8_t ce, uint8_t ld)
    : _clk(clk), _mosi(mosi), _le(le), _ce(ce), _ld(ld) {}

void ADF4351::begin() {
    pinMode(_clk,  OUTPUT);
    pinMode(_mosi, OUTPUT);
    pinMode(_le,   OUTPUT);
    pinMode(_ce,   OUTPUT);
    pinMode(_ld,   INPUT_PULLDOWN);

    digitalWrite(_clk, LOW);
    digitalWrite(_le,  LOW);
    digitalWrite(_ce,  HIGH);  // enabled by default
}

void ADF4351::writeReg(uint32_t reg) {
    // MSB first, 32 bits
    for (int i = 31; i >= 0; i--) {
        digitalWrite(_mosi, (reg >> i) & 1 ? HIGH : LOW);
        digitalWrite(_clk, HIGH);
        delayMicroseconds(1);
        digitalWrite(_clk, LOW);
        delayMicroseconds(1);
    }
    // Latch
    digitalWrite(_le, HIGH);
    delayMicroseconds(1);
    digitalWrite(_le, LOW);
    delayMicroseconds(1);
}

void ADF4351::program(const uint32_t regs[6]) {
    // ADF4351 requires registers written R5 down to R0
    for (int i = 5; i >= 0; i--) {
        writeReg(regs[i]);
        delay(10);
    }
}

bool ADF4351::isLocked() {
    return digitalRead(_ld) == HIGH;
}

void ADF4351::enable(bool on) {
    digitalWrite(_ce, on ? HIGH : LOW);
}

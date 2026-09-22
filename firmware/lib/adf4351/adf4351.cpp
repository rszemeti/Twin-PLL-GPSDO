#include "adf4351.h"
#include "../../include/config.h"

namespace {
bool g_adfSpiReady = false;

// ADF_MOSI_PIN (GPIO8) is not a valid hardware SPI TX pin on either SPI0 or
// SPI1 for this core (only a valid SPI1 RX pin) — the board is wired that
// way and can't be rewired, so clock the data out manually instead.
// ADF4351 samples MOSI on the rising edge of SCLK, MSB first (mode 0).
void spiBitBangWrite(const uint8_t *bytes, size_t len) {
    for (size_t i = 0; i < len; i++) {
        uint8_t b = bytes[i];
        for (int bit = 7; bit >= 0; bit--) {
            digitalWrite(ADF_MOSI_PIN, (b >> bit) & 0x01);
            digitalWrite(ADF_SCK_PIN, HIGH);
            digitalWrite(ADF_SCK_PIN, LOW);
        }
    }
}
}

ADF4351::ADF4351(uint8_t le, uint8_t ce, uint8_t ld)
    : _le(le), _ce(ce), _ld(ld) {}

void ADF4351::beginSharedSPI() {
    if (g_adfSpiReady) {
        return;
    }

    pinMode(ADF_SCK_PIN, OUTPUT);
    pinMode(ADF_MOSI_PIN, OUTPUT);
    digitalWrite(ADF_SCK_PIN, LOW);
    digitalWrite(ADF_MOSI_PIN, LOW);
    g_adfSpiReady = true;
}

void ADF4351::begin() {
    beginSharedSPI();
    pinMode(_le,   OUTPUT);
    pinMode(_ce,   OUTPUT);
    pinMode(_ld,   INPUT_PULLDOWN);

    digitalWrite(_le,  LOW);
    digitalWrite(_ce,  HIGH);  // enabled by default
}

void ADF4351::writeReg(uint32_t reg) {
    const uint8_t bytes[4] = {
        static_cast<uint8_t>(reg >> 24),
        static_cast<uint8_t>(reg >> 16),
        static_cast<uint8_t>(reg >> 8),
        static_cast<uint8_t>(reg)
    };

    spiBitBangWrite(bytes, sizeof(bytes));

    digitalWrite(_le, HIGH);
    delayMicroseconds(1);
    digitalWrite(_le, LOW);
    delayMicroseconds(1);
}

void ADF4351::program(const uint32_t regs[6]) {
    // ADF4351 requires registers written R5 down to R0.
    for (int i = 5; i >= 0; i--) {
        writeReg(regs[i]);
        delay(10);
    }
    // R1 and R4 are double-buffered against R0 (confirmed against ADI's own
    // Linux IIO driver for this chip family, adf4350_sync_config): writing
    // R1/R4 doesn't take full effect for the VCO band-select calibration
    // until R0 is written again afterward. Since every call here writes
    // fresh R1/R4 values, always re-write R0 to flush that double buffer.
    writeReg(regs[0]);
    delay(10);
}

bool ADF4351::isLocked() {
    return digitalRead(_ld) == HIGH;
}

void ADF4351::enable(bool on) {
    digitalWrite(_ce, on ? HIGH : LOW);
}

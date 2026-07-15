#include "adf4351.h"
#include "../../include/config.h"
#include <SPI.h>

namespace {
bool g_adfSpiReady = false;
}

ADF4351::ADF4351(uint8_t le, uint8_t ce, uint8_t ld)
    : _le(le), _ce(ce), _ld(ld) {}

void ADF4351::beginSharedSPI() {
    if (g_adfSpiReady) {
        return;
    }

    ADF_SPI_PORT.setSCK(ADF_SCK_PIN);
    ADF_SPI_PORT.setTX(ADF_MOSI_PIN);
    ADF_SPI_PORT.begin();
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

    ADF_SPI_PORT.beginTransaction(SPISettings(ADF_SPI_HZ, MSBFIRST, SPI_MODE0));
    ADF_SPI_PORT.transfer(const_cast<uint8_t*>(bytes), sizeof(bytes));
    ADF_SPI_PORT.endTransaction();

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

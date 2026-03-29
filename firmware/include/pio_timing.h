#pragma once
#include <Arduino.h>
#include "hardware/pio.h"
#include "hardware/timer.h"
#include "hardware/clocks.h"

// ============================================================
// PIO-based precision timing engine
//
// Uses two PIO state machines:
//   SM0 - 1PPS edge capture    (GPIO_1PPS_PIN)
//   SM1 - OCXO frequency count (FREQ_COUNT_PIN)
//
// At 200MHz sysclk:
//   1PPS timestamp resolution: 5ns
//   Frequency measurement: ~0.5ppb per measurement
// ============================================================

struct TimingResult {
    // 1PPS phase measurement
    bool     ppsValid;           // true if fresh measurement
    int64_t  phaseError_ns;      // OCXO phase error vs GPS (ns)
                                 // +ve = OCXO running fast
    uint32_t ppsCycleCount;      // raw cycle count (1PPS interval)
    uint32_t ppsCount;           // total 1PPS edges seen

    // OCXO frequency measurement
    bool     freqValid;
    uint32_t freqPulseCount;     // number of 10MHz edges in the last PPS interval
    int32_t  freqError_Hz;       // raw count error (edges - nominal), exact integer
    double   measuredFreq_Hz;    // measured OCXO frequency
    double   freqError_ppb;      // error in parts per billion
    uint32_t freqCycleCount;     // PPS interval in microseconds used for freq calc
};

class PIOTimingEngine {
public:
    PIOTimingEngine(uint8_t ppsPin, uint8_t freqPin);

    // Call from setup() on Core1
    bool begin();

    // Call from Core1 loop - processes FIFO data
    void update();

    // Read results (safe to call from Core0 - uses atomic copy)
    TimingResult getResult();

    // Nominal sysclk frequency (set after clock_configure)
    void setSysclkHz(uint32_t hz) { _sysclkHz = hz; }

    // Nominal OCXO frequency
    void setOCXOFreq(uint32_t hz) { _ocxoHz = hz; }

private:
    uint8_t  _ppsPin;
    uint8_t  _freqPin;
    uint32_t _sysclkHz;
    uint32_t _ocxoHz;

    PIO      _pio;
    uint     _ppsSM;
    uint     _freqSM;

    // Raw timing state
    uint32_t _prevPPScycles;     // cycle count at last 1PPS
    uint32_t _ppsCount;
    bool     _firstPPS;

    // Shared result (written by Core1, read by Core0)
    // Use critical section for safe cross-core access
    TimingResult _result;
    volatile bool _resultReady;
    volatile bool _syncReady;

    bool initPPSsm();
    bool initFreqCounter();
public:
    // Called from the PPS IRQ handler on Core1.
    void processPPS(uint32_t ts_us);

private:
    // Frequency measurement via PIO edge counter (SM1).
    // x counts DOWN from 0xFFFFFFFF on every rising edge of the OCXO input.
    // At each 1PPS edge the CPU snapshots x via exec-injection.
    // delta = x_prev - x_now = edges counted in that second (~10,000,000).
    // error_Hz = delta - _ocxoHz;  error_ppb = error_Hz * (1e9 / _ocxoHz)
    uint     _freqSMOffset;  // offset of edge_counter program in PIO memory
    uint32_t _prevEdgeX;     // x value at previous PPS snapshot
    bool     _freqSeeded;    // true once we have a valid first snapshot
};

// Global instance accessible from both cores
extern PIOTimingEngine* g_timing;

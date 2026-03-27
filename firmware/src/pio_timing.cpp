#include "pio_timing.h"
#include "config.h"
#include "gps.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "hardware/pio_instructions.h"
#include "pico/critical_section.h"

// Forward declaration - GPS parser is instantiated in main.cpp
extern GPSParser gps;

// ============================================================
// SM0: 1PPS edge detector
// Waits for the rising edge of the 1PPS signal then fires
// PIO0_IRQ_0 so the CPU ISR can read timer_hw->timerawl.
//
// Verified encodings (WAIT: [15:13]=001, [7]=polarity, [6:5]=src 01=PIN):
//   wait 0 pin 0 = 001_00000_0_01_00000 = 0x2020
//   wait 1 pin 0 = 001_00000_1_01_00000 = 0x20A0
//   irq nowait set 0 = 110_00000_0_0_0_00000 = 0xC000
//
// wrap_target=0, wrap=2 (auto-loops after irq)
// ============================================================
static const uint16_t pps_sm_instructions[] = {
    0x2020,  // 0: wait 0 pin 0   (wait for low)   <- wrap_target
    0x20A0,  // 1: wait 1 pin 0   (rising edge)
    0xC000,  // 2: irq nowait set 0               <- wrap
};
static constexpr uint8_t PPS_SM_WRAP_TARGET = 0;
static constexpr uint8_t PPS_SM_WRAP        = 2;

// ============================================================
// SM1: OCXO edge counter
// x counts DOWN from 0xFFFFFFFF on every rising edge of pin 0
// (relative to SM IN base = _freqPin). No FIFO output - the
// CPU snapshots x by exec-injecting mov+push at each PPS edge.
//
//   mov x, ~null = 101_00000_01_001_011 = 0xA04B  (one-time init)
//   jmp x-- 1   = 000_00000_100_00001  = 0x0081  (decrement, loop to wrap_target)
//
// wrap_target=1 (skip the init after first run), wrap=3
// ============================================================
static const uint16_t edge_counter_instructions[] = {
    0xA02B,  // 0: mov x, ~null  (x = 0xFFFFFFFF, one-time init)
    0x2020,  // 1: wait 0 pin 0                  <- wrap_target
    0x20A0,  // 2: wait 1 pin 0  (rising edge)
    0x0041,  // 3: jmp x-- 1     (decrement x, loop to wrap_target)  <- wrap
};
static constexpr uint8_t EDGE_COUNTER_WRAP_TARGET = 1;
static constexpr uint8_t EDGE_COUNTER_WRAP        = 3;

// ============================================================
// Hardware timer for timestamping (1MHz, 64-bit)
// ============================================================
static inline uint32_t timer_us() {
    return timer_hw->timerawl;
}

// ============================================================
// Critical section for cross-core result sharing
// ============================================================
static critical_section_t _cs;

// Global instance
PIOTimingEngine* g_timing = nullptr;

// IRQ handlers - static so they can be function pointers
static PIOTimingEngine* _instance = nullptr;

static void pps_irq_handler() {
    // Read timer IMMEDIATELY on entry for best accuracy
    uint32_t ts = timer_us();
    if (_instance) {
        _instance->processPPS(ts);
    }
    // Notify GPS parser that a 1PPS edge arrived
    gps.notifyPPS();
    // Clear PIO IRQ flag
    pio_interrupt_clear(pio0, 0);
}

// ============================================================
// Constructor
// ============================================================
PIOTimingEngine::PIOTimingEngine(uint8_t ppsPin, uint8_t freqPin)
    : _ppsPin(ppsPin),
      _freqPin(freqPin),
      _sysclkHz(150000000),
      _ocxoHz(10000000),
      _pio(pio0),
      _ppsSM(0),
      _freqSM(1),
      _prevPPScycles(0),
      _ppsCount(0),
      _firstPPS(true),
      _resultReady(false),
      _syncReady(false),
      _freqSMOffset(0),
      _prevEdgeX(0),
      _freqSeeded(false) {
    memset(&_result, 0, sizeof(_result));
}

bool PIOTimingEngine::begin() {
    _instance = this;
    critical_section_init(&_cs);
        _syncReady = true;

    if (!initPPSsm())  return false;
#if USE_FREQ_COUNTER
    if (!initFreqCounter()) return false;
#else
    // Frequency SM disabled by config - leave it uninitialised to avoid
    // spurious/high-rate IRQs when no 10MHz reference is present.
#endif

    return true;
}

bool PIOTimingEngine::initPPSsm() {
    pio_program_t prog = {};
    prog.instructions = pps_sm_instructions;
    prog.length       = count_of(pps_sm_instructions);
    prog.origin       = -1;

    if (!pio_can_add_program(_pio, &prog)) return false;
    uint offset = pio_add_program(_pio, &prog);

    pio_gpio_init(_pio, _ppsPin);
    pio_sm_set_consecutive_pindirs(_pio, _ppsSM, _ppsPin, 1, false);
    gpio_pull_down(_ppsPin);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, offset + PPS_SM_WRAP_TARGET, offset + PPS_SM_WRAP);
    sm_config_set_in_pins(&c, _ppsPin);
    sm_config_set_clkdiv(&c, 1.0f);

    pio_sm_init(_pio, _ppsSM, offset, &c);

    pio_set_irq0_source_enabled(_pio, pis_interrupt0, true);
    irq_set_exclusive_handler(PIO0_IRQ_0, pps_irq_handler);
    irq_set_enabled(PIO0_IRQ_0, true);

    pio_sm_set_enabled(_pio, _ppsSM, true);
    return true;
}

bool PIOTimingEngine::initFreqCounter() {
    pio_program_t prog = {};
    prog.instructions = edge_counter_instructions;
    prog.length       = count_of(edge_counter_instructions);
    prog.origin       = -1;

    if (!pio_can_add_program(_pio, &prog)) return false;
    _freqSMOffset = pio_add_program(_pio, &prog);

    pio_gpio_init(_pio, _freqPin);
    pio_sm_set_consecutive_pindirs(_pio, _freqSM, _freqPin, 1, false);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, _freqSMOffset + EDGE_COUNTER_WRAP_TARGET,
                           _freqSMOffset + EDGE_COUNTER_WRAP);
    sm_config_set_in_pins(&c, _freqPin);
    sm_config_set_clkdiv(&c, 1.0f);

    pio_sm_init(_pio, _freqSM, _freqSMOffset, &c);
    pio_sm_set_enabled(_pio, _freqSM, true);
    return true;
}

// ============================================================
// Called from PIO IRQ handler - extremely time-sensitive
// ts = hardware timer value in microseconds at edge
// ============================================================
void PIOTimingEngine::processPPS(uint32_t ts_us) {
    _ppsCount++;

#if USE_FREQ_COUNTER
    // Snapshot the edge counter x register by exec-injecting two instructions
    // into the running SM. The SM may be briefly stalled on a wait instruction
    // (at most one 10MHz period = 100ns); pio_sm_exec_wait_blocking() handles this.
    // mov isr, x  — copy x directly into ISR
    pio_sm_exec_wait_blocking(_pio, _freqSM,
        pio_encode_mov(pio_isr, pio_x));
    // push noblock — push ISR to RX FIFO (drop if full, don't stall)
    pio_sm_exec_wait_blocking(_pio, _freqSM,
        pio_encode_push(false, false));
    const uint32_t xNow = pio_sm_is_rx_fifo_empty(_pio, _freqSM)
                          ? 0u : pio_sm_get(_pio, _freqSM);
#endif

    if (_firstPPS) {
        _firstPPS      = false;
        _prevPPScycles = ts_us;
#if USE_FREQ_COUNTER
        _prevEdgeX  = xNow;
        _freqSeeded = true;
#endif
        return;
    }

    const uint32_t interval_us = ts_us - _prevPPScycles;
    _prevPPScycles = ts_us;

    const int32_t error_us = (int32_t)interval_us - 1000000;
    const int64_t error_ns = (int64_t)error_us * 1000LL;

    critical_section_enter_blocking(&_cs);
    _result.ppsValid      = true;
    _result.phaseError_ns = error_ns;
    _result.ppsCycleCount = interval_us;
    _result.ppsCount      = _ppsCount;

#if USE_FREQ_COUNTER
    if (_freqSeeded && interval_us > 0) {
        // x counts DOWN; edges this second = x_prev - x_now.
        // Unsigned subtraction handles the rare 32-bit wrap correctly.
        const uint32_t edgesThisSec = _prevEdgeX - xNow;
        _prevEdgeX = xNow;

        // pulse_count over one 1PPS window IS the frequency in Hz.
        // error = counts - nominal, scaled to ppb.
        const int32_t freqError_Hz = (int32_t)edgesThisSec - (int32_t)_ocxoHz;
        const double error_ppb     = (double)freqError_Hz * (1e9 / (double)_ocxoHz);

        _result.freqValid       = true;
        _result.freqPulseCount  = edgesThisSec;
        _result.measuredFreq_Hz = edgesThisSec;   // counts = Hz over 1 PPS window
        _result.freqError_ppb   = error_ppb;
        _result.freqCycleCount  = interval_us;
    } else {
        if (!_freqSeeded) {
            _prevEdgeX  = xNow;
            _freqSeeded = true;
        }
        _result.freqValid = false;
    }
#else
    _result.freqValid = false;
#endif

    _resultReady = true;
    critical_section_exit(&_cs);
}

// ============================================================
// Safe cross-core result read
// ============================================================
TimingResult PIOTimingEngine::getResult() {
    if (!_syncReady) {
        TimingResult r;
        memset(&r, 0, sizeof(r));
        return r;
    }

    TimingResult r;
    critical_section_enter_blocking(&_cs);
    r = _result;
    _result.ppsValid  = false;  // clear ready flag after read
    _result.freqValid = false;
    critical_section_exit(&_cs);
    return r;
}

void PIOTimingEngine::update() {
    // Nothing to do - all processing happens in IRQ handlers
    // This exists for future FIFO-based polling if needed
}

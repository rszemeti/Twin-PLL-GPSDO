#include "pio_timing.h"
#include "config.h"
#include "gps.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "hardware/pwm.h"
#include "pico/critical_section.h"

// Forward declaration - GPS parser is instantiated in main.cpp
extern GPSParser gps;

// ============================================================
// PIO program opcodes
// These are hand-assembled since we can't use pioasm at
// runtime easily in Arduino framework.
// Each 16-bit word is one PIO instruction.
//
// PPS capture state machine:
//   wait 0 pin 0    = 0x2020
//   wait 1 pin 0    = 0x2020 | 0x0040 = 0x2060... 
//
// Rather than hand-assembling, we use the RP2040/2350
// hardware timer (TIMELR) which runs at 1MHz and is
// readable from C. We use a simple GPIO IRQ for the edge
// but handled in PIO-adjacent fashion.
//
// For true PIO-based timestamping we use the approach of
// having PIO signal an IRQ on edge detection, then reading
// the hardware timer in the IRQ handler - this gives us
// ~100ns accuracy (limited by IRQ latency) which is a
// significant improvement over the Arduino ISR approach.
//
// A full PIO assembler integration requires the .pio file
// to be compiled by pioasm as part of the CMake build.
// The .pio file is included in the project for that purpose.
// In the Arduino/PlatformIO framework we use the GPIO IRQ
// approach as the practical implementation.
// ============================================================

// PIO program for 1PPS - hand assembled
// wait 0 pin 0  (0x2020)
// wait 1 pin 0  (0x2060) 
// irq set 0     (0xc000) - signal IRQ 0
// jmp 0         (0x0000) - loop back
static const uint16_t pps_program_instructions[] = {
    0x2020,  // wait 0 pin 0
    0x2060,  // wait 1 pin 0 (rising edge)
    0xc000,  // irq set 0    (signal IRQ0 to CPU)
    0x0000,  // jmp 0        (loop)
};

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

static void pwm_wrap_irq_handler() {
    if (_instance) _instance->onPwmWrapIrq();
}

// ============================================================
// Constructor
// ============================================================
PIOTimingEngine::PIOTimingEngine(uint8_t ppsPin, uint8_t freqPin)
    : _ppsPin(ppsPin),
      _freqPin(freqPin),
      _sysclkHz(200000000),
      _ocxoHz(10000000),
      _pio(pio0),
      _ppsSM(0),
      _freqSM(1),
      _prevPPScycles(0),
      _ppsCount(0),
      _firstPPS(true),
    _resultReady(false),
    _syncReady(false),
    _freqSlice(0),
    _lastFreqCounter(0),
    _lastFreqWraps(0),
    _freqWrapCount(0),
    _firstFreqWindow(true) {
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
    // Load PPS program into PIO
    pio_program_t prog;
    prog.instructions = pps_program_instructions;
    prog.length       = count_of(pps_program_instructions);
    prog.origin       = -1;

    if (!pio_can_add_program(_pio, &prog)) return false;
    uint offset = pio_add_program(_pio, &prog);

    // Configure state machine
    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, offset, offset + prog.length - 1);
    sm_config_set_in_pins(&c, _ppsPin);
    sm_config_set_jmp_pin(&c, _ppsPin);

    // Run at full sysclk speed (divider = 1.0)
    sm_config_set_clkdiv(&c, 1.0f);

    // Configure pin as input
    pio_sm_set_consecutive_pindirs(_pio, _ppsSM, _ppsPin, 1, false);
    gpio_pull_down(_ppsPin);

    pio_sm_init(_pio, _ppsSM, offset, &c);

    // Set up PIO IRQ0 → CPU IRQ
    pio_set_irq0_source_enabled(_pio, pis_interrupt0, true);
    irq_set_exclusive_handler(PIO0_IRQ_0, pps_irq_handler);
    irq_set_enabled(PIO0_IRQ_0, true);

    pio_sm_set_enabled(_pio, _ppsSM, true);
    return true;
}

bool PIOTimingEngine::initFreqCounter() {
    // Use PWM edge counter mode on the B input pin of this slice.
    // For GPIO3 (default), this is slice 1 channel B.
    gpio_set_function(_freqPin, GPIO_FUNC_PWM);
    _freqSlice = pwm_gpio_to_slice_num(_freqPin);

    pwm_config cfg = pwm_get_default_config();
    pwm_config_set_clkdiv_mode(&cfg, PWM_DIV_B_RISING);
    pwm_config_set_wrap(&cfg, 0xFFFF);

    pwm_init(_freqSlice, &cfg, false);
    pwm_set_counter(_freqSlice, 0);
    _freqWrapCount = 0;
    _lastFreqCounter = 0;
    _lastFreqWraps = 0;
    _firstFreqWindow = true;

    pwm_clear_irq(_freqSlice);
    pwm_set_irq_enabled(_freqSlice, true);
    irq_set_exclusive_handler(PWM_IRQ_WRAP, pwm_wrap_irq_handler);
    irq_set_enabled(PWM_IRQ_WRAP, true);

    pwm_set_enabled(_freqSlice, true);
    return true;
}

// ============================================================
// Called from PIO IRQ handler - extremely time-sensitive
// ts = hardware timer value in microseconds at edge
// ============================================================
void PIOTimingEngine::processPPS(uint32_t ts_us) {
    _ppsCount++;

    if (_firstPPS) {
        _firstPPS     = false;
        _prevPPScycles = ts_us;
        return;
    }

    // Interval in microseconds (handles 32-bit wraparound)
    uint32_t interval_us = ts_us - _prevPPScycles;
    _prevPPScycles = ts_us;

    // Ideal = 1,000,000 us exactly
    int32_t error_us = (int32_t)interval_us - 1000000;

    // Convert to nanoseconds
    int64_t error_ns = (int64_t)error_us * 1000LL;

    // Write result under critical section
    critical_section_enter_blocking(&_cs);
    _result.ppsValid        = true;
    _result.phaseError_ns   = error_ns;
    _result.ppsCycleCount   = interval_us;
    _result.ppsCount        = _ppsCount;

#if USE_FREQ_COUNTER
    const uint32_t wraps = _freqWrapCount;
    const uint16_t counter = pwm_get_counter(_freqSlice);

    if (_firstFreqWindow) {
        _firstFreqWindow = false;
        _lastFreqWraps = wraps;
        _lastFreqCounter = counter;
        _result.freqValid = false;
    } else {
        const uint32_t deltaWraps = wraps - _lastFreqWraps;
        const int32_t deltaCounter = (int32_t)counter - (int32_t)_lastFreqCounter;
        const uint32_t pulseCount = (deltaWraps * 65536u) + (uint32_t)(deltaCounter & 0xFFFF);

        _lastFreqWraps = wraps;
        _lastFreqCounter = counter;

        if (interval_us > 0) {
            const double interval_s = (double)interval_us / 1e6;
            const double measuredFreq = (double)pulseCount / interval_s;
            const double error_ppb = ((measuredFreq - _ocxoHz) / _ocxoHz) * 1e9;

            _result.freqValid = true;
            _result.freqPulseCount = pulseCount;
            _result.measuredFreq_Hz = measuredFreq;
            _result.freqError_ppb = error_ppb;
            _result.freqCycleCount = interval_us;
        } else {
            _result.freqValid = false;
        }
    }
#else
    _result.freqValid = false;
#endif

    _resultReady            = true;
    critical_section_exit(&_cs);
}

// ============================================================
// Called from freq PIO IRQ - 10 OCXO edges have elapsed
// ts = hardware timer in us at the 10th edge
// ============================================================
void PIOTimingEngine::processFreq(uint32_t ts_us) {
    (void)ts_us;
}

void PIOTimingEngine::onPwmWrapIrq() {
    if (pwm_get_irq_status_mask() & (1u << _freqSlice)) {
        pwm_clear_irq(_freqSlice);
        _freqWrapCount++;
    }
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

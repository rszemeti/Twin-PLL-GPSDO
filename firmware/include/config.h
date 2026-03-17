#pragma once

#include <stdint.h>

// ============================================================
// GPSDO + Dual ADF4351 Reference Source
// Hardware Configuration
// ============================================================

// --- GPS ---
#define GPS_UART        uart0
#define GPS_TX_PIN      0
#define GPS_RX_PIN      1
#define GPS_1PPS_PIN    2
#define GPS_BAUD        9600

// --- 10MHz frequency counter input (from OCXO) ---
#define FREQ_COUNT_PIN  3

// --- ADF4351 #1 (104 MHz) ---
#define ADF1_CLK_PIN    4
#define ADF1_MOSI_PIN   5
#define ADF1_LE_PIN     6
#define ADF1_CE_PIN     12
#define ADF1_LD_PIN     10   // lock detect

// Set to 1 when ADF4351 #1 hardware is physically installed, else 0.
#define ADF1_INSTALLED  0

// --- ADF4351 #2 (116 MHz) ---
#define ADF2_CLK_PIN    7
#define ADF2_MOSI_PIN   8
#define ADF2_LE_PIN     9
#define ADF2_CE_PIN     13
#define ADF2_LD_PIN     11   // lock detect

// Set to 1 when ADF4351 #2 hardware is physically installed, else 0.
#define ADF2_INSTALLED  0

// --- I2C for MCP4725 DAC (OCXO EFC) ---
#define I2C_SDA_PIN     14
#define I2C_SCL_PIN     15
#define MCP4725_ADDR    0x60  // A0 tied low

// --- Status LEDs ---
#define LED_GPS_LOCK    16
#define LED_DISCIPLINED 17
#define LED_ADF1_LOCK   18
#define LED_ADF2_LOCK   19
#define LED_ALARM       20

// Additional spare LEDs
// LED that flashes at 0.5s intervals when any satellites are present
#define LED_SATS_BLINK  23
// LED that indicates a usable fix (satellites in use >= 4)
#define LED_SATS_USED   24

// --- Alarm output (open drain / active low) ---
#define ALARM_PIN       21

// --- Debug UART ---
#define DEBUG_TX_PIN    22

// ============================================================
// ADF4351 Register Values
// Calculated for:
//   OCXO ref = 10MHz, R=5, PFD=2MHz
//   Integer-N mode, output divider /32
//   Output power = 0dBm (+3dBm register setting,
//   external attenuator to taste)
//
// IMPORTANT: Verify these with ADIsimPLL before use.
// Register order: R0..R5 (written R5 first, R0 last)
// ============================================================

// --- 104 MHz: VCO=3328MHz, N=1664, /32 ---
// R0: INT=1664, FRAC=0
// R1: MOD=1, PHASE=1, prescaler 8/9
// R2: low-noise mode, MUXOUT=digital lock, R=5, CP=2.50mA,
//     integer-N lock detect settings (DB8:DB7 = 11)
// R3: clock divider value = 150, mode off
// R4: feedback=fundamental, RF div=/32, band select clk div=150,
//     RF out enable, +5dBm
// R5: LD pin = digital lock detect
static const uint32_t ADF1_REGS[6] = {
    0x03400000,  // R0
    0x08008009,  // R1
    0x18014FC2,  // R2
    0x000004B3,  // R3: clk div=150
    0x00D9603C,  // R4
    0x00580005   // R5: LD=digital lock detect
};

// --- 116 MHz: VCO=3712MHz, N=1856, /32 ---
static const uint32_t ADF2_REGS[6] = {
    0x03A00000,  // R0
    0x08008009,  // R1
    0x18014FC2,  // R2
    0x000004B3,  // R3: same as above
    0x00D9603C,  // R4
    0x00580005   // R5: same as above
};

// ============================================================
// GPSDO Disciplining Parameters
// ============================================================

// PI loop time constants (in 1PPS ticks = seconds)
#define DISC_P_GAIN         0.001f   // proportional gain
#define DISC_I_GAIN         0.0001f  // integral gain
#define DISC_P_GAIN_MIN     0.000001f
#define DISC_P_GAIN_MAX     0.05f
#define DISC_I_GAIN_MIN     0.0000001f
#define DISC_I_GAIN_MAX     0.01f

// DAC limits (12-bit MCP4725, 0-4095)
// Set to keep OCXO EFC within safe range
#define DAC_MIN             100
#define DAC_MAX             3995
#define DAC_CENTRE          2048     // nominal centre voltage

// How many 1PPS edges to average before first correction
#define DISC_WARMUP_SECS    30

// Phase error threshold to declare "disciplined" (nanoseconds)
#define DISC_LOCK_THRESHOLD_NS  100

// GPS lock required before disciplining starts
#define GPS_FIX_REQUIRED    true

// Frequency counter gate time (seconds)
#define FREQ_GATE_SECS      1
// Number of PPS samples to average before applying PI correction.
#define DISC_AVERAGE_SECS   8
#define DISC_AVERAGE_SECS_MIN 1
#define DISC_AVERAGE_SECS_MAX 120

// ============================================================
// Status / Alarm
// ============================================================

// Alarm if ADF lock lost for this many seconds
#define ALARM_LOCK_TIMEOUT  5

// Alarm if GPS 1PPS missing for this many seconds  
#define ALARM_GPS_TIMEOUT   10

// ============================================================
// Persistent storage
// ============================================================
// EEPROM address to store unlocked DAC value (uint16_t)
#define DAC_EEPROM_ADDR     0
// How often to save DAC value when changed (seconds)
#define DAC_SAVE_INTERVAL_SECS  300
// Minimum DAC change (in DAC counts) required to trigger saving
#define DAC_SAVE_HYSTERESIS     4

// ============================================================
// Discipliner runtime control persistence
// ============================================================
// EEPROM layout:
//   uint32_t magic
//   uint32_t version
//   uint32_t avg_window_s
//   float    p_gain
//   float    i_gain
#define DISC_CTRL_EEPROM_ADDR  512
#define DISC_CTRL_MAGIC        0xD15CC710UL
#define DISC_CTRL_VERSION      1

// ============================================================
// ADF4351 register persistence
// ============================================================
// EEPROM layout for each ADF block:
//   uint32_t magic
//   uint32_t version
//   uint32_t regs[6]
// Addresses must not overlap with DAC_EEPROM_ADDR (uint16_t at addr 0)
#define ADF1_EEPROM_ADDR    64
#define ADF2_EEPROM_ADDR    256
#define ADF_REGS_MAGIC      0xADF43510UL
#define ADF_REGS_VERSION    2
// Set to 0 to disable ADF register EEPROM writes (RAM-only updates for debug).
#define ADF_PERSIST_EEPROM  0
// Debounce interval before committing staged ADF EEPROM writes.
#define ADF_EEPROM_COMMIT_DELAY_MS 50
// When using staged EEPROM persistence, auto-commit pending staged writes.
#define ADF_AUTO_COMMIT_STAGED 1
// Use multicore lockout around EEPROM.commit() on dual-core builds.
#define ADF_EEPROM_USE_LOCKOUT 0
// Extra debug delay around deferred commit logs (used only in optional EEPROM branch).
#define ADF_EEPROM_DEBUG_SLEEP_MS 0
// Enable verbose JSON trace events for debug/instrumentation.
#define ADF_TRACE_ENABLED 0

// Enable 10MHz pulse counting between PPS edges (PWM hardware counter).
#define USE_FREQ_COUNTER    1

// Test switch: disable core1 timing engine initialization to isolate
// commit/pause behavior from timing IRQ activity.
#define DISABLE_CORE1_TIMING_ENGINE 0

// Enable JSON mode for serial I/O (1 = JSON single-line status output, and
// accept simple JSON commands). If 0, legacy textual output/CLI is used.
#define JSON_OUTPUT 1

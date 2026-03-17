# Reference Locking Technical Notes

This document is a **technical companion** to the main user README.
It explains how the 10 MHz reference is disciplined to GPS, how timing capture works, and where the real error limits come from.

## 1) System intent

The project disciplines a local 10 MHz oscillator (TCXO/OCXO class behavior via DAC EFC control) to GPS 1PPS, then uses that disciplined reference to synthesize RF outputs with ADF4351 devices.

High-level loop:

1. GPS receiver provides 1PPS edge (absolute time reference).
2. Firmware timestamps consecutive PPS edges and computes phase error.
3. PI loop converts phase error into DAC correction (EFC control voltage).
4. Corrected 10 MHz reference drives ADF4351 PLL(s).
5. ADF lock detect and system telemetry provide health status.

---

## 2) Timing capture path (PIO + IRQ model)

The timing engine uses PIO state machines to detect edges and raise PIO interrupts. CPU IRQ handlers then timestamp events.

- `SM0`: GPS 1PPS edge detector (`GPIO2` by default).
- `SM1`: optional frequency edge detector (`GPIO3`, currently disabled in config).
- IRQ handlers are bound to `PIO0_IRQ_0` and `PIO0_IRQ_1`.

### Important implementation detail

Current firmware timestamps using:

- `timer_hw->timerawl` (microsecond timer), read at IRQ entry.

So while PIO edge detection is fast and deterministic, the **timestamp quantization in the current code path is 1 µs**.

That means:

- phase error is currently computed in 1000 ns steps,
- not in raw PIO-cycle granularity.

---

## 3) Where 16.5 ns / 6.67 ns numbers come from

Those numbers are from **clock-period resolution**, not from the current microsecond timer quantization path.

Clock period:

$$
T = \frac{1}{f_{sysclk}}
$$

Examples:

- At ~60.6 MHz: $T \approx 16.5\ \text{ns}$
- At 150 MHz (current default): $T \approx 6.67\ \text{ns}$

So if/when timestamping is done using cycle-level capture, those are the relevant granularity numbers.

---

## 4) PI disciplining loop

The discipliner uses a PI controller on each valid PPS update:

- proportional term: `DISC_P_GAIN`
- integral term: `DISC_I_GAIN`
- output clamps: `DAC_MIN..DAC_MAX`
- lock threshold: `DISC_LOCK_THRESHOLD_NS`

State machine:

- `WARMUP` → `ACQUIRING` → `LOCKED`
- `HOLDOVER` when GPS validity is lost
- `FREERUN` if GPS has never been valid

DAC state is periodically persisted with hysteresis to avoid excessive writes.

---

## 5) Interrupt flow and concurrency

- PIO asserts interrupt source (`pis_interrupt0/1`).
- IRQ handler reads timestamp immediately and updates shared timing result.
- Shared timing result is protected by RP critical sections for cross-core safety.
- Main control/telemetry runs on core0; timing engine is kept active on core1.

For EEPROM commit windows, PIO IRQ sources are explicitly gated to avoid commit-time interference.

---

## 6) Error budget intuition: why GPS jitter dominates (in principle)

When discussing the *ideal* timing floor, GPS 1PPS short-term noise is often the dominant term.

If GPS 1PPS edge jitter is roughly 20 ns RMS, and internal timestamp granularity/noise is below that scale, then GPS dominates short-term phase observation noise.

Conceptually:

$$
\sigma_{total} \approx \sqrt{\sigma_{gps}^2 + \sigma_{capture}^2 + \sigma_{irq}^2}
$$

If $\sigma_{gps} \gg \sigma_{capture},\sigma_{irq}$, then $\sigma_{total}$ is set mostly by GPS.

### For this firmware revision

Because PPS intervals are currently measured with microsecond timer ticks, the practical quantization floor in this path is much coarser than 20 ns.

So two truths coexist:

- **Architecture goal:** nanosecond-scale capture where GPS jitter (~20 ns) dominates.
- **Current implementation path:** microsecond-quantized timestamping in `pio_timing.cpp`.

---

## 7) ADF lock telemetry

Each ADF4351 uses dedicated lock-detect input pins (`ADF1_LD_PIN`, `ADF2_LD_PIN`) for lock status.

This lock status is used by:

- status LEDs,
- JSON telemetry fields,
- alarm timeout logic (`ALARM_LOCK_TIMEOUT`).

---

## 8) Practical summary

- The control architecture is correct for GPS disciplining.
- PIO edge detection + IRQ model is in place.
- Current PPS timestamp quantization is 1 µs due to timer source selection.
- Theoretical sub-20 ns discussions (16.5 ns / 6.67 ns clock periods, ~20 ns GPS jitter dominance) apply to cycle-level timestamping paths.

If you later move to cycle-count-based capture end-to-end, the same architecture can support the tighter timing narrative directly.

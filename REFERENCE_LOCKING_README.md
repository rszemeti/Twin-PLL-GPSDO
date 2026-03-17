# Reference Locking Technical Notes

This document is the technical companion to the main user README.
It describes the current reference-locking architecture used in firmware.

## 1) System objective

The GPSDO loop disciplines a local 10 MHz reference to GPS 1PPS.
That disciplined 10 MHz then drives ADF4351 synthesis.

Top-level control flow:

1. Detect each GPS 1PPS edge.
2. Measure PPS interval and 10 MHz pulse count over that PPS window.
3. Compute phase and frequency error observables.
4. Average phase over multiple seconds (`DISC_AVERAGE_SECS`).
5. Apply PI correction to DAC/EFC.

Control-loop signal flow:

```mermaid
flowchart LR
	OCXO["10 MHz OCXO"] --> EDGE["PWM edge counter\n(count 10 MHz edges)"]
	GPS["GPS 1PPS"] --> PPS["PIO PPS capture ISR\n(sample counter + timestamp)"]
	EDGE --> PPS

	PPS --> OBS["Per-second observables\nphase_error_ns, pulse_count, measured_hz"]
	OBS --> AVG["Phase averaging\nDISC_AVERAGE_SECS"]
	AVG --> PI["Discipliner PI controller"]
	PI --> DAC["MCP4725 DAC / EFC voltage"]
	DAC --> OCXO
```

---

## 2) Measurement architecture (current)

### 2.1 PPS capture

- A PIO state machine detects PPS edges.
- `PIO0_IRQ_0` ISR timestamps the edge using `timer_hw->timerawl` (microsecond counter).

So PPS interval / phase observable resolution in the present implementation is based on 1 µs timer ticks.
This applies to timestamp-derived phase error, not to the 10 MHz edge-count observable itself.

### 2.2 10 MHz pulse counting per PPS window

The 10 MHz input is counted continuously using PWM edge-counter mode:

- GPIO is assigned to PWM B input,
- PWM counter increments on each rising edge,
- wrap IRQ tracks overflows,
- at each PPS ISR, firmware snapshots `(wraps, counter)` and forms window delta.

This yields pulse count per PPS interval:

$$
N_{10MHz} = \Delta wraps \cdot 65536 + \Delta counter
$$

Frequency estimate per PPS interval:

$$
f_{meas} = \frac{N_{10MHz}}{T_{pps}}
$$

with $T_{pps}$ from measured PPS interval in seconds.

Frequency error in ppb:

$$
error_{ppb} = \frac{f_{meas} - f_{nominal}}{f_{nominal}} \cdot 10^9
$$

---

## 3) PI loop and averaging

The discipliner receives phase error in ns and GPS validity.

Key loop characteristics:

- PI gains: `DISC_P_GAIN`, `DISC_I_GAIN`
- output clamp: `DAC_MIN..DAC_MAX`
- lock threshold: `DISC_LOCK_THRESHOLD_NS`
- phase averaging window: `DISC_AVERAGE_SECS` (currently used before PI update)

State machine remains:

- `WARMUP`
- `ACQUIRING`
- `LOCKED`
- `HOLDOVER`
- `FREERUN`

---

## 4) Telemetry signals

### 4.1 OCXO event telemetry

`event: "ocxo"` now includes:

- `pulse_count`
- `measured_hz`
- `freq_error_ppb`

### 4.2 Periodic status telemetry

Status JSON includes averaging visibility fields:

- `disc_avg_window_s`
- `disc_avg_phase_ns`

and the standard lock/GPS/DAC/ADF fields.

---

## 5) Timing-resolution notes

Clock-period references still hold:

$$
T = \frac{1}{f_{sysclk}}
$$

Examples:

- ~60.6 MHz → ~16.5 ns
- 150 MHz → ~6.67 ns

Those values are the hardware clock period scale.
Current PPS interval timestamping (used for phase error) is still read via microsecond timer in ISR.
The frequency observable is based on PPS-gated 10 MHz edge counting, with timer quantization entering through the interval term in $f_{meas}=N/T$.

---

## 6) Why this is better than the previous path

Compared with the earlier simplified frequency path, the current method:

- measures true pulse count over each PPS window,
- avoids high-rate per-edge CPU interrupts,
- provides a direct ppb observable each second,
- supports stable control with multi-second averaging.

This architecture is a practical, low-overhead step toward tighter reference disciplining.

---

## 7) ADF lock and alarm context

ADF lock detect pins (`ADF1_LD_PIN`, `ADF2_LD_PIN`) remain the runtime lock truth source.
They drive status LEDs, alarm logic, and lock-related JSON fields.

---

## 8) Practical summary

- PPS edge timing: PIO-detected, ISR timestamped.
- Frequency observable: PPS-window pulse count of 10 MHz.
- Control update: averaged phase over `DISC_AVERAGE_SECS`.
- Telemetry now exposes averaging and pulse-count observables for verification/tuning.

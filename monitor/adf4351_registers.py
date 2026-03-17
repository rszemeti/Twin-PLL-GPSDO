from __future__ import annotations

from dataclasses import dataclass
from math import ceil, gcd
from typing import List, Literal


PrescalerMode = Literal["auto", "4/5", "8/9"]
NoiseMode = Literal["low_noise", "low_spur"]


@dataclass(frozen=True)
class ADF4351Config:
    ref_hz: float = 10_000_000.0
    r_counter: int = 5
    ref_doubler: bool = False
    ref_div2: bool = False
    channel_spacing_hz: float = 1.0
    phase: int = 1
    prescaler: PrescalerMode = "auto"
    integer_n: bool = True
    charge_pump_code: int = 7
    pd_polarity_positive: bool = True
    band_select_clock_div: int = 150
    rf_output_enable: bool = True
    rf_output_power_code: int = 3
    feedback_fundamental: bool = True
    lock_detect_pin_digital: bool = True
    noise_mode: NoiseMode = "low_noise"


@dataclass(frozen=True)
class ADF4351Solution:
    target_hz: float
    actual_hz: float
    pfd_hz: float
    vco_hz: float
    output_divider: int
    int_value: int
    frac_value: int
    mod_value: int
    prescaler_89: bool
    registers_r0_to_r5: List[int]

    @property
    def registers_r5_to_r0(self) -> List[int]:
        return list(reversed(self.registers_r0_to_r5))

    @property
    def error_hz(self) -> float:
        return self.actual_hz - self.target_hz


@dataclass(frozen=True)
class ADF4351Decoded:
    int_value: int
    frac_value: int
    mod_value: int
    r_counter: int
    ref_doubler: bool
    ref_div2: bool
    rf_divider: int
    prescaler_89: bool
    pfd_hz: float
    vco_hz: float
    rf_out_hz: float


class ADF4351RegisterCalculator:
    VCO_MIN_HZ = 2_200_000_000.0
    VCO_MAX_HZ = 4_400_000_000.0
    RF_DIVIDERS = (1, 2, 4, 8, 16, 32, 64)

    def __init__(self, config: ADF4351Config | None = None) -> None:
        self.config = config or ADF4351Config()
        self._validate_config(self.config)

    @staticmethod
    def _validate_config(config: ADF4351Config) -> None:
        if not (1 <= config.r_counter <= 1023):
            raise ValueError("r_counter must be in 1..1023")
        if config.ref_hz <= 0:
            raise ValueError("ref_hz must be > 0")
        if config.channel_spacing_hz <= 0:
            raise ValueError("channel_spacing_hz must be > 0")
        if not (1 <= config.phase <= 4095):
            raise ValueError("phase must be in 1..4095")
        if not (0 <= config.charge_pump_code <= 15):
            raise ValueError("charge_pump_code must be in 0..15")
        if not (1 <= config.band_select_clock_div <= 255):
            raise ValueError("band_select_clock_div must be in 1..255")
        if not (0 <= config.rf_output_power_code <= 3):
            raise ValueError("rf_output_power_code must be in 0..3")

    def pfd_hz(self) -> float:
        doubler = 2.0 if self.config.ref_doubler else 1.0
        div2 = 2.0 if self.config.ref_div2 else 1.0
        return (self.config.ref_hz * doubler) / (self.config.r_counter * div2)

    def solve(self, target_hz: float) -> ADF4351Solution:
        if target_hz <= 0:
            raise ValueError("target_hz must be > 0")

        output_div = self._choose_output_divider(target_hz)
        vco_hz = target_hz * output_div
        pfd = self.pfd_hz()

        n_float = vco_hz / pfd
        int_value, frac_value, mod_value = self._synthesize_n(n_float, pfd)
        prescaler_89 = self._choose_prescaler(int_value)

        self._validate_n(int_value, mod_value, prescaler_89)
        self._validate_mode_limits(pfd_hz=pfd, is_fractional=(frac_value != 0))
        self._validate_band_select_clock(pfd_hz=pfd)

        actual_vco_hz = pfd * (int_value + (frac_value / mod_value))
        actual_hz = actual_vco_hz / output_div

        regs = self._build_registers(
            int_value=int_value,
            frac_value=frac_value,
            mod_value=mod_value,
            prescaler_89=prescaler_89,
            rf_divider=output_div,
        )

        return ADF4351Solution(
            target_hz=target_hz,
            actual_hz=actual_hz,
            pfd_hz=pfd,
            vco_hz=actual_vco_hz,
            output_divider=output_div,
            int_value=int_value,
            frac_value=frac_value,
            mod_value=mod_value,
            prescaler_89=prescaler_89,
            registers_r0_to_r5=regs,
        )

    @staticmethod
    def _validate_mode_limits(pfd_hz: float, is_fractional: bool) -> None:
        if is_fractional and pfd_hz > 32_000_000.0:
            raise ValueError("Fractional-N mode requires PFD <= 32 MHz")
        if (not is_fractional) and pfd_hz > 90_000_000.0:
            raise ValueError("Integer-N mode requires PFD <= 90 MHz")

    def _validate_band_select_clock(self, pfd_hz: float) -> None:
        band_select_clk_hz = pfd_hz / self.config.band_select_clock_div
        if band_select_clk_hz > 125_000.0:
            raise ValueError(
                "Band-select clock exceeds 125 kHz in low mode; increase band_select_clock_div"
            )

    @classmethod
    def decode_registers(
        cls,
        registers_r0_to_r5: List[int],
        ref_hz: float,
    ) -> ADF4351Decoded:
        if len(registers_r0_to_r5) != 6:
            raise ValueError("registers_r0_to_r5 must have exactly 6 entries")
        if ref_hz <= 0:
            raise ValueError("ref_hz must be > 0")

        r0, r1, r2, _, r4, _ = registers_r0_to_r5

        int_value = (r0 >> 15) & 0xFFFF
        frac_value = (r0 >> 3) & 0x0FFF
        mod_value = (r1 >> 3) & 0x0FFF
        mod_value = mod_value if mod_value > 0 else 1

        prescaler_89 = bool((r1 >> 27) & 0x1)

        ref_doubler = bool((r2 >> 25) & 0x1)
        ref_div2 = bool((r2 >> 24) & 0x1)
        r_counter = (r2 >> 14) & 0x03FF
        r_counter = r_counter if r_counter > 0 else 1

        rf_div_sel = (r4 >> 20) & 0b111
        rf_divider = 1 << rf_div_sel

        pfd_hz = (ref_hz * (2.0 if ref_doubler else 1.0)) / (
            r_counter * (2.0 if ref_div2 else 1.0)
        )
        vco_hz = pfd_hz * (int_value + (frac_value / mod_value))
        rf_out_hz = vco_hz / rf_divider

        return ADF4351Decoded(
            int_value=int_value,
            frac_value=frac_value,
            mod_value=mod_value,
            r_counter=r_counter,
            ref_doubler=ref_doubler,
            ref_div2=ref_div2,
            rf_divider=rf_divider,
            prescaler_89=prescaler_89,
            pfd_hz=pfd_hz,
            vco_hz=vco_hz,
            rf_out_hz=rf_out_hz,
        )

    @classmethod
    def verify_target(
        cls,
        registers_r0_to_r5: List[int],
        ref_hz: float,
        target_hz: float,
        tolerance_hz: float = 1.0,
    ) -> tuple[bool, ADF4351Decoded, float]:
        decoded = cls.decode_registers(registers_r0_to_r5, ref_hz)
        error_hz = decoded.rf_out_hz - target_hz
        ok = abs(error_hz) <= tolerance_hz
        return ok, decoded, error_hz

    def _choose_output_divider(self, target_hz: float) -> int:
        for div in self.RF_DIVIDERS:
            vco = target_hz * div
            if self.VCO_MIN_HZ <= vco <= self.VCO_MAX_HZ:
                return div
        raise ValueError("target_hz is outside ADF4351 synthesizable range")

    def _synthesize_n(self, n_float: float, pfd_hz: float) -> tuple[int, int, int]:
        if self.config.integer_n:
            int_value = int(round(n_float))
            return int_value, 0, 1

        mod_value = int(round(pfd_hz / self.config.channel_spacing_hz))
        mod_value = max(2, min(mod_value, 4095))
        int_value = int(n_float)
        frac_value = int(round((n_float - int_value) * mod_value))

        if frac_value >= mod_value:
            int_value += 1
            frac_value = 0

        if frac_value == 0:
            return int_value, 0, 1

        divisor = gcd(frac_value, mod_value)
        frac_value //= divisor
        mod_value //= divisor
        mod_value = max(2, min(mod_value, 4095))

        if frac_value >= mod_value:
            raise ValueError("Invalid FRAC/MOD after reduction")

        return int_value, frac_value, mod_value

    def _choose_prescaler(self, int_value: int) -> bool:
        choice = self.config.prescaler
        if choice == "8/9":
            return True
        if choice == "4/5":
            return False
        return int_value >= 75

    @staticmethod
    def _validate_n(int_value: int, mod_value: int, prescaler_89: bool) -> None:
        if not (23 <= int_value <= 65535):
            raise ValueError("INT out of range (23..65535)")
        if prescaler_89 and int_value < 75:
            raise ValueError("INT must be >= 75 when prescaler is 8/9")
        if not (1 <= mod_value <= 4095):
            raise ValueError("MOD out of range (1..4095)")

    def _build_registers(
        self,
        int_value: int,
        frac_value: int,
        mod_value: int,
        prescaler_89: bool,
        rf_divider: int,
    ) -> List[int]:
        c = self.config
        rf_div_sel = self.RF_DIVIDERS.index(rf_divider)

        r0 = ((int_value & 0xFFFF) << 15) | ((frac_value & 0x0FFF) << 3) | 0

        phase_adjust = 0
        r1 = (
            (phase_adjust << 28)
            | ((1 if prescaler_89 else 0) << 27)
            | ((c.phase & 0x0FFF) << 15)
            | ((mod_value & 0x0FFF) << 3)
            | 1
        )

        noise_mode_bits = 0b00 if c.noise_mode == "low_noise" else 0b11
        muxout_digital_lock = 0b110
        ldf_integer_n = 1 if frac_value == 0 else 0
        # Datasheet recommendation:
        #  - Fractional-N: DB8:DB7 = 00
        #  - Integer-N:   DB8:DB7 = 11
        ldp_bit = 1 if frac_value == 0 else 0

        r2 = (
            (noise_mode_bits << 29)
            | (muxout_digital_lock << 26)
            | ((1 if c.ref_doubler else 0) << 25)
            | ((1 if c.ref_div2 else 0) << 24)
            | ((c.r_counter & 0x03FF) << 14)
            | ((c.charge_pump_code & 0x0F) << 9)
            | (ldf_integer_n << 8)
            | (ldp_bit << 7)
            | ((1 if c.pd_polarity_positive else 0) << 6)
            | 2
        )

        clock_divider_value = 150
        clk_div_mode = 0
        csr = 0
        r3 = (
            (csr << 18)
            | ((clk_div_mode & 0b11) << 15)
            | ((clock_divider_value & 0x0FFF) << 3)
            | 3
        )

        band_select = c.band_select_clock_div
        r4 = (
            ((1 if c.feedback_fundamental else 0) << 23)
            | ((rf_div_sel & 0b111) << 20)
            | ((band_select & 0xFF) << 12)
            | ((1 if c.rf_output_enable else 0) << 5)
            | ((c.rf_output_power_code & 0b11) << 3)
            | 4
        )

        ld_pin_mode = 0b01 if c.lock_detect_pin_digital else 0b00
        r5 = (0b01011 << 19) | (ld_pin_mode << 22) | 5

        return [r0, r1, r2, r3, r4, r5]


def format_registers_hex(registers_r0_to_r5: List[int]) -> List[str]:
    return [f"R{i}: 0x{v:08X}" for i, v in enumerate(registers_r0_to_r5)]


if __name__ == "__main__":
    calc = ADF4351RegisterCalculator(
        ADF4351Config(
            ref_hz=10_000_000.0,
            r_counter=5,
            integer_n=True,
            prescaler="8/9",
            band_select_clock_div=200,
            rf_output_power_code=3,
        )
    )

    for f in (104_000_000.0, 116_000_000.0):
        solution = calc.solve(f)
        print(f"\nTarget: {f:.0f} Hz, Actual: {solution.actual_hz:.3f} Hz")
        for line in format_registers_hex(solution.registers_r0_to_r5):
            print(line)

    print("\nDecode existing firmware words example:")
    firmware_words = [0x006A0000, 0x00008029, 0x00004E42, 0x000004B3, 0x00BC803C, 0x00580005]
    decoded = ADF4351RegisterCalculator.decode_registers(firmware_words, ref_hz=10_000_000.0)
    print(
        f"INT={decoded.int_value}, FRAC={decoded.frac_value}, MOD={decoded.mod_value}, "
        f"R={decoded.r_counter}, RF_DIV={decoded.rf_divider}, Fout={decoded.rf_out_hz:.3f} Hz"
    )
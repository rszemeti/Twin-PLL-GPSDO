GPSDO Configuration Tool

A minimal PySide6 GUI configuration tool that connects to the device over serial, parses JSON status lines, and displays key fields.

Full software manual:
- See `docs/MONITOR_USER_MANUAL.md` for complete usage, workflows, and troubleshooting.

Setup

To configure the unit, a simple GUI is available as a pre-built Windows .exe file Just download, connect your device over USB and run the program. You can configue the two fully independent outputs directly from the program. 

Updating the software on the unit itself is simply a question of powering it up ovr USB with the "reset" button pressed down and dragging a new .uf2 firmware file onto the folder that appears on your desktop.


Advanced

The adventurous may prefer to download the Python GUI sourcecode and run the GUI from its native Python source.

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

Run

python gpsdo_monitor.py

Usage

- Select serial port and press Connect.
- The configuration tool displays firmware version, peripheral states, DAC value, ADF lock states, and logs raw/non-JSON lines.
- Send JSON commands from the **Advanced** tab send box (e.g. {"cmd":"dac","value":2048}).
- Use **Set PLL1 Register** or **Set PLL2 Register** to enter a target frequency and apply generated ADF4351 registers; popups preload from decoded live register values when available (otherwise fallback defaults are 10 MHz ref, R=5).
- Popup **Synth mode** options:
	- **Auto**: use Integer-N when exact, otherwise use Fractional-N.
	- **Integer-N only**: force integer synthesis.
	- **Fractional-N only**: force fractional synthesis for closest frequency.
- Popup **RF output power** lets you choose ADF4351 output power (-4, -1, +2, +5 dBm setting).
- Popup **Noise mode** lets you choose **Low noise** or **Low spur** register mode.
- Popup **Charge pump current** lets you choose the ADF4351 CP current code (0.31 mA to 5.00 mA).
- The firmware now auto-saves ADF registers to EEPROM when `program` is issued, so settings persist across reboot without a separate save step.
- **Discipliner Control (Advanced)** (Advanced tab) lets you read/apply runtime `avg_window_s`, `P gain`, and `I gain` via firmware JSON command `disc_ctrl`.
- Preset buttons (**Slow**, **Normal**, **Fast**) provide one-click loop tuning and immediately apply those values.
- Applied discipliner settings are persisted in firmware and restored after reboot.
- **Step Response...** in **Discipliner Control (Advanced)** opens a test tool that can run a timed DAC step, temporarily increase telemetry rate to 1 Hz, capture response samples, and export CSV.

ADF4351 register helper

- `adf4351_registers.py` contains a reusable `ADF4351RegisterCalculator` class.
- Use `solve(target_hz)` to generate `R0..R5` values from a reference/config.
- Use `decode_registers(regs, ref_hz)` and `verify_target(...)` to validate any existing register set before programming hardware.
- This is intended to avoid trusting unverified register words from external tools.

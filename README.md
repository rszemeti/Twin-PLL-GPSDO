# Twin PLL GPSDO

A dual-output GPS-disciplined reference project for Raspberry Pi Pico 2 (RP2350), with:
- Firmware for GPSDO control + PLL programming
- A desktop configuration tool (Windows-friendly)
- Release artifacts (`.uf2` firmware + configuration tool `.exe` + PDF user manual) via GitHub Releases

## Who this is for
This README is for users who want to **flash**, **run**, and **use** the project without digging through source code first.

## What you get
- **Output 1 / Output 2 PLL control** from the GUI
- **Persistent settings** (ADF register writes are auto-committed to EEPROM)
- **Live status** (GPS fix/lock, satellites, DOP, alarm, lock states)
- **Windows configuration tool** with Main/Details/Advamced/About tabs

## RF/output notes (important)
- **Two outputs are fully independent**: each PLL output has its own register set and can be tuned separately.
- **Frequency range**: any frequency within the ADF4351 synthesizer output range is supported in software (approximately **34.375 MHz to 4.4 GHz**).
	- Practical usable range depends on your board layout, output network, filtering, and measurement method.
- **Resolution / step size**:
	- In **Integer-N**, tuning is quantized by $f_{PFD}/\text{OUTDIV}$.
	- With default settings (10 MHz ref, R=5, so $f_{PFD}=2$ MHz), Integer-N step is $2\text{ MHz}/\text{OUTDIV}$ (for example, 62.5 kHz at OUTDIV=32).
	- In **Fractional-N**, finer step sizes are possible (GUI default channel spacing is 1 Hz), but with fractional spurs/tradeoffs.
- **Phase-noise tradeoff**:
	- **Integer-N** is generally preferred for best close-in noise / cleaner spectrum when an exact frequency is possible.
	- **Fractional-N** gives finer frequency placement when exact Integer-N is not available, but may increase spur content.
- **Intended use guidance**:
	- Phase noise is typically **good enough for injection-locking crystal multipliers**.
	- It may **not** be good enough for all **direct multiplication** chains, depending on your multiplier stages and phase-noise budget.

## Repository layout
- `firmware/` – PlatformIO project for Pico 2
- `monitor/` – Python GUI configuration tool app
- `.github/workflows/` – release build automation
- `REFERENCE_LOCKING_README.md` – technical notes on GPS reference locking, PIO timing, interrupts, and timing/error budget

## Quick start (recommended)
### 1) Download a Release
From GitHub Releases, download:
- `Twin-PLL-GPSDO-<tag>.uf2` (firmware)
- `TwinPLLGPSDOMonitor-<tag>.exe` (Windows configuration tool)

### 2) Flash firmware (`.uf2`)
1. Put Pico 2 into BOOTSEL mode.
2. Copy the `.uf2` file to the mounted RP drive.
3. Board reboots automatically.

### 3) Run the configuration tool (`.exe`)
1. Start `TwinPLLGPSDOMonitor-<tag>.exe`.
2. Select COM port and click **Connect**.
3. Use **Set O/P 1** / **Set O/P 2** to configure outputs.

---

## Build from source
## Firmware
Prerequisites:
- Python 3.11+
- PlatformIO

Commands:
```bash
cd firmware
python -m platformio run
```
Upload (example COM port):
```bash
python -m platformio run -t upload --upload-port COM7
```

## Configuration tool (Python)
Prerequisites:
- Python 3.11+

Commands:
```bash
cd monitor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python gpsdo_monitor.py
```

## Configuration tool usage notes
- **Main tab**: front-panel style status + output frequencies + output set buttons
- **Details tab**: logs, decoded registers, DAC/state details
- **About tab**: project links and author links

The configuration tool supports JSON command send for advanced/manual control.

## Persistence behavior
When a PLL/program command is applied:
- Registers are staged
- EEPROM auto-commit is performed in firmware
- On success, firmware emits a success event and the configuration tool shows **Device Updated**

## Releases and updates
On published release, GitHub Actions builds and attaches:
- Firmware UF2
- Configuration tool one-file EXE

So most users can update without local toolchains.

## Troubleshooting
- **Configuration tool cannot connect**: verify correct COM port and close other serial tools.
- **No lock / unstable GPS**: verify antenna visibility and allow warm-up time.
- **Upload issues on Windows**: retry BOOTSEL entry and ensure USB cable supports data.

## Author / links
- Robin Szemeti, G1YFG
- QRZ: https://www.qrz.com/db/G1YFG
- Blog: https://g1yfg.blogspot.com/
- Project repo: https://github.com/rszemeti/Twin-PLL-GPSDO

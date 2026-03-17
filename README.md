# Twin PLL GPSDO

A dual-output GPS-disciplined reference project for Raspberry Pi Pico 2 (RP2350), with:
- Firmware for GPSDO control + PLL programming
- A desktop monitor/config tool (Windows-friendly)
- Release artifacts (`.uf2` firmware + monitor `.exe`) via GitHub Releases

## Who this is for
This README is for users who want to **flash**, **run**, and **use** the project without digging through source code first.

## What you get
- **Output 1 / Output 2 PLL control** from the GUI
- **Persistent settings** (ADF register writes are auto-committed to EEPROM)
- **Live status** (GPS fix/lock, satellites, DOP, alarm, lock states)
- **Dark-themed monitor UI** with Main/Details/About tabs

## Repository layout
- `firmware/` – PlatformIO project for Pico 2
- `monitor/` – Python GUI monitor/config app
- `.github/workflows/` – release build automation

## Quick start (recommended)
### 1) Download a Release
From GitHub Releases, download:
- `Twin-PLL-GPSDO-<tag>.uf2` (firmware)
- `TwinPLLGPSDOMonitor-<tag>.exe` (Windows monitor)

### 2) Flash firmware (`.uf2`)
1. Put Pico 2 into BOOTSEL mode.
2. Copy the `.uf2` file to the mounted RP drive.
3. Board reboots automatically.

### 3) Run the monitor (`.exe`)
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

## Monitor (Python)
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

## Monitor usage notes
- **Main tab**: front-panel style status + output frequencies + output set buttons
- **Details tab**: logs, decoded registers, DAC/state details
- **About tab**: project links and author links

The monitor supports JSON command send for advanced/manual control.

## Persistence behavior
When a PLL/program command is applied:
- Registers are staged
- EEPROM auto-commit is performed in firmware
- On success, firmware emits a success event and monitor shows **Device Updated**

## Releases and updates
On published release, GitHub Actions builds and attaches:
- Firmware UF2
- Monitor one-file EXE

So most users can update without local toolchains.

## Troubleshooting
- **Monitor cannot connect**: verify correct COM port and close other serial tools.
- **No lock / unstable GPS**: verify antenna visibility and allow warm-up time.
- **Upload issues on Windows**: retry BOOTSEL entry and ensure USB cable supports data.

## Author / links
- Robin Szemeti, G1YFG
- QRZ: https://www.qrz.com/db/G1YFG
- Blog: https://g1yfg.blogspot.com/
- Project repo: https://github.com/rszemeti/Twin-PLL-GPSDO

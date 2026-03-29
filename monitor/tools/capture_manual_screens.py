from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication, QTabWidget


SCRIPT_DIR = Path(__file__).resolve().parent
MONITOR_DIR = SCRIPT_DIR.parent
ROOT_DIR = MONITOR_DIR.parent
OUT_DIR = ROOT_DIR / 'docs' / 'screenshots'

if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

from gpsdo_monitor import MainWindow, PLLConfigDialog, RawRegistersDialog
from adf4351_registers import ADF4351Config, ADF4351RegisterCalculator


DEMO_REGS_R0_TO_R5 = [
    0x002C8050,
    0x00008011,
    0x00004E42,
    0x00000003,
    0x00800004,
    0x00580005,
]


def _build_demo_regs(target_mhz, integer_n):
    cfg = ADF4351Config(
        ref_hz=10_000_000.0,
        r_counter=5,
        integer_n=bool(integer_n),
        channel_spacing_hz=1.0,
        prescaler='auto',
        band_select_clock_div=150,
        rf_output_power_code=3,
        noise_mode='low_noise',
        charge_pump_code=7,
    )
    solution = ADF4351RegisterCalculator(cfg).solve(float(target_mhz) * 1_000_000.0)
    return [int(v) for v in solution.registers_r0_to_r5]


def _save_widget_png(widget, out_path):
    widget.show()
    QApplication.processEvents()
    pixmap = widget.grab()
    if not pixmap.save(str(out_path), 'PNG'):
        raise RuntimeError(f'Failed to save screenshot: {out_path}')
    print(f'Saved {out_path}')


def _seed_demo_data(window):
    pll1_int_regs = _build_demo_regs(128.0, integer_n=True)
    pll2_frac_regs = _build_demo_regs(168.7537, integer_n=False)

    window.handle_json(
        {
            'gps_fix': True,
            'gps_pps': True,
            'sats': 8,
            'sats_used': 8,
            'sats_in_view': 13,
            'hdop': 0.9,
            'disc_state': 'LOCKED',
            'disc_count_err': 0.015625,
            'disc_avg_count_err': 0.015625,
            'disc_avg_window_s': 64,
            'disc_p_gain': 0.0,
            'disc_i_gain': 5.0,
            'disc_i_gain_eff': 2.5,
            'disc_warmup_s': 30,
            'count_err_sum': 1,
            'freq_ppb': 51.7,
            'measured_freq_hz': 10000000.0,
            'measured_freq_error_ppb': 0,
            'dac_value': 2565,
            'saved_dac': 2560,
            'adf1_locked': True,
            'adf2_locked': True,
            'alarm_steady': False,
            'alarm_flash': False,
            'status_interval_ms': 5000,
        }
    )
    window.handle_json(
        {
            'cmd': 'adf_regs',
            'status': 'ok',
            'name': 'adf1',
            'regs': pll1_int_regs,
        }
    )
    window.handle_json(
        {
            'cmd': 'adf_regs',
            'status': 'ok',
            'name': 'adf2',
            'regs': pll2_frac_regs,
        }
    )


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    main_window = MainWindow()
    main_window.show()
    _seed_demo_data(main_window)
    QApplication.processEvents()

    tabs = main_window.findChild(QTabWidget)
    if tabs is None:
        raise RuntimeError('Unable to locate main tab widget for screenshot capture')

    tabs.setCurrentIndex(0)
    QApplication.processEvents()
    _save_widget_png(main_window, OUT_DIR / '01-main-live.png')

    tabs.setCurrentIndex(1)
    QApplication.processEvents()
    _save_widget_png(main_window, OUT_DIR / '02-details-live.png')

    tabs.setCurrentIndex(2)
    QApplication.processEvents()
    _save_widget_png(main_window, OUT_DIR / '06-advanced-live.png')

    tabs.setCurrentIndex(3)
    QApplication.processEvents()
    _save_widget_png(main_window, OUT_DIR / '07-saverestore-live.png')

    tabs.setCurrentIndex(4)
    QApplication.processEvents()
    _save_widget_png(main_window, OUT_DIR / '03-about-live.png')

    pll_dialog = PLLConfigDialog(pll_name='PLL1', parent=main_window)
    _save_widget_png(pll_dialog, OUT_DIR / '04-set-op1-dialog.png')
    pll_dialog.close()

    regs_dialog = RawRegistersDialog(pll_name='PLL1', default_raw_regs=DEMO_REGS_R0_TO_R5, parent=main_window)
    _save_widget_png(regs_dialog, OUT_DIR / '05-registers-dialog.png')
    regs_dialog.close()

    main_window.close()
    app.quit()
    print('Capture complete.')


if __name__ == '__main__':
    main()

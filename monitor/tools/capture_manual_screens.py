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


DEMO_REGS_R0_TO_R5 = [
    0x002C8050,
    0x00008011,
    0x00004E42,
    0x00000003,
    0x00800004,
    0x00580005,
]


def _save_widget_png(widget, out_path):
    widget.show()
    QApplication.processEvents()
    pixmap = widget.grab()
    if not pixmap.save(str(out_path), 'PNG'):
        raise RuntimeError(f'Failed to save screenshot: {out_path}')
    print(f'Saved {out_path}')


def _seed_demo_data(window):
    window.handle_json(
        {
            'gps_fix': True,
            'gps_pps': True,
            'sats': 8,
            'sats_used': 8,
            'sats_in_view': 13,
            'hdop': 0.9,
            'disc_state': 'LOCKED',
            'phase_error_ns': -12,
            'disc_avg_window_s': 120,
            'disc_avg_phase_ns': -8,
            'dac_value': 2048,
            'adf1_locked': True,
            'adf2_locked': True,
            'alarm_steady': False,
            'alarm_flash': False,
        }
    )
    window.handle_json(
        {
            'cmd': 'adf_regs',
            'status': 'ok',
            'name': 'adf1',
            'regs': DEMO_REGS_R0_TO_R5,
        }
    )
    window.handle_json(
        {
            'cmd': 'adf_regs',
            'status': 'ok',
            'name': 'adf2',
            'regs': DEMO_REGS_R0_TO_R5,
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

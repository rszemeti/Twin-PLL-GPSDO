import sys
import json
import threading
import time
import re
from queue import Queue, Empty

from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                               QComboBox, QVBoxLayout, QHBoxLayout, QTextEdit,
                               QLineEdit, QGridLayout, QGroupBox, QDialog,
                               QDialogButtonBox, QFormLayout, QDoubleSpinBox,
                               QSpinBox, QCheckBox, QMessageBox, QTabWidget)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
import serial
import serial.tools.list_ports

from adf4351_registers import ADF4351RegisterCalculator, ADF4351Config
from monitor_version import VERSION as GUI_VERSION


class PLLConfigDialog(QDialog):
    def __init__(
        self,
        pll_name='PLL',
        default_freq_mhz=104.0,
        default_ref_mhz=10.0,
        default_r_counter=5,
        default_integer_n=True,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f'Set {pll_name} Frequency')
        self.setModal(True)

        form = QFormLayout()

        self.freq_mhz = QDoubleSpinBox()
        self.freq_mhz.setRange(34.375, 4400.0)
        self.freq_mhz.setDecimals(6)
        self.freq_mhz.setSingleStep(1.0)
        self.freq_mhz.setValue(default_freq_mhz)
        self.freq_mhz.setSuffix(' MHz')

        self.ref_mhz = QDoubleSpinBox()
        self.ref_mhz.setRange(0.1, 500.0)
        self.ref_mhz.setDecimals(6)
        self.ref_mhz.setSingleStep(1.0)
        self.ref_mhz.setValue(default_ref_mhz)
        self.ref_mhz.setSuffix(' MHz')

        self.r_counter = QSpinBox()
        self.r_counter.setRange(1, 1023)
        self.r_counter.setValue(default_r_counter)

        self.integer_n = QCheckBox('Use Integer-N mode')
        self.integer_n.setChecked(default_integer_n)
        self.integer_n.setVisible(False)

        self.synth_mode = QComboBox()
        self.synth_mode.addItem('Auto (prefer Integer-N, fallback Fractional-N)', 'auto')
        self.synth_mode.addItem('Integer-N only', 'int')
        self.synth_mode.addItem('Fractional-N only', 'frac')
        self.synth_mode.setCurrentIndex(0 if default_integer_n else 2)

        self.rf_power = QComboBox()
        self.rf_power.addItem('-4 dBm', 0)
        self.rf_power.addItem('-1 dBm', 1)
        self.rf_power.addItem('+2 dBm', 2)
        self.rf_power.addItem('+5 dBm', 3)
        self.rf_power.setCurrentIndex(3)

        self.noise_mode = QComboBox()
        self.noise_mode.addItem('Low noise', 'low_noise')
        self.noise_mode.addItem('Low spur', 'low_spur')
        self.noise_mode.setCurrentIndex(0)

        self.charge_pump = QComboBox()
        cp_items = [
            ('0.31 mA', 0), ('0.63 mA', 1), ('0.94 mA', 2), ('1.25 mA', 3),
            ('1.56 mA', 4), ('1.88 mA', 5), ('2.19 mA', 6), ('2.50 mA', 7),
            ('2.81 mA', 8), ('3.13 mA', 9), ('3.44 mA', 10), ('3.75 mA', 11),
            ('4.06 mA', 12), ('4.38 mA', 13), ('4.69 mA', 14), ('5.00 mA', 15),
        ]
        for label, code in cp_items:
            self.charge_pump.addItem(label, code)
        self.charge_pump.setCurrentIndex(7)

        form.addRow('Output frequency', self.freq_mhz)
        form.addRow('Reference', self.ref_mhz)
        form.addRow('R counter', self.r_counter)
        form.addRow('Synth mode', self.synth_mode)
        form.addRow('RF output power', self.rf_power)
        form.addRow('Noise mode', self.noise_mode)
        form.addRow('Charge pump current', self.charge_pump)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def values(self):
        return {
            'target_hz': self.freq_mhz.value() * 1_000_000.0,
            'ref_hz': self.ref_mhz.value() * 1_000_000.0,
            'r_counter': self.r_counter.value(),
            'integer_n': self.integer_n.isChecked(),
            'synth_mode': self.synth_mode.currentData(),
            'rf_output_power_code': self.rf_power.currentData(),
            'noise_mode': self.noise_mode.currentData(),
            'charge_pump_code': self.charge_pump.currentData(),
        }


class RawRegistersDialog(QDialog):
    def __init__(self, pll_name='PLL', default_raw_regs=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'{pll_name} Registers')
        self.setModal(True)
        self.resize(420, 300)

        self.raw_regs_text = QTextEdit()
        self.raw_regs_text.setPlaceholderText(
            'Enter 6 hex values in order R5..R0, one per line\n'
            'Example:\n0x00580005\n0x00800004\n0x00000003\n0x00004E42\n0x00008011\n0x002C8050'
        )

        if default_raw_regs and len(default_raw_regs) == 6:
            prefill = []
            for reg_index in range(5, -1, -1):
                prefill.append(f'R{reg_index}: 0x{int(default_raw_regs[reg_index]) & 0xFFFFFFFF:08X}')
            self.raw_regs_text.setPlainText('\n'.join(prefill))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(self.raw_regs_text)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def registers_r0_to_r5(self):
        lines = [line.strip() for line in self.raw_regs_text.toPlainText().splitlines() if line.strip()]
        if not lines:
            raise ValueError('Raw register list is empty')

        tokens = []
        for line in lines:
            cleaned = re.sub(r'^[Rr]\s*[0-5]\s*[:=]\s*', '', line)
            parts = [part.strip() for part in cleaned.split(',') if part.strip()]
            if not parts:
                continue
            tokens.extend(parts)

        if len(tokens) != 6:
            raise ValueError(f'Expected 6 register values (R5..R0), got {len(tokens)}')

        regs_r5_to_r0 = []
        for idx, token in enumerate(tokens):
            value_text = token[2:] if token.lower().startswith('0x') else token
            value = int(value_text, 16)
            if value < 0 or value > 0xFFFFFFFF:
                raise ValueError(f'R{5 - idx} out of range (0..0xFFFFFFFF)')
            regs_r5_to_r0.append(value)

        return list(reversed(regs_r5_to_r0))


class SerialReader(threading.Thread):
    def __init__(self, ser, out_q, stop_event):
        super().__init__(daemon=True)
        self.ser = ser
        self.out_q = out_q
        self.stop_event = stop_event

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    line = self.ser.readline()
                    if not line:
                        continue
                    try:
                        s = line.decode('utf-8', errors='replace').strip()
                    except Exception:
                        s = str(line)
                    self.out_q.put(s)
                except Exception:
                    break
        finally:
            pass


class Signals(QObject):
    json_received = Signal(dict)
    raw_received = Signal(str)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('GPSDO Monitor')
        self.resize(900, 600)

        self.signals = Signals()
        self.signals.json_received.connect(self.handle_json)
        self.signals.raw_received.connect(self.handle_raw)

        self.serial = None
        self.reader = None
        self.read_q = Queue()
        self.stop_event = threading.Event()
        self.max_serial_items_per_tick = 50
        self.latest_regs = {'adf1': None, 'adf2': None}
        self.decode_ref_hz = 10_000_000.0
        self.last_device_updated_popup_ts = 0.0
        self.status_state = {
            'gps_fix': False,
            'gps_pps': False,
            'sats': 0,
            'disc_state': '',
            'adf1_locked': False,
            'adf2_locked': False,
            'alarm_steady': False,
            'alarm_flash': False,
        }
        self.led_blink_phase = False

        self._build_ui()
        self._apply_dark_theme()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_serial_queue)
        self.poll_timer.start(100)

        self.led_timer = QTimer(self)
        self.led_timer.timeout.connect(self._tick_leds)
        self.led_timer.start(500)

    def _build_ui(self):
        top_layout = QHBoxLayout()

        self.port_combo = QComboBox()
        self.refresh_ports()
        self.refresh_btn = QPushButton('Refresh')
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(['115200', '9600'])
        self.connect_btn = QPushButton('Connect')
        self.connect_btn.clicked.connect(self.toggle_connect)

        top_layout.addWidget(QLabel('Port:'))
        top_layout.addWidget(self.port_combo)
        top_layout.addWidget(self.refresh_btn)
        top_layout.addWidget(QLabel('Baud:'))
        top_layout.addWidget(self.baud_combo)
        top_layout.addWidget(self.connect_btn)

        # Status area (details tab)
        status_box = QGroupBox('Status Details')
        grid = QGridLayout()
        self.fw_label = QLabel('')
        self.board_label = QLabel('')
        self.gps_fix = QLabel('')
        self.gps_pps = QLabel('')
        self.sats = QLabel('')
        self.sats_used = QLabel('')
        self.sats_in_view = QLabel('')
        self.hdop = QLabel('')
        self.disc_state = QLabel('')
        self.phase_error = QLabel('')
        self.disc_avg_window = QLabel('')
        self.disc_avg_phase = QLabel('')
        self.dac_value = QLabel('')
        self.adf1_locked = QLabel('')
        self.adf2_locked = QLabel('')
        self.adf1_freq = QLabel('')
        self.adf2_freq = QLabel('')

        grid.addWidget(QLabel('Firmware'), 0, 0); grid.addWidget(self.fw_label, 0, 1)
        grid.addWidget(QLabel('Board'), 1, 0); grid.addWidget(self.board_label, 1, 1)
        grid.addWidget(QLabel('GPS Fix'), 2, 0); grid.addWidget(self.gps_fix, 2, 1)
        grid.addWidget(QLabel('GPS PPS'), 3, 0); grid.addWidget(self.gps_pps, 3, 1)
        grid.addWidget(QLabel('Sats'), 4, 0); grid.addWidget(self.sats, 4, 1)
        grid.addWidget(QLabel('Sats Used'), 5, 0); grid.addWidget(self.sats_used, 5, 1)
        grid.addWidget(QLabel('Sats In View'), 6, 0); grid.addWidget(self.sats_in_view, 6, 1)
        grid.addWidget(QLabel('HDOP'), 7, 0); grid.addWidget(self.hdop, 7, 1)
        self.hdop.setStyleSheet('font-weight: 700; color: #aeb7c2;')
        grid.addWidget(QLabel('Disc State'), 8, 0); grid.addWidget(self.disc_state, 8, 1)
        grid.addWidget(QLabel('Phase error (ns)'), 9, 0); grid.addWidget(self.phase_error, 9, 1)
        grid.addWidget(QLabel('Disc avg window (s)'), 10, 0); grid.addWidget(self.disc_avg_window, 10, 1)
        grid.addWidget(QLabel('Disc avg phase (ns)'), 11, 0); grid.addWidget(self.disc_avg_phase, 11, 1)
        grid.addWidget(QLabel('DAC Value'), 12, 0); grid.addWidget(self.dac_value, 12, 1)
        grid.addWidget(QLabel('adf1_locked'), 13, 0); grid.addWidget(self.adf1_locked, 13, 1)
        grid.addWidget(QLabel('adf2_locked'), 14, 0); grid.addWidget(self.adf2_locked, 14, 1)
        grid.addWidget(QLabel('adf1 decoded'), 15, 0); grid.addWidget(self.adf1_freq, 15, 1)
        grid.addWidget(QLabel('adf2 decoded'), 16, 0); grid.addWidget(self.adf2_freq, 16, 1)

        status_box.setLayout(grid)

        # Virtual front-panel LEDs
        leds_box = QGroupBox('Virtual LEDs')
        leds_grid = QGridLayout()
        self.led_widgets = {}
        self._add_led_widget(leds_grid, 0, 0, 'gps_fix_led', 'GPS Fix')
        self._add_led_widget(leds_grid, 0, 1, 'gps_lock', 'GPS Lock')
        self._add_led_widget(leds_grid, 0, 2, 'disciplined', 'Disciplined')
        self._add_led_widget(leds_grid, 1, 2, 'alarm', 'Alarm')
        self._add_led_widget(leds_grid, 1, 0, 'adf1_lock', 'ADF1 Lock')
        self._add_led_widget(leds_grid, 1, 1, 'adf2_lock', 'ADF2 Lock')
        leds_box.setLayout(leds_grid)

        # PLL quick-control cards (main tab)
        pll1_box = QGroupBox('PLL1')
        pll1_layout = QVBoxLayout()
        pll1_layout.addWidget(QLabel('Frequency'))
        self.pll1_freq_main = QLabel('-')
        self.pll1_freq_main.setStyleSheet('font-size: 16px; font-weight: 700; color: #7ee787;')
        pll1_layout.addWidget(self.pll1_freq_main)
        self.set_pll1_btn = QPushButton('Set O/P 1')
        self.set_pll1_btn.clicked.connect(self.open_set_pll1_dialog)
        self.set_pll1_regs_btn = QPushButton('Registers')
        self.set_pll1_regs_btn.setMaximumWidth(90)
        self.set_pll1_regs_btn.clicked.connect(self.open_set_pll1_registers_dialog)
        pll1_btn_row = QHBoxLayout()
        pll1_btn_row.addWidget(self.set_pll1_btn)
        pll1_btn_row.addWidget(self.set_pll1_regs_btn)
        pll1_layout.addLayout(pll1_btn_row)
        pll1_box.setLayout(pll1_layout)

        pll2_box = QGroupBox('PLL2')
        pll2_layout = QVBoxLayout()
        pll2_layout.addWidget(QLabel('Frequency'))
        self.pll2_freq_main = QLabel('-')
        self.pll2_freq_main.setStyleSheet('font-size: 16px; font-weight: 700; color: #79c0ff;')
        pll2_layout.addWidget(self.pll2_freq_main)
        self.set_pll2_btn = QPushButton('Set O/P 2')
        self.set_pll2_btn.clicked.connect(self.open_set_pll2_dialog)
        self.set_pll2_regs_btn = QPushButton('Registers')
        self.set_pll2_regs_btn.setMaximumWidth(90)
        self.set_pll2_regs_btn.clicked.connect(self.open_set_pll2_registers_dialog)
        pll2_btn_row = QHBoxLayout()
        pll2_btn_row.addWidget(self.set_pll2_btn)
        pll2_btn_row.addWidget(self.set_pll2_regs_btn)
        pll2_layout.addLayout(pll2_btn_row)
        pll2_box.setLayout(pll2_layout)

        pll_row = QHBoxLayout()
        pll_row.addWidget(pll1_box)
        pll_row.addWidget(pll2_box)

        front_status_box = QGroupBox('Front Status')
        front_status_layout = QHBoxLayout(front_status_box)
        front_status_layout.addWidget(QLabel('Sats Used'))
        self.sats_used_main = QLabel('0')
        self.sats_used_main.setStyleSheet('font-size: 18px; font-weight: 700; color: #ffd60a;')
        front_status_layout.addWidget(self.sats_used_main)
        front_status_layout.addStretch(1)

        # ADF regs area
        self.adf1_regs_text = QTextEdit(); self.adf1_regs_text.setReadOnly(True)
        self.adf2_regs_text = QTextEdit(); self.adf2_regs_text.setReadOnly(True)
        regs_box = QGroupBox('ADF Registers')
        regs_layout = QHBoxLayout()
        regs_layout.addWidget(self.adf1_regs_text)
        regs_layout.addWidget(self.adf2_regs_text)
        regs_box.setLayout(regs_layout)

        # Log area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(2000)

        # Send command
        send_layout = QHBoxLayout()
        self.cmd_input = QLineEdit()
        self.send_btn = QPushButton('Send JSON')
        self.send_btn.clicked.connect(self.send_command)
        send_layout.addWidget(self.cmd_input)
        send_layout.addWidget(self.send_btn)

        # Main tab (visual front panel)
        main_tab = QWidget()
        main_layout = QVBoxLayout(main_tab)
        main_layout.addLayout(pll_row)
        main_layout.addWidget(front_status_box)
        main_layout.addWidget(leds_box)
        main_layout.addStretch(1)

        # Details tab (logs and internals)
        details_tab = QWidget()
        details_layout = QHBoxLayout(details_tab)

        details_left = QVBoxLayout()
        details_left.addWidget(status_box)
        details_left.addLayout(send_layout)
        details_left.addStretch(1)

        details_right = QVBoxLayout()
        details_right.addWidget(regs_box)
        details_right.addWidget(QLabel('Log'))
        details_right.addWidget(self.log_text)

        details_layout.addLayout(details_left, 1)
        details_layout.addLayout(details_right, 1)

        # About tab
        about_tab = QWidget()
        about_layout = QVBoxLayout(about_tab)

        about_project_box = QGroupBox('Project')
        about_project_layout = QVBoxLayout(about_project_box)
        project_title = QLabel('Twin PLL GPSDO Monitor')
        project_title.setStyleSheet('font-size: 16px; font-weight: 700; color: #79c0ff;')
        project_info = QLabel('Get full project details, documentation, and the latest software upgrades here:')
        project_info.setStyleSheet('color: #ffffff; font-size: 13px;')
        project_info.setWordWrap(True)
        project_repo = QLabel('<a style="color:#ffd60a;" href="https://github.com/rszemeti/Twin-PLL-GPSDO">https://github.com/rszemeti/Twin-PLL-GPSDO</a>')
        project_repo.setStyleSheet('font-size: 13px;')
        project_repo.setOpenExternalLinks(True)
        project_repo.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.about_fw_label = QLabel(GUI_VERSION)
        self.about_fw_label.setStyleSheet('font-size: 13px; color: #e6e8eb;')
        about_project_layout.addWidget(project_title)
        about_project_layout.addWidget(project_info)
        about_project_layout.addWidget(project_repo)
        about_project_layout.addWidget(QLabel('GUI Version:'))
        about_project_layout.addWidget(self.about_fw_label)

        about_author_box = QGroupBox('Author')
        about_author_layout = QVBoxLayout(about_author_box)
        author_name = QLabel('Robin Szemeti, G1YFG')
        author_name.setStyleSheet('font-size: 14px; font-weight: 600; color: #e6e8eb;')
        author_text = QLabel('Amateur radio operator and creator of this Twin PLL GPSDO project.')
        author_text.setStyleSheet('color: #ffffff; font-size: 13px;')
        author_text.setWordWrap(True)
        qrz_link = QLabel('<a style="color:#ffd60a;" href="https://www.qrz.com/db/G1YFG">QRZ: G1YFG</a>')
        qrz_link.setStyleSheet('font-size: 13px;')
        qrz_link.setOpenExternalLinks(True)
        qrz_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        blog_link = QLabel('<a style="color:#ffd60a;" href="https://g1yfg.blogspot.com/">Blog: https://g1yfg.blogspot.com/</a>')
        blog_link.setStyleSheet('font-size: 13px;')
        blog_link.setOpenExternalLinks(True)
        blog_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        about_author_layout.addWidget(author_name)
        about_author_layout.addWidget(author_text)
        about_author_layout.addWidget(qrz_link)
        about_author_layout.addWidget(blog_link)

        about_layout.addWidget(about_project_box)
        about_layout.addWidget(about_author_box)
        about_layout.addStretch(1)

        tabs = QTabWidget()
        tabs.addTab(main_tab, 'Main')
        tabs.addTab(details_tab, 'Details')
        tabs.addTab(about_tab, 'About')

        root = QVBoxLayout()
        root.addLayout(top_layout)
        root.addWidget(tabs)
        self.setLayout(root)
        self._update_virtual_leds()

    def _apply_dark_theme(self):
        self.setStyleSheet('''
            QWidget {
                background-color: #0f1115;
                color: #e6e8eb;
                font-size: 12px;
            }
            QGroupBox {
                border: 1px solid #2a2f38;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 8px;
                background-color: #141820;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: #aeb7c2;
            }
            QLabel {
                background: transparent;
            }
            QLineEdit, QComboBox, QTextEdit {
                background-color: #1a1f28;
                border: 1px solid #2a2f38;
                border-radius: 6px;
                padding: 4px 6px;
                color: #edf0f3;
                selection-background-color: #2f81f7;
            }
            QTextEdit {
                border-radius: 8px;
            }
            QPushButton {
                background-color: #242b36;
                border: 1px solid #344154;
                border-radius: 6px;
                padding: 6px 10px;
                color: #e8edf4;
            }
            QPushButton:hover {
                background-color: #2d3746;
            }
            QPushButton:pressed {
                background-color: #1f2630;
            }
        ''')

    def _add_led_widget(self, grid, row, col, key, label_text):
        dot = QLabel('●')
        dot.setAlignment(Qt.AlignCenter)
        dot.setStyleSheet('color: #3a3a3a; font-size: 20px;')

        txt = QLabel(label_text)
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(dot)
        layout.addWidget(txt)
        layout.addStretch(1)

        self.led_widgets[key] = dot
        grid.addWidget(holder, row, col)

    def _set_led(self, key, on, color):
        dot = self.led_widgets.get(key)
        if not dot:
            return
        dot.setStyleSheet(f"color: {color if on else '#3a3a3a'}; font-size: 20px;")

    def _to_bool(self, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)

    def _tick_leds(self):
        self.led_blink_phase = not self.led_blink_phase
        self._update_virtual_leds()

    def _set_hdop_visual(self, hdop_value):
        if hdop_value <= 1.5:
            color = '#32d74b'
        elif hdop_value <= 3.0:
            color = '#ffd60a'
        else:
            color = '#ff453a'
        self.hdop.setStyleSheet(f'font-weight: 700; color: {color};')

    def _update_virtual_leds(self):
        gps_fix = self.status_state['gps_fix']
        gps_lock = gps_fix and self.status_state['gps_pps']
        disciplined = self.status_state['disc_state'] in ('LOCKED', 'HOLDOVER')

        adf1_locked = self.status_state['adf1_locked']
        adf2_locked = self.status_state['adf2_locked']

        sats = int(self.status_state['sats'])
        sats_used = sats >= 4 and self.status_state['gps_fix']
        self.sats_used_main.setText(str(sats))
        if self.sats_used.text() == '':
            self.sats_used.setText(str(sats))

        alarm_steady = self.status_state['alarm_steady']
        alarm_flash = self.status_state['alarm_flash']
        alarm_on = alarm_steady or (alarm_flash and self.led_blink_phase)

        self._set_led('gps_fix_led', True, '#32d74b' if gps_fix else '#ff453a')
        self._set_led('gps_lock', True, '#32d74b' if gps_lock else '#ff453a')
        self._set_led('disciplined', disciplined, '#0a84ff')
        self._set_led('adf1_lock', True, '#32d74b' if adf1_locked else '#ff453a')
        self._set_led('adf2_lock', True, '#32d74b' if adf2_locked else '#ff453a')
        self._set_led('alarm', alarm_on, '#ff453a')

    def refresh_ports(self):
        self.port_combo.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.port_combo.addItem(p.device)

    def toggle_connect(self):
        if self.serial and self.serial.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_combo.currentText()
        baud = int(self.baud_combo.currentText())
        try:
            self.serial = serial.Serial(port, baud, timeout=0.2, write_timeout=0)
        except Exception as e:
            self.log_text.append(f'Failed to open {port}: {e}')
            return
        self.stop_event.clear()
        self.reader = SerialReader(self.serial, self.read_q, self.stop_event)
        self.reader.start()
        self.connect_btn.setText('Disconnect')
        self.log_text.append(f'Connected to {port} @ {baud}')
        self._request_info()
        self._request_adf_regs()

    def _serial_write_line(self, line, log_tx=True):
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Serial not connected')
            return False
        try:
            self.serial.write((line + '\n').encode('utf-8'))
            if log_tx:
                self.log_text.append('TX: ' + line)
            return True
        except serial.SerialTimeoutException:
            self.log_text.append('TX timeout: ' + line)
            return False
        except Exception as e:
            self.log_text.append(f'TX failed: {e}')
            return False

    def _disconnect(self):
        self.stop_event.set()
        if self.reader:
            self.reader.join(timeout=1)
            self.reader = None
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None
        self.connect_btn.setText('Connect')
        self.log_text.append('Disconnected')

    def _poll_serial_queue(self):
        for _ in range(self.max_serial_items_per_tick):
            try:
                line = self.read_q.get_nowait()
            except Empty:
                break

            try:
                obj = json.loads(line)
                self.signals.json_received.emit(obj)
            except Exception:
                self.signals.raw_received.emit(line)

    def _request_info(self):
        if not self.serial or not self.serial.is_open:
            return
        self._serial_write_line('{"cmd":"info"}')

    def _request_adf_regs(self):
        if not self.serial or not self.serial.is_open:
            return
        cmds = (
            '{"cmd":"adf1","action":"show"}',
            '{"cmd":"adf2","action":"show"}',
        )
        for cmd in cmds:
            if not self._serial_write_line(cmd):
                break

    def handle_raw(self, line):
        self.log_text.append(line)

    def handle_json(self, obj):
        try:
            # update known fields
            if 'event' in obj and obj['event'] == 'firmware_boot':
                self.fw_label.setText(obj.get('version', ''))
                self.board_label.setText(obj.get('board', ''))
                self.log_text.append(json.dumps(obj))
                return
            if obj.get('cmd') == 'info':
                self.fw_label.setText(obj.get('version', ''))
                self.board_label.setText(obj.get('board', ''))
                self.log_text.append(json.dumps(obj))
                return
            if obj.get('event') == 'saved_adf_regs':
                self.log_text.append(json.dumps(obj))
                return
            if obj.get('event') == 'eeprom_write_success':
                self.log_text.append(json.dumps(obj))
                now_ts = time.monotonic()
                if (now_ts - self.last_device_updated_popup_ts) >= 1.0:
                    self.last_device_updated_popup_ts = now_ts
                    QMessageBox.information(self, 'Device Updated', 'ADF settings were written to EEPROM successfully.')
                return
            if obj.get('action') == 'set_all' and obj.get('cmd') in ('adf1', 'adf2'):
                self.log_text.append(json.dumps(obj))
                return
            if 'status' in obj and obj.get('cmd') == 'adf_regs':
                name = obj.get('name', '')
                regs = obj.get('regs', [])
                txt = '\n'.join([f'R{i}: 0x{v:08X}' for i,v in enumerate(regs)])
                decoded_text = self._decode_adf_regs_text(regs)
                freq_mhz_text = self._decode_adf_freq_mhz_text(regs)
                if name.lower().startswith('adf1'):
                    self.latest_regs['adf1'] = [int(v) for v in regs]
                    self.adf1_regs_text.setPlainText(txt)
                    self.adf1_freq.setText(decoded_text)
                    self.pll1_freq_main.setText(freq_mhz_text)
                else:
                    self.latest_regs['adf2'] = [int(v) for v in regs]
                    self.adf2_regs_text.setPlainText(txt)
                    self.adf2_freq.setText(decoded_text)
                    self.pll2_freq_main.setText(freq_mhz_text)
                self.log_text.append(json.dumps(obj))
                return
            if 'gps_fix' in obj:
                gps_fix = self._to_bool(obj.get('gps_fix'))
                self.status_state['gps_fix'] = gps_fix
                self.gps_fix.setText('YES' if gps_fix else 'NO')
            if 'gps_pps' in obj:
                gps_pps = self._to_bool(obj.get('gps_pps'))
                self.status_state['gps_pps'] = gps_pps
                self.gps_pps.setText('YES' if gps_pps else 'NO')
            if 'sats' in obj:
                sats = int(obj.get('sats', 0))
                self.status_state['sats'] = sats
                self.sats.setText(str(sats))
            if 'sats_used' in obj:
                sats_used = int(obj.get('sats_used', 0))
                self.status_state['sats'] = sats_used
                self.sats_used.setText(str(sats_used))
                self.sats_used_main.setText(str(sats_used))
            if 'sats_in_view' in obj:
                self.sats_in_view.setText(str(int(obj.get('sats_in_view', 0))))
            if 'hdop' in obj:
                hdop_value = float(obj.get('hdop', 0.0))
                self.hdop.setText(f"{hdop_value:.1f}")
                self._set_hdop_visual(hdop_value)
            if 'disc_state' in obj:
                disc_state = str(obj.get('disc_state'))
                self.status_state['disc_state'] = disc_state
                self.disc_state.setText(disc_state)
            if 'phase_error_ns' in obj:
                self.phase_error.setText(str(obj.get('phase_error_ns')))
            if 'disc_avg_window_s' in obj:
                self.disc_avg_window.setText(str(obj.get('disc_avg_window_s')))
            if 'disc_avg_phase_ns' in obj:
                self.disc_avg_phase.setText(str(obj.get('disc_avg_phase_ns')))
            if 'dac_value' in obj:
                self.dac_value.setText(str(obj.get('dac_value')))
            if 'adf1_locked' in obj:
                adf1_locked = self._to_bool(obj.get('adf1_locked'))
                self.status_state['adf1_locked'] = adf1_locked
                self.adf1_locked.setText('YES' if adf1_locked else 'NO')
            if 'adf2_locked' in obj:
                adf2_locked = self._to_bool(obj.get('adf2_locked'))
                self.status_state['adf2_locked'] = adf2_locked
                self.adf2_locked.setText('YES' if adf2_locked else 'NO')
            if 'alarm_steady' in obj:
                self.status_state['alarm_steady'] = self._to_bool(obj.get('alarm_steady'))
            if 'alarm_flash' in obj:
                self.status_state['alarm_flash'] = self._to_bool(obj.get('alarm_flash'))

            self._update_virtual_leds()

            # Log non-periodic JSON events so firmware errors/info are visible.
            if ('gps_fix' not in obj
                    and 'gps_pps' not in obj
                    and 'sats' not in obj
                    and 'sats_used' not in obj
                    and 'sats_in_view' not in obj
                    and 'hdop' not in obj
                    and 'disc_state' not in obj
                    and 'phase_error_ns' not in obj
                    and 'dac_value' not in obj):
                self.log_text.append(json.dumps(obj))
        except Exception as e:
            self.log_text.append(f'handle_json error: {e}')

    def send_command(self):
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Not connected')
            return
        txt = self.cmd_input.text().strip()
        if not txt:
            return
        try:
            # ensure it's valid JSON
            json.loads(txt)
            self._serial_write_line(txt)
        except Exception as e:
            self.log_text.append('Invalid JSON: ' + str(e))

    def _request_adf_show_delayed(self, firmware_cmd, delay_ms=200):
        def _send_show():
            if not self.serial or not self.serial.is_open:
                return
            show_cmd = json.dumps({'cmd': firmware_cmd, 'action': 'show'})
            self._serial_write_line(show_cmd)

        QTimer.singleShot(delay_ms, _send_show)

    def _decode_adf_regs_text(self, regs):
        try:
            words = [int(v) for v in regs]
            decoded = ADF4351RegisterCalculator.decode_registers(words, ref_hz=self.decode_ref_hz)
            return (
                f"{decoded.rf_out_hz/1_000_000.0:.6f} MHz "
                f"(INT={decoded.int_value}, FRAC={decoded.frac_value}, MOD={decoded.mod_value}, "
                f"R={decoded.r_counter}, RF_DIV={decoded.rf_divider})"
            )
        except Exception:
            return 'decode failed'

    def _decode_adf_freq_mhz_text(self, regs):
        try:
            words = [int(v) for v in regs]
            decoded = ADF4351RegisterCalculator.decode_registers(words, ref_hz=self.decode_ref_hz)
            return f"{decoded.rf_out_hz/1_000_000.0:.6f} MHz"
        except Exception:
            return '-'

    def _suggest_pll_defaults(self, firmware_cmd, fallback_freq_mhz):
        defaults = {
            'freq_mhz': fallback_freq_mhz,
            'ref_mhz': self.decode_ref_hz / 1_000_000.0,
            'r_counter': 5,
            'integer_n': True,
            'rf_output_power_code': 3,
            'noise_mode': 'low_noise',
            'charge_pump_code': 7,
        }

        regs = self.latest_regs.get(firmware_cmd)
        if not regs:
            return defaults

        try:
            decoded = ADF4351RegisterCalculator.decode_registers(regs, ref_hz=self.decode_ref_hz)
            defaults['freq_mhz'] = decoded.rf_out_hz / 1_000_000.0
            defaults['integer_n'] = decoded.frac_value == 0
        except Exception:
            pass

        return defaults

    def open_set_pll1_dialog(self):
        self._open_set_pll_dialog('PLL1', 'adf1', 104.0)

    def open_set_pll2_dialog(self):
        self._open_set_pll_dialog('PLL2', 'adf2', 116.0)

    def open_set_pll1_registers_dialog(self):
        self._open_set_pll_registers_dialog('PLL1', 'adf1')

    def open_set_pll2_registers_dialog(self):
        self._open_set_pll_registers_dialog('PLL2', 'adf2')

    def _open_set_pll_registers_dialog(self, pll_name, firmware_cmd):
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Not connected')
            QMessageBox.warning(self, 'Not connected', 'Connect to the device before setting PLL registers.')
            return

        dlg = RawRegistersDialog(
            pll_name=pll_name,
            default_raw_regs=self.latest_regs.get(firmware_cmd),
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        try:
            regs_r0_to_r5 = dlg.registers_r0_to_r5()
        except Exception as e:
            msg = f'{pll_name} raw register input is invalid:\n{e}'
            QMessageBox.warning(self, f'{pll_name} Invalid Registers', msg)
            self.log_text.append(msg)
            return

        try:
            set_all_cmd = json.dumps({
                'cmd': firmware_cmd,
                'action': 'set_all',
                'regs': [int(x) for x in regs_r0_to_r5],
                'program': True,
            })
            if not self._serial_write_line(set_all_cmd):
                return
            self.log_text.append(f'{pll_name} apply sent: raw hex registers')
            self._request_adf_show_delayed(firmware_cmd, delay_ms=250)
        except Exception as e:
            self.log_text.append(f'Failed to send {pll_name} raw registers: {e}')

    def _open_set_pll_dialog(self, pll_name, firmware_cmd, default_freq_mhz):
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Not connected')
            QMessageBox.warning(self, 'Not connected', 'Connect to the device before setting PLL registers.')
            return

        suggested = self._suggest_pll_defaults(firmware_cmd, default_freq_mhz)
        dlg = PLLConfigDialog(
            pll_name=pll_name,
            default_freq_mhz=suggested['freq_mhz'],
            default_ref_mhz=suggested['ref_mhz'],
            default_r_counter=suggested['r_counter'],
            default_integer_n=suggested['integer_n'],
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()

        int_cfg = ADF4351Config(
            ref_hz=vals['ref_hz'],
            r_counter=vals['r_counter'],
            integer_n=True,
            prescaler='auto',
            band_select_clock_div=150,
            rf_output_power_code=vals.get('rf_output_power_code', 3),
            noise_mode=vals.get('noise_mode', 'low_noise'),
            charge_pump_code=vals.get('charge_pump_code', 7),
        )
        frac_cfg = ADF4351Config(
            ref_hz=vals['ref_hz'],
            r_counter=vals['r_counter'],
            integer_n=False,
            channel_spacing_hz=1.0,
            prescaler='auto',
            band_select_clock_div=150,
            rf_output_power_code=vals.get('rf_output_power_code', 3),
            noise_mode=vals.get('noise_mode', 'low_noise'),
            charge_pump_code=vals.get('charge_pump_code', 7),
        )

        int_solution = None
        frac_solution = None
        int_error = None
        frac_error = None

        try:
            int_solution = ADF4351RegisterCalculator(int_cfg).solve(vals['target_hz'])
        except Exception as e:
            int_error = str(e)

        try:
            frac_solution = ADF4351RegisterCalculator(frac_cfg).solve(vals['target_hz'])
        except Exception as e:
            frac_error = str(e)

        synth_mode = vals.get('synth_mode', 'auto')
        solution = None
        alt_solution = None
        selected_mode = ''
        alt_mode = ''

        if synth_mode == 'int':
            if int_solution is None:
                msg = f'{pll_name} Integer-N is invalid for this configuration:\n{int_error}'
                QMessageBox.warning(self, f'{pll_name} Invalid Integer-N', msg)
                self.log_text.append(msg)
                return
            solution = int_solution
            selected_mode = 'Integer-N'
            alt_solution = frac_solution
            alt_mode = 'Fractional-N'
        elif synth_mode == 'frac':
            if frac_solution is None:
                msg = f'{pll_name} Fractional-N is invalid for this configuration:\n{frac_error}'
                QMessageBox.warning(self, f'{pll_name} Invalid Fractional-N', msg)
                self.log_text.append(msg)
                return
            solution = frac_solution
            selected_mode = 'Fractional-N'
            alt_solution = int_solution
            alt_mode = 'Integer-N'
        else:
            if int_solution is not None and abs(int_solution.error_hz) <= 1e-3:
                solution = int_solution
                selected_mode = 'Integer-N (exact)'
                alt_solution = frac_solution
                alt_mode = 'Fractional-N'
            elif frac_solution is not None:
                solution = frac_solution
                selected_mode = 'Fractional-N (auto fallback)'
                alt_solution = int_solution
                alt_mode = 'Integer-N'
                if int_error:
                    note = f'{pll_name} Auto note: Integer-N unavailable ({int_error})'
                    self.log_text.append(note)
            elif int_solution is not None:
                solution = int_solution
                selected_mode = 'Integer-N (auto fallback)'
                alt_solution = None
                alt_mode = 'Fractional-N'
                if frac_error:
                    note = f'{pll_name} Auto note: Fractional-N unavailable ({frac_error})'
                    self.log_text.append(note)
            else:
                msg = (
                    f'{pll_name} configuration is invalid for both modes.\n'
                    f'Integer-N: {int_error}\nFractional-N: {frac_error}'
                )
                QMessageBox.warning(self, f'{pll_name} Invalid Configuration', msg)
                self.log_text.append(msg)
                return

        try:
            set_all_cmd = json.dumps({
                'cmd': firmware_cmd,
                'action': 'set_all',
                'regs': [int(x) for x in solution.registers_r0_to_r5],
                'program': True,
            })
            if not self._serial_write_line(set_all_cmd):
                return
            self.log_text.append(f"{pll_name} apply sent: {vals['target_hz']/1_000_000.0:.6f} MHz ({selected_mode})")
            self._request_adf_show_delayed(firmware_cmd, delay_ms=250)
            if alt_solution is not None:
                self.log_text.append(
                    f"{pll_name} alternate {alt_mode}: {alt_solution.actual_hz/1_000_000.0:.6f} MHz "
                    f"(error {alt_solution.error_hz:.3f} Hz)"
                )
        except Exception as e:
            self.log_text.append(f'Failed to send {pll_name} registers: {e}')


if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

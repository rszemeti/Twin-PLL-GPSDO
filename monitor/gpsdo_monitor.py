import sys
import json
import threading
import time
import re
from collections import deque
from queue import Queue, Empty

from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                               QComboBox, QVBoxLayout, QHBoxLayout, QTextEdit,
                               QLineEdit, QGridLayout, QGroupBox, QDialog,
                               QDialogButtonBox, QFormLayout, QDoubleSpinBox,
                               QSpinBox, QCheckBox, QMessageBox, QTabWidget,
                               QFileDialog)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
import serial
import serial.tools.list_ports

from adf4351_registers import ADF4351RegisterCalculator, ADF4351Config
from monitor_version import VERSION as GUI_VERSION


DAC_MAX_CODE = 4095
DAC_FULL_SCALE_VOLTS = 3.3


class DACHistoryWidget(QWidget):
    def __init__(self, parent=None, max_samples=180):
        super().__init__(parent)
        self.samples = deque(maxlen=max_samples)
        self.setMinimumHeight(140)

    def add_sample(self, dac_code):
        try:
            value = max(0, min(DAC_MAX_CODE, int(dac_code)))
        except Exception:
            return
        self.samples.append(value)
        self.update()

    def clear(self):
        self.samples.clear()
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.fillRect(rect, QColor('#10151d'))
        painter.setPen(QPen(QColor('#2a2f38'), 1))
        painter.drawRoundedRect(rect, 8, 8)

        if rect.width() <= 24 or rect.height() <= 24:
            return

        plot = rect.adjusted(10, 10, -10, -10)
        grid_pen = QPen(QColor('#243040'), 1)
        grid_pen.setStyle(Qt.DashLine)
        painter.setPen(grid_pen)
        for frac in (0.25, 0.5, 0.75):
            y = plot.top() + int(plot.height() * frac)
            painter.drawLine(plot.left(), y, plot.right(), y)

        label_pen = QPen(QColor('#7d8590'), 1)
        painter.setPen(label_pen)
        painter.drawText(plot.left(), plot.top() + 12, '3.30 V')
        painter.drawText(plot.left(), plot.bottom() - 4, '0.00 V')

        if len(self.samples) < 2:
            painter.setPen(QColor('#7d8590'))
            painter.drawText(plot, Qt.AlignCenter, 'Waiting for DAC telemetry')
            return

        points = []
        span = max(1, len(self.samples) - 1)
        for index, sample in enumerate(self.samples):
            x = plot.left() + int((plot.width() * index) / span)
            y_ratio = sample / DAC_MAX_CODE
            y = plot.bottom() - int(plot.height() * y_ratio)
            points.append((x, y))

        trace_pen = QPen(QColor('#79c0ff'), 2)
        painter.setPen(trace_pen)
        for idx in range(1, len(points)):
            painter.drawLine(points[idx - 1][0], points[idx - 1][1], points[idx][0], points[idx][1])

        latest_x, latest_y = points[-1]
        painter.setBrush(QColor('#ffd60a'))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(latest_x - 3, latest_y - 3, 6, 6)


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

        self.channel_step = QLabel('-')
        self.channel_step.setStyleSheet('color: #aeb7c2; font-weight: 600;')

        form.addRow('Output frequency', self.freq_mhz)
        form.addRow('Reference', self.ref_mhz)
        form.addRow('R counter', self.r_counter)
        form.addRow('Synth mode', self.synth_mode)
        form.addRow('RF output power', self.rf_power)
        form.addRow('Noise mode', self.noise_mode)
        form.addRow('Charge pump current', self.charge_pump)
        form.addRow('Channel step', self.channel_step)

        self.freq_mhz.valueChanged.connect(self._update_channel_step)
        self.ref_mhz.valueChanged.connect(self._update_channel_step)
        self.r_counter.valueChanged.connect(self._update_channel_step)
        self.synth_mode.currentIndexChanged.connect(self._update_channel_step)
        self._update_channel_step()

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

    def _format_hz(self, value_hz):
        value = float(value_hz)
        abs_value = abs(value)
        if abs_value >= 1_000_000.0:
            return f'{value / 1_000_000.0:.6f} MHz'
        if abs_value >= 1_000.0:
            return f'{value / 1_000.0:.3f} kHz'
        return f'{value:.3f} Hz'

    def _select_solution(self, int_solution, frac_solution):
        synth_mode = self.synth_mode.currentData()
        if synth_mode == 'int':
            return int_solution, 'Integer-N'
        if synth_mode == 'frac':
            return frac_solution, 'Fractional-N'
        if int_solution is not None and abs(float(int_solution.error_hz)) <= 1e-3:
            return int_solution, 'Integer-N (auto)'
        if frac_solution is not None:
            return frac_solution, 'Fractional-N (auto)'
        if int_solution is not None:
            return int_solution, 'Integer-N (auto)'
        return None, ''

    def _update_channel_step(self, *_):
        try:
            vals = self.values()
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
            try:
                int_solution = ADF4351RegisterCalculator(int_cfg).solve(vals['target_hz'])
            except Exception:
                pass
            try:
                frac_solution = ADF4351RegisterCalculator(frac_cfg).solve(vals['target_hz'])
            except Exception:
                pass

            solution, mode_label = self._select_solution(int_solution, frac_solution)
            if solution is None:
                self.channel_step.setText('Unavailable for current settings')
                return

            mod_value = int(solution.mod_value) if int(solution.mod_value) > 0 else 1
            step_hz = float(solution.pfd_hz) / float(mod_value * int(solution.output_divider))
            self.channel_step.setText(f'{mode_label}: {self._format_hz(step_hz)}')
        except Exception:
            self.channel_step.setText('-')


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
    DISC_PRESETS = {
        'slow':   {'avg_window_s': 32, 'p_gain': 0.0, 'i_gain': 2.0, 'warmup_s': 60},
        'normal': {'avg_window_s': 16, 'p_gain': 0.0, 'i_gain': 5.0, 'warmup_s': 30},
        'fast':   {'avg_window_s': 12, 'p_gain': 0.0, 'i_gain': 10.0, 'warmup_s': 10},
    }

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
        self.last_disc_updated_popup_ts = 0.0
        self.status_interval_ms = 5000
        self.latest_dac_code = None

        self.disc_ctrl_last = {
            'avg_window_s': None,
            'p_gain': None,
            'i_gain': None,
            'warmup_s': None,
        }
        self._freq_hz_samples_removed = True  # measured freq display removed
        self.status_state = {
            'gps_fix': False,
            'gps_pps': False,
            'sats': 0,
            'disc_state': '',
            'adf1_locked': False,
            'adf2_locked': False,
            'adf1_enabled': True,
            'adf2_enabled': True,
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

    def _dac_code_to_voltage(self, dac_code):
        return (float(dac_code) / DAC_MAX_CODE) * DAC_FULL_SCALE_VOLTS

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
        self.disc_avg_freq = QLabel('')
        self.dac_value = QLabel('')
        self.saved_dac = QLabel('')
        self.adf1_locked = QLabel('')
        self.adf2_locked = QLabel('')
        self.adf1_freq = QLabel('')
        self.adf2_freq = QLabel('')
        self.disc_p_gain = QLabel('')
        self.disc_i_gain = QLabel('')

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
        grid.addWidget(QLabel('Freq error (ppb)'), 9, 0); grid.addWidget(self.phase_error, 9, 1)
        grid.addWidget(QLabel('Disc avg window (s)'), 10, 0); grid.addWidget(self.disc_avg_window, 10, 1)
        grid.addWidget(QLabel('Disc avg count err (Hz)'), 11, 0); grid.addWidget(self.disc_avg_freq, 11, 1)
        grid.addWidget(QLabel('DAC Value'), 12, 0); grid.addWidget(self.dac_value, 12, 1)
        grid.addWidget(QLabel('Last Saved DAC'), 13, 0); grid.addWidget(self.saved_dac, 13, 1)
        grid.addWidget(QLabel('adf1_locked'), 14, 0); grid.addWidget(self.adf1_locked, 14, 1)
        grid.addWidget(QLabel('adf2_locked'), 15, 0); grid.addWidget(self.adf2_locked, 15, 1)
        grid.addWidget(QLabel('adf1 decoded'), 16, 0); grid.addWidget(self.adf1_freq, 16, 1)
        grid.addWidget(QLabel('adf2 decoded'), 17, 0); grid.addWidget(self.adf2_freq, 17, 1)
        grid.addWidget(QLabel('Disc P gain'), 18, 0); grid.addWidget(self.disc_p_gain, 18, 1)
        grid.addWidget(QLabel('Disc I gain'), 19, 0); grid.addWidget(self.disc_i_gain, 19, 1)

        status_box.setLayout(grid)

        disc_ctrl_box = QGroupBox('PID Tuning')
        disc_ctrl_form = QFormLayout()

        self.disc_avg_input = QSpinBox()
        self.disc_avg_input.setRange(1, 32)
        self.disc_avg_input.setValue(16)
        self.disc_avg_input.setSuffix(' s')
        self.disc_avg_input.setToolTip('Acquiring window (locked = this × 4, max 128 s)')

        self.disc_p_input = QDoubleSpinBox()
        self.disc_p_input.setDecimals(4)
        self.disc_p_input.setRange(0.0, 10.0)
        self.disc_p_input.setSingleStep(0.01)
        self.disc_p_input.setValue(0.0)

        self.disc_i_input = QDoubleSpinBox()
        self.disc_i_input.setDecimals(4)
        self.disc_i_input.setRange(0.0, 10.0)
        self.disc_i_input.setSingleStep(0.01)
        self.disc_i_input.setValue(0.05)

        self.disc_warmup_input = QSpinBox()
        self.disc_warmup_input.setRange(5, 120)
        self.disc_warmup_input.setValue(30)
        self.disc_warmup_input.setSuffix(' s')

        disc_ctrl_form.addRow('Average window', self.disc_avg_input)
        disc_ctrl_form.addRow('P gain', self.disc_p_input)
        disc_ctrl_form.addRow('I gain', self.disc_i_input)
        disc_ctrl_form.addRow('Warmup time', self.disc_warmup_input)

        disc_ctrl_btns = QHBoxLayout()
        self.disc_refresh_btn = QPushButton('Refresh')
        self.disc_refresh_btn.clicked.connect(self.request_disc_ctrl)
        self.disc_apply_btn = QPushButton('Apply')
        self.disc_apply_btn.clicked.connect(self.apply_disc_ctrl)
        disc_ctrl_btns.addWidget(self.disc_refresh_btn)
        disc_ctrl_btns.addWidget(self.disc_apply_btn)

        disc_preset_btns = QHBoxLayout()
        self.disc_preset_cons_btn = QPushButton('Slow')
        self.disc_preset_cons_btn.clicked.connect(lambda: self.apply_disc_preset('slow'))
        self.disc_preset_norm_btn = QPushButton('Normal')
        self.disc_preset_norm_btn.clicked.connect(lambda: self.apply_disc_preset('normal'))
        self.disc_preset_fast_btn = QPushButton('Fast')
        self.disc_preset_fast_btn.clicked.connect(lambda: self.apply_disc_preset('fast'))

        disc_preset_btns.addWidget(self.disc_preset_cons_btn)
        disc_preset_btns.addWidget(self.disc_preset_norm_btn)
        disc_preset_btns.addWidget(self.disc_preset_fast_btn)

        dac_test_btns = QHBoxLayout()
        self.dac_min_btn = QPushButton('DAC Min')
        self.dac_min_btn.clicked.connect(lambda: self._send_dac_preset('min'))
        self.dac_max_btn = QPushButton('DAC Max')
        self.dac_max_btn.clicked.connect(lambda: self._send_dac_preset('max'))
        self.dac_centre_btn = QPushButton('DAC Centre')
        self.dac_centre_btn.clicked.connect(lambda: self._send_dac_preset('centre'))
        self.dac_resume_btn = QPushButton('Resume')
        self.dac_resume_btn.clicked.connect(lambda: self._send_dac_preset('resume'))
        dac_test_btns.addWidget(self.dac_min_btn)
        dac_test_btns.addWidget(self.dac_max_btn)
        dac_test_btns.addWidget(self.dac_centre_btn)
        dac_test_btns.addWidget(self.dac_resume_btn)

        disc_ctrl_layout = QVBoxLayout(disc_ctrl_box)
        disc_ctrl_layout.addLayout(disc_ctrl_form)
        disc_ctrl_layout.addLayout(disc_ctrl_btns)
        presets_label = QLabel('Presets')
        presets_label.setStyleSheet('color: #aeb7c2; font-weight: 600;')
        disc_ctrl_layout.addWidget(presets_label)
        disc_ctrl_layout.addLayout(disc_preset_btns)
        dac_test_label = QLabel('DAC Test')
        dac_test_label.setStyleSheet('color: #aeb7c2; font-weight: 600;')
        disc_ctrl_layout.addWidget(dac_test_label)
        disc_ctrl_layout.addLayout(dac_test_btns)

        disc_ctrl_row = QHBoxLayout()
        disc_ctrl_row.addWidget(disc_ctrl_box, 1)

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
        self.pll1_enable_cb = QCheckBox('Enabled')
        self.pll1_enable_cb.setChecked(True)
        self.pll1_enable_cb.toggled.connect(lambda en: self._send_pll_enable(1, en))
        pll1_layout.addWidget(self.pll1_enable_cb)
        pll1_header = QHBoxLayout()
        pll1_header.addStretch(1)
        self.pll1_mode_main = QLabel('-')
        self.pll1_mode_main.setStyleSheet('font-size: 11px; font-weight: 700; color: #aeb7c2;')
        pll1_header.addWidget(self.pll1_mode_main)
        pll1_layout.addLayout(pll1_header)
        pll1_layout.addWidget(QLabel('Frequency'))
        self.pll1_freq_main = QLabel('-')
        self.pll1_freq_main.setStyleSheet('font-size: 16px; font-weight: 700; color: #79c0ff;')
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
        self.pll2_enable_cb = QCheckBox('Enabled')
        self.pll2_enable_cb.setChecked(True)
        self.pll2_enable_cb.toggled.connect(lambda en: self._send_pll_enable(2, en))
        pll2_layout.addWidget(self.pll2_enable_cb)
        pll2_header = QHBoxLayout()
        pll2_header.addStretch(1)
        self.pll2_mode_main = QLabel('-')
        self.pll2_mode_main.setStyleSheet('font-size: 11px; font-weight: 700; color: #aeb7c2;')
        pll2_header.addWidget(self.pll2_mode_main)
        pll2_layout.addLayout(pll2_header)
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

        dac_box = QGroupBox('DAC Correction')
        dac_layout = QVBoxLayout(dac_box)
        dac_readout_row = QHBoxLayout()
        dac_readout_row.addWidget(QLabel('Approx. correction'))
        dac_readout_row.addStretch(1)
        self.dac_voltage_main = QLabel('-')
        self.dac_voltage_main.setStyleSheet('font-size: 22px; font-weight: 700; color: #ffd60a;')
        dac_readout_row.addWidget(self.dac_voltage_main)
        dac_layout.addLayout(dac_readout_row)

        dac_detail_row = QHBoxLayout()
        self.dac_code_main = QLabel('Code: -')
        self.dac_code_main.setStyleSheet('color: #aeb7c2;')
        dac_detail_row.addWidget(self.dac_code_main)
        dac_detail_row.addStretch(1)
        self.dac_percent_main = QLabel('Full scale: -')
        self.dac_percent_main.setStyleSheet('color: #aeb7c2;')
        dac_detail_row.addWidget(self.dac_percent_main)
        dac_layout.addLayout(dac_detail_row)

        self.dac_history = DACHistoryWidget()
        dac_layout.addWidget(self.dac_history)

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
        main_layout.addWidget(leds_box)
        main_layout.addWidget(front_status_box)
        main_layout.addLayout(pll_row)
        main_layout.addStretch(1)

        # Details tab (status/log visibility)
        details_tab = QWidget()
        details_layout = QHBoxLayout(details_tab)

        details_left = QVBoxLayout()
        details_left.addWidget(status_box)
        details_left.addStretch(1)

        details_right = QVBoxLayout()
        details_right.addWidget(regs_box)
        details_right.addWidget(QLabel('Log'))
        details_right.addWidget(self.log_text)

        details_layout.addLayout(details_left, 1)
        details_layout.addLayout(details_right, 1)

        # Tuning status box
        tuning_box = QGroupBox('Status')
        tuning_grid = QGridLayout(tuning_box)
        tuning_grid.addWidget(QLabel('State:'), 0, 0)
        self.tuning_state_label = QLabel('-')
        self.tuning_state_label.setStyleSheet('font-size: 16px; font-weight: 700; color: #aeb7c2;')
        tuning_grid.addWidget(self.tuning_state_label, 0, 1)
        tuning_grid.addWidget(QLabel('Lock:'), 0, 2)
        self.tuning_lock_label = QLabel('-')
        self.tuning_lock_label.setStyleSheet('font-size: 16px; font-weight: 700; color: #aeb7c2;')
        tuning_grid.addWidget(self.tuning_lock_label, 0, 3)
        tuning_grid.addWidget(QLabel('I gain:'), 1, 0)
        self.tuning_igain_label = QLabel('-')
        self.tuning_igain_label.setStyleSheet('font-size: 14px; font-weight: 600; color: #79c0ff;')
        tuning_grid.addWidget(self.tuning_igain_label, 1, 1)
        tuning_grid.addWidget(QLabel('P gain:'), 1, 2)
        self.tuning_pgain_label = QLabel('-')
        self.tuning_pgain_label.setStyleSheet('font-size: 14px; font-weight: 600; color: #79c0ff;')
        tuning_grid.addWidget(self.tuning_pgain_label, 1, 3)
        tuning_grid.addWidget(QLabel('Avg window:'), 2, 0)
        self.tuning_avg_label = QLabel('-')
        self.tuning_avg_label.setStyleSheet('font-size: 14px; font-weight: 600; color: #79c0ff;')
        tuning_grid.addWidget(self.tuning_avg_label, 2, 1)
        tuning_grid.addWidget(QLabel('Avg count err:'), 2, 2)
        self.tuning_avg_count_err_label = QLabel('-')
        self.tuning_avg_count_err_label.setStyleSheet('font-size: 14px; font-weight: 600; color: #79c0ff;')
        tuning_grid.addWidget(self.tuning_avg_count_err_label, 2, 3)
        tuning_grid.addWidget(QLabel('Count err sum:'), 3, 0)
        self.tuning_count_err_sum_label = QLabel('-')
        self.tuning_count_err_sum_label.setStyleSheet('font-size: 14px; font-weight: 600; color: #79c0ff;')
        tuning_grid.addWidget(self.tuning_count_err_sum_label, 3, 1)
        tuning_grid.setColumnStretch(4, 1)

        # Advanced tab (controls and tooling)
        advanced_tab = QWidget()
        advanced_layout = QVBoxLayout(advanced_tab)
        advanced_layout.addWidget(dac_box)
        pid_status_row = QHBoxLayout()
        pid_status_row.addWidget(disc_ctrl_box, 1)
        pid_status_row.addWidget(tuning_box, 1)
        advanced_layout.addLayout(pid_status_row)
        advanced_layout.addLayout(send_layout)
        advanced_layout.addStretch(1)

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

        # Settings tab
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)

        settings_file_box = QGroupBox('Settings File')
        settings_file_layout = QVBoxLayout(settings_file_box)
        settings_info = QLabel(
            'Save or restore all device settings (discipliner tuning and PLL registers) to/from a JSON file.\n'
            'The DAC value is not included — it is unique to each oscillator.'
        )
        settings_info.setWordWrap(True)
        settings_info.setStyleSheet('color: #e6e8eb; font-size: 13px;')
        settings_file_layout.addWidget(settings_info)

        settings_btn_layout = QHBoxLayout()
        save_settings_btn = QPushButton('Save Settings to File')
        save_settings_btn.clicked.connect(self._save_settings_to_file)
        restore_settings_btn = QPushButton('Restore Settings from File')
        restore_settings_btn.clicked.connect(self._restore_settings_from_file)
        settings_btn_layout.addWidget(save_settings_btn)
        settings_btn_layout.addWidget(restore_settings_btn)
        settings_file_layout.addLayout(settings_btn_layout)

        self.settings_summary_label = QLabel('No settings file loaded or saved this session.')
        self.settings_summary_label.setWordWrap(True)
        self.settings_summary_label.setStyleSheet('color: #8b949e; font-size: 12px;')
        settings_file_layout.addWidget(self.settings_summary_label)

        settings_layout.addWidget(settings_file_box)
        settings_layout.addStretch(1)

        tabs = QTabWidget()
        tabs.addTab(main_tab, 'Main')
        tabs.addTab(details_tab, 'Details')
        tabs.addTab(advanced_tab, 'Tuning')
        tabs.addTab(settings_tab, 'Save/Restore')
        tabs.addTab(about_tab, 'About')

        root = QVBoxLayout()
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(0)
        root.addLayout(top_layout)
        root.addSpacing(8)
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
            QTabWidget::pane {
                border: 1px solid #2a2f38;
                border-radius: 8px;
                top: -1px;
                background-color: #141820;
            }
            QTabBar {
                background: transparent;
                margin-top: 4px;
            }
            QTabBar::tab {
                background-color: #1a1f28;
                color: #aeb7c2;
                border: 1px solid #2a2f38;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 8px 14px;
                margin-right: 4px;
                min-width: 84px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background-color: #141820;
                color: #e6e8eb;
                border-color: #3a4658;
            }
            QTabBar::tab:hover:!selected {
                background-color: #242b36;
                color: #edf0f3;
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

    def _update_dac_display(self, dac_code):
        try:
            code = max(0, min(DAC_MAX_CODE, int(dac_code)))
        except Exception:
            return

        volts = self._dac_code_to_voltage(code)
        pct = (100.0 * code) / DAC_MAX_CODE
        self.latest_dac_code = code
        self.dac_value.setText(str(code))
        self.dac_voltage_main.setText(f'{volts:.3f} V')
        self.dac_code_main.setText(f'Code: {code}')
        self.dac_percent_main.setText(f'Full scale: {pct:.1f}%')
        self.dac_history.add_sample(code)

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
        adf1_enabled = self.status_state['adf1_enabled']
        adf2_enabled = self.status_state['adf2_enabled']

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
        self._set_led('adf1_lock', adf1_enabled, '#32d74b' if adf1_locked else '#ff453a')
        self._set_led('adf2_lock', adf2_enabled, '#32d74b' if adf2_locked else '#ff453a')
        self._set_led('alarm', alarm_on, '#ff453a')

    def _update_tuning_state(self, state):
        state_colors = {
            'FREERUN': '#ff453a',
            'WARMUP': '#ff9f0a',
            'ACQUIRING': '#ffd60a',
            'LOCKED': '#32d74b',
            'HOLDOVER': '#0a84ff',
        }
        color = state_colors.get(state, '#aeb7c2')
        self.tuning_state_label.setText(state)
        self.tuning_state_label.setStyleSheet(f'font-size: 16px; font-weight: 700; color: {color};')
        locked = state in ('LOCKED', 'HOLDOVER')
        lock_text = 'LOCKED' if state == 'LOCKED' else ('HOLDOVER' if state == 'HOLDOVER' else 'UNLOCKED')
        lock_color = '#32d74b' if state == 'LOCKED' else ('#0a84ff' if state == 'HOLDOVER' else '#ff453a')
        self.tuning_lock_label.setText(lock_text)
        self.tuning_lock_label.setStyleSheet(f'font-size: 16px; font-weight: 700; color: {lock_color};')

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
        self.request_disc_ctrl()
        self.request_status_ctrl()

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
        self.latest_dac_code = None

        # Clear Details tab fields
        self.fw_label.setText('')
        self.board_label.setText('')
        self.gps_fix.setText('')
        self.gps_pps.setText('')
        self.sats.setText('')
        self.sats_used.setText('')
        self.sats_in_view.setText('')
        self.hdop.setText('')
        self.disc_state.setText('')
        self.phase_error.setText('')
        self.disc_avg_window.setText('')
        self.disc_avg_freq.setText('')
        self.dac_value.setText('')
        self.saved_dac.setText('')
        self.adf1_locked.setText('')
        self.adf2_locked.setText('')
        self.adf1_freq.setText('')
        self.adf2_freq.setText('')
        self.disc_p_gain.setText('')
        self.disc_i_gain.setText('')

        # Clear Main tab fields
        self.pll1_freq_main.setText('-')
        self.pll2_freq_main.setText('-')
        self.sats_used_main.setText('0')
        self.dac_voltage_main.setText('-')
        self.dac_code_main.setText('Code: -')
        self.dac_percent_main.setText('Full scale: -')
        self.dac_history.clear()

        # Reset status state and LEDs
        self.status_state = {
            'gps_fix': False,
            'gps_pps': False,
            'sats': 0,
            'disc_state': '',
            'adf1_locked': False,
            'adf2_locked': False,
            'adf1_enabled': True,
            'adf2_enabled': True,
            'alarm_steady': False,
            'alarm_flash': False,
        }
        self._update_virtual_leds()
        self.tuning_state_label.setText('-')
        self.tuning_state_label.setStyleSheet('font-size: 16px; font-weight: 700; color: #aeb7c2;')
        self.tuning_lock_label.setText('-')
        self.tuning_lock_label.setStyleSheet('font-size: 16px; font-weight: 700; color: #aeb7c2;')
        self.tuning_igain_label.setText('-')
        self.tuning_pgain_label.setText('-')
        self.tuning_avg_label.setText('-')
        self.tuning_avg_count_err_label.setText('-')
        self.tuning_count_err_sum_label.setText('-')

        self.connect_btn.setText('Connect')
        self.log_text.append('Disconnected')

    def request_status_ctrl(self):
        if not self.serial or not self.serial.is_open:
            return
        self._serial_write_line(json.dumps({'cmd': 'status_ctrl', 'action': 'get'}))

    def _send_dac_preset(self, preset):
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Not connected')
            return
        self._serial_write_line(json.dumps({'cmd': 'dac', 'value': preset}))
        self.log_text.append(f'Sent DAC {preset}')

    def request_disc_ctrl(self):
        if not self.serial or not self.serial.is_open:
            return
        self._serial_write_line(json.dumps({'cmd': 'disc_ctrl', 'action': 'get'}))

    def apply_disc_ctrl(self):
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Not connected')
            return
        payload = {
            'cmd': 'disc_ctrl',
            'action': 'set',
            'avg_window_s': int(self.disc_avg_input.value()),
            'p_gain': float(self.disc_p_input.value()),
            'i_gain': float(self.disc_i_input.value()),
            'warmup_s': int(self.disc_warmup_input.value()),
        }
        self._serial_write_line(json.dumps(payload))

    def apply_disc_preset(self, preset_name):
        preset = self.DISC_PRESETS.get(preset_name)
        if not preset:
            self.log_text.append(f'Unknown preset: {preset_name}')
            return

        self.disc_avg_input.setValue(int(preset['avg_window_s']))
        self.disc_p_input.setValue(float(preset['p_gain']))
        self.disc_i_input.setValue(float(preset['i_gain']))
        self.disc_warmup_input.setValue(int(preset['warmup_s']))
        self.log_text.append(
            f"Applying preset '{preset_name}': avg={preset['avg_window_s']}s, "
            f"P={preset['p_gain']:.4f}, I={preset['i_gain']:.4f}, warmup={preset['warmup_s']}s"
        )
        self.apply_disc_ctrl()

    def _apply_disc_ctrl_values(self, obj):
        avg = obj.get('avg_window_s') or obj.get('disc_avg_window_s')
        p_gain = obj.get('p_gain') or obj.get('disc_p_gain')
        i_gain = obj.get('i_gain') or obj.get('disc_i_gain')

        if avg is not None:
            avg_i = int(avg)
            self.disc_ctrl_last['avg_window_s'] = avg_i
            self.disc_avg_window.setText(str(avg_i))
            self.tuning_avg_label.setText(f'{avg_i} s')
            if not self.disc_avg_input.hasFocus():
                self.disc_avg_input.setValue(avg_i)

        if p_gain is not None:
            p_f = float(p_gain)
            self.disc_ctrl_last['p_gain'] = p_f
            self.disc_p_gain.setText(f'{p_f:.4f}')
            self.tuning_pgain_label.setText(f'{p_f:.4f}')
            if not self.disc_p_input.hasFocus():
                self.disc_p_input.setValue(p_f)

        if i_gain is not None:
            i_f = float(i_gain)
            self.disc_ctrl_last['i_gain'] = i_f
            self.disc_i_gain.setText(f'{i_f:.4f}')
            self.tuning_igain_label.setText(f'{i_f:.4f}')
            if not self.disc_i_input.hasFocus():
                self.disc_i_input.setValue(i_f)

        warmup = obj.get('warmup_s') or obj.get('disc_warmup_s')
        if warmup is not None:
            w_i = int(warmup)
            self.disc_ctrl_last['warmup_s'] = w_i
            if not self.disc_warmup_input.hasFocus():
                self.disc_warmup_input.setValue(w_i)

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
                self._apply_disc_ctrl_values(obj)
                if 'status_interval_ms' in obj:
                    self.status_interval_ms = int(obj.get('status_interval_ms'))
                self.log_text.append(json.dumps(obj))
                return
            if obj.get('cmd') == 'disc_ctrl':
                self._apply_disc_ctrl_values(obj)
                if obj.get('action') == 'set':
                    persist_requested = self._to_bool(obj.get('persist_requested', True))
                    if persist_requested:
                            now_ts = time.monotonic()
                            if (now_ts - self.last_disc_updated_popup_ts) >= 1.0:
                                self.last_disc_updated_popup_ts = now_ts
                                if self._to_bool(obj.get('persisted', False)):
                                    QMessageBox.information(self, 'Device Updated', 'Loop settings were written to EEPROM successfully.')
                                else:
                                    QMessageBox.warning(self, 'Persistence Warning', 'Loop settings applied, but EEPROM save failed.')
                self.log_text.append(json.dumps(obj))
                return
            if obj.get('cmd') == 'status_ctrl':
                if 'status_interval_ms' in obj:
                    self.status_interval_ms = int(obj.get('status_interval_ms'))
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
                mode_text, mode_style = self._decode_adf_mode(regs)
                if name.lower().startswith('adf1'):
                    self.latest_regs['adf1'] = [int(v) for v in regs]
                    self.adf1_regs_text.setPlainText(txt)
                    self.adf1_freq.setText(decoded_text)
                    self.pll1_freq_main.setText(freq_mhz_text)
                    self.pll1_mode_main.setText(mode_text)
                    self.pll1_mode_main.setStyleSheet(mode_style)
                else:
                    self.latest_regs['adf2'] = [int(v) for v in regs]
                    self.adf2_regs_text.setPlainText(txt)
                    self.adf2_freq.setText(decoded_text)
                    self.pll2_freq_main.setText(freq_mhz_text)
                    self.pll2_mode_main.setText(mode_text)
                    self.pll2_mode_main.setStyleSheet(mode_style)
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
                self._update_tuning_state(disc_state)
            if 'freq_error_ppb' in obj:
                self.phase_error.setText(str(obj.get('freq_error_ppb')))
            if 'disc_avg_window_s' in obj:
                avg_eff = obj.get('disc_avg_window_s')
                self.disc_avg_window.setText(str(avg_eff))
                self.tuning_avg_label.setText(f'{avg_eff} s')
            if 'disc_avg_count_err' in obj:
                avg_ce = obj.get('disc_avg_count_err')
                self.disc_avg_freq.setText(f'{avg_ce}')
                self.tuning_avg_count_err_label.setText(f'{float(avg_ce):.6f}')
            if 'count_err_sum' in obj:
                ces = obj.get('count_err_sum')
                self.tuning_count_err_sum_label.setText(str(int(ces)))
            if 'measured_freq_hz' in obj:
                pass  # measured freq display removed
            if 'measured_freq_error_ppb' in obj:
                pass  # measured freq display removed
            if 'disc_p_gain' in obj:
                p_gain = float(obj.get('disc_p_gain'))
                self.disc_p_gain.setText(f'{p_gain:.6f}')
                self.tuning_pgain_label.setText(f'{p_gain:.4f}')
            if 'disc_warmup_s' in obj:
                w = int(obj.get('disc_warmup_s'))
                self.disc_ctrl_last['warmup_s'] = w
                if not self.disc_warmup_input.hasFocus():
                    self.disc_warmup_input.setValue(w)
            if 'disc_i_gain' in obj:
                i_gain = float(obj.get('disc_i_gain'))
                self.disc_i_gain.setText(f'{i_gain:.7f}')
            if 'disc_i_gain_eff' in obj:
                i_eff = float(obj.get('disc_i_gain_eff'))
                self.tuning_igain_label.setText(f'{i_eff:.4f}')
            if 'status_interval_ms' in obj:
                self.status_interval_ms = int(obj.get('status_interval_ms'))

            if 'dac_value' in obj:
                self._update_dac_display(obj.get('dac_value'))
            if 'saved_dac' in obj:
                self.saved_dac.setText(str(obj.get('saved_dac')))
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
            if 'adf1_enabled' in obj:
                en = self._to_bool(obj.get('adf1_enabled'))
                self.status_state['adf1_enabled'] = en
                self.pll1_enable_cb.blockSignals(True)
                self.pll1_enable_cb.setChecked(en)
                self.pll1_enable_cb.blockSignals(False)
                self._update_pll_widgets_enabled(1, en)
            if 'adf2_enabled' in obj:
                en = self._to_bool(obj.get('adf2_enabled'))
                self.status_state['adf2_enabled'] = en
                self.pll2_enable_cb.blockSignals(True)
                self.pll2_enable_cb.setChecked(en)
                self.pll2_enable_cb.blockSignals(False)
                self._update_pll_widgets_enabled(2, en)

            self._update_virtual_leds()

            # Log all JSON to the log pane.
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

    def _decode_adf_mode(self, regs):
        try:
            words = [int(v) for v in regs]
            decoded = ADF4351RegisterCalculator.decode_registers(words, ref_hz=self.decode_ref_hz)
            if int(decoded.frac_value) == 0:
                return 'Int N', 'font-size: 11px; font-weight: 700; color: #7ee787;'
            return 'Frac. N', 'font-size: 11px; font-weight: 700; color: #ffd60a;'
        except Exception:
            return '-', 'font-size: 11px; font-weight: 700; color: #aeb7c2;'

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

    def _send_pll_enable(self, pll_num, enabled):
        """Send pll_ctrl set command to enable/disable a PLL at runtime."""
        if not self.serial or not self.serial.is_open:
            self.log_text.append('Not connected — cannot change PLL enable')
            # Revert checkbox to current state
            cb = self.pll1_enable_cb if pll_num == 1 else self.pll2_enable_cb
            key = f'adf{pll_num}_enabled'
            cb.blockSignals(True)
            cb.setChecked(self.status_state[key])
            cb.blockSignals(False)
            return
        key = f'adf{pll_num}_enabled'
        cmd = json.dumps({'cmd': 'pll_ctrl', 'action': 'set', key: enabled})
        self._serial_write_line(cmd)
        self.log_text.append(f'PLL{pll_num} {"enabled" if enabled else "disabled"}')

    def _update_pll_widgets_enabled(self, pll_num, enabled):
        """Grey out PLL controls when disabled."""
        if pll_num == 1:
            self.set_pll1_btn.setEnabled(enabled)
            self.set_pll1_regs_btn.setEnabled(enabled)
            self.pll1_freq_main.setStyleSheet(
                f'font-size: 16px; font-weight: 700; color: {"#79c0ff" if enabled else "#555"};')
        else:
            self.set_pll2_btn.setEnabled(enabled)
            self.set_pll2_regs_btn.setEnabled(enabled)
            self.pll2_freq_main.setStyleSheet(
                f'font-size: 16px; font-weight: 700; color: {"#79c0ff" if enabled else "#555"};')

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

    # ── Settings save / restore ──────────────────────────────────────

    def _save_settings_to_file(self):
        # Check we have data to save
        disc = self.disc_ctrl_last
        missing = []
        if disc['avg_window_s'] is None or disc['p_gain'] is None or disc['i_gain'] is None or disc['warmup_s'] is None:
            missing.append('discipliner tuning parameters')
        if self.latest_regs.get('adf1') is None:
            missing.append('ADF1 registers')
        if self.latest_regs.get('adf2') is None:
            missing.append('ADF2 registers')
        if missing:
            QMessageBox.warning(
                self, 'Incomplete Settings',
                'The following settings have not been received from the device yet:\n\n'
                + '\n'.join(f'  \u2022 {m}' for m in missing)
                + '\n\nConnect to the device and wait for telemetry before saving.'
            )
            return

        data = {
            'disc_ctrl': {
                'avg_window_s': disc['avg_window_s'],
                'p_gain': disc['p_gain'],
                'i_gain': disc['i_gain'],
                'warmup_s': disc['warmup_s'],
            },
            'adf1_regs': list(self.latest_regs['adf1']),
            'adf2_regs': list(self.latest_regs['adf2']),
            'adf1_enabled': self.status_state['adf1_enabled'],
            'adf2_enabled': self.status_state['adf2_enabled'],
        }

        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Settings', 'gpsdo_settings.json', 'JSON Files (*.json)'
        )
        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            self.log_text.append(f'Settings saved to {path}')
            self.settings_summary_label.setText(f'Saved to: {path}')
        except Exception as e:
            QMessageBox.critical(self, 'Save Error', f'Failed to save settings:\n{e}')

    def _restore_settings_from_file(self):
        if not self.serial or not self.serial.is_open:
            QMessageBox.warning(self, 'Not Connected', 'Connect to the device before restoring settings.')
            return

        path, _ = QFileDialog.getOpenFileName(
            self, 'Restore Settings', '', 'JSON Files (*.json)'
        )
        if not path:
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, 'Load Error', f'Failed to read settings file:\n{e}')
            return

        # Validate structure
        dc = data.get('disc_ctrl')
        adf1 = data.get('adf1_regs')
        adf2 = data.get('adf2_regs')
        errors = []
        if not isinstance(dc, dict) or not all(k in dc for k in ('avg_window_s', 'p_gain', 'i_gain', 'warmup_s')):
            errors.append('Missing or invalid disc_ctrl section')
        if not isinstance(adf1, list) or len(adf1) != 6:
            errors.append('Missing or invalid adf1_regs (need 6 registers)')
        if not isinstance(adf2, list) or len(adf2) != 6:
            errors.append('Missing or invalid adf2_regs (need 6 registers)')
        if errors:
            QMessageBox.warning(self, 'Invalid Settings File', '\n'.join(errors))
            return

        adf1_en = data.get('adf1_enabled', '?')
        adf2_en = data.get('adf2_enabled', '?')
        reply = QMessageBox.question(
            self, 'Restore Settings',
            f'Restore all settings from:\n{path}\n\n'
            f'Discipliner: avg={dc["avg_window_s"]}s, P={dc["p_gain"]}, I={dc["i_gain"]}, warmup={dc["warmup_s"]}s\n'
            f'PLL1 enabled: {adf1_en}   PLL2 enabled: {adf2_en}\n'
            f'ADF1 regs: {[hex(r) for r in adf1]}\n'
            f'ADF2 regs: {[hex(r) for r in adf2]}\n\n'
            'This will overwrite the current device settings. Continue?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # Send disc_ctrl
        disc_cmd = json.dumps({
            'cmd': 'disc_ctrl', 'action': 'set',
            'avg_window_s': int(dc['avg_window_s']),
            'p_gain': float(dc['p_gain']),
            'i_gain': float(dc['i_gain']),
            'warmup_s': int(dc['warmup_s']),
        })
        self._serial_write_line(disc_cmd)
        self.log_text.append('Restored discipliner tuning parameters')

        # Send ADF1
        adf1_cmd = json.dumps({
            'cmd': 'adf1', 'action': 'set_all',
            'regs': [int(r) for r in adf1], 'program': True,
        })
        self._serial_write_line(adf1_cmd)
        self.log_text.append('Restored ADF1 registers')

        # Send ADF2
        adf2_cmd = json.dumps({
            'cmd': 'adf2', 'action': 'set_all',
            'regs': [int(r) for r in adf2], 'program': True,
        })
        self._serial_write_line(adf2_cmd)
        self.log_text.append('Restored ADF2 registers')

        # Restore PLL enable state if present in the settings file
        if 'adf1_enabled' in data or 'adf2_enabled' in data:
            pll_cmd = {'cmd': 'pll_ctrl', 'action': 'set'}
            if 'adf1_enabled' in data:
                pll_cmd['adf1_enabled'] = bool(data['adf1_enabled'])
            if 'adf2_enabled' in data:
                pll_cmd['adf2_enabled'] = bool(data['adf2_enabled'])
            self._serial_write_line(json.dumps(pll_cmd))
            self.log_text.append('Restored PLL enable state')

        self.settings_summary_label.setText(f'Restored from: {path}')
        self.log_text.append(f'All settings restored from {path}')

        # Refresh displayed values after a short delay
        QTimer.singleShot(500, self._request_info)
        QTimer.singleShot(600, self._request_adf_regs)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

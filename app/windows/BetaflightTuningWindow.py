from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QComboBox,
    QDoubleSpinBox,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from betaflight.cli import BetaflightCli, CliSetting
from betaflight.filter_analysis import FilterProposal, analyze_blackbox
from betaflight.pid_analysis import PidProposal, PidTuneInput, analyze_pid_tune
from betaflight.serial import discover_serial_ports
from windows.BlackboxExtractor import BlackboxExtractorWorker


APP_TITLE = "PID Autotune"
BLACKBOX_EXPECTATIONS = {
    "blackbox_device": ("SPIFLASH", "SDCARD", "SD_CARD", "ONBOARD_FLASH"),
    "blackbox_sample_rate": ("1/2", "1:2", "2KHZ", "2000"),
    "debug_mode": ("GYRO_SCALED",),
}
BLACKBOX_FIXES = {
    "blackbox_device": "SPIFLASH",
    "blackbox_sample_rate": "1/2",
    "debug_mode": "GYRO_SCALED",
}


class CliWorker(QObject):
    log = pyqtSignal(str)
    settings_checked = pyqtSignal(list)
    applied = pyqtSignal(bool, str)
    finished = pyqtSignal()

    def __init__(self, port: str, mode: str, settings: dict[str, str] | None = None) -> None:
        super().__init__()
        self.port = port
        self.mode = mode
        self.settings = settings or {}

    def run(self) -> None:
        client: BetaflightCli | None = None
        try:
            self.log.emit(f"Opening Betaflight CLI on {self.port}...")
            client = BetaflightCli(self.port)
            if self.mode == "check_blackbox":
                checked = client.settings(BLACKBOX_EXPECTATIONS)
                self.settings_checked.emit(checked)
            elif self.mode == "apply_blackbox":
                backup = client.save_backup(Path.cwd() / "backups", "before_blackbox_setup")
                self.log.emit(f"Backup saved: {backup}")
                output = client.apply_settings(self.settings)
                self.applied.emit(True, output or "Settings applied. The flight controller is rebooting.")
            elif self.mode == "apply_filters":
                backup = client.save_backup(Path.cwd() / "backups", "before_filter_tune")
                self.log.emit(f"Backup saved: {backup}")
                output = client.apply_settings(self.settings)
                self.applied.emit(True, output or "Filter settings applied. The flight controller is rebooting.")
            elif self.mode == "apply_pid":
                backup = client.save_backup(Path.cwd() / "backups", "before_pid_tune")
                self.log.emit(f"Backup saved: {backup}")
                output = client.apply_settings(self.settings)
                self.applied.emit(True, output or "PID settings applied. The flight controller is rebooting.")
        except Exception as exc:
            self.applied.emit(False, str(exc))
        finally:
            if client is not None:
                client.close()
            self.finished.emit()


class ExtractTab(QWidget):
    log = pyqtSignal(str)

    def __init__(self, get_port) -> None:
        super().__init__()
        self.get_port = get_port
        self.worker: BlackboxExtractorWorker | None = None
        self.worker_thread: QThread | None = None

        self.output_dir_input = QLineEdit(str(Path.cwd() / "blackbox"))
        self.output_dir_button = QPushButton("Browse")
        self.file_name_input = QLineEdit(self.default_file_name())
        self.extract_button = QPushButton("Extract Blackbox")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status_label = QLabel("Ready to extract a Betaflight Blackbox log.")
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)

        self._build()
        self.output_dir_button.clicked.connect(self.choose_output_dir)
        self.extract_button.clicked.connect(self.extract_blackbox)
        self.stop_button.clicked.connect(self.stop_extraction)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)

        title = QLabel("Blackbox Extractor")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Extract the onboard Dataflash into a local .bbl file.")
        subtitle.setObjectName("subtitle")

        form = QFormLayout()
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_dir_input, stretch=1)
        output_row.addWidget(self.output_dir_button)
        form.addRow("Folder", output_row)
        form.addRow("File name", self.file_name_input)

        action_row = QHBoxLayout()
        action_row.addWidget(self.extract_button)
        action_row.addWidget(self.stop_button)
        action_row.addStretch(1)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addLayout(form)
        root.addLayout(action_row)
        root.addWidget(self.progress_bar)
        root.addWidget(self.status_label)
        root.addWidget(self.log_output, stretch=1)

    def choose_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_dir_input.text())
        if selected:
            self.output_dir_input.setText(selected)

    def extract_blackbox(self) -> None:
        port = self.get_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        file_name = self.file_name_input.text().strip()
        if not file_name:
            QMessageBox.warning(self, "Missing file name", "Enter a Blackbox file name.")
            return

        output_path = Path(self.output_dir_input.text()).expanduser() / file_name
        self.worker_thread = QThread(self)
        self.worker = BlackboxExtractorWorker(port, output_path)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.extraction_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.cleanup_worker)

        self.set_extracting(True)
        self.progress_bar.setValue(0)
        self.append_log(f"Selected port: {port}")
        self.append_log(f"Output file: {output_path}")
        self.worker_thread.start()

    def stop_extraction(self) -> None:
        if self.worker is not None:
            self.append_log("Stop requested.")
            self.worker.cancel()

    def extraction_finished(self, success: bool, message: str) -> None:
        self.set_extracting(False)
        self.status_label.setText("Extraction complete." if success else "Extraction failed.")
        self.append_log(message)
        if success:
            self.progress_bar.setValue(100)

    def cleanup_worker(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.worker_thread is not None:
            self.worker_thread.deleteLater()
        self.worker = None
        self.worker_thread = None

    def set_extracting(self, extracting: bool) -> None:
        self.extract_button.setEnabled(not extracting)
        self.stop_button.setEnabled(extracting)
        self.status_label.setText("Extracting..." if extracting else "Ready.")

    def append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)
        self.log.emit(message)

    @staticmethod
    def default_file_name() -> str:
        return f"blackbox_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bbl"


class BlackboxSetupTab(QWidget):
    def __init__(self, get_port, start_cli_worker) -> None:
        super().__init__()
        self.get_port = get_port
        self.start_cli_worker = start_cli_worker
        self.table = QTableWidget(0, 4)
        self.check_button = QPushButton("Check Blackbox Setup")
        self.apply_button = QPushButton("Apply Required Setup")
        self.apply_button.setEnabled(False)
        self.pending_fixes: dict[str, str] = {}
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self._build()
        self.check_button.clicked.connect(self.check_setup)
        self.apply_button.clicked.connect(self.apply_setup)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("Blackbox Setup")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Required for filter tuning: onboard flash or SD card, 2 kHz logging, debug mode GYRO_SCALED.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")
        self.table.setHorizontalHeaderLabels(("Setting", "Current", "Expected", "Status"))
        self.table.horizontalHeader().setStretchLastSection(True)

        action_row = QHBoxLayout()
        action_row.addWidget(self.check_button)
        action_row.addWidget(self.apply_button)
        action_row.addStretch(1)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addLayout(action_row)
        root.addWidget(self.table)
        root.addWidget(self.log_output, stretch=1)

    def check_setup(self) -> None:
        port = self.get_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        self.start_cli_worker(port, "check_blackbox")

    def apply_setup(self) -> None:
        port = self.get_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        if not self.pending_fixes:
            QMessageBox.information(self, "Blackbox setup", "No Blackbox changes are required.")
            return
        self.start_cli_worker(port, "apply_blackbox", self.pending_fixes)

    def set_results(self, settings: list[CliSetting]) -> None:
        self.table.setRowCount(len(settings))
        all_ok = True
        self.pending_fixes = {}
        for row, setting in enumerate(settings):
            status = "OK" if setting.ok else "Needs change"
            all_ok = all_ok and setting.ok
            if not setting.ok and setting.name in BLACKBOX_FIXES:
                self.pending_fixes[setting.name] = BLACKBOX_FIXES[setting.name]
            values = (setting.name, setting.value or "Not found", " or ".join(setting.expected), status)
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.apply_button.setEnabled(not all_ok)
        self.append_log("Blackbox setup is ready." if all_ok else "Some Blackbox settings need changes.")

    def append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)


class FilterTuneTab(QWidget):
    def __init__(self, get_port, start_cli_worker) -> None:
        super().__init__()
        self.get_port = get_port
        self.start_cli_worker = start_cli_worker
        self.proposal: FilterProposal | None = None

        self.file_input = QLineEdit()
        self.browse_button = QPushButton("Browse")
        self.analyze_button = QPushButton("Analyze Blackbox")
        self.apply_button = QPushButton("Apply Proposed Filters")
        self.apply_button.setEnabled(False)
        self.summary_label = QLabel("Select a raw .bbl log or a decoded Blackbox CSV for frequency-based filter proposals.")
        self.summary_label.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self._build()
        self.browse_button.clicked.connect(self.choose_file)
        self.analyze_button.clicked.connect(self.analyze)
        self.apply_button.clicked.connect(self.apply_filters)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("Filter Tuning")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Review the proposed filter values before writing them to Betaflight.")
        subtitle.setObjectName("subtitle")

        file_row = QHBoxLayout()
        file_row.addWidget(self.file_input, stretch=1)
        file_row.addWidget(self.browse_button)
        file_row.addWidget(self.analyze_button)

        self.table.setHorizontalHeaderLabels(("Setting", "Proposed value"))
        self.table.horizontalHeader().setStretchLastSection(True)

        action_row = QHBoxLayout()
        action_row.addWidget(self.apply_button)
        action_row.addStretch(1)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addLayout(file_row)
        root.addWidget(self.summary_label)
        root.addWidget(self.table)
        root.addLayout(action_row)
        root.addWidget(self.log_output, stretch=1)

    def choose_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Blackbox log",
            str(Path.cwd() / "blackbox"),
            "Blackbox files (*.csv *.bbl);;All files (*.*)",
        )
        if selected:
            self.file_input.setText(selected)

    def analyze(self) -> None:
        path = Path(self.file_input.text()).expanduser()
        if not path.exists():
            QMessageBox.warning(self, "Missing Blackbox file", "Choose an existing Blackbox file first.")
            return
        try:
            self.proposal = analyze_blackbox(path)
        except Exception as exc:
            self.apply_button.setEnabled(False)
            self.summary_label.setText("Blackbox analysis failed.")
            self.log_output.clear()
            self.append_log(str(exc))
            QMessageBox.warning(self, "Blackbox analysis failed", str(exc))
            return
        self.summary_label.setText(self.proposal.summary)
        self.table.setRowCount(len(self.proposal.settings))
        for row, (name, value) in enumerate(self.proposal.settings.items()):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(value))
        self.log_output.clear()
        for detail in self.proposal.details:
            self.append_log(detail)
        self.apply_button.setEnabled(True)

    def apply_filters(self) -> None:
        port = self.get_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        if self.proposal is None:
            QMessageBox.warning(self, "No proposal", "Analyze a Blackbox log before applying filters.")
            return
        self.start_cli_worker(port, "apply_filters", self.proposal.settings)

    def append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)


class PidTuneTab(QWidget):
    def __init__(self, get_port, start_cli_worker) -> None:
        super().__init__()
        self.get_port = get_port
        self.start_cli_worker = start_cli_worker
        self.proposal: PidProposal | None = None

        self.file_input = QLineEdit()
        self.browse_button = QPushButton("Browse")
        self.weight_input = QDoubleSpinBox()
        self.weight_input.setRange(20, 20000)
        self.weight_input.setDecimals(0)
        self.weight_input.setSuffix(" g")
        self.weight_input.setValue(650)
        self.prop_input = QDoubleSpinBox()
        self.prop_input.setRange(1.0, 13.0)
        self.prop_input.setDecimals(1)
        self.prop_input.setSingleStep(0.5)
        self.prop_input.setSuffix("\"")
        self.prop_input.setValue(5.0)
        self.style_select = QComboBox()
        self.style_select.addItems(("Freestyle", "Racing", "Cinematic", "Heavy freestyle"))
        self.analyze_button = QPushButton("Analyze PID Tune")
        self.apply_button = QPushButton("Apply Proposed PID")
        self.apply_button.setEnabled(False)
        self.summary_label = QLabel("Select a raw .bbl log and describe the airframe to generate PID recommendations.")
        self.summary_label.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self._build()
        self.browse_button.clicked.connect(self.choose_file)
        self.analyze_button.clicked.connect(self.analyze)
        self.apply_button.clicked.connect(self.apply_pid)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("PID Tuning")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Generate a BF 4.5 PID proposal from the Blackbox log, airframe weight, prop size, and desired flight style.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")

        file_row = QHBoxLayout()
        file_row.addWidget(self.file_input, stretch=1)
        file_row.addWidget(self.browse_button)

        form = QFormLayout()
        form.addRow("Blackbox log", file_row)
        form.addRow("Drone weight", self.weight_input)
        form.addRow("Prop size", self.prop_input)
        form.addRow("Flight style", self.style_select)

        action_row = QHBoxLayout()
        action_row.addWidget(self.analyze_button)
        action_row.addWidget(self.apply_button)
        action_row.addStretch(1)

        self.table.setHorizontalHeaderLabels(("Setting", "Proposed value"))
        self.table.horizontalHeader().setStretchLastSection(True)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addLayout(form)
        root.addLayout(action_row)
        root.addWidget(self.summary_label)
        root.addWidget(self.table)
        root.addWidget(self.log_output, stretch=1)

    def choose_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Blackbox log",
            str(Path.cwd() / "blackbox"),
            "Blackbox files (*.bbl);;All files (*.*)",
        )
        if selected:
            self.file_input.setText(selected)

    def analyze(self) -> None:
        path = Path(self.file_input.text()).expanduser()
        if not path.exists():
            QMessageBox.warning(self, "Missing Blackbox file", "Choose an existing Blackbox file first.")
            return
        tune_input = PidTuneInput(
            weight_g=self.weight_input.value(),
            prop_size_in=self.prop_input.value(),
            flight_style=self.style_select.currentText(),
        )
        try:
            self.proposal = analyze_pid_tune(path, tune_input)
        except Exception as exc:
            self.apply_button.setEnabled(False)
            self.summary_label.setText("PID analysis failed.")
            self.log_output.clear()
            self.append_log(str(exc))
            QMessageBox.warning(self, "PID analysis failed", str(exc))
            return

        self.summary_label.setText(self.proposal.summary)
        self.table.setRowCount(len(self.proposal.settings))
        for row, (name, value) in enumerate(self.proposal.settings.items()):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(value))
        self.log_output.clear()
        for detail in self.proposal.details:
            self.append_log(detail)
        self.apply_button.setEnabled(True)

    def apply_pid(self) -> None:
        port = self.get_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        if self.proposal is None:
            QMessageBox.warning(self, "No proposal", "Analyze a Blackbox log before applying PID settings.")
            return
        self.start_cli_worker(port, "apply_pid", self.proposal.settings)

    def append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)


class BetaflightTuningWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.worker_thread: QThread | None = None
        self.worker: CliWorker | None = None
        self.extract_worker_thread: QThread | None = None
        self.extract_worker: BlackboxExtractorWorker | None = None
        self.active_cli_mode: str | None = None
        self.blackbox_path: Path | None = None
        self.pending_fixes: dict[str, str] = {}
        self.filter_proposal: FilterProposal | None = None
        self.pid_proposal: PidProposal | None = None
        self.pending_extract_output_path: Path | None = None

        self.setWindowTitle(APP_TITLE)
        self.resize(1120, 860)

        self.port_select = QComboBox()
        self.refresh_button = QPushButton("Refresh")

        self.setup_table = QTableWidget(0, 4)
        self.check_setup_button = QPushButton("Check Blackbox Setup")
        self.apply_setup_button = QPushButton("Apply Required Setup")
        self.apply_setup_button.setEnabled(False)
        self.output_dir_input = QLineEdit(str(Path.cwd() / "blackbox"))
        self.output_dir_button = QPushButton("Browse")
        self.file_name_input = QLineEdit(self.default_file_name())
        self.extract_button = QPushButton("Extract Blackbox")
        self.stop_extract_button = QPushButton("Stop")
        self.stop_extract_button.setEnabled(False)
        self.load_blackbox_button = QPushButton("Load .bbl")
        self.selected_blackbox_label = QLabel("No Blackbox loaded.")
        self.selected_blackbox_label.setObjectName("subtitle")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.extract_status_label = QLabel("Ready.")
        self.setup_log = QPlainTextEdit()
        self.setup_log.setReadOnly(True)

        self.filter_analyze_button = QPushButton("Analyze Filters")
        self.filter_apply_button = QPushButton("Apply Proposed Filters")
        self.filter_apply_button.setEnabled(False)
        self.filter_summary_label = QLabel("Load a Blackbox file to enable filter tuning.")
        self.filter_summary_label.setWordWrap(True)
        self.filter_summary_label.setObjectName("subtitle")
        self.filter_table = QTableWidget(0, 2)
        self.filter_log = QPlainTextEdit()
        self.filter_log.setReadOnly(True)

        self.pid_weight_input = QLineEdit("650")
        self.pid_prop_select = QComboBox()
        self.pid_prop_select.addItems(('7"', '6"', '5.5"', '5"', '4"', '3.5"', '3"', '2.5"'))
        self.pid_prop_select.setCurrentText('5"')
        self.pid_style_select = QComboBox()
        self.pid_style_select.addItems(("Freestyle", "Racing", "Cinematic", "Heavy freestyle"))
        self.pid_analyze_button = QPushButton("Analyze PID Tune")
        self.pid_apply_button = QPushButton("Apply Proposed PID")
        self.pid_apply_button.setEnabled(False)
        self.pid_summary_label = QLabel("Load a Blackbox file to enable PID tuning.")
        self.pid_summary_label.setWordWrap(True)
        self.pid_summary_label.setObjectName("subtitle")
        self.pid_table = QTableWidget(0, 2)
        self.pid_log = QPlainTextEdit()
        self.pid_log.setReadOnly(True)

        self._build()
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.check_setup_button.clicked.connect(self.check_blackbox_setup)
        self.apply_setup_button.clicked.connect(self.apply_blackbox_setup)
        self.output_dir_button.clicked.connect(self.choose_output_dir)
        self.extract_button.clicked.connect(self.extract_blackbox)
        self.stop_extract_button.clicked.connect(self.stop_extraction)
        self.load_blackbox_button.clicked.connect(self.load_blackbox_file)
        self.filter_analyze_button.clicked.connect(self.analyze_filters)
        self.filter_apply_button.clicked.connect(self.apply_filters)
        self.pid_analyze_button.clicked.connect(self.analyze_pid)
        self.pid_apply_button.clicked.connect(self.apply_pid)
        self.refresh_ports()
        self.update_blackbox_dependent_controls()

    def _build(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        header = QGridLayout()
        title = QLabel("PID Autotune")
        title.setObjectName("title")
        subtitle = QLabel("Betaflight Blackbox setup, extraction, filter tuning, and PID tuning assistant.")
        subtitle.setObjectName("subtitle")

        port_box = QGroupBox("Flight controller")
        port_layout = QHBoxLayout(port_box)
        port_layout.addWidget(self.port_select, stretch=1)
        port_layout.addWidget(self.refresh_button)

        header.addWidget(title, 0, 0)
        header.addWidget(subtitle, 1, 0)
        header.addWidget(port_box, 0, 1, 2, 1)
        header.setColumnStretch(0, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setSpacing(16)
        page_layout.addWidget(self._build_blackbox_group())
        page_layout.addWidget(self._build_filter_group())
        page_layout.addWidget(self._build_pid_group())
        page_layout.addStretch(1)
        scroll.setWidget(page)

        outer.addLayout(header)
        outer.addWidget(scroll, stretch=1)
        self.setCentralWidget(central)
        self.setStyleSheet(
            """
            QWidget { font-size: 14px; }
            QLabel#title { font-size: 30px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 24px; font-weight: 700; }
            QLabel#subtitle { color: #50565f; }
            QGroupBox {
                font-weight: 700;
                border: 1px solid #cfd4dc;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QPlainTextEdit {
                background: #111318;
                color: #f1f3f5;
                border: 1px solid #2c313a;
                border-radius: 6px;
                padding: 10px;
                font-family: Consolas, monospace;
            }
            QPushButton { min-height: 34px; padding: 0 14px; }
            QTableWidget { gridline-color: #d4d7dc; }
            """
        )

    def _build_blackbox_group(self) -> QGroupBox:
        group = QGroupBox("Blackbox Setup")
        root = QVBoxLayout(group)
        root.setSpacing(12)

        setup_actions = QHBoxLayout()
        setup_actions.addWidget(self.check_setup_button)
        setup_actions.addWidget(self.apply_setup_button)
        setup_actions.addStretch(1)

        self.setup_table.setHorizontalHeaderLabels(("Setting", "Current", "Expected", "Status"))
        self.setup_table.horizontalHeader().setStretchLastSection(True)
        self.setup_table.setMinimumHeight(130)

        extract_form = QFormLayout()
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_dir_input, stretch=1)
        output_row.addWidget(self.output_dir_button)
        extract_form.addRow("Extract folder", output_row)
        extract_form.addRow("Extract file name", self.file_name_input)

        blackbox_actions = QHBoxLayout()
        blackbox_actions.addWidget(self.extract_button)
        blackbox_actions.addWidget(self.stop_extract_button)
        blackbox_actions.addWidget(self.load_blackbox_button)
        blackbox_actions.addStretch(1)

        root.addLayout(setup_actions)
        root.addWidget(self.setup_table)
        root.addLayout(extract_form)
        root.addLayout(blackbox_actions)
        root.addWidget(self.progress_bar)
        root.addWidget(self.extract_status_label)
        root.addWidget(self.selected_blackbox_label)
        root.addWidget(self.setup_log)
        return group

    def _build_filter_group(self) -> QGroupBox:
        group = QGroupBox("Filter Tuning")
        root = QVBoxLayout(group)
        actions = QHBoxLayout()
        actions.addWidget(self.filter_analyze_button)
        actions.addWidget(self.filter_apply_button)
        actions.addStretch(1)
        self.filter_table.setHorizontalHeaderLabels(("Setting", "Proposed value"))
        self.filter_table.horizontalHeader().setStretchLastSection(True)
        self.filter_table.setMinimumHeight(180)
        root.addLayout(actions)
        root.addWidget(self.filter_summary_label)
        root.addWidget(self.filter_table)
        root.addWidget(self.filter_log)
        return group

    def _build_pid_group(self) -> QGroupBox:
        group = QGroupBox("PID Tuning")
        root = QVBoxLayout(group)
        form = QFormLayout()
        form.addRow("Prop size", self.pid_prop_select)
        form.addRow("Drone weight", self.pid_weight_input)
        form.addRow("Tune type", self.pid_style_select)
        actions = QHBoxLayout()
        actions.addWidget(self.pid_analyze_button)
        actions.addWidget(self.pid_apply_button)
        actions.addStretch(1)
        self.pid_table.setHorizontalHeaderLabels(("Setting", "Proposed value"))
        self.pid_table.horizontalHeader().setStretchLastSection(True)
        self.pid_table.setMinimumHeight(220)
        root.addLayout(form)
        root.addLayout(actions)
        root.addWidget(self.pid_summary_label)
        root.addWidget(self.pid_table)
        root.addWidget(self.pid_log)
        return group

    def refresh_ports(self) -> None:
        current_device = self.port_select.currentData()
        self.port_select.clear()
        ports = discover_serial_ports()
        for port in ports:
            self.port_select.addItem(port.label, port.device)
        if not ports:
            self.port_select.addItem("No serial port detected", None)
        elif current_device:
            index = self.port_select.findData(current_device)
            if index >= 0:
                self.port_select.setCurrentIndex(index)

    def selected_port(self) -> str | None:
        return self.port_select.currentData()

    def check_blackbox_setup(self) -> None:
        port = self.selected_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        self.start_cli_worker(port, "check_blackbox")

    def apply_blackbox_setup(self) -> None:
        port = self.selected_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        if not self.pending_fixes:
            QMessageBox.information(self, "Blackbox setup", "No Blackbox changes are required.")
            return
        self.start_cli_worker(port, "apply_blackbox", self.pending_fixes)

    def set_blackbox_results(self, settings: list[CliSetting]) -> None:
        self.setup_table.setRowCount(len(settings))
        all_ok = True
        self.pending_fixes = {}
        for row, setting in enumerate(settings):
            status = "OK" if setting.ok else "Needs change"
            all_ok = all_ok and setting.ok
            if not setting.ok and setting.name in BLACKBOX_FIXES:
                self.pending_fixes[setting.name] = BLACKBOX_FIXES[setting.name]
            values = (setting.name, setting.value or "Not found", " or ".join(setting.expected), status)
            for column, value in enumerate(values):
                self.setup_table.setItem(row, column, QTableWidgetItem(value))
        self.apply_setup_button.setEnabled(not all_ok)
        self.append_setup_log("Blackbox setup is ready." if all_ok else "Some Blackbox settings need changes.")

    def choose_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_dir_input.text())
        if selected:
            self.output_dir_input.setText(selected)

    def load_blackbox_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Load Blackbox log",
            str(Path.cwd() / "blackbox"),
            "Blackbox files (*.bbl);;All files (*.*)",
        )
        if selected:
            self.set_blackbox_path(Path(selected))

    def set_blackbox_path(self, path: Path) -> None:
        self.blackbox_path = path
        self.selected_blackbox_label.setText(f"Loaded Blackbox: {path}")
        self.filter_proposal = None
        self.pid_proposal = None
        self.filter_apply_button.setEnabled(False)
        self.pid_apply_button.setEnabled(False)
        self.filter_summary_label.setText("Blackbox loaded. Analyze filters when ready.")
        self.pid_summary_label.setText("Blackbox loaded. Enter airframe details, then analyze PID tune.")
        self.filter_table.setRowCount(0)
        self.pid_table.setRowCount(0)
        self.filter_log.clear()
        self.pid_log.clear()
        self.update_blackbox_dependent_controls()

    def update_blackbox_dependent_controls(self) -> None:
        has_blackbox = self.blackbox_path is not None
        for widget in (
            self.filter_analyze_button,
            self.pid_weight_input,
            self.pid_prop_select,
            self.pid_style_select,
            self.pid_analyze_button,
        ):
            widget.setEnabled(has_blackbox)
        self.filter_apply_button.setEnabled(has_blackbox and self.filter_proposal is not None)
        self.pid_apply_button.setEnabled(has_blackbox and self.pid_proposal is not None)

    def extract_blackbox(self) -> None:
        if self.extract_worker_thread is not None:
            QMessageBox.information(self, "Busy", "A Blackbox extraction is already running.")
            return
        port = self.selected_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        file_name = self.file_name_input.text().strip()
        if not file_name:
            QMessageBox.warning(self, "Missing file name", "Enter a Blackbox file name.")
            return

        output_path = Path(self.output_dir_input.text()).expanduser() / file_name
        self.pending_extract_output_path = output_path
        self.extract_worker_thread = QThread(self)
        self.extract_worker = BlackboxExtractorWorker(port, output_path)
        self.extract_worker.moveToThread(self.extract_worker_thread)
        self.extract_worker_thread.started.connect(self.extract_worker.run)
        self.extract_worker.log.connect(self.append_setup_log)
        self.extract_worker.progress.connect(self.progress_bar.setValue)
        self.extract_worker.finished.connect(self.extraction_finished)
        self.extract_worker.finished.connect(self.extract_worker_thread.quit)
        self.extract_worker_thread.finished.connect(self.cleanup_extract_worker)

        self.set_extracting(True)
        self.progress_bar.setValue(0)
        self.append_setup_log(f"Selected port: {port}")
        self.append_setup_log(f"Output file: {output_path}")
        self.extract_worker_thread.start()

    def stop_extraction(self) -> None:
        if self.extract_worker is not None:
            self.append_setup_log("Stop requested.")
            self.extract_worker.cancel()

    def extraction_finished(self, success: bool, message: str) -> None:
        self.set_extracting(False)
        self.extract_status_label.setText("Extraction complete." if success else "Extraction failed.")
        self.append_setup_log(message)
        if success:
            self.progress_bar.setValue(100)
            if self.blackbox_path is None and self.pending_extract_output_path is not None:
                self.set_blackbox_path(self.pending_extract_output_path)

    def cleanup_extract_worker(self) -> None:
        if self.extract_worker is not None:
            self.extract_worker.deleteLater()
        if self.extract_worker_thread is not None:
            self.extract_worker_thread.deleteLater()
        self.extract_worker = None
        self.extract_worker_thread = None
        self.pending_extract_output_path = None

    def set_extracting(self, extracting: bool) -> None:
        self.extract_button.setEnabled(not extracting)
        self.load_blackbox_button.setEnabled(not extracting)
        self.stop_extract_button.setEnabled(extracting)
        self.extract_status_label.setText("Extracting..." if extracting else "Ready.")

    def analyze_filters(self) -> None:
        if self.blackbox_path is None:
            QMessageBox.warning(self, "Missing Blackbox", "Load or extract a Blackbox file first.")
            return
        try:
            self.filter_proposal = analyze_blackbox(self.blackbox_path)
        except Exception as exc:
            self.filter_apply_button.setEnabled(False)
            self.filter_summary_label.setText("Blackbox analysis failed.")
            self.filter_log.clear()
            self.append_filter_log(str(exc))
            QMessageBox.warning(self, "Blackbox analysis failed", str(exc))
            return
        self.filter_summary_label.setText(self.filter_proposal.summary)
        self.filter_table.setRowCount(len(self.filter_proposal.settings))
        for row, (name, value) in enumerate(self.filter_proposal.settings.items()):
            self.filter_table.setItem(row, 0, QTableWidgetItem(name))
            self.filter_table.setItem(row, 1, QTableWidgetItem(value))
        self.filter_log.clear()
        for detail in self.filter_proposal.details:
            self.append_filter_log(detail)
        self.update_blackbox_dependent_controls()

    def apply_filters(self) -> None:
        port = self.selected_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        if self.filter_proposal is None:
            QMessageBox.warning(self, "No proposal", "Analyze the loaded Blackbox before applying filters.")
            return
        self.start_cli_worker(port, "apply_filters", self.filter_proposal.settings)

    def analyze_pid(self) -> None:
        if self.blackbox_path is None:
            QMessageBox.warning(self, "Missing Blackbox", "Load or extract a Blackbox file first.")
            return
        try:
            weight_g = float(self.pid_weight_input.text().replace(",", "."))
        except ValueError:
            QMessageBox.warning(self, "Invalid weight", "Enter the drone weight in grams.")
            return
        prop_size = float(self.pid_prop_select.currentText().replace('"', ""))
        tune_input = PidTuneInput(
            weight_g=weight_g,
            prop_size_in=prop_size,
            flight_style=self.pid_style_select.currentText(),
        )
        try:
            self.pid_proposal = analyze_pid_tune(self.blackbox_path, tune_input)
        except Exception as exc:
            self.pid_apply_button.setEnabled(False)
            self.pid_summary_label.setText("PID analysis failed.")
            self.pid_log.clear()
            self.append_pid_log(str(exc))
            QMessageBox.warning(self, "PID analysis failed", str(exc))
            return
        self.pid_summary_label.setText(self.pid_proposal.summary)
        self.pid_table.setRowCount(len(self.pid_proposal.settings))
        for row, (name, value) in enumerate(self.pid_proposal.settings.items()):
            self.pid_table.setItem(row, 0, QTableWidgetItem(name))
            self.pid_table.setItem(row, 1, QTableWidgetItem(value))
        self.pid_log.clear()
        for detail in self.pid_proposal.details:
            self.append_pid_log(detail)
        self.update_blackbox_dependent_controls()

    def apply_pid(self) -> None:
        port = self.selected_port()
        if not port:
            QMessageBox.warning(self, "Missing serial port", "Select a Betaflight serial port first.")
            return
        if self.pid_proposal is None:
            QMessageBox.warning(self, "No proposal", "Analyze the loaded Blackbox before applying PID settings.")
            return
        self.start_cli_worker(port, "apply_pid", self.pid_proposal.settings)

    def append_setup_log(self, message: str) -> None:
        if message:
            self.setup_log.appendPlainText(message)

    def append_filter_log(self, message: str) -> None:
        if message:
            self.filter_log.appendPlainText(message)

    def append_pid_log(self, message: str) -> None:
        if message:
            self.pid_log.appendPlainText(message)

    def start_cli_worker(self, port: str, mode: str, settings: dict[str, str] | None = None) -> None:
        if self.worker_thread is not None:
            QMessageBox.information(self, "Busy", "A Betaflight operation is already running.")
            return

        self.active_cli_mode = mode
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.worker_thread = QThread(self)
        self.worker = CliWorker(port, mode, settings)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.route_log)
        self.worker.settings_checked.connect(self.set_blackbox_results)
        self.worker.applied.connect(self.operation_applied)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.cleanup_worker)
        self.worker_thread.start()

    def route_log(self, message: str) -> None:
        if self.active_cli_mode in ("check_blackbox", "apply_blackbox"):
            self.append_setup_log(message)
        elif self.active_cli_mode == "apply_filters":
            self.append_filter_log(message)
        elif self.active_cli_mode == "apply_pid":
            self.append_pid_log(message)

    def operation_applied(self, success: bool, message: str) -> None:
        self.route_log(message)
        if not success:
            QMessageBox.warning(self, "Betaflight operation failed", message)
        elif self.active_cli_mode == "apply_blackbox":
            self.apply_setup_button.setEnabled(False)

    def cleanup_worker(self) -> None:
        QApplication.restoreOverrideCursor()
        if self.worker is not None:
            self.worker.deleteLater()
        if self.worker_thread is not None:
            self.worker_thread.deleteLater()
        self.worker = None
        self.worker_thread = None
        self.active_cli_mode = None

    @staticmethod
    def default_file_name() -> str:
        return f"blackbox_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bbl"

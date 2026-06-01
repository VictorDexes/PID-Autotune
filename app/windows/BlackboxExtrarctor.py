import struct

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QComboBox,
    QVBoxLayout,
    QWidget,
)

from betaflight.serial import (
    READ_CHUNK_SIZE,
    MSP_DATAFLASH_READ,
    MSP_DATAFLASH_SUMMARY,
    MspClient,
    MspError,
    discover_serial_ports,
    parse_dataflash_read,
    parse_dataflash_summary
)

APP_TITLE = "PID Autotune - Blackbox Extractor"

class BlackboxExtractorWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(self, port: str, output_path: Path) -> None:
        super().__init__()
        self.port = port
        self.output_path = output_path
        self.cancel_requested = False

    def cancel(self) -> None:
        self.cancel_requested = True

    def run(self) -> None:
        client: MspClient | None = None
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.log.emit(f"Ouverture du port {self.port}...")
            client = MspClient(self.port)

            self.log.emit("Lecture du resume Dataflash...")
            summary = parse_dataflash_summary(client.command(MSP_DATAFLASH_SUMMARY))
            self.log.emit(
                "Dataflash: "
                f"ready={summary.ready}, supported={summary.supported}, "
                f"used={summary.used_size} bytes, total={summary.total_size} bytes"
            )

            if not summary.supported:
                raise MspError("Ce controleur ne declare pas de Dataflash compatible.")
            if not summary.ready:
                raise MspError("La Dataflash n'est pas prete.")
            if summary.used_size <= 0:
                raise MspError("Aucune Blackbox n'est presente dans la Dataflash.")

            written = 0
            with self.output_path.open("wb") as output_file:
                while written < summary.used_size:
                    if self.cancel_requested:
                        raise MspError("Extraction annulee.")

                    size = min(READ_CHUNK_SIZE, summary.used_size - written)
                    request = struct.pack("<IH", written, size)
                    payload = client.command(MSP_DATAFLASH_READ, request)
                    data = parse_dataflash_read(payload, written)
                    if not data:
                        raise MspError("Bloc Dataflash vide.")

                    output_file.write(data[:size])
                    written += min(len(data), size)
                    self.progress.emit(int((written / summary.used_size) * 100))

            self.finished.emit(True, f"Extraction terminee: {self.output_path}")
        except Exception as exc:
            self.finished.emit(False, str(exc))
        finally:
            if client is not None:
                client.close()


class BlackboxExtractorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.worker: BlackboxExtractorWorker | None = None
        self.worker_thread: QThread | None = None

        self.setWindowTitle(APP_TITLE)
        self.resize(860, 620)

        self.port_select = QComboBox()
        self.refresh_button = QPushButton("Rafraichir")
        self.output_dir_input = QLineEdit(str(Path.cwd() / "blackbox"))
        self.output_dir_button = QPushButton("Parcourir")
        self.file_name_input = QLineEdit(self.default_file_name())
        self.extract_button = QPushButton("Extraire la Blackbox")
        self.stop_button = QPushButton("Arreter")
        self.stop_button.setEnabled(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status_label = QLabel("Pret a extraire une Blackbox Betaflight.")
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Les messages d'extraction apparaitront ici.")

        self.build_layout()
        self.connect_signals()
        self.refresh_ports()

    def build_layout(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Extraction Blackbox")
        title.setObjectName("title")
        subtitle = QLabel(
            "Connecte un controleur de vol Betaflight en USB, choisis le port, "
            "puis lance l'extraction vers un fichier .bbl local."
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)

        port_row = QHBoxLayout()
        port_row.addWidget(self.port_select, stretch=1)
        port_row.addWidget(self.refresh_button)
        form.addRow("Port USB", port_row)

        output_row = QHBoxLayout()
        output_row.addWidget(self.output_dir_input, stretch=1)
        output_row.addWidget(self.output_dir_button)
        form.addRow("Dossier", output_row)
        form.addRow("Nom du fichier", self.file_name_input)

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
        self.setCentralWidget(central)

        self.setStyleSheet(
            """
            QWidget {
                font-size: 14px;
            }
            QLabel#title {
                font-size: 28px;
                font-weight: 700;
            }
            QLabel#subtitle {
                color: #50565f;
            }
            QPlainTextEdit {
                background: #111318;
                color: #f1f3f5;
                border: 1px solid #2c313a;
                border-radius: 6px;
                padding: 10px;
                font-family: Consolas, monospace;
            }
            QPushButton {
                min-height: 34px;
                padding: 0 14px;
            }
            """
        )

    def connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.output_dir_button.clicked.connect(self.choose_output_dir)
        self.extract_button.clicked.connect(self.extract_blackbox)
        self.stop_button.clicked.connect(self.stop_extraction)

    def refresh_ports(self) -> None:
        current_device = self.port_select.currentData()
        self.port_select.clear()

        ports = discover_serial_ports()
        for port in ports:
            self.port_select.addItem(port.label, port.device)

        if not ports:
            self.port_select.addItem("Aucun port detecte", None)
            self.append_log(
                "Aucun port serie detecte. Verifie que le controleur est branche "
                "en USB et que pyserial est installe."
            )
        elif current_device:
            index = self.port_select.findData(current_device)
            if index >= 0:
                self.port_select.setCurrentIndex(index)

    def choose_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choisir le dossier de sortie",
            self.output_dir_input.text(),
        )
        if selected:
            self.output_dir_input.setText(selected)

    def extract_blackbox(self) -> None:
        port = self.port_select.currentData()
        if not port:
            QMessageBox.warning(
                self,
                "Port USB manquant",
                "Aucun port serie Betaflight n'est selectionne.",
            )
            return

        output_dir = Path(self.output_dir_input.text()).expanduser()
        file_name = self.file_name_input.text().strip()
        if not file_name:
            QMessageBox.warning(
                self,
                "Nom de fichier manquant",
                "Indique un nom pour le fichier Blackbox extrait.",
            )
            return

        output_path = output_dir / file_name
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
        self.append_log(f"Port selectionne: {port}")
        self.append_log(f"Fichier de sortie: {output_path}")
        self.worker_thread.start()

    def stop_extraction(self) -> None:
        if self.worker is not None:
            self.append_log("Arret demande.")
            self.worker.cancel()

    def extraction_finished(self, success: bool, message: str) -> None:
        self.set_extracting(False)
        self.status_label.setText("Extraction terminee." if success else "Extraction echouee.")
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
        self.refresh_button.setEnabled(not extracting)
        self.stop_button.setEnabled(extracting)
        self.status_label.setText("Extraction en cours..." if extracting else "Pret.")

    def append_log(self, message: str) -> None:
        if not message:
            return
        self.log_output.appendPlainText(message)

    @staticmethod
    def default_file_name() -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"blackbox_{timestamp}.bbl"

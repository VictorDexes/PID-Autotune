from __future__ import annotations

import sys

from PyQt6.QtWidgets import (
    QApplication,
)

from windows.BetaflightTuningWindow import BetaflightTuningWindow

def main() -> int:
    app = QApplication(sys.argv)
    window = BetaflightTuningWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

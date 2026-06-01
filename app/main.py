from __future__ import annotations

import sys

from PyQt6.QtWidgets import (
    QApplication,
)

from windows.BlackboxExtrarctor import BlackboxExtractorWindow

def main() -> int:
    app = QApplication(sys.argv)
    window = BlackboxExtractorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

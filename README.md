# PID-Autotune

Python/PyQt6 desktop app for Betaflight Blackbox extraction and first-pass filter tuning.

## Features

- Shared serial port selector for the connected Betaflight flight controller.
- Blackbox setup check for filter tuning:
  - logging device: onboard flash or SD card
  - logging rate: 2 kHz target
  - debug mode: `GYRO_SCALED`
- One-click Blackbox setup apply through the Betaflight CLI.
- Automatic `diff all` backup before applying Blackbox or filter settings.
- Dataflash extraction to a local `.bbl` file.
- Filter tuning page that proposes BF 4.5 filter values before applying them.

Raw `.bbl` frequency parsing is not implemented yet. Export a decoded CSV from Blackbox Explorer for log-based peak detection; selecting a `.bbl` file currently proposes the conservative BF 4.5 baseline from the supplied tuning PDF.

## Installation

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python app\main.py
```

## Workflow

1. Connect the Betaflight flight controller over USB.
2. Select the serial port.
3. Open `Blackbox Setup` and click `Check Blackbox Setup`.
4. If required, click `Apply Required Setup`; a backup is created in `backups`.
5. Use `Extract` to save a `.bbl` log.
6. Use `Filter Tuning` with a decoded CSV for analysis, review the proposed values, then apply them when ready.

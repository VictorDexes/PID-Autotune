# PID-Autotune

Python/PyQt6 desktop app for Betaflight Blackbox extraction, first-pass filter tuning, and PID tuning.

## Features

- Shared serial port selector for the connected Betaflight flight controller.
- Blackbox setup check for filter tuning:
  - logging device: onboard flash or SD card
  - logging rate: 2 kHz target
  - debug mode: `GYRO_SCALED`
- One-click Blackbox setup apply through the Betaflight CLI.
- Automatic `diff all` backup before applying Blackbox, filter, or PID settings.
- Dataflash extraction to a local `.bbl` file.
- Single-page workflow with shared Blackbox loading for setup, extraction, filter tuning, and PID tuning.
- Filter tuning section that parses raw `.bbl` logs or decoded CSV logs and proposes BF 4.5 filter values before applying them.
- PID tuning section that uses a raw `.bbl` log plus drone weight, prop size, and desired flight style to propose BF 4.5 PID values before applying them.

The parser extracts Betaflight Blackbox headers plus `I`/`P` main frames, then uses gyro and motor fields to estimate frame resonance, RPM filter fade-in, dynamic notch count, motor headroom, and airframe-sensitive PID changes. If parsing fails, the app falls back to conservative BF 4.5 baselines from the supplied tuning PDFs.

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
3. In `Blackbox Setup`, click `Check Blackbox Setup`.
4. If required, click `Apply Required Setup`; a backup is created in `backups`.
5. Extract a `.bbl` log or load an existing one. A successful extraction auto-selects the file when no Blackbox is already loaded.
6. Use `Filter Tuning`, review the proposed values, then apply them when ready.
7. Use `PID Tuning`, enter weight, prop size, and flight style, review the proposed values, then apply them when ready.

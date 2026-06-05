from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep

try:
    import serial
except ImportError:
    serial = None

from betaflight.serial import MspError


CLI_BAUDRATE = 115200
READ_TIMEOUT = 0.25


@dataclass(frozen=True)
class CliSetting:
    name: str
    value: str | None
    expected: tuple[str, ...]
    ok: bool


class BetaflightCli:
    def __init__(self, port: str, baudrate: int = CLI_BAUDRATE) -> None:
        if serial is None:
            raise MspError("pyserial is not installed.")

        self.connection = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=READ_TIMEOUT,
            write_timeout=2,
        )
        self._enter_cli()

    def close(self) -> None:
        if self.connection.is_open:
            self.connection.close()

    def _enter_cli(self) -> None:
        self.connection.reset_input_buffer()
        self.connection.write(b"#\r\n")
        self.connection.flush()
        self._read_until_prompt(timeout=5)

    def _read_until_prompt(self, timeout: float = 4) -> str:
        end_at = monotonic() + timeout
        received = bytearray()

        while monotonic() < end_at:
            chunk = self.connection.read(256)
            if chunk:
                received.extend(chunk)
                text = received.decode(errors="replace")
                if text.rstrip().endswith("#"):
                    return text
            else:
                sleep(0.02)

        text = received.decode(errors="replace")
        if text:
            return text
        raise MspError("Timeout waiting for Betaflight CLI prompt.")

    def command(self, command: str, timeout: float = 4) -> str:
        self.connection.write(command.encode("ascii", errors="ignore") + b"\r\n")
        self.connection.flush()
        return self._clean_response(command, self._read_until_prompt(timeout=timeout))

    def _clean_response(self, command: str, response: str) -> str:
        lines = response.replace("\r", "").split("\n")
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "#" or stripped == command:
                continue
            if stripped.startswith("# "):
                stripped = stripped[2:].strip()
            cleaned.append(stripped)
        return "\n".join(cleaned)

    def get(self, name: str) -> str | None:
        output = self.command(f"get {name}")
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip()
        return None

    def settings(self, expectations: dict[str, tuple[str, ...]]) -> list[CliSetting]:
        results: list[CliSetting] = []
        for name, expected in expectations.items():
            value = self.get(name)
            normalized = value.upper() if value else ""
            accepted = tuple(item.upper() for item in expected)
            results.append(
                CliSetting(
                    name=name,
                    value=value,
                    expected=expected,
                    ok=normalized in accepted,
                )
            )
        return results

    def diff_all(self) -> str:
        return self.command("diff all", timeout=8)

    def save_backup(self, backup_dir: Path, prefix: str) -> Path:
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{prefix}_{timestamp}.txt"
        backup_path.write_text(self.diff_all(), encoding="utf-8")
        return backup_path

    def apply_settings(self, settings: dict[str, str], save: bool = True) -> str:
        output: list[str] = []
        for name, value in settings.items():
            output.append(self.command(f"set {name} = {value}"))
        if save:
            output.append(self.command("save", timeout=2))
        return "\n".join(item for item in output if item)

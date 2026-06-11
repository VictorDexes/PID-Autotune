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
        self.reboot_expected = False
        self.in_cli = False
        self._enter_cli()

    def close(self) -> None:
        try:
            if self.connection.is_open:
                self.exit_cli()
                self.connection.close()
        except Exception:
            pass

    def _enter_cli(self) -> None:
        self.connection.reset_input_buffer()
        self.connection.write(b"#\r\n")
        self.connection.flush()
        self._read_until_prompt(timeout=5)
        self.in_cli = True

    def exit_cli(self) -> None:
        if self.reboot_expected or not self.in_cli:
            return
        try:
            self.connection.write(b"exit\r\n")
            self.connection.flush()
            sleep(0.2)
        except Exception:
            pass
        finally:
            self.in_cli = False

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

    def save_and_reboot(self) -> str:
        self.reboot_expected = True
        self.in_cli = False
        try:
            self.connection.write(b"save\r\n")
            self.connection.flush()
            response = self._read_until_disconnect_or_prompt(timeout=3)
            cleaned = self._clean_response("save", response)
            if cleaned:
                return f"{cleaned}\nFlight controller reboot detected."
        except Exception as exc:
            if _is_expected_reboot_error(exc):
                return "Settings saved. Flight controller reboot detected."
            raise
        return "Settings saved. Flight controller is rebooting."

    def _read_until_disconnect_or_prompt(self, timeout: float) -> str:
        end_at = monotonic() + timeout
        received = bytearray()

        while monotonic() < end_at:
            try:
                chunk = self.connection.read(256)
            except Exception as exc:
                if _is_expected_reboot_error(exc):
                    break
                raise
            if chunk:
                received.extend(chunk)
                text = received.decode(errors="replace")
                if text.rstrip().endswith("#"):
                    return text
            else:
                sleep(0.02)

        return received.decode(errors="replace")

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
            output.append(self.save_and_reboot())
        return "\n".join(item for item in output if item)


def _is_expected_reboot_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "clearcommerror" in text
        or "device reports readiness to read but returned no data" in text
        or "the handle is invalid" in text
        or "access is denied" in text
        or "port is closed" in text
        or "permissionerror" in type(exc).__name__.lower()
        or "serialexception" in type(exc).__name__.lower()
    )

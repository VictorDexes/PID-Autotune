import struct
from dataclasses import dataclass

try:
    import serial
    from serial.tools import list_ports
    
except ImportError:
    serial = None
    list_ports = None

MSP_DATAFLASH_SUMMARY = 70
MSP_DATAFLASH_READ = 71
READ_CHUNK_SIZE = 192

@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str

    @property
    def label(self) -> str:
        if self.description and self.description != "n/a":
            return f"{self.device} - {self.description}"
        return self.device


@dataclass(frozen=True)
class DataflashSummary:
    ready: bool
    supported: bool
    sectors: int
    total_size: int
    used_size: int


class MspError(RuntimeError):
    pass


class MspClient:
    def __init__(self, port: str, baudrate: int = 115200) -> None:
        if serial is None:
            raise MspError("pyserial is not installed.")

        self.connection = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=2,
            write_timeout=2,
        )

    def close(self) -> None:
        self.connection.close()

    def command(self, command_id: int, payload: bytes = b"") -> bytes:
        if len(payload) > 255:
            raise MspError("Payload MSP v1 too large.")

        frame = bytearray(b"$M<")
        frame.append(len(payload))
        frame.append(command_id)
        checksum = len(payload) ^ command_id
        for item in payload:
            checksum ^= item
            frame.append(item)
        frame.append(checksum)

        self.connection.reset_input_buffer()
        self.connection.write(frame)
        self.connection.flush()
        return self._read_response(command_id)

    def _read_response(self, expected_command_id: int) -> bytes:
        header = self._read_exact(3)
        if header != b"$M>":
            if header == b"$M!":
                raise MspError(f"Betaflight has refused the command {expected_command_id}.")
            raise MspError(f"Invalid MSP response: {header!r}")

        payload_size = self._read_exact(1)[0]
        command_id = self._read_exact(1)[0]
        payload = self._read_exact(payload_size)
        received_checksum = self._read_exact(1)[0]

        checksum = payload_size ^ command_id
        for item in payload:
            checksum ^= item

        if checksum != received_checksum:
            raise MspError("Invalid MSP checksum.")
        if command_id != expected_command_id:
            raise MspError(
                f"Unexpected command: {command_id}, expect {expected_command_id}."
            )

        return payload

    def _read_exact(self, size: int) -> bytes:
        data = self.connection.read(size)
        if len(data) != size:
            raise MspError("Timeout during MSP read.")
        return data


def discover_serial_ports() -> list[SerialPort]:
    if list_ports is None:
        return []

    return [
        SerialPort(device=port.device, description=port.description or "n/a")
        for port in list_ports.comports()
    ]


def parse_dataflash_summary(payload: bytes) -> DataflashSummary:
    if len(payload) < 13:
        raise MspError("Dataflash summary is too short.")

    flags = payload[0]
    sectors, total_size, used_size = struct.unpack_from("<III", payload, 1)
    return DataflashSummary(
        ready=bool(flags & 0x01),
        supported=bool(flags & 0x02),
        sectors=sectors,
        total_size=total_size,
        used_size=used_size,
    )


def parse_dataflash_read(payload: bytes, expected_address: int) -> bytes:
    if len(payload) < 4:
        raise MspError("Dataflash block is too short.")

    address = struct.unpack_from("<I", payload, 0)[0]
    if address != expected_address:
        raise MspError(
            f"Unexpected Dataflash address: {address}, expected {expected_address}."
        )
    return payload[4:]

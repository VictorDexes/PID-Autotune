from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


LOG_START = b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n"
MAX_SAMPLES = 16000


@dataclass(frozen=True)
class BlackboxSample:
    time: float
    gyro: float
    gyro_unfiltered: float | None
    motors: tuple[int, ...]


@dataclass(frozen=True)
class BlackboxParseResult:
    samples: list[BlackboxSample]
    headers: dict[str, str]
    warnings: list[str]


@dataclass
class _FieldFormat:
    names: list[str]
    signed: list[int]
    predictor: list[int]
    encoding: list[int]


class BlackboxParseError(RuntimeError):
    pass


class _ByteReader:
    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def read_u8(self) -> int:
        if self.offset >= len(self.data):
            raise EOFError
        value = self.data[self.offset]
        self.offset += 1
        return value

    def read_bytes(self, count: int) -> bytes:
        if self.offset + count > len(self.data):
            raise EOFError
        value = self.data[self.offset : self.offset + count]
        self.offset += count
        return value

    def read_unsigned_vb(self) -> int:
        shift = 0
        value = 0
        while True:
            byte = self.read_u8()
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value
            shift += 7
            if shift > 35:
                raise BlackboxParseError("Variable-byte value is too long.")

    def read_signed_vb(self) -> int:
        return _zigzag_decode(self.read_unsigned_vb())


class _BitReader:
    def __init__(self, reader: _ByteReader) -> None:
        self.reader = reader
        self.current = 0
        self.mask = 0

    def read_bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            if self.mask == 0:
                self.current = self.reader.read_u8()
                self.mask = 0x80
            value = (value << 1) | (1 if self.current & self.mask else 0)
            self.mask >>= 1
        return value


class _BlackboxDecoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.headers: dict[str, str] = {}
        self.format_i: _FieldFormat | None = None
        self.format_p: _FieldFormat | None = None
        self.last_values: list[int] = []
        self.previous_values: list[int] = []
        self.last_time_us: int | None = None
        self.warnings: list[str] = []

    def parse(self) -> BlackboxParseResult:
        start = self.data.find(LOG_START)
        if start < 0:
            raise BlackboxParseError("Blackbox log start marker was not found.")

        payload_offset = self._load_log_headers(start)

        if self.format_i is None or self.format_p is None:
            raise BlackboxParseError("Blackbox field definitions are incomplete.")

        samples: list[BlackboxSample] = []
        reader = _ByteReader(self.data, payload_offset)
        consecutive_errors = 0

        while reader.remaining() > 0 and len(samples) < MAX_SAMPLES:
            frame_offset = reader.offset
            try:
                frame_type = reader.read_u8()
                if frame_type == ord("I"):
                    values = self._decode_frame(reader, self.format_i)
                    sample = self._sample_from_values(values)
                    self._store_history(values, sample, reset_history=True)
                    if sample is not None:
                        samples.append(sample)
                    consecutive_errors = 0
                elif frame_type == ord("P"):
                    if not self.last_values:
                        continue
                    values = self._decode_frame(reader, self.format_p)
                    sample = self._sample_from_values(values)
                    self._store_history(values, sample, reset_history=False)
                    if sample is not None:
                        samples.append(sample)
                    consecutive_errors = 0
                elif frame_type == ord("E"):
                    next_log_offset = self._skip_event(reader)
                    if next_log_offset is not None:
                        payload_offset = self._load_log_headers(next_log_offset)
                        reader.offset = payload_offset
                        consecutive_errors = 0
                elif frame_type in (ord("S"), ord("G"), ord("H")):
                    self._skip_to_next_main_frame(reader)
                elif frame_type in (0x00, 0xFF):
                    continue
                else:
                    self._resync(reader, frame_offset + 1)
                    consecutive_errors += 1
            except (EOFError, BlackboxParseError, OverflowError, ValueError) as exc:
                consecutive_errors += 1
                self.warnings.append(f"Skipped corrupt frame near byte {frame_offset}: {exc}")
                self._resync(reader, frame_offset + 1, keyframes_only=True)

            if consecutive_errors > 200:
                break

        if not samples:
            raise BlackboxParseError("No usable Blackbox main frames were decoded.")

        return BlackboxParseResult(samples=samples, headers=self.headers, warnings=self.warnings[:8])

    def _load_log_headers(self, start: int) -> int:
        payload_offset = self._parse_headers(start)
        self._build_formats()
        self.last_values = []
        self.previous_values = []
        self.last_time_us = None
        return payload_offset

    def _parse_headers(self, start: int) -> int:
        offset = start
        while offset < len(self.data):
            if self.data[offset : offset + 1] != b"H":
                return offset
            newline = self.data.find(b"\n", offset)
            if newline < 0:
                raise BlackboxParseError("Blackbox header is not terminated.")
            line = self.data[offset : newline].decode("latin1", errors="replace")
            if not line.startswith("H "):
                return offset
            body = line[2:]
            if ":" in body:
                name, value = body.split(":", 1)
                self.headers[name.strip()] = value.strip()
            offset = newline + 1
        return offset

    def _build_formats(self) -> None:
        names = _csv_header(self.headers.get("Field I name", ""))
        signed = _int_header(self.headers.get("Field I signed", ""), len(names))
        i_predictor = _int_header(self.headers.get("Field I predictor", ""), len(names))
        i_encoding = _int_header(self.headers.get("Field I encoding", ""), len(names))
        p_predictor = _int_header(self.headers.get("Field P predictor", ""), len(names))
        p_encoding = _int_header(self.headers.get("Field P encoding", ""), len(names))

        if names:
            self.format_i = _FieldFormat(names, signed, i_predictor, i_encoding)
            self.format_p = _FieldFormat(names, signed, p_predictor, p_encoding)

    def _decode_frame(self, reader: _ByteReader, fmt: _FieldFormat) -> list[int]:
        values = [0] * len(fmt.names)
        index = 0
        while index < len(fmt.names):
            encoding = fmt.encoding[index]
            if encoding == 0:
                remainders = [reader.read_signed_vb()]
                width = 1
            elif encoding == 1:
                remainders = [reader.read_unsigned_vb()]
                width = 1
            elif encoding == 3:
                remainders = [-reader.read_unsigned_vb()]
                width = 1
            elif encoding == 7:
                remainders = self._read_tag2_3s32(reader)
                width = min(3, len(fmt.names) - index)
            elif encoding == 8:
                remainders = self._read_tag8_4s16(reader)
                width = min(4, len(fmt.names) - index)
            elif encoding == 9:
                remainders = [0]
                width = 1
            else:
                raise BlackboxParseError(f"Unsupported field encoding {encoding}.")

            for item in range(width):
                field_index = index + item
                predictor = self._predict(fmt.predictor[field_index], field_index, values)
                values[field_index] = predictor + remainders[item]
            index += width
        return values

    def _read_tag2_3s32(self, reader: _ByteReader) -> list[int]:
        first = reader.read_u8()
        selector = first >> 6
        if selector == 0:
            return [
                _sign_extend((first >> 4) & 0x03, 2),
                _sign_extend((first >> 2) & 0x03, 2),
                _sign_extend(first & 0x03, 2),
            ]
        if selector == 1:
            second = reader.read_u8()
            return [
                _sign_extend(first & 0x0F, 4),
                _sign_extend((second >> 4) & 0x0F, 4),
                _sign_extend(second & 0x0F, 4),
            ]
        if selector == 2:
            second = reader.read_u8()
            third = reader.read_u8()
            return [
                _sign_extend(first & 0x3F, 6),
                _sign_extend(second & 0x3F, 6),
                _sign_extend(third & 0x3F, 6),
            ]

        sizes = first & 0x3F
        values: list[int] = []
        for index in range(3):
            byte_count = ((sizes >> (index * 2)) & 0x03) + 1
            raw = int.from_bytes(reader.read_bytes(byte_count), "little", signed=False)
            values.append(_sign_extend(raw, byte_count * 8))
        return values

    def _read_tag8_4s16(self, reader: _ByteReader) -> list[int]:
        header = reader.read_u8()
        bit_reader = _BitReader(reader)
        values: list[int] = []
        for index in range(4):
            selector = (header >> (index * 2)) & 0x03
            bits = (0, 4, 8, 16)[selector]
            if bits == 0:
                values.append(0)
            else:
                values.append(_sign_extend(bit_reader.read_bits(bits), bits))
        return values

    def _predict(self, predictor: int, index: int, current: list[int]) -> int:
        last = self.last_values[index] if index < len(self.last_values) else 0
        previous = self.previous_values[index] if index < len(self.previous_values) else last
        name = self.format_i.names[index] if self.format_i else ""

        if predictor == 0:
            return 0
        if predictor == 1:
            return last
        if predictor == 2:
            return 2 * last - previous
        if predictor == 3:
            return (last + previous) // 2
        if predictor == 4:
            return _header_int(self.headers, "minthrottle", 0)
        if predictor == 5 and name.startswith("motor["):
            return current[_field_index(self.format_i.names, "motor[0]")] if self.format_i else 0
        if predictor == 6:
            p_interval = max(1, _header_int(self.headers, "P interval", 1))
            return last + p_interval
        if predictor == 8:
            return 1500
        if predictor == 11:
            return _motor_minimum(self.headers)
        return 0

    def _store_history(self, values: list[int], sample: BlackboxSample | None, reset_history: bool) -> None:
        self.previous_values = values if reset_history else self.last_values
        self.last_values = values
        if sample is not None:
            self.last_time_us = int(sample.time * 1_000_000)

    def _sample_from_values(self, values: list[int]) -> BlackboxSample | None:
        if self.format_i is None:
            return None
        names = self.format_i.names
        time_index = _field_index(names, "time")
        gyro_index = _first_field_index(names, ("gyroUnfilt[0]", "gyroADC[0]", "debug[0]"))
        gyro_unfiltered_index = _field_index(names, "gyroUnfilt[0]", default=-1)
        if time_index < 0 or gyro_index < 0:
            return None
        motor_indexes = [index for index, name in enumerate(names) if name.startswith("motor[")]
        time_us = values[time_index]
        gyro = values[gyro_index]
        gyro_unfiltered = values[gyro_unfiltered_index] if gyro_unfiltered_index >= 0 else None
        motors = tuple(values[index] for index in motor_indexes)
        self._validate_sample_values(time_us, gyro, gyro_unfiltered, motors)
        return BlackboxSample(
            time=time_us / 1_000_000.0,
            gyro=float(gyro),
            gyro_unfiltered=float(gyro_unfiltered) if gyro_unfiltered is not None else None,
            motors=motors,
        )

    def _validate_sample_values(
        self,
        time_us: int,
        gyro: int,
        gyro_unfiltered: int | None,
        motors: tuple[int, ...],
    ) -> None:
        if not -1 <= time_us <= 3_600_000_000:
            raise BlackboxParseError(f"implausible timestamp {time_us}.")
        if self.last_time_us is not None:
            if time_us < self.last_time_us:
                raise BlackboxParseError(f"timestamp moved backwards from {self.last_time_us} to {time_us}.")
            if time_us - self.last_time_us > 2_000_000:
                raise BlackboxParseError(f"timestamp jump from {self.last_time_us} to {time_us}.")
        if abs(gyro) > 2_000_000:
            raise BlackboxParseError(f"implausible gyro value {gyro}.")
        if gyro_unfiltered is not None and abs(gyro_unfiltered) > 2_000_000:
            raise BlackboxParseError(f"implausible unfiltered gyro value {gyro_unfiltered}.")
        if any(motor < -100 or motor > 2500 for motor in motors):
            raise BlackboxParseError(f"implausible motor values {motors}.")

    def _skip_event(self, reader: _ByteReader) -> int | None:
        next_log_offset = self.data.find(LOG_START, reader.offset)
        if 0 <= next_log_offset <= reader.offset + 512:
            return next_log_offset

        if reader.remaining() <= 0:
            return None
        event = reader.read_u8()
        if event in (13, 14):
            _ = reader.read_unsigned_vb()
        next_log_offset = self.data.find(LOG_START, reader.offset)
        if 0 <= next_log_offset <= reader.offset + 512:
            return next_log_offset
        return None

    def _skip_to_next_main_frame(self, reader: _ByteReader) -> None:
        self._resync(reader, reader.offset)

    def _resync(self, reader: _ByteReader, start: int, keyframes_only: bool = False) -> None:
        frame_markers = (b"I",) if keyframes_only else (b"I", b"P")
        candidates = [position for position in (self.data.find(marker, start) for marker in frame_markers) if position >= 0]
        reader.offset = min(candidates) if candidates else len(self.data)


def parse_blackbox_file(path: Path) -> BlackboxParseResult:
    return _BlackboxDecoder(path.read_bytes()).parse()


def _csv_header(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int_header(value: str, count: int) -> list[int]:
    items: list[int] = []
    for raw in value.replace("/", ",").split(","):
        try:
            items.append(int(raw.strip()))
        except ValueError:
            pass
    if len(items) < count:
        items.extend([0] * (count - len(items)))
    return items[:count]


def _header_int(headers: dict[str, str], name: str, default: int) -> int:
    try:
        return int(headers.get(name, str(default)).split("/", 1)[0])
    except ValueError:
        return default


def _motor_minimum(headers: dict[str, str]) -> int:
    motor_output = headers.get("motorOutput", "")
    if "," in motor_output:
        try:
            return int(motor_output.split(",", 1)[0])
        except ValueError:
            pass
    return _header_int(headers, "minthrottle", 0)


def _field_index(names: list[str], field_name: str, default: int = -1) -> int:
    try:
        return names.index(field_name)
    except ValueError:
        return default


def _first_field_index(names: list[str], candidates: tuple[str, ...]) -> int:
    for candidate in candidates:
        index = _field_index(names, candidate)
        if index >= 0:
            return index
    return -1


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign

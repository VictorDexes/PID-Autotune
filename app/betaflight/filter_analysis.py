from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from betaflight.blackbox_parser import BlackboxParseError, parse_blackbox_file


@dataclass(frozen=True)
class FilterProposal:
    settings: dict[str, str]
    summary: str
    details: list[str]


def default_filter_proposal() -> FilterProposal:
    return FilterProposal(
        settings={
            "gyro_lpf1_static_hz": "1000",
            "gyro_lpf2_static_hz": "0",
            "dterm_lpf1_type": "PT1",
            "dterm_lpf1_dyn_min_hz": "75",
            "dterm_lpf1_dyn_max_hz": "150",
            "dterm_lpf1_dyn_expo": "5",
            "dterm_lpf2_type": "PT1",
            "dterm_lpf2_static_hz": "150",
            "dyn_notch_count": "1",
            "dyn_notch_min_hz": "150",
            "dyn_notch_max_hz": "650",
            "rpm_filter_min_hz": "100",
            "rpm_filter_fade_range_hz": "50",
            "rpm_filter_weights": "100,100,100",
        },
        summary="BF 4.5 conservative filter tune from the PDF.",
        details=[
            "Gyro: 1 kHz static PT1 and Gyro Lowpass 2 disabled.",
            "D term: 75 -> 150 Hz dynamic PT1, expo 5, plus 150 Hz static PT1.",
            "Dynamic notch: one notch, 150 -> 650 Hz.",
            "RPM filter: fade in from 100 Hz with a 50 Hz fade range.",
        ],
    )


def analyze_blackbox(path: Path) -> FilterProposal:
    parser_details: list[str] = []
    if path.suffix.lower() == ".csv":
        samples = _load_gyro_samples(path)
        source = "decoded Blackbox CSV"
    else:
        try:
            parsed = parse_blackbox_file(path)
            samples = [
                _Sample(
                    time=sample.time,
                    value=sample.gyro_unfiltered if sample.gyro_unfiltered is not None else sample.gyro,
                    motors=sample.motors,
                )
                for sample in parsed.samples
            ]
            source = "raw Betaflight Blackbox log"
            firmware = parsed.headers.get("Firmware revision")
            board = parsed.headers.get("Board information") or parsed.headers.get("Board informan")
            if firmware:
                parser_details.append(f"Firmware: {firmware}.")
            if board:
                parser_details.append(f"Board: {board}.")
            if parsed.warnings:
                parser_details.append(f"Parser recovered from {len(parsed.warnings)} corrupt frame(s).")
        except (BlackboxParseError, OSError, ValueError) as exc:
            preset = default_filter_proposal()
            return FilterProposal(
                settings=preset.settings,
                summary="Blackbox parsing failed; proposing the BF 4.5 baseline.",
                details=[
                    f"Parser error: {exc}",
                    "Extract a new log with this app before analysis; older logs may contain MSP Dataflash chunk headers.",
                    *preset.details,
                ],
            )

    if len(samples) < 256:
        preset = default_filter_proposal()
        return FilterProposal(
            settings=preset.settings,
            summary="Not enough gyro samples were found; proposing the PDF baseline.",
            details=preset.details,
        )

    sample_rate = _estimate_sample_rate(samples)
    values = [sample.value for sample in samples[:4096]]
    frame_peak = _peak_frequency(values, sample_rate, 140, 500)
    motor_peak = _estimate_motor_noise(samples, sample_rate)
    rpm_weights, rpm_weight_detail = _estimate_rpm_weights(values, sample_rate, motor_peak)

    preset = default_filter_proposal()
    settings = dict(preset.settings)
    dynamic_notch_count = _estimate_notch_count(values, sample_rate)
    settings["dyn_notch_count"] = str(dynamic_notch_count)
    if frame_peak:
        dyn_min = max(100, int(frame_peak - 25))
        if dyn_min < 150:
            dyn_min = 150
        settings["dyn_notch_min_hz"] = str(dyn_min)
    if motor_peak:
        rpm_min = max(50, int(motor_peak - 20))
        settings["rpm_filter_min_hz"] = str(rpm_min)
    if rpm_weights:
        settings["rpm_filter_weights"] = rpm_weights

    details = [
        *parser_details,
        f"Estimated sample rate: {sample_rate:.0f} Hz.",
        f"Decoded samples used: {len(samples)}.",
        f"Dominant frame/noise peak: {frame_peak:.0f} Hz." if frame_peak else "No clear frame peak found.",
        f"Low motor-noise candidate: {motor_peak:.0f} Hz." if motor_peak else "No clear low motor-noise peak found.",
        rpm_weight_detail,
        f"Dynamic notch count proposal: {dynamic_notch_count}.",
        "Review motor temperature after applying any filter change.",
    ]
    return FilterProposal(settings=settings, summary=f"Frequency-based proposal from {source}.", details=details)


@dataclass(frozen=True)
class _Sample:
    time: float
    value: float
    motors: tuple[int, ...] = ()


def _load_gyro_samples(path: Path) -> list[_Sample]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []

        time_column = _first_matching(reader.fieldnames, ("time", "looptime", "timestamp"))
        gyro_column = _first_matching(
            reader.fieldnames,
            ("gyroADC[0]", "gyroADC[1]", "gyroUnfilt[0]", "gyroUnfilt[1]", "gyroData[0]", "gyroData[1]"),
        )
        if not gyro_column:
            return []

        samples: list[_Sample] = []
        fallback_time = 0.0
        for row in reader:
            try:
                value = float(row[gyro_column])
            except (TypeError, ValueError):
                continue

            if time_column:
                raw_time = row.get(time_column, "")
                try:
                    time_value = float(raw_time)
                    if time_value > 1000:
                        time_value /= 1_000_000.0
                except ValueError:
                    time_value = fallback_time
            else:
                time_value = fallback_time

            samples.append(_Sample(time=time_value, value=value))
            fallback_time += 1 / 2000
            if len(samples) >= 12000:
                break
        return samples


def _first_matching(names: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {name.lower(): name for name in names}
    for candidate in candidates:
        match = lowered.get(candidate.lower())
        if match:
            return match
    for name in names:
        lower = name.lower()
        if any(candidate.lower() in lower for candidate in candidates):
            return name
    return None


def _estimate_sample_rate(samples: list[_Sample]) -> float:
    if len(samples) < 2:
        return 2000.0
    deltas = [
        samples[index].time - samples[index - 1].time
        for index in range(1, len(samples))
        if 0 < samples[index].time - samples[index - 1].time < 0.05
    ]
    if not deltas:
        return 2000.0
    deltas.sort()
    median_delta = deltas[len(deltas) // 2]
    if median_delta <= 0:
        return 2000.0
    return max(100.0, min(8000.0, 1 / median_delta))


def _peak_frequency(values: list[float], sample_rate: float, start_hz: int, end_hz: int) -> float | None:
    if not values:
        return None

    count = min(len(values), 4096)
    data = values[:count]
    mean = sum(data) / count
    data = [value - mean for value in data]

    best_frequency: float | None = None
    best_power = 0.0
    for frequency in range(start_hz, min(end_hz, int(sample_rate / 2) - 10), 5):
        power = _goertzel_power(data, sample_rate, frequency)
        if power > best_power:
            best_power = power
            best_frequency = float(frequency)

    if best_power <= 0:
        return None
    return best_frequency


def _estimate_motor_noise(samples: list[_Sample], sample_rate: float) -> float | None:
    with_motors = [sample for sample in samples if sample.motors]
    if len(with_motors) < 512:
        return _peak_frequency([sample.value for sample in samples[:4096]], sample_rate, 80, 260)

    sorted_samples = sorted(with_motors, key=lambda sample: sum(sample.motors) / len(sample.motors))
    low_throttle = sorted_samples[: min(4096, max(512, len(sorted_samples) // 3))]
    high_throttle = sorted_samples[-min(4096, max(512, len(sorted_samples) // 3)) :]
    low_peak = _peak_frequency([sample.value for sample in low_throttle], sample_rate, 80, 260)
    high_peak = _peak_frequency([sample.value for sample in high_throttle], sample_rate, 120, 500)
    if low_peak and high_peak and high_peak > low_peak:
        return low_peak
    return low_peak or high_peak


def _estimate_notch_count(values: list[float], sample_rate: float) -> int:
    peaks = _top_peaks(values, sample_rate, 140, 500, 3)
    if not peaks:
        return 1
    strongest = peaks[0][1]
    meaningful = [peak for peak, power in peaks if power >= strongest * 0.45]
    return max(1, min(3, len(meaningful)))


def _estimate_rpm_weights(values: list[float], sample_rate: float, motor_peak: float | None) -> tuple[str | None, str]:
    if not motor_peak or motor_peak * 3 >= sample_rate / 2:
        return None, "RPM harmonic dimming: no confident harmonic estimate."

    data_count = min(len(values), 4096)
    data = values[:data_count]
    mean = sum(data) / data_count
    data = [value - mean for value in data]
    first = _goertzel_power(data, sample_rate, motor_peak)
    second = _goertzel_power(data, sample_rate, motor_peak * 2)
    third = _goertzel_power(data, sample_rate, motor_peak * 3)
    if first <= 0:
        return None, "RPM harmonic dimming: no confident harmonic estimate."

    second_ratio = second / first
    third_ratio = third / first
    if second_ratio < 0.35 and third_ratio >= 0.35:
        return "100,0,80", "RPM harmonic dimming: weak 2nd harmonic, proposing triblade-style weights 100,0,80."
    if third_ratio < 0.35 and second_ratio >= 0.35:
        return "100,80,0", "RPM harmonic dimming: weak 3rd harmonic, proposing biblade-style weights 100,80,0."
    return "100,100,100", "RPM harmonic dimming: harmonics are not clearly weak, keeping all RPM harmonics enabled."


def _top_peaks(values: list[float], sample_rate: float, start_hz: int, end_hz: int, count: int) -> list[tuple[float, float]]:
    data_count = min(len(values), 4096)
    if data_count <= 0:
        return []
    data = values[:data_count]
    mean = sum(data) / data_count
    data = [value - mean for value in data]
    powers: list[tuple[float, float]] = []
    for frequency in range(start_hz, min(end_hz, int(sample_rate / 2) - 10), 5):
        powers.append((float(frequency), _goertzel_power(data, sample_rate, frequency)))
    powers.sort(key=lambda item: item[1], reverse=True)
    selected: list[tuple[float, float]] = []
    for frequency, power in powers:
        if all(abs(frequency - existing) >= 35 for existing, _ in selected):
            selected.append((frequency, power))
        if len(selected) >= count:
            break
    return selected


def _goertzel_power(data: list[float], sample_rate: float, frequency: float) -> float:
    omega = 2.0 * math.pi * frequency / sample_rate
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for value in data:
        s = value + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2

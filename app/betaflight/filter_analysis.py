from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path


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
    if path.suffix.lower() != ".csv":
        preset = default_filter_proposal()
        return FilterProposal(
            settings=preset.settings,
            summary="Raw .bbl parsing is not implemented yet; proposing the PDF baseline.",
            details=[
                "Export the log to CSV from Blackbox Explorer for frequency-based recommendations.",
                *preset.details,
            ],
        )

    samples = _load_gyro_samples(path)
    if len(samples) < 256:
        preset = default_filter_proposal()
        return FilterProposal(
            settings=preset.settings,
            summary="Not enough gyro samples were found; proposing the PDF baseline.",
            details=preset.details,
        )

    sample_rate = _estimate_sample_rate(samples)
    values = [sample.value for sample in samples[:4096]]
    frame_peak = _peak_frequency(values, sample_rate, 120, 450)
    motor_peak = _peak_frequency(values, sample_rate, 80, 260)

    preset = default_filter_proposal()
    settings = dict(preset.settings)
    if frame_peak:
        dyn_min = max(100, int(frame_peak - 25))
        if dyn_min < 150:
            dyn_min = 150
        settings["dyn_notch_min_hz"] = str(dyn_min)
    if motor_peak:
        rpm_min = max(50, int(motor_peak - 20))
        settings["rpm_filter_min_hz"] = str(rpm_min)

    details = [
        f"Estimated sample rate: {sample_rate:.0f} Hz.",
        f"Dominant frame/noise peak: {frame_peak:.0f} Hz." if frame_peak else "No clear frame peak found.",
        f"Low motor-noise candidate: {motor_peak:.0f} Hz." if motor_peak else "No clear low motor-noise peak found.",
        "Review motor temperature after applying any filter change.",
    ]
    return FilterProposal(settings=settings, summary="Frequency-based proposal from decoded Blackbox CSV.", details=details)


@dataclass(frozen=True)
class _Sample:
    time: float
    value: float


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
    duration = samples[-1].time - samples[0].time
    if duration <= 0:
        return 2000.0
    return max(100.0, min(8000.0, (len(samples) - 1) / duration))


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

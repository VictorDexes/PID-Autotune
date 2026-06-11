from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

from betaflight.blackbox_parser import BlackboxParseError, BlackboxParseResult, parse_blackbox_file


@dataclass(frozen=True)
class PidTuneInput:
    weight_g: float
    prop_size_in: float
    flight_style: str


@dataclass(frozen=True)
class PidProposal:
    settings: dict[str, str]
    summary: str
    details: list[str]


STYLE_PROFILES = {
    "Freestyle": {
        "master": 1.00,
        "tracking": 1.00,
        "i": 1.00,
        "ff": 1.00,
        "iterm_relax_cutoff": 15,
        "anti_gravity_gain": 80,
    },
    "Racing": {
        "master": 1.08,
        "tracking": 1.04,
        "i": 1.03,
        "ff": 1.08,
        "iterm_relax_cutoff": 35,
        "anti_gravity_gain": 75,
    },
    "Cinematic": {
        "master": 0.88,
        "tracking": 0.92,
        "i": 0.95,
        "ff": 0.82,
        "iterm_relax_cutoff": 10,
        "anti_gravity_gain": 70,
    },
    "Heavy freestyle": {
        "master": 0.94,
        "tracking": 0.96,
        "i": 1.08,
        "ff": 0.92,
        "iterm_relax_cutoff": 10,
        "anti_gravity_gain": 90,
    },
}

DYNAMIC_IDLE_TABLE = {
    1.5: (66, 133),
    2.0: (50, 100),
    2.5: (40, 80),
    3.0: (33, 66),
    3.5: (28, 57),
    4.0: (25, 50),
    5.0: (20, 40),
    5.5: (18, 36),
    6.0: (16, 33),
    7.0: (14, 28),
    8.0: (12, 25),
    10.0: (10, 20),
    13.0: (7, 15),
}


def analyze_pid_tune(path: Path, tune_input: PidTuneInput) -> PidProposal:
    try:
        parsed = parse_blackbox_file(path)
    except (BlackboxParseError, OSError, ValueError) as exc:
        return PidProposal(
            settings=_fallback_pid_settings(tune_input),
            summary="Blackbox parsing failed; proposing a conservative PID baseline.",
            details=[f"Parser error: {exc}", "Use a raw .bbl log for PID values adapted from the current tune."],
        )

    profile = STYLE_PROFILES.get(tune_input.flight_style, STYLE_PROFILES["Freestyle"])
    roll_pid = _pid_triplet(parsed.headers, "rollPID", (45, 80, 40))
    pitch_pid = _pid_triplet(parsed.headers, "pitchPID", (47, 84, 46))
    yaw_pid = _pid_triplet(parsed.headers, "yawPID", (45, 80, 0))
    ff = _pid_triplet(parsed.headers, "ff_weight", (120, 125, 120))
    d_min = _pid_triplet(parsed.headers, "d_min", (30, 34, 0))

    loading = _loading_factor(tune_input.weight_g, tune_input.prop_size_in)
    motor_state = _motor_state(parsed)
    master = _master_multiplier(profile["master"], loading, motor_state)
    tracking = profile["tracking"]
    i_gain = profile["i"] * _i_gain_factor(loading)
    ff_gain = profile["ff"] * _ff_factor(motor_state)

    new_roll = _scale_pid(roll_pid, master, tracking, i_gain, ff_gain, ff[0])
    new_pitch = _scale_pid(pitch_pid, master, tracking, i_gain, ff_gain, ff[1])
    new_yaw = _scale_yaw(yaw_pid, master, i_gain, ff_gain, ff[2])
    new_d_min = (
        _clamp_int(round(d_min[0] * master), 15, new_roll[2]),
        _clamp_int(round(d_min[1] * master), 15, new_pitch[2]),
        0,
    )

    dynamic_idle = _dynamic_idle_value(tune_input.prop_size_in, loading)
    tpa_breakpoint, tpa_rate = _tpa_values(motor_state)
    dmax_gain = _dmax_gain(motor_state, tune_input.flight_style)

    settings = {
        "p_roll": str(new_roll[0]),
        "i_roll": str(new_roll[1]),
        "d_roll": str(new_roll[2]),
        "f_roll": str(new_roll[3]),
        "p_pitch": str(new_pitch[0]),
        "i_pitch": str(new_pitch[1]),
        "d_pitch": str(new_pitch[2]),
        "f_pitch": str(new_pitch[3]),
        "p_yaw": str(new_yaw[0]),
        "i_yaw": str(new_yaw[1]),
        "d_yaw": "0",
        "f_yaw": str(new_yaw[2]),
        "d_min_roll": str(new_d_min[0]),
        "d_min_pitch": str(new_d_min[1]),
        "d_min_yaw": "0",
        "d_max_gain": str(dmax_gain),
        "d_max_advance": "0",
        "iterm_relax_cutoff": str(profile["iterm_relax_cutoff"]),
        "anti_gravity_gain": str(profile["anti_gravity_gain"]),
        "anti_gravity_cutoff_hz": "5",
        "dyn_idle_min_rpm": str(dynamic_idle),
        "tpa_rate": str(tpa_rate),
        "tpa_breakpoint": str(tpa_breakpoint),
        "feedforward_boost": str(_feedforward_boost(tune_input.flight_style, motor_state)),
        "feedforward_max_rate_limit": str(_feedforward_rate_limit(tune_input.flight_style)),
        "pidsum_limit": "1000",
        "pidsum_limit_yaw": "1000",
    }

    firmware = parsed.headers.get("Firmware revision", "Unknown firmware")
    board = parsed.headers.get("Board information") or parsed.headers.get("Board informan") or "Unknown board"
    details = [
        f"Firmware: {firmware}.",
        f"Board: {board}.",
        f"Current roll PID: {roll_pid[0]}/{roll_pid[1]}/{roll_pid[2]}; pitch PID: {pitch_pid[0]}/{pitch_pid[1]}/{pitch_pid[2]}.",
        f"Airframe loading estimate: {loading:.2f} ({_loading_label(loading)}).",
        f"Motor headroom estimate: {motor_state['headroom_label']}.",
        f"Master multiplier equivalent: {master:.2f}; tracking factor: {tracking:.2f}; I factor: {i_gain:.2f}; FF factor: {ff_gain:.2f}.",
        "Dynamic Damping advance is set to 0 as recommended; gain is kept moderate unless the log shows low motor headroom.",
        "After applying, do short sharp-move tests and check motor temperature before increasing gains further.",
    ]
    return PidProposal(settings=settings, summary=f"PID proposal for {tune_input.flight_style.lower()} from Blackbox and PDF guidance.", details=details)


def _fallback_pid_settings(tune_input: PidTuneInput) -> dict[str, str]:
    profile = STYLE_PROFILES.get(tune_input.flight_style, STYLE_PROFILES["Freestyle"])
    loading = _loading_factor(tune_input.weight_g, tune_input.prop_size_in)
    return {
        "iterm_relax_cutoff": str(profile["iterm_relax_cutoff"]),
        "anti_gravity_gain": str(profile["anti_gravity_gain"]),
        "anti_gravity_cutoff_hz": "5",
        "dyn_idle_min_rpm": str(_dynamic_idle_value(tune_input.prop_size_in, loading)),
        "d_max_advance": "0",
        "pidsum_limit": "1000",
        "pidsum_limit_yaw": "1000",
    }


def _pid_triplet(headers: dict[str, str], name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = headers.get(name, "")
    parts: list[int] = []
    for item in raw.split(","):
        try:
            parts.append(int(item.strip()))
        except ValueError:
            pass
    if len(parts) < 3:
        return default
    return parts[0], parts[1], parts[2]


def _scale_pid(
    pid: tuple[int, int, int],
    master: float,
    tracking: float,
    i_gain: float,
    ff_gain: float,
    feedforward: int,
) -> tuple[int, int, int, int]:
    p = _clamp_int(round(pid[0] * master * tracking), 20, 120)
    i = _clamp_int(round(pid[1] * master * i_gain), 30, 180)
    d = _clamp_int(round(pid[2] * master), 15, 100)
    f = _clamp_int(round(feedforward * ff_gain), 40, 220)
    return p, i, d, f


def _scale_yaw(pid: tuple[int, int, int], master: float, i_gain: float, ff_gain: float, feedforward: int) -> tuple[int, int, int]:
    p = _clamp_int(round(pid[0] * min(master, 1.05)), 20, 110)
    i = _clamp_int(round(pid[1] * master * i_gain), 30, 180)
    f = _clamp_int(round(feedforward * ff_gain), 40, 220)
    return p, i, f


def _loading_factor(weight_g: float, prop_size_in: float) -> float:
    if prop_size_in <= 0:
        return 1.0
    nominal = 4.3 * (prop_size_in ** 3)
    return max(0.65, min(1.6, weight_g / nominal))


def _loading_label(loading: float) -> str:
    if loading < 0.85:
        return "light/responsive"
    if loading > 1.15:
        return "heavy/high inertia"
    return "typical"


def _motor_state(parsed: BlackboxParseResult) -> dict[str, float | str]:
    averages = [sum(sample.motors) / len(sample.motors) for sample in parsed.samples if sample.motors]
    if not averages:
        return {"p95": 0.0, "span": 0.0, "headroom_label": "unknown"}
    sorted_avg = sorted(averages)
    p95 = sorted_avg[min(len(sorted_avg) - 1, int(len(sorted_avg) * 0.95))]
    span = max(averages) - min(averages)
    if p95 > 1900:
        label = "low headroom / frequent high throttle"
    elif p95 > 1650:
        label = "moderate headroom"
    else:
        label = "good headroom"
    return {"p95": p95, "span": span, "headroom_label": label}


def _master_multiplier(base: float, loading: float, motor_state: dict[str, float | str]) -> float:
    value = base
    if loading > 1.15:
        value -= min(0.12, (loading - 1.15) * 0.18)
    elif loading < 0.85:
        value += min(0.08, (0.85 - loading) * 0.16)
    if float(motor_state["p95"]) > 1900:
        value -= 0.08
    return max(0.72, min(1.18, value))


def _i_gain_factor(loading: float) -> float:
    if loading > 1.15:
        return 1.08
    if loading < 0.85:
        return 0.96
    return 1.0


def _ff_factor(motor_state: dict[str, float | str]) -> float:
    return 0.92 if float(motor_state["p95"]) > 1900 else 1.0


def _dynamic_idle_value(prop_size: float, loading: float) -> int:
    closest = min(DYNAMIC_IDLE_TABLE, key=lambda item: abs(item - prop_size))
    low_pitch, steep_pitch = DYNAMIC_IDLE_TABLE[closest]
    value = steep_pitch if loading < 0.9 else low_pitch
    return _clamp_int(value, 5, 150)


def _tpa_values(motor_state: dict[str, float | str]) -> tuple[int, int]:
    p95 = float(motor_state["p95"])
    if p95 > 1900:
        return 1300, 70
    if p95 > 1650:
        return 1350, 60
    return 1350, 50


def _dmax_gain(motor_state: dict[str, float | str], style: str) -> int:
    if style == "Racing":
        return 45
    if float(motor_state["p95"]) > 1900:
        return 30
    return 37


def _feedforward_boost(style: str, motor_state: dict[str, float | str]) -> int:
    if float(motor_state["p95"]) > 1900:
        return 10
    return {"Racing": 20, "Freestyle": 15, "Heavy freestyle": 12, "Cinematic": 5}.get(style, 15)


def _feedforward_rate_limit(style: str) -> int:
    return {"Racing": 120, "Freestyle": 90, "Heavy freestyle": 75, "Cinematic": 60}.get(style, 90)


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))

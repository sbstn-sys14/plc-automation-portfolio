"""Digital PI controller simulation for a thermal plant.

The project is intentionally simulation-only. It models:
- a first-order thermal room,
- boiler/actuator first-order dynamics,
- transport delay,
- PI control with conditional-integration anti-windup,
- outdoor-temperature feedforward,
- sensor noise, quantization and digital filtering,
- nominal validation, parametric robustness and Monte Carlo noise tests.
"""

from __future__ import annotations

import csv
import itertools
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


# -----------------------------------------------------------------------------
# Nominal process model
# -----------------------------------------------------------------------------
C_TH_NOMINAL = 5_000_000.0  # J/°C
K_TH_NOMINAL = 250.0        # W/°C
T_B_NOMINAL = 3600.0        # s
L_DELAY_NOMINAL = 1800.0    # s

# Actuator limits
P_MIN = 0.0                  # W
P_MAX = 6000.0               # W

# Final controller
K_P = 450.0                  # W/°C
T_I = 20_000.0               # s
K_I = K_P / T_I              # W/(°C*s)

# Digital implementation
T_E = 60.0                   # s
T_FILTER = 120.0             # s
Q_SENSOR = 0.1               # °C
SIGMA_NOISE = 0.2            # °C

# Performance criteria
MAX_STEADY_ERROR = 0.1              # °C
MAX_OVERSHOOT = 10.0                # %
MAX_SETTLING_REFERENCE = 6.0        # h
MAX_DISTURBANCE_DEVIATION = 1.8     # °C
MAX_SETTLING_DISTURBANCE = 12.0     # h
MAX_SATURATION_PERCENT = 20.0       # %

RESULTS_DIR = Path("results")


def quantize(value: float, q: float) -> float:
    """Quantize by rounding to the closest multiple of q."""
    if q <= 0:
        return value
    return float(np.round(value / q) * q)


def simulate_system(
    scenario: str,
    Cth_real: float = C_TH_NOMINAL,
    Kth_real: float = K_TH_NOMINAL,
    TB_real: float = T_B_NOMINAL,
    L_real: float = L_DELAY_NOMINAL,
    sigma_noise: float = SIGMA_NOISE,
    seed: int = 1,
) -> dict[str, np.ndarray | float]:
    """Simulate either reference tracking or disturbance rejection.

    The controller always uses the nominal model. Process parameters can be changed
    independently to test robustness.
    """
    if scenario == "reference":
        T_initial = 20.0
        outside_initial = 10.0
        step_time = 4 * 3600.0
        simulation_time = 24 * 3600.0
    elif scenario == "disturbance":
        T_initial = 22.0
        outside_initial = 10.0
        step_time = 4 * 3600.0
        simulation_time = 24 * 3600.0
    else:
        raise ValueError("scenario must be 'reference' or 'disturbance'")

    # Nominal base power used by the controller and true equilibrium power.
    P_base_nominal = K_TH_NOMINAL * (T_initial - outside_initial)
    P_equilibrium_real = Kth_real * (T_initial - outside_initial)

    # Assume the plant was already in stationary automatic operation before test start.
    integral_initial = (P_equilibrium_real - P_base_nominal) / K_I

    n = int(simulation_time / T_E) + 1
    time = np.arange(n) * T_E
    rng = np.random.default_rng(seed)

    temperature = np.zeros(n)
    sensor_temperature = np.zeros(n)
    measured_temperature = np.zeros(n)
    filtered_temperature = np.zeros(n)
    measurement_noise = np.zeros(n)
    reference = np.zeros(n)
    outside_temperature = np.zeros(n)
    error = np.zeros(n)
    integral_state = np.zeros(n)
    p_feedforward = np.zeros(n)
    p_proportional = np.zeros(n)
    p_integral = np.zeros(n)
    p_requested = np.zeros(n)
    p_command = np.zeros(n)
    power = np.zeros(n)

    temperature[0] = T_initial
    power[0] = P_equilibrium_real
    integral_state[0] = integral_initial

    n_delay = int(round(L_real / T_E))
    represented_delay = n_delay * T_E
    delay_buffer = [float(P_equilibrium_real)] * n_delay

    alpha = T_FILTER / (T_FILTER + T_E) if T_FILTER > 0 else 0.0

    for k in range(n - 1):
        if scenario == "reference":
            reference[k] = 20.0 if time[k] < step_time else 22.0
            outside_temperature[k] = 10.0
        else:
            reference[k] = 22.0
            outside_temperature[k] = 10.0 if time[k] < step_time else 0.0

        measurement_noise[k] = rng.normal(0.0, sigma_noise)
        sensor_temperature[k] = temperature[k] + measurement_noise[k]
        measured_temperature[k] = quantize(sensor_temperature[k], Q_SENSOR)

        if k == 0:
            filtered_temperature[k] = measured_temperature[k]
        elif T_FILTER > 0:
            filtered_temperature[k] = (
                alpha * filtered_temperature[k - 1]
                + (1.0 - alpha) * measured_temperature[k]
            )
        else:
            filtered_temperature[k] = measured_temperature[k]

        error[k] = reference[k] - filtered_temperature[k]

        # Outdoor-temperature feedforward uses nominal heat-loss coefficient.
        p_feedforward[k] = K_TH_NOMINAL * (
            outside_initial - outside_temperature[k]
        )

        integral_candidate = integral_state[k] + T_E * error[k]
        p_proportional[k] = K_P * error[k]
        p_integral_candidate = K_I * integral_candidate

        p_candidate = (
            P_base_nominal
            + p_feedforward[k]
            + p_proportional[k]
            + p_integral_candidate
        )

        harmful_upper = p_candidate > P_MAX and error[k] > 0
        harmful_lower = p_candidate < P_MIN and error[k] < 0

        # Conditional integration anti-windup: freeze I only when saturation would
        # be driven further in the harmful direction.
        if harmful_upper or harmful_lower:
            integral_state[k + 1] = integral_state[k]
            p_integral[k] = K_I * integral_state[k]
        else:
            integral_state[k + 1] = integral_candidate
            p_integral[k] = p_integral_candidate

        p_requested[k] = (
            P_base_nominal
            + p_feedforward[k]
            + p_proportional[k]
            + p_integral[k]
        )
        p_command[k] = np.clip(p_requested[k], P_MIN, P_MAX)

        if n_delay > 0:
            p_delayed = delay_buffer.pop(0)
            delay_buffer.append(float(p_command[k]))
        else:
            p_delayed = float(p_command[k])

        # Boiler first-order dynamics.
        power[k + 1] = power[k] + T_E * (p_delayed - power[k]) / TB_real

        # Room thermal energy balance.
        temperature[k + 1] = temperature[k] + T_E * (
            power[k] - Kth_real * (temperature[k] - outside_temperature[k])
        ) / Cth_real

    # Fill last sample for plotting/export.
    reference[-1] = 22.0
    outside_temperature[-1] = 10.0 if scenario == "reference" else 0.0
    measurement_noise[-1] = rng.normal(0.0, sigma_noise)
    sensor_temperature[-1] = temperature[-1] + measurement_noise[-1]
    measured_temperature[-1] = quantize(sensor_temperature[-1], Q_SENSOR)
    if T_FILTER > 0:
        filtered_temperature[-1] = (
            alpha * filtered_temperature[-2]
            + (1.0 - alpha) * measured_temperature[-1]
        )
    else:
        filtered_temperature[-1] = measured_temperature[-1]
    error[-1] = reference[-1] - filtered_temperature[-1]
    p_feedforward[-1] = K_TH_NOMINAL * (
        outside_initial - outside_temperature[-1]
    )
    p_proportional[-1] = K_P * error[-1]
    p_integral[-1] = K_I * integral_state[-1]
    p_requested[-1] = (
        P_base_nominal
        + p_feedforward[-1]
        + p_proportional[-1]
        + p_integral[-1]
    )
    p_command[-1] = np.clip(p_requested[-1], P_MIN, P_MAX)

    return {
        "time": time,
        "temperature": temperature,
        "sensor_temperature": sensor_temperature,
        "measured_temperature": measured_temperature,
        "filtered_temperature": filtered_temperature,
        "measurement_noise": measurement_noise,
        "reference": reference,
        "outside_temperature": outside_temperature,
        "error": error,
        "integral_state": integral_state,
        "P_feedforward": p_feedforward,
        "P_proportional": p_proportional,
        "P_integral": p_integral,
        "P_requested": p_requested,
        "P_command": p_command,
        "power": power,
        "step_time": step_time,
        "Cth": Cth_real,
        "Kth": Kth_real,
        "TB": TB_real,
        "L": represented_delay,
    }


def settling_time(
    time: np.ndarray,
    signal: np.ndarray,
    target: float,
    start_time: float,
    band: float = 0.2,
) -> float:
    mask = time >= start_time
    t = time[mask]
    y = signal[mask]
    inside = np.abs(y - target) <= band
    outside_indices = np.where(~inside)[0]
    if len(outside_indices) == 0:
        return 0.0
    last_outside = outside_indices[-1]
    if last_outside == len(y) - 1:
        return np.nan
    return float(t[last_outside + 1] - start_time)


def reference_metrics(result: dict) -> dict[str, float]:
    time = result["time"]
    temperature = result["temperature"]
    p_command = result["P_command"]
    step_time = result["step_time"]

    final_mask = time >= time[-1] - 3600.0
    t_final = np.mean(temperature[final_mask])
    steady_error = 22.0 - t_final

    after_step = time >= step_time
    t_max = np.max(temperature[after_step])
    overshoot = max(0.0, t_max - 22.0) / 2.0 * 100.0

    ts = settling_time(time, temperature, 22.0, step_time, 0.2)
    ts_h = np.inf if np.isnan(ts) else ts / 3600.0

    saturation = np.mean(
        np.isclose(p_command, P_MIN) | np.isclose(p_command, P_MAX)
    ) * 100.0

    return {
        "ess": float(steady_error),
        "overshoot": float(overshoot),
        "settling": float(ts_h),
        "saturation": float(saturation),
    }


def disturbance_metrics(result: dict) -> dict[str, float]:
    time = result["time"]
    temperature = result["temperature"]
    p_command = result["P_command"]
    step_time = result["step_time"]

    after_step = time >= step_time
    maximum_deviation = np.max(np.abs(temperature[after_step] - 22.0))

    ts = settling_time(time, temperature, 22.0, step_time, 0.2)
    ts_h = np.inf if np.isnan(ts) else ts / 3600.0

    final_mask = time >= time[-1] - 3600.0
    t_final = np.mean(temperature[final_mask])
    steady_error = 22.0 - t_final

    saturation = np.mean(
        np.isclose(p_command, P_MIN) | np.isclose(p_command, P_MAX)
    ) * 100.0

    return {
        "deviation": float(maximum_deviation),
        "settling": float(ts_h),
        "ess": float(steady_error),
        "saturation": float(saturation),
    }


def ideal_disturbance_limit(
    Cth_real: float,
    Kth_real: float,
    TB_real: float,
    L_real: float,
) -> float:
    """Best causal disturbance response with Pcmd forced to P_MAX immediately."""
    simulation_time = 20 * 3600.0
    step_time = 2 * 3600.0
    n = int(simulation_time / T_E) + 1
    time = np.arange(n) * T_E

    temperature = np.zeros(n)
    power = np.zeros(n)
    temperature[0] = 22.0
    p_initial = Kth_real * (22.0 - 10.0)
    power[0] = p_initial

    n_delay = int(round(L_real / T_E))
    delay_buffer = [float(p_initial)] * n_delay

    for k in range(n - 1):
        if time[k] < step_time:
            outside = 10.0
            command = p_initial
        else:
            outside = 0.0
            command = P_MAX

        if n_delay > 0:
            p_delayed = delay_buffer.pop(0)
            delay_buffer.append(float(command))
        else:
            p_delayed = float(command)

        power[k + 1] = power[k] + T_E * (p_delayed - power[k]) / TB_real
        temperature[k + 1] = temperature[k] + T_E * (
            power[k] - Kth_real * (temperature[k] - outside)
        ) / Cth_real

    minimum_temperature = np.min(temperature[time >= step_time])
    return float(22.0 - minimum_temperature)


def evaluate_case(Cth: float, Kth: float, TB: float, L: float, seed: int = 1) -> dict:
    ref = simulate_system("reference", Cth, Kth, TB, L, seed=seed)
    dist = simulate_system("disturbance", Cth, Kth, TB, L, seed=seed)
    ref_m = reference_metrics(ref)
    dist_m = disturbance_metrics(dist)

    ideal_drop = ideal_disturbance_limit(Cth, Kth, TB, L)
    physical_gap = dist_m["deviation"] - ideal_drop
    gap_percent = physical_gap / ideal_drop * 100.0 if ideal_drop > 0 else 0.0

    physically_feasible = Kth * 22.0 <= P_MAX
    criteria_passed = (
        abs(ref_m["ess"]) <= MAX_STEADY_ERROR
        and ref_m["overshoot"] <= MAX_OVERSHOOT
        and ref_m["settling"] <= MAX_SETTLING_REFERENCE
        and dist_m["deviation"] <= MAX_DISTURBANCE_DEVIATION
        and dist_m["settling"] <= MAX_SETTLING_DISTURBANCE
        and ref_m["saturation"] <= MAX_SATURATION_PERCENT
        and dist_m["saturation"] <= MAX_SATURATION_PERCENT
    )

    return {
        "Cth": Cth,
        "Kth": Kth,
        "TB": TB,
        "L": L,
        "feasible": physically_feasible,
        "ref_ess": ref_m["ess"],
        "ref_Mp": ref_m["overshoot"],
        "ref_ts": ref_m["settling"],
        "ref_sat": ref_m["saturation"],
        "dist": dist_m["deviation"],
        "dist_ts": dist_m["settling"],
        "dist_sat": dist_m["saturation"],
        "ideal_dist": ideal_drop,
        "physical_gap": physical_gap,
        "gap_percent": gap_percent,
        "criteria_pass": bool(physically_feasible and criteria_passed),
        "reference_simulation": ref,
        "disturbance_simulation": dist,
    }


def save_timeseries(filename: str, result: dict) -> None:
    with open(RESULTS_DIR / filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "time_s",
                "time_h",
                "reference_C",
                "outside_temperature_C",
                "temperature_C",
                "sensor_temperature_C",
                "measured_temperature_C",
                "filtered_temperature_C",
                "error_C",
                "P_feedforward_W",
                "P_proportional_W",
                "P_integral_W",
                "P_requested_W",
                "P_command_W",
                "power_effective_W",
            ]
        )
        for k in range(len(result["time"])):
            writer.writerow(
                [
                    result["time"][k],
                    result["time"][k] / 3600.0,
                    result["reference"][k],
                    result["outside_temperature"][k],
                    result["temperature"][k],
                    result["sensor_temperature"][k],
                    result["measured_temperature"][k],
                    result["filtered_temperature"][k],
                    result["error"][k],
                    result["P_feedforward"][k],
                    result["P_proportional"][k],
                    result["P_integral"][k],
                    result["P_requested"][k],
                    result["P_command"][k],
                    result["power"][k],
                ]
            )


def save_figure(filename: str) -> None:
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / filename, dpi=200, bbox_inches="tight")
    plt.close()


def run_nominal() -> tuple[dict, dict, dict, dict]:
    ref = simulate_system("reference", seed=1)
    dist = simulate_system("disturbance", seed=1)
    ref_m = reference_metrics(ref)
    dist_m = disturbance_metrics(dist)

    save_timeseries("nominal_reference_timeseries.csv", ref)
    save_timeseries("nominal_disturbance_timeseries.csv", dist)

    with open(RESULTS_DIR / "final_parameters.txt", "w", encoding="utf-8") as f:
        f.write("CONFIGURATIA FINALA\n===================\n")
        f.write(f"Kp = {K_P:g} W/°C\n")
        f.write(f"Ki = {K_I:g} W/(°C*s)\n")
        f.write(f"Ti = {T_I:g} s\n")
        f.write(f"Te = {T_E:g} s\n")
        f.write(f"Tf = {T_FILTER:g} s\n")
        f.write(f"Pmax = {P_MAX:g} W\n")
        f.write(f"q = {Q_SENSOR:g} °C\n")
        f.write(f"sigma_noise = {SIGMA_NOISE:g} °C\n\n")
        f.write("PERFORMANTE NOMINALE\n====================\n")
        f.write(f"Eroare stationara = {ref_m['ess']:.6f} °C\n")
        f.write(f"Suprareglaj = {ref_m['overshoot']:.6f} %\n")
        f.write(f"Timp stabilire referinta = {ref_m['settling']:.6f} h\n")
        f.write(f"Abatere perturbatie = {dist_m['deviation']:.6f} °C\n")
        f.write(f"Timp restabilire perturbatie = {dist_m['settling']:.6f} h\n")

    plt.figure()
    plt.plot(ref["time"] / 3600, ref["temperature"], label="Temperatura reala")
    plt.plot(ref["time"] / 3600, ref["reference"], "--", label="Referinta")
    plt.axhline(22.2, linestyle=":", label="Banda +0.2 °C")
    plt.axhline(21.8, linestyle=":", label="Banda -0.2 °C")
    plt.xlabel("Timp [h]")
    plt.ylabel("Temperatura [°C]")
    plt.title("Test final - urmarirea referintei")
    plt.grid()
    plt.legend()
    save_figure("01_nominal_reference.png")

    plt.figure()
    plt.plot(dist["time"] / 3600, dist["temperature"], label="Temperatura reala")
    plt.plot(dist["time"] / 3600, dist["reference"], "--", label="Referinta")
    plt.axvline(4, linestyle="--", label="Text: 10 -> 0 °C")
    plt.axhline(22 - MAX_DISTURBANCE_DEVIATION, linestyle=":", label="Limita abatere")
    plt.xlabel("Timp [h]")
    plt.ylabel("Temperatura [°C]")
    plt.title("Test final - rejetia perturbatiei")
    plt.grid()
    plt.legend()
    save_figure("02_nominal_disturbance.png")

    plt.figure()
    plt.plot(ref["time"] / 3600, ref["temperature"], label="Temperatura reala")
    plt.step(ref["time"] / 3600, ref["measured_temperature"], where="post", label="Temperatura masurata")
    plt.plot(ref["time"] / 3600, ref["filtered_temperature"], label="Temperatura filtrata")
    plt.xlim(8, 12)
    plt.ylim(21, 23)
    plt.xlabel("Timp [h]")
    plt.ylabel("Temperatura [°C]")
    plt.title("Senzor, cuantizare, zgomot si filtrare")
    plt.grid()
    plt.legend()
    save_figure("03_sensor_filter.png")

    plt.figure()
    plt.step(ref["time"] / 3600, ref["P_command"], where="post", label="Putere comandata")
    plt.plot(ref["time"] / 3600, ref["power"], label="Putere efectiva")
    plt.axhline(P_MAX, linestyle="--", label="Pmax")
    plt.xlabel("Timp [h]")
    plt.ylabel("Putere [W]")
    plt.title("Puterea - test referinta")
    plt.grid()
    plt.legend()
    save_figure("04_power_reference.png")

    plt.figure()
    plt.step(dist["time"] / 3600, dist["P_command"], where="post", label="Putere comandata")
    plt.plot(dist["time"] / 3600, dist["power"], label="Putere efectiva")
    plt.step(dist["time"] / 3600, dist["P_feedforward"], where="post", label="Compensare perturbatie")
    plt.axhline(P_MAX, linestyle="--", label="Pmax")
    plt.xlabel("Timp [h]")
    plt.ylabel("Putere [W]")
    plt.title("Puterea - test perturbatie")
    plt.grid()
    plt.legend()
    save_figure("05_power_disturbance.png")

    return ref, dist, ref_m, dist_m


def run_robustness() -> list[dict]:
    Cth_values = [0.8 * C_TH_NOMINAL, 1.2 * C_TH_NOMINAL]
    Kth_values = [0.95 * K_TH_NOMINAL, 1.05 * K_TH_NOMINAL]
    TB_values = [0.8 * T_B_NOMINAL, 1.2 * T_B_NOMINAL]
    L_values = [0.8 * L_DELAY_NOMINAL, 1.2 * L_DELAY_NOMINAL]

    corners = [
        evaluate_case(Cth, Kth, TB, L, seed=1)
        for Cth, Kth, TB, L in itertools.product(
            Cth_values, Kth_values, TB_values, L_values
        )
    ]

    with open(RESULTS_DIR / "robustness_corners.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Cth", "Kth", "TB", "L",
                "reference_steady_error_C", "reference_overshoot_percent",
                "reference_settling_h", "disturbance_deviation_C",
                "disturbance_settling_h", "ideal_physical_drop_C",
                "gap_C", "gap_percent", "criteria_pass",
            ]
        )
        for r in corners:
            writer.writerow(
                [
                    r["Cth"], r["Kth"], r["TB"], r["L"], r["ref_ess"],
                    r["ref_Mp"], r["ref_ts"], r["dist"], r["dist_ts"],
                    r["ideal_dist"], r["physical_gap"], r["gap_percent"],
                    r["criteria_pass"],
                ]
            )

    case_numbers = np.arange(1, len(corners) + 1)
    plt.figure()
    plt.plot(case_numbers, [r["dist"] for r in corners], marker="o", label="Regulator")
    plt.plot(case_numbers, [r["ideal_dist"] for r in corners], marker="o", label="Limita fizica idealizata")
    plt.axhline(MAX_DISTURBANCE_DEVIATION, linestyle="--", label="Criteriu nominal")
    plt.xlabel("Caz de robustete")
    plt.ylabel("Abatere maxima [°C]")
    plt.title("Regulator vs limita fizica a procesului")
    plt.grid()
    plt.legend()
    save_figure("06_robustness_physical_limit.png")

    return corners


def run_monte_carlo(n_runs: int = 50) -> list[dict]:
    rows = []
    for seed in range(n_runs):
        ref = simulate_system("reference", seed=seed)
        dist = simulate_system("disturbance", seed=seed)
        rm = reference_metrics(ref)
        dm = disturbance_metrics(dist)

        passed = (
            abs(rm["ess"]) <= MAX_STEADY_ERROR
            and rm["overshoot"] <= MAX_OVERSHOOT
            and rm["settling"] <= MAX_SETTLING_REFERENCE
            and dm["deviation"] <= MAX_DISTURBANCE_DEVIATION
            and dm["settling"] <= MAX_SETTLING_DISTURBANCE
            and rm["saturation"] <= MAX_SATURATION_PERCENT
            and dm["saturation"] <= MAX_SATURATION_PERCENT
        )
        rows.append(
            {
                "seed": seed,
                "ess": rm["ess"],
                "overshoot": rm["overshoot"],
                "ref_settling": rm["settling"],
                "disturbance": dm["deviation"],
                "dist_settling": dm["settling"],
                "ref_saturation": rm["saturation"],
                "dist_saturation": dm["saturation"],
                "passed": bool(passed),
            }
        )

    with open(RESULTS_DIR / "monte_carlo.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "seed", "steady_error_C", "overshoot_percent",
                "reference_settling_h", "disturbance_deviation_C",
                "disturbance_settling_h", "reference_saturation_percent",
                "disturbance_saturation_percent", "passed",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["seed"], r["ess"], r["overshoot"], r["ref_settling"],
                    r["disturbance"], r["dist_settling"], r["ref_saturation"],
                    r["dist_saturation"], r["passed"],
                ]
            )

    seeds = np.array([r["seed"] for r in rows])
    mc_dist = np.array([r["disturbance"] for r in rows])
    mc_overshoot = np.array([r["overshoot"] for r in rows])

    plt.figure()
    plt.plot(seeds, mc_dist, marker="o")
    plt.axhline(MAX_DISTURBANCE_DEVIATION, linestyle="--", label="Criteriu")
    plt.xlabel("Seed zgomot")
    plt.ylabel("Abatere maxima [°C]")
    plt.title("Monte Carlo - rejetia perturbatiei")
    plt.grid()
    plt.legend()
    save_figure("07_monte_carlo_disturbance.png")

    plt.figure()
    plt.plot(seeds, mc_overshoot, marker="o")
    plt.axhline(MAX_OVERSHOOT, linestyle="--", label="Criteriu")
    plt.xlabel("Seed zgomot")
    plt.ylabel("Suprareglaj [%]")
    plt.title("Monte Carlo - suprareglaj")
    plt.grid()
    plt.legend()
    save_figure("08_monte_carlo_overshoot.png")

    return rows


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    _, _, ref_m, dist_m = run_nominal()
    corners = run_robustness()
    monte_carlo = run_monte_carlo(50)

    worst = max(corners, key=lambda x: x["dist"])
    n_corner_pass = sum(r["criteria_pass"] for r in corners)
    n_mc_pass = sum(r["passed"] for r in monte_carlo)

    print("\nCONFIGURATIA FINALA")
    print(f"Kp={K_P:g} W/°C, Ki={K_I:g} W/(°C*s), Ti={T_I:g} s")
    print(f"Te={T_E:g} s, Tf={T_FILTER:g} s")
    print("\nPERFORMANTE NOMINALE")
    print(f"ess = {ref_m['ess']:.4f} °C")
    print(f"Mp = {ref_m['overshoot']:.2f} %")
    print(f"ts_ref = {ref_m['settling']:.2f} h")
    print(f"dist = {dist_m['deviation']:.4f} °C")
    print(f"ts_dist = {dist_m['settling']:.2f} h")
    print("\nROBUSTETE")
    print(f"cazuri care respecta toate criteriile = {n_corner_pass}/{len(corners)}")
    print(
        "worst-case disturbance = "
        f"{worst['dist']:.4f} °C, ideal physical limit = {worst['ideal_dist']:.4f} °C, "
        f"gap = {worst['gap_percent']:.2f}%"
    )
    print("\nMONTE CARLO")
    print(f"simulari valide = {n_mc_pass}/{len(monte_carlo)}")


if __name__ == "__main__":
    main()

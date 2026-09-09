import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

# ============================================================
# PARAMETRII MODELULUI SI AI REGULATORULUI FINAL
# ============================================================

C_TH = 5_000_000.0       # [J/°C]
K_TH = 250.0             # [W/°C]
T_B = 3600.0             # [s]
L_DELAY = 1800.0         # [s]

P_MIN = 0.0              # [W]
P_MAX = 6000.0           # [W]

K_P = 450.0              # [W/°C]
T_I = 20000.0            # [s]
K_I = K_P / T_I          # [W/(°C*s)]

T_E = 60.0               # [s]
T_FILTER = 120.0          # [s]
Q_SENSOR = 0.1            # [°C]
SIGMA_NOISE = 0.2         # [°C]

RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)


def quantize(value, q):
    if q <= 0:
        return value
    return np.round(value / q) * q


def read_float(prompt, default=None):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw == "" and default is not None:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            print("Introdu o valoare numerica valida.")


def feasibility_message(t_ref, t_ext, label):
    required_power = K_TH * (t_ref - t_ext)
    max_maintainable = t_ext + P_MAX / K_TH

    print(f"\n{label}")
    print("-" * len(label))

    if t_ref < t_ext:
        print(f"Temperatura dorita = {t_ref:.2f} °C")
        print(f"Temperatura exterioara = {t_ext:.2f} °C")
        print("ATENTIE: modelul are numai incalzire, nu si racire.")
        print(
            "Cu centrala oprita, incaperea tinde natural spre temperatura "
            "exterioara, deci referinta nu poate fi mentinuta activ."
        )
        return False

    print(f"Putere stationara estimata necesara = {required_power:.1f} W")
    print(f"Putere maxima disponibila = {P_MAX:.1f} W")

    if required_power > P_MAX:
        print("ATENTIE: REFERINTA NU POATE FI MENTINUTA FIZIC.")
        print(
            "Chiar daca regulatorul cere 100% putere, pierderile termice "
            "sunt mai mari decat poate compensa centrala."
        )
        print(
            f"La aceasta temperatura exterioara, temperatura interioara "
            f"maxima mentenabila in regim stationar este aproximativ "
            f"{max_maintainable:.2f} °C."
        )
        return False

    print("Referinta este fezabila stationar pentru modelul nominal.")
    return True


def simulate(
    t_initial,
    t_reference,
    t_outside_initial,
    duration_h,
    t_outside_after=None,
    outside_change_h=None,
    seed=1,
):
    duration_s = duration_h * 3600.0
    n = int(duration_s / T_E) + 1
    time = np.arange(n) * T_E

    rng = np.random.default_rng(seed)

    temperature = np.zeros(n)
    measured = np.zeros(n)
    filtered = np.zeros(n)
    reference = np.full(n, t_reference)
    outside = np.full(n, t_outside_initial)
    error = np.zeros(n)
    p_ff = np.zeros(n)
    p_prop = np.zeros(n)
    p_int = np.zeros(n)
    p_requested = np.zeros(n)
    p_command = np.zeros(n)
    power = np.zeros(n)
    integral_state = np.zeros(n)

    if t_outside_after is not None and outside_change_h is not None:
        outside[time >= outside_change_h * 3600.0] = t_outside_after

    # Puterea care ar mentine temperatura initiala in conditiile initiale.
    p_initial_required = K_TH * (t_initial - t_outside_initial)
    p_initial = float(np.clip(p_initial_required, P_MIN, P_MAX))

    temperature[0] = t_initial
    power[0] = p_initial

    # Daca starea initiala este realizabila, regulatorul porneste fara soc.
    # Daca nu este realizabila, actuatorul porneste deja saturat.
    integral_state[0] = 0.0

    n_delay = int(round(L_DELAY / T_E))
    delay_buffer = [p_initial] * n_delay

    alpha = T_FILTER / (T_FILTER + T_E) if T_FILTER > 0 else 0.0

    # Aceeasi structura folosita la validarea finala:
    # putere de baza pentru starea initiala + FF pentru schimbarea lui Text
    # + feedback PI.
    p_base = K_TH * (t_initial - t_outside_initial)

    for k in range(n - 1):
        sensor_value = temperature[k] + rng.normal(0.0, SIGMA_NOISE)
        measured[k] = quantize(sensor_value, Q_SENSOR)

        if k == 0 or T_FILTER <= 0:
            filtered[k] = measured[k]
        else:
            filtered[k] = (
                alpha * filtered[k - 1]
                + (1.0 - alpha) * measured[k]
            )

        error[k] = reference[k] - filtered[k]

        p_ff[k] = K_TH * (t_outside_initial - outside[k])
        p_prop[k] = K_P * error[k]

        integral_candidate = integral_state[k] + T_E * error[k]
        p_integral_candidate = K_I * integral_candidate

        p_candidate = (
            p_base
            + p_ff[k]
            + p_prop[k]
            + p_integral_candidate
        )

        harmful_upper = p_candidate > P_MAX and error[k] > 0
        harmful_lower = p_candidate < P_MIN and error[k] < 0

        if harmful_upper or harmful_lower:
            integral_state[k + 1] = integral_state[k]
            p_int[k] = K_I * integral_state[k]
        else:
            integral_state[k + 1] = integral_candidate
            p_int[k] = p_integral_candidate

        p_requested[k] = p_base + p_ff[k] + p_prop[k] + p_int[k]
        p_command[k] = np.clip(p_requested[k], P_MIN, P_MAX)

        if n_delay > 0:
            p_delayed = delay_buffer.pop(0)
            delay_buffer.append(p_command[k])
        else:
            p_delayed = p_command[k]

        power[k + 1] = (
            power[k]
            + T_E * (p_delayed - power[k]) / T_B
        )

        temperature[k + 1] = (
            temperature[k]
            + T_E
            * (
                power[k]
                - K_TH * (temperature[k] - outside[k])
            )
            / C_TH
        )

    # Ultimul esantion pentru afisare.
    sensor_value = temperature[-1] + rng.normal(0.0, SIGMA_NOISE)
    measured[-1] = quantize(sensor_value, Q_SENSOR)
    filtered[-1] = (
        alpha * filtered[-2] + (1.0 - alpha) * measured[-1]
        if T_FILTER > 0 and n > 1
        else measured[-1]
    )
    error[-1] = reference[-1] - filtered[-1]
    p_ff[-1] = K_TH * (t_outside_initial - outside[-1])
    p_prop[-1] = K_P * error[-1]
    p_int[-1] = K_I * integral_state[-1]
    p_requested[-1] = p_base + p_ff[-1] + p_prop[-1] + p_int[-1]
    p_command[-1] = np.clip(p_requested[-1], P_MIN, P_MAX)

    return {
        "time": time,
        "temperature": temperature,
        "measured": measured,
        "filtered": filtered,
        "reference": reference,
        "outside": outside,
        "error": error,
        "p_ff": p_ff,
        "p_prop": p_prop,
        "p_int": p_int,
        "p_requested": p_requested,
        "p_command": p_command,
        "power": power,
    }


def settling_time(time, temperature, target, band=0.2):
    inside = np.abs(temperature - target) <= band
    outside_idx = np.where(~inside)[0]
    if len(outside_idx) == 0:
        return 0.0
    last = outside_idx[-1]
    if last >= len(time) - 1:
        return np.nan
    return time[last + 1] / 3600.0


def main():
    print("=" * 58)
    print("SIMULATOR DE CENTRALA TERMICA - REGULATOR PI DIGITAL")
    print("=" * 58)

    t_initial = read_float("Temperatura initiala din camera [°C]", 20.0)
    t_reference = read_float("Temperatura dorita [°C]", 22.0)
    t_outside_initial = read_float("Temperatura exterioara initiala [°C]", 10.0)
    duration_h = read_float("Durata simularii [h]", 24.0)

    raw = input(
        "Temperatura exterioara se modifica in timpul simularii? [d/N]: "
    ).strip().lower()

    t_outside_after = None
    outside_change_h = None

    if raw in {"d", "da", "y", "yes"}:
        t_outside_after = read_float(
            "Noua temperatura exterioara [°C]", 0.0
        )
        outside_change_h = read_float(
            "Momentul schimbarii temperaturii exterioare [h]", 4.0
        )

    feasibility_message(
        t_reference,
        t_outside_initial,
        "FEZABILITATE - CONDITII INITIALE",
    )

    if t_outside_after is not None:
        feasibility_message(
            t_reference,
            t_outside_after,
            "FEZABILITATE - DUPA SCHIMBAREA TEMPERATURII EXTERIOARE",
        )

    result = simulate(
        t_initial=t_initial,
        t_reference=t_reference,
        t_outside_initial=t_outside_initial,
        duration_h=duration_h,
        t_outside_after=t_outside_after,
        outside_change_h=outside_change_h,
        seed=1,
    )

    time_h = result["time"] / 3600.0
    final_hour_mask = result["time"] >= max(0.0, result["time"][-1] - 3600.0)
    final_temp = float(np.mean(result["temperature"][final_hour_mask]))
    final_error = t_reference - final_temp
    sat_percent = float(
        np.mean(
            np.isclose(result["p_command"], P_MIN)
            | np.isclose(result["p_command"], P_MAX)
        )
        * 100.0
    )
    ts = settling_time(
        result["time"],
        result["temperature"],
        t_reference,
        band=0.2,
    )

    print("\n" + "=" * 58)
    print("REZULTATUL SIMULARII")
    print("=" * 58)
    print(f"Temperatura medie in ultima ora = {final_temp:.3f} °C")
    print(f"Eroare fata de referinta = {final_error:.3f} °C")
    print(f"Putere maxima comandata = {np.max(result['p_command']):.1f} W")
    print(f"Timp in saturatie = {sat_percent:.2f} %")

    if np.isnan(ts):
        print("Timp de stabilire in banda ±0.2 °C = N/A")
    else:
        print(f"Timp de stabilire in banda ±0.2 °C = {ts:.2f} h")

    # ========================================================
    # GRAFIC TEMPERATURI
    # ========================================================
    plt.figure()
    plt.plot(time_h, result["temperature"], label="Temperatura interioara")
    plt.plot(time_h, result["reference"], linestyle="--", label="Referinta")
    plt.plot(time_h, result["outside"], linestyle=":", label="Temperatura exterioara")
    plt.xlabel("Timp [h]")
    plt.ylabel("Temperatura [°C]")
    plt.title("Simulator centrala - temperaturi")
    plt.grid()
    plt.legend()
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "interactive_temperature.png", dpi=200)
    plt.show()

    # ========================================================
    # GRAFIC PUTERE
    # ========================================================
    plt.figure()
    plt.step(time_h, result["p_command"], where="post", label="Putere comandata")
    plt.plot(time_h, result["power"], label="Putere efectiva")
    plt.axhline(P_MAX, linestyle="--", label="Pmax")
    plt.xlabel("Timp [h]")
    plt.ylabel("Putere [W]")
    plt.title("Simulator centrala - putere")
    plt.grid()
    plt.legend()
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "interactive_power.png", dpi=200)
    plt.show()

    print("\nGrafice salvate in folderul results/:")
    print("- interactive_temperature.png")
    print("- interactive_power.png")


if __name__ == "__main__":
    main()

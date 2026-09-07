import cv2
import mediapipe as mp
import serial
import time
import math
import collections

# Initialize serial communication with Arduino
try:
    arduino = serial.Serial(port='COM3', baudrate=9600, timeout=1)
    time.sleep(2)
except serial.SerialException as e:
    # Adauga un fallback pentru a permite rularea codului fara Arduino conectat
    print(f"Eroare la conectarea seriala: {e}. Continua fara Arduino.")
    class DummyArduino:
        def write(self, *args):
            pass
        def close(self):
            pass
    arduino = DummyArduino()


# Initialize Mediapipe
mp_hands = mp.solutions.hands # modulul pt detectarea mainilor
hands = mp_hands.Hands() # initializeaza detectorul de miani
mp_drawing = mp.solutions.drawing_utils # desenarea landmark urilor detectate pe imagine

# ------------------------------
# Parametri/constante modificate
# ------------------------------


# Parametri pentru incertitudinea dinamica
N = 10 # numarul de frame-uri luate in calcul (pentru estimarea sigma (deviatia standard)) 
       #pt fiecare deget se tin minte ultimele 10 valori de unghi
       
finger_buffers = { #pt fiecare deget se pastreaza ultimele N unghiuri
    'Thumb': collections.deque(maxlen=N),
    'Index': collections.deque(maxlen=N),
    'Middle': collections.deque(maxlen=N),
    'Ring': collections.deque(maxlen=N),
    'Pinky': collections.deque(maxlen=N)
}
finger_names = ['Thumb', 'Index', 'Middle', 'Ring', 'Pinky'] #defineste ordinea logica a degetelor


# Modele statistice specifice pentru fiecare deget
# Format: 'Nume': (mu_extins, mu_strans, sigma)
FINGER_MODELS = {
    'Thumb':  (165.0, 125.0, 25.0), # Thumb are cursa mai scurta, deci mu_strans e mai mare
    'Index':  (170.0, 60.0, 40.0),
    'Middle': (170.0, 60.0, 40.0),
    'Ring':   (170.0, 60.0, 40.0),
    'Pinky':  (170.0, 60.0, 40.0)
}

# Initializam prior-ul la 0.5 (stare neutra)
priors_state = {name: 0.5 for name in finger_names}

# Memorie pentru Prior (starea anterioara)
# Initializam cu 0.5 (incertitudine maxima: 50% sanse sa fie intins)

def likelihood_pure(theta, mu, sigma):
    """PDF Normal: N(theta | mu, sigma)."""
    # protectie: sigma nu are voie sa fie 0
    sigma = max(float(sigma), 1e-6)
    z = (theta - mu) / sigma
    return (1.0 / (sigma * math.sqrt(2.0 * math.pi))) * math.exp(-0.5 * z * z)

def stabilize_angles(finger_buffers, N = 10):
    """
    Stabilizeaza unghiurile degetelor pe o perioada mai lunga, folosind o medie pe N cadre.
    Returneaza tuple (mean_angle, sigma) pentru fiecare deget.
    """
    stabilized_angles = {}
    for name in finger_buffers:
        buffer = list(finger_buffers[name])
        if len(buffer) >= N:
            mean_angle = sum(buffer) / len(buffer) #mmedia 
            variance = sum((x - mean_angle)**2 for x in buffer) / len(buffer) #varianta
            sigma = math.sqrt(variance) #deviatia standard 
            stabilized_angles[name] = (mean_angle, sigma)
        elif buffer:
            stabilized_angles[name] = (buffer[-1], 0.0)
        else:
            stabilized_angles[name] = (0.0, 0.0)
    return stabilized_angles


# def check_for_closed_hand(angles):
#     """
#     Verifica daca toate unghiurile sunt sub 30 de grade (pumn relaxat) sau daca sunt intre 30 si 100 grade.
#     """
#     if all(angle < 30 for angle in angles):
#         return "pumn relaxat"
#     elif all(angle > 100 for angle in angles):
#         return "pumn strans"
#     return "zona gri" # intre 30 si 100 grade


# ==============================
# Functii matematice
# ==============================
def calculate_angle(A, B, C):
    """Calculeaza unghiul (in grade) la punctul B format de segmentele BA si BC."""
    ABx = A.x - B.x
    ABy = A.y - B.y
    CBx = C.x - B.x
    CBy = C.y - B.y
    dot = ABx * CBx + ABy * CBy
    normAB = math.hypot(ABx, ABy)
    normCB = math.hypot(CBx, CBy)
    if normAB * normCB == 0:
        return 0.0
    cos_theta = dot / (normAB * normCB)
    cos_theta = max(min(cos_theta, 1.0), -1.0)   #mentine cosinusul intre -1 si 1
    return math.degrees(math.acos(cos_theta))

# ==============================
# Functii auxiliare
# ==============================
def count_fingers(fingers_state):
    """Calculeaza numarul de degete extinse (starea binara)."""
    return sum(fingers_state)

# ==============================
# Filtrare pentru afisare (logica veche pastrata pentru display)
# ==============================
finger_history = []
HISTORY_LENGTH = 5

def filtered_finger_count_for_display(raw_count):
    """Netezirea numarului de degete pentru afisajul CV2."""
    finger_history.append(raw_count)
    if len(finger_history) > HISTORY_LENGTH:
        finger_history.pop(0)
    return round(sum(finger_history) / len(finger_history))

# ==============================
# Functii UNIFICATE si Probabilistice
# ==============================

def compute_probabilities_bayesian(angles, q_flip=0.02):
    global priors_state
    probs = []

    for i, name in enumerate(finger_names):
        theta = angles[i]
        mu_ext, mu_flex, sigma_model = FINGER_MODELS[name]

        prior = priors_state[name]
        prior = min(max(prior, 1e-12), 1.0 - 1e-12)

        l_ext  = likelihood_pure(theta, mu_ext,  sigma_model)
        l_flex = likelihood_pure(theta, mu_flex, sigma_model)
 
        # Rescalare (nu schimba posteriorul)
        scale = max(l_ext, l_flex)

        if scale > 0.0:
            l_ext  /= scale
            l_flex /= scale
            denom = l_ext * prior + l_flex * (1.0 - prior)
            posterior = (l_ext * prior) / denom
        else:
            # caz extrem: ambele pdf-uri au subfluat la 0
            sigma = max(float(sigma_model), 1e-6)
            z_ext  = (theta - mu_ext) / sigma
            z_flex = (theta - mu_flex) / sigma
            expo = -0.5 * (z_flex*z_flex - z_ext*z_ext)  # = log( l_flex / l_ext )

            # FARA praguri: lasam exp sa ridice OverflowError
            try:
                ratio = math.exp(expo)   # l_flex / l_ext
            except OverflowError:
                ratio = float('inf')     # expo foarte mare => l_flex >> l_ext

            t = ((1.0 - prior) / prior) * ratio

            # stabil pentru t foarte mare
            if math.isinf(t):
                posterior = 0.0
            else:
                posterior = 1.0 / (1.0 + t)

        probs.append(posterior)
        priors_state[name] = (1.0 - q_flip) * posterior + q_flip * (1.0 - posterior)

    return probs
def finger_count_pmf(probabilities):
    """
    PMF pentru K = numarul de degete extinse.
    Poisson-binomial (convolutie). Returneaza lista de lungime 6: P(K=0..5).
    """
    pmf = [1.0]  # P(K=0)=1 initial
    for p in probabilities:
        new = [0.0] * (len(pmf) + 1)
        for k in range(len(new)):
            stay0 = pmf[k] * (1.0 - p) if k < len(pmf) else 0.0
            add1  = pmf[k-1] * p       if k-1 >= 0 else 0.0
            new[k] = stay0 + add1
        pmf = new
    return pmf  # len 6

def map_finger_count(probabilities):
    pmf = finger_count_pmf(probabilities)
    k_map = max(range(len(pmf)), key=lambda k: pmf[k])
    p_map = pmf[k_map]
    expected = sum(k * pmf[k] for k in range(len(pmf)))
    return k_map, p_map, expected, pmf

def expected_finger_count(probabilities):
    """
    Estimeaza numarul de degete intinse pe baza posteriorului bayesian.
    Returneaza valoare intre 0 si 5.
    """
    return sum(probabilities)


# Functie pentru a calcula medii si deviatii standard pe un buffer mai mare

def get_finger_angles(hand_landmarks):
    """Extrage unghiurile pentru toate cele 5 degete."""
    angles = [0] * 5
    index_angle = 0 # Unghiul Indexului pentru afisare
     
    try:
        lm = hand_landmarks.landmark
        
        # Thumb
        thumb_angle = calculate_angle(lm[2], lm[3], lm[4])
        angles[0] = thumb_angle
        
        # Celelalte degete
        fingers = {
            1: (5, 6, 8), # Index (MCP, PIP, TIP)
            2: (9, 10, 12),
            3: (13, 14, 16),
            4: (17, 18, 20)
        }
        for idx, (mcp_i, pip_i, tip_i) in fingers.items():
            angle = calculate_angle(lm[mcp_i], lm[pip_i], lm[tip_i])
            angles[idx] = angle
            if idx == 1:
                index_angle = angle
    except Exception:
        index_angle = 0
        angles = [0] * 5
        pass
        
    return index_angle, angles

# Parola
PASSWORD = [4, 3, 0, 2]
password_index = 0
unlocked = False

# Stabilizare
stable_fingers = 0
stable_start_time = 0
stable_threshold = 1.0

# Cooldown
cooldown_start_time = 0
cooldown_duration = 5
cooldown_active = False
cooldown_message_printed = False

# LED progress
led_progress = [0, 0, 0, 0, 0]

# Stabilizare pentru reset (ambele maini)
stable_hands_start_time = 0

# Ignorare input dupa reset/deblocare
ignore_input_until = 0
ignore_message = ""

cap = cv2.VideoCapture(0) #deschide camera cu indexul 0 (camera principala)

def freeze_flash_success(flash_count=5, flash_duration=0.4):
    for _ in range(flash_count):
        arduino.write(bytes([1,1,1,1,1]))
        start = time.time()
        while time.time() - start < flash_duration:
            ret, img = cap.read()
            if not ret:
                continue
            img = cv2.cvtColor(cv2.flip(img, 1), cv2.COLOR_BGR2RGB)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            cv2.putText(img, "Sistem deblocat!", (50, 200),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
            cv2.imshow('Hand Tracking', img)
            cv2.waitKey(1)
        arduino.write(bytes([0,0,0,0,0]))
        start = time.time()
        while time.time() - start < flash_duration:
            ret, img = cap.read()
            if not ret:
                continue
            img = cv2.cvtColor(cv2.flip(img, 1), cv2.COLOR_BGR2RGB)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB) 
            cv2.putText(img, "Sistem deblocat!", (50, 200),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
            cv2.imshow('Hand Tracking', img)
            cv2.waitKey(1)

def enter_ignore_mode(message, duration=1.0):
    global ignore_input_until, ignore_message
    ignore_input_until = time.time() + duration
    ignore_message = message
    for _ in range(5):
        cap.grab()

def check_landmarks_visibility(hand_landmarks, finger_indices):
    lm = hand_landmarks.landmark
    visible = []
    for idx in finger_indices:
        x, y = lm[idx].x, lm[idx].y
        if x < 0 or y < 0 or x > 1 or y > 1:
            visible.append(False)
        else:
            visible.append(True)
    return visible



def get_final_binary_state(probabilities):
    """
    Returneaza direct probabilitatile Bayesian, fara praguri binare.
    """
    return probabilities

# ==============================
# Bucla principala (RESCRISA)
# ==============================
while cap.isOpened():
    success, image = cap.read()
    if not success:
        break

    image = cv2.cvtColor(cv2.flip(image, 1), cv2.COLOR_BGR2RGB)
    results = hands.process(image)
    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

    current_time = time.time()
    index_angle_display = 0

    # --------------------------
    # COOLDOWN 
    # --------------------------
    if cooldown_active:
        elapsed = int(current_time - cooldown_start_time)
        if elapsed >= cooldown_duration:
            cooldown_active = False
            cooldown_message_printed = False
            password_index = 0
            led_progress = [0, 0, 0, 0, 0]
            arduino.write(bytes(led_progress))
            finger_history.clear()
        else:
            blink = 1 if int((current_time - cooldown_start_time) * 2) % 2 == 0 else 0
            led_state = [blink] * 5
            arduino.write(bytes(led_state))

            cv2.putText(
                image,
                f"Parola gresita! Sistem blocat {cooldown_duration - elapsed}s",
                (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2
            )
            if not cooldown_message_printed:
                print("Parola gresita! Sistem blocat 5 secunde.")
                cooldown_message_printed = True

        cv2.imshow('Hand Tracking', image)

        key = cv2.waitKey(1) & 0xFF

        # --- BYPASS PAROLA CU TASTA X ---
        if key == ord('x') or key == ord('X'):
            print("Bypass activat! Sistem deblocat instant.")
            unlocked = True
            password_index = len(PASSWORD)
            led_progress = [1, 1, 1, 1, 1]
            arduino.write(bytes(led_progress))
            enter_ignore_mode("Sistem deblocat prin bypass!", 1.0)
            freeze_flash_success(flash_count=3, flash_duration=0.25)
            continue

        if key == 27:
            break

        continue

    # --------------------------
    # Ignorare input dupa reset/deblocare
    # --------------------------
    if current_time < ignore_input_until:
        ret, img = cap.read()
        if ret:
            img = cv2.flip(img, 1)
            cv2.putText(img, ignore_message, (50, 200),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
            cv2.imshow('Hand Tracking', img)
            if cv2.waitKey(5) & 0xFF == 27:
                break
        continue

    hand_present = False

    # --------------------------
    # Daca avem maini detectate
    # --------------------------
    if results.multi_hand_landmarks:
        hand_present = True

        # ==========================================================
        # RESET sistem cu 2 maini cand e UNLOCKED (fixat bayesian)
        # ==========================================================
        if unlocked and len(results.multi_hand_landmarks) == 2:
            temp_digits = []
            for hand in results.multi_hand_landmarks:
                _, angles_all = get_finger_angles(hand)

                # posterior per deget (bayes)
                probs = compute_probabilities_bayesian(angles_all)

                # cifra MAP (bayes pe K=0..5)
                k_map, p_map, expected, pmf = map_finger_count(probs)
                temp_digits.append(k_map)

            # pastram logica ta: "ambele maini arata ceva > 0"
            if all(d > 0 for d in temp_digits):
                if stable_hands_start_time == 0:
                    stable_hands_start_time = current_time
                elif current_time - stable_hands_start_time >= stable_threshold:
                    print("Reset sistem activat!")
                    password_index = 0
                    unlocked = False
                    stable_start_time = 0
                    stable_fingers = 0
                    cooldown_active = False
                    cooldown_message_printed = False
                    led_progress = [0, 0, 0, 0, 0]
                    arduino.write(bytes([0, 0, 0, 0, 0]))
                    enter_ignore_mode("Sistem resetat! Introdu parola.", 1.0)
                    stable_hands_start_time = 0
            else:
                stable_hands_start_time = 0
        else:
            stable_hands_start_time = 0

        # ==========================================================
        # Procesare maini pentru parola si LED-uri
        # ==========================================================
        for hand_landmarks in results.multi_hand_landmarks:
            mp_drawing.draw_landmarks(image, hand_landmarks, mp_hands.HAND_CONNECTIONS)

            # 1) Unghiuri
            current_angle_idx, angles_all = get_finger_angles(hand_landmarks)
            index_angle_display = current_angle_idx

            # 2) Probabilitati bayesiene per deget
            fingers_prob = compute_probabilities_bayesian(angles_all)

            # 3) Bayes pe numarul degete: K=0..5 (MAP + incredere)
            k_map, p_map, expected, pmf = map_finger_count(fingers_prob)

            # Asta e "cifra" pe care o folosim peste tot (parola, stabilizare)
            raw_count_digit = int(k_map)          # 0..5
            raw_count_conf = float(p_map)         # 0..1

            # Pentru afisaj, pastram netezirea (dar acum pe cifra)
            display_count = filtered_finger_count_for_display(raw_count_digit)

            # --------------------------
            # AFISARE + LED-uri
            # --------------------------
            if unlocked:
                # Probabilitati per deget in colt dreapta sus
                h, w, _ = image.shape
                for i, prob in enumerate(fingers_prob):
                    cv2.putText(
                        image,
                        f'{finger_names[i]}: {prob*100:5.1f}%',
                        (w - 250, 30 + i * 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2
                    )

                # Afisare cifra MAP + incredere
                cv2.putText(
                    image,
                    f'Digit MAP: {raw_count_digit} ({raw_count_conf*100:.0f}%)',
                    (50, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2
                )

                print(f"MAP digit: {raw_count_digit} ({raw_count_conf*100:.0f}%), E[K]={expected:.2f}")

                # LED-uri in functie de prob per deget (0..255)
                led_values = [1 if p >= 0.5 else 0 for p in fingers_prob]
                arduino.write(bytes(led_values))
            else:
                print(f"Citit momentan (MAP): {raw_count_digit} ({raw_count_conf*100:.0f}%)")

            cv2.putText(
                image,
                f'Degete: {display_count}',
                (50, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2
            )

            # Unghiuri in colt dreapta jos (nemodificat)
            h, w, _ = image.shape
            for i, angle in enumerate(angles_all):
                if angle > 0:
                    cv2.putText(
                        image,
                        f'{finger_names[i]}: {angle:.2f}',
                        (w - 300, h - 50 - i * 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 128, 0), 2
                    )

            # --------------------------
            # Stabilizare parola (ACUM pe cifra MAP, bayesian)
            # --------------------------
            if not unlocked:
                # (optional, dar recomandat): poti cere o incredere minima pe cifra
                # ca sa nu-ti intre zgomot in parola. Ramane strict bayesian (decizie pe posterior).
                MIN_DIGIT_CONF = 0.55

                if raw_count_conf < MIN_DIGIT_CONF:
                    # Daca nu suntem suficient de siguri, nu stabilizam (eviti resetari aiurea)
                    stable_start_time = 0
                    stable_fingers = raw_count_digit
                else:
                    if raw_count_digit == stable_fingers:
                        if stable_start_time == 0:
                            stable_start_time = current_time
                        elif current_time - stable_start_time >= stable_threshold:
                            if raw_count_digit == PASSWORD[password_index]:
                                password_index += 1
                                stable_start_time = 0
                                stable_fingers = 0
                                print(f"Cifra corecta! Progress: {password_index}/{len(PASSWORD)}")
                                led_progress[password_index - 1] = 1
                                arduino.write(bytes(led_progress))

                                if password_index == len(PASSWORD):
                                    unlocked = True
                                    print("Parola corecta! Sistem deblocat!")
                                    enter_ignore_mode("Sistem deblocat!", 1.0)
                                    freeze_flash_success(flash_count=5, flash_duration=0.4)
                                    led_progress = [0, 0, 0, 0, 0]
                            else:
                                print("Numar gresit! Parola resetata.")
                                password_index = 0
                                stable_start_time = 0
                                stable_fingers = raw_count_digit
                                cooldown_active = True
                                cooldown_start_time = current_time
                    else:
                        stable_fingers = raw_count_digit
                        stable_start_time = current_time

    else:
        # daca nu e mana prezenta, golim istoricul (nemodificat)
        finger_history.clear()
        hand_present = False
        stable_start_time = 0
        stable_fingers = 0

    # --------------------------
    # Daca nu e mana prezenta: LED-uri
    # --------------------------
    if not hand_present:
        if not unlocked:
            arduino.write(bytes(led_progress))
            cv2.putText(image, "Arata un semn pentru a incepe", (50, 100),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        else:
            arduino.write(bytes([0, 0, 0, 0, 0]))

    # --------------------------
    # Afisare progres parola (nemodificat)
    # --------------------------
    if not unlocked:
        start_x = 50
        start_y = 150
        radius = 20
        gap = 50
        for i in range(len(PASSWORD)):
            color = (0, 255, 0) if i < password_index else (0, 0, 255)
            cv2.circle(image, (start_x + i * gap, start_y), radius, color, -1)

    cv2.imshow('Hand Tracking', image)
    if cv2.waitKey(5) & 0xFF == 27:
        break
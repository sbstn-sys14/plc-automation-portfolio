# Sistem de acces controlat prin recunoașterea secvențială a degetelor

Lucrare de licență — Automatică și Informatică Aplicată, Universitatea Transilvania din Brașov (2026).
Lucrare prezentată la conferința **AFCO — Absolvenți în Fața Companiilor**.

Sistem de control al accesului în care codul de deblocare se introduce printr-o **secvență de gesturi**. Un traductor optic (cameră web) furnizează mărimea de intrare, o unitate de procesare o filtrează și decide, iar un microcontroler comandă elementele de semnalizare.

Intrarea vine dintr-un flux video, dar problema rezolvată este una de automatizare: **conducerea unui proces secvențial pe baza unui semnal de măsură zgomotos**, cu filtrare, temporizări de stabilizare, interblocări de siguranță și comunicație serială către un controler de ieșiri.

![Detecția mâinii și estimarea numărului de degete](docs/detectie-mana.png)

---

## Corespondența cu conceptele de automatizare

| Element din sistem | Concept de automatizare |
|---|---|
| Cameră web + MediaPipe → unghiuri articulare | traductor și condiționare de semnal |
| Mediere pe fereastră glisantă + estimare bayesiană | filtrarea semnalului de măsură |
| `MIN_DIGIT_CONF = 0.55` | prag de validare a măsurii înainte de a comanda procesul |
| `stable_threshold = 1.0` s | temporizare la anclanșare (TON) — anti-bounce pe intrare |
| `PASSWORD` + `password_index` | control secvențial pe pași, cu tranziții condiționate |
| `cooldown_duration = 5` s | interblocare cu resetare temporizată la eroare |
| Reset cu ambele mâini menținute 1 s | comandă de reset cu confirmare, protejată la declanșare accidentală |
| `arduino.write(bytes([...]))` | scrierea ieșirilor digitale la finalul ciclului |
| Bucla `while cap.isOpened()` | ciclu de execuție: citire intrări → prelucrare → decizie → scriere ieșiri |

Structura buclei principale respectă același tipar ca ciclul de scanare al unui automat programabil: toate intrările se citesc la începutul ciclului, decizia se ia pe imaginea coerentă a procesului, iar ieșirile se scriu o singură dată, la final.

---

## Arhitectura sistemului

![Arhitectura generală a sistemului](docs/arhitectura-sistem.png)

Sistemul e organizat pe trei niveluri:

| Nivel | Componentă | Funcție |
|---|---|---|
| Câmp | Cameră web | achiziția mărimii de intrare |
| Control | Aplicație Python | filtrare, estimare, logică secvențială, comandă |
| Execuție / semnalizare | Arduino + 5 LED-uri | acționarea ieșirilor digitale, feedback către operator |

Modulul de analiză a gesturilor lucrează cu o **memorie probabilistică**: starea estimată la ciclul anterior devine informația a priori pentru ciclul curent. Astfel, o oscilație de câteva grade nu răstoarnă decizia, dar o schimbare reală de poziție e urmărită în câteva cicluri.

---

## Prelucrarea semnalului de măsură

Unghiul măsurat cadru cu cadru oscilează prea mult pentru a comanda direct o ieșire. Se aplică două straturi de prelucrare.

### 1. Mediere pe fereastră glisantă

`stabilize_angles()` menține pentru fiecare deget ultimele N = 10 valori și calculează media și deviația standard. Este echivalentul unui filtru de netezire aplicat pe o intrare analogică înainte de comparare.

### 2. Estimare bayesiană recursivă

Fiecare deget e tratat ca un proces cu două stări posibile — **extins** sau **strâns**. Pentru fiecare stare există un model normal al unghiului, cu parametri determinați experimental (`FINGER_MODELS`):

$$p(\theta \mid H) = \frac{1}{\sigma\sqrt{2\pi}} \, e^{-\frac{(\theta-\mu_H)^2}{2\sigma^2}}$$

Decizia nu se ia comparând unghiul cu un prag fix, ci aplicând **teorema lui Bayes** pentru a obține probabilitatea a posteriori ca degetul să fie extins, dat fiind unghiul măsurat:

$$P(E \mid \theta) = \frac{p(\theta \mid E)\,P(E)}{p(\theta \mid E)\,P(E) + p(\theta \mid S)\,P(S)}$$

unde $E$ = deget extins, $S$ = deget strâns, $P(E)$ este probabilitatea a priori (starea estimată la ciclul anterior), iar $p(\theta \mid \cdot)$ sunt verosimilitățile date de cele două modele normale.

Rezultatul devine informația a priori pentru ciclul următor, printr-un model de tranziție care permite schimbarea stării cu probabilitatea $q = 0{,}02$ per ciclu:

$$P_{k+1}(E) = (1-q)\,P_k(E \mid \theta) + q\,\bigl(1 - P_k(E \mid \theta)\bigr)$$

Termenul $q$ este esențial: fără el, estimatorul s-ar bloca într-o stare după câteva cicluri de confirmare și n-ar mai urmări procesul. Cu el, filtrul rămâne stabil la zgomot, dar reactiv la o schimbare reală. Implementarea se află în `compute_probabilities_bayesian()` și include rescalarea verosimilităților, pentru a evita depășirea inferioară a intervalului numeric când unghiul e departe de ambele modele.

### 3. De la 5 probabilități la o cifră

Numărul de degete extinse nu se obține numărând stările binare, ci din **distribuția Poisson-binomială** peste cele 5 probabilități (`finger_count_pmf()`):

$$P(K = k) = \sum_{\substack{A \subseteq \{1..5\} \\ |A| = k}} \prod_{i \in A} p_i \prod_{j \notin A} (1 - p_j)$$

calculată prin convoluție. Se rețin valoarea cea mai probabilă (estimatorul MAP) și nivelul ei de încredere, iar acesta din urmă condiționează dacă decizia are voie să ajungă la logica de comandă.

---

## Logica de control secvențial

**Codul implicit este `4 → 3 → 0 → 2`** (numărul de degete arătate la fiecare pas), definit în `main.py`:

```python
PASSWORD = [4, 3, 0, 2]
```

![Secvența de deblocare și feedback-ul pe LED-uri](docs/secventa-deblocare.png)

Procesul avansează pas cu pas, fiecare tranziție fiind condiționată de o cifră corectă menținută stabil. Mecanismele implementate:

| Mecanism | Parametru | Rol |
|---|---|---|
| Temporizare de stabilizare | `stable_threshold = 1.0` s | O cifră se validează doar dacă se menține stabilă o secundă. Elimină comutările parazite pe intrare. |
| Prag de validare a măsurii | `MIN_DIGIT_CONF = 0.55` | Sub acest nivel de încredere nu se ia nicio decizie: zgomotul nu ajunge să comande procesul. |
| Interblocare la eroare | `cooldown_duration = 5` s | Cifră greșită → proces blocat 5 secunde, semnalizare intermitentă, progres resetat la pasul 0. |
| Reset cu confirmare | ambele mâini menținute 1 s | Comanda de reset cere un gest deliberat și susținut. |
| Ignorarea intrărilor după tranziții | `enter_ignore_mode()` | Blochează citirile imediat după deblocare sau reset, până când procesul revine în stare de repaus. |

Aceleași mecanisme — pași, tranziții condiționate, temporizări de validare, interblocare la eroare — sunt cele pe care le-aș implementa într-un SFC sau într-o mașină de stări pe `CASE` în Structured Text; aici sunt scrise în Python pentru că unitatea de procesare trebuie să execute și partea de vedere artificială.

---

## Montajul hardware

![Montajul: Arduino și cele 5 LED-uri pe breadboard](docs/montaj-hardware.png)

- Placă Arduino Uno (compatibilă), conectată prin USB
- 5 LED-uri pe ieșirile digitale **8, 9, 10, 11, 12**, în ordinea Thumb, Index, Middle, Ring, Pinky
- Cameră web

![Sistemul în funcțiune](docs/montaj-in-functiune.png)

## Protocolul de comunicație

Unitatea de procesare trimite exact **5 octeți** per actualizare, câte unul pentru fiecare ieșire, cu valoarea 0 sau 1, pe UART la 9600 baud:

```python
arduino.write(bytes([1, 0, 1, 1, 0]))
```

Firmware-ul așteaptă cei 5 octeți înainte de a actualiza ieșirile, ceea ce garantează că starea scrisă e întotdeauna completă și coerentă:

```cpp
if (Serial.available() >= 5) { ... }
```

| Stare proces | Semnalizare |
|---|---|
| Introducere cod | progresul: câte cifre corecte s-au validat |
| Cifră greșită | toate ieșirile clipesc 5 secunde |
| Deblocat | starea instantanee a celor 5 degete (probabilitate ≥ 0,5) |
| Deblocare reușită | cinci pulsuri pe toate ieșirile |

---

## Structura proiectului

```
.
├── main.py                              # achiziție, filtrare, estimare bayesiană, logică secvențială, UART
├── arduino/led_controller/
│   └── led_controller.ino               # firmware: citește 5 octeți pe serial, comandă 5 ieșiri
├── requirements.txt
├── docs/                                # schema bloc, capturi de ecran, poze montaj
└── README.md
```

## Instalare și rulare

```bash
git clone https://github.com/sbstn-sys14/finger-sequence-access-control.git
cd finger-sequence-access-control

python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

Înainte de rulare, verifică portul serial în `main.py`:

```python
SERIAL_PORT = 'COM3'   # Windows; pe Linux tipic '/dev/ttyUSB0'
```

Încarcă `arduino/led_controller/led_controller.ino` pe placă din Arduino IDE, apoi:

```bash
python main.py
```

Aplicația pornește și fără controlerul conectat (se folosește un obiect substitut), astfel încât logica de comandă să poată fi testată separat de partea de execuție. Oprire: tasta `Esc`.

## Note

Constanta `DEBUG` din `main.py` este `False`. Setată pe `True`, activează o scurtătură de test (tasta `X`) care deblochează sistemul fără cod — utilă doar la punerea în funcțiune.

## Direcții de extindere

- Portarea logicii secvențiale pe un automat programabil, cu cifra validată primită pe o magistrală de câmp și cu ieșirile comandate direct din Structured Text — partea de vedere artificială ar rămâne pe PC, ca sistem de nivel superior.
- Înlocuirea semnalizării pe LED-uri cu comanda unui element de execuție real (electromagnet de zăvor, contactor), cu bucla de siguranță tratată separat de logica funcțională.

## Tehnologii

Python · OpenCV · MediaPipe · pySerial · Arduino (C/C++) · UART · estimare bayesiană

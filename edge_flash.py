"""
Edge Flash - overlay di flash e laser ai bordi dello schermo, a tempo di musica.

LUCI: la posizione del tasto sulla tastiera decide tutto. La direzione del tasto
rispetto al centro della tastiera diventa la direzione della luce sullo schermo,
a 360 gradi. La distanza dal centro decide l'intensità: G, H, T, Y fanno le luci
più forti (secche, intense, con i laser), i tasti sul bordo della tastiera quelle
più morbide (lente, ampie, tenui). La barra spaziatrice è lo special.

DISEGNO: sulla GPU. L'overlay è una finestra OpenGL trasparente composta da Windows, e
tutti gli effetti sono immagini preparate una volta sola e caricate sulla scheda video
all'avvio: durante la sessione la GPU le scala, le ruota e le sfuma da sé, senza che la
CPU rigeneri o ricarichi niente. Se la tua scheda video dà problemi (per esempio uno
sfondo nero invece che trasparente), dal menu della tray scegli Rendering > CPU.

Ctrl+Alt+F  accende / spegne la modalità fidget
Ctrl+Alt+D  cambia quanto viene scurito lo schermo (spento, leggero, medio, forte)
Ctrl+Alt+L  laser: spenti, insieme ai flash, solo laser
Ctrl+Alt+M  modalità: Quiet (punteggio discreto in alto) o Loud (punteggio grande al centro)
Esc         spegne la modalità fidget
Icona nella tray (vicino all'orologio): palette, scurimento, laser, analisi musica, esci.
Le impostazioni vengono ricordate al prossimo avvio.

TASTIERA: niente keylogger. Le tre scorciatoie sono registrate con RegisterHotKey,
l'API standard di Windows per i tasti rapidi globali: Windows avvisa l'app solo
quando premi quelle combinazioni. Durante la sessione Edge Flash prende il focus
con una finestrella invisibile e riceve i tasti come qualsiasi app in primo piano;
quando chiudi la sessione il focus torna alla finestra su cui stavi lavorando.
Se clicchi su un'altra finestra o fai Alt+Tab, la sessione si chiude da sola.
Nessun tasto viene salvato o inviato da nessuna parte.

ANALISI MUSICA: l'app ascolta l'audio in uscita dal PC (non il microfono),
trova i colpi della canzone e ricava BPM e battute. A ogni tap ti dice se eri
a tempo. La latenza delle cuffie viene calibrata da sola sui primi tap.
Misura anche l'energia del momento: luci morbide nelle parti calme e luci forti
in quelle cariche valgono più punti, e lo special vale un bonus se lo usi sul drop.
L'audio viene analizzato al volo e mai registrato.
"""

import cmath
import ctypes
import math
import os
import subprocess
import random
import sys
import threading
import time
from collections import deque
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QFontMetrics, QIcon, QImage,
                           QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient,
                           QRegion)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

try:  # disegno sulla GPU: QPainter su una superficie OpenGL usa il motore accelerato di Qt
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtOpenGL import QOpenGLWindow
    GPU_AVAILABLE = True
except Exception:
    GPU_AVAILABLE = False

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
    AUDIO_AVAILABLE = True
except Exception:  # librerie mancanti: l'app funziona lo stesso, senza analisi
    AUDIO_AVAILABLE = False

# ---------------------------------------------------------------- impostazioni
LIFE = 0.7            # durata di un flash forte in secondi
SOFT_LIFE = 1.6       # durata di una luce morbida (sale piano e svanisce piano)
SOFT_ATTACK = 0.18    # secondi per arrivare al massimo, per le luci morbide
LASER_LIFE = 0.55     # durata di un laser in secondi
SPECIAL_LIFE = 1.1    # durata dell'effetto della barra spaziatrice
SPECIAL_COOLDOWN = 0.6  # lo special non si ripete più spesso di così

LASER_MIN_POWER = 0.6   # i laser partono solo dai tasti abbastanza vicini al centro

# Prestazioni
FRAME_MS = 16           # ~60 fotogrammi al secondo, solo quando c'è qualcosa da animare
MAX_FLASHES = 40        # luci contemporanee al massimo (le più vecchie lasciano il posto)
MAX_LASERS = 12
SPRITE_SIZE = 128       # risoluzione degli aloni pre-renderizzati (vengono scalati)

# Energia della canzone (volume rispetto alla sua stessa dinamica, 0..1).
# Il punteggio confronta l'intensità del tasto con l'energia del momento.
ENERGY_TOLERANCE = 0.25   # entro questa differenza la luce è "giusta"
ENERGY_OFF = 0.5          # oltre questa differenza è proprio fuori luogo
ENERGY_PEAK = 0.8         # sopra: picco, il momento dello special
ENERGY_RISE = 0.25        # ...oppure energia alta che sale così tanto in un secondo (il drop)
ENERGY_MATCH = 1.5        # moltiplicatore se la luce è adatta al momento
ENERGY_MISMATCH = 0.5     # moltiplicatore se è fuori luogo
DROP_BONUS = 1000         # bonus per lo special al picco

# Scurimento dello schermo mentre la modalità fidget è accesa (0 = niente, 1 = nero)
DIM_LEVELS = {"Spento": 0.0, "Leggero": 0.35, "Medio": 0.55, "Forte": 0.75}
DIM_TINT = (6, 4, 14)  # nero leggermente violaceo, più morbido del nero puro
DIM_FADE = 0.25        # secondi per scurire / schiarire

LASER_MODES = ["Spenti", "Con i flash", "Solo laser"]
RENDERERS = ["GPU", "CPU (compatibilità)"]
# Quiet: punteggio e analisi discreti in alto a destra.
# Loud: punteggio grande al centro che cresce, colonna dei giudizi sopra, analisi a sinistra.
MODES = ["Quiet", "Loud"]
FEED_LENGTH = 6          # giudizi visibili nella colonna della modalità Loud
FEED_LIFE = 3.5          # secondi prima che un giudizio sparisca del tutto
LABEL_SIZE = 44          # dimensione delle scritte dei giudizi in modalità Loud
EXTRA_LABELS = [("DROP!", (255, 90, 210)), ("SAVE IT!", (255, 150, 80)),
                ("CALIBRATING", (180, 176, 200))]

# Giudizi sul tempo: (errore massimo in ms, scritta, colore, punti, peso nella precisione)
JUDGEMENTS = [
    (30,  "PERFECT", (255, 228, 120), 300, 1.0),
    (60,  "GREAT",   (120, 230, 255), 150, 0.6),
    (100, "GOOD",    (190, 160, 255), 50,  0.2),
    (140, "ALMOST",  (255, 170, 90),  15,  0.05),
    (9e9, "MISS",    (255, 110, 110), 0,   0.0),
]
FREESTYLE_POINTS = 10  # punti per tap quando il ritmo della canzone non è agganciato
SUMMARY_TIME = 2.4     # secondi della schermata finale (2 di oscillazione + uscita)

# Commenti di fine sessione, dal peggiore al migliore
COMMENTS = [
    ["NICE TRY!", "THE DRUMMER QUIT", "KEEP PRACTICING!", "FREE JAZZ?", "BOLD CHOICES!"],
    ["NOT BAD!", "GROOVY!", "SOLID!", "WARMING UP!", "VIBING!"],
    ["WOW!", "ON FIRE!", "SICK!", "AMAZING!", "CROWD GOES WILD!"],
    ["LEGENDARY!", "UNSTOPPABLE!", "GODLIKE!", "FLAWLESS!", "HUMAN METRONOME!"],
]

PALETTES = {
    "Neon":   dict(L=(285, 330), R=(175, 210), T=(45, 60),   B=(0, 20),   sat=255, val=255),
    "Club":   dict(L=(0, 25),    R=(220, 250), T=(300, 320), B=(30, 45),  sat=240, val=255),
    "Aurora": dict(L=(140, 170), R=(185, 215), T=(260, 285), B=(95, 120), sat=200, val=255),
    "Ambra":  dict(L=(20, 40),   R=(35, 50),   T=(10, 25),   B=(45, 55),  sat=240, val=255),
}

# ---------------------------------------------------------------- mappa tasti
# Codici virtuali Windows, layout QWERTY italiano.
# Ogni tasto ha una posizione sulla tastiera. Da quella posizione nascono:
#   - la DIREZIONE della luce: la direzione del tasto rispetto al centro della tastiera
#     diventa la direzione rispetto al centro dello schermo, a 360 gradi
#   - l'INTENSITÀ: 1 al centro della tastiera (luce forte, secca, con laser),
#     0 sul bordo (luce morbida, lenta, tenue), con tutte le sfumature in mezzo
# Ogni voce: (bordo, posizione lungo il bordo 0..1, angolo in gradi, onda grande, intensità)
# L'intensità "special" è la barra spaziatrice.
def vk(ch):
    return ord(ch)

KEYBOARD_ROWS = [  # (spostamento della riga in larghezze di tasto, tasti da sinistra)
    (0.0,  [0xDC] + [vk(c) for c in "1234567890"] + [0xDB, 0xDD]),       # \ 1 … 0 ' ì
    (1.5,  [vk(c) for c in "QWERTYUIOP"] + [0xBA, 0xBB]),                 # q … p è +
    (1.75, [vk(c) for c in "ASDFGHJKL"] + [0xC0, 0xDE, 0xBF]),            # a … l ò à ù
    (1.25, [0xE2] + [vk(c) for c in "ZXCVBNM"] + [0xBC, 0xBE, 0xBD]),     # < z … m , . -
]


def edge_point(dx, dy):
    """Punto sul bordo dello schermo nella direzione (dx, dy) dal centro."""
    m = max(abs(dx), abs(dy)) or 1.0
    bx, by = dx / m, dy / m
    if abs(bx) >= abs(by):
        return ("L" if bx < 0 else "R"), min(0.96, max(0.04, (by + 1) / 2))
    return ("T" if by < 0 else "B"), min(0.96, max(0.04, (bx + 1) / 2))


def build_keymap():
    keys = [(code, start + i + 0.5, row)
            for row, (start, codes) in enumerate(KEYBOARD_ROWS)
            for i, code in enumerate(codes)]
    xs = [x for _, x, _ in keys]
    ys = [y for _, _, y in keys]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    hx, hy = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    polar = {}
    for code, x, y in keys:
        dx, dy = (x - cx) / hx, (y - cy) / hy   # tastiera "schiacciata" in un quadrato
        # per l'intensità le righe pesano la metà: la tastiera ne ha solo 4, e così
        # anche i tasti centrali della riga dei numeri e di quella in basso restano vivaci
        polar[code] = (dx, dy, math.hypot(dx, dy * 0.5))
    r_min = min(r for _, _, r in polar.values())
    r_max = max(r for _, _, r in polar.values())
    keymap = {}
    for code, (dx, dy, r) in polar.items():
        edge, pos = edge_point(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) % 360
        power = (r_max - r) / (r_max - r_min)   # 1 al centro, 0 sul bordo
        keymap[code] = (edge, pos, angle, False, round(power, 3))
    return keymap


KEYMAP = build_keymap()
KEYMAP.update({
    0x20: ("B", 0.5, 90, True, "special"),   # spazio: lo special
    0x0D: ("T", 0.5, 270, True, 1.0),        # invio: onda dall'alto
    0x25: ("L", 0.5, 180, False, 0.8),       # freccia sinistra
    0x26: ("T", 0.5, 270, False, 0.8),       # freccia su
    0x27: ("R", 0.5, 0, False, 0.8),         # freccia destra
    0x28: ("B", 0.5, 90, False, 0.8),        # freccia giù
})

VK_F, VK_D, VK_L, VK_M = 0x46, 0x44, 0x4C, 0x4D
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
user32 = ctypes.windll.user32 if sys.platform == "win32" else None


# ================================================================ analisi audio
class BeatAnalyzer:
    """Ascolta l'audio in uscita dal PC e stima BPM e posizione delle battute."""

    def __init__(self):
        self.enabled = True
        self.running = False
        self.error = None
        self.lock = threading.Lock()
        self.onsets = deque(maxlen=300)   # istanti dei colpi rilevati
        self.last_sound = 0.0
        self.grid = None                  # (periodo, fase, affidabilità)
        self.loud = deque(maxlen=4000)    # (istante, volume in dB smussato), ~40 secondi
        self.loud_now = -120.0
        self.loud_range = None            # (dB "tranquillo", dB "picco") della canzone

    def start(self):
        if self.running or not AUDIO_AVAILABLE:
            return
        try:
            self.pa = pyaudio.PyAudio()
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                for lb in self.pa.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        dev = lb
                        break
                else:
                    raise RuntimeError("uscita audio non trovata")
            self.sr = int(dev["defaultSampleRate"])
            self.ch = max(1, int(dev["maxInputChannels"]))
            self.hop = 512 if self.sr <= 48000 else 1024
            self.n = 2048 if self.sr <= 48000 else 4096
            self.ring = np.zeros(self.n, np.float32)
            self.win = np.hanning(self.n).astype(np.float32)
            freqs = np.fft.rfftfreq(self.n, 1 / self.sr)
            # due bande: la cassa (basse) e il resto (rullante, hi-hat, voce)
            self.low = (freqs >= 40) & (freqs < 160)
            self.mid = (freqs >= 160) & (freqs < 4000)
            self.prev = None
            self.flux_hist = deque(maxlen=120)
            self.last_onset = 0.0
            self.stream = self.pa.open(format=pyaudio.paFloat32, channels=self.ch,
                                       rate=self.sr, input=True,
                                       input_device_index=dev["index"],
                                       frames_per_buffer=self.hop,
                                       stream_callback=self._callback)
            self.running = True
            self.error = None
        except Exception as e:
            self.error = str(e)

    def stop(self):
        if self.running:
            try:
                self.stream.stop_stream()
                self.stream.close()
                self.pa.terminate()
            except Exception:
                pass
            self.running = False

    def _callback(self, in_data, frame_count, time_info, status):
        now = time.perf_counter()
        if not self.enabled:
            return (None, pyaudio.paContinue)
        try:
            x = np.frombuffer(in_data, dtype=np.float32)
            x = x.reshape(-1, self.ch).mean(axis=1)
            if len(x) >= self.n:
                self.ring = x[-self.n:].copy()
            else:
                self.ring = np.concatenate((self.ring[len(x):], x))
            rms = float(np.sqrt(np.mean(x * x)))
            if rms > 1e-4:
                self.last_sound = now
            # inviluppo del volume: sale in fretta (un drop si sente subito) e scende piano,
            # così segue le sezioni del brano e non il su e giù di ogni singolo colpo
            db = 20 * math.log10(rms + 1e-9)
            dt = frame_count / self.sr
            tau = 0.12 if db > self.loud_now else 1.5
            self.loud_now += (db - self.loud_now) * (1 - math.exp(-dt / tau))
            self.loud.append((now, self.loud_now))
            spec = np.log1p(20 * np.abs(np.fft.rfft(self.ring * self.win)))
            if self.prev is not None:
                rise = np.maximum(spec - self.prev, 0)
                kick = float(rise[self.low].mean())
                flux = kick + 0.5 * float(rise[self.mid].mean())
                hist = self.flux_hist
                if len(hist) > 30:
                    m = sum(hist) / len(hist)
                    sd = math.sqrt(sum((h - m) ** 2 for h in hist) / len(hist))
                    if flux > m + 1.6 * sd and flux > 0.02 and now - self.last_onset > 0.12:
                        t = now - 0.5 * frame_count / self.sr
                        self.last_onset = now
                        with self.lock:
                            self.onsets.append((t, kick))
                hist.append(flux)
            self.prev = spec
        except Exception:
            pass
        return (None, pyaudio.paContinue)

    def energy_at(self, t):
        """Energia della canzone all'istante t, da 0 (parte più calma) a 1 (picco)."""
        r = self.loud_range
        if not (self.running and self.enabled and r) or t - self.last_sound > 1.5:
            return None
        db = None
        for when, value in reversed(self.loud):
            db = value
            if when <= t:
                break
        if db is None:
            return None
        return min(1.0, max(0.0, (db - r[0]) / (r[1] - r[0])))

    def update_tempo(self):
        """Chiamata un paio di volte al secondo: ricalcola BPM, fase e dinamica."""
        if not self.running:
            self.grid = None
            self.loud_range = None
            return
        now = time.perf_counter()
        # dinamica: confronta il volume con quello degli ultimi 40 secondi di musica
        levels = np.array([v for w, v in self.loud if v > -70])
        if len(levels) > 300:
            lo, hi = np.percentile(levels, 10), np.percentile(levels, 98)
            self.loud_range = (float(lo), float(max(hi, lo + 6)))
        else:
            self.loud_range = None
        with self.lock:
            recent = [o for o in self.onsets if now - o[0] < 10]
        if len(recent) < 8 or now - self.last_sound > 2:
            self.grid = None
            return
        ons = np.array([o[0] for o in recent])
        strength = np.array([o[1] for o in recent])
        d = ons[None, :] - ons[:, None]
        d = d[(d > 0.25) & (d < 2.2)]
        bpms = np.arange(70, 181, 0.5)
        periods = 60 / bpms
        k = np.clip(np.round(d[None, :] / periods[:, None]), 1, 4)
        err = d[None, :] - k * periods[:, None]
        score = (np.exp(-(err / 0.018) ** 2) / k).sum(axis=1)
        score *= np.exp(-0.5 * (np.log2(bpms / 120) / 0.6) ** 2)  # preferisce 85-170 bpm
        P = float(periods[int(np.argmax(score))])
        if self.grid and abs(self.grid[0] - P) / P < 0.03:
            P = 0.8 * self.grid[0] + 0.2 * P  # stabilizza
        w = np.exp((ons - now) / 4)              # i colpi recenti contano di più
        # affidabilità: quanto i colpi cadono sulla griglia (battere, levare o sedicesimi)
        conf = max(float(abs((np.exp(2j * np.pi * h * ons / P) * w).sum()) / w.sum())
                   for h in (2, 4))
        # fase: solo i colpi più forti (di solito la cassa) indicano dove sta il battere
        strong = strength >= np.percentile(strength, 60)
        z = (np.exp(2j * np.pi * ons[strong] / P) * w[strong]).sum()
        phase = float(np.angle(z) / (2 * np.pi) * P)
        self.grid = (P, phase, conf) if conf > 0.3 else None


# ================================================================ tastiera
class KeyBridge(QObject):
    """Segnali emessi dal thread dei tasti rapidi."""
    toggle = Signal()
    cycle_dim = Signal()
    cycle_laser = Signal()
    cycle_mode = Signal()


class HotkeyActions(QObject):
    """Riceve le scorciatoie nel thread principale, dove si può toccare la grafica."""

    def __init__(self, toggle, cycle_dim, cycle_laser, cycle_mode):
        super().__init__()
        self._toggle, self._dim, self._laser = toggle, cycle_dim, cycle_laser
        self._mode = cycle_mode

    @Slot()
    def toggle(self):
        self._toggle()

    @Slot()
    def cycle_dim(self):
        self._dim()

    @Slot()
    def cycle_laser(self):
        self._laser()

    @Slot()
    def cycle_mode(self):
        self._mode()


class HotkeyThread(threading.Thread):
    """Registra Ctrl+Alt+F/D/L con RegisterHotKey e avvisa Qt quando li premi.

    Windows manda all'app solo queste combinazioni: il resto della tastiera
    non passa mai di qui.
    """

    def __init__(self, keys):
        super().__init__(daemon=True)
        self.keys = list(keys.items())   # [(codice tasto, segnale)]
        self.thread_id = None
        self.failed = []
        self.ready = threading.Event()

    def run(self):
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for hk_id, (code, _) in enumerate(self.keys, start=1):
            if not user32.RegisterHotKey(None, hk_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, code):
                self.failed.append(code)
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and 1 <= msg.wParam <= len(self.keys):
                self.keys[msg.wParam - 1][1].emit()
        for hk_id in range(1, len(self.keys) + 1):
            user32.UnregisterHotKey(None, hk_id)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


class KeyCatcher(QWidget):
    """Finestrella invisibile che durante la sessione riceve i tasti, come una normale app."""

    def __init__(self, on_key, on_escape, on_lost):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # quasi invisibile ma "vera": Windows dà il focus solo a finestre che esistono a schermo
        self.setWindowOpacity(0.01)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(1, 1)
        self.on_key, self.on_escape, self.on_lost = on_key, on_escape, on_lost
        self.in_session = False
        self.got_focus = False
        self.previous = None

    def grab(self, screen):
        """Prende il focus della tastiera, ricordando quale finestra lo aveva."""
        self.previous = user32.GetForegroundWindow()
        g = screen.geometry()
        self.move(g.right() - 1, g.bottom() - 1)
        self.in_session = True
        self.got_focus = False
        self.show()
        self.raise_()
        user32.SetForegroundWindow(int(self.winId()))
        self.activateWindow()
        self.setFocus()

    def release(self, restore=True):
        """Lascia il focus e lo restituisce alla finestra su cui stavi lavorando."""
        self.in_session = False
        self.hide()
        if restore and self.previous and user32.IsWindow(self.previous):
            user32.SetForegroundWindow(self.previous)
        self.previous = None

    def keyPressEvent(self, e):
        t = time.perf_counter()  # istante del tasto, per il giudizio sul tempo
        if e.isAutoRepeat():
            return
        if e.key() == Qt.Key_Escape:
            self.on_escape()
            return
        if e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return
        m = KEYMAP.get(e.nativeVirtualKey())
        if m:
            self.on_key(*m, t)

    def changeEvent(self, e):
        if e.type() == QEvent.ActivationChange and self.in_session:
            if self.isActiveWindow():
                self.got_focus = True
            elif self.got_focus:
                # hai cliccato altrove o fatto Alt+Tab: la sessione finisce qui
                QTimer.singleShot(0, self.on_lost)
        super().changeEvent(e)


# ================================================================ overlay
# Tutto ciò che si disegna è un'immagine preparata UNA volta (sprite). Sulla GPU diventa
# una texture caricata una volta sola: a ogni fotogramma la scheda video la scala, la ruota
# e la sfuma da sé, senza che la CPU rigeneri o ricarichi niente.
_SPRITES = {}
HUE_STEP = 6                       # sfumature di colore disponibili: 360 / 6 = 60
GLOW_SATS = (96, 176, 240)         # saturazioni disponibili per gli aloni


def q_hue(hue):
    return int(round(hue / HUE_STEP)) * HUE_STEP % 360


def q_sat(sat, levels=GLOW_SATS):
    return min(levels, key=lambda v: abs(v - sat))


def glow_sprite(hue, sat):
    """Alone radiale pre-renderizzato."""
    key = ("glow", q_hue(hue), q_sat(sat))
    img = _SPRITES.get(key)
    if img is None:
        n = SPRITE_SIZE
        img = QImage(n, n, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        g = QRadialGradient(QPointF(n / 2, n / 2), n / 2)
        for stop, alpha in ((0.0, 1.0), (0.35, 0.5), (1.0, 0.0)):
            c = QColor.fromHsv(key[1], key[2], 255)
            c.setAlphaF(alpha)
            g.setColorAt(stop, c)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(0, 0, n, n))
        p.end()
        _SPRITES[key] = img
    return img


def beam_sprite(hue, sat):
    """Sezione di un fascio laser (alone, bagliore, nucleo bianco), pre-renderizzata:
    per disegnare un laser basta allungarla e ruotarla, in un solo passaggio."""
    key = ("beam", q_hue(hue), 224)
    img = _SPRITES.get(key)
    if img is None:
        n = 64                       # altezza = larghezza totale del fascio
        img = QImage(4, n, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setCompositionMode(QPainter.CompositionMode_Plus)
        # (frazione della larghezza, opacità, quanto è sbiancato)
        for frac, alpha, white in ((1.0, 0.10, 0.0), (0.36, 0.30, 0.2), (0.11, 0.95, 0.7)):
            half = n * frac / 2
            g = QLinearGradient(0, n / 2 - half, 0, n / 2 + half)
            c = QColor.fromHsv(key[1], int(key[2] * (1 - white)), 255)
            c.setAlphaF(alpha)
            clear = QColor(c)
            clear.setAlphaF(0)
            g.setColorAt(0, clear)
            g.setColorAt(0.25, c)
            g.setColorAt(0.75, c)
            g.setColorAt(1, clear)
            p.fillRect(QRectF(0, n / 2 - half, 4, 2 * half), g)
        p.end()
        _SPRITES[key] = img
    return img


def edge_sprite(hue, vertical):
    """Linea luminosa sul bordo: chiara al centro, sfumata alle estremità."""
    key = ("edge", q_hue(hue), vertical)
    img = _SPRITES.get(key)
    if img is None:
        n = 64
        img = QImage(4 if vertical else n, n if vertical else 4,
                     QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        g = QLinearGradient(0, 0, 0, n) if vertical else QLinearGradient(0, 0, n, 0)
        hot = QColor.fromHsv(key[1], 80, 255)
        clear = QColor(hot)
        clear.setAlphaF(0)
        g.setColorAt(0, clear)
        g.setColorAt(0.5, hot)
        g.setColorAt(1, clear)
        p.fillRect(img.rect(), g)
        p.end()
        _SPRITES[key] = img
    return img


def ring_sprite(hue, white, thickness):
    """Anello morbido per l'onda d'urto dello special."""
    key = ("ring", q_hue(hue), white, thickness)
    img = _SPRITES.get(key)
    if img is None:
        n = 256
        img = QImage(n, n, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        g = QRadialGradient(QPointF(n / 2, n / 2), n / 2)
        c = QColor.fromHsv(key[1], int(255 * (1 - white)), 255)
        clear = QColor(c)
        clear.setAlphaF(0)
        g.setColorAt(0, clear)
        g.setColorAt(max(0.0, 1 - 2 * thickness), clear)
        g.setColorAt(1 - thickness, c)
        g.setColorAt(1, clear)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(0, 0, n, n))
        p.end()
        _SPRITES[key] = img
    return img


def text_sprite(text, size, weight, spacing=0, color=(255, 255, 255)):
    """Testo trasformato in immagine, a risoluzione doppia: si può scalare e ruotare
    senza che i caratteri vengano ri-rasterizzati a ogni fotogramma."""
    key = ("text", text, size, weight, spacing, color)
    img = _SPRITES.get(key)
    if img is None:
        font = QFont("Segoe UI")
        font.setPixelSize(size * 2)
        font.setWeight(weight)
        if spacing:
            font.setLetterSpacing(QFont.AbsoluteSpacing, spacing * 2)
        fm = QFontMetrics(font)
        w, h = fm.horizontalAdvance(text) + 8, fm.height() + 8
        img = QImage(max(w, 1), max(h, 1), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(font)
        p.setPen(QColor(*color))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, text)
        p.end()
        _SPRITES[key] = img
    return img


SCORE_SIZE, SCORE_WEIGHT = 118, QFont.Black
COMMENT_SIZE, COMMENT_WEIGHT = 54, QFont.Bold


def all_sprites():
    """Tutti gli sprite usati durante una sessione, per caricarli sulla GPU all'avvio."""
    for h in range(0, 360, HUE_STEP):
        for sat in GLOW_SATS:
            yield glow_sprite(h, sat)
        yield beam_sprite(h, 224)
        yield edge_sprite(h, True)
        yield edge_sprite(h, False)
    for pal in PALETTES.values():   # lo special usa solo il colore inferiore della palette
        yield ring_sprite(pal["B"][0], 0.0, 0.10)
        yield ring_sprite(pal["B"][0], 0.6, 0.03)
    yield shade_sprite()
    for ch in "0123456789,":
        yield text_sprite(ch, SCORE_SIZE, SCORE_WEIGHT)
    for _, label, color, _, _ in JUDGEMENTS:
        yield text_sprite(label, LABEL_SIZE, QFont.Bold, 5, color)
    for label, color in EXTRA_LABELS:
        yield text_sprite(label, LABEL_SIZE, QFont.Bold, 5, color)


def ray_exit(x0, y0, dx, dy, W, H):
    """Distanza a cui un raggio che parte da (x0, y0) esce dallo schermo."""
    t = 1e9
    if dx > 1e-9:
        t = min(t, (W - x0) / dx)
    elif dx < -1e-9:
        t = min(t, -x0 / dx)
    if dy > 1e-9:
        t = min(t, (H - y0) / dy)
    elif dy < -1e-9:
        t = min(t, -y0 / dy)
    return max(0.0, t)


def shade_sprite():
    """Alone scuro pre-renderizzato, per far leggere il testo della schermata finale."""
    img = _SPRITES.get("shade")
    if img is None:
        n = SPRITE_SIZE
        img = QImage(n, n, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        g = QRadialGradient(QPointF(n / 2, n / 2), n / 2)
        g.setColorAt(0, QColor(0, 0, 0, 150))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(0, 0, n, n))
        p.end()
        _SPRITES["shade"] = img
    return img



class OverlayBase:
    """Logica ed effetti dell'overlay. Le due classi sotto decidono solo DOVE si disegna:
    GpuOverlay su una superficie OpenGL (scheda video), CpuOverlay su una finestra normale."""

    def full_rect(self):
        return QRect(0, 0, self.width(), self.height())

    def _setup(self, analyzer):
        self.analyzer = analyzer
        self.flashes = []
        self.lasers = []
        self.specials = []
        self.palette_name = "Neon"
        self.laser_mode = "Con i flash"
        self.dim_name = "Medio"
        self.dim_now = 0.0
        self.armed = False
        self.taps = []
        self.bpm = None
        self.best = 0
        self.on_best = lambda score: None
        self.summary = None
        self.reset_calibration()
        self.reset_session()
        self.last_paint = time.perf_counter()
        self.prev_region = QRegion()
        self.paint_ms = 0.0
        self.font_hud = QFont("Consolas", 9)
        self.font_detail = QFont("Consolas", 10)
        self.font_stats = QFont("Consolas", 12)
        self.font_best = QFont("Segoe UI", 14, QFont.Bold)
        self.font_best.setLetterSpacing(QFont.AbsoluteSpacing, 5)
        self.font_judge = QFont("Segoe UI", 24, QFont.Bold)
        self.font_judge.setLetterSpacing(QFont.AbsoluteSpacing, 4)
        self.font_judge_big = QFont("Segoe UI", 38, QFont.Black)
        self.font_judge_big.setLetterSpacing(QFont.AbsoluteSpacing, 6)
        self.prewarm = False   # la versione GPU carica tutti gli sprite al primo fotogramma
        self.mode = "Quiet"
        self.frame_dt = 0.016
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ fotogrammi
    def _wake(self):
        """Riparte con l'animazione (il timer si ferma da solo quando non serve)."""
        if not self.timer.isActive():
            self.last_paint = time.perf_counter()
            self.timer.start(FRAME_MS)

    def _dim_target(self):
        return DIM_LEVELS[self.dim_name] if (self.armed or self.summary) else 0.0

    def _tick(self):
        """Ridisegna solo i rettangoli dove c'è qualcosa che cambia."""
        now = time.perf_counter()
        W, H = self.width(), self.height()
        full = self.full_rect()
        dim_moving = abs(self.dim_now - self._dim_target()) > 1e-3
        if dim_moving or self.summary or self.specials:
            region = QRegion(full)   # effetti che coprono tutto lo schermo
        else:
            region = QRegion()
            for f in self.flashes:
                region = region.united(f["rect"])
            for l in self.lasers:
                region = region.united(l["rect"])
            if self.armed and self.mode == "Loud":
                region = region.united(QRect(0, 0, 340, 150))         # analisi a sinistra
                region = region.united(QRect(W // 2 - 600, H // 2 - 560, 1200, 960))
            elif self.armed:
                region = region.united(QRect(W - 340, 0, 340, 150))  # HUD in alto a destra
                if self.judgement and now - self.judgement["t0"] < 1.0:
                    region = region.united(QRect(0, 20, W, 120))  # giudizio in alto
        dirty = region.united(self.prev_region)   # anche dove c'era qualcosa da cancellare
        self.prev_region = region
        if not dirty.isEmpty():
            self.update(dirty)
        elif not self.armed:
            self.timer.stop()  # niente da mostrare: zero lavoro finché non serve

    def reset_calibration(self):
        self.latency = deque(maxlen=24)   # scarti grezzi, per stimare la latenza cuffie
        self.judgement = None             # dict con scritta, colore, dettagli e istante

    def reset_session(self):
        self.results = deque(maxlen=32)   # precisione degli ultimi tap, per l'HUD
        self.combo = 0
        self.max_combo = 0
        self.score = 0
        self.judged = 0                   # tap giudicati sul ritmo
        self.precision = 0.0              # somma dei pesi dei giudizi
        self.tap_count = 0
        self.energy_judged = 0            # tap giudicati sull'energia
        self.energy_score = 0.0           # 1 = luce adatta, 0.5 = neutra, 0 = sbagliata
        self.drops = 0
        self.last_special = 0.0
        self.feed = []                    # colonna dei giudizi (modalità Loud)
        self.shown_score = 0.0            # punteggio mostrato, che "conta" verso quello vero
        self.score_bump = 0.0             # istante dell'ultimo punto preso, per il colpo di zoom
        self.last_power = None            # (intensità dell'ultimo tasto, istante)

    def latency_estimate(self, P):
        """Latenza cuffie stimata dai tap (media circolare: non salta tra battere e levare)."""
        if len(self.latency) < 6:
            return None
        h = P / 2
        z = sum(cmath.exp(2j * math.pi * x / h) for x in self.latency)
        return cmath.phase(z) / (2 * math.pi) * h

    @property
    def multiplier(self):
        return 1 + min(self.combo // 10, 3)  # x1, poi x2 a combo 10, fino a x4

    # ------------------------------------------------------------ eventi
    def fire(self, edge, pos, angle, big, power, t_key):
        if not self.armed:
            return
        now = time.perf_counter()
        tier = "special" if power == "special" else "light"
        if tier == "special":
            if now - self.last_special < SPECIAL_COOLDOWN:
                return
            self.last_special = now

        timing = self._judge_timing(t_key)
        note, factor = self._judge_energy(power, t_key)
        mult = self.multiplier

        # punti: precisione sul tempo x adattezza della luce al momento x combo
        judged = timing is not None and timing["label"] != "CALIBRATING"
        base = timing["points"] if judged else FREESTYLE_POINTS
        bonus = DROP_BONUS if (tier == "special" and factor >= ENERGY_MATCH) else 0
        points = round((base * factor + bonus) * mult)
        self.score += points

        if judged:
            self.combo = self.combo + 1 if timing["label"] in ("PERFECT", "GREAT") else 0
        if tier == "special" and note == "SAVE IT!":
            self.combo = 0  # special sprecato in un momento calmo
        self.max_combo = max(self.max_combo, self.combo)
        self.tap_count += 1

        # cosa scrivere in alto al centro
        if tier == "special" and note in ("DROP!", "SAVE IT!"):
            color = (255, 90, 210) if note == "DROP!" else (255, 150, 80)
            self.judgement = dict(label=note, color=color, big=True, t0=now,
                                  detail=self._detail(timing, None, points))
        elif timing is not None:
            self.judgement = dict(label=timing["label"], color=timing["color"], big=False,
                                  t0=now, detail=self._detail(timing, note, points))

        if points:
            self.score_bump = now
        if self.judgement and self.judgement["t0"] == now:   # nuovo giudizio in colonna
            self.feed.insert(0, dict(label=self.judgement["label"],
                                     color=self.judgement["color"], t0=now, y=None,
                                     points=points, note=note or ""))
            del self.feed[FEED_LENGTH:]

        boost = judged and timing["label"] == "PERFECT"
        if tier == "special":
            self._add_special(boost)
        else:
            self.last_power = (power, now)
            hue = self._angle_hue(angle)
            lasers = self.laser_mode != "Spenti" and power >= LASER_MIN_POWER
            if not (lasers and self.laser_mode == "Solo laser"):
                self._add(edge, pos, 0.5, big, boost, power=power, hue=hue)
            if lasers:
                self._add_laser(edge, pos, 0.5, big, boost, power=power, hue=hue)
        self._tap()

    @staticmethod
    def _detail(timing, note, points):
        parts = []
        if timing is None or timing["label"] == "CALIBRATING":
            if timing is not None:
                parts.append("tap along to the beat")
        else:
            parts.append(f"{timing['ms']:+.0f} ms")
            if timing["offbeat"]:
                parts.append("off-beat")
        if note:
            parts.append(note)
        if points:
            parts.append(f"+{points:,}")
        return "  ·  ".join(parts)

    def _judge_timing(self, t):
        """Confronta il tap con il ritmo della canzone. Ritorna un dict o None."""
        a = self.analyzer
        if not (a.running and a.enabled):
            return None
        g = a.grid
        if g is None:
            return None
        P, ph, _ = g
        half = P / 2
        raw = ((t - ph + half) % P) - half      # posizione del tap rispetto al battere
        self.latency.append(raw)
        lat = self.latency_estimate(P)
        if lat is None:  # primi tap: misura la latenza delle cuffie, senza giudicare
            return dict(label="CALIBRATING", color=(180, 176, 200))
        x = ((raw - lat + half) % P) - half     # rispetto al battere, latenza tolta
        offbeat = abs(x) > P / 4
        err = x - math.copysign(half, x) if offbeat else x
        ms = err * 1000
        for limit, label, color, points, weight in JUDGEMENTS:
            if abs(ms) <= limit:
                break
        self.judged += 1
        self.precision += weight
        self.results.append(weight)
        return dict(label=label, color=color, ms=ms, offbeat=offbeat, points=points)

    def _judge_energy(self, power, t):
        """Quanto l'intensità della luce è adatta al momento: (nota, moltiplicatore)."""
        a = self.analyzer
        lat = (self.latency_estimate(a.grid[0]) if a.grid else None) or 0.0
        level = a.energy_at(t - lat)  # la musica che senti ora è uscita dal PC "lat" fa
        if level is None:
            return None, 1.0
        if power == "special":
            # l'audio arriva al PC prima che alle tue orecchie: guarda anche quello più recente
            latest = a.energy_at(time.perf_counter())
            if latest is not None:
                level = max(level, latest)
            before = a.energy_at(t - lat - 1.0)
            rising = before is not None and level - before >= ENERGY_RISE
            if level >= ENERGY_PEAK or (level >= 0.65 and rising):
                note, factor, score = "DROP!", ENERGY_MATCH, 1.0
                self.drops += 1
            else:
                note, factor, score = "SAVE IT!", 0.0, 0.0
        else:
            diff = power - level
            if abs(diff) <= ENERGY_TOLERANCE:
                note, factor = "vibe ✓", ENERGY_MATCH
            elif diff < -ENERGY_OFF:
                note, factor = "go bigger!", ENERGY_MISMATCH
            elif diff > ENERGY_OFF:
                note, factor = "too much!", ENERGY_MISMATCH
            else:
                note, factor = "", 1.0
            score = max(0.0, 1 - abs(diff) / ENERGY_OFF)
        self.energy_judged += 1
        self.energy_score += score
        return note, factor

    def _angle_hue(self, angle):
        """Colore che sfuma girando attorno allo schermo, tra i colori dei quattro bordi."""
        pal = PALETTES[self.palette_name]
        order = ["R", "B", "L", "T"]   # 0°, 90°, 180°, 270° (y verso il basso)
        a = angle % 360
        i = int(a // 90)
        f = (a % 90) / 90
        h1 = sum(pal[order[i]]) / 2
        h2 = sum(pal[order[(i + 1) % 4]]) / 2
        d = ((h2 - h1 + 540) % 360) - 180   # via più breve sul cerchio dei colori
        return (h1 + d * f + random.uniform(-4, 4)) % 360

    def _palette_hue(self, edge, hue_t):
        p = PALETTES[self.palette_name]
        h0, h1 = p[edge]
        return (h0 + (h1 - h0) * hue_t + random.uniform(-4, 4)) % 360, p

    def _add(self, edge, pos, hue_t, big, boost=False, power=1.0, hue=None):
        h, p = self._palette_hue(edge, hue_t)
        life = SOFT_LIFE + (LIFE - SOFT_LIFE) * power   # più morbida = più lunga
        f = dict(edge=edge, pos=pos, hue=h if hue is None else hue, sat=p["sat"],
                 val=p["val"], t0=time.perf_counter(), big=big, boost=boost, power=power,
                 life=life)
        W, H = self.width(), self.height()
        span, depth, _, _ = self._flash_shape(f, W, H)
        x, y = self._anchor(edge, pos, W, H)
        # ingombro massimo durante tutta la vita della luce: solo lì si ridisegna
        along, across = 0.55 * span, 0.64 * depth
        rx, ry = (across, along) if edge in "LR" else (along, across)
        f["rect"] = QRectF(x - rx, y - ry, 2 * rx, 2 * ry).toAlignedRect() \
            .adjusted(-2, -2, 2, 2).intersected(self.full_rect())
        self.flashes.append(f)
        del self.flashes[:-MAX_FLASHES]
        self._wake()

    @staticmethod
    def _flash_shape(f, W, H):
        """Dimensioni di base, luminosità e saturazione di una luce."""
        pw = f["power"]
        soft = 1 - pw
        vertical = f["edge"] in "LR"
        S = min(W, H)
        if f["big"]:
            span = (H if vertical else W) * 0.95
            depth = S * 0.55
        else:
            # forti: strette e intense. Morbide: ampie e diffuse.
            span = (H * (0.5 + 0.25 * soft)) if vertical else (W * (0.26 + 0.14 * soft))
            depth = S * (0.36 + 0.14 * soft)
        peak = 0.42 + 0.53 * pw
        if f["boost"]:  # tap perfetto: luce più grande e più luminosa
            span *= 1.2
            depth *= 1.25
            peak = min(1.0, peak + 0.1)
        sat = int(f["sat"] * (0.65 + 0.35 * pw))
        return span, depth, peak, sat

    def _add_special(self, boost):
        """Barra spaziatrice: lampo, onda d'urto dal centro, luce e laser da ogni bordo."""
        hue = PALETTES[self.palette_name]["B"][0]
        self.specials.append(dict(t0=time.perf_counter(), hue=hue, boost=boost))
        del self.specials[:-3]
        self._wake()
        for edge in "LRTB":
            if self.laser_mode != "Solo laser":
                self._add(edge, 0.5, 0.5, True, True)
            if self.laser_mode != "Spenti":
                self._add_laser(edge, 0.5, 0.5, True, True)

    def _add_laser(self, edge, pos, hue_t, big, boost=False, power=1.0, hue=None):
        h, p = self._palette_hue(edge, hue_t)
        hue = h if hue is None else hue
        if big:
            # ventaglio di fasci che si aprono a forbice
            beams = [(-55 + i * 110 / 6, 18 if i % 2 else -18) for i in range(7)]
        else:
            beams = [(None, random.choice((-1, 1)) * random.uniform(7, 13))]
        l = dict(edge=edge, pos=0.5 if big else pos, hue=hue, sat=p["sat"],
                 t0=time.perf_counter(), boost=boost, power=power)
        W, H = self.width(), self.height()
        x0, y0 = self._anchor(edge, l["pos"], W, H)
        # direzione verso il lato opposto, in posizione speculare: i fasci si incrociano
        tx, ty = {"L": (W, (1 - pos) * H), "R": (0, (1 - pos) * H),
                  "T": ((1 - pos) * W, H), "B": ((1 - pos) * W, 0)}[edge]
        base = math.degrees(math.atan2(ty - y0, tx - x0))
        normal = {"L": 0, "R": 180, "T": 90, "B": -90}[edge]
        l["origin"] = (x0, y0)
        l["beams"] = [((normal + spread) if spread is not None else base, sweep)
                      for spread, sweep in beams]
        # area occupata durante tutta la vita del laser (solo lì si ridisegna)
        xs, ys = [x0], [y0]
        for start, sweep in l["beams"]:
            for kk in (0.0, 0.5, 1.0):
                rad = math.radians(start + sweep * kk)
                dx, dy = math.cos(rad), math.sin(rad)
                t = ray_exit(x0, y0, dx, dy, W, H)
                xs.append(x0 + dx * t)
                ys.append(y0 + dy * t)
        pad = 50
        l["rect"] = QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad,
                           max(ys) - min(ys) + 2 * pad).toAlignedRect().intersected(self.full_rect())
        self.lasers.append(l)
        del self.lasers[:-MAX_LASERS]
        self._wake()

    def _tap(self):
        now = time.perf_counter()
        if self.taps and now - self.taps[-1] < 0.09:
            return  # accordi contano una volta
        self.taps.append(now)
        self.taps = [t for t in self.taps[-12:] if now - t < 6]
        if len(self.taps) >= 4:
            iv = sorted(b - a for a, b in zip(self.taps, self.taps[1:]))
            bpm = 60 / iv[len(iv) // 2]
            while bpm > 200:
                bpm /= 2
            while bpm < 60:
                bpm *= 2
            self.bpm = round(bpm)

    def set_armed(self, on):
        self.armed = on
        self._wake()
        self.taps.clear()
        self.bpm = None
        self.judgement = None
        if on:  # piccolo saluto per far capire che è acceso
            self.summary = None
            self.reset_session()
            self._add("L", 0.5, 0.5, False)
            self._add("R", 0.5, 0.5, False)
        elif self.tap_count > 0:
            self._start_summary()
        else:
            self.flashes.clear()
            self.lasers.clear()

    def _start_summary(self):
        """Fine sessione: commento, punteggio e un'ultima esplosione di luce."""
        if self.judged >= 8:
            acc = self.precision / self.judged
            stats = f"max combo {self.max_combo}  ·  {round(acc * 100)}% timing"
            if self.energy_judged >= 8:
                vibe = self.energy_score / self.energy_judged
                acc = 0.75 * acc + 0.25 * vibe  # il tempo conta di più, l'energia aiuta
                stats += f"  ·  {round(vibe * 100)}% vibe"
            if self.drops:
                stats += f"  ·  {self.drops} drop" + ("s" if self.drops > 1 else "")
            stats += f"  ·  {self.tap_count} taps"
            tier = 3 if acc >= 0.9 else 2 if acc >= 0.76 else 1 if acc >= 0.63 else 0
        else:  # niente analisi: conta l'impegno
            n = self.tap_count
            tier = 3 if n >= 150 else 2 if n >= 60 else 1 if n >= 20 else 0
            stats = f"{n} taps"
        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            self.on_best(self.score)
        accent = QColor.fromHsv(int(PALETTES[self.palette_name]["L"][0]), 150, 255)
        self.summary = dict(t0=time.perf_counter(), comment=random.choice(COMMENTS[tier]),
                            score=self.score, stats=stats, new_best=new_best,
                            accent=(accent.red(), accent.green(), accent.blue()))
        for edge in "LRTB":
            if self.laser_mode != "Solo laser":
                self._add(edge, 0.5, 0.5, True, True)
            if self.laser_mode != "Spenti" and edge in "TB":
                self._add_laser(edge, 0.5, 0.5, True, True)

    # ------------------------------------------------------------ disegno
    @staticmethod
    def _anchor(edge, pos, W, H):
        if edge == "L":
            return 0.0, pos * H
        if edge == "R":
            return float(W), pos * H
        if edge == "T":
            return pos * W, 0.0
        return pos * W, float(H)

    def _render(self, p, area):
        started = time.perf_counter()
        W, H = self.width(), self.height()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.prewarm:
            # primo fotogramma sulla GPU: carica ora tutte le texture, una volta per tutte,
            # disegnandole in un pixel quasi trasparente (poi viene ripulito qui sotto)
            p.setOpacity(1 / 255)
            for img in all_sprites():
                p.drawImage(QRectF(0, 0, 1, 1), img)
            p.setOpacity(1.0)
            self.prewarm = False
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(area, Qt.transparent)

        now = time.perf_counter()
        dt = min(now - self.last_paint, 0.1)
        self.last_paint = now
        self.frame_dt = dt

        # velo scuro: entra e esce con una dissolvenza quando accendi/spegni
        target = self._dim_target()
        step = dt / DIM_FADE
        if self.dim_now < target:
            self.dim_now = min(target, self.dim_now + step)
        else:
            self.dim_now = max(target, self.dim_now - step)
        if self.dim_now > 0.003:
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            veil = QColor(*DIM_TINT)
            veil.setAlphaF(self.dim_now)
            p.fillRect(area, veil)

        p.setCompositionMode(QPainter.CompositionMode_Plus)
        self._draw_specials(p, W, H, now)
        self._draw_flashes(p, W, H, now, area)
        self._draw_lasers(p, W, H, now, area)
        p.setOpacity(1.0)

        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self.armed:
            if self.mode == "Loud":
                self._draw_loud(p, W, H, now)
            self._draw_hud(p, W, H, now)
        if self.summary:
            self._draw_summary(p, W, H, now)
        p.end()
        # tempo di disegno medio, mostrato nell'HUD per capire quanto pesa
        ms = (time.perf_counter() - started) * 1000
        self.paint_ms += (ms - self.paint_ms) * 0.1

    def _draw_summary(self, p, W, H, now):
        s = self.summary
        t = now - s["t0"]
        if t > SUMMARY_TIME:
            self.summary = None
            return
        fade_start = SUMMARY_TIME - 0.4
        alpha = 1.0 if t < fade_start else (SUMMARY_TIME - t) / 0.4
        # entrata con un piccolo rimbalzo, poi oscillazione, poi zoom in uscita
        k = min(t / 0.3, 1)
        pop = 1 + 2.2 * (k - 1) ** 3 + 1.2 * (k - 1) ** 2   # ease-out con rimbalzo
        wobble = 1 + 0.07 * math.sin(2 * math.pi * 1.6 * t)
        exit_zoom = 1 + 0.35 * (1 - alpha)
        scale = max(pop, 0.01) * wobble * exit_zoom
        tilt = 3 * math.sin(2 * math.pi * 0.8 * t)

        # alone scuro dietro al testo per farlo leggere su qualsiasi sfondo
        r = min(W, H) * 0.45
        p.setOpacity(alpha)
        p.drawImage(QRectF(W / 2 - r, H / 2 - r, 2 * r, 2 * r), shade_sprite())
        p.setOpacity(1.0)

        shown = int(s["score"] * min(t / 0.8, 1) ** 0.5)   # il punteggio "conta" da zero

        # commento e cifre sono immagini già pronte: la GPU le scala e le ruota da sé
        p.save()
        p.translate(W / 2, H / 2)
        p.rotate(tilt)
        p.scale(scale, scale)
        p.setOpacity(alpha)
        img = text_sprite(s["comment"], COMMENT_SIZE, COMMENT_WEIGHT, 6, s["accent"])
        w, h = img.width() / 2, img.height() / 2
        p.drawImage(QRectF(-w / 2, -85 - h / 2, w, h), img)
        digits = [text_sprite(ch, SCORE_SIZE, SCORE_WEIGHT) for ch in f"{shown:,}"]
        total = sum(d.width() for d in digits) / 2
        x = -total / 2
        for d in digits:
            w, h = d.width() / 2, d.height() / 2
            p.drawImage(QRectF(x, 10 - h / 2, w, h), d)
            x += w
        p.restore()
        p.setOpacity(1.0)

        p.setFont(self.font_stats)
        dim = QColor(255, 255, 255)
        dim.setAlphaF(0.7 * alpha)
        p.setPen(dim)
        p.drawText(QRectF(0, H / 2 + 95, W, 24), Qt.AlignCenter, s["stats"])
        if s["new_best"] and s["score"] > 0:
            gold = QColor(255, 210, 90)
            gold.setAlphaF(alpha * (0.6 + 0.4 * math.sin(now * 10)))
            p.setFont(self.font_best)
            p.setPen(gold)
            p.drawText(QRectF(0, H / 2 + 125, W, 28), Qt.AlignCenter, "NEW BEST!")

    def _draw_specials(self, p, W, H, now):
        cx, cy = W / 2, H / 2
        diag = math.hypot(W, H)
        self.specials = [s for s in self.specials if now - s["t0"] < SPECIAL_LIFE]
        for s in self.specials:
            t = now - s["t0"]
            k = t / SPECIAL_LIFE
            hue = int(s["hue"])
            # lampo su tutto lo schermo, brevissimo
            if t < 0.3:
                wash = QColor.fromHsv(hue, 90, 255)
                wash.setAlphaF(0.3 * (1 - t / 0.3) ** 2)
                p.fillRect(QRectF(0, 0, W, H), wash)
            # due onde d'urto ad anello che partono dal centro
            for delay, white, thickness in ((0.0, 0.0, 0.10), (0.09, 0.6, 0.03)):
                kk = (t - delay) / (SPECIAL_LIFE - delay)
                if not 0 <= kk < 1:
                    continue
                r = diag * 0.62 * (1 - (1 - kk) ** 3)
                p.setOpacity(0.75 * (1 - kk) ** 1.4)
                p.drawImage(QRectF(cx - r, cy - r, 2 * r, 2 * r),
                            ring_sprite(hue, white, thickness))
            p.setOpacity(1.0)
            # bagliore al centro
            r = diag * 0.25
            p.setOpacity(0.35 * (1 - k) ** 2)
            p.drawImage(QRectF(cx - r, cy - r, 2 * r, 2 * r), glow_sprite(hue, 120))
            p.setOpacity(1.0)

    def _draw_flashes(self, p, W, H, now, area):
        self.flashes = [f for f in self.flashes if now - f["t0"] < f["life"]]
        for f in self.flashes:
            if not f["rect"].intersects(area):
                continue
            age = now - f["t0"]
            k = age / f["life"]
            pw = f["power"]          # 0 = morbida e lenta, 1 = forte e secca
            soft = 1 - pw
            edge = f["edge"]
            vertical = edge in "LR"
            x, y = self._anchor(edge, f["pos"], W, H)
            # le luci morbide salgono piano invece di accendersi di colpo
            attack = SOFT_ATTACK * soft
            if attack > 0.01 and age < attack:
                a = (age / attack) ** 0.7
            else:
                rest = (age - attack) / (f["life"] - attack)
                a = (1 - rest) ** (2.2 - 0.6 * soft)
            grow = 1 - (1 - k) ** (3 - soft)
            span, depth, peak, sat = self._flash_shape(f, W, H)
            depth *= 0.55 + grow * 0.6

            # alone: uno sprite già pronto, schiacciato contro il bordo e scalato
            r = span * 0.5 * (0.7 + grow * 0.4)
            across = r * depth / span
            rx, ry = (across, r) if vertical else (r, across)
            p.setOpacity(max(0.0, min(1.0, peak * a)))
            p.drawImage(QRectF(x - rx, y - ry, 2 * rx, 2 * ry), glow_sprite(f["hue"], sat))
            p.setOpacity(1.0)

            edge_line = min(1.0, max(0.0, (pw - 0.3) / 0.4))
            if edge_line <= 0:
                continue  # le luci morbide sono solo alone, senza linea dura sul bordo
            length = span * (0.35 + grow * 0.5)
            th = (4 + 8 * a) * (0.5 + 0.5 * edge_line)
            if vertical:
                rect = QRectF(0 if edge == "L" else W - th, y - length / 2, th, length)
            else:
                rect = QRectF(x - length / 2, 0 if edge == "T" else H - th, length, th)
            p.setOpacity(max(0.0, min(1.0, a * edge_line)))
            p.drawImage(rect, edge_sprite(f["hue"], vertical))
            p.setOpacity(1.0)

    def _draw_lasers(self, p, W, H, now, area):
        self.lasers = [l for l in self.lasers if now - l["t0"] < LASER_LIFE]
        for l in self.lasers:
            if not l["rect"].intersects(area):
                continue
            k = (now - l["t0"]) / LASER_LIFE
            a = (1 - k) ** 1.6 * (0.55 + 0.45 * l["power"])
            x0, y0 = l["origin"]
            width = 22 if l["boost"] else 16
            sprite = beam_sprite(l["hue"], l["sat"])
            p.setOpacity(min(1.0, a))
            for start, sweep in l["beams"]:
                ang = start + sweep * k
                rad = math.radians(ang)
                length = ray_exit(x0, y0, math.cos(rad), math.sin(rad), W, H) + width
                p.save()
                p.translate(x0, y0)
                p.rotate(ang)
                p.drawImage(QRectF(0, -width / 2, length, width), sprite)
                p.restore()
            # bagliore sulla sorgente
            p.setOpacity(min(1.0, 0.8 * a))
            p.drawImage(QRectF(x0 - 40, y0 - 40, 80, 80),
                        glow_sprite(l["hue"], l["sat"] * 0.4))
            p.setOpacity(1.0)

    def _draw_hud(self, p, W, H, now):
        a = self.analyzer
        lat = (self.latency_estimate(a.grid[0]) if a.grid else None) or 0.0
        loud = self.mode == "Loud"   # Loud: analisi in alto a sinistra, punteggio al centro
        dot_x = 18 if loud else W - 18

        # spia: pulsa sulle battute della canzone, se le sente
        if a.running and a.enabled and a.grid:
            P, ph, _ = a.grid
            frac = ((now - lat - ph) % P) / P
            glow = 0.35 + 0.65 * math.exp(-frac * 7)
            radius = 8 + 5 * math.exp(-frac * 7)
        else:
            glow = 0.6 + 0.25 * math.sin(now * 3)
            radius = 8
        p.setOpacity(glow)
        p.drawImage(QRectF(dot_x - radius, 18 - radius, 2 * radius, 2 * radius),
                    glow_sprite(PALETTES[self.palette_name]["L"][0], 176))
        p.setOpacity(1.0)

        # testo di stato
        mult = f"  x{self.multiplier}" if self.multiplier > 1 else ""
        lines = [] if loud else [f"SCORE {self.score:,}{mult}"]
        if a.enabled and AUDIO_AVAILABLE:
            if not a.running:
                lines.append("analysis unavailable")
            elif now - a.last_sound > 2:
                lines.append("♪ silence")
            elif a.grid:
                song = round(60 / a.grid[0])
                you = f" · you {self.bpm}" if self.bpm else ""
                lines.append(f"♪ {song}{you} bpm")
            else:
                lines.append("♪ listening…")
            if self.results:
                acc = round(100 * sum(self.results) / len(self.results))
                lines.append(f"combo {self.combo} · {acc}%")
            if a.grid and len(self.latency) >= 6:
                lines.append(f"latency {round(lat * 1000)} ms")
        elif self.bpm:
            lines.append(f"{self.bpm} bpm")
        lines.append(f"render {self.paint_ms:.1f} ms")
        p.setFont(self.font_hud)
        p.setPen(QColor(255, 255, 255, 150))
        for i, text in enumerate(lines):
            if loud:
                p.drawText(QRectF(36, 10 + i * 16, 300, 16),
                           Qt.AlignLeft | Qt.AlignVCenter, text)
            else:
                p.drawText(QRectF(W - 330, 10 + i * 16, 300, 16),
                           Qt.AlignRight | Qt.AlignVCenter, text)

        # indicatore di energia: in che zona è la canzone adesso
        level = a.energy_at(now - lat)
        if level is not None:
            y = 10 + len(lines) * 16 + 6
            x0, w = (96, 120) if loud else (W - 150, 120)
            p.drawText(QRectF(x0 - 60, y - 6, 54, 16), Qt.AlignRight | Qt.AlignVCenter,
                       "energy")
            p.fillRect(QRectF(x0, y, w, 5), QColor(255, 255, 255, 40))
            if level >= ENERGY_PEAK:
                fill = QColor(255, 90, 210)
            elif level >= 0.5:
                fill = QColor.fromHsv(int(PALETTES[self.palette_name]["L"][0]), 200, 255)
            else:
                fill = QColor(130, 190, 255)
            p.fillRect(QRectF(x0, y, max(5, w * level), 5), fill)
            p.fillRect(QRectF(x0 + w * ENERGY_PEAK, y - 3, 2, 11),
                       QColor(255, 255, 255, 170))  # soglia del picco
            # triangolino: intensità dell'ultimo tasto, per confrontarla con la canzone
            if self.last_power and now - self.last_power[1] < 1.2:
                fade = 1 - (now - self.last_power[1]) / 1.2
                mx = round(x0 + w * self.last_power[0])
                p.fillRect(QRectF(mx - 1, y + 7, 3, 7), QColor(255, 255, 255, int(230 * fade)))

        # giudizio dell'ultimo tap, in alto al centro (in Loud c'è la colonna al centro)
        if self.judgement and not loud:
            j = self.judgement
            duration = 1.0 if j["big"] else 0.7
            k = (now - j["t0"]) / duration
            if k < 1:
                alpha = (1 - k) ** 1.5
                lift = round(10 * k)   # pixel interi: i caratteri restano quelli in cache
                c = QColor(*j["color"])
                c.setAlphaF(alpha)
                p.setFont(self.font_judge_big if j["big"] else self.font_judge)
                p.setPen(c)
                top = 34 if j["big"] else 40
                height = 60 if j["big"] else 40
                p.drawText(QRectF(0, top - lift, W, height), Qt.AlignCenter, j["label"])
                if j["detail"]:
                    p.setFont(self.font_detail)
                    c.setAlphaF(alpha * 0.8)
                    p.setPen(c)
                    p.drawText(QRectF(0, top + height - lift, W, 20), Qt.AlignCenter,
                               j["detail"])


    def _draw_loud(self, p, W, H, now):
        """Modalità Loud: punteggio grande al centro che cresce, colonna dei giudizi sopra."""
        dt = self.frame_dt
        # il punteggio mostrato rincorre quello vero: si vede "salire"
        gap = self.score - self.shown_score
        self.shown_score += gap * min(1.0, dt * 6)
        if abs(self.score - self.shown_score) < 1:
            self.shown_score = float(self.score)
        shown = int(round(self.shown_score))

        # più punti hai, più il numero è grande: 56 px a zero, ~145 px a 500.000
        size = min(190.0, 56 + 30 * math.log10(1 + shown / 500))
        size *= 1 + 0.12 * math.exp(-(now - self.score_bump) * 9)   # colpo a ogni punto
        scale = size / SCORE_SIZE
        cx, cy = W / 2, H / 2 + 60

        # alone scuro dietro, per leggere il numero su qualsiasi sfondo
        r = 260 + size * 2
        p.setOpacity(0.7)
        p.drawImage(QRectF(cx - r, cy - r * 0.8, 2 * r, 1.6 * r), shade_sprite())

        # cifre: sprite già sulla GPU, scalati
        p.setOpacity(1.0)
        digits = [text_sprite(ch, SCORE_SIZE, SCORE_WEIGHT) for ch in f"{shown:,}"]
        total = sum(d.width() for d in digits) / 2 * scale
        digit_h = (digits[0].height() / 2) * scale
        x = cx - total / 2
        for d in digits:
            w = d.width() / 2 * scale
            p.drawImage(QRectF(x, cy - digit_h / 2, w, digit_h), d)
            x += w

        # moltiplicatore e combo sotto il numero
        extra = []
        if self.multiplier > 1:
            extra.append(f"x{self.multiplier}")
        if self.combo:
            extra.append(f"combo {self.combo}")
        if extra:
            p.setFont(self.font_stats)
            p.setPen(QColor(255, 255, 255, 190))
            p.drawText(QRectF(0, round(cy + digit_h / 2 - 4), W, 24), Qt.AlignCenter,
                       "  ·  ".join(extra))

        # colonna dei giudizi: il più nuovo appena sopra il numero, i vecchi salgono e sfumano
        self.feed = [e for e in self.feed if now - e["t0"] < FEED_LIFE]
        bottom = cy - digit_h / 2 + 6
        for i, e in enumerate(self.feed):
            age = now - e["t0"]
            img = text_sprite(e["label"], LABEL_SIZE, QFont.Bold, 5, e["color"])
            s = 0.8 ** i * (1 + 0.3 * math.exp(-age * 12))   # i nuovi entrano con un colpo
            w, h = img.width() / 2 * s, img.height() / 2 * s
            target = bottom - h
            e["y"] = target if e["y"] is None else e["y"] + (target - e["y"]) * min(1.0, dt * 14)
            alpha = (1 - age / FEED_LIFE) ** 0.7 * 0.85 ** i
            p.setOpacity(max(0.0, min(1.0, alpha)))
            p.drawImage(QRectF(cx - w / 2, e["y"], w, h), img)
            if i == 0 and (e["points"] or e["note"]):
                detail = "  ·  ".join(x for x in (f"+{e['points']:,}" if e["points"] else "",
                                                     e["note"]) if x)
                p.setFont(self.font_detail)
                c = QColor(*e["color"])
                p.setPen(c)
                p.drawText(QRectF(round(cx + w / 2 + 12), round(e["y"] + h / 2 - 10), 300, 20),
                           Qt.AlignLeft | Qt.AlignVCenter, detail)
            bottom = e["y"] - 2
        p.setOpacity(1.0)


class CpuOverlay(OverlayBase, QWidget):
    """Disegno sulla CPU: compatibile ovunque, ma pesante con tanti effetti."""

    def __init__(self, screen, analyzer):
        QWidget.__init__(self, None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self._setup(analyzer)

    def paintEvent(self, event):
        self._render(QPainter(self), event.rect())  # Qt ritaglia già alla zona da aggiornare


if GPU_AVAILABLE:
    class GpuOverlay(OverlayBase, QOpenGLWindow):
        """Disegno sulla scheda video: finestra OpenGL trasparente, composta da Windows."""

        def __init__(self, screen, analyzer):
            QOpenGLWindow.__init__(self)
            fmt = QSurfaceFormat()
            fmt.setAlphaBufferSize(8)   # canale alfa: trasparenza pixel per pixel
            fmt.setSamples(4)           # bordi morbidi per anelli e testo
            fmt.setSwapInterval(1)      # sincronizzato col monitor
            self.setFormat(fmt)
            self.setFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                          Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
            g = screen.geometry()
            # un pixel più bassa dello schermo: così Windows non la scambia per un gioco a
            # schermo intero (che nasconderebbe barra delle applicazioni e notifiche)
            self.setGeometry(QRect(g.x(), g.y(), g.width(), g.height() - 1))
            self._setup(analyzer)
            self.prewarm = True

        def paintGL(self):
            self._render(QPainter(self), self.full_rect())


class MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


def make_gpu_window_transparent(hwnd):
    """Chiede a Windows (DWM) di comporre la finestra OpenGL usando il suo canale alfa,
    la stessa tecnica degli overlay dei giochi."""
    try:
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            wintypes.HWND(hwnd), ctypes.byref(MARGINS(-1, -1, -1, -1)))
    except Exception:
        pass


def make_icon(on):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QRadialGradient(QPointF(32, 32), 30)
    base = QColor(255, 80, 220) if on else QColor(120, 110, 140)
    g.setColorAt(0, base)
    g.setColorAt(1, QColor(base.red(), base.green(), base.blue(), 0))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(32, 32), 30, 30)
    p.end()
    return QIcon(pm)


def radio_menu(parent_menu, title, options, current, on_pick):
    """Sottomenu con scelta singola. Ritorna una funzione per aggiornare la spunta."""
    sub = parent_menu.addMenu(title)
    group = QActionGroup(sub)
    actions = {}
    for name in options:
        a = QAction(name, sub, checkable=True, checked=(name == current))
        a.triggered.connect(lambda _=False, n=name: on_pick(n))
        group.addAction(a)
        sub.addAction(a)
        actions[name] = a
    return lambda n: actions[n].setChecked(True)


def main():
    if sys.platform != "win32":
        sys.exit("Edge Flash funziona solo su Windows.")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    settings = QSettings("EdgeFlash", "EdgeFlash")

    analyzer = BeatAnalyzer()
    analyzer.enabled = settings.value("analysis", "true") == "true"
    renderer = settings.value("renderer", RENDERERS[0])
    if renderer not in RENDERERS:
        renderer = RENDERERS[0]
    use_gpu = renderer == "GPU" and GPU_AVAILABLE
    overlay = (GpuOverlay if use_gpu else CpuOverlay)(app.primaryScreen(), analyzer)
    if settings.value("palette") in PALETTES:
        overlay.palette_name = settings.value("palette")
    if settings.value("dim") in DIM_LEVELS:
        overlay.dim_name = settings.value("dim")
    if settings.value("laser") in LASER_MODES:
        overlay.laser_mode = settings.value("laser")
    if settings.value("mode") in MODES:
        overlay.mode = settings.value("mode")
    try:
        overlay.best = int(settings.value("best", 0))
    except (TypeError, ValueError):
        overlay.best = 0
    overlay.on_best = lambda score: settings.setValue("best", score)
    overlay.show()
    if use_gpu:
        make_gpu_window_transparent(int(overlay.winId()))
        overlay.update()   # primo fotogramma subito: carica le texture prima della sessione
    if analyzer.enabled:
        analyzer.start()

    tempo_timer = QTimer()
    tempo_timer.timeout.connect(analyzer.update_tempo)
    tempo_timer.start(400)

    bridge = KeyBridge()

    # ---------------------------------------------------------- tray
    tray = QSystemTrayIcon(make_icon(False))
    tray.setToolTip("Edge Flash - Ctrl+Alt+F per accendere")
    menu = QMenu()
    act_toggle = QAction("Accendi (Ctrl+Alt+F)")
    menu.addAction(act_toggle)
    menu.addSeparator()

    def set_palette(n):
        overlay.palette_name = n
        settings.setValue("palette", n)

    def set_dim(n):
        overlay.dim_name = n
        settings.setValue("dim", n)
        check_dim(n)

    def set_laser(n):
        overlay.laser_mode = n
        settings.setValue("laser", n)
        check_laser(n)

    def set_mode(n):
        overlay.mode = n
        overlay.feed.clear()
        settings.setValue("mode", n)
        check_mode(n)

    check_mode = radio_menu(menu, "Modalità (Ctrl+Alt+M)", MODES, overlay.mode, set_mode)
    radio_menu(menu, "Palette", PALETTES, overlay.palette_name, set_palette)
    check_dim = radio_menu(menu, "Scurisci schermo (Ctrl+Alt+D)", DIM_LEVELS,
                           overlay.dim_name, set_dim)
    check_laser = radio_menu(menu, "Laser (Ctrl+Alt+L)", LASER_MODES,
                             overlay.laser_mode, set_laser)

    def set_renderer(n):
        if n == renderer:
            return
        settings.setValue("renderer", n)
        settings.sync()
        restart_app()  # il motore di disegno si sceglie all'avvio

    radio_menu(menu, "Rendering", RENDERERS if GPU_AVAILABLE else RENDERERS[1:],
               renderer if use_gpu else RENDERERS[1], set_renderer)

    def cycle(options, current, setter):
        options = list(options)
        setter(options[(options.index(current) + 1) % len(options)])

    menu.addSeparator()
    act_analysis = QAction("Analizza il ritmo della musica", checkable=True,
                           checked=analyzer.enabled)
    act_recal = QAction("Ricalibra latenza")
    if not AUDIO_AVAILABLE:
        act_analysis.setText("Analisi musica (librerie mancanti)")
        act_analysis.setEnabled(False)
        act_recal.setEnabled(False)

    def set_analysis(on):
        analyzer.enabled = on
        settings.setValue("analysis", "true" if on else "false")
        if on:
            analyzer.start()
        overlay.reset_calibration()

    act_analysis.toggled.connect(set_analysis)
    act_recal.triggered.connect(overlay.reset_calibration)
    menu.addAction(act_analysis)
    menu.addAction(act_recal)
    menu.addSeparator()
    act_quit = QAction("Esci")
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.show()

    def set_armed(on, restore_focus=True):
        if on == overlay.armed:
            return
        overlay.set_armed(on)
        if on:
            catcher.grab(app.primaryScreen())
            QTimer.singleShot(500, check_focus)
        else:
            catcher.release(restore_focus)
        tray.setIcon(make_icon(on))
        act_toggle.setText(("Spegni" if on else "Accendi") + " (Ctrl+Alt+F)")

    def check_focus():
        # Windows a volte nega il focus: meglio dirlo che restare accesi senza tasti
        if overlay.armed and not catcher.got_focus:
            set_armed(False, restore_focus=False)
            tray.showMessage("Edge Flash", "Non sono riuscito a prendere la tastiera. "
                             "Riprova con Ctrl+Alt+F.", make_icon(False), 3000)

    catcher = KeyCatcher(on_key=overlay.fire,
                         on_escape=lambda: set_armed(False),
                         on_lost=lambda: set_armed(False, restore_focus=False))

    actions = HotkeyActions(
        toggle=lambda: set_armed(not overlay.armed),
        cycle_dim=lambda: cycle(DIM_LEVELS, overlay.dim_name, set_dim),
        cycle_laser=lambda: cycle(LASER_MODES, overlay.laser_mode, set_laser),
        cycle_mode=lambda: cycle(MODES, overlay.mode, set_mode))
    bridge.toggle.connect(actions.toggle, Qt.QueuedConnection)
    bridge.cycle_dim.connect(actions.cycle_dim, Qt.QueuedConnection)
    bridge.cycle_laser.connect(actions.cycle_laser, Qt.QueuedConnection)
    bridge.cycle_mode.connect(actions.cycle_mode, Qt.QueuedConnection)
    act_toggle.triggered.connect(lambda: set_armed(not overlay.armed))
    tray.activated.connect(lambda reason: reason == QSystemTrayIcon.Trigger
                           and set_armed(not overlay.armed))

    # ---------------------------------------------------------- scorciatoie globali
    hotkeys = HotkeyThread({VK_F: bridge.toggle, VK_D: bridge.cycle_dim,
                            VK_L: bridge.cycle_laser, VK_M: bridge.cycle_mode})
    hotkeys.start()
    hotkeys.ready.wait(2)

    def quit_app():
        if overlay.armed:
            set_armed(False)
        hotkeys.stop()
        hotkeys.join(1)   # libera le scorciatoie prima che una nuova istanza le registri
        analyzer.stop()
        tray.hide()
        app.quit()

    def restart_app():
        quit_app()
        subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                         cwd=os.path.dirname(os.path.abspath(__file__)))

    act_quit.triggered.connect(quit_app)

    if hotkeys.failed:
        taken = ", ".join("Ctrl+Alt+" + chr(c) for c in hotkeys.failed)
        tray.showMessage("Edge Flash", f"{taken} già usata da un'altra app. "
                         "Puoi usare il menu dell'icona.", make_icon(False), 5000)
    else:
        tray.showMessage("Edge Flash", "Pronto. Premi Ctrl+Alt+F per accendere i flash.",
                         make_icon(True), 3000)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

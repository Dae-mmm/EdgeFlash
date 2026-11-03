"""
Edge Flash - overlay di flash e laser ai bordi dello schermo, a tempo di musica.

Ctrl+Alt+F  accende / spegne la modalità fidget
Ctrl+Alt+D  cambia quanto viene scurito lo schermo (spento, leggero, medio, forte)
Ctrl+Alt+L  laser: spenti, insieme ai flash, solo laser
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
L'audio viene analizzato al volo e mai registrato.
"""

import cmath
import ctypes
import math
import random
import sys
import threading
import time
from collections import deque
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QLinearGradient,
                           QPainter, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
    AUDIO_AVAILABLE = True
except Exception:  # librerie mancanti: l'app funziona lo stesso, senza analisi
    AUDIO_AVAILABLE = False

# ---------------------------------------------------------------- impostazioni
LIFE = 0.7            # durata di un flash in secondi
LASER_LIFE = 0.55     # durata di un laser in secondi

# Scurimento dello schermo mentre la modalità fidget è accesa (0 = niente, 1 = nero)
DIM_LEVELS = {"Spento": 0.0, "Leggero": 0.35, "Medio": 0.55, "Forte": 0.75}
DIM_TINT = (6, 4, 14)  # nero leggermente violaceo, più morbido del nero puro
DIM_FADE = 0.25        # secondi per scurire / schiarire

LASER_MODES = ["Spenti", "Con i flash", "Solo laser"]

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
# Ogni voce: (bordo, posizione lungo il bordo 0..1, sfumatura colore 0..1, onda grande)
def vk(ch):
    return ord(ch)

KEYMAP = {}

# Bordo SUPERIORE: riga dei numeri  \ 1 2 3 4 5 6 7 8 9 0 ' ì
TOP = [0xDC] + [vk(c) for c in "1234567890"] + [0xDB, 0xDD]
for i, code in enumerate(TOP):
    t = i / (len(TOP) - 1)
    KEYMAP[code] = ("T", 0.06 + t * 0.88, t, False)

# Bordi LATERALI: Q e A divise tra le due mani, più gli estremi della riga in basso.
LEFT = [
    [vk(c) for c in "QWERT"],
    [vk(c) for c in "ASDFG"],
    [0xE2] + [vk(c) for c in "ZXC"],          # <  z  x  c
]
RIGHT = [
    [vk(c) for c in "YUIOP"] + [0xBA, 0xBB],  # y u i o p è +
    [vk(c) for c in "HJKL"] + [0xC0, 0xDE, 0xBF],  # h j k l ò à ù
    [0xBC, 0xBE, 0xBD],                       # ,  .  -
]
ROW_Y = (0.22, 0.5, 0.78)
for side, rows in (("L", LEFT), ("R", RIGHT)):
    for r, row in enumerate(rows):
        for c, code in enumerate(row):
            t = c / max(len(row) - 1, 1)
            outer = 1 - t if side == "L" else t
            KEYMAP[code] = (side, ROW_Y[r] - 0.09 + 0.18 * (1 - outer), t, False)

# Bordo INFERIORE: v b n m al centro, spazio come onda larga
BOTTOM = [vk(c) for c in "VBNM"]
for i, code in enumerate(BOTTOM):
    t = i / (len(BOTTOM) - 1)
    KEYMAP[code] = ("B", 0.3 + t * 0.4, t, False)
KEYMAP[0x20] = ("B", 0.5, 0.5, True)          # spazio

KEYMAP.update({
    0x0D: ("T", 0.5, 0.5, True),    # invio: onda dall'alto
    0x25: ("L", 0.5, 0.5, False),   # freccia sinistra
    0x26: ("T", 0.5, 0.5, False),   # freccia su
    0x27: ("R", 0.5, 0.5, False),   # freccia destra
    0x28: ("B", 0.5, 0.5, False),   # freccia giù
})

VK_F, VK_D, VK_L = 0x46, 0x44, 0x4C
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
            if np.sqrt(np.mean(x * x)) > 1e-4:
                self.last_sound = now
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

    def update_tempo(self):
        """Chiamata un paio di volte al secondo: ricalcola BPM e fase."""
        if not self.running:
            self.grid = None
            return
        now = time.perf_counter()
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


class HotkeyActions(QObject):
    """Riceve le scorciatoie nel thread principale, dove si può toccare la grafica."""

    def __init__(self, toggle, cycle_dim, cycle_laser):
        super().__init__()
        self._toggle, self._dim, self._laser = toggle, cycle_dim, cycle_laser

    @Slot()
    def toggle(self):
        self._toggle()

    @Slot()
    def cycle_dim(self):
        self._dim()

    @Slot()
    def cycle_laser(self):
        self._laser()


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


class Overlay(QWidget):
    def __init__(self, screen, analyzer):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self.analyzer = analyzer
        self.flashes = []
        self.lasers = []
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
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(16)

    def reset_calibration(self):
        self.latency = deque(maxlen=24)   # scarti grezzi, per stimare la latenza cuffie
        self.judgement = None             # (scritta, colore, ms, levare, t0, punti)

    def reset_session(self):
        self.results = deque(maxlen=32)   # precisione degli ultimi tap, per l'HUD
        self.combo = 0
        self.max_combo = 0
        self.score = 0
        self.judged = 0                   # tap giudicati sul ritmo
        self.precision = 0.0              # somma dei pesi dei giudizi
        self.tap_count = 0

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
    def fire(self, edge, pos, hue_t, big, t_key):
        if not self.armed:
            return
        label = self._judge(t_key)
        boost = label == "PERFECT"
        if label in (None, "CALIBRATING"):
            self.score += FREESTYLE_POINTS
        self.tap_count += 1
        if self.laser_mode != "Solo laser":
            self._add(edge, pos, hue_t, big, boost)
        if self.laser_mode != "Spenti":
            self._add_laser(edge, pos, hue_t, big, boost)
        self._tap()

    def _judge(self, t):
        """Confronta il tap con il ritmo della canzone. Ritorna il giudizio o None."""
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
            self.judgement = ("CALIBRATING", (180, 176, 200), None, False,
                              time.perf_counter(), 0)
            return "CALIBRATING"
        x = ((raw - lat + half) % P) - half     # rispetto al battere, latenza tolta
        offbeat = abs(x) > P / 4
        err = x - math.copysign(half, x) if offbeat else x
        ms = err * 1000
        for limit, label, color, points, weight in JUDGEMENTS:
            if abs(ms) <= limit:
                break
        good = label in ("PERFECT", "GREAT")
        points *= self.multiplier
        self.score += points
        self.combo = self.combo + 1 if good else 0
        self.max_combo = max(self.max_combo, self.combo)
        self.judged += 1
        self.precision += weight
        self.results.append(weight)
        self.judgement = (label, color, ms, offbeat, time.perf_counter(), points)
        return label

    def _palette_hue(self, edge, hue_t):
        p = PALETTES[self.palette_name]
        h0, h1 = p[edge]
        return (h0 + (h1 - h0) * hue_t + random.uniform(-4, 4)) % 360, p

    def _add(self, edge, pos, hue_t, big, boost=False):
        hue, p = self._palette_hue(edge, hue_t)
        self.flashes.append(dict(edge=edge, pos=pos, hue=hue, sat=p["sat"], val=p["val"],
                                 t0=time.perf_counter(), big=big, boost=boost))
        del self.flashes[:-120]

    def _add_laser(self, edge, pos, hue_t, big, boost=False):
        hue, p = self._palette_hue(edge, hue_t)
        if big:
            # ventaglio di fasci che si aprono a forbice
            beams = [(-55 + i * 110 / 6, 18 if i % 2 else -18) for i in range(7)]
        else:
            beams = [(None, random.choice((-1, 1)) * random.uniform(7, 13))]
        self.lasers.append(dict(edge=edge, pos=0.5 if big else pos, hue=hue, sat=p["sat"],
                                beams=beams, t0=time.perf_counter(), boost=boost))
        del self.lasers[:-60]

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
            tier = 3 if acc >= 0.9 else 2 if acc >= 0.76 else 1 if acc >= 0.63 else 0
            stats = (f"max combo {self.max_combo}  ·  {round(acc * 100)}% accuracy"
                     f"  ·  {self.tap_count} taps")
        else:  # niente analisi: conta l'impegno
            n = self.tap_count
            tier = 3 if n >= 150 else 2 if n >= 60 else 1 if n >= 20 else 0
            stats = f"{n} taps"
        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            self.on_best(self.score)
        self.summary = dict(t0=time.perf_counter(), comment=random.choice(COMMENTS[tier]),
                            score=self.score, stats=stats, new_best=new_best,
                            hue=PALETTES[self.palette_name]["L"][0])
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

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)

        now = time.perf_counter()
        dt = now - self.last_paint
        self.last_paint = now

        # velo scuro: entra e esce con una dissolvenza quando accendi/spegni
        target = DIM_LEVELS[self.dim_name] if (self.armed or self.summary) else 0.0
        step = dt / DIM_FADE
        if self.dim_now < target:
            self.dim_now = min(target, self.dim_now + step)
        else:
            self.dim_now = max(target, self.dim_now - step)
        if self.dim_now > 0.003:
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            veil = QColor(*DIM_TINT)
            veil.setAlphaF(self.dim_now)
            p.fillRect(self.rect(), veil)

        p.setCompositionMode(QPainter.CompositionMode_Plus)
        self._draw_flashes(p, W, H, now)
        self._draw_lasers(p, W, H, now)

        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self.armed:
            self._draw_hud(p, W, H, now)
        if self.summary:
            self._draw_summary(p, W, H, now)
        p.end()

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
        g = QRadialGradient(QPointF(W / 2, H / 2), min(W, H) * 0.45)
        g.setColorAt(0, QColor(0, 0, 0, int(150 * alpha)))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRect(QRectF(0, 0, W, H))

        shown = int(s["score"] * min(t / 0.8, 1) ** 0.5)   # il punteggio "conta" da zero
        accent = QColor.fromHsv(int(s["hue"]), 150, 255)
        accent.setAlphaF(alpha)
        white = QColor(255, 255, 255)
        white.setAlphaF(alpha)

        p.save()
        p.translate(W / 2, H / 2)
        p.rotate(tilt)
        p.scale(scale, scale)
        f = QFont("Segoe UI", 40, QFont.Bold)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 6)
        p.setFont(f)
        p.setPen(accent)
        p.drawText(QRectF(-W, -120, 2 * W, 70), Qt.AlignCenter, s["comment"])
        f = QFont("Segoe UI", 88, QFont.Black)
        p.setFont(f)
        p.setPen(white)
        p.drawText(QRectF(-W, -55, 2 * W, 130), Qt.AlignCenter, f"{shown:,}")
        p.restore()

        p.setFont(QFont("Consolas", 12))
        dim = QColor(255, 255, 255)
        dim.setAlphaF(0.7 * alpha)
        p.setPen(dim)
        p.drawText(QRectF(0, H / 2 + 95, W, 24), Qt.AlignCenter, s["stats"])
        if s["new_best"] and s["score"] > 0:
            gold = QColor(255, 210, 90)
            gold.setAlphaF(alpha * (0.6 + 0.4 * math.sin(now * 10)))
            f = QFont("Segoe UI", 14, QFont.Bold)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 5)
            p.setFont(f)
            p.setPen(gold)
            p.drawText(QRectF(0, H / 2 + 125, W, 28), Qt.AlignCenter, "NEW BEST!")

    def _draw_flashes(self, p, W, H, now):
        S = min(W, H)
        self.flashes = [f for f in self.flashes if now - f["t0"] < LIFE]
        for f in self.flashes:
            k = (now - f["t0"]) / LIFE
            a = (1 - k) ** 2.2
            grow = 1 - (1 - k) ** 3
            edge = f["edge"]
            vertical = edge in "LR"
            x, y = self._anchor(edge, f["pos"], W, H)
            if f["big"]:
                span = (H if vertical else W) * 0.95
                depth = S * 0.55
            else:
                span = (H * 0.5) if vertical else (W * 0.26)
                depth = S * 0.36
            if f["boost"]:  # tap perfetto: flash più grande e più luminoso
                span *= 1.2
                depth *= 1.25
            depth *= 0.55 + grow * 0.6
            peak = 1.0 if f["boost"] else 0.85

            def col(alpha):
                c = QColor.fromHsv(int(f["hue"]), f["sat"], f["val"])
                c.setAlphaF(max(0.0, min(1.0, alpha)))
                return c

            p.save()
            p.translate(x, y)
            if vertical:
                p.scale(depth / span, 1)
            else:
                p.scale(1, depth / span)
            r = span * 0.5 * (0.7 + grow * 0.4)
            g = QRadialGradient(QPointF(0, 0), r)
            g.setColorAt(0, col(peak * a))
            g.setColorAt(0.35, col(0.45 * a))
            g.setColorAt(1, col(0))
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), r, r)
            p.restore()

            length = span * (0.35 + grow * 0.5)
            th = 4 + 8 * a
            hot = QColor.fromHsv(int(f["hue"]), int(f["sat"] * 0.35), 255)
            if vertical:
                lg = QLinearGradient(0, y - length / 2, 0, y + length / 2)
                rect = QRectF(0 if edge == "L" else W - th, y - length / 2, th, length)
            else:
                lg = QLinearGradient(x - length / 2, 0, x + length / 2, 0)
                rect = QRectF(x - length / 2, 0 if edge == "T" else H - th, length, th)
            hot.setAlphaF(a)
            clear = QColor(hot)
            clear.setAlphaF(0)
            lg.setColorAt(0, clear)
            lg.setColorAt(0.5, hot)
            lg.setColorAt(1, clear)
            p.fillRect(rect, lg)

    def _draw_lasers(self, p, W, H, now):
        reach = math.hypot(W, H) * 1.1
        self.lasers = [l for l in self.lasers if now - l["t0"] < LASER_LIFE]
        for l in self.lasers:
            k = (now - l["t0"]) / LASER_LIFE
            a = (1 - k) ** 1.6
            edge, pos = l["edge"], l["pos"]
            x0, y0 = self._anchor(edge, pos, W, H)
            # direzione verso il lato opposto, in posizione speculare: i fasci si incrociano
            if edge == "L":
                tx, ty = W, (1 - pos) * H
            elif edge == "R":
                tx, ty = 0, (1 - pos) * H
            elif edge == "T":
                tx, ty = (1 - pos) * W, H
            else:
                tx, ty = (1 - pos) * W, 0
            base = math.degrees(math.atan2(ty - y0, tx - x0))
            normal = {"L": 0, "R": 180, "T": 90, "B": -90}[edge]
            widths = (22, 8, 2.4) if l["boost"] else (16, 6, 2.0)
            for spread, sweep in l["beams"]:
                ang = (normal + spread if spread is not None else base) + sweep * k
                rad = math.radians(ang)
                x1, y1 = x0 + math.cos(rad) * reach, y0 + math.sin(rad) * reach
                for width, alpha, white in ((widths[0], 0.10, 0.0),
                                            (widths[1], 0.30, 0.2),
                                            (widths[2], 0.95, 0.7)):
                    c = QColor.fromHsv(int(l["hue"]), int(l["sat"] * (1 - white)), 255)
                    c.setAlphaF(alpha * a)
                    pen = QPen(c, width)
                    pen.setCapStyle(Qt.RoundCap)
                    p.setPen(pen)
                    p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
            # bagliore sulla sorgente
            g = QRadialGradient(QPointF(x0, y0), 40)
            c = QColor.fromHsv(int(l["hue"]), int(l["sat"] * 0.4), 255)
            c.setAlphaF(0.8 * a)
            g.setColorAt(0, c)
            c2 = QColor(c)
            c2.setAlphaF(0)
            g.setColorAt(1, c2)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(x0, y0), 40, 40)

    def _draw_hud(self, p, W, H, now):
        a = self.analyzer
        lat = (self.latency_estimate(a.grid[0]) if a.grid else None) or 0.0

        # spia in alto a destra: pulsa sulle battute della canzone, se le sente
        accent = QColor.fromHsv(int(PALETTES[self.palette_name]["L"][0]), 200, 255)
        if a.running and a.enabled and a.grid:
            P, ph, _ = a.grid
            frac = ((now - lat - ph) % P) / P
            accent.setAlphaF(0.25 + 0.75 * math.exp(-frac * 7))
            radius = 4 + 3 * math.exp(-frac * 7)
        else:
            accent.setAlphaF(0.55 + 0.25 * math.sin(now * 3))
            radius = 4
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QPointF(W - 18, 18), radius, radius)

        # testo di stato
        mult = f"  x{self.multiplier}" if self.multiplier > 1 else ""
        lines = [f"SCORE {self.score:,}{mult}"]
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
        p.setFont(QFont("Consolas", 9))
        p.setPen(QColor(255, 255, 255, 150))
        for i, text in enumerate(lines):
            p.drawText(QRectF(W - 330, 10 + i * 16, 300, 16),
                       Qt.AlignRight | Qt.AlignVCenter, text)

        # giudizio dell'ultimo tap, in alto al centro
        if self.judgement:
            label, color, ms, offbeat, t0, points = self.judgement
            k = (now - t0) / 0.7
            if k < 1:
                alpha = (1 - k) ** 1.5
                lift = 10 * k
                c = QColor(*color)
                c.setAlphaF(alpha)
                font = QFont("Segoe UI", 24, QFont.Bold)
                font.setLetterSpacing(QFont.AbsoluteSpacing, 4)
                p.setFont(font)
                p.setPen(c)
                p.drawText(QRectF(0, 40 - lift, W, 40), Qt.AlignCenter, label)
                small = QFont("Consolas", 10)
                p.setFont(small)
                c.setAlphaF(alpha * 0.8)
                p.setPen(c)
                if ms is None:
                    detail = "tap along to the beat"
                else:
                    detail = (f"{ms:+.0f} ms" + ("  ·  off-beat" if offbeat else "")
                              + (f"  ·  +{points}" if points else ""))
                p.drawText(QRectF(0, 80 - lift, W, 20), Qt.AlignCenter, detail)


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
    overlay = Overlay(app.primaryScreen(), analyzer)
    if settings.value("palette") in PALETTES:
        overlay.palette_name = settings.value("palette")
    if settings.value("dim") in DIM_LEVELS:
        overlay.dim_name = settings.value("dim")
    if settings.value("laser") in LASER_MODES:
        overlay.laser_mode = settings.value("laser")
    try:
        overlay.best = int(settings.value("best", 0))
    except (TypeError, ValueError):
        overlay.best = 0
    overlay.on_best = lambda score: settings.setValue("best", score)
    overlay.show()
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

    radio_menu(menu, "Palette", PALETTES, overlay.palette_name, set_palette)
    check_dim = radio_menu(menu, "Scurisci schermo (Ctrl+Alt+D)", DIM_LEVELS,
                           overlay.dim_name, set_dim)
    check_laser = radio_menu(menu, "Laser (Ctrl+Alt+L)", LASER_MODES,
                             overlay.laser_mode, set_laser)

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
        cycle_laser=lambda: cycle(LASER_MODES, overlay.laser_mode, set_laser))
    bridge.toggle.connect(actions.toggle, Qt.QueuedConnection)
    bridge.cycle_dim.connect(actions.cycle_dim, Qt.QueuedConnection)
    bridge.cycle_laser.connect(actions.cycle_laser, Qt.QueuedConnection)
    act_toggle.triggered.connect(lambda: set_armed(not overlay.armed))
    tray.activated.connect(lambda reason: reason == QSystemTrayIcon.Trigger
                           and set_armed(not overlay.armed))

    # ---------------------------------------------------------- scorciatoie globali
    hotkeys = HotkeyThread({VK_F: bridge.toggle, VK_D: bridge.cycle_dim,
                            VK_L: bridge.cycle_laser})
    hotkeys.start()
    hotkeys.ready.wait(2)

    def quit_app():
        if overlay.armed:
            set_armed(False)
        hotkeys.stop()
        analyzer.stop()
        app.quit()

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

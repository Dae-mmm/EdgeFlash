# Edge Flash

Turn your keyboard into concert lights while you listen to music.

Edge Flash is a transparent, click-through overlay for Windows that sits on top of everything. Switch it on and every key fires a flash or a laser from the edge of the screen. It also listens to what your PC is playing, finds the beat, and tells you if you are tapping in time, rhythm-game style.

## Features

- **Edge flashes** mapped to the keyboard layout (Italian QWERTY):
  - number row → top edge
  - left-hand letters (`Q W E R T`, `A S D F G`, `< Z X C`) → left edge
  - right-hand letters (`Y U I O P è +`, `H J K L ò à ù`, `, . -`) → right edge
  - `V B N M` and Space → bottom edge
  - the row sets the height on the side edges, so a roll across `A S D F` travels along the border
- **Lasers** that cross the screen, with Space and Enter opening a 7-beam fan
- **Screen dimming** while active, so the colors pop
- **Beat detection** on the audio your PC is playing (Spotify, YouTube, anything), no microphone
- **Timing judgements**: PERFECT / GREAT / GOOD / ALMOST / MISS, with off-beat taps accepted
- **Automatic latency calibration**, Bluetooth headphones included
- **Session score** with combo multiplier, a random end-of-session comment and a saved personal best
- 4 color palettes

## Controls

| Shortcut | Action |
| --- | --- |
| `Ctrl+Alt+F` | Start / end a session |
| `Esc` | End the session |
| `Ctrl+Alt+D` | Cycle screen dimming (off, light, medium, strong) |
| `Ctrl+Alt+L` | Cycle lasers (off, with flashes, lasers only) |

The tray icon menu has palettes, dimming, lasers, the beat analysis toggle and latency recalibration. Settings and your best score are remembered.

While a session is off, Edge Flash ignores the keyboard and everything reaches your apps as usual. While it is on, the mapped keys trigger lights and are **not** typed into the active window, so you can drum without messing up your work. Shortcuts with Ctrl, Alt or Win still work.

## Install

Requires Windows and [Python 3](https://www.python.org/downloads/).

1. Download or clone this repository.
2. Double-click `Avvia Edge Flash.bat`.

The first run creates a local virtual environment and installs the dependencies, then Edge Flash starts in the tray. Later runs start it directly.

Manual install:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw edge_flash.py
```

## Privacy and antivirus

Edge Flash uses a global keyboard hook (via `pynput`) to react to keys while other apps are focused, the same mechanism keyloggers use. Nothing is stored or sent anywhere: keys only trigger lights, and audio is analyzed in memory and never recorded.

Because of the hook, some antivirus tools may quarantine `pynput`. If the app stops starting after the first run, check your antivirus history. Avoid running it on a managed work computer without asking your IT team.

## How the beat detection works

Edge Flash captures the system output through WASAPI loopback and computes a spectral flux onset detector on two bands (kick drum and everything else). From the last 10 seconds of onsets it estimates the tempo between 70 and 180 BPM and the beat phase, using the strongest low-frequency hits to find the downbeat. Your tap times are compared with that grid after subtracting your measured headphone latency.

It works best with music that has a clear kick drum. Quiet acoustic tracks may not lock.

## License

MIT

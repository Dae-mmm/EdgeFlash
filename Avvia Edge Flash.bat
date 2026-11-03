@echo off
rem Prima volta: crea un ambiente Python locale e installa le librerie.
rem Le volte successive: avvia direttamente Edge Flash senza finestra di console.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

if not exist ".venv\Scripts\pythonw.exe" (
    echo Prima configurazione, ci vuole un minuto...
    %PY% -m venv .venv || (echo Python non trovato. Installalo da python.org e riprova. & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
)

if not exist ".venv\librerie_v2.ok" (
    echo Installo le librerie...
    ".venv\Scripts\python.exe" -m pip install PySide6 numpy PyAudioWPatch || (echo Installazione non riuscita. & pause & exit /b 1)
    echo ok> ".venv\librerie_v2.ok"
)

start "" ".venv\Scripts\pythonw.exe" edge_flash.py

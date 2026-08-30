@echo off
REM Launch JeevesBot from the folder this script lives in.
cd /d "%~dp0"

REM Prefer the `py` launcher. A bare `python` on Windows can resolve to the
REM Microsoft Store stub, whose MSIX sandbox silently hangs the subprocess
REM spawns the bot needs to launch the game server — it does not error, it
REM just never returns. Set PYTHON yourself to pin a specific interpreter:
REM   set "PYTHON=C:\Users\You\AppData\Local\Programs\Python\Python312\python.exe"
if not defined PYTHON (
    where /q py && (set "PYTHON=py -3") || (set "PYTHON=python")
)

%PYTHON% Jeeves.py

REM Deliberately no `pause` here. Under a scheduled task it would block forever
REM waiting on a keypress nobody is there to press. Run this from an already
REM open console window if you want to read the output after it exits.

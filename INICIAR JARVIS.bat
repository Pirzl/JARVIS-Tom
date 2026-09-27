@echo off
REM ── J.A.R.V.I.S-Tom launcher ──────────────────────────────────────────────
REM Starts the assistant with the venv interpreter, so the user never has to
REM think about which Python is active. Double-click this file.
cd /d "E:\JARVIS 3.0\Mark-LIV-main - copia"
if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo   ERROR: the virtual environment is missing.
    echo   Run this once from a terminal:
    echo     .venv\Scripts\python.exe setup.py
    echo.
    pause
    exit /b 1
)
REM pythonw = no console window. Stdout is redirected to logs\launch.log so
REM there is still a place to look if startup fails.
if not exist "logs" mkdir "logs"
start "" ".venv\Scripts\pythonw.exe" "main.py"

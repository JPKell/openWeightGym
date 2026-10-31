@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.13 -m venv .venv
    if errorlevel 1 goto failed
)
if not exist ".venv\voice-installed-aec-v1" (
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto failed
    type nul > .venv\voice-installed-aec-v1
)
.venv\Scripts\python.exe -m voice.desktop %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Install Python 3.13 from python.org, including Tcl/Tk and the Python launcher.
pause
exit /b 1

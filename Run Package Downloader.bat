@echo off
REM Starts Package Downloader straight from the .py (no build needed).
REM Use it on the PC with internet if you haven't built the .exe yet.
setlocal
cd /d "%~dp0"
call :findpython
if errorlevel 1 (
    pause
    exit /b 1
)
"%PY%" -c "import PySide6.QtWidgets, PySide6.QtSvg" >nul 2>nul
if errorlevel 1 (
    echo Installing PySide6 ^(the window toolkit, about 70 MB^)...
    "%PY%" -m pip install PySide6-Essentials
)
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%~dp0package_downloader.py" %*
exit /b 0

REM ---------------------------------------------------------------------------
REM Finds a real Python 3 (py launcher, PATH, or the usual install folders) and
REM puts its full path in PY. The Microsoft Store "python" shortcut is skipped.
:findpython
set "PY="
for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY if exist "%PY%" exit /b 0
set "PY="
for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY if exist "%PY%" exit /b 0
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)
if defined PY exit /b 0
echo.
echo Python 3 was not found on this PC (the Microsoft Store shortcut doesn't count).
echo Install Python 3 from python.org and tick "Add python.exe to PATH", then run this again.
exit /b 1

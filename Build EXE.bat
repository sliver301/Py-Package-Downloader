@echo off
REM Builds "Package Downloader.exe" (a single file) in this folder.
REM Build it here on the offline PC: the exe then knows this PC's Python version and
REM picks it by default. Copy the exe to the PC with internet and run it there.
REM Needs PySide6 (the window toolkit) and PyInstaller in that Python.
setlocal
cd /d "%~dp0"
call :findpython
if errorlevel 1 (
    pause
    exit /b 1
)
echo Using Python: %PY%
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo pip is missing in this Python. Adding it...
    "%PY%" -m ensurepip --default-pip
)
"%PY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 if exist "PyInstaller offline\wheels" (
    echo PyInstaller is not installed. Installing it from the "PyInstaller offline" folder...
    "%PY%" -m pip install --no-index --find-links "PyInstaller offline\wheels" -r "PyInstaller offline\requirements.txt"
)
"%PY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed. Trying to install it ^(needs internet^)...
    "%PY%" -m pip install pyinstaller
    "%PY%" -m PyInstaller --version >nul 2>nul
    if errorlevel 1 goto :nopyinstaller
)
REM Remember this PC's Python version and Windows type (the default target in the portal)
"%PY%" -c "import sys,sysconfig,json;json.dump({'python':'.'.join(map(str,sys.version_info[:2])),'platform':sysconfig.get_platform().replace('-','_').replace('.','_')},open('build_info.json','w'))"
type build_info.json
echo.
"%PY%" -c "import PySide6.QtWidgets, PySide6.QtSvg" >nul 2>nul
if errorlevel 1 (
    echo PySide6 ^(the window toolkit^) is not installed in this Python. Trying to install it ^(needs internet^)...
    "%PY%" -m pip install PySide6-Essentials
    "%PY%" -c "import PySide6.QtWidgets, PySide6.QtSvg" >nul 2>nul
    if errorlevel 1 (
        echo PySide6 could not be installed. Use a Python that already has PySide6, or turn the
        echo internet on once and run this file again.
        pause
        exit /b 1
    )
)
REM An open copy of the app locks its .exe and makes the build fail at the very end.
:unlock
set "LOCKED="
if exist "dist\Package Downloader.exe" del /f /q "dist\Package Downloader.exe" >nul 2>nul
if exist "dist\Package Downloader.exe" set "LOCKED=1"
REM (appending nothing to a file fails while that program is running)
if exist "Package Downloader.exe" (type nul>>"Package Downloader.exe") 2>nul || set "LOCKED=1"
if defined LOCKED (
    echo.
    echo Package Downloader is still open ^(or its file is in use^).
    echo Close every Package Downloader window, then press a key to try again.
    pause >nul
    goto :unlock
)
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "Package Downloader" --icon app_icon.ico ^
  --collect-all pip --add-data "build_info.json;." --add-data "app_icon.ico;." ^
  --hidden-import package_downloader_ui --hidden-import concurrent.futures.thread ^
  --exclude-module tkinter --exclude-module numpy --exclude-module PIL --exclude-module PyQt5 ^
  --exclude-module PySide6.QtNetwork --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtPdf --exclude-module PySide6.QtOpenGL --exclude-module PySide6.QtMultimedia ^
  package_downloader.py
set "BUILDRC=%errorlevel%"
echo.
if "%BUILDRC%"=="0" if exist "dist\Package Downloader.exe" (
    copy /y "dist\Package Downloader.exe" "Package Downloader.exe" >nul || echo Could not replace "Package Downloader.exe" - close it and run this again.
    rmdir /s /q build >nul 2>nul
    echo Done: "%~dp0Package Downloader.exe"
    echo Copy that file to the PC with internet and double-click it there.
) else (
    echo Build failed - see the last lines above.
)
pause
exit /b 0

:nopyinstaller
echo.
echo PyInstaller could not be installed (this PC has no internet).
echo Two ways out:
echo   1. Copy this whole folder to the PC with internet and run "Build EXE.bat" there, or
echo   2. On the PC with internet run "Run Package Downloader.bat", add "pyinstaller",
echo      copy the "...Install.zip" it makes back here, unzip it, run Install.bat,
echo      then run this file again.
pause
exit /b 1

REM ---------------------------------------------------------------------------
REM Finds a real Python 3 (py launcher, PATH, or the usual install folders) and
REM puts its full path in PY. The Microsoft Store "python" shortcut is skipped.
:findpython
REM Prefers a Python that already has PySide6.
set "PY="
set "PYANY="
for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do call :trypy "%%P"
for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do call :trypy "%%P"
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%LOCALAPPDATA%\Python\pythoncore-3*" "%ProgramFiles%\Python3*") do call :trypy "%%~D\python.exe"
if defined PY exit /b 0
if defined PYANY (
    set "PY=%PYANY%"
    exit /b 0
)
echo.
echo Python 3 was not found on this PC (the Microsoft Store shortcut doesn't count).
echo Install Python 3 from python.org and tick "Add python.exe to PATH", then run this again.
exit /b 1

:trypy
if defined PY exit /b 0
if not exist "%~1" exit /b 0
if not defined PYANY set "PYANY=%~1"
"%~1" -c "import PySide6.QtWidgets" >nul 2>nul && set "PY=%~1"
exit /b 0

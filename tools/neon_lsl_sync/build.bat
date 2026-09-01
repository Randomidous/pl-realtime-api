@echo off
REM Build NeonLSLSync.exe from source.
REM Run this from a normal Windows Command Prompt / PowerShell, in this folder.
REM
REM If "python" on your PATH resolves to conda/Miniforge/Anaconda (a "(base)"
REM prompt is the tell) or the Microsoft Store, PASS THE RIGHT INTERPRETER
REM EXPLICITLY as the first argument instead of fighting PATH order, e.g.:
REM   build.bat "py -3.12"
REM   build.bat "C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"
REM Run "py -0p" first to see every python.org install the launcher knows
REM about (this does not depend on PATH, so conda can't shadow it).

setlocal

echo === Neon LSL Sync - Windows build ===

set PY=%~1
if "%PY%"=="" set PY=python
set PY_WAS_EXPLICIT=%~1

where %PY% >nul 2>nul
if errorlevel 1 (
    REM "where" doesn't understand "py -3.12" as one token; only bail out
    REM here if PY is a single bare command that truly isn't found at all.
    echo %PY%| findstr /C:" " >nul
    if errorlevel 1 (
        echo Could not find "%PY%" on PATH.
        echo Run "py -0p" to list installed Pythons, then either:
        echo   - install Python from https://www.python.org/downloads/
        echo     ^(check "Add python.exe to PATH" during setup^), or
        echo   - re-run this script with the interpreter to use, e.g.:
        echo       build.bat "py -3.12"
        pause
        exit /b 1
    )
)

echo.
echo [0/4] Checking which Python this will build with ...
for /f "delims=" %%P in ('%PY% -c "import sys; print(sys.executable)" 2^>nul') do set PYEXE=%%P
if "%PYEXE%"=="" (
    echo Could not run "%PY%". Double check the value you passed in.
    pause
    exit /b 1
)
echo   Using: %PYEXE%

if not "%PY_WAS_EXPLICIT%"=="" (
    echo   ^(explicitly requested via argument - skipping the conda/Store check^)
    goto :venv
)

set BADPYTHON=0
echo %PYEXE%| findstr /I "WindowsApps" >nul
if not errorlevel 1 set BADPYTHON=1
for /f "delims=" %%C in ('%PY% -c "import sys, os; print(1 if os.path.isdir(os.path.join(sys.prefix, 'conda-meta')) else 0)"') do set ISCONDA=%%C
if "%ISCONDA%"=="1" set BADPYTHON=1

if "%BADPYTHON%"=="1" (
    echo.
    echo *** WARNING: this Python is either the Microsoft Store build or a ***
    echo *** conda / Miniforge / Anaconda environment.                    ***
    echo.
    echo Both are known to ship a Tcl/Tk DLL and Tcl/Tk script library at
    echo mismatched versions ^(conda packages "python" and "tk" separately,
    echo and they can drift^). PyInstaller then freezes the mismatched pair
    echo and the exe fails at startup with:
    echo   "version conflict for package Tcl: have X.X.X, need exactly Y.Y.Y"
    echo.
    echo Run "py -0p" to see the exact path of every python.org install on
    echo this machine ^(this does not depend on PATH, so conda can't hide
    echo it^), then re-run this script as, e.g.:
    echo   build.bat "py -3.12"
    echo or with the full path it printed. Delete the .venv folder in this
    echo directory first if one already exists.
    echo.
    pause
    exit /b 1
)

:venv
echo.
echo [1/4] Creating a clean virtual environment in .venv ...
if exist .venv (
    echo   .venv already exists, reusing it.
) else (
    %PY% -m venv .venv
    if errorlevel 1 goto :error
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto :error

echo.
echo [2/4] Installing dependencies ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo [3/4] Running PyInstaller ...
REM --collect-all pylsl    : bundles the native liblsl shared library, which
REM                          PyInstaller's import analysis does not find on its own.
REM --collect-all zeroconf  : device discovery depends on zeroconf's platform
REM                          backends, which are easy to miss otherwise.
pyinstaller --onefile --windowed --name NeonLSLSync ^
    --collect-all pylsl ^
    --collect-all zeroconf ^
    --hidden-import=pupil_labs.realtime_api ^
    --noconfirm ^
    app.py
if errorlevel 1 goto :error

echo.
echo [4/4] Done.
echo Your exe is at: %cd%\dist\NeonLSLSync.exe
echo.
echo IMPORTANT: run it once from this same Command Prompt window first
echo   (dist\NeonLSLSync.exe)
echo so you can see any error output, before double-clicking it normally -
echo a --windowed exe shows no console, so a crash on first run would
echo otherwise look like nothing happened.
echo.
pause
exit /b 0

:error
echo.
echo Build failed - see the error above.
pause
exit /b 1

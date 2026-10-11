@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Build Video Archiver

set "SRC=%~f0"
set "WORK=%TEMP%\yta-build"
set "WG=--silent"

echo.
echo  Build Video Archiver
echo  -------------------
echo  Downloads yt-dlp, FFmpeg and Deno from their official GitHub releases and
echo  packs them with the app, then installs Video Archiver with its own desktop and
echo  Start menu shortcuts.
echo  Takes about 5-10 minutes. Close Video Archiver first if it is open.
echo.

rmdir /s /q "%WORK%" 2>nul
mkdir "%WORK%" 2>nul
if not exist "%WORK%" goto :fail_work

echo [1/4] Unpacking build files...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=[IO.File]::ReadAllText($env:SRC); $b='#=== BUILD' + ' SCRIPT ===#'; $a='#=== APP' + ' SOURCE ===#'; $i=$c.LastIndexOf($b); $j=$c.LastIndexOf($a); if($i -lt 0 -or $j -lt $i){exit 1}; [IO.File]::WriteAllText((Join-Path $env:WORK 'build.py'), $c.Substring($i+$b.Length, $j-$i-$b.Length).Trim([char]13,[char]10)); [IO.File]::WriteAllText((Join-Path $env:WORK 'video_archiver.py'), $c.Substring($j+$a.Length).TrimStart([char]13,[char]10)); exit 0"
if errorlevel 1 goto :fail_unpack

echo [2/4] Checking Python 3.11 or newer...
call :findpy
if defined PY goto :have_py
where winget >nul 2>nul || goto :fail_winget
echo       No suitable Python found. Installing Python 3.13...
echo       winget may show license terms first. Read them and type Y to agree.
winget install -e --id Python.Python.3.13 --scope user %WG%
call :findpy
if not defined PY goto :fail_python
:have_py
echo       Using "%PY%"

echo [3/4] Installing the packaging tool PyInstaller 6.22.3, temporarily...
"%PY%" -m venv "%WORK%\venv"
if errorlevel 1 goto :fail_venv
"%WORK%\venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check "pyinstaller==6.22.3"
if errorlevel 1 goto :fail_pip

echo [4/4] Building...
"%WORK%\venv\Scripts\python.exe" "%WORK%\build.py" "%WORK%"
if errorlevel 1 goto :fail_build

cd /d "%TEMP%"
rmdir /s /q "%WORK%" 2>nul
echo.
echo  Done. Video Archiver is installed and opening now. Start it from its desktop
echo  shortcut or the Start menu; uninstall it from Settings ^> Apps.
echo  You can delete this builder file.
echo.
pause
exit /b 0


:findpy
set "PY="
for %%V in (314 313 312 311) do if not defined PY call :trypy "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
if not defined PY for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :trypy "%%P"
if not defined PY for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :trypy "%%P"
exit /b 0

:trypy
rem Accept a Python only if it is 3.11+ and has tkinter, which the app window needs.
if not exist "%~1" exit /b 0
"%~1" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=%~1"
exit /b 0

:fail_work
echo.
echo  Could not create the temporary build folder "%WORK%".
goto :end_fail

:fail_unpack
echo.
echo  Could not unpack the build files. If this file is inside a ZIP, extract it first.
goto :end_fail

:fail_winget
echo.
echo  Python 3.11+ is needed to build, and "winget" is missing to install it.
echo  Install Python from python.org, then run this file again.
goto :end_fail

:fail_python
echo.
echo  Python could not be installed automatically.
echo  Install Python 3.11 or newer from python.org, then run this file again.
goto :end_fail

:fail_venv
echo.
echo  Could not create a temporary Python environment.
goto :end_fail

:fail_pip
echo.
echo  Could not install PyInstaller. Check your internet connection and try again.
goto :end_fail

:fail_build
echo.
echo  The build stopped. The reason is shown above.
goto :end_fail

:end_fail
echo.
pause
exit /b 1


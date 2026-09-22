@echo off
setlocal enabledelayedexpansion

if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
del /q *.spec 2>nul

set EXTRA=
if exist icon.ico set EXTRA=!EXTRA! --icon=icon.ico

pyinstaller ^
  --onefile ^
  --noconsole ^
  --clean ^
  --name "MSU2_Console" ^
  !EXTRA! ^
  --collect-submodules=msu2_core ^
  --collect-submodules=widgets ^
  --add-data "msu2_core;msu2_core" ^
  --add-data "widgets;widgets" ^
  --hidden-import=pyserial ^
  --hidden-import=serial ^
  --hidden-import=serial.tools.list_ports ^
  --hidden-import=PIL ^
  --hidden-import=PIL.Image ^
  --hidden-import=PIL.ImageDraw ^
  --hidden-import=PIL.ImageFont ^
  --hidden-import=psutil ^
  --hidden-import=pynput ^
  --hidden-import=pynput.keyboard ^
  --hidden-import=pynput.mouse ^
  --optimize=1 ^
  msu2_mini.py

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

echo.
echo succeed
dir dist
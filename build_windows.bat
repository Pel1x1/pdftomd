@echo off
rem Сборка PDF2MD.exe для Windows -> dist\PDF2MD.exe
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv 2>nul
  if errorlevel 1 python -m venv .venv
)
.venv\Scripts\python -m pip install -q -U pip
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python -m PyInstaller --noconfirm --clean pdf2md.spec
echo.
echo Готово: dist\PDF2MD.exe
pause

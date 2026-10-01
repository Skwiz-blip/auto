@echo off
rem Construit installateur\sortie\Setup-Controle-KIK.exe (environ 1,5 Go libres nécessaires).
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] Outils de construction...
python -m pip install --quiet pyinstaller
if not exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" if not exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" (
  winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
)
set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" (echo [X] Inno Setup introuvable. & pause & exit /b 1)

echo [2/4] Modeles PP-OCR (telecharges une fois, puis embarques dans l'application)...
python -m pip install --quiet -r ..\requirements.txt
python -c "from rapidocr import RapidOCR; RapidOCR()" >nul 2>&1 || (echo [X] Modeles PP-OCR indisponibles. & pause & exit /b 1)

echo [3/4] Application (PyInstaller)...
python -m PyInstaller --noconfirm --clean --distpath dist --workpath tmp\build controle_kik.spec
if errorlevel 1 (echo [X] PyInstaller a echoue. & pause & exit /b 1)

echo [4/4] Installateur (Inno Setup)...
"%ISCC%" installateur.iss
if errorlevel 1 (echo [X] Inno Setup a echoue. & pause & exit /b 1)

rmdir /s /q tmp
echo.
echo Termine : installateur\sortie\Setup-Controle-KIK.exe
pause

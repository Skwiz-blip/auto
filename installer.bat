@echo off
rem Prépare une nouvelle machine : bibliothèques Python et vérification de Tesseract.
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo [X] Python est introuvable. Installez Python 3.13 depuis https://www.python.org
  echo     en cochant "Add python.exe to PATH", puis relancez ce fichier.
  pause
  exit /b 1
)

echo Installation des bibliotheques Python...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [X] L'installation a echoue : verifiez la connexion Internet.
  pause
  exit /b 1
)

if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
  echo [OK] Tesseract OCR est installe.
) else (
  echo [X] Tesseract OCR manque. Installez-le depuis
  echo     https://github.com/UB-Mannheim/tesseract/wiki
  echo     dans le dossier propose par defaut : C:\Program Files\Tesseract-OCR
)

python -c "from pyzbar import pyzbar" >nul 2>&1
if errorlevel 1 (
  echo [X] Le lecteur de QR code ne demarre pas : installez
  echo     "Visual C++ Redistributable 2013 (x64)" depuis le site de Microsoft.
) else (
  echo [OK] Lecteur de QR code pret.
)

echo.
echo Termine. Ouvrez l'application puis collez la cle API dans Parametres.
pause

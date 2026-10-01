@echo off
rem Prépare une nouvelle machine : bibliothèques Python (OCR PP-OCR et lecteur de QR compris).
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

echo Preparation de la lecture des documents (PP-OCR, environ 30 Mo au premier lancement)...
python -c "from rapidocr import RapidOCR; RapidOCR()" >nul 2>&1
if errorlevel 1 (
  echo [X] La lecture des documents ne demarre pas : verifiez la connexion Internet.
) else (
  echo [OK] Lecture des documents prete.
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
